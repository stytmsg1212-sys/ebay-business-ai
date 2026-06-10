"""W229 Terapeak ハーベストエンジン 実機スモークテスト (1 回限り).

目的:
  1. harvest_product_list() が CDP Chrome 本番経路で動くことを確認
  2. 括弧記法の除外語のみクエリ が dayRange=7+新着順 / dayRange=730+古い順 と組み合わせて成立することを確認
  3. MEDIUM-A 検証: two_year_echo の startDate を Terapeak が honor するか

実行:
  cd tools/ebay-manager
  python scripts/smoke_harvest_2026_06_10.py

注意:
  - DB への書込は一切しない
  - max_pages=1 固定 (合計ページロード 2 回 + 予備 1 回)
  - terapeak_scraper.py 本体は変更しない
"""
import sys
import os
import re
import datetime

# stdout 文字化け対策 (Windows pythonw / PowerShell 等)
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# プロジェクト root を sys.path に追加
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from monitor.terapeak_scraper import (
    harvest_product_list,
    build_harvest_url,
    parse_harvest_rows,
    filter_harvest_window,
    _harvest_product_list_impl,  # raw 行数を取るために使用 (terapeak_scraper.py 本体変更禁止)
)

KEYWORD = "(-abcd)  (-Card) (-camera) (-Vuitton) (-Hermes) (-GUCCI) (-Mint) (-COACH)"
CATEGORY_ID = 0
MIN_PRICE = 100
MAX_PAGES = 1
PROBE_DIR = os.path.join(_ROOT, "data", "terapeak_probe")

os.makedirs(PROBE_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# raw 行数取得用ヘルパー
# _harvest_product_list_impl は内部で filter_harvest_window を適用してしまうため、
# raw 行数は parse_harvest_rows を直接呼ぶ形でスクリプト側で計測する。
# ただし impl は CDP に 1 回接続して HTML を取得する処理全体を含んでいる。
# HarvestResult には products (フィルタ後) しか入っていないため、
# raw 行数を知るには HTML を別途取得する必要がある。
#
# 方針: HTML 保存が必要な ② two_year_echo の際に予備 1 ロードを使い、
# goto → outerHTML 取得 → parse_harvest_rows でカウントする。
# ---------------------------------------------------------------------------

def print_separator(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def summarize_result(label: str, result, raw_row_count=None) -> None:
    """HarvestResult の要約を print."""
    print(f"[{label}]")
    print(f"  success      : {result.success}")
    print(f"  error        : {result.error}")
    print(f"  pages_loaded : {result.pages_loaded}")
    if raw_row_count is not None:
        print(f"  raw 行数     : {raw_row_count}  (filter 前 parse_harvest_rows)")
    print(f"  filter 後件数: {len(result.products)}")
    print()
    for i, p in enumerate(result.products[:3]):
        print(f"  [{i+1}] title           : {p.title[:80]}")
        print(f"       avg_sold_price : {p.avg_sold_price_usd}")
        print(f"       date_last_sold : {p.date_last_sold}")
    if not result.products:
        print("  (filter 後 products = 0 件 → 窓外のため正常の可能性あり)")


# ---------------------------------------------------------------------------
# パターン①: fresh_24h
# ---------------------------------------------------------------------------
print_separator("パターン① fresh_24h  (dayRange=7, 新着順)")
print(f"keyword: {KEYWORD!r}")
print(f"category_id={CATEGORY_ID}, min_price={MIN_PRICE}, max_pages={MAX_PAGES}")
print()
print(f"build_harvest_url → {build_harvest_url(KEYWORD, 'fresh_24h', category_id=CATEGORY_ID, min_price=MIN_PRICE)[:180]}")
print()
print(">>> harvest_product_list() 実行中 ... (CDP Chrome に接続してページロード)")

result_fresh = harvest_product_list(
    KEYWORD,
    "fresh_24h",
    category_id=CATEGORY_ID,
    min_price=MIN_PRICE,
    max_pages=MAX_PAGES,
)

summarize_result("fresh_24h", result_fresh)
pages_total = result_fresh.pages_loaded

# ---------------------------------------------------------------------------
# パターン②: two_year_echo
# ---------------------------------------------------------------------------
print_separator("パターン② two_year_echo  (dayRange=730, 古い順)")
print(f"keyword: {KEYWORD!r}")
print()

today_jst = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=9))).date()
two_year_url = build_harvest_url(
    KEYWORD, "two_year_echo",
    category_id=CATEGORY_ID,
    min_price=MIN_PRICE,
)
print(f"build_harvest_url → {two_year_url[:180]}")

# startDate 計算値を表示 (MEDIUM-A 検証の期待値)
try:
    target_date = today_jst.replace(year=today_jst.year - 2)
except ValueError:
    target_date = today_jst.replace(year=today_jst.year - 2, day=28)
print(f"期待 startDate  : {target_date} JST 00:00 − 1 日 buffer = {target_date - datetime.timedelta(days=1)}")
print()
print(">>> harvest_product_list() 実行中 ... (CDP Chrome に接続してページロード)")

result_echo = harvest_product_list(
    KEYWORD,
    "two_year_echo",
    category_id=CATEGORY_ID,
    min_price=MIN_PRICE,
    max_pages=MAX_PAGES,
)

summarize_result("two_year_echo", result_echo)
pages_total += result_echo.pages_loaded

# ---------------------------------------------------------------------------
# 予備ロード: HTML 取得 + 保存 + raw 行数カウント + MEDIUM-A 判定
# ---------------------------------------------------------------------------
print_separator("予備ロード: HTML 取得・保存・MEDIUM-A 判定")
print("CDP Chrome に接続して outerHTML を取得します (予備の 1 ロード)。")
print("※ harvest_product_list とは別に Playwright で 1 goto を実施")

