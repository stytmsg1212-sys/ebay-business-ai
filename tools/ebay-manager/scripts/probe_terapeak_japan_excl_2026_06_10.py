"""
W229 Terapeak probe: Japan + 除外語クエリの動作確認
2026-06-10

Probe 1: keywords = "Japan -Card -camera -Vuitton -Hermes -GUCCI -Mint -COACH", dayRange=7, catId=0
Probe 2: 同 keywords, dayRange=730, sorting=datelastsold (古い順) -- Probe 1 が行を返した場合のみ
Probe 3: keywords = "Japan" 単体, dayRange=7 -- Probe 1 が 0 行の場合のみ

使用ページロード: 最大 3 回
"""

import sys
import io
import json
import re
import time
import os
from pathlib import Path
from urllib.parse import quote

# stdout UTF-8 強制 (house パターン)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Playwright sync API
from playwright.sync_api import sync_playwright

PROBE_DIR = Path(__file__).parent.parent / "data" / "terapeak_probe"
PROBE_DIR.mkdir(parents=True, exist_ok=True)

CDP_URL = "http://localhost:9222"
POLL_TIMEOUT_S = 30
POLL_INTERVAL_MS = 1000

EXCL_WORDS = ["Card", "camera", "Vuitton", "Hermes", "GUCCI", "Mint", "COACH"]
KEYWORDS_EXCL = "Japan -Card -camera -Vuitton -Hermes -GUCCI -Mint -COACH"
KEYWORDS_BARE = "Japan"


def _build_url(keyword: str, day_range: int, sorting: str = "-datelastsold") -> str:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - day_range * 24 * 3600 * 1000
    return (
        f"https://www.ebay.com/sh/research?marketplace=EBAY-US"
        f"&keywords={quote(keyword)}"
        f"&dayRange={day_range}"
        f"&endDate={now_ms}&startDate={start_ms}"
        f"&categoryId=0&offset=0&limit=50&tabName=SOLD"
        f"&sellerCountry={quote('SellerLocation:::JP')}"
        f"&sorting={sorting}"
        f"&minPrice=100"
    )


def _poll_rows(page, timeout_s: int = POLL_TIMEOUT_S):
    """tr.research-table-row が現れるまでポーリング。タイムアウトで None を返す。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            rows = page.query_selector_all("tr.research-table-row")
            if rows:
                return rows
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_MS / 1000)
    return None


def _extract_rows(page):
    """DOM から各行の title / avgSoldPrice / dateLastSold を取得。"""
    rows_data = []
    try:
        html = page.evaluate("() => document.documentElement.outerHTML")
    except Exception as e:
        print(f"  [ERROR] outerHTML 取得失敗: {e}", flush=True)
        return rows_data, ""

    # tr.research-table-row を JS で取得
    try:
        raw = page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('tr.research-table-row'));
            return rows.map(r => {
                const cells = Array.from(r.querySelectorAll('td'));
                const texts = cells.map(c => c.innerText.trim());
                return texts;
            });
        }""")
    except Exception as e:
        print(f"  [ERROR] row evaluate 失敗: {e}", flush=True)
        raw = []

    for cells in raw:
        # 列構成は動的だが title は最初のセル、avgSoldPrice / dateLastSold は後ろのセルにある想定
        title = cells[0] if len(cells) > 0 else ""
        avg_price = cells[2] if len(cells) > 2 else ""
        date_last = cells[-1] if cells else ""
        rows_data.append({
            "title": title,
            "avgSoldPrice": avg_price,
            "dateLastSold": date_last,
            "raw_cells": cells,
        })
    return rows_data, html


def _count_excl_hits(rows_data):
    """除外語を含む行数をカウント (大文字小文字無視)。"""
    hits = 0
    pattern = re.compile(
        "|".join(re.escape(w) for w in EXCL_WORDS), re.IGNORECASE
    )
    for r in rows_data:
        if pattern.search(r.get("title", "")):
            hits += 1
    return hits


def _detect_period_display(page):
    """ページ上の期間表示テキストを取得 (例: 'Last 7 days' / 'Last 730 days')。"""
    try:
        txt = page.evaluate("""() => {
            // よくある期間表示ウィジェット
            const candidates = [
                ...document.querySelectorAll('[class*="date-range"]'),
                ...document.querySelectorAll('[class*="dateRange"]'),
                ...document.querySelectorAll('[class*="period"]'),
                ...document.querySelectorAll('[data-testid*="date"]'),
            ];
            for (const el of candidates) {
                const t = el.innerText.trim();
                if (t.length > 0 && t.length < 80) return t;
            }
            // fallback: URL の dayRange パラメータ
            return window.location.href;
        }""")
        return txt
    except Exception:
        return "(取得不可)"


