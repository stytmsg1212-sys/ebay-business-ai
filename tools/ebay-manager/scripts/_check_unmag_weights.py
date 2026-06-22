"""未MAG (DDP等) の product が DB の weight と紐付くか検証 (read-only)。

Product.productid が eBay item_id なら DB ebay_listings.weight_g と照合でき、
既存パイプライン (band判定→割当→値設定) を89商品に流用できる。
"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G
from monitor import database as db

PRODUCTS_Q = """query Products($first: Int, $after: String){
  products(first: $first, after: $after){
    nodes { id productid shippingProfileId }
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

    with db.get_conn() as c:
        wmap = {r["ebay_item_id"]: r["weight_g"]
                for r in c.execute("SELECT ebay_item_id, weight_g FROM ebay_listings").fetchall()}

    have = 0
    for x in unmag:
        pid = str(x.get("productid"))
        w = wmap.get(pid)
        if w is not None and w > 0:
            have += 1
    print(f"DB ebay_listings に weight>0 がある未MAG: {have}/{len(unmag)}")
    print("サンプル:")
    for x in unmag[:8]:
        pid = str(x.get("productid"))
        print(f"  product id={x['id']} productid={pid} weight_g={wmap.get(pid)}")
