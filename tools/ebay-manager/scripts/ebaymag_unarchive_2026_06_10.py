"""eBaymag: 開いているアクションメニューの「戻す」(アーカイブ解除) をクリックして観察.

クリック後 5 秒待ち、画面状態 (トグル/警告/URL) を dump + screenshot。
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

CLICK_RESTORE_JS = r"""() => {
  // アクションメニューが閉じていたら開く
  const els = Array.from(document.querySelectorAll('li, a, button, [role="menuitem"]'));
  let restore = els.find(el => el.innerText && el.innerText.trim() === '戻す');
  if (!restore) {
    const act = Array.from(document.querySelectorAll('button'))
      .find(b => b.innerText.trim() === 'アクション');
    if (!act) return 'アクション button not found';
    act.click();
    return 'MENU_REOPENED';
  }
  restore.click();
  return 'RESTORE_CLICKED';
}"""

AFTER_JS = r"""() => {
  const body = document.body.innerText;
  return {
    url: location.href,
    hasError: /エラー|失敗|error/i.test(body.slice(0, 4000)),
    oosWarn: /在庫切れ/.test(body),
    head: body.slice(0, 400),
  };
}"""

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0]
    target = next((pg for pg in ctx.pages if "ebaymag.com" in pg.url), None)
    if target is None:
        print("eBaymag タブなし")
        sys.exit(1)
    print("URL(前):", target.url[:110])

    r = target.evaluate(CLICK_RESTORE_JS)
    print("1回目:", r)
    if r == "MENU_REOPENED":
        target.wait_for_timeout(1200)
        r = target.evaluate(CLICK_RESTORE_JS)
        print("2回目:", r)
    if r != "RESTORE_CLICKED":
        print("→ 中止")
        sys.exit(1)

    target.wait_for_timeout(5000)
    after = target.evaluate(AFTER_JS)
    print("URL(後):", after["url"][:120])
    print("エラー語:", after["hasError"], "/ 在庫切れ警告:", after["oosWarn"])
    print("--- 画面冒頭 ---")
    print(after["head"])
    target.screenshot(path="data/ebaymag_after_restore.png", timeout=15000)
    print("screenshot: data/ebaymag_after_restore.png")
