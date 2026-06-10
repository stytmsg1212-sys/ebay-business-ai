"""eBaymag: ebay.co.uk 行コンテナの全文 + 周辺 status 文言を採取 (read-only)."""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

JS = r"""() => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let tnode = null;
  while (walker.nextNode()) {
    if (walker.currentNode.textContent.trim() === 'ebay.co.uk') { tnode = walker.currentNode; break; }
  }
  if (!tnode) return {found: false};
  let node = tnode.parentElement;
  for (let i = 0; i < 10 && node; i++) {
    const btns = Array.from(node.querySelectorAll('button'))
      .filter(b => /掲載され|リストされ/.test(b.innerText));
    if (btns.length >= 2) {
      return {found: true, text: node.innerText,
              parentText: node.parentElement ? node.parentElement.innerText.slice(0, 1200) : ''};
    }
    node = node.parentElement;
  }
  return {found: false};
}"""

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0]
    target = next((pg for pg in ctx.pages if "ebaymag.com" in pg.url), None)
    if target is None:
        print("eBaymag タブなし")
        sys.exit(1)
    print("URL:", target.url[:120])
    res = target.evaluate(JS)
    if not res.get("found"):
        print("UK 行が見つからない (パネルが閉じている可能性)")
        sys.exit(0)
    print("--- UK 行コンテナ全文 ---")
    print(res["text"])
    print("--- 親要素 (周辺) ---")
    print(res["parentText"])
