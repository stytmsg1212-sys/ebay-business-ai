"""
W229 Terapeak harvest probe (第 3 弾) — ソートクリック URL 変化確認
====================================================================
第 2 弾で row_count 取得 evaluate セレクタが誤りだったため、
PROBE B (sort) がスキップされた。修正版。

保存先: data/terapeak_probe/
"""
import sys
import time

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

CAT_ID = 15052
TEST_KW = "sony headphones"


def build_url(keywords, day_range=365, offset=0, limit=50, now_ms=None, extra=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    start_ms = now_ms - day_range * 24 * 3600 * 1000
    params = {
        "marketplace": "EBAY-US",
        "keywords": keywords,
        "dayRange": day_range,
        "endDate": now_ms,
        "startDate": start_ms,
        "categoryId": CAT_ID,
        "offset": offset,
        "limit": limit,
        "tabName": "SOLD",
        "sellerCountry": "SellerLocation:::JP",
    }
    if extra:
        params.update(extra)
    return "https://www.ebay.com/sh/research?" + urlencode(params)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_rows(page, timeout_s=40):
    """research-table-row が DOM に出るまで待機 (正しいセレクタ)。"""
    deadline = time.time() + timeout_s
    try:
        page.wait_for_load_state("networkidle", timeout=int(timeout_s * 1000))
    except Exception:
        pass
    while time.time() < deadline:
        try:
            count = page.evaluate("""() => {
                return document.querySelectorAll('tr.research-table-row').length;
            }""")
            if count > 0:
                return count
        except Exception:
            pass
        time.sleep(1.5)
    return 0


def save_html(page, name):
    try:
        html = page.evaluate("() => document.documentElement.outerHTML")
    except Exception:
        html = page.content()
    p = PROBE_DIR / f"{name}_live.html"
    p.write_text(html, encoding="utf-8")
    log(f"  saved {p.name} ({len(html):,} bytes)")
    return html


def save_screenshot(page, name):
    p = PROBE_DIR / f"{name}.png"
    page.screenshot(path=str(p))
    log(f"  screenshot {p.name}")


def main():
    from playwright.sync_api import sync_playwright
    now_ms = int(time.time() * 1000)
    results = {}
    page_load_count = 0

    log("=== W229 probe #3 (sort + re-verify rows) ===")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
        ctx = browser.contexts[0]
        probe_page = ctx.new_page()
        log("New probe tab opened.")

        try:
            # ============================================================
            # PROBE G: keywords あり 365d — 正しいセレクタで行数確認
            # ============================================================
            log(f"\n--- PROBE G: {TEST_KW!r} 365d, correct row selector ---")
            url_g = build_url(TEST_KW, day_range=365, now_ms=now_ms)
            probe_page.goto(url_g, wait_until="domcontentloaded", timeout=35000)
            page_load_count += 1
            row_count = wait_rows(probe_page)
            log(f"Row count (research-table-row): {row_count}  [loads: {page_load_count}]")
            results["probeG_row_count"] = row_count

            if row_count == 0:
                log("  No rows found, saving debug HTML...")
                save_html(probe_page, "probe3g_debug")
                save_screenshot(probe_page, "probe3g_debug")
            else:
                # カラムヘッダ確認
                headers_info = probe_page.evaluate("""() => {
                    const ths = document.querySelectorAll('tr.research-table-header th');
                    return Array.from(ths).map(th => ({
                        class: th.className,
                        text: th.innerText.trim().split('\\n')[0],
                        tabindex: th.getAttribute('tabindex'),
                        aria_sort: th.getAttribute('aria-sort') || '',
                        sortable: th.className.includes('sortable'),
                        cursor: window.getComputedStyle(th).cursor,
                    }));
                }""")
                results["probeG_headers"] = headers_info
                log("Column headers:")
                for h in headers_info:
                    log(f"  [{h['text']!r:25s}] sortable={h['sortable']} tabindex={h['tabindex']} cursor={h['cursor']}")

                # ============================================================
                # PROBE H: ソートクリック ("Date last sold")
                # ============================================================
                log("\n--- PROBE H: Click 'Date last sold' sort ---")
                url_before = probe_page.url

                # th.research-table-header__date-last-sold をクリック
                date_th = probe_page.locator('th.research-table-header__date-last-sold')
                if date_th.count() > 0:
                    log(f"  Found date-last-sold th, clicking...")
                    date_th.first.click(timeout=5000)
                    probe_page.wait_for_timeout(3000)
                    url_after_1st = probe_page.url
                    row_count_after = probe_page.evaluate("""() =>
                        document.querySelectorAll('tr.research-table-row').length""")
                    log(f"  URL before: {url_before[:100]}")
                    log(f"  URL after 1st click: {url_after_1st[:100]}")
                    log(f"  URL changed: {url_after_1st != url_before}")
                    log(f"  Rows after click: {row_count_after}")

                    # sort 状態確認
                    sort_state = probe_page.evaluate("""() => {
                        const th = document.querySelector('th.research-table-header__date-last-sold');
                        if (!th) return null;
                        return {
                            aria_sort: th.getAttribute('aria-sort'),
                            class: th.className,
                        };
                    }""")
                    log(f"  Sort state after 1st click: {sort_state}")

                    # 2回目クリック
                    date_th.first.click(timeout=5000)
                    probe_page.wait_for_timeout(3000)
                    url_after_2nd = probe_page.url
                    sort_state_2nd = probe_page.evaluate("""() => {
                        const th = document.querySelector('th.research-table-header__date-last-sold');
                        if (!th) return null;
                        return {
                            aria_sort: th.getAttribute('aria-sort'),
                            class: th.className,
                        };
                    }""")
                    log(f"  URL after 2nd click: {url_after_2nd[:100]}")
                    log(f"  Sort state after 2nd click: {sort_state_2nd}")

                    # ソート後の最初の行の date を確認
                    first_date_after_sort = probe_page.evaluate("""() => {
                        const rows = document.querySelectorAll('tr.research-table-row');
                        if (rows.length === 0) return null;
                        const dateCell = rows[0].querySelector('[class*="dateLastSold"]');
                        return dateCell ? dateCell.innerText.trim() : null;
                    }""")
                    log(f"  First row date after 2nd click: {first_date_after_sort!r}")

                    save_html(probe_page, "probe3h_after_sort")
                    save_screenshot(probe_page, "probe3h_after_sort")

                    results["probeH_sort"] = {
                        "url_before": url_before,
                        "url_after_1st": url_after_1st,
                        "url_changed_1st": url_after_1st != url_before,
                        "sort_state_1st": sort_state,
                        "url_after_2nd": url_after_2nd,
                        "url_changed_2nd": url_after_2nd != url_after_1st,
                        "sort_state_2nd": sort_state_2nd,
                        "first_date_after_sort": first_date_after_sort,
                    }
                else:
                    log("  date-last-sold th NOT found")
                    results["probeH_sort"] = {"not_found": True}

                # ============================================================
                # PROBE I: ソート後の URL 直叩きで状態再現可能か
                # ============================================================
                if results.get("probeH_sort", {}).get("url_changed_1st"):
                    sort_url = results["probeH_sort"]["url_after_1st"]
                    log(f"\n--- PROBE I: Re-navigate to sort URL ---")
                    log(f"  URL: {sort_url[:150]}")
                    probe_page.goto(sort_url, wait_until="domcontentloaded", timeout=35000)
                    page_load_count += 1
                    rows_i = wait_rows(probe_page)
                    first_date_i = probe_page.evaluate("""() => {
                        const rows = document.querySelectorAll('tr.research-table-row');
                        if (rows.length === 0) return null;
                        const dateCell = rows[0].querySelector('[class*="dateLastSold"]');
                        return dateCell ? dateCell.innerText.trim() : null;
                    }""")
                    sort_state_i = probe_page.evaluate("""() => {
                        const th = document.querySelector('th.research-table-header__date-last-sold');
                        return th ? {aria_sort: th.getAttribute('aria-sort'), class: th.className} : null;
                    }""")
                    log(f"  Rows: {rows_i}  First date: {first_date_i!r}")
                    log(f"  Sort state: {sort_state_i}")
                    results["probeI_sort_url_replay"] = {
                        "url": sort_url,
                        "rows": rows_i,
                        "first_date": first_date_i,
                        "sort_state": sort_state_i,
                    }
                    log(f"  [loads: {page_load_count}]")

        except Exception as e:
            import traceback
            log(f"ERROR: {e}")
            traceback.print_exc()
            results["error"] = str(e)
        finally:
            probe_page.close()

    out_path = PROBE_DIR / "probe3_results_2026_06_10.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\nResults: {out_path}")
    log(f"Page loads: {page_load_count}")

    log("\n=== SUMMARY ===")
    log(f"Probe G row_count: {results.get('probeG_row_count', 'N/A')}")
    h = results.get("probeH_sort", {})
    log(f"Probe H sort url_changed_1st: {h.get('url_changed_1st', 'N/A')}")
    log(f"  sort_state_1st: {h.get('sort_state_1st', 'N/A')}")
    log(f"  sort_state_2nd: {h.get('sort_state_2nd', 'N/A')}")
    log(f"  first_date_after_sort: {h.get('first_date_after_sort', 'N/A')}")
    i = results.get("probeI_sort_url_replay", {})
    log(f"Probe I (URL replay): first_date={i.get('first_date', 'N/A')}  sort_state={i.get('sort_state', 'N/A')}")


if __name__ == "__main__":
    main()
