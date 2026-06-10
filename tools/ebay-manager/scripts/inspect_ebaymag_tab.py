"""eBaymag タブの現在状態を read-only で観察 (URL / 本文 / screenshot).

ナビゲーションは一切しない (user が見ている画面をそのまま読む)。

実行: python scripts/inspect_ebaymag_tab.py
"""
import sys

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"
SHOT = "data/ebaymag_tab_now.png"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0]
    target = None
    for pg in ctx.pages:
        if "ebaymag.com" in pg.url:
            target = pg
            break
    if target is None:
        print("eBaymag タブが見つからない。全タブ:")
        for pg in ctx.pages:
            print(" ", pg.url[:120])
        sys.exit(1)

    print(f"URL: {target.url}")
    try:
        target.screenshot(path=SHOT, timeout=15000)
        print(f"screenshot: {SHOT}")
    except Exception as e:
        print(f"screenshot 失敗: {e}")

    try:
        txt = target.evaluate("() => document.body ? document.body.innerText : ''")
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        print(f"=== 本文テキスト (全 {len(lines)} 行) ===")
        for ln in lines[:100]:
            print(" ", ln)
    except Exception as e:
        print(f"innerText 失敗: {e}")
