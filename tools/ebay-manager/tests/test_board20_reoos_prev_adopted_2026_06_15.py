"""依頼ボード#20 再対応 (2026-06-15): get_ebay_listings_supply_risk の
「採用→再OOS」区別フィールド (prev_adopted_at / prev_adopted_platform) 検証。

過去に採用 (supplier_candidates.status='applied') した仕入先が現在 OOS =
採用した仕入先 (1 点物の多い yahoo/mercari) が再び売切れた『正当な再OOS』。
在庫監視 UI が「採用済みでしたが再び在庫切れ」バナーで区別表示するための
判定根拠を提供する。listing 識別は ebay_item_id (sku-rules)。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _insert_oos_listing(eid: str, sku: str = "ebayyh_reoos00001"):
    from monitor.database import init_db, get_conn

    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM ebay_listings WHERE ebay_item_id=?", (eid,))
        conn.execute("DELETE FROM supplier_candidates WHERE ebay_item_id=?", (eid,))
        conn.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, quantity_ebay, source_status,
                source, is_ended, risk_confirmed, source_out_of_stock_since)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (eid, sku, "Re-OOS Test", 1, "在庫無",
             "Yahoo Auctions", 0, 0, "2026-06-14 13:00:00"),
        )


def _insert_candidate(eid: str, status: str, platform: str,
                      user_action_at: str | None, cand_url: str):
    from monitor.database import get_conn

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO supplier_candidates
               (ebay_item_id, sku, candidate_url, source_platform,
                match_score, status, user_action_at, alt_listing_possible)
               VALUES (?,?,?,?,?,?,?,0)""",
            (eid, "ebayyh_reoos00001", cand_url, platform, 80, status,
             user_action_at),
        )


def _get_item(eid: str):
    from monitor.database import get_ebay_listings_supply_risk
    risk = get_ebay_listings_supply_risk()
    for it in risk["out_of_stock"]:
        if it["ebay_item_id"] == eid:
            return it
    return None


def test_applied_candidate_sets_prev_adopted():
    """applied 候補があれば prev_adopted_at / platform が populate される。"""
    eid = "test_reoos_applied_1"
    _insert_oos_listing(eid)
    _insert_candidate(eid, "applied", "yahoo_auctions",
                      "2026-06-11 09:13:06", "https://example.com/a")
    item = _get_item(eid)
    assert item is not None, "在庫無 listing が out_of_stock に出ていない"
    assert item["prev_adopted_at"] == "2026-06-11 09:13:06"
    assert item["prev_adopted_platform"] == "yahoo_auctions"


def test_no_applied_candidate_prev_adopted_none():
    """pending / rejected のみ (applied 無し) なら prev_adopted_at は None。"""
    eid = "test_reoos_noapplied_1"
    _insert_oos_listing(eid)
    _insert_candidate(eid, "pending", "yahoo_auctions",
                      None, "https://example.com/p")
    _insert_candidate(eid, "rejected", "mercari",
                      "2026-06-10 00:00:00", "https://example.com/r")
    item = _get_item(eid)
    assert item is not None
    assert item["prev_adopted_at"] is None
    assert item["prev_adopted_platform"] is None


def test_latest_applied_wins():
    """複数 applied があれば最新 (user_action_at 最大) が採用される。"""
    eid = "test_reoos_latest_1"
    _insert_oos_listing(eid)
    _insert_candidate(eid, "applied", "yahoo_auctions",
                      "2026-05-09 12:00:00", "https://example.com/old")
    _insert_candidate(eid, "applied", "mercari",
                      "2026-06-14 13:43:38", "https://example.com/new")
    item = _get_item(eid)
    assert item is not None
    assert item["prev_adopted_at"] == "2026-06-14 13:43:38"
    assert item["prev_adopted_platform"] == "mercari"
