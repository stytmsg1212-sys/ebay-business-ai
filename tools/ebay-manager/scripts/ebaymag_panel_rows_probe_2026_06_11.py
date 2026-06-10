"""eBaymag panel の国行の実テキストを全部出す (read-only probe)."""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

JS = r"""() => {
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = walker.currentNode.textContent.trim();
    if (/^ebay\./.test(t)) {
      let node = walker.currentNode.parentElement;
      for (let i = 0; i < 8 && node; i++) {
        const others = (node.innerText.match(/ebay\.[a-z.]+/g) || []).length;
        if (others > 1) break;
        const btns = Array.from(node.querySelectorAll('button')).map(b => ({
          t: b.innerText.trim().slice(0, 30), cls: b.className.slice(0, 60)}));
        if (btns.length >= 1 && node.innerText.length > t.length + 2) {
          out.push({site: t, rowText: node.innerText.replace(/\n/g, ' | ').slice(0, 160),
                    btns});
          break;
        }
        node = node.parentElement;
      }
    }
  }
  return {url: location.href, rows: out};
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
    for r in res["rows"]:
        print(f"\n[{r['site']}] {r['rowText']}")
        for b in r["btns"]:
            print(f"    btn: {b['t']!r} cls={b['cls']}")
