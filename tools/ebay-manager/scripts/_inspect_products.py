"""eBaymag 全 product の shippingProfileId 分布を確認 (read-only)。

DDP に残る商品が「未割当の product」か「numberOfProducts=各国版listing延べ数」かを切り分け。
完了判断の再検証。
"""
import sys
sys.path.insert(0, '.')
from collections import Counter
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
        nodes = conn.get("nodes") or []
        all_prods.extend(nodes)
        pi = conn.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")
    print(f"total products (GraphQL): {len(all_prods)}")

    profs = G.list_profiles(page, first=200)
    id2title = {str(x["id"]): x["title"] for x in profs}
    c = Counter(str(x.get("shippingProfileId")) for x in all_prods)
    for pid, n in sorted(c.items(), key=lambda kv: -kv[1]):
        title = id2title.get(pid.split(":")[-1], pid)
        flag = " <-- DDP/未MAG" if not title.startswith("MAG_") else ""
        print(f"  {title}: {n} products{flag}")
