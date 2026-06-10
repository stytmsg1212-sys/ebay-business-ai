"""
probe_terapeak_exclusion_2026_06_10.py
Terapeak Product Research で除外キーワードのみクエリが機能するか実機確認。
Read-only probe。Terapeak ページロード最大 3 回。
"""

import sys
import time
import re
import json
import os
from urllib.parse import quote
from datetime import datetime

# Windows cp932 stdout 文字化け対策
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

PROBE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "terapeak_probe"
)
os.makedirs(PROBE_DIR, exist_ok=True)

EXCLUSION_QUERY = "-abcd -Card -camera -Vuitton -Hermes -GUCCI -Mint -COACH"
EXCLUDE_WORDS = ["GUCCI", "COACH", "Vuitton", "Hermes", "camera", "card"]

PAGE_LOAD_COUNT = 0
MAX_PAGE_LOADS = 3


def build_url(keyword: str, day_range: int = 7, category_id: int = 0) -> str:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - day_range * 24 * 3600 * 1000
    return (
        f"https://www.ebay.com/sh/research?marketplace=EBAY-US"
        f"&keywords={quote(keyword)}"
        f"&dayRange={day_range}"
        f"&endDate={now_ms}&startDate={start_ms}"
        f"&categoryId={category_id}&offset=0&limit=50&tabName=SOLD"
        f"&sellerCountry={quote('SellerLocation:::JP')}"
        f"&sorting=-datelastsold&minPrice=100"
    )


