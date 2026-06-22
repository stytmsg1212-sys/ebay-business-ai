"""eBaymag ページ reload → CSRF 更新 → graphql 疎通テスト (read-only)。

graphql が HTML を返した (CSRF失効/レート制限) ときの回復確認用。
疎通すれば 4件 fix を再開してよい。
"""
import sys, time
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
    print("reloading", page.url)
    page.reload(wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    csrf = page.evaluate("() => (document.querySelector('meta[name=csrf-token]')||{}).content || ''")
    print(f"csrf token len: {len(csrf)}")
    try:
        profs = G.list_profiles(page, first=5)
        print(f"graphql OK: {len(profs)} profiles (疎通回復)")
    except Exception as e:
        print(f"graphql NG: {str(e)[:160]}")
