"""eBaymag: 指定 URL へ遷移予約 (setTimeout 方式、観察は別プロセス).

usage: python ebaymag_goto_2026_06_10.py "https://ebaymag.com/stock?name=91512-1&productId=718746739"
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"
url = sys.argv[1]

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0]
    target = next((pg for pg in ctx.pages if "ebaymag.com" in pg.url), None)
    if target is None:
        print("eBaymag タブなし")
        sys.exit(1)
    print("URL(前):", target.url[:120])
    target.evaluate(
        "url => { setTimeout(() => { location.href = url; }, 100); }", url)
    print("遷移予約 OK →", url[:120])
