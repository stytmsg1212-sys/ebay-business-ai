"""未MAG 89商品の listings.publicationUrl を確認 (read-only)。

eBay listing が生きている (active) か、削除済み (対象外) かの最終切り分け。
"""
import sys
sys.path.insert(0, '.')
from collections import Counter
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G

Q = """query Products($first: Int, $after: String){
  products(first: $first, after: $after){
    nodes { id shippingProfileId listings { id site { id } publicationUrl } }
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

    unmag = [x for x in all_prods if not is_mag(x)]
    has_pub = no_pub = 0
    for x in unmag:
        us = next((li for li in (x.get("listings") or [])
                   if str((li.get("site") or {}).get("id")) == "0"), None)
        url = (us or {}).get("publicationUrl")
        if url:
            has_pub += 1
        else:
            no_pub += 1
    print(f"未MAG={len(unmag)} / US listing に publicationUrl あり={has_pub} / なし={no_pub}")
    print("サンプル:")
    for x in unmag[:6]:
        us = next((li for li in (x.get("listings") or [])
                   if str((li.get("site") or {}).get("id")) == "0"), None)
        print(f"  product {x['id']}: US pubUrl={(us or {}).get('publicationUrl')}")
