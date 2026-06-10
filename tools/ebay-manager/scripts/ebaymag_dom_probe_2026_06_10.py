"""eBaymag 詳細パネルの DOM 構造 probe (read-only、クリックなし).

サイト別トグル (掲載されていません/掲載されている) の実コントロールと
アクションメニューのボタン要素を特定する。
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

JS = r"""() => {
  const out = {sites: [], actions: [], buttons: []};
  // 1) サイト行: ebay.co.uk 等のテキストを含むブロックの input/select を採取
  const all = Array.from(document.querySelectorAll('input, select, button, [role="switch"], [role="radio"]'));
  for (const el of all) {
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type') || '';
    const name = el.getAttribute('name') || '';
    const id = el.id || '';
    const cls = (el.className || '').toString().slice(0, 60);
    const checked = el.checked === undefined ? null : el.checked;
    const disabled = el.disabled === undefined ? null : el.disabled;
    const label = (el.closest('label')?.innerText || '').slice(0, 40).replace(/\n/g, ' ');
    // 近傍テキストからサイト名を推定
    let near = '';
    let node = el.parentElement;
    for (let i = 0; i < 6 && node; i++) {
      const t = node.innerText || '';
      const m = t.match(/ebay\.(com\.au|co\.uk|com|ca|de|it|fr|es)/);
      if (m) { near = m[0]; break; }
      node = node.parentElement;
    }
    if (tag === 'input' && (type === 'radio' || type === 'checkbox')) {
      out.sites.push({tag, type, name, id, checked, disabled, label, near, cls});
    } else if (tag === 'button') {
      const txt = (el.innerText || '').slice(0, 30).replace(/\n/g, ' ');
      if (txt) out.buttons.push({txt, disabled, cls: cls.slice(0,40)});
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
    print("URL:", target.url[:110])
    res = target.evaluate(JS)
    print(f"--- radio/checkbox: {len(res['sites'])} 個 ---")
    for s in res["sites"]:
        print(f"  near={s['near']:<12} type={s['type']:<8} name={s['name'][:40]:<40} "
              f"checked={s['checked']} disabled={s['disabled']} label={s['label']!r}")
    print(f"--- button: {len(res['buttons'])} 個 ---")
    for b in res["buttons"]:
        print(f"  {b['txt']!r} disabled={b['disabled']} cls={b['cls']}")
