"""適用済み productId 全件の item_id 照合監査 (read-only、mutation なし).

各 productId の panel を開き、itm リンクの eBay item id と期待値を照合 +
各国行の現在状態 (PSfVs ラベル) を記録する。
"""
import json
import sys
import time

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"
OUT = r"data\ebaymag_audit_2026_06_11.json"

# (productId, expected_item_id, label)
AUDIT = [
    ("718746739", "357947963186", "TE 91512-1 (UK)"),
    ("718746512", "356733099716", "SONY ICD-ST25 (UK)"),
    ("714154004", "358626622317", "HIOKI DT4261 (no-mutation)"),
    ("718746698", "357414236596", "Leica X310 (no-mutation)"),
    ("644831699", "358352049570", "Keithley 237 (FR)"),
    ("592103124", "358207319749", "AOR AR-3000A (no-mutation)"),
    ("718746702", "357839289258", "SHARP PC-G850VS (DE,IT)"),
    ("718746638", "357374753803", "Brother PJ-723 (DE,IT)"),
    ("640119315", "358333799417", "Marantz PM8006 (CA)"),
    ("718746640", "357387217824", "Ajazz AKP846 (CA)"),
    ("718746535", "357200863085", "KP-707G (誤適用疑い→set 356776795931)"),
    ("652417022", "358377470398", "Fluke DSP-FTA440 (IT)"),
    ("718746737", "357944436089", "Pioneer KP-717G (DE,UK)"),
    ("718746569", "357065276999", "CRYPTON LUKA V4X (DE,UK)"),
    ("584218610", "357418184869", "Alice Madness (DE,ES)"),
    ("718746878", "358228793891", "Kikkoman LuciPac (DE,ES)"),
    ("718746908", "357190920884", "Wallhack SP-004 (CA,FR)"),
    ("718746695", "358377346781", "BMW Car Eye (CA,FR)"),
]

PANEL_JS = r"""() => {
  const out = {url: location.href, itm: null, title: '', sites: []};
  for (const a of document.querySelectorAll('a[href*="ebay."]')) {
    const m = a.href.match(/itm\/.*?(\d{12})/);
    if (m) { out.itm = m[1]; break; }
  }
  const act = Array.from(document.querySelectorAll('button'))
    .find(b => b.innerText.trim() === 'アクション');
  if (act) {
    let n = act.parentElement;
    for (let i = 0; i < 6 && n; i++) {
      if (n.innerText.length > 80) { out.title = n.innerText.split('\n')[0].trim(); break; }
      n = n.parentElement;
    }
  }
  // 各国行の現在 active ラベル
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = walker.currentNode.textContent.trim();
    if (!/^ebay\.[a-z.]+$/.test(t)) continue;
    let node = walker.currentNode.parentElement;
    for (let i = 0; i < 8 && node; i++) {
      const mentions = (node.innerText.match(/ebay\.[a-z][a-z.]+/g) || []);
      if (new Set(mentions).size > 1) break;
      const btns = Array.from(node.querySelectorAll('button'));
      if (btns.length >= 2) {
        const on = btns.find(b => b.className.includes('PSfVs'));
        out.sites.push({site: t, state: on ? on.innerText.trim() : '?'});
        break;
      }
      node = node.parentElement;
    }
  }
  return out;
}"""


def goto_panel(page, product_id: str) -> None:
    url = f"https://ebaymag.com/stock?productId={product_id}"
    page.evaluate("url => { setTimeout(() => { location.href = url; }, 100); }", url)
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            cur = page.evaluate("() => location.href")
            if f"productId={product_id}" in cur:
                break
        except Exception:
            continue
    time.sleep(5)


def main() -> None:
    results = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "ebaymag.com" in pg.url), None)
        if page is None:
            print("eBaymag タブなし")
            sys.exit(1)
        for product_id, expected, label in AUDIT:
            goto_panel(page, product_id)
            try:
                res = page.evaluate(PANEL_JS)
            except Exception as e:
                results.append({"productId": product_id, "label": label, "error": str(e)[:200]})
                print(f"NG  {label}: evaluate 失敗 {e}")
                continue
            match = res["itm"] == expected
            on_sites = [s for s in res["sites"] if s["state"] not in ("掲載されていません", "?")]
            mark = "OK " if match else "*** MISMATCH"
            print(f"{mark} {label}: itm={res['itm']} expected={expected}")
            print(f"     title={res['title'][:80]}")
            print(f"     on={[(s['site'], s['state']) for s in on_sites]}")
            results.append({"productId": product_id, "expected": expected,
                            "itm": res["itm"], "match": match, "label": label,
                            "title": res["title"], "sites": res["sites"]})
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=1)
    n_mis = sum(1 for r in results if not r.get("match"))
    print(f"\n==== 監査完了: {len(results)} 件 / MISMATCH {n_mis} 件 (log: {OUT}) ====")


if __name__ == "__main__":
    main()
