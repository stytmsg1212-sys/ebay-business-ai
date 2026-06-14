"""依頼ボード#21 (2026-06-14): 状態不明の手動判定が要対応バケツへ正しく振り分く。

在庫監視「状態不明」カードで user が実状態 (在庫有/在庫無/ページなし) を記載 → 保存すると
update_ebay_listing_status で source_status に反映され、get_ebay_listings_supply_risk の
バケツが切り替わる。UI が依存するこの round-trip を検証する。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _insert_unknown(eid: str):
    from monitor.database import init_db, get_conn
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM ebay_listings WHERE ebay_item_id=?", (eid,))
        conn.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, quantity_ebay, source_status,
                source, is_ended, risk_confirmed)
               VALUES (?,?,?,?,?,?,?,?)""",
            (eid, "ebayyh_unk0000001", "Unknown Test", 1, "不明",
             "Yahoo Auctions", 0, 0),
        )


def _buckets(eid: str):
    from monitor.database import get_ebay_listings_supply_risk
    r = get_ebay_listings_supply_risk()
    return {
        k: any(it["ebay_item_id"] == eid for it in r[k])
        for k in ("out_of_stock", "page_not_found", "status_unknown")
    }


def test_unknown_starts_in_status_unknown_bucket():
    eid = "test_b21_unk_init"
    _insert_unknown(eid)
    b = _buckets(eid)
    assert b["status_unknown"] and not b["out_of_stock"] and not b["page_not_found"], b


def test_manual_set_out_of_stock_moves_to_oos():
    from monitor.database import update_ebay_listing_status
    eid = "test_b21_unk_oos"
    _insert_unknown(eid)
    update_ebay_listing_status(eid, "在庫無")
    b = _buckets(eid)
    assert b["out_of_stock"] and not b["status_unknown"], b


def test_manual_set_page_not_found_moves_to_pnf():
    from monitor.database import update_ebay_listing_status
    eid = "test_b21_unk_pnf"
    _insert_unknown(eid)
    update_ebay_listing_status(eid, "ページなし")
    b = _buckets(eid)
    assert b["page_not_found"] and not b["status_unknown"], b


def test_manual_set_in_stock_clears_from_all_buckets():
    """在庫有 → 要対応の全バケツから消える (解決) + risk_confirmed リセット。"""
    from monitor.database import update_ebay_listing_status, get_conn
    eid = "test_b21_unk_instock"
    _insert_unknown(eid)
    update_ebay_listing_status(eid, "在庫有")
    b = _buckets(eid)
    assert not any(b.values()), b
    with get_conn() as conn:
        row = conn.execute(
            "SELECT source_status, COALESCE(risk_confirmed,0) FROM ebay_listings "
            "WHERE ebay_item_id=?", (eid,)).fetchone()
    assert row[0] == "在庫有" and row[1] == 0
