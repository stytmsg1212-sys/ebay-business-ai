"""
Terapeak exclusion-only probe 続き (probe5 continuation)
残り 3 ロードで:
  - UI カテゴリ検索 (修正版)
  - 1文字 "a" + 除外語
  - 1文字 "e" + 除外語
  - 括弧記法のヘッダテキスト / 件数詳細確認は既存 HTML から読む
"""
import sys
import time
import json
from pathlib import Path
from urllib.parse import urlencode

for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE_DIR = Path(__file__).resolve().parent.parent
PROBE_DIR = BASE_DIR / "data" / "terapeak_probe"
PROBE_DIR.mkdir(parents=True, exist_ok=True)

CDP_ENDPOINT = "http://localhost:9222"
MAX_LOADS = 3  # この実行での上限


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def build_url(keywords: str, category_id: int = 0, day_range: int = 30,
              offset: int = 0, limit: int = 50, now_ms: int | None = None) -> str:
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
        "tabName": "SOLD",
        "sellerCountry": "SellerLocation:::JP",
        "sorting": "-datelastsold",
        "minPrice": 10,
    }
    return "https://www.ebay.com/sh/research?" + urlencode(params)


def save_html(page, name: str) -> Path:
    content = page.content()
    p = PROBE_DIR / f"probe5_{name}.html"
    p.write_text(content, encoding="utf-8")
    log(f"  -> saved {p.name} ({len(content):,} bytes)")
    return p


def wait_result(page, timeout_ms: int = 25000) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        rows = page.query_selector_all("tr.research-table-row, tr.research-table__row")
        if rows:
            return True
        body = page.inner_text("body")[:2000]
        if "didn't return any results" in body or "no results" in body.lower():
            return True
        if "/sh/research" not in page.url:
            return True
        page.wait_for_timeout(800)
    return False


def extract_info(page) -> dict:
    info = {"current_url": page.url}
    rows = page.query_selector_all("tr.research-table-row, tr.research-table__row")
    info["row_count"] = len(rows)
    for sel in [".results-header__left", "[class*='results-header']", "[class*='research-header']"]:
        el = page.query_selector(sel)
        if el:
            info["header_text"] = el.inner_text().strip()[:200]
            break
    body = page.inner_text("body")[:3000]
    if "didn't return any results" in body:
        info["no_results"] = True
    if rows:
        info["first_row"] = rows[0].inner_text().replace("\n", " | ")[:200]
    # 結果総件数を URL から解析
    if "offset=0" in page.url:
        info["pagination_at_start"] = True
    return info


def run_url_probe(page, load_counter: list, label: str, url: str) -> dict:
    log(f"\n--- {label} ---")
    log(f"  URL: {url[:140]}")
    load_counter[0] += 1
    log(f"  [load {load_counter[0]}/{MAX_LOADS}]")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    wait_result(page)
    save_html(page, label)
    info = extract_info(page)
    log(f"  row_count={info.get('row_count')}  no_results={info.get('no_results', False)}")
    log(f"  current_url={info['current_url'][:120]}")
    if info.get("first_row"):
        log(f"  first_row={info['first_row'][:100]}")
    if info.get("header_text"):
        log(f"  header_text={info['header_text'][:100]}")
    return {"label": label, "url": url, "info": info}


