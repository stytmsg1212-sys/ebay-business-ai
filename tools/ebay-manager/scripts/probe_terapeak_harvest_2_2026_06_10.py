"""
W229 Terapeak harvest probe (第 2 弾) — keywords あり + live DOM evaluate
==========================================================================
第 1 弾で keywords=空 はすべてリダイレクト→結果なし が判明。
第 2 弾では keywords を入れた検索で:
  - live DOM (page.evaluate outerHTML) でテーブル行を取得
  - ソートヘッダ/日付カラムを確認
  - dayRange=7/730 の期間表示確認
  - ページング確認

最大 6 ページロード (第 2 弾独立)。
保存先: data/terapeak_probe/
"""
import os
import sys
import time

# Windows cp932 対策
for stream_name in ("stdout", "stderr"):
    stream = getattr(sys, stream_name, None)
    if stream is not None and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import json
import re
from pathlib import Path
from urllib.parse import urlencode

BASE_DIR = Path(__file__).resolve().parent.parent
PROBE_DIR = BASE_DIR / "data" / "terapeak_probe"
PROBE_DIR.mkdir(parents=True, exist_ok=True)

CDP_ENDPOINT = "http://localhost:9222"
# カテゴリ: Consumer Electronics = 293, Cameras & Photo = 625
# Portable Audio & Headphones = 15052 (SONY/ATH 等で実績あり)
CAT_ID = 15052  # 既存 scraper と同じ系統のカテゴリ


def build_url(
    keywords: str,
    category_id: int = CAT_ID,
    day_range: int = 365,
    seller_country: str = "SellerLocation:::JP",
    offset: int = 0,
    limit: int = 50,
    tab: str = "SOLD",
    now_ms: int | None = None,
    extra: dict | None = None,
) -> str:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    start_ms = now_ms - day_range * 24 * 3600 * 1000
    params = {
        "marketplace": "EBAY-US",
        "keywords": keywords,
        "dayRange": day_range,
        "endDate": now_ms,
        "startDate": start_ms,
        "categoryId": category_id,
        "offset": offset,
        "limit": limit,
        "tabName": tab,
        "sellerCountry": seller_country,
    }
    if extra:
        params.update(extra)
    return "https://www.ebay.com/sh/research?" + urlencode(params)


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def save_html(page, name: str, use_evaluate: bool = True) -> tuple[Path, str]:
    """live DOM (evaluate) と page.content() の両方を保存して返す。"""
    if use_evaluate:
        try:
            html = page.evaluate("() => document.documentElement.outerHTML")
            p = PROBE_DIR / f"{name}_live.html"
            p.write_text(html, encoding="utf-8")
            log(f"  -> saved (live) {p.name} ({len(html):,} bytes)")
            return p, html
        except Exception as e:
            log(f"  evaluate failed: {e}, fallback to content()")
    html = page.content()
    p = PROBE_DIR / f"{name}.html"
    p.write_text(html, encoding="utf-8")
    log(f"  -> saved {p.name} ({len(html):,} bytes)")
    return p, html


def save_screenshot(page, name: str) -> Path:
    p = PROBE_DIR / f"{name}.png"
    page.screenshot(path=str(p), full_page=False)
    log(f"  -> screenshot {p.name}")
    return p


def wait_for_results(page, timeout_ms: int = 35000) -> bool:
    """テーブル行か no-results が現れるまで待機。live DOM 評価で確認。"""
    deadline = time.time() + timeout_ms / 1000
    # まず networkidle を待つ
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    # その後 DOM に行が出るまで最大 15 秒ポーリング
    while time.time() < deadline:
        try:
            count = page.evaluate("""() => {
                const rows = document.querySelectorAll(
                  'tr.research-table__row, [class*="research-table"] tbody tr, tbody tr'
                );
                return rows.length;
            }""")
            if count > 0:
                return True
            # no-results テキスト
            no_res = page.evaluate("""() => {
                const el = document.querySelector(
                  '[class*="no-results"], [class*="empty-state"], [class*="zero-results"]'
                );
                return el ? el.innerText : null;
            }""")
            if no_res:
                log(f"  no-results text: {no_res[:100]}")
                return False
        except Exception:
            pass
        time.sleep(1.5)
    return False


