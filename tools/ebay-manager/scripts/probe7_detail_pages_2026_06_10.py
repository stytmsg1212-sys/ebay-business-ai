"""W229 Phase 2 probe7: scrape_product_detail 用 DOM 根拠取得 (1 回限り、2 ロード).

目的:
  A. SOLD タブ (dayRange=90) の Total sold メトリクス / 行数の取り方を確認
  B. ACTIVE タブの行構造 (出品開始日 'Date started' 相当の列があるか) を確認

実行: python scripts/probe7_detail_pages_2026_06_10.py
注意: DB 書込なし / 2 ロードのみ / 本体変更なし
"""
import sys
import os
import re
import time

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from monitor.terapeak_scraper import _build_terapeak_search_url

KEYWORD = "MAFEX Robocop"
PROBE_DIR = os.path.join(_ROOT, "data", "terapeak_probe")
os.makedirs(PROBE_DIR, exist_ok=True)


def _fetch_html(url: str) -> str:
    import asyncio
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        tab = ctx.new_page()
        try:
            tab.goto(url, wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                cnt = tab.evaluate(
                    "() => document.querySelectorAll('tr.research-table-row').length"
                )
                if cnt and cnt > 0:
                    break
                time.sleep(1.0)
            html = tab.evaluate("() => document.documentElement.outerHTML")
        finally:
            try:
                tab.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
    return html


def summarize(label: str, html: str) -> None:
    print(f"--- {label} ---")
    print(f"  chars: {len(html):,}")
    rows = len(re.findall(r'tr class="research-table-row', html))
    print(f"  tr.research-table-row: {rows}")
    # メトリクスタイル候補
    for pat, name in [
        (r'([\d,]+)\s*Total\s*sold', "Total sold"),
        (r'([\d,]+)\s*Total\s*sellers', "Total sellers"),
        (r'([\d.]+)%\s*Sell-through', "Sell-through"),
        (r'\$([\d,.]+)\s*Avg\s*sold\s*price', "Avg sold price"),
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        print(f"  {name}: {m.group(1) if m else '(not found)'}")
    # td class suffix 一覧 (列構造の把握)
    suffixes = sorted(set(re.findall(r'class="research-table-row__([a-zA-Z]+)', html)))
    print(f"  td suffixes: {suffixes}")
    # 日付らしき文字列 (行内)
    dates = re.findall(
        r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}', html
    )
    print(f"  date-like strings: {len(dates)} 件 (先頭5: {dates[:5]})")
    print()


# --- A. SOLD タブ dayRange=90 ---
sold_url = _build_terapeak_search_url(KEYWORD, 90)
print(f"[A] SOLD 90d URL: {sold_url[:160]}")
html_sold = _fetch_html(sold_url)
p1 = os.path.join(PROBE_DIR, "probe7_sold90.html")
with open(p1, "w", encoding="utf-8") as f:
    f.write(html_sold)
print(f"saved: {p1}")
summarize("SOLD 90d", html_sold)

time.sleep(4)

# --- B. ACTIVE タブ (同 keyword) ---
active_url = sold_url.replace("tabName=SOLD", "tabName=ACTIVE")
print(f"[B] ACTIVE URL: {active_url[:160]}")
html_active = _fetch_html(active_url)
p2 = os.path.join(PROBE_DIR, "probe7_active.html")
with open(p2, "w", encoding="utf-8") as f:
    f.write(html_active)
print(f"saved: {p2}")
summarize("ACTIVE", html_active)

print("=== probe7 完了 (2 ロード) ===")
