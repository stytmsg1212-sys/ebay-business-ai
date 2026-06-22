"""Product.listings の要素型と、その中の eBay item_id / siteId フィールドを確認 (read-only)。

89商品の US listing item_id を取得 → DB weight 紐付けの前提。
"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G

# Product.listings の要素型名を深掘り
Q_PROD = """query IntrospectProductListings {
  __type(name: "Product") {
    fields { name type { kind name ofType { kind name ofType { kind name } } } }
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

    d = G.gql(page, "IntrospectProductListings", Q_PROD, {})
    elem_type = None
    for f in ((d.get("__type") or {}).get("fields") or []):
        if f["name"] == "listings":
            t = f["type"]
            # LIST -> ofType -> (NON_NULL ->) 要素型
            inner = t.get("ofType") or {}
            elem_type = inner.get("name") or (inner.get("ofType") or {}).get("name")
            print(f"listings type: {t.get('kind')} -> elem={elem_type}")
            break

    if elem_type:
        Q_ELEM = ("query IntrospectListingElem { __type(name: \"%s\") "
                  "{ fields { name type { kind name ofType { kind name } } } } }" % elem_type)
        d2 = G.gql(page, "IntrospectListingElem", Q_ELEM, {})
        print(f"\n{elem_type} fields:")
        for f in ((d2.get("__type") or {}).get("fields") or []):
            t = f.get("type") or {}
            tn = t.get("name") or (t.get("ofType") or {}).get("name") or t.get("kind")
            print(f"  {f['name']}: {tn}")
