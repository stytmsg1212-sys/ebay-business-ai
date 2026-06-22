"""未MAG 1-2商品の listings を dump (read-only)。productId が eBay item_id か確認。"""
import sys, json
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G

Q = """query Products($first: Int){
  products(first: $first){
    nodes { id title shippingProfileId
            listings { id productId primary title site { id } } }
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
    profs = G.list_profiles(page, first=200)
    id2title = {str(x["id"]): x["title"] for x in profs}
    d = G.gql(page, "Products", Q, {"first": 100})
    nodes = (d.get("products") or {}).get("nodes") or []
    for x in nodes:
        pid = str(x.get("shippingProfileId") or "").split(":")[-1]
        if id2title.get(pid, "").startswith("MAG_"):
            continue
        print(f"\nproduct {x['id']} ({str(x.get('title'))[:40]}) policy={id2title.get(pid, pid)}")
        for li in (x.get("listings") or []):
            site = li.get("site") or {}
            print(f"  listing id={li.get('id')} productId={li.get('productId')} "
                  f"primary={li.get('primary')} siteId={site.get('id')}")
        break  # 1商品だけ
