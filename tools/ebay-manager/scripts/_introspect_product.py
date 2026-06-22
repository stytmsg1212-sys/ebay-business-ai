"""eBaymag GraphQL の Product 型フィールドを introspection で確認 (read-only)。

DB 外の89商品の weight/handling/eBay item_id を取得する手段を探す。
"""
import sys, json
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G

INTRO = 'query IntrospectProduct { __type(name: "Product") { fields { name type { name kind ofType { name kind } } } } }'

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ebaymag" in (pg.url or ""):
                page = pg
    page.goto("https://ebaymag.com/shipping", wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(3000)
    try:
        d = G.gql(page, "IntrospectProduct", INTRO, {})
        fields = ((d.get("__type") or {}).get("fields")) or []
        print(f"Product fields ({len(fields)}):")
        for f in fields:
            t = f.get("type") or {}
            tn = t.get("name") or (t.get("ofType") or {}).get("name") or t.get("kind")
            print(f"  {f['name']}: {tn}")
    except Exception as e:
        print("introspection NG:", str(e)[:160])
