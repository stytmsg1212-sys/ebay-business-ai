"""
Terapeak「除外語のみ」抜け道 probe (2026-06-10)
================================================
直近 probe で確定済み:
  - keywords="" + categoryId → リダイレクトで結果ゼロ
  - "-abcd -Card ..." 除外語のみ  → "no results"

本 probe で試す 4 案 (最大 6 ページロード):
  1. ワイルドカード: keywords="* -Card -camera -Vuitton -Hermes -GUCCI -Mint -COACH"
  2. 括弧記法:       keywords="(-Card) (-camera) (-Vuitton) (-Hermes) (-GUCCI) (-Mint) (-COACH)"
  3. UI 経由:        Seller Hub Research 画面をキーワード空・カテゴリ 293 で検索ボタン押下
  4. 1 文字近似:     keywords="a -Card -camera -Vuitton -Hermes -GUCCI -Mint -COACH"
               + "e -Card -camera ..."

保存先: data/terapeak_probe/probe5_<name>.html
"""
import sys
import time
import json
from pathlib import Path
from urllib.parse import urlencode

# Windows cp932 対策
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
MAX_LOADS = 6


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def build_url(keywords: str, category_id: int = 0, day_range: int = 365,
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
    """テーブル行 or no-results テキストが出るまで待つ (SPA 対応)。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        # テーブル行
        rows = page.query_selector_all("tr.research-table-row, tr.research-table__row")
        if rows:
            return True
        # no results
        body = page.inner_text("body")[:2000]
        if "didn't return any results" in body or "no results" in body.lower():
            return True
        # リダイレクト先 (research ページでなくなった)
        if "/sh/research" not in page.url:
            return True
        page.wait_for_timeout(800)
    return False


def extract_info(page) -> dict:
    info = {"current_url": page.url}
    # 行数
    rows = page.query_selector_all("tr.research-table-row, tr.research-table__row")
    info["row_count"] = len(rows)
    # ヘッダ
    for sel in [".results-header__left", "[class*='results-header']", "[class*='research-header']"]:
        el = page.query_selector(sel)
        if el:
            info["header_text"] = el.inner_text().strip()[:200]
            break
    # no-results テキスト
    body = page.inner_text("body")[:3000]
    if "didn't return any results" in body:
        info["no_results"] = True
    # 最初の行タイトル
    if rows:
        info["first_row"] = rows[0].inner_text().replace("\n", " | ")[:200]
    return info


def run_probe(page, load_counter: list, label: str, url: str, now_ms: int) -> dict:
    """URL を goto して結果を返す。load_counter は [n] の形式で副作用更新。"""
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
    return {"label": label, "url": url, "info": info}


def main() -> None:
    from playwright.sync_api import sync_playwright

    now_ms = int(time.time() * 1000)
    load_counter = [0]
    results = []

    log("=== Terapeak exclusion-only probe (probe5) START ===")

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
            # PROBE 5-A: ワイルドカード  * + 除外語
            # ====================================================================
            if load_counter[0] < MAX_LOADS:
                kw_a = "* -Card -camera -Vuitton -Hermes -GUCCI -Mint -COACH"
                url_a = build_url(kw_a, category_id=0, day_range=30, now_ms=now_ms)
                r_a = run_probe(page, load_counter, "wildcard_excl", url_a, now_ms)
                results.append(r_a)

            # ====================================================================
            # PROBE 5-B: 括弧記法  (-word) (-word) ...
            # ====================================================================
            if load_counter[0] < MAX_LOADS:
                kw_b = "(-Card) (-camera) (-Vuitton) (-Hermes) (-GUCCI) (-Mint) (-COACH)"
                url_b = build_url(kw_b, category_id=0, day_range=30, now_ms=now_ms)
                r_b = run_probe(page, load_counter, "paren_excl", url_b, now_ms)
                results.append(r_b)

            # ====================================================================
            # PROBE 5-C: UI 経由 — Seller Hub Research 画面でキーワード空・
            #            カテゴリ 293 を選択 → 検索ボタン押下
            # ====================================================================
            if load_counter[0] < MAX_LOADS:
                log("\n--- probe5_ui_category_search (UI操作) ---")
                log(f"  [load {load_counter[0]+1}/{MAX_LOADS}] (SH Research home)")
                load_counter[0] += 1
                page.goto(
                    "https://www.ebay.com/sh/research",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                # SPA がロードされるまで少し待つ
                page.wait_for_timeout(3000)

                # キーワード入力欄をクリアして空にする
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

                if kw_input:
                    kw_input.triple_click()
                    page.keyboard.press("Delete")
                    kw_input.fill("")
                    log("  Keyword field cleared.")
                else:
                    log("  WARNING: keyword input not found, trying to proceed.")

                # 検索ボタンを探してクリック
                search_btn = None
                for sel in [
                    "button[data-testid='research-search-button']",
                    "button.sh-research__search-btn",
                    "button:has-text('Search')",
                    "[class*='search-button']",
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
                    page.wait_for_timeout(4000)
                    wait_result(page)
                    log(f"  URL after search: {page.url[:120]}")
                else:
                    log("  WARNING: search button not found.")

                save_html(page, "ui_category_empty_kw")
                info_c = extract_info(page)
                info_c["keyword_input_found"] = kw_input is not None
                info_c["search_btn_found"] = search_btn is not None
                info_c["before_url"] = before_url
                log(f"  row_count={info_c.get('row_count')}  no_results={info_c.get('no_results', False)}")
                results.append({"label": "ui_category_empty_kw", "url": before_url, "info": info_c})

            # ====================================================================
            # PROBE 5-D: 1文字キーワード "a" + 除外語
            # (全商品の近似になるかを確認)
            # ====================================================================
            if load_counter[0] < MAX_LOADS:
                kw_d = "a -Card -camera -Vuitton -Hermes -GUCCI -Mint -COACH"
                url_d = build_url(kw_d, category_id=0, day_range=30, now_ms=now_ms)
                r_d = run_probe(page, load_counter, "single_a_excl", url_d, now_ms)
                results.append(r_d)

            # ====================================================================
            # PROBE 5-E: 1文字キーワード "e" + 除外語
            # (母音違いでカバー率が変わるか比較)
            # ====================================================================
            if load_counter[0] < MAX_LOADS:
                kw_e = "e -Card -camera -Vuitton -Hermes -GUCCI -Mint -COACH"
                url_e = build_url(kw_e, category_id=0, day_range=30, now_ms=now_ms)
                r_e = run_probe(page, load_counter, "single_e_excl", url_e, now_ms)
                results.append(r_e)

        except KeyboardInterrupt:
            log("Interrupted.")
        except Exception as ex:
            import traceback
            log(f"ERROR: {ex}")
            traceback.print_exc()
            try:
                save_html(page, "error_state")
            except Exception:
                pass
            results.append({"label": "error", "error": str(ex)})
        finally:
            log("\nClosing probe tab ...")
            try:
                page.close()
            except Exception:
                pass

    # 結果保存
    out_path = PROBE_DIR / "probe5_results_2026_06_10.json"
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"\nResults saved: {out_path}")
    log(f"Total page loads: {load_counter[0]}/{MAX_LOADS}")

    log("\n=== SUMMARY ===")
    for r in results:
        lbl = r.get("label", "?")
        info = r.get("info", {})
        rows = info.get("row_count", "?")
        no_r = info.get("no_results", False)
        log(f"  {lbl}: rows={rows}  no_results={no_r}  url={info.get('current_url','?')[:80]}")


if __name__ == "__main__":
    main()