_html_fresh = None
_html_echo = None

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    def _fetch_html(url: str) -> str:
        """指定 URL を CDP Chrome で開き outerHTML を返す."""
        import asyncio
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            ctx = browser.contexts[0]
            tab = ctx.new_page()
            try:
                tab.goto(url, wait_until="domcontentloaded", timeout=30000)
                # tr.research-table-row ポーリング (最大 30s)
                import time
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

    # two_year_echo の HTML を取得 (MEDIUM-A 判定が主目的)
    echo_html_url = build_harvest_url(
        KEYWORD, "two_year_echo",
        category_id=CATEGORY_ID,
        min_price=MIN_PRICE,
    )
    print(f"URL: {echo_html_url[:180]}")
    _html_echo = _fetch_html(echo_html_url)

    pages_total += 1

    # HTML 保存
    echo_html_path = os.path.join(PROBE_DIR, "smoke_2yecho.html")
    with open(echo_html_path, "w", encoding="utf-8") as f:
        f.write(_html_echo)
    print(f"保存: {echo_html_path}  ({len(_html_echo):,} chars)")

    # raw 行数 (フィルタ前)
    raw_rows_echo = parse_harvest_rows(_html_echo)
    print(f"raw 行数 (parse_harvest_rows): {len(raw_rows_echo)} 件")

    # ページヘッダの日付範囲を抽出 (MEDIUM-A 判定)
    # Terapeak のヘッダ例: "Jun 9, 2024 – Jun 10, 2026"
    date_range_pat = re.compile(
        r'(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4})'
        r'\s*[–—-]\s*'
        r'(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4})'
    )
    m = date_range_pat.search(_html_echo)
    if m:
        header_start_str = m.group(1)
        header_end_str = m.group(2)
        print()
        print(f"ヘッダ日付範囲: {header_start_str} – {header_end_str}")

        # MEDIUM-A 判定
        # 期待 startDate: target_date - 1 日 (buffer)
        buf_date = target_date - datetime.timedelta(days=1)
        try:
            header_start_dt = datetime.datetime.strptime(header_start_str, "%b %d, %Y").date()
        except ValueError:
            try:
                header_start_dt = datetime.datetime.strptime(header_start_str, "%b %d, %Y").date()
            except Exception:
                header_start_dt = None

        print()
        print("=== MEDIUM-A 判定 ===")
        print(f"  要求 startDate (buffer込み): {buf_date}")
        print(f"  ヘッダ開始日             : {header_start_dt}")
        if header_start_dt is not None:
            # ±1 日以内なら honored とみなす
            diff = abs((header_start_dt - buf_date).days)
            if diff <= 1:
                print(f"  判定: startDate HONORED  (差 {diff} 日 ≤ 1 → OK)")
            else:
                # dayRange=730d 換算での期待開始日
                now_approx = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=9))).date()
                dr730_start = now_approx - datetime.timedelta(days=730)
                diff_dr = abs((header_start_dt - dr730_start).days)
                print(
                    f"  判定: MEDIUM-A 顕在の可能性 "
                    f"(差 {diff} 日 > 1, dayRange=730d 起点との差={diff_dr} 日)"
                )
                print(f"  → dayRange=730d 固定プリセット優先と推定: {dr730_start}")
        else:
            print("  判定: ヘッダ日付パース失敗 → 手動確認要")
    else:
        print()
        print("ヘッダ日付範囲: 未検出 (ページ未ロード or DOM 構造変更の可能性)")
        print("MEDIUM-A 判定: 手動で smoke_2yecho.html を確認してください")

    # fresh_24h HTML も取得して保存 (オプション)
    fresh_html_url = build_harvest_url(
        KEYWORD, "fresh_24h",
        category_id=CATEGORY_ID,
        min_price=MIN_PRICE,
    )
    print()
    print("fresh_24h の HTML も保存します (dayRange=7 確認用) ...")
    _html_fresh = _fetch_html(fresh_html_url)
    pages_total += 1
    fresh_html_path = os.path.join(PROBE_DIR, "smoke_fresh24h.html")
    with open(fresh_html_path, "w", encoding="utf-8") as f:
        f.write(_html_fresh)
    raw_rows_fresh = parse_harvest_rows(_html_fresh)
    print(f"保存: {fresh_html_path}  ({len(_html_fresh):,} chars)")
    print(f"raw 行数 (parse_harvest_rows): {len(raw_rows_fresh)} 件")

except ImportError:
    print("playwright 未インストール → HTML 取得・保存をスキップ")
    fresh_html_path = "(not saved)"
    echo_html_path = "(not saved)"
    raw_rows_fresh = None
    raw_rows_echo = None
except Exception as e:
    print(f"予備ロード中に例外: {e}")
    fresh_html_path = "(error)"
    echo_html_path = "(error)"
    raw_rows_fresh = None
    raw_rows_echo = None

# ---------------------------------------------------------------------------
# 最終サマリ
# ---------------------------------------------------------------------------
print_separator("最終サマリ")
print(f"ページロード総数: {pages_total}")
print()
print(f"スクリプト        : {os.path.abspath(__file__)}")
print(f"fresh_24h HTML  : {fresh_html_path if 'fresh_html_path' in dir() else '(not reached)'}")
print(f"two_year_echo HTML: {echo_html_path if 'echo_html_path' in dir() else '(not reached)'}")
print()
print("--- ① fresh_24h ---")
summarize_result("fresh_24h (再掲)", result_fresh)

print("--- ② two_year_echo ---")
summarize_result("two_year_echo (再掲)", result_echo)
