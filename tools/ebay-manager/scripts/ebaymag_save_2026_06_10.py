"""eBaymag: 詳細パネルの「N 変動 を保存」を 1 クリックして結果観察.

前提: ebaymag_click_uk_2026_06_10.py で ebay.co.uk を「リストされている」に
切替済み (1 変動 pending)。保存後の画面状態とエラー有無を dump する。
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

SAVE_JS = r"""() => {
  const btns = Array.from(document.querySelectorAll('button'));
  const save = btns.find(b => /変動\s*を保存/.test(b.innerText));
  if (!save) return 'SAVE BUTTON NOT FOUND';
  const label = save.innerText.trim();
  if (!/^1 /.test(label)) return 'ABORT: 変動数が1でない → ' + label;
  save.click();
  return 'CLICKED: ' + label;
}"""

AFTER_JS = r"""() => {
  const txt = document.body.innerText;
  return {
    hasError: /エラー|失敗|error/i.test(txt.slice(0, 4000)),
    snippet: txt.slice(0, 600),
    buttons: Array.from(document.querySelectorAll('button'))
      .map(b => b.innerText.trim()).filter(Boolean).slice(0, 30),
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

    result = target.evaluate(SAVE_JS)
    print("保存クリック:", result)
    if not result.startswith("CLICKED"):
        sys.exit(1)
    target.wait_for_timeout(5000)

    print("URL(後):", target.url[:110])
    after = target.evaluate(AFTER_JS)
    print("エラー語検出:", after["hasError"])
    print("--- 画面冒頭 600 字 ---")
    print(after["snippet"])
    print("--- ボタン ---")
    print(" ", " | ".join(after["buttons"]))
    target.screenshot(path="data/ebaymag_after_save.png", timeout=15000)
    print("screenshot: data/ebaymag_after_save.png")
