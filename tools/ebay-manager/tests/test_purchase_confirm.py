"""W133 (2026-05-16) tests: task_purchase_confirm 純ロジック.

検証対象:
  - suggest_listings: 正/誤/ゼロ件/low_confidence/有在庫フィルタ
  - confirm_purchase: 加算 / 二重加算 INSERT OR IGNORE ガード / 無在庫拒否
  - undo_purchase: 引き戻し + 負値ガード
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    import monitor.inventory_sync as inv_mod
    monkeypatch.setattr(inv_mod, "_oos_control_cache", None)
    yield db_path


def _insert_listing(ebay_item_id, sku, inventory_count, title):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, is_ended, inventory_count, quantity_ebay)
               VALUES (?, ?, ?, 0, ?, 0)""",
            (ebay_item_id, sku, title, inventory_count),
        )


# inventory_sync は本テストの対象外なので常に成功で stub.
def _stub_sync():
    return patch(
        "monitor.inventory_sync.sync_listing_quantity",
        return_value={"success": True, "message": "stub", "ebay_item_id": ""},
    )


# =============================================================================
# suggest_listings
# =============================================================================

def test_suggest_returns_best_match_first(tmp_db):
    _insert_listing("A1", "stock:01", 2, "Sony WH-1000XM5 Wireless Headphones")
    _insert_listing("A2", "stock:02", 1, "Logitech MX Master 3S Mouse")
    from tasks.task_purchase_confirm import suggest_listings
    res = suggest_listings("Sony WH-1000XM5 Wireless Headphones 入荷しました")
    assert res, "候補が空"
    assert res[0]["ebay_item_id"] == "A1"
    assert res[0]["score"] >= res[-1]["score"]  # 降順


def test_suggest_low_confidence_flag(tmp_db):
    _insert_listing("B1", "stock:01", 1, "Completely Unrelated Product XYZ")
    from tasks.task_purchase_confirm import suggest_listings
    res = suggest_listings("zzz qqq totally different text", threshold=0.9)
    assert res
    assert res[0]["low_confidence"] is True


def test_suggest_excludes_non_stock_sku(tmp_db):
    """無在庫 (ebay prefix) SKU は候補に出さない (集合フィルタ)."""
    _insert_listing("C1", "ebayyh_p123", 5, "Some Supplier Sourced Item")
    _insert_listing("C2", "stock:01", 3, "Some Supplier Sourced Item")
    from tasks.task_purchase_confirm import suggest_listings
    res = suggest_listings("Some Supplier Sourced Item")
    ids = {r["ebay_item_id"] for r in res}
    assert "C1" not in ids, "無在庫 SKU が候補に混入"
    assert "C2" in ids


def test_suggest_empty_text_returns_empty(tmp_db):
    _insert_listing("D1", "stock:01", 1, "Anything")
    from tasks.task_purchase_confirm import suggest_listings
    assert suggest_listings("") == []
    assert suggest_listings("   ") == []


def test_suggest_no_listings_returns_empty(tmp_db):
    from tasks.task_purchase_confirm import suggest_listings
    assert suggest_listings("some text") == []


def test_suggest_top_limit(tmp_db):
    for i in range(8):
        _insert_listing(f"E{i}", "stock:01", 1, f"Product number {i}")
    from tasks.task_purchase_confirm import suggest_listings
    res = suggest_listings("Product number 3", top=3)
    assert len(res) == 3


# =============================================================================
# confirm_purchase
# =============================================================================

def test_confirm_adds_inventory(tmp_db):
    _insert_listing("F1", "stock:01", 4, "Widget")
    from tasks.task_purchase_confirm import confirm_purchase
    with _stub_sync():
        res = confirm_purchase("GMAIL1", "F1", 3)
    assert res["success"] is True
    assert res["old_count"] == 4
    assert res["new_count"] == 7
    from monitor.database import get_conn
    with get_conn() as c:
        inv = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id='F1'"
        ).fetchone()["inventory_count"]
    assert inv == 7


def test_confirm_from_null_inventory(tmp_db):
    _insert_listing("F2", "stock:01", None, "Widget2")
    from tasks.task_purchase_confirm import confirm_purchase
    with _stub_sync():
        res = confirm_purchase("GMAIL2", "F2", 5)
    assert res["success"] is True
    assert res["old_count"] == 0
    assert res["new_count"] == 5


