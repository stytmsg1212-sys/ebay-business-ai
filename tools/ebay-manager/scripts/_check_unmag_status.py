"""未MAG (productid=None) の product が active か archived かを確認 (read-only)。

archived/published/imported/totalQuantity で「送料漏れ対象 (active出品)」か
「対象外 (アーカイブ/未公開)」かを判別。完了判断の決め手。
"""
import sys
sys.path.insert(0, '.')
from collections import Counter
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G

PRODUCTS_Q = """query Products($first: Int, $after: String){
  products(first: $first, after: $after){
    nodes { id productid shippingProfileId archived imported totalQuantity soldOff }
    pageInfo { hasNextPage endCursor }
  }
}"""

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
        d = G.gql(page, "Products", PRODUCTS_Q, {"first": 100, "after": after})
        conn = d.get("products") or {}
        all_prods.extend(conn.get("nodes") or [])
        pi = conn.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")

    profs = G.list_profiles(page, first=200)
    id2title = {str(x["id"]): x["title"] for x in profs}

    def is_mag(x):
        pid = str(x.get("shippingProfileId") or "").split(":")[-1]
        return id2title.get(pid, "").startswith("MAG_")

    unmag = [x for x in all_prods if not is_mag(x)]
    print(f"total={len(all_prods)} / 未MAG={len(unmag)}")
    print("未MAG の状態分布:")
    print("  archived:", Counter(x.get("archived") for x in unmag))
    print("  imported:", Counter(x.get("imported") for x in unmag))
    print("  soldOff:", Counter(x.get("soldOff") for x in unmag))
    print("  productid None:", sum(1 for x in unmag if x.get("productid") is None))
    print("  totalQuantity=0:", sum(1 for x in unmag if (x.get("totalQuantity") or 0) == 0))
    # 送料漏れ対象 = アーカイブされておらず productid (eBay連携) がある未MAG
    active_leak = [x for x in unmag if not x.get("archived") and x.get("productid")]
    print(f"\n=== 送料漏れ対象 (非archived かつ productid あり) の未MAG: {len(active_leak)} ===")
    for x in active_leak[:10]:
        print(f"  id={x['id']} productid={x.get('productid')} archived={x.get('archived')}")