def run_probe(page, probe_num: int, keyword: str, day_range: int, sorting: str = "-datelastsold"):
    url = _build_url(keyword, day_range, sorting)
    print(f"\n=== Probe {probe_num} ===", flush=True)
    print(f"  URL: {url}", flush=True)
    print(f"  keyword={keyword!r}  dayRange={day_range}  sorting={sorting}", flush=True)

    page.goto(url, wait_until="domcontentloaded")

    # captcha / rate limit チェック
    cur_url = page.url
    title_txt = page.title()
    if "captcha" in cur_url.lower() or "captcha" in title_txt.lower():
        print("  [ABORT] CAPTCHA 検出。中断します。", flush=True)
        return None, None

    rows_el = _poll_rows(page, timeout_s=POLL_TIMEOUT_S)
    if rows_el is None:
        print(f"  [WARN] {POLL_TIMEOUT_S}s 待機後も tr.research-table-row なし", flush=True)
    else:
        print(f"  [OK] tr.research-table-row {len(rows_el)} 行検出", flush=True)

    rows_data, html = _extract_rows(page)
    period_disp = _detect_period_display(page)

    # HTML 保存
    fname = f"probe_japan_excl_probe{probe_num}.html"
    fpath = PROBE_DIR / fname
    fpath.write_text(html, encoding="utf-8", errors="replace")
    print(f"  HTML 保存: {fpath}", flush=True)

    n_rows = len(rows_data)
    excl_hits = _count_excl_hits(rows_data)
    print(f"  行数: {n_rows}", flush=True)
    print(f"  除外語ヒット行数: {excl_hits}", flush=True)
    print(f"  期間表示 (DOM/URL): {period_disp[:120]}", flush=True)

    # 先頭 N 行サンプル
    n_sample = 10 if probe_num == 1 else 5
    print(f"  先頭 {min(n_sample, n_rows)} 行:", flush=True)
    for i, r in enumerate(rows_data[:n_sample]):
        print(f"    [{i+1}] title={r['title'][:60]!r}  avgPrice={r['avgSoldPrice']!r}  date={r['dateLastSold']!r}", flush=True)

    return n_rows, rows_data


def main():
    load_count = 0

    with sync_playwright() as pw:
        print(f"CDP接続: {CDP_URL}", flush=True)
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        print("新規タブ作成完了", flush=True)

        try:
            # --- Probe 1 ---
            n1, rows1 = run_probe(
                page, probe_num=1,
                keyword=KEYWORDS_EXCL,
                day_range=7,
                sorting="-datelastsold",
            )
            load_count += 1

            if n1 is None:
                print("\n[ABORT] Probe 1 で中断。Probe 2/3 スキップ。", flush=True)
                return

            if n1 > 0:
                # --- Probe 2: 古い順 730 日 ---
                n2, rows2 = run_probe(
                    page, probe_num=2,
                    keyword=KEYWORDS_EXCL,
                    day_range=730,
                    sorting="datelastsold",
                )
                load_count += 1
            else:
                # --- Probe 3: Japan 単体 ---
                print(f"\n  Probe 1 が 0 行のため Probe 3 (Japan 単体) に切り替えます。", flush=True)
                n3, rows3 = run_probe(
                    page, probe_num=3,
                    keyword=KEYWORDS_BARE,
                    day_range=7,
                    sorting="-datelastsold",
                )
                load_count += 1

        finally:
            page.close()
            print(f"\n新規タブを閉じました。", flush=True)

    print(f"\n=== 完了 ===", flush=True)
    print(f"ページロード回数: {load_count}", flush=True)

    # 判定まとめ
    print("\n=== 判定 ===", flush=True)
    if n1 is not None and n1 > 0:
        excl1 = _count_excl_hits(rows1)
        print(f"Probe 1 (Japan+除外 / 新着順 / 7日): {n1} 行, 除外語ヒット {excl1} 行", flush=True)
        if excl1 == 0:
            print("  -> 除外語フィルタ: 機能している可能性あり (ヒット0)", flush=True)
        else:
            print(f"  -> 除外語フィルタ: 不完全 (ヒット{excl1}件)", flush=True)

        if load_count >= 2 and 'n2' in dir():
            print(f"Probe 2 (Japan+除外 / 古い順 / 730日): {n2} 行", flush=True)
            if rows2:
                dates = [r['dateLastSold'] for r in rows2[:5]]
                print(f"  先頭5行の dateLastSold: {dates}", flush=True)
    else:
        print(f"Probe 1: 0 行 (Japan+除外 クエリ不成立)", flush=True)
        if load_count >= 2 and 'n3' in dir():
            print(f"Probe 3 (Japan 単体 / 7日): {n3} 行", flush=True)
            if n3 and n3 > 0:
                print("  -> Japan 単体は機能する。除外語との組み合わせが問題。", flush=True)
            else:
                print("  -> Japan 単体も 0 行。ログイン切れ/captcha 等の可能性。", flush=True)


if __name__ == "__main__":
    main()
