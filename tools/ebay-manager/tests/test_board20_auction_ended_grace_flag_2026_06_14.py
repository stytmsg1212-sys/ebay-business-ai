"""依頼ボード#20 (2026-06-14): get_ebay_listings_supply_risk の
auction_ended_grace フラグ検証。

ヤフオク「落札者なし終了」(yahoo_grace_until が未来 = 再出品待ち 24h 猶予中) を
売り切れ(落札済・即時再検索)と区別するための表示フラグ。在庫監視 UI が
「オークション終了（落札者なし・再出品待ち）」と明示するために使う。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _insert_oos_listing(eid: str, grace_sql: str | None):
    """在庫無 + qty1 の無在庫 listing を入れる。grace_sql は
    yahoo_grace_until に入れる SQL 式 (None / "datetime('now','+12 hours')" 等)。"""
    from monitor.database import init_db, get_conn

    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM ebay_listings WHERE ebay_item_id=?", (eid,))
        if grace_sql is None:
            conn.execute(
                """INSERT INTO ebay_listings
                   (ebay_item_id, sku, title, quantity_ebay, source_status,
                    source, is_ended, risk_confirmed, yahoo_grace_until)
                   VALUES (?,?,?,?,?,?,?,?,NULL)""",
                (eid, "ebayyh_grace000001", "Grace Test", 1, "在庫無",
                 "Yahoo Auctions", 0, 0),
            )
        else:
            conn.execute(
                f"""INSERT INTO ebay_listings
                    (ebay_item_id, sku, title, quantity_ebay, source_status,
                     source, is_ended, risk_confirmed, yahoo_grace_until)
                    VALUES (?,?,?,?,?,?,?,?,{grace_sql})""",
                (eid, "ebayyh_grace000001", "Grace Test", 1, "在庫無",
                 "Yahoo Auctions", 0, 0),
            )


def _get_item(eid: str):
    from monitor.database import get_ebay_listings_supply_risk
    risk = get_ebay_listings_supply_risk()
    for it in risk["out_of_stock"]:
        if it["ebay_item_id"] == eid:
            return it
    return None


def test_future_grace_sets_auction_ended_grace_1():
    """yahoo_grace_until が未来 → auction_ended_grace=1 (落札なし終了・猶予中)。"""
    eid = "test_b20grace_future_1"
    _insert_oos_listing(eid, "datetime('now', '+12 hours')")
    item = _get_item(eid)
    assert item is not None, "在庫無 listing が out_of_stock に出ていない"
    assert item["auction_ended_grace"] == 1, item.get("auction_ended_grace")


def test_expired_grace_sets_auction_ended_grace_0():
    """yahoo_grace_until が過去 (猶予切れ) → auction_ended_grace=0 (通常の在庫無扱い)。"""
    eid = "test_b20grace_expired_1"
    _insert_oos_listing(eid, "datetime('now', '-2 hours')")
    item = _get_item(eid)
    assert item is not None
    assert item["auction_ended_grace"] == 0, item.get("auction_ended_grace")


def test_null_grace_sets_auction_ended_grace_0():
    """yahoo_grace_until=NULL (売り切れ=即時検索) → auction_ended_grace=0。"""
    eid = "test_b20grace_null_1"
    _insert_oos_listing(eid, None)
    item = _get_item(eid)
    assert item is not None
    assert item["auction_ended_grace"] == 0, item.get("auction_ended_grace")
