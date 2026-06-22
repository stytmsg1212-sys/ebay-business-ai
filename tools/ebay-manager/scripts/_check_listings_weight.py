"""未MAG 89商品の listings.productId を DB ebay_listings.weight_g に紐付けられるか検証 (read-only)。

各 eBaymag product の listings (各国版) の productId のうち、DB に存在する=US本体の
item_id を見つけ、その weight_g を取得できるか確認 (user 提案: US側情報で帯を決める)。
"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G
from monitor import database as db

PRODUCTS_Q = """query Products($first: Int, $after: String){
  products(first: $first, after: $after){
    nodes { id title shippingProfileId listings { id productId primary } }
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

    with db.get_conn() as c:
        wmap = {str(r["ebay_item_id"]): r["weight_g"]
                for r in c.execute("SELECT ebay_item_id, weight_g FROM ebay_listings").fetchall()}

    unmag = [x for x in all_prods if not is_mag(x)]
    got = 0
    samples = []
    for x in unmag:
        ls = x.get("listings") or []
        # listings の productId のうち DB に存在し weight>0 のものを探す
        w = None
        used = None
        for li in ls:
            pid = str(li.get("productId"))
            if pid in wmap and (wmap[pid] or 0) > 0:
                w = wmap[pid]
                used = pid
                break
        if w:
            got += 1
        if len(samples) < 8:
            samples.append((x["id"], len(ls), used, w))
    print(f"未MAG={len(unmag)} / listings経由でweight取得可: {got}")
    print("サンプル (product_id, listings数, 使ったitem_id, weight):")
    for s in samples:
        print(f"  {s}")
