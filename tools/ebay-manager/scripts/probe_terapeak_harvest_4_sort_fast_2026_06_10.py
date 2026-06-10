"""
W229 Terapeak harvest probe (第 4 弾) — ソートクリック URL 変化確認 (高速版)
=============================================================================
networkidle を使わず wait_for_timeout で待機。
"""
import sys, time, json, re
from pathlib import Path
from urllib.parse import urlencode

for s in ("stdout", "stderr"):
    stream = getattr(sys, s, None)
    if stream and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE_DIR = Path(__file__).resolve().parent.parent
PROBE_DIR = BASE_DIR / "data" / "terapeak_probe"
PROBE_DIR.mkdir(parents=True, exist_ok=True)
CDP_ENDPOINT = "http://localhost:9222"
CAT_ID = 15052
TEST_KW = "sony headphones"

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

def build_url(kw, day_range=365, now_ms=None, extra=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    start_ms = now_ms - day_range * 24 * 3600 * 1000
    params = dict(marketplace="EBAY-US", keywords=kw, dayRange=day_range,
                  endDate=now_ms, startDate=start_ms, categoryId=CAT_ID,
                  offset=0, limit=50, tabName="SOLD",
                  sellerCountry="SellerLocation:::JP")
    if extra:
        params.update(extra)
    return "https://www.ebay.com/sh/research?" + urlencode(params)

def wait_rows_timeout(page, wait_s=12):
    """networkidle を使わず timeout ポーリングで行を待つ。"""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            n = page.evaluate("() => document.querySelectorAll('tr.research-table-row').length")
            if n > 0:
                return n
        except Exception:
            pass
        time.sleep(2)
    return 0

def save(page, name):
    try:
        html = page.evaluate("() => document.documentElement.outerHTML")
    except Exception:
        html = page.content()
    path = PROBE_DIR / f"{name}_live.html"
    path.write_text(html, encoding="utf-8")
    page.screenshot(path=str(PROBE_DIR / f"{name}.png"))
    log(f"  saved {name} ({len(html):,})")
    return html


def main():
    from playwright.sync_api import sync_playwright
    now_ms = int(time.time() * 1000)
    results = {}
    loads = 0

    log("=== probe #4 sort fast ===")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
        ctx = browser.contexts[0]
        pg = ctx.new_page()
        log("tab opened")

        try:
            # PROBE G2: keywords あり 365d, domcontentloaded + timeout
            log(f"\n--- G2: {TEST_KW!r} 365d ---")
            url = build_url(TEST_KW, now_ms=now_ms)
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            loads += 1
            pg.wait_for_timeout(8000)  # React render 待機
            n = wait_rows_timeout(pg, wait_s=15)
            log(f"  rows: {n}  [loads: {loads}]")
            results["g2_rows"] = n

            if n > 0:
                # カラムヘッダ
                hdrs = pg.evaluate("""() => Array.from(
                    document.querySelectorAll('tr.research-table-header th')
                ).map(th => ({
                    class: th.className,
                    text: th.innerText.trim().split('\\n')[0],
                    sortable: th.className.includes('sortable'),
                    cursor: getComputedStyle(th).cursor,
                }))""")
                log("Headers:")
                for h in hdrs:
                    log(f"  {h['text']!r:25s} sortable={h['sortable']} cursor={h['cursor']}")
                results["g2_headers"] = hdrs

                # 最初の行のデータ
                first_row = pg.evaluate("""() => {
                    const row = document.querySelector('tr.research-table-row');
                    if (!row) return null;
                    const cells = {};
                    row.querySelectorAll('td').forEach(td => {
                        const m = td.className.match(/research-table-row__([a-zA-Z]+)/);
                        if (m) cells[m[1]] = td.innerText.trim();
                    });
                    return cells;
                }""")
                log(f"  First row: {first_row}")
                results["g2_first_row"] = first_row

                # PROBE H2: Sort クリック (date-last-sold)
                log("\n--- H2: Sort click ---")
                url_before = pg.url
                date_th = pg.locator("th.research-table-header__date-last-sold")
                if date_th.count() > 0:
                    date_th.first.click(timeout=5000)
                    pg.wait_for_timeout(4000)
                    url_1st = pg.url
                    n_after = pg.evaluate("() => document.querySelectorAll('tr.research-table-row').length")
                    sort_cls = pg.evaluate("""() => {
                        const th = document.querySelector('th.research-table-header__date-last-sold');
                        return th ? th.className : null;
                    }""")
                    first_date_1st = pg.evaluate("""() => {
                        const rows = document.querySelectorAll('tr.research-table-row');
                        if (!rows.length) return null;
                        const td = rows[0].querySelector('[class*="dateLastSold"]');
                        return td ? td.innerText.trim() : null;
                    }""")
                    log(f"  After 1st click:")
                    log(f"    URL before: {url_before[-80:]}")
                    log(f"    URL after:  {url_1st[-80:]}")
                    log(f"    URL changed: {url_1st != url_before}")
                    log(f"    rows: {n_after}  first_date: {first_date_1st!r}")
                    log(f"    th class: {sort_cls}")

                    # 2回目クリック
                    date_th.first.click(timeout=5000)
                    pg.wait_for_timeout(4000)
                    url_2nd = pg.url
                    sort_cls_2nd = pg.evaluate("""() => {
                        const th = document.querySelector('th.research-table-header__date-last-sold');
                        return th ? th.className : null;
                    }""")
                    first_date_2nd = pg.evaluate("""() => {
                        const rows = document.querySelectorAll('tr.research-table-row');
                        if (!rows.length) return null;
                        const td = rows[0].querySelector('[class*="dateLastSold"]');
                        return td ? td.innerText.trim() : null;
                    }""")
                    log(f"  After 2nd click:")
                    log(f"    URL: {url_2nd[-80:]}")
                    log(f"    URL changed: {url_2nd != url_1st}")
                    log(f"    first_date: {first_date_2nd!r}")
                    log(f"    th class: {sort_cls_2nd}")

                    save(pg, "probe4h_after_sort")

                    results["h2_sort"] = {
                        "url_before": url_before,
                        "url_after_1st": url_1st,
                        "url_changed_1st": url_1st != url_before,
                        "th_class_1st": sort_cls,
                        "first_date_1st": first_date_1st,
                        "url_after_2nd": url_2nd,
                        "url_changed_2nd": url_2nd != url_1st,
                        "th_class_2nd": sort_cls_2nd,
                        "first_date_2nd": first_date_2nd,
                    }

                    # PROBE I2: ソート後 URL を直叩き
                    if url_1st != url_before:
                        log(f"\n--- I2: Re-navigate to sorted URL ---")
                        pg.goto(url_1st, wait_until="domcontentloaded", timeout=30000)
                        loads += 1
                        pg.wait_for_timeout(8000)
                        n_i = wait_rows_timeout(pg, 10)
                        fd_i = pg.evaluate("""() => {
                            const rows = document.querySelectorAll('tr.research-table-row');
                            if (!rows.length) return null;
                            const td = rows[0].querySelector('[class*="dateLastSold"]');
                            return td ? td.innerText.trim() : null;
                        }""")
                        sc_i = pg.evaluate("""() => {
                            const th = document.querySelector('th.research-table-header__date-last-sold');
                            return th ? th.className : null;
                        }""")
                        log(f"  rows: {n_i}  first_date: {fd_i!r}  th_class: {sc_i}")
                        results["i2_url_replay"] = {
                            "rows": n_i, "first_date": fd_i, "th_class": sc_i,
                            "sort_preserved": "sort" in url_1st.lower() or fd_i == first_date_1st,
                        }
                        log(f"  [loads: {loads}]")
                else:
                    log("  date-last-sold th NOT found")
                    results["h2_sort"] = {"not_found": True}
            else:
                log("  No rows — saving debug")
                save(pg, "probe4g2_debug")

        except Exception as e:
            import traceback
            log(f"ERROR: {e}")
            traceback.print_exc()
            results["error"] = str(e)
            try:
                save(pg, "probe4_error")
            except Exception:
                pass
        finally:
            pg.close()
            log("tab closed")

    out = PROBE_DIR / "probe4_results_2026_06_10.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Saved: {out}")
    log(f"Loads used: {loads}")

    log("\n=== SUMMARY ===")
    log(f"G2 rows: {results.get('g2_rows', 'N/A')}")
    h = results.get("h2_sort", {})
    log(f"H2 sort url_changed_1st: {h.get('url_changed_1st', 'N/A')}")
    log(f"  th_class_1st: {h.get('th_class_1st', 'N/A')}")
    log(f"  th_class_2nd: {h.get('th_class_2nd', 'N/A')}")
    log(f"  first_date after sort: {h.get('first_date_2nd', 'N/A')!r}")
    i = results.get("i2_url_replay", {})
    log(f"I2 replay first_date: {i.get('first_date', 'N/A')!r}")


if __name__ == "__main__":
    main()
