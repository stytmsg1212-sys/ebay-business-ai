"""W317 Phase 0 probe (read-only): eBaymag GraphQL products の pagination 型 +
publicationUrl→eBay item id map で awaiting_import 滞留 7 件が拾えるか実測する。

CDP Chrome (port 9222) 既存タブに attach するのみ。kill/relaunch/新規 goto はしない
(reference_claude_dedicated_chrome.md 規約)。mutation は一切行わない。
"""
import sys
import re
import json

sys.path.insert(0, '.')
sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G

# 依頼ボード滞留 (awaiting_import) 7 件: id -> ebay_item_id (DB SELECT で確認済)
TARGET = {
    5: "358689688709",
    6: "358724549446",
    8: "358663924423",
    9: "358663938263",
    10: "358663940394",
    13: "358663696382",
    36: "358738647421",
}

ITEMID_RE = re.compile(r"/(\d{9,})(?:\D|$)")


def itemid_from_url(u):
    if not u:
        return None
    m = ITEMID_RE.search(u)
    return m.group(1) if m else None


# --- Q1: introspection で products connection の pagination 型を確認 ---
INTROSPECT_Q = """query IntrospectProducts {
  __schema {
    queryType {
      fields(includeDeprecated: true) {
        name
        args { name type { name kind ofType { name kind } } }
        type { name kind ofType { name kind ofType { name kind } } }
      }
    }
  }
}"""

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    ctx = b.contexts[0]
    page = next(
        (pg for pg in ctx.pages if "ebaymag.com" in (pg.url or "") and "ebaymag.com/login" not in (pg.url or "")),
        None,
    )
    if page is None:
        print("PROBE_FAIL: ebaymag.com タブが見つかりません (login のみの可能性)")
        for pg in ctx.pages:
            print("  page:", pg.url)
        sys.exit(1)

    print(f"[attach] page.url = {page.url}")

    # session 生存確認 (W293 権威化パターン: list_profiles で例外なければ alive)
    try:
        profs = G.list_profiles(page, first=5)
        print(f"[session] alive, profiles sample n={len(profs)}")
    except Exception as e:
        print(f"PROBE_FAIL: session dead or GraphQL error: {e}")
        sys.exit(1)

    # --- Q1: introspection ---
    try:
        idata = G.gql(page, "IntrospectProducts", INTROSPECT_Q, {})
        fields = (idata.get("__schema") or {}).get("queryType", {}).get("fields", [])
        products_field = next((f for f in fields if f["name"] == "products"), None)
        print("\n=== Q1: introspection (products field) ===")
        if products_field:
            print(json.dumps(products_field, ensure_ascii=False, indent=2))
        else:
            print("products field not found in introspection (schema かもしれない: introspection disabled)")
    except Exception as e:
        print(f"[introspection] 失敗 (schema introspection 無効の可能性): {e}")

    # introspect ProductConnection type directly for pageInfo/edges
    TYPE_Q = """query T($name: String!){ __type(name: $name){ name kind fields{ name type{ name kind ofType{name kind} } } } }"""
    for tname in ("ProductConnection", "products_connection", "ProductsConnection"):
        try:
            td = G.gql(page, "T", TYPE_Q, {"name": tname})
            t = td.get("__type")
            if t:
                print(f"\n[type introspect] {tname}:")
                print(json.dumps(t, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"[type introspect {tname}] error: {e}")

    # --- 実クエリで pageInfo/after を試す ---
    # filters: null (省略と等価) で archived true+false 両方を含む全件を取得する。
    # 実測 (2026-07-05): filters=None(未指定相当) -> totalCount=823 / filters={}
    # (空 object) -> 218 (=archived:false 相当) / filters={archived:true} -> 605。
    # 823 = 605+218 で filters 省略が全件corpusと確定。
    Q_WITH_PAGEINFO = """query Products($first: Int, $after: String, $filters: ProductFilterInput){
  products(first: $first, after: $after, filters: $filters){
    totalCount
    pageInfo { endCursor hasNextPage }
    nodes { id title shippingProfileId listings { site { id } publicationUrl } }
  }
}"""
    pagination_style = None
    try:
        d = G.gql(page, "Products", Q_WITH_PAGEINFO, {"first": 5, "after": None, "filters": None})
        prod = d.get("products") or {}
        print(f"totalCount (filters=null) = {prod.get('totalCount')}")
        pi = prod.get("pageInfo")
        print(f"\n=== Q1 実クエリ結果: pageInfo あり = {pi is not None} ===")
        print("pageInfo sample:", pi)
        pagination_style = "relay_pageinfo"
    except Exception as e:
        print(f"\n[pageInfo query] 失敗: {e} → first 上限方式にフォールバック試行")
        pagination_style = "first_only"

    # --- 全商品取得 ---
    all_nodes = []
    if pagination_style == "relay_pageinfo":
        after = None
        page_no = 0
        while True:
            page_no += 1
            d = G.gql(page, "Products", Q_WITH_PAGEINFO, {"first": 200, "after": after, "filters": None})
            prod = d.get("products") or {}
            nodes = prod.get("nodes") or []
            all_nodes.extend(nodes)
            pi = prod.get("pageInfo") or {}
            print(f"  page {page_no}: +{len(nodes)} nodes (total={len(all_nodes)}) hasNextPage={pi.get('hasNextPage')}")
            if not pi.get("hasNextPage") or not pi.get("endCursor"):
                break
            after = pi.get("endCursor")
            if page_no > 30:
                print("  [guard] 30 page 到達で打ち切り (無限ループ防止)")
                break
    else:
        # first 上限を実測 (段階的に増やして打ち切り件数を見る)
        Q_FIRST_ONLY = """query Products($first: Int){
  products(first: $first){ nodes { id title shippingProfileId listings { site { id } publicationUrl } } }
}"""
        for n in (100, 500, 1000, 2000):
            d = G.gql(page, "Products", Q_FIRST_ONLY, {"first": n})
            nodes = (d.get("products") or {}).get("nodes") or []
            print(f"  first={n}: nodes={len(nodes)}")
            if len(nodes) > len(all_nodes):
                all_nodes = nodes
            if len(nodes) < n:
                break  # 全件取得できた (n 未満 = 末端到達)

    print(f"\n=== 総商品数 (nodes) = {len(all_nodes)} ===")

    # --- Q2/Q3: item_id map 構築 + 統計 ---
    itemid_map = {}  # item_id -> list of (product_id, site_id)
    collisions = {}
    total_listing_urls = 0
    total_extracted = 0
    for node in all_nodes:
        pid = node.get("id")
        for li in (node.get("listings") or []):
            total_listing_urls += 1
            url = li.get("publicationUrl")
            iid = itemid_from_url(url)
            if not iid:
                continue
            total_extracted += 1
            sid = str((li.get("site") or {}).get("id"))
            itemid_map.setdefault(iid, []).append((pid, sid))

    for iid, entries in itemid_map.items():
        pids = set(e[0] for e in entries)
        if len(pids) > 1:
            collisions[iid] = entries

    print(f"\n=== map 統計 ===")
    print(f"総 listing URL 数 = {total_listing_urls}")
    print(f"item_id 抽出成功 = {total_extracted} (成功率 {total_extracted/max(total_listing_urls,1)*100:.1f}%)")
    print(f"unique item_id 数 = {len(itemid_map)}")
    print(f"item_id 衝突 (複数 product_id) 件数 = {len(collisions)}")
    if collisions:
        for iid, entries in list(collisions.items())[:5]:
            print(f"  衝突例: item_id={iid} entries={entries}")

    print(f"\n=== Q2/Q3: 滞留 7 件の出現確認 ===")
    for job_id, eid in TARGET.items():
        entries = itemid_map.get(eid)
        if not entries:
            print(f"  job={job_id} eid={eid}: MISS (map に出現なし)")
            continue
        pids = set(e[0] for e in entries)
        sites = [e[1] for e in entries]
        has_us = "0" in sites
        print(f"  job={job_id} eid={eid}: HIT product_id(s)={pids} sites={sites} has_US={has_us}")

print("\nPROBE_DONE")
