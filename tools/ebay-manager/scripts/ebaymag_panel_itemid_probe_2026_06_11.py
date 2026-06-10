"""eBaymag panel 内の eBay item 番号 / itm リンクを探す (read-only probe)."""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

JS = r"""() => {
  const out = {url: location.href, itmLinks: [], digits: [], skuText: []};
  for (const a of document.querySelectorAll('a[href*="ebay."]')) {
    if (/itm/.test(a.href)) out.itmLinks.push({href: a.href.slice(0, 120),
                                               text: a.innerText.slice(0, 60)});
  }
  // 12 桁数字 (eBay item id 形式) を本文から収集
  const body = document.body.innerText;
  const m = body.match(/\b3\d{11}\b/g);
  if (m) out.digits = Array.from(new Set(m)).slice(0, 10);
  // SKU らしき文字列
  const s = body.match(/\b(stock\S{0,8}|ebay[a-zA-Z]{2}_\S+)\b/g);
  if (s) out.skuText = Array.from(new Set(s)).slice(0, 10);
  return out;
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
    print("itm リンク:")
    for ln in res["itmLinks"]:
        print("  ", ln["href"], "|", ln["text"])
    print("12桁数字:", res["digits"])
    print("SKU 風:", res["skuText"])
