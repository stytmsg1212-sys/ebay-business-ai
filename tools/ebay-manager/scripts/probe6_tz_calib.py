"""Step 1: TZ 較正 probe - Terapeak の日付軸 (PDT/PST/UTC?) を実測で特定.

目的:
  two_year_echo の startDate を「target 日 00:00 PDT」にセットしたとき、
  ヘッダ表示が「Jun 10, 2024 –」かつ先頭行 (古い順) が Jun 10, 2024 から始まるか確認.

仮説:
  - Terapeak 表示・窓軸 = US Pacific time (PDT/PST)
  - 今日 JST 2026-06-10 → target = 2024-06-10
  - 2024-06-10 00:00 PDT (UTC-7) = epoch ms 1718002800000

上限: ページロード 2 回 (予備 1 回込み).
出力: data/terapeak_probe/probe6_tz_calib.html
"""

from __future__ import annotations

import datetime
import os
import re
import sys
import time

# stdout 文字化け対策 (Windows)
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from urllib.parse import quote

PROBE_DIR = os.path.join(_ROOT, "data", "terapeak_probe")
os.makedirs(PROBE_DIR, exist_ok=True)

KEYWORD = "(-abcd) (-Card) (-camera) (-Vuitton) (-Hermes) (-GUCCI) (-Mint) (-COACH)"
MIN_PRICE = 100
CAT_ID = 0

# ---------------------------------------------------------------------------
# TZ 仮説: target 日 = 2024-06-10 (today_jst 2026-06-10 の 2 年前)
# ---------------------------------------------------------------------------
PDT = datetime.timezone(datetime.timedelta(hours=-7))  # June = DST
PST = datetime.timezone(datetime.timedelta(hours=-8))  # 安全側 (タスク指定)

target = datetime.date(2024, 6, 10)
now_ms = int(time.time() * 1000)

# 仮説A: 2024-06-10 00:00 PDT
dt_pdt = datetime.datetime(2024, 6, 10, 0, 0, 0, tzinfo=PDT)
start_ms_pdt = int(dt_pdt.timestamp() * 1000)

# 仮説B (予備): 2024-06-10 00:00 PST (タスク文書記載: 安全側 UTC-8)
dt_pst = datetime.datetime(2024, 6, 10, 0, 0, 0, tzinfo=PST)
start_ms_pst = int(dt_pst.timestamp() * 1000)

# 現行 (比較用): 2024-06-09 00:00 JST (1 日 buffer)
JST = datetime.timezone(datetime.timedelta(hours=9))
dt_jst_buf = datetime.datetime(2024, 6, 9, 0, 0, 0, tzinfo=JST)
start_ms_jst_buf = int(dt_jst_buf.timestamp() * 1000)

print("=" * 70)
print("  probe6_tz_calib: Terapeak 日付軸 較正")
print("=" * 70)
print(f"  target date : {target}")
print(f"  PDT仮説 startDate: {start_ms_pdt} ({dt_pdt})")
print(f"  PST仮説 startDate: {start_ms_pst} ({dt_pst})")
print(f"  現行 JST-buf     : {start_ms_jst_buf} ({dt_jst_buf})")
print()


def _build_probe_url(start_ms: int, end_ms: int) -> str:
    return (
        f"https://www.ebay.com/sh/research?marketplace=EBAY-US"
        f"&keywords={quote(KEYWORD)}"
        f"&dayRange=730"
        f"&endDate={end_ms}&startDate={start_ms}"
        f"&categoryId={CAT_ID}"
        f"&offset=0&limit=50"
        f"&tabName=SOLD"
        f"&sellerCountry={quote('SellerLocation:::JP')}"
        f"&sorting=datelastsold"  # 古い順
        f"&minPrice={MIN_PRICE}"
    )


# ---------------------------------------------------------------------------
# Playwright で fetch
# ---------------------------------------------------------------------------
DATE_RANGE_PAT = re.compile(
    r"(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4})"
    r"\s*[–—-]\s*"
    r"(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4})"
)
DATE_ROW_PAT = re.compile(
    r'class="research-table-row__item research-table-row__dateLastSold">(.*?)</td>',
    re.DOTALL,
)
DIV_INNER = re.compile(r"<div>([^<]+)</div>")


def _extract_header_and_first_row_date(html: str) -> tuple[str | None, str | None]:
    """ヘッダ開始日と先頭行の Date last sold を抽出."""
    m_range = DATE_RANGE_PAT.search(html)
    header_start = m_range.group(1) if m_range else None

    first_date: str | None = None
    m_row = DATE_ROW_PAT.search(html)
    if m_row:
        m_inner = DIV_INNER.search(m_row.group(1))
        if m_inner:
            first_date = m_inner.group(1).strip()

    return header_start, first_date


