"""eBaymag: 詳細パネルの全サイトトグル状態を class (PSfVs=選択中) で検証 (read-only).

保存後に ebay.co.uk だけ ON / 他 6 国 OFF を確認する。アーカイブ状態も併せて採取。
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

STATE_JS = r"""() => {
  const sites = [];
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
        const others = (node.innerText.match(/ebay\.(ca|de|it|fr|es|com\b|com\.au)/g) || [])
          .filter(s => s !== tn.textContent.trim()).length;
        if (others === 0) {
          sites.push({
            site: tn.textContent.trim(),
            listingId: (node.innerText.match(/ListingID:\s*(\d+)/) || [])[1] || null,
            states: btns.map(b => ({t: b.innerText.trim(),
                                    sel: b.className.includes('PSfVs')})),
          });
          break;
        }
      }
      node = node.parentElement;
    }
  }
  const body = document.body.innerText;
  return {
    sites,
    archivedHint: /アーカイブ/.test(body)
      ? (body.match(/.{0,40}アーカイブ.{0,40}/g) || []).slice(0, 4) : [],
    oosWarn: /在庫切れ/.test(body),
  };
}"""

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0]
    target = next((pg for pg in ctx.pages if "ebaymag.com" in pg.url), None)
    if target is None:
        print("eBaymag タブなし")
        sys.exit(1)
    print("URL:", target.url[:110])
    st = target.evaluate(STATE_JS)
    print(f"--- サイト状態 ({len(st['sites'])} 件) ---")
    for s in st["sites"]:
        marks = " / ".join(f"{x['t']}[{'●選択' if x['sel'] else '○'}]" for x in s["states"])
        print(f"  {s['site']:<14} ListingID={s['listingId']} {marks}")
    print("在庫切れ警告:", st["oosWarn"])
    print("アーカイブ語近傍:", st["archivedHint"])
