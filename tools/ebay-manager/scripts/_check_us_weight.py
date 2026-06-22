"""未MAG 89商品の primary US listing id (eBay item_id) で DB weight を取得できるか検証。

listing.id (各国版 eBay ListingID) の primary/siteId=0 が US 本体 item_id。
これを DB ebay_listings.weight_g と照合 (user 提案: US側情報で帯決定)。read-only。
"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G
from monitor import database as db

Q = """query Products($first: Int, $after: String){
  products(first: $first, after: $after){
    nodes { id shippingProfileId listings { id primary site { id } } }
    pageInfo { hasNextPage endCursor }
  }
}"""


def us_item_id(prod):
    for li in (prod.get("listings") or []):
        if str((li.get("site") or {}).get("id")) == "0" or li.get("primary"):
            return str(li.get("id"))
    return None


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

    def is_mag(x):
        pid = str(x.get("shippingProfileId") or "").split(":")[-1]
        return id2title.get(pid, "").startswith("MAG_")

    with db.get_conn() as c:
        wmap = {str(r["ebay_item_id"]): r["weight_g"]
                for r in c.execute("SELECT ebay_item_id, weight_g FROM ebay_listings").fetchall()}

    unmag = [x for x in all_prods if not is_mag(x)]
    got = no_us = no_db = 0
    for x in unmag:
        uid = us_item_id(x)
        if not uid:
            no_us += 1
            continue
        w = wmap.get(uid)
        if w is not None and w > 0:
            got += 1
        else:
            no_db += 1
    print(f"未MAG={len(unmag)} / US item_id で weight取得可={got} / US無={no_us} / DB未登録={no_db}")
    for x in unmag[:6]:
        uid = us_item_id(x)
        print(f"  product {x['id']} US item_id={uid} weight={wmap.get(uid)}")
