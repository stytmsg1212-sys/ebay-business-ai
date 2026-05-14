#!/usr/bin/env python3
"""
仕入先候補タブの UI 目視確認用 seed データ。

6種のケース:
  1. 高スコア(95) + 採算OK → 置き換え候補（緑）
  2. 中スコア(72) + 採算NG → 置き換え候補（橙バッジ）
  3. 高スコア(88) + accepted → 「反映済み」ボタン表示確認
  4. 中スコア(65) + rejected → ステータスフィルタ確認
  5. 低スコア(42) + alt_listing_possible → 別出品機会
  6. 低スコア(30) + junk_likely_untested → ジャンク警告表示
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn, init_db

init_db()

SEED = [
    # (sku, platform, url, price, title, score, reasoning, profit, profitable,
    #  status, discovered_via, junk, alt_possible, alt_note)
    (
        "ebayme_35359409025", "mercari",
        "https://jp.mercari.com/item/m95551111001",
        18500, "KEYENCE LR-XH50 レーザーセンサー 未使用品 箱付き",
        95, "画像一致、型番LR-XH50完全一致、新品/未使用で状態整合",
        12400, 1, "pending", "pattern1_async", 0, 0, None,
    ),
    (
        "ebayme_35359409025", "yahoo_auction",
        "https://auctions.yahoo.co.jp/jp/auction/w95551111002",
        23800, "KEYENCE LR-XH50 センサー中古動作品",
        72, "型番一致だが中古表記。eBay出品は新品なので状態差分あり",
        -450, 0, "pending", "pattern1_async", 0, 0, None,
    ),
    (
        "ebayyh_w1195336864", "mercari",
        "https://jp.mercari.com/item/m95551111003",
        9800, "Pioneer KA-E454 カセットデッキ 整備済み 動作良好",
        88, "画像一致、モデル番号KA-E454一致、整備済で出品状態と近い",
        15200, 1, "accepted", "pattern2_batch", 0, 0, None,
    ),
    (
        "ebayyh_w1195336864", "paypay_furima",
        "https://paypayfleamarket.yahoo.co.jp/item/p95551111004",
        7500, "Pioneer カセットデッキ ジャンク KA-E454",
        65, "型番一致だがジャンク表記",
        8300, 1, "rejected", "pattern1_async", 0, 0, None,
    ),
    (
        "ebayPF_l1157723711", "mercari",
        "https://jp.mercari.com/item/m95551111005",
        14200, "KEYENCE FL-002 FL002 レベルセンサー アンプ",
        42, "型番が FL-001 vs FL-002 で型番相違。別商品の可能性高い",
        9800, 1, "pending", "pattern1_async", 0, 1,
        "FL-002 は別SKUとして新規出品可能。月販実績あり",
    ),
    (
        "ebayPF_l1157723711", "yahoo_auction",
        "https://auctions.yahoo.co.jp/jp/auction/w95551111006",
        5200, "KEYENCE センサー ジャンク品 動作未確認 複数点",
        30, "型番不明、動作未確認",
        2100, 1, "pending", "pattern2_batch", 1, 0, None,
    ),
]

SQL = """
INSERT OR REPLACE INTO supplier_candidates
  (sku, source_platform, candidate_url, candidate_price_jpy,
   candidate_title, match_score, match_reasoning, profit_jpy, profitable,
   status, discovered_via, junk_likely_untested, alt_listing_possible,
   alt_listing_note)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

with get_conn() as conn:
    conn.execute("DELETE FROM supplier_candidates WHERE candidate_url LIKE '%95551111%'")
    for row in SEED:
        conn.execute(SQL, row)
    n = conn.execute("SELECT COUNT(*) FROM supplier_candidates").fetchone()[0]
print(f"seeded. total rows: {n}")