def test_confirm_double_add_guard(tmp_db):
    """同 (gmail_id, ebay_item_id) の 2 回目は INSERT OR IGNORE で弾く."""
    _insert_listing("F3", "stock:01", 2, "Widget3")
    from tasks.task_purchase_confirm import confirm_purchase
    with _stub_sync():
        r1 = confirm_purchase("GMAIL3", "F3", 4)
        r2 = confirm_purchase("GMAIL3", "F3", 4)
    assert r1["success"] is True
    assert r2["success"] is False
    assert r2["already"] is True
    from monitor.database import get_conn
    with get_conn() as c:
        inv = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id='F3'"
        ).fetchone()["inventory_count"]
    assert inv == 6, "二重加算が発生 (4+4 でなく 2+4 が正しい)"


def test_confirm_rejects_non_stock_sku(tmp_db):
    _insert_listing("F4", "ebayyh_p999", 1, "Supplier item")
    from tasks.task_purchase_confirm import confirm_purchase
    with _stub_sync():
        res = confirm_purchase("GMAIL4", "F4", 2)
    assert res["success"] is False
    assert "無在庫" in res["message"]


def test_confirm_rejects_zero_qty(tmp_db):
    _insert_listing("F5", "stock:01", 1, "Widget5")
    from tasks.task_purchase_confirm import confirm_purchase
    with _stub_sync():
        res = confirm_purchase("GMAIL5", "F5", 0)
    assert res["success"] is False
    assert res["already"] is False


def test_confirm_missing_listing(tmp_db):
    from tasks.task_purchase_confirm import confirm_purchase
    with _stub_sync():
        res = confirm_purchase("GMAIL6", "NOPE", 1)
    assert res["success"] is False
    assert "無い" in res["message"]


def test_confirm_sync_failure_not_fake_success(tmp_db):
    """加算成功・eBay 反映失敗時に偽装成功にしない (Q0)."""
    _insert_listing("F7", "stock:01", 1, "Widget7")
    from tasks.task_purchase_confirm import confirm_purchase
    with patch(
        "monitor.inventory_sync.sync_listing_quantity",
        return_value={"success": False, "message": "API down"},
    ):
        res = confirm_purchase("GMAIL7", "F7", 2)
    assert res["success"] is True  # DB 加算自体は成功
    assert res["sync_success"] is False
    assert "失敗" in res["message"]
    from monitor.database import get_conn
    with get_conn() as c:
        ok = c.execute(
            "SELECT ebay_qty_sync_ok FROM purchase_confirmation_log "
            "WHERE gmail_id='GMAIL7' AND ebay_item_id='F7'"
        ).fetchone()["ebay_qty_sync_ok"]
    assert ok == 0


# =============================================================================
# undo_purchase
# =============================================================================

def test_undo_restores_inventory(tmp_db):
    _insert_listing("G1", "stock:01", 5, "Widget")
    from tasks.task_purchase_confirm import confirm_purchase, undo_purchase
    with _stub_sync():
        confirm_purchase("GMAILU1", "G1", 3)  # 5 -> 8
        u = undo_purchase("GMAILU1", "G1")
    assert u["success"] is True
    assert u["restored_count"] == 5
    from monitor.database import get_conn
    with get_conn() as c:
        inv = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id='G1'"
        ).fetchone()["inventory_count"]
        log = c.execute(
            "SELECT COUNT(*) FROM purchase_confirmation_log "
            "WHERE gmail_id='GMAILU1' AND ebay_item_id='G1'"
        ).fetchone()[0]
    assert inv == 5
    assert log == 0, "undo 後に log 行が残っている (再 confirm 不能)"


def test_undo_negative_guard(tmp_db):
    """現在庫 < quantity_added でも負値にならない (max(0,...))."""
    _insert_listing("G2", "stock:01", 5, "Widget")
    from tasks.task_purchase_confirm import confirm_purchase, undo_purchase
    from monitor.database import get_conn
    with _stub_sync():
        confirm_purchase("GMAILU2", "G2", 4)  # 5 -> 9
        # 外的要因で在庫が 2 に下がったと仮定 (売れた等)
        with get_conn() as c:
            c.execute(
                "UPDATE ebay_listings SET inventory_count=2 "
                "WHERE ebay_item_id='G2'"
            )
        u = undo_purchase("GMAILU2", "G2")
    assert u["success"] is True
    assert u["restored_count"] == 0  # max(0, 2-4) = 0