def extract_live_info(page, html: str) -> dict:
    """live DOM から結果情報を取得。"""
    info = {}

    # --- page.evaluate で DOM を直接クエリ ---
    try:
        dom_info = page.evaluate("""() => {
            const result = {};

            // ヘッダテキスト
            const headerEls = [
              '.results-header__left',
              '[class*="results-header"]',
              '[class*="research-header"]',
              '[class*="dateRange"]',
              '[class*="date-range"]',
            ];
            for (const sel of headerEls) {
              const el = document.querySelector(sel);
              if (el && el.innerText.trim()) {
                result.header_text = el.innerText.trim();
                result.header_selector = sel;
                break;
              }
            }

            // 行数
            const rowSels = [
              'tr.research-table__row',
              '[class*="research-table"] tbody tr',
              '[class*="results-list"] [class*="row"]',
              'tbody tr',
            ];
            for (const sel of rowSels) {
              const rows = document.querySelectorAll(sel);
              if (rows.length > 0) {
                result.row_count = rows.length;
                result.row_selector = sel;
                // 最初の行のセル
                const firstRow = rows[0];
                const cells = firstRow.querySelectorAll('td, [class*="cell"]');
                result.first_row_cells = Array.from(cells).map(c => ({
                  class: (c.className || '').substring(0, 80),
                  text: c.innerText.trim().substring(0, 80),
                }));
                result.first_row_text = firstRow.innerText.replace(/\\n/g, ' | ').substring(0, 300);
                break;
              }
            }

            // カラムヘッダ
            const headerCells = document.querySelectorAll('thead th, [class*="table-header"] th');
            if (headerCells.length > 0) {
              result.column_headers = Array.from(headerCells).map(th => ({
                text: th.innerText.trim(),
                class: (th.className || '').substring(0, 80),
                aria_sort: th.getAttribute('aria-sort') || '',
                data_sort: th.getAttribute('data-sort') || th.getAttribute('data-field') || '',
                cursor: window.getComputedStyle(th).cursor,
              }));
            }

            // ソート状態
            const sortedEl = document.querySelector('[aria-sort="ascending"], [aria-sort="descending"]');
            if (sortedEl) {
              result.current_sort = {
                text: sortedEl.innerText.trim(),
                aria_sort: sortedEl.getAttribute('aria-sort'),
              };
            }

            result.current_url = location.href;
            result.title = document.title;
            result.body_text_len = document.body.innerText.length;

            return result;
        }""")
        info.update(dom_info)
    except Exception as e:
        info["dom_eval_error"] = str(e)

    # HTML からも補足
    # 選択中の dayRange ボタン (JSON 埋め込み)
    m = re.search(r'"selected"\s*:\s*true[^}]+?"value"\s*:\s*"(\d+)"', html)
    if m:
        info["selected_dayrange_from_html"] = m.group(1)

    # dayRange ボタン groups
    dr_matches = re.findall(r'"value"\s*:\s*"(\d+)"[^}]*?"label"[^:]*:\s*"([^"]*)"[^}]*?"selected"\s*:\s*(true|false)', html)
    if dr_matches:
        info["dayrange_options"] = [{"value": v, "label": l, "selected": s} for v, l, s in dr_matches]

    return info


def probe_sort_header_click(page, label_substr: str) -> dict:
    """ソートヘッダをクリックして URL 変化を記録。"""
    result = {}
    url_before = page.url

    # ヘッダセル検索
    found = page.evaluate(f"""() => {{
        const ths = document.querySelectorAll('thead th, [class*="table-header"] th, [class*="header-cell"]');
        for (const th of ths) {{
            if (th.innerText.toLowerCase().includes('{label_substr.lower()}')) {{
                return {{found: true, text: th.innerText.trim(), class: th.className}};
            }}
        }}
        return {{found: false}};
    }}""")
    result["header_found"] = found

    if found.get("found"):
        try:
            th = page.locator(f'thead th:has-text("{label_substr}"), [class*="table-header"] th:has-text("{label_substr}")')
            if th.count() > 0:
                th.first.click(timeout=5000)
                page.wait_for_timeout(2000)
                url_after_1st = page.url
                result["url_after_1st_click"] = url_after_1st
                result["url_changed_1st"] = url_after_1st != url_before
                # 2回目クリック
                th.first.click(timeout=5000)
                page.wait_for_timeout(2000)
                url_after_2nd = page.url
                result["url_after_2nd_click"] = url_after_2nd
                result["url_changed_2nd"] = url_after_2nd != url_after_1st
        except Exception as e:
            result["click_error"] = str(e)

    return result


