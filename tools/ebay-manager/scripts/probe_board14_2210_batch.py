# -*- coding: utf-8 -*-
"""依頼ボード#14 22:10 batch 定時確認 (read-only)."""
import sqlite3

conn = sqlite3.connect("file:data/monitor.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print("=== inventory_check 直近実行 ===")
for r in conn.execute(
    "SELECT task_key, started_at, finished_at, status, substr(message,1,140) m "
    "FROM task_execution_log WHERE task_key='inventory_check' "
    "ORDER BY started_at DESC LIMIT 3"
):
    print(" ", dict(r))

print("\n=== board#14 PayPay 3 商品の現在状態 ===")
for r in conn.execute(
    "SELECT ebay_item_id, substr(title,1,45) t, source_status, "
    "source_out_of_stock_since, source_last_checked "
    "FROM ebay_listings WHERE source_url LIKE '%paypayfleamarket%' "
    "AND source_status != '在庫有' AND COALESCE(is_ended,0)=0 AND quantity_ebay>=1 "
    "AND sku GLOB 'ebay*' ORDER BY source_last_checked DESC LIMIT 10"
):
    print(" ", dict(r))

print("\n=== PayPay 不明 残数 ===")
r = conn.execute(
    "SELECT COUNT(*) n FROM ebay_listings "
    "WHERE source_url LIKE '%paypayfleamarket%' AND source_status='不明' "
    "AND COALESCE(is_ended,0)=0 AND quantity_ebay>=1"
).fetchone()
print("  PayPay 不明:", r["n"])
conn.close()