def test_undo_no_history(tmp_db):
    _insert_listing("G3", "stock:01", 1, "Widget")
    from tasks.task_purchase_confirm import undo_purchase
    u = undo_purchase("NOPE", "G3")
    assert u["success"] is False
    assert "見つかりません" in u["message"]


# =============================================================================
# HIGH-1 回帰 (code-review 2026-05-16): confirm/undo の read-modify-write を
# SQL 内 atomic 加減算へ. stale な Python 側 read を base にしないことの契約検証.
# (注: 単一スレッド逐次模擬。真の並行 lost-update は SQLite では再現困難なため、
#  「現在の DB 値を base に相対加減算する」契約の guard として位置づける。)
# =============================================================================

def test_confirm_is_relative_delta_not_stale_absolute(tmp_db):
    """insert 後に外部要因で在庫が変動しても、confirm は現在値に +qty する."""
    _insert_listing("RW1", "stock:01", 5, "RaceWidget")
    from tasks.task_purchase_confirm import confirm_purchase
    from monitor.database import get_conn
    # confirm 呼出前に外部で在庫が 9 へ変動 (他経路の加算等)
    with get_conn() as c:
        c.execute(
            "UPDATE ebay_listings SET inventory_count=9 WHERE ebay_item_id='RW1'"
        )
    with _stub_sync():
        res = confirm_purchase("GRW1", "RW1", 3)
    assert res["success"] is True
    # insert 時の 5 を base にしたら 8 (BUG). 現在値 9 を base = 12 が正.
    assert res["new_count"] == 12
    assert res["old_count"] == 9
    with get_conn() as c:
        inv = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id='RW1'"
        ).fetchone()["inventory_count"]
    assert inv == 12, f"lost-update: 期待 9+3=12, 実 {inv}"


def test_undo_is_relative_delta(tmp_db):
    """undo も現在値から相対減算 (MAX(0,...) 負値ガード付き)."""
    _insert_listing("RW2", "stock:01", 5, "RaceWidget2")
    from tasks.task_purchase_confirm import confirm_purchase, undo_purchase
    from monitor.database import get_conn
    with _stub_sync():
        confirm_purchase("GRW2", "RW2", 4)  # 5 -> 9
        # 外部で在庫が 7 に下がった (1 つ売れた 等)
        with get_conn() as c:
            c.execute(
                "UPDATE ebay_listings SET inventory_count=7 "
                "WHERE ebay_item_id='RW2'"
            )
        u = undo_purchase("GRW2", "RW2")
    assert u["success"] is True
    # 現在値 7 から quantity_added 4 を相対減算 = 3 が正 (絶対復元でない)
    assert u["restored_count"] == 3
    with get_conn() as c:
        inv = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id='RW2'"
        ).fetchone()["inventory_count"]
    assert inv == 3


# =============================================================================
# F3 回帰 (Codex 2026-05-16): undo の atomic claim — 二重 undo で二重減算しない.
# =============================================================================

def test_undo_double_call_no_double_subtract(tmp_db):
    """undo を 2 回呼んでも claim-first で二重減算されない (UI 二度押し耐性)."""
    _insert_listing("RW3", "stock:01", 5, "RaceWidget3")
    from tasks.task_purchase_confirm import confirm_purchase, undo_purchase
    from monitor.database import get_conn
    with _stub_sync():
        confirm_purchase("GRW3", "RW3", 3)   # 5 -> 8
        u1 = undo_purchase("GRW3", "RW3")    # 8 -> 5, log 削除 (claim 成立)
        u2 = undo_purchase("GRW3", "RW3")    # 2 回目: claim 失敗で abort
    assert u1["success"] is True
    assert u2["success"] is False
    assert u2["already"] is True
    with get_conn() as c:
        inv = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id='RW3'"
        ).fetchone()["inventory_count"]
    assert inv == 5, f"二重減算が発生 (5 が正、実 {inv})"