try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # type: ignore

    def _fetch_html(url: str, label: str) -> str | None:
        """CDP Chrome で指定 URL を開き outerHTML を返す."""
        print(f"  [{label}] navigate: {url[:120]}")
        try:
            if sys.platform == "win32":
                import asyncio
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp("http://localhost:9222")
                ctx = browser.contexts[0]
                tab = ctx.new_page()
                try:
                    tab.goto(url, wait_until="domcontentloaded", timeout=30000)
                    deadline = time.monotonic() + 35
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
        except Exception as e:
            print(f"  [{label}] エラー: {e}")
            return None

    page_load_count = 0

    # ---- Load 1: PDT 仮説 ----
    url_pdt = _build_probe_url(start_ms_pdt, now_ms)
    print(f"[Load 1] PDT 仮説 startDate={start_ms_pdt}")
    html_pdt = _fetch_html(url_pdt, "PDT仮説")
    page_load_count += 1

    if html_pdt:
        save_path = os.path.join(PROBE_DIR, "probe6_tz_calib.html")
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(html_pdt)
        print(f"  保存: {save_path}  ({len(html_pdt):,} chars)")

        header_pdt, first_row_pdt = _extract_header_and_first_row_date(html_pdt)
        print(f"  ヘッダ開始日  : {header_pdt!r}")
        print(f"  先頭行 date  : {first_row_pdt!r}")
        print()

        # 判定
        pdt_verdict = "UNKNOWN"
        if header_pdt:
            try:
                hd = datetime.datetime.strptime(header_pdt, "%b %d, %Y").date()
                diff = abs((hd - target).days)
                if diff == 0:
                    pdt_verdict = f"MATCH (差 0 日 → PDT 仮説 CONFIRMED)"
                elif diff == 1:
                    pdt_verdict = f"NEAR-MATCH (差 {diff} 日 → PST 寄りの可能性)"
                else:
                    pdt_verdict = f"MISMATCH (差 {diff} 日 → PDT 仮説 否定)"
            except ValueError:
                pdt_verdict = f"PARSE FAIL: {header_pdt!r}"

        print(f"  PDT 仮説 判定: {pdt_verdict}")

    else:
        print("  [Load 1] HTML 取得失敗")
        header_pdt = None
        first_row_pdt = None
        pdt_verdict = "LOAD FAILED"

    # ---- Load 2 (予備): PST 仮説 (PDT が MISMATCH の場合) ----
    html_pst = None
    header_pst = None
    first_row_pst = None
    pst_verdict = "SKIPPED"

    if page_load_count < 2 and ("否定" in pdt_verdict or "FAIL" in pdt_verdict or "UNKNOWN" in pdt_verdict):
        url_pst = _build_probe_url(start_ms_pst, now_ms)
        print(f"\n[Load 2] PST 仮説 startDate={start_ms_pst} (予備)")
        html_pst = _fetch_html(url_pst, "PST仮説")
        page_load_count += 1

        if html_pst:
            save_path2 = os.path.join(PROBE_DIR, "probe6_tz_calib_pst.html")
            with open(save_path2, "w", encoding="utf-8") as f:
                f.write(html_pst)
            print(f"  保存: {save_path2}  ({len(html_pst):,} chars)")

            header_pst, first_row_pst = _extract_header_and_first_row_date(html_pst)
            print(f"  ヘッダ開始日  : {header_pst!r}")
            print(f"  先頭行 date  : {first_row_pst!r}")

            if header_pst:
                try:
                    hd2 = datetime.datetime.strptime(header_pst, "%b %d, %Y").date()
                    diff2 = abs((hd2 - target).days)
                    pst_verdict = f"差 {diff2} 日" + (" → PST CONFIRMED" if diff2 == 0 else " → PST も否定")
                except ValueError:
                    pst_verdict = f"PARSE FAIL: {header_pst!r}"

            print(f"  PST 仮説 判定: {pst_verdict}")

    # ---- 最終サマリ ----
    print()
    print("=" * 70)
    print("  最終判定サマリ")
    print("=" * 70)
    print(f"  ページロード総数: {page_load_count}")
    print(f"  target date     : {target}")
    print()
    print(f"  PDT仮説 ({start_ms_pdt})")
    print(f"    ヘッダ: {header_pdt!r}  先頭行: {first_row_pdt!r}")
    print(f"    判定 : {pdt_verdict}")
    if pst_verdict != "SKIPPED":
        print(f"  PST仮説 ({start_ms_pst})")
        print(f"    ヘッダ: {header_pst!r}  先頭行: {first_row_pst!r}")
        print(f"    判定 : {pst_verdict}")
    print()

    # 結論
    if "CONFIRMED" in pdt_verdict:
        print("結論: Terapeak 日付軸 = US Pacific (PDT/PST) 確定")
        print("  → startDate を target 日 00:00 PST (UTC-8) に変更すれば安全側に包含")
    elif "PST CONFIRMED" in pst_verdict:
        print("結論: Terapeak 日付軸 = PST (UTC-8) 確定 (DST 非依存)")
    else:
        print(f"結論: 確定できず。観測データ:")
        print(f"  PDT ヘッダ = {header_pdt!r}")
        print(f"  PST ヘッダ = {header_pst!r}")
        print("  → 実装に進まず、観測データとして報告")

except ImportError:
    print("playwright 未インストール → 実行不可")
    sys.exit(1)
except Exception as e:
    print(f"予期しない例外: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
