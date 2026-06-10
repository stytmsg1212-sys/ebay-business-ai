"""eBaymag: リロード後の UK 行状態 + eBay リンク href を採取 (read-only).

「リストされている」が再読込後も維持されているか、UK listing の実 URL を取得。
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

JS = r"""() => {
  const out = {sites: [], links: []};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const names = [];
  while (walker.nextNode()) {
    const t = walker.currentNode.textContent.trim();
    if (/^ebay\.(com|ca|co\.uk|de|it|fr|es|com\.au)$/.test(t)) names.push(walker.currentNode);
  }
  for (const tn of names) {
    let node = tn.parentElement;
    for (let i = 0; i < 8 && node; i++) {
      const btns = Array.from(node.querySelectorAll('button'))
        .filter(b => /掲載され|リストされ/.test(b.innerText));
      if (btns.length >= 2) {
        const site = tn.textContent.trim();
        const others = (node.innerText.match(/ebay\.(ca|co\.uk|de|it|fr|es|com\b|com\.au)/g) || [])
          .filter(s => s !== site).length;
        if (others === 0) {
          out.sites.push({
            site,
            listingId: (node.innerText.match(/ListingID:\s*(\d+)/) || [])[1] || null,
            states: btns.map(b => ({t: b.innerText.trim(),
                                    sel: b.className.includes('PSfVs')})),
            hrefs: Array.from(node.querySelectorAll('a[href]'))
              .map(a => a.href).filter(h => /ebay\./.test(h)).slice(0, 3),
          });
          break;
        }
      }
      node = node.parentElement;
    }
  }
  return out;
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
    for s in res["sites"]:
        marks = " / ".join(f"{x['t']}[{'●' if x['sel'] else '○'}]" for x in s["states"])
        print(f"  {s['site']:<14} ListingID={s['listingId']} {marks}")
        for h in s["hrefs"]:
            print(f"      link: {h}")
