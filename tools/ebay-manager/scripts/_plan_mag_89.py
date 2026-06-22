"""未MAG 89商品の割当計画を作成 (read-only)。

weight = publicationUrl の実 eBay ItemID 経由で DB ebay_listings.weight_g。
handling = 実 ItemID の GetItem DispatchTimeMax (1/7)。
band×day → MAG_{帯}_{日}day。出力は data/mag_assignment_plan.json (89分で上書き=
既割当119は plan に含めない→_assign_mag_batch は89のみ処理、119は触らない)。
"""
import sys, json, re
import xml.etree.ElementTree as ET
import httpx
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G
from monitor import database as db
from monitor.credentials import get_ebay_credentials
from monitor.ebay_client import _resolve_active_token, TRADING_API_URL, API_VERSION
from monitor.ebaymag_policy_mapping import band_for_weight_g

NS = {"ns": "urn:ebay:apis:eBLBaseComponents"}
Q = """query Products($first: Int, $after: String){
  products(first: $first, after: $after){
    nodes { id shippingProfileId listings { site { id } publicationUrl } }
    pageInfo { hasNextPage endCursor }
  }
}"""

creds = get_ebay_credentials()
token = _resolve_active_token(creds["user_token"])
HEADERS = {
    "X-EBAY-API-SITEID": "0", "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
    "X-EBAY-API-CALL-NAME": "GetItem", "X-EBAY-API-APP-NAME": creds["app_id"],
    "X-EBAY-API-DEV-NAME": creds["dev_id"], "X-EBAY-API-CERT-NAME": creds["cert_id"],
    "Content-Type": "text/xml",
}


def item_id_from_url(url):
    m = re.search(r"(\d+)/?$", url or "")
    return m.group(1) if m else None


def us_item_id(prod):
    for li in (prod.get("listings") or []):
        if str((li.get("site") or {}).get("id")) == "0":
            return item_id_from_url(li.get("publicationUrl"))
    return None


def get_dispatch(item_id):
    xml = (f'<?xml version="1.0" encoding="utf-8"?>'
           f'<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
           f'<RequesterCredentials><eBayAuthToken>{token}</eBayAuthToken></RequesterCredentials>'
           f'<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel></GetItemRequest>')
    r = httpx.post(TRADING_API_URL, content=xml.encode("utf-8"), headers=HEADERS, timeout=30)
    root = ET.fromstring(r.text)
    if root.findtext("ns:Ack", namespaces=NS) not in ("Success", "Warning"):
        return None
    item = root.find(".//ns:Item", namespaces=NS)
    return item.findtext("ns:DispatchTimeMax", namespaces=NS) if item is not None else None


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                page = pg
    page.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(3000)

    all_prods, after = [], None
    for _ in range(30):
        d = G.gql(page, "Products", Q, {"first": 100, "after": after})
        conn = d.get("products") or {}
        all_prods.extend(conn.get("nodes") or [])
        pi = conn.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")

    profs = G.list_profiles(page, first=200)
    id2title = {str(x["id"]): x["title"] for x in profs}
    mag = {x["title"]: str(x["id"]) for x in profs if (x.get("title") or "").startswith("MAG_")}

    def is_mag(x):
        pid = str(x.get("shippingProfileId") or "").split(":")[-1]
        return id2title.get(pid, "").startswith("MAG_")

    with db.get_conn() as c:
        wmap = {str(r["ebay_item_id"]): r["weight_g"]
                for r in c.execute("SELECT ebay_item_id, weight_g FROM ebay_listings").fetchall()}

    unmag = [x for x in all_prods if not is_mag(x)]
    print(f"未MAG={len(unmag)} 件の handling を GetItem 取得中...", flush=True)
    plan, issues = [], []
    for i, x in enumerate(unmag):
        iid = us_item_id(x)
        wg = wmap.get(iid)
        if not iid or wg is None or wg <= 0:
            issues.append({"product_id": x["id"], "reason": f"item={iid} weight={wg}"})
            continue
        dtm = get_dispatch(iid)
        if dtm not in ("1", "7"):
            issues.append({"product_id": x["id"], "ebay_item_id": iid, "reason": f"dispatch={dtm}"})
            continue
        band = band_for_weight_g(wg)
        day = "1" if dtm == "1" else "7"
        title = f"MAG_{band}_{day}day"
        tid = mag.get(title)
        if not tid:
            issues.append({"product_id": x["id"], "reason": f"no policy {title}"})
            continue
        plan.append({"product_id": x["id"], "ebay_item_id": iid, "weight_g": wg,
                     "band": band, "dispatch": dtm, "target_title": title, "target_id": tid})
        if (i + 1) % 20 == 0:
            print(f"  progress {i + 1}/{len(unmag)}", flush=True)

    by_title = {}
    for x in plan:
        by_title[x["target_title"]] = by_title.get(x["target_title"], 0) + 1
    print(f"\n=== plan={len(plan)} issues={len(issues)} ===", flush=True)
    for t in sorted(by_title):
        print(f"  {t}: {by_title[t]}", flush=True)
    for it in issues[:15]:
        print(f"  ISSUE {it['product_id']}: {it['reason']}", flush=True)
    json.dump({"plan": plan, "issues": issues},
              open("data/mag_assignment_plan.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("saved data/mag_assignment_plan.json (89分)", flush=True)
