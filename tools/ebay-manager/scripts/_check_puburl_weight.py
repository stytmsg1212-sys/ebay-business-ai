"""publicationUrl 末尾の eBay 実 ItemID で DB weight を取得できるか検証 (read-only)。

listings.id は eBaymag 内部ID。eBay 実 ItemID は publicationUrl 末尾の数字。
これで DB ebay_listings.weight_g に紐付くか確認 (89商品の処理可否)。
"""
import sys, re
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G
from monitor import database as db

Q = """query Products($first: Int, $after: String){
  products(first: $first, after: $after){
    nodes { id shippingProfileId listings { site { id } publicationUrl } }
    pageInfo { hasNextPage endCursor }
  }
}"""


def item_id_from_url(url):
    m = re.search(r"(\d+)/?$", url or "")
    return m.group(1) if m else None


def us_item_id(prod):
    for li in (prod.get("listings") or []):
        if str((li.get("site") or {}).get("id")) == "0":
            return item_id_from_url(li.get("publicationUrl"))
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
    got = no_db = 0
    for x in unmag:
        iid = us_item_id(x)
        w = wmap.get(iid) if iid else None
        if w is not None and w > 0:
            got += 1
        else:
            no_db += 1
    print(f"未MAG={len(unmag)} / 実ItemIDでDB weight取得可={got} / DB未登録={no_db}")
    for x in unmag[:6]:
        iid = us_item_id(x)
        print(f"  product {x['id']} 実ItemID={iid} weight={wmap.get(iid)}")
