"""W229 Step 3 再スモーク: probe6_tz_calib 修正後の two_year_echo 動作確認.

目的:
  修正後の harvest_product_list("...", pattern="two_year_echo", max_pages=3) を実行し
  filter 後件数 > 0 (2024-06-10 の行が回収される) を確認する.

実行:
  cd tools/ebay-manager
  python scripts/smoke_harvest_2026_06_10_v2.py

制約: ページロード上限 2 回 (max_pages=2 で実行、2 回以内に target に到達するはず).
"""
from __future__ import annotations

import datetime
import os
import sys

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from monitor.terapeak_scraper import (
    build_harvest_url,
    harvest_product_list,
    _JST,
    _PST,
    _two_year_target,
)

KEYWORD = "(-abcd)  (-Card) (-camera) (-Vuitton) (-Hermes) (-GUCCI) (-Mint) (-COACH)"
CATEGORY_ID = 0
MIN_PRICE = 100

today_jst = datetime.datetime.now(tz=_JST).date()
target = _two_year_target(today_jst)
target_ms = int(
    datetime.datetime.combine(target, datetime.time(0), tzinfo=_PST).timestamp() * 1000
)

print("=" * 70)
print("  W229 Step 3 再スモーク: two_year_echo 修正後")
print("=" * 70)
print(f"  today_jst  : {today_jst}")
print(f"  target     : {target}  (2 年前)")
print(f"  startDate  : {target_ms}  (target 00:00 PST = UTC-8)")
print()

url = build_harvest_url(KEYWORD, "two_year_echo", category_id=CATEGORY_ID, min_price=MIN_PRICE)
print(f"URL: {url[:160]}")
print()
print(">>> harvest_product_list() 実行中 (max_pages=2) ...")

result = harvest_product_list(
    KEYWORD,
    "two_year_echo",
    category_id=CATEGORY_ID,
    min_price=MIN_PRICE,
    max_pages=2,
)

print()
print("=" * 70)
print("  結果")
print("=" * 70)
print(f"  success      : {result.success}")
print(f"  error        : {result.error}")
print(f"  pages_loaded : {result.pages_loaded}")
print(f"  filter後件数 : {len(result.products)}")
print()

if result.products:
    print(f"  先頭 3 件:")
    for i, p in enumerate(result.products[:3]):
        print(f"  [{i+1}] {p.title[:70]}")
        print(f"       date_last_sold: {p.date_last_sold}  avg_price: ${p.avg_sold_price_usd}")
    print()
    if all(p.date_last_sold == target for p in result.products):
        print(f"  PASS: 全件 target 日 ({target}) 一致")
    else:
        dates = sorted({p.date_last_sold for p in result.products})
        print(f"  WARNING: target 以外の日付も含む: {dates}")
else:
    print("  WARNING: filter 後 products = 0 件")
    if result.pages_loaded > 0:
        print(f"  ({result.pages_loaded} ページをロードしたが target 日の行なし)")
        print("  → max_pages を増やすか、target 日の行がない可能性を確認してください")

print()
print(f"ページロード総数: {result.pages_loaded}")
