"""eBaymag: 詳細パネルの「アクション」メニューを開いて項目を列挙 (観察のみ).

開いた後、項目 text を dump して screenshot。項目クリックはしない。
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

OPEN_JS = r"""() => {
  const btns = Array.from(document.querySelectorAll('button'));
  const act = btns.find(b => b.innerText.trim() === 'アクション');
  if (!act) return 'アクション button not found';
  act.click();
  return 'OPENED';
}"""

MENU_JS = r"""() => {
  // メニュー項目候補: クリック後に出現した li / a / button / [role=menuitem]
  const items = Array.from(document.querySelectorAll(
    '[role="menuitem"], [role="menu"] *, li, a'))
    .map(el => el.innerText && el.innerText.trim())
    .filter(t => t && t.length < 40);
  return [...new Set(items)];
}"""

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0]
    target = next((pg for pg in ctx.pages if "ebaymag.com" in pg.url), None)
    if target is None:
        print("eBaymag タブなし")
        sys.exit(1)
    print("URL:", target.url[:110])
    print("開く:", target.evaluate(OPEN_JS))
    target.wait_for_timeout(1500)
    items = target.evaluate(MENU_JS)
    print("--- メニュー候補項目 ---")
    for t in items:
        print(" ", repr(t))
    target.screenshot(path="data/ebaymag_action_menu.png", timeout=15000)
    print("screenshot: data/ebaymag_action_menu.png")