def main() -> None:
    from playwright.sync_api import sync_playwright

    now_ms = int(time.time() * 1000)
    load_counter = [0]
    results = []

    log("=== Terapeak exclusion-only probe CONTINUATION START ===")

    with sync_playwright() as p:
        log("Connecting to CDP Chrome ...")
        try:
            browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
        except Exception as e:
            log(f"ERROR: CDP connect failed: {e}")
            sys.exit(1)

        ctx = browser.contexts[0]
        page = ctx.new_page()
        log("New probe tab opened.")

        try:
            # ====================================================================
            # PROBE 5-C (続き): UI 経由カテゴリ検索
            # キーワード空のまま検索を試みる (Playwright fill のみ使用)
            # ====================================================================
            if load_counter[0] < MAX_LOADS:
                log("\n--- probe5_ui_empty_kw (UI操作、修正版) ---")
                load_counter[0] += 1
                log(f"  [load {load_counter[0]}/{MAX_LOADS}]")
                page.goto(
                    "https://www.ebay.com/sh/research",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                page.wait_for_timeout(3000)

                # キーワード入力欄を空にする
                kw_input = None
                for sel in [
                    "input[placeholder*='keyword']",
                    "input[placeholder*='Keyword']",
                    "input[name='keywords']",
                    "[data-testid='research-keyword-input']",
                    ".sh-research__keywords-input",
                    "input[class*='keyword']",
                ]:
                    el = page.query_selector(sel)
                    if el:
                        kw_input = el
                        log(f"  Found keyword input via: {sel}")
                        break

                search_btn = None
                if kw_input:
                    # fill で空文字 → select-all delete で確実にクリア
                    kw_input.click(click_count=3)
                    page.keyboard.press("Backspace")
                    kw_input.fill("")
                    log(f"  Keyword field cleared. value={kw_input.input_value()!r}")

                    for sel in [
                        "button[data-testid='research-search-button']",
                        "button.sh-research__search-btn",
                        "button:has-text('Search')",
                        "[class*='search-btn']",
                        "button[type='submit']",
                    ]:
                        el = page.query_selector(sel)
                        if el:
                            search_btn = el
                            log(f"  Found search button via: {sel}")
                            break

                before_url = page.url
                if search_btn:
                    search_btn.click()
                    page.wait_for_timeout(5000)
                    wait_result(page)
                    log(f"  URL after click: {page.url[:120]}")
                else:
                    log("  Search button not found, skipping click.")

                save_html(page, "ui_empty_kw")
                info_c = extract_info(page)
                info_c["kw_found"] = kw_input is not None
                info_c["btn_found"] = search_btn is not None
                info_c["before_url"] = before_url
                log(f"  row_count={info_c.get('row_count')}  no_results={info_c.get('no_results', False)}")
                results.append({"label": "ui_empty_kw", "info": info_c})

            # ====================================================================
            # PROBE 5-D: 1文字 "a" + 除外語
            # ====================================================================
            if load_counter[0] < MAX_LOADS:
                kw_d = "a -Card -camera -Vuitton -Hermes -GUCCI -Mint -COACH"
                url_d = build_url(kw_d, day_range=30, now_ms=now_ms)
                r_d = run_url_probe(page, load_counter, "single_a_excl", url_d)
                results.append(r_d)

            # ====================================================================
            # PROBE 5-E: 1文字 "e" + 除外語
            # ====================================================================
            if load_counter[0] < MAX_LOADS:
                kw_e = "e -Card -camera -Vuitton -Hermes -GUCCI -Mint -COACH"
                url_e = build_url(kw_e, day_range=30, now_ms=now_ms)
                r_e = run_url_probe(page, load_counter, "single_e_excl", url_e)
                results.append(r_e)

        except KeyboardInterrupt:
            log("Interrupted.")
        except Exception as ex:
            import traceback
            log(f"ERROR: {ex}")
            traceback.print_exc()
            try:
                save_html(page, "cont_error_state")
            except Exception:
                pass
            results.append({"label": "error", "error": str(ex)})
        finally:
            log("\nClosing probe tab ...")
            try:
                page.close()
            except Exception:
                pass

    out_path = PROBE_DIR / "probe5_cont_results_2026_06_10.json"
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"\nResults saved: {out_path}")
    log(f"Total page loads this run: {load_counter[0]}/{MAX_LOADS}")

    log("\n=== SUMMARY ===")
    for r in results:
        lbl = r.get("label", "?")
        info = r.get("info", {})
        rows = info.get("row_count", "?")
        no_r = info.get("no_results", False)
        log(f"  {lbl}: rows={rows}  no_results={no_r}")


if __name__ == "__main__":
    main()
