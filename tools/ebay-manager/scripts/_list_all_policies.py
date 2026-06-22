"""全 eBaymag 配送ポリシーの現状一覧 (read-only)。

クリーンアップ前の状況把握: B1DE/CAA2 (誤作成疑い) の商品数、旧 DDP の drain 状況、
MAG の商品数を確認。冒頭 reload で CSRF 更新。
"""
import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from monitor import ebaymag_graphql as G

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
    print(f"total {len(profs)} policies")
    mag = ddp = other = 0
    for x in sorted(profs, key=lambda v: v.get("title") or ""):
        t = x.get("title") or ""
        n = x.get("numberOfProducts")
        tag = "MAG" if t.startswith("MAG_") else ("DDP" if t.startswith("DDP") else "OTHER")
        if tag == "MAG":
            mag += 1
        elif tag == "DDP":
            ddp += 1
        else:
            other += 1
        print(f"  [{tag}] {t} id={x['id']} products={n}")
    print(f"\n=== MAG={mag} DDP={ddp} OTHER={other} ===")
