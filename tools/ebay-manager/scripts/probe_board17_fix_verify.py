# -*- coding: utf-8 -*-
"""依頼ボード#17 修正の実機検証 (read-only).

1. D: GS-71N5 の Yahoo 定額ページを実 HTML で判定 (旧: 不明 → 新: available 期待)
2. B: 本番 DB に対し新 throttle SQL を dry-run し、再探索対象になる listing を列挙
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# --- 1. D: 実ページ判定 ---
print("=== D: Yahoo 定額ページ実判定 ===")
from monitor.scrapers import _check_with_httpx

# site_config 34 (ヤフオク) の実テキスト
url = "https://page.auctions.yahoo.co.jp/jp/auction/t1233209327"
status = _check_with_httpx(
    url,
    in_stock_texts=["入札する", "今すぐ落札"],
    sold_out_texts=["このオークションは終了"],
    no_page_texts=["このオークションは存在しません"],
)
print(f"GS-71N5 新仕入先 ({url}):")
print(f"  -> {status}  (旧実装: None=unknown へ落ちて『不明』stuck / 新期待: available)")

# --- 2. B: 新 throttle SQL dry-run (READ ONLY) ---
print("\n=== B: 新 throttle SQL が拾う continuing_oos (本番 DB read-only) ===")
db = BASE / "data" / "monitor.db"
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
skip_days = 7

new_rows = conn.execute(
    """SELECT l.ebay_item_id, substr(l.title,1,40) t, l.source_status,
              l.source_out_of_stock_since
        FROM ebay_listings l
        WHERE l.source_status IN ('在庫無', 'ページなし')
          AND (l.is_ended IS NULL OR l.is_ended=0)
          AND l.quantity_ebay >= 1
          AND l.sku GLOB 'ebay*'
          AND (
              l.source_status = '在庫無'
              OR (l.source_out_of_stock_since IS NOT NULL
                  AND l.source_out_of_stock_since <= datetime('now', '-24 hours'))
          )
          AND (l.yahoo_grace_until IS NULL OR l.yahoo_grace_until <= datetime('now'))
          AND NOT EXISTS (
              SELECT 1 FROM supplier_candidates sc
              WHERE sc.ebay_item_id = l.ebay_item_id
                AND sc.created_at >= datetime('now', ?)
                AND (l.source_out_of_stock_since IS NULL
                     OR sc.created_at >= l.source_out_of_stock_since)
          )""",
    (f"-{skip_days} days",),
).fetchall()

old_rows = conn.execute(
    """SELECT l.ebay_item_id
        FROM ebay_listings l
        WHERE l.source_status IN ('在庫無', 'ページなし')
          AND (l.is_ended IS NULL OR l.is_ended=0)
          AND l.quantity_ebay >= 1
          AND l.sku GLOB 'ebay*'
          AND (
              l.source_status = '在庫無'
              OR (l.source_out_of_stock_since IS NOT NULL
                  AND l.source_out_of_stock_since <= datetime('now', '-24 hours'))
          )
          AND (l.yahoo_grace_until IS NULL OR l.yahoo_grace_until <= datetime('now'))
          AND NOT EXISTS (
              SELECT 1 FROM supplier_candidates sc
              WHERE sc.ebay_item_id = l.ebay_item_id
                AND sc.created_at >= datetime('now', ?)
          )""",
    (f"-{skip_days} days",),
).fetchall()

old_ids = {r["ebay_item_id"] for r in old_rows}
print(f"旧 SQL 対象: {len(old_rows)} 件 / 新 SQL 対象: {len(new_rows)} 件")
print("新 SQL で追加で拾われる listing (= OOS イベント後に探索が無い):")
for r in new_rows:
    mark = "NEW" if r["ebay_item_id"] not in old_ids else "   "
    print(f"  [{mark}] {r['t']} ({r['ebay_item_id'][-4:]}) "
          f"{r['source_status']} oos_since={r['source_out_of_stock_since']}")

# --- 3. C: status_unknown バケツの本番件数 ---
print("\n=== C: status_unknown バケツ (本番 DB) ===")
unk = conn.execute(
    """SELECT ebay_item_id, substr(title,1,40) t, source_status
        FROM ebay_listings
        WHERE quantity_ebay >= 1 AND COALESCE(is_ended,0)=0
          AND source_status IS NOT NULL
          AND source_status NOT IN ('在庫有', 'unknown', '在庫無', 'ページなし')
          AND sku GLOB 'ebay*' AND COALESCE(risk_confirmed,0)=0"""
).fetchall()
print(f"不明等 {len(unk)} 件:")
for r in unk:
    print(f"  {r['t']} ({r['ebay_item_id'][-4:]}) status={r['source_status']}")
conn.close()