def wait_for_rows(page, timeout_s: int = 30) -> int:
    """tr.research-table-row が出現するまでポーリング。出現した行数を返す。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        count = page.evaluate(
            "() => document.querySelectorAll('tr.research-table-row').length"
        )
        if count and count > 0:
            return count
        time.sleep(1.5)
    return 0


def extract_rows(page) -> list[dict]:
    """先頭 10 行の title / avgSoldPrice / dateLastSold を抽出。"""
    rows = page.evaluate("""
        () => {
            const trs = document.querySelectorAll('tr.research-table-row');
            const results = [];
            const limit = Math.min(trs.length, 10);
            for (let i = 0; i < limit; i++) {
                const tr = trs[i];
                // タイトル: a タグのテキスト or td[data-label="Title"] 等
                let title = '';
                const titleEl = tr.querySelector('td[class*="title"] a') ||
                                tr.querySelector('a[href*="itm"]') ||
                                tr.querySelector('td:first-child a');
                if (titleEl) title = titleEl.textContent.trim();
                if (!title) {
                    const firstTd = tr.querySelectorAll('td');
                    if (firstTd.length > 0) title = firstTd[0].textContent.trim().slice(0, 120);
                }

                // 価格
                let price = '';
                const priceEl = tr.querySelector('td[class*="price"]') ||
                                tr.querySelector('[data-testid*="price"]');
                if (priceEl) price = priceEl.textContent.trim();

                // 日付
                let date = '';
                const dateEl = tr.querySelector('td[class*="date"]') ||
                               tr.querySelector('[data-testid*="date"]');
                if (dateEl) date = dateEl.textContent.trim();

                results.push({ title, price, date });
            }
            return results;
        }
    """)
    return rows or []


def get_results_header(page) -> str:
    """results-header / period 表示テキストを取得。"""
    try:
        text = page.evaluate("""
            () => {
                const candidates = [
                    '.research-table-header',
                    '[class*="result-count"]',
                    '[class*="results-header"]',
                    '[class*="table-summary"]',
                    '.listings-count',
                ];
                for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (el) return el.textContent.trim().slice(0, 200);
                }
                return '';
            }
        """)
        return text or ""
    except Exception:
        return ""


def save_html(page, filename: str) -> str:
    """DOM の outerHTML を保存。"""
    try:
        html = page.evaluate("() => document.documentElement.outerHTML")
    except Exception as e:
        html = f"<!-- evaluate failed: {e} -->"
    path = os.path.join(PROBE_DIR, filename)
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(html)
    return path


def probe_once(playwright, browser_ctx, keyword: str, day_range: int, category_id: int, label: str) -> dict:
    global PAGE_LOAD_COUNT
    PAGE_LOAD_COUNT += 1
    url = build_url(keyword, day_range=day_range, category_id=category_id)
    print(f"\n[Probe {PAGE_LOAD_COUNT}/{MAX_PAGE_LOADS}] {label}")
    print(f"  URL: {url[:140]}...")

    page = browser_ctx.new_page()
    result = {
        "label": label,
        "url": url,
        "row_count": 0,
        "rows": [],
        "results_header": "",
        "final_url": "",
        "excluded_word_hits": [],
        "html_path": "",
        "error": None,
    }

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        final_url = page.url
        result["final_url"] = final_url

        # captcha / error redirect 検知
        if "/n/error" in final_url or "/errors" in final_url:
            print(f"  [WARN] Error redirect detected: {final_url}")
            result["error"] = f"error_redirect: {final_url}"
            html_path = save_html(page, f"probe_{label}_error.html")
            result["html_path"] = html_path
            page.close()
            return result

        # eBay ログイン画面へリダイレクトされた場合
        if "signin" in final_url or "login" in final_url:
            print(f"  [WARN] Login redirect: {final_url}")
            result["error"] = f"login_redirect: {final_url}"
            html_path = save_html(page, f"probe_{label}_login.html")
            result["html_path"] = html_path
            page.close()
            return result

        # 行出現を待機
        row_count = wait_for_rows(page, timeout_s=30)
        result["row_count"] = row_count
        print(f"  行数: {row_count}")

        if row_count == 0:
            print("  [注意] 行が出現しませんでした (0行)")
            html_path = save_html(page, f"probe_{label}_0rows.html")
            result["html_path"] = html_path
            page.close()
            return result

        # 先頭10行取得
        rows = extract_rows(page)
        result["rows"] = rows

        # 除外語ヒット確認
        hits = []
        for row in rows:
            title_lower = row["title"].lower()
            for excl in EXCLUDE_WORDS:
                if excl.lower() in title_lower:
                    hits.append({"title": row["title"], "matched_word": excl})
        result["excluded_word_hits"] = hits

        # results-header
        header = get_results_header(page)
        result["results_header"] = header

        # HTML保存
        html_path = save_html(page, f"probe_{label}.html")
        result["html_path"] = html_path

    except Exception as e:
        print(f"  [ERROR] {e}")
        result["error"] = str(e)
        try:
            html_path = save_html(page, f"probe_{label}_exception.html")
            result["html_path"] = html_path
        except Exception:
            pass
    finally:
        page.close()

    return result


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] playwright not installed. pip install playwright")
        sys.exit(1)

    results = []

    with sync_playwright() as p:
        # CDP Chrome に接続 (新規ブラウザ起動しない)
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            print(f"[OK] CDP Chrome 接続成功。コンテキスト数: {len(browser.contexts)}")
        except Exception as e:
            print(f"[ERROR] CDP Chrome 接続失敗: {e}")
            sys.exit(1)

        # 既存コンテキストを使用 (なければ新規)
        if browser.contexts:
            ctx = browser.contexts[0]
        else:
            ctx = browser.new_context()

        # --- Probe 1: 除外キーワードのみ / categoryId=0 / 7日 ---
        if PAGE_LOAD_COUNT < MAX_PAGE_LOADS:
            r1 = probe_once(
                p, ctx,
                keyword=EXCLUSION_QUERY,
                day_range=7,
                category_id=0,
                label="excl_cat0_7d",
            )
            results.append(r1)

        # --- Probe 2: 除外キーワードのみ / categoryId=293 (Computers, Tablets) / 7日 ---
        if PAGE_LOAD_COUNT < MAX_PAGE_LOADS and results and results[0]["row_count"] > 0:
            r2 = probe_once(
                p, ctx,
                keyword=EXCLUSION_QUERY,
                day_range=7,
                category_id=293,
                label="excl_cat293_7d",
            )
            results.append(r2)
        elif PAGE_LOAD_COUNT < MAX_PAGE_LOADS and results and results[0]["row_count"] == 0:
            # 0行だった場合でも30日で再試行
            r2 = probe_once(
                p, ctx,
                keyword=EXCLUSION_QUERY,
                day_range=30,
                category_id=0,
                label="excl_cat0_30d",
            )
            results.append(r2)

    # --- 判定 ---
    print("\n" + "=" * 60)
    print("判定サマリー")
    print("=" * 60)

    for r in results:
        print(f"\n[{r['label']}]")
        if r["error"]:
            print(f"  エラー: {r['error']}")
            print("  -> FAIL (エラー/リダイレクト)")
            continue

        row_count = r["row_count"]
        hits = r["excluded_word_hits"]

        if row_count == 0:
            print(f"  行数: 0 -> FAIL (0行: 除外のみクエリは機能しない可能性)")
        elif len(hits) > 0:
            print(f"  行数: {row_count}")
            print(f"  除外語ヒット: {len(hits)} 件 -> FAIL (除外フィルタ未機能)")
            for h in hits:
                print(f"    - '{h['matched_word']}' in '{h['title'][:80]}'")
        else:
            print(f"  行数: {row_count} -> PASS (除外語ヒット0)")

        print(f"  results_header: {r['results_header'][:100]}")
        print(f"  先頭10行サンプル:")
        for i, row in enumerate(r["rows"][:10], 1):
            title_disp = row["title"][:80] if row["title"] else "(タイトル取得失敗)"
            print(f"    {i:2}. {title_disp} | {row['price']} | {row['date']}")

        print(f"  HTML保存: {r['html_path']}")
        print(f"  最終URL: {r['final_url'][:100]}")

    # JSON保存
    report_path = os.path.join(PROBE_DIR, "probe_exclusion_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[完了] レポート保存: {report_path}")
    print(f"ページロード合計: {PAGE_LOAD_COUNT}/{MAX_PAGE_LOADS}")


if __name__ == "__main__":
    main()
