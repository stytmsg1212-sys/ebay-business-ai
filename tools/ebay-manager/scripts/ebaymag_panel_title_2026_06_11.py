"""eBaymag: 開いている panel の商品タイトル + 状態を観察 (read-only)."""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

JS = r"""() => {
  const body = document.body.innerText;
  // panel ヘッダ近辺: アクション button の祖先コンテナのテキスト
  const act = Array.from(document.querySelectorAll('button'))
    .find(b => b.innerText.trim() === 'アクション');
  let panelHead = '';
  if (act) {
    let n = act.parentElement;
    for (let i = 0; i < 6 && n; i++) {
      if (n.innerText.length > 80) { panelHead = n.innerText.slice(0, 500); break; }
      n = n.parentElement;
    }
  }
  return {url: location.href, hasAction: !!act, panelHead,
          bodyHead: body.slice(0, 300)};
}"""

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0]
    target = next((pg for pg in ctx.pages if "ebaymag.com" in pg.url), None)
    if target is None:
        print("eBaymag タブなし")
        sys.exit(1)
    res = target.evaluate(JS)
    print("URL:", res["url"][:140])
    print("アクションbtn:", res["hasAction"])
    print("--- panel ---")
    print(res["panelHead"][:500] or "(なし)")