def main() -> None:
    from playwright.sync_api import sync_playwright

    page_load_count = 0
    MAX_LOADS = 6
    now_ms = int(time.time() * 1000)

    # テスト用キーワード (実績のある JP seller カテゴリ)
    # 既存 scraper テストで使っている軽量キーワード
    TEST_KW = "sony headphones"

    results = {}
    log("=== W229 Terapeak harvest probe #2 (keywords あり) START ===")
    log(f"keyword: {TEST_KW!r}  categoryId: {CAT_ID}")

    with sync_playwright() as p:
        log("Connecting CDP ...")
        try:
            browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
        except Exception as e:
            log(f"ERROR CDP: {e}")
            sys.exit(1)
        ctx = browser.contexts[0]
        probe_page = ctx.new_page()
        log("New probe tab opened.")

        try:
            # ============================================================
            # PROBE A: keywords あり + categoryId + dayRange=365 (base)
            # ============================================================
            log(f"\n--- PROBE A: keywords={TEST_KW!r}, cat={CAT_ID}, 365d ---")
            url_a = build_url(TEST_KW, category_id=CAT_ID, day_range=365, now_ms=now_ms)
            log(f"URL: {url_a}")
            probe_page.goto(url_a, wait_until="domcontentloaded", timeout=35000)
            page_load_count += 1
            loaded = wait_for_results(probe_page)
            log(f"loaded (rows found): {loaded}  [loads: {page_load_count}/{MAX_LOADS}]")
            _, html_a = save_html(probe_page, "probe2a_kw_365d")
            save_screenshot(probe_page, "probe2a_kw_365d")
            info_a = extract_live_info(probe_page, html_a)
            results["probeA_base"] = {"url": url_a, "loaded": loaded, "info": info_a}
            log(f"  row_count: {info_a.get('row_count', 'N/A')}")
            log(f"  header_text: {info_a.get('header_text', 'N/A')}")
            log(f"  current_url: {info_a.get('current_url', 'N/A')[:120]}")
            log(f"  selected_dayrange: {info_a.get('selected_dayrange_from_html', 'N/A')}")
            if info_a.get("column_headers"):
                for h in info_a["column_headers"]:
                    log(f"  col: {h['text']!r:30s} aria_sort={h['aria_sort']!r} data_sort={h['data_sort']!r} cursor={h['cursor']!r}")
            if info_a.get("first_row_cells"):
                log("  first row cells:")
                for c in info_a["first_row_cells"]:
                    log(f"    [{c['class'][:40]}] {c['text']!r}")

            # ============================================================
            # PROBE B: ソートクリック ("Date last sold" or "Date")
            # ============================================================
            if loaded and info_a.get("row_count", 0) > 0:
                log("\n--- PROBE B: Sort header click ---")
                for sort_label in ["Date last sold", "Date", "Last sold"]:
                    sort_result = probe_sort_header_click(probe_page, sort_label)
                    if sort_result.get("header_found", {}).get("found"):
                        results["probeB_sort"] = sort_result
                        log(f"  Sort label: {sort_label!r}")
                        log(f"  URL after 1st: {sort_result.get('url_after_1st_click', 'N/A')[:120]}")
                        log(f"  URL after 2nd: {sort_result.get('url_after_2nd_click', 'N/A')[:120]}")
                        break
                else:
                    log("  No sort header found.")
                    results["probeB_sort"] = {"not_found": True}
            else:
                log("PROBE B skipped (no rows in probe A)")
                results["probeB_sort"] = {"skipped": "no rows"}

            # ============================================================
            # PROBE C: dayRange=7
            # ============================================================
            if page_load_count < MAX_LOADS:
                log(f"\n--- PROBE C: dayRange=7, keywords={TEST_KW!r} ---")
                url_c = build_url(TEST_KW, category_id=CAT_ID, day_range=7, now_ms=now_ms)
                log(f"URL: {url_c}")
                probe_page.goto(url_c, wait_until="domcontentloaded", timeout=35000)
                page_load_count += 1
                wait_for_results(probe_page)
                _, html_c = save_html(probe_page, "probe2c_kw_7d")
                save_screenshot(probe_page, "probe2c_kw_7d")
                info_c = extract_live_info(probe_page, html_c)
                results["probeC_7d"] = {"url": url_c, "info": info_c}
                log(f"  row_count: {info_c.get('row_count', 'N/A')}")
                log(f"  header_text: {info_c.get('header_text', 'N/A')}")
                log(f"  selected_dayrange: {info_c.get('selected_dayrange_from_html', 'N/A')}")
                log(f"  [loads: {page_load_count}/{MAX_LOADS}]")
            else:
                log("PROBE C skipped (load limit)")

            # ============================================================
            # PROBE D: dayRange=730
            # ============================================================
            if page_load_count < MAX_LOADS:
                log(f"\n--- PROBE D: dayRange=730, keywords={TEST_KW!r} ---")
                url_d = build_url(TEST_KW, category_id=CAT_ID, day_range=730, now_ms=now_ms)
                log(f"URL: {url_d}")
                probe_page.goto(url_d, wait_until="domcontentloaded", timeout=35000)
                page_load_count += 1
                wait_for_results(probe_page)
                _, html_d = save_html(probe_page, "probe2d_kw_730d")
                save_screenshot(probe_page, "probe2d_kw_730d")
                info_d = extract_live_info(probe_page, html_d)
                results["probeD_730d"] = {"url": url_d, "info": info_d}
                log(f"  row_count: {info_d.get('row_count', 'N/A')}")
                log(f"  header_text: {info_d.get('header_text', 'N/A')}")
                log(f"  selected_dayrange: {info_d.get('selected_dayrange_from_html', 'N/A')}")
                log(f"  [loads: {page_load_count}/{MAX_LOADS}]")
            else:
                log("PROBE D skipped (load limit)")

            # ============================================================
            # PROBE E: minPrice=100
            # ============================================================
            if page_load_count < MAX_LOADS:
                log(f"\n--- PROBE E: minPrice=100, keywords={TEST_KW!r}, 365d ---")
                url_e = build_url(TEST_KW, category_id=CAT_ID, day_range=365, now_ms=now_ms,
                                   extra={"minPrice": 100})
                log(f"URL: {url_e}")
                probe_page.goto(url_e, wait_until="domcontentloaded", timeout=35000)
                page_load_count += 1
                wait_for_results(probe_page)
                _, html_e = save_html(probe_page, "probe2e_minprice100")
                save_screenshot(probe_page, "probe2e_minprice100")
                info_e = extract_live_info(probe_page, html_e)
                results["probeE_minprice"] = {"url": url_e, "info": info_e}
                log(f"  row_count: {info_e.get('row_count', 'N/A')}")
                log(f"  header_text: {info_e.get('header_text', 'N/A')}")
                log(f"  [loads: {page_load_count}/{MAX_LOADS}]")
            else:
                log("PROBE E skipped (load limit)")

            # ============================================================
            # PROBE F: offset=50 (page 2)
            # ============================================================
            if page_load_count < MAX_LOADS:
                log(f"\n--- PROBE F: offset=50, limit=50, keywords={TEST_KW!r}, 365d ---")
                url_f = build_url(TEST_KW, category_id=CAT_ID, day_range=365, now_ms=now_ms,
                                   offset=50, limit=50)
                log(f"URL: {url_f}")
                probe_page.goto(url_f, wait_until="domcontentloaded", timeout=35000)
                page_load_count += 1
                wait_for_results(probe_page)
                _, html_f = save_html(probe_page, "probe2f_offset50")
                save_screenshot(probe_page, "probe2f_offset50")
                info_f = extract_live_info(probe_page, html_f)
                results["probeF_offset50"] = {"url": url_f, "info": info_f}
                log(f"  row_count: {info_f.get('row_count', 'N/A')}")
                log(f"  first_row: {info_f.get('first_row_text', 'N/A')[:100]}")
                log(f"  [loads: {page_load_count}/{MAX_LOADS}]")
            else:
                log("PROBE F skipped (load limit)")

        except KeyboardInterrupt:
            log("Interrupted.")
        except Exception as e:
            import traceback
            log(f"ERROR: {e}")
            traceback.print_exc()
            results["error"] = str(e)
            try:
                save_html(probe_page, "probe2_error_state")
                save_screenshot(probe_page, "probe2_error_state")
            except Exception:
                pass
        finally:
            log("\nClosing probe tab ...")
            try:
                probe_page.close()
            except Exception:
                pass

    # 結果保存
    out_path = PROBE_DIR / "probe2_results_2026_06_10.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\nResults saved: {out_path}")
    log(f"Total page loads used: {page_load_count}/{MAX_LOADS}")

    # サマリ
    log("\n=== SUMMARY ===")
    a = results.get("probeA_base", {}).get("info", {})
    log(f"Probe A (365d kw): rows={a.get('row_count','N/A')} header={a.get('header_text','N/A')}")
    b = results.get("probeB_sort", {})
    log(f"Probe B (sort): found={b.get('header_found',{}).get('found','?')} url_changed={b.get('url_changed_1st','?')}")
    c = results.get("probeC_7d", {}).get("info", {})
    log(f"Probe C (7d): rows={c.get('row_count','N/A')} selected_dr={c.get('selected_dayrange_from_html','N/A')}")
    d = results.get("probeD_730d", {}).get("info", {})
    log(f"Probe D (730d): rows={d.get('row_count','N/A')} selected_dr={d.get('selected_dayrange_from_html','N/A')}")
    e = results.get("probeE_minprice", {}).get("info", {})
    log(f"Probe E (minPrice): rows={e.get('row_count','N/A')}")
    f = results.get("probeF_offset50", {}).get("info", {})
    log(f"Probe F (offset=50): rows={f.get('row_count','N/A')}")


if __name__ == "__main__":
    main()
