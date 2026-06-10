"""W229 two_year_echo 再スモーク (UTC-7 / Pacific 整列修正後の確認、1 回限り).

目的: HIGH-A (UTC-8→UTC-7) 修正後も filter 後件数 > 0 を実機確認。
実行: python scripts/smoke_harvest_2yecho_utc7.py
注意: DB 書込なし / max_pages=2 (最大 2 ロード) / 本体変更なし
"""
import sys
import os
import datetime

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from monitor.terapeak_scraper import (
    harvest_product_list,
    build_harvest_url,
    _two_year_target,
)

KEYWORD = "(-abcd)  (-Card) (-camera) (-Vuitton) (-Hermes) (-GUCCI) (-Mint) (-COACH)"
CATEGORY_ID = 0
MIN_PRICE = 100
MAX_PAGES = 2

today_jst = datetime.datetime.now(
    tz=datetime.timezone(datetime.timedelta(hours=9))
).date()
target = _two_year_target(today_jst)

print("=" * 70)
print("  two_year_echo 再スモーク (UTC-7 startDate 整列後)")
print("=" * 70)
print(f"today (JST)   : {today_jst}")
print(f"target (2y前) : {target}")
url = build_harvest_url(KEYWORD, "two_year_echo", category_id=CATEGORY_ID, min_price=MIN_PRICE)
print(f"URL           : {url[:200]}")
print()
print(f">>> harvest_product_list(max_pages={MAX_PAGES}) 実行中 ...")

result = harvest_product_list(
    KEYWORD,
    "two_year_echo",
    category_id=CATEGORY_ID,
    min_price=MIN_PRICE,
    max_pages=MAX_PAGES,
)

print()
print(f"success      : {result.success}")
print(f"error        : {result.error}")
print(f"pages_loaded : {result.pages_loaded}")
print(f"filter 後件数: {len(result.products)}")
print()

dates = sorted({p.date_last_sold for p in result.products})
print(f"date_last_sold の分布: {dates}")
mismatch = [p for p in result.products if p.date_last_sold != target]
print(f"target ({target}) 不一致件数: {len(mismatch)}")
print()
for i, p in enumerate(result.products[:5]):
    print(f"  [{i+1}] {p.date_last_sold} | ${p.avg_sold_price_usd} | {p.title[:70]}")

print()
verdict = (
    "PASS"
    if (result.success and len(result.products) > 0 and not mismatch)
    else "FAIL"
)
print(f"=== 判定: {verdict} ===")
