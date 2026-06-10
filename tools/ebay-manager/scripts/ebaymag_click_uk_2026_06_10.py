"""eBaymag: 開いている詳細パネルで ebay.co.uk を「掲載されている」に切替 (1 クリック).

クリック後は画面状態を dump するのみ (保存等の追加操作はしない)。
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

FIND_JS = r"""() => {
  // 'ebay.co.uk' テキストを含む最小ブロックを探し、その中のボタンを列挙
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let tnode = null;
  while (walker.nextNode()) {
    if (walker.currentNode.textContent.trim() === 'ebay.co.uk') { tnode = walker.currentNode; break; }
  }
  if (!tnode) return {found: false};
  // 祖先を上がって「掲載されている」ボタンを含む最小コンテナを特定
  let node = tnode.parentElement;
  for (let i = 0; i < 8 && node; i++) {
    const btns = Array.from(node.querySelectorAll('button'))
      .filter(b => /掲載され/.test(b.innerText));
    if (btns.length >= 2) {
      // 他サイト名を含まないことを確認 (コンテナ越境防止)
      const txt = node.innerText;
      const others = (txt.match(/ebay\.(ca|de|it|fr|es|com\b|com\.au)/g) || []);
      if (others.length === 0) {
        return {
          found: true,
          containerText: txt.slice(0, 120),
          buttons: btns.map(b => ({txt: b.innerText.trim(), cls: b.className})),
        };
      }
    }
    node = node.parentElement;
  }
  return {found: false, reason: 'container not isolated'};
}"""

CLICK_JS = r"""() => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let tnode = null;
  while (walker.nextNode()) {
    if (walker.currentNode.textContent.trim() === 'ebay.co.uk') { tnode = walker.currentNode; break; }
  }
  if (!tnode) return 'NOT FOUND';
  let node = tnode.parentElement;
  for (let i = 0; i < 8 && node; i++) {
    const btns = Array.from(node.querySelectorAll('button'))
      .filter(b => /掲載され/.test(b.innerText));
    if (btns.length >= 2) {
      const others = (node.innerText.match(/ebay\.(ca|de|it|fr|es|com\b|com\.au)/g) || []);
      if (others.length === 0) {
        const onBtn = btns.find(b => b.innerText.trim() === '掲載されている');
        if (!onBtn) return 'ON button not found';
        onBtn.click();
        return 'CLICKED';
      }
    }
    node = node.parentElement;
  }
  return 'container not isolated';
}"""

STATE_JS = r"""() => {
  // 全サイトのトグル状態と、新たに現れたボタン (保存等) を採取
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
      const btns = Array.from(node.querySelectorAll('button')).filter(b => /掲載され/.test(b.innerText));
      if (btns.length >= 2) {
        const others = (node.innerText.match(/ebay\.(ca|de|it|fr|es|com\b|com\.au)/g) || []).length;
        if (others === 0) {
          sites.push({site: tn.textContent.trim(),
            states: btns.map(b => ({t: b.innerText.trim(), cls: b.className}))});
          break;
        }
      }
      node = node.parentElement;
    }
  }
  const allBtns = Array.from(document.querySelectorAll('button'))
    .map(b => b.innerText.trim()).filter(Boolean);
  return {sites, allBtns};
}"""

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0]
    target = next((pg for pg in ctx.pages if "ebaymag.com" in pg.url), None)
    if target is None:
        print("eBaymag タブなし")
        sys.exit(1)
    print("URL:", target.url[:110])

    info = target.evaluate(FIND_JS)
    print("特定結果:", info)
    if not info.get("found"):
        print("→ 中止 (対象を特定できない)")
        sys.exit(1)

    result = target.evaluate(CLICK_JS)
    print("クリック:", result)
    target.wait_for_timeout(3000)

    state = target.evaluate(STATE_JS)
    print("--- クリック後のサイト状態 ---")
    for s in state["sites"]:
        marks = " / ".join(f"{x['t']}[{'選択' if 'PSfVs' in x['cls'] else '非選択'}]" for x in s["states"])
        print(f"  {s['site']:<14} {marks}")
    print("--- 画面上の全ボタン ---")
    print(" ", " | ".join(state["allBtns"][:40]))
    target.screenshot(path="data/ebaymag_after_uk_click.png", timeout=15000)
    print("screenshot: data/ebaymag_after_uk_click.png")
