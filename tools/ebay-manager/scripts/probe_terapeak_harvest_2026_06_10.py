"""
W229 Terapeak harvest probe (2026-06-10, read-only)
===================================================
カテゴリ起点 SOLD リスト取得の実現可否を確認する。
最大 6 ページロード。出品・設定変更は一切しない。

保存先: data/terapeak_probe/
"""
import io
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
from pathlib import Path
from urllib.parse import quote, urlencode

# --- paths -----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
PROBE_DIR = BASE_DIR / "data" / "terapeak_probe"
PROBE_DIR.mkdir(parents=True, exist_ok=True)

CDP_ENDPOINT = "http://localhost:9222"

# ---------------------------------------------------------------------------
# URL ビルダ (terapeak_scraper.py と同方式)
# ---------------------------------------------------------------------------

def build_url(
    keywords: str = "",
    category_id: int = 0,
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
    # keywords が空の時は除外しない (空文字で送る → eBay の反応を観察)
    return "https://www.ebay.com/sh/research?" + urlencode(params)


# ---------------------------------------------------------------------------
# probe ロジック
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def save_html(page, name: str) -> Path:
    content = page.content()
    p = PROBE_DIR / f"{name}.html"
    p.write_text(content, encoding="utf-8")
    log(f"  -> saved {p.name} ({len(content):,} bytes)")
    return p


def save_screenshot(page, name: str) -> Path:
    p = PROBE_DIR / f"{name}.png"
    page.screenshot(path=str(p), full_page=False)
    log(f"  -> screenshot {p.name}")
    return p


def wait_terapeak_loaded(page, timeout_ms: int = 30000) -> bool:
    """SOLD タブのテーブルか 'No results' が出るまで待つ。"""
    try:
        page.wait_for_selector(
            ".research-table, .research-results, [data-testid='results-table'], "
            ".sh-research__no-results, .search-results-table, "
            "table[class*='research'], div[class*='results-list']",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        # fallback: network idle
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            return True
        except Exception:
            return False


def extract_results_info(page) -> dict:
    """ヘッダ部の表示テキスト・結果件数・行数を取得。"""
    info = {}

    # 結果ヘッダ (件数表示・日付範囲)
    for sel in [
        ".results-header__left",
        "[class*='results-header']",
        "[class*='research-header']",
        ".sh-research__header",
        "[data-testid='results-header']",
    ]:
        el = page.query_selector(sel)
        if el:
            info["header_text"] = el.inner_text().strip()
            info["header_selector"] = sel
            break

    # 行数カウント (テーブル行)
    for sel in [
        "tr.research-table__row",
        "[class*='research-table'] tbody tr",
        "[class*='results-list'] [class*='row']",
        "tbody tr",
    ]:
        rows = page.query_selector_all(sel)
        if rows:
            info["row_count"] = len(rows)
            info["row_selector"] = sel
            break

    # No results テキスト
    for sel in [".sh-research__no-results", "[class*='no-results']", "[class*='empty-state']"]:
        el = page.query_selector(sel)
        if el:
            info["no_results_text"] = el.inner_text().strip()[:200]
            break

    # 最初の行フィールド抽出 (タイトル / 価格 / sold数 / date last sold)
    if info.get("row_count", 0) > 0 and info.get("row_selector"):
        first_row = page.query_selector(info["row_selector"])
        if first_row:
            info["first_row_text"] = first_row.inner_text().replace("\n", " | ")[:300]
            # 各セルのクラス名を記録
            cells = first_row.query_selector_all("td, [class*='cell']")
            cell_classes = []
            for c in cells:
                cls = c.get_attribute("class") or ""
                txt = c.inner_text().strip()[:60]
                cell_classes.append({"class": cls[:80], "text": txt})
            info["first_row_cells"] = cell_classes

    # URL
    info["current_url"] = page.url

    return info


def probe_column_headers(page) -> list:
    """テーブルのカラムヘッダを取得してソート可否を判定。"""
    headers = []
    for sel in [
        "thead th",
        "tr[class*='header'] th",
        "tr[class*='header'] [class*='header-cell']",
        "[class*='table-header'] [class*='col']",
    ]:
        els = page.query_selector_all(sel)
        if els:
            for el in els:
                txt = el.inner_text().strip()
                cls = el.get_attribute("class") or ""
                # aria-sort や data-sort 属性
                aria_sort = el.get_attribute("aria-sort") or ""
                data_sort = el.get_attribute("data-sort") or ""
                headers.append({
                    "text": txt,
                    "class": cls[:80],
                    "aria_sort": aria_sort,
                    "data_sort": data_sort,
                })
            break
    return headers


# ---------------------------------------------------------------------------
# main probe
# ---------------------------------------------------------------------------

def main() -> None:
    from playwright.sync_api import sync_playwright

    page_load_count = 0
    MAX_LOADS = 6

    results = {}
    now_ms = int(time.time() * 1000)

    log("=== W229 Terapeak harvest probe START ===")
    log(f"PROBE_DIR: {PROBE_DIR}")

    with sync_playwright() as p:
        log("Connecting to CDP Chrome ...")
        try:
            browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
        except Exception as e:
            log(f"ERROR: CDP connect failed: {e}")
            sys.exit(1)

        if not browser.contexts:
            log("ERROR: no browser context")
            sys.exit(1)

        ctx = browser.contexts[0]

        # 新規タブを開く (既存タブを汚染しない)
        probe_page = ctx.new_page()
        log("New probe tab opened.")

        try:
            # ======================================================================
            # PROBE 1: カテゴリ起点一覧
            # keywords 空 + categoryId=293 (Consumer Electronics)
            # ======================================================================
            log("\n--- PROBE 1: Category-only (keywords='', categoryId=293, 365d) ---")
            url1 = build_url(keywords="", category_id=293, day_range=365, now_ms=now_ms)
            log(f"URL: {url1}")
            probe_page.goto(url1, wait_until="domcontentloaded", timeout=30000)
            page_load_count += 1
            loaded = wait_terapeak_loaded(probe_page)
            log(f"Loaded: {loaded}  [page loads: {page_load_count}/{MAX_LOADS}]")
            save_html(probe_page, "probe1_category293_keywords_empty")
            save_screenshot(probe_page, "probe1_category293_keywords_empty")
            info1 = extract_results_info(probe_page)
            results["probe1_category_only"] = {
                "url": url1,
                "loaded": loaded,
                "info": info1,
                "current_url_after_nav": probe_page.url,
            }
            log(f"  header_text: {info1.get('header_text', 'N/A')}")
            log(f"  row_count: {info1.get('row_count', 'N/A')}")
            log(f"  no_results: {info1.get('no_results_text', '')}")
            log(f"  current URL after nav: {probe_page.url[:120]}")

            # ======================================================================
            # PROBE 2: カラムヘッダ + ソート調査 (probe1 の結果ページで実施)
            # ======================================================================
            log("\n--- PROBE 2: Column headers & sort (on probe1 result page) ---")
            headers = probe_column_headers(probe_page)
            results["probe2_column_headers"] = headers
            for h in headers:
                log(f"  col: {h['text']!r:30s} aria_sort={h['aria_sort']!r} data_sort={h['data_sort']!r}")

            # "Date last sold" ヘッダをクリックしてソートURLを記録
            date_header = None
            for sel_candidate in [
                "th:has-text('Date last sold')",
                "th:has-text('Date')",
                "[class*='header']:has-text('Date last sold')",
                "th:has-text('Last sold')",
            ]:
                el = probe_page.query_selector(sel_candidate)
                if el:
                    date_header = el
                    log(f"  Found date header via: {sel_candidate}")
                    break

            sort_url_after_click = None
            if date_header:
                log("  Clicking 'Date last sold' header ...")
                date_header.click()
                probe_page.wait_for_timeout(2000)
                sort_url_after_click = probe_page.url
                log(f"  URL after click: {sort_url_after_click[:150]}")
                results["probe2_sort_url_after_click"] = sort_url_after_click
                # 2回クリックで descending
                date_header.click()
                probe_page.wait_for_timeout(2000)
                sort_url_desc = probe_page.url
                log(f"  URL after 2nd click (desc?): {sort_url_desc[:150]}")
                results["probe2_sort_url_desc"] = sort_url_desc
            else:
                log("  'Date last sold' header NOT found on this page.")

            # ======================================================================
            # PROBE 3: 期間 dayRange=7 (Last 7 days)
            # ======================================================================
            if page_load_count < MAX_LOADS:
                log("\n--- PROBE 3: dayRange=7 (Last 7 days), keywords empty, cat=293 ---")
                url3 = build_url(keywords="", category_id=293, day_range=7, now_ms=now_ms)
                log(f"URL: {url3}")
                probe_page.goto(url3, wait_until="domcontentloaded", timeout=30000)
                page_load_count += 1
                wait_terapeak_loaded(probe_page)
                save_html(probe_page, "probe3_dayrange7")
                save_screenshot(probe_page, "probe3_dayrange7")
                info3 = extract_results_info(probe_page)
                results["probe3_dayrange7"] = {
                    "url": url3,
                    "info": info3,
                    "current_url": probe_page.url,
                }
                log(f"  header_text: {info3.get('header_text', 'N/A')}")
                log(f"  row_count: {info3.get('row_count', 'N/A')}")
                log(f"  [page loads: {page_load_count}/{MAX_LOADS}]")
            else:
                log("PROBE 3 skipped (page load limit)")

            # ======================================================================
            # PROBE 4: 期間 dayRange=730 (Last 2 years)
            # ======================================================================
            if page_load_count < MAX_LOADS:
                log("\n--- PROBE 4: dayRange=730 (Last 2 years), keywords empty, cat=293 ---")
                url4 = build_url(keywords="", category_id=293, day_range=730, now_ms=now_ms)
                log(f"URL: {url4}")
                probe_page.goto(url4, wait_until="domcontentloaded", timeout=30000)
                page_load_count += 1
                wait_terapeak_loaded(probe_page)
                save_html(probe_page, "probe4_dayrange730")
                save_screenshot(probe_page, "probe4_dayrange730")
                info4 = extract_results_info(probe_page)
                results["probe4_dayrange730"] = {
                    "url": url4,
                    "info": info4,
                    "current_url": probe_page.url,
                }
                log(f"  header_text: {info4.get('header_text', 'N/A')}")
                log(f"  row_count: {info4.get('row_count', 'N/A')}")
                log(f"  [page loads: {page_load_count}/{MAX_LOADS}]")
            else:
                log("PROBE 4 skipped (page load limit)")

            # ======================================================================
            # PROBE 5: 価格フィルタ ($100+ minPrice)
            # ======================================================================
            if page_load_count < MAX_LOADS:
                log("\n--- PROBE 5: Price filter ($100+), keywords empty, cat=293, 365d ---")
                url5 = build_url(
                    keywords="",
                    category_id=293,
                    day_range=365,
                    now_ms=now_ms,
                    extra={"minPrice": 100},
                )
                log(f"URL: {url5}")
                probe_page.goto(url5, wait_until="domcontentloaded", timeout=30000)
                page_load_count += 1
                wait_terapeak_loaded(probe_page)
                save_html(probe_page, "probe5_minprice100")
                save_screenshot(probe_page, "probe5_minprice100")
                info5 = extract_results_info(probe_page)
                results["probe5_minprice"] = {
                    "url": url5,
                    "info": info5,
                    "current_url": probe_page.url,
                }
                log(f"  header_text: {info5.get('header_text', 'N/A')}")
                log(f"  row_count: {info5.get('row_count', 'N/A')}")
                log(f"  [page loads: {page_load_count}/{MAX_LOADS}]")
            else:
                log("PROBE 5 skipped (page load limit)")

            # ======================================================================
            # PROBE 6: ページング (offset=50)
            # ======================================================================
            if page_load_count < MAX_LOADS:
                log("\n--- PROBE 6: Paging offset=50, limit=50, cat=293, 365d ---")
                url6 = build_url(
                    keywords="",
                    category_id=293,
                    day_range=365,
                    now_ms=now_ms,
                    offset=50,
                    limit=50,
                )
                log(f"URL: {url6}")
                probe_page.goto(url6, wait_until="domcontentloaded", timeout=30000)
                page_load_count += 1
                wait_terapeak_loaded(probe_page)
                save_html(probe_page, "probe6_offset50")
                save_screenshot(probe_page, "probe6_offset50")
                info6 = extract_results_info(probe_page)
                results["probe6_paging"] = {
                    "url": url6,
                    "info": info6,
                    "current_url": probe_page.url,
                }
                log(f"  header_text: {info6.get('header_text', 'N/A')}")
                log(f"  row_count: {info6.get('row_count', 'N/A')}")
                log(f"  [page loads: {page_load_count}/{MAX_LOADS}]")
            else:
                log("PROBE 6 skipped (page load limit)")

        except KeyboardInterrupt:
            log("Interrupted.")
        except Exception as e:
            import traceback
            log(f"ERROR: {e}")
            traceback.print_exc()
            results["error"] = str(e)
            try:
                save_html(probe_page, "probe_error_state")
                save_screenshot(probe_page, "probe_error_state")
            except Exception:
                pass
        finally:
            log("\nClosing probe tab ...")
            try:
                probe_page.close()
            except Exception:
                pass

    # ======================================================================
    # 結果保存
    # ======================================================================
    out_path = PROBE_DIR / "probe_results_2026_06_10.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\nResults saved: {out_path}")
    log(f"Total page loads used: {page_load_count}/{MAX_LOADS}")

    # サマリ表示
    log("\n=== SUMMARY ===")
    log(f"Probe 1 (category-only): row_count={results.get('probe1_category_only', {}).get('info', {}).get('row_count', 'N/A')}")
    if "probe3_dayrange7" in results:
        log(f"Probe 3 (7d): header={results['probe3_dayrange7']['info'].get('header_text', 'N/A')}")
    if "probe4_dayrange730" in results:
        log(f"Probe 4 (730d): header={results['probe4_dayrange730']['info'].get('header_text', 'N/A')}")
    if "probe5_minprice" in results:
        log(f"Probe 5 (minPrice): row_count={results['probe5_minprice']['info'].get('row_count', 'N/A')}")
    if "probe6_paging" in results:
        log(f"Probe 6 (offset=50): row_count={results['probe6_paging']['info'].get('row_count', 'N/A')}")


if __name__ == "__main__":
    main()
