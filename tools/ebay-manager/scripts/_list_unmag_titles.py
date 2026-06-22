"""未MAG 89商品の title を確認し、処理済み(MAG)商品との関係を調べる (read-only)。

title が処理済み商品と重複 → variation の可能性。独立 title → 独立商品。
"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G

PRODUCTS_Q = """query Products($first: Int, $after: String){
  products(first: $first, after: $after){
    nodes { id title shippingProfileId }
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

    mag_titles = {x.get("title") for x in all_prods if is_mag(x)}
    unmag = [x for x in all_prods if not is_mag(x)]
    dup = [x for x in unmag if x.get("title") in mag_titles]
    uniq = [x for x in unmag if x.get("title") not in mag_titles]
    print(f"未MAG={len(unmag)} / うち処理済みと同title={len(dup)} / 独立title={len(uniq)}")
    print("\n未MAG title サンプル (先頭15):")
    for x in unmag[:15]:
        same = " [MAGと同title=variation疑い]" if x.get("title") in mag_titles else ""
        print(f"  id={x['id']} {str(x.get('title'))[:55]}{same}")
