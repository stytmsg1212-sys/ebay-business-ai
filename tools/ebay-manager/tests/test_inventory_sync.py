"""W133 (2026-05-16) tests: migration v40 冪等性 + inventory_sync.

検証対象:
  - migration v40: ebay_listings 3 列追加 + purchase_confirmation_log 新設
  - Q2 冪等性: init_db 2 連続でデータ保持
  - sync_listing_quantity: 成功 / API 失敗 / target0+OOS_ON / target0+OOS_OFF
    / target0+OOS_unknown(抑止) / 列遷移
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
    # OOS Control キャッシュをテスト毎にリセット (プロセス内 global).
    import monitor.inventory_sync as inv_mod
    monkeypatch.setattr(inv_mod, "_oos_control_cache", None)
    yield db_path


def _insert_stock_listing(ebay_item_id, sku, inventory_count,
                          quantity_ebay=0, title="T"):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, is_ended, inventory_count, quantity_ebay)
               VALUES (?, ?, ?, 0, ?, ?)""",
            (ebay_item_id, sku, title, inventory_count, quantity_ebay),
        )


# =============================================================================
# migration v40
# =============================================================================

def test_v40_schema_version_at_least_40(tmp_db):
    from monitor.database import get_conn
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver >= 40, f"schema_ver={ver} < 40 (migration v40 未適用)"


def test_v40_ebay_listings_new_columns(tmp_db):
    from monitor.database import get_conn
    with get_conn() as c:
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info(ebay_listings)"
        ).fetchall()}
    for expected in (
        "last_qty_sync_at", "last_synced_quantity", "qty_sync_error",
    ):
        assert expected in cols, f"{expected} 列が無い (migration v40)"


def test_v40_purchase_confirmation_log_table(tmp_db):
    from monitor.database import get_conn
    with get_conn() as c:
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='purchase_confirmation_log'"
        ).fetchone()
    assert row is not None, "purchase_confirmation_log が作成されていない"


def test_v40_pcl_unique_is_gmail_and_item_not_sku(tmp_db):
    """dedupe キーが (gmail_id, ebay_item_id) で SKU を含まないこと."""
    from monitor.database import get_conn
    with get_conn() as c:
        idx_list = c.execute(
            "PRAGMA index_list(purchase_confirmation_log)"
        ).fetchall()
        unique_cols = []
        for idx in idx_list:
            if idx[2]:  # unique flag
                info = c.execute(
                    f"PRAGMA index_info({idx[1]})"
                ).fetchall()
                unique_cols.append([col[2] for col in info])
    assert ["gmail_id", "ebay_item_id"] in unique_cols, (
        f"UNIQUE が (gmail_id, ebay_item_id) でない: {unique_cols}"
    )
    # SKU が UNIQUE に含まれていないことを明示検証 (sku-rules.md).
    for cols in unique_cols:
        assert "sku" not in cols, "UNIQUE 制約に sku が含まれている (禁止)"


def test_v40_init_db_idempotent_preserves_data(tmp_db):
    """Q2: init_db 2 連続実行で purchase_confirmation_log のデータが保持."""
    from monitor.database import get_conn, init_db
    with get_conn() as c:
        c.execute(
            """INSERT INTO purchase_confirmation_log
               (gmail_id, ebay_item_id, sku, quantity_added)
               VALUES ('G1', 'item1', 'stock:01', 3)"""
        )
    init_db()  # 再実行
    with get_conn() as c:
        cnt = c.execute(
            "SELECT COUNT(*) FROM purchase_confirmation_log"
        ).fetchone()[0]
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert cnt == 1, "init_db 再実行でデータ消失 (冪等性違反)"
    assert ver >= 40


def test_v40_ebay_listings_inventory_preserved_on_reinit(tmp_db):
    """既存 inventory_count が init_db 再実行で消えないこと (Q2)."""
    from monitor.database import get_conn, init_db
    _insert_stock_listing("itemX", "stock:01", 7)
    init_db()
    with get_conn() as c:
        row = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id='itemX'"
        ).fetchone()
    assert row["inventory_count"] == 7


# =============================================================================
# sync_listing_quantity
# =============================================================================

_FAKE_CREDS = ("app", "dev", "cert", "tok")


def test_sync_success_updates_columns(tmp_db):
    _insert_stock_listing("S1", "stock:01", 5, quantity_ebay=0)
    from monitor import inventory_sync
    with patch.object(inventory_sync, "_get_credentials",
                      return_value=_FAKE_CREDS), \
         patch("monitor.ebay_client.revise_inventory_quantity",
               return_value={"success": True, "message": "ok"}) as m:
        res = inventory_sync.sync_listing_quantity("S1")
    assert res["success"] is True
    assert res["target_quantity"] == 5
    m.assert_called_once()
    from monitor.database import get_conn
    with get_conn() as c:
        row = c.execute(
            "SELECT quantity_ebay, last_synced_quantity, last_qty_sync_at, "
            "qty_sync_error FROM ebay_listings WHERE ebay_item_id='S1'"
        ).fetchone()
    assert row["quantity_ebay"] == 5
    assert row["last_synced_quantity"] == 5
    assert row["last_qty_sync_at"] is not None
    assert row["qty_sync_error"] is None


def test_sync_api_failure_records_error(tmp_db):
    _insert_stock_listing("S2", "stock:01", 4)
    from monitor import inventory_sync
    with patch.object(inventory_sync, "_get_credentials",
                      return_value=_FAKE_CREDS), \
         patch("monitor.ebay_client.revise_inventory_quantity",
               return_value={"success": False, "message": "API エラー: boom"}):
        res = inventory_sync.sync_listing_quantity("S2")
    assert res["success"] is False
    assert "boom" in res["message"]
    from monitor.database import get_conn
    with get_conn() as c:
        row = c.execute(
            "SELECT qty_sync_error, last_qty_sync_at "
            "FROM ebay_listings WHERE ebay_item_id='S2'"
        ).fetchone()
    assert row["qty_sync_error"] is not None
    assert "boom" in row["qty_sync_error"]
    assert row["last_qty_sync_at"] is None  # 失敗時は時刻を更新しない


def test_sync_target_zero_oos_on_executes_revise(tmp_db):
    """在庫0 + OOS Control ON → 数量0 revise を実行する."""
    _insert_stock_listing("S3", "stock:01", 0)
    from monitor import inventory_sync
    with patch.object(inventory_sync, "_get_credentials",
                      return_value=_FAKE_CREDS), \
         patch("monitor.ebay_client.get_out_of_stock_control_enabled",
               return_value=True), \
         patch("monitor.ebay_client.revise_inventory_quantity",
               return_value={"success": True, "message": "ok"}) as m:
        res = inventory_sync.sync_listing_quantity("S3")
    assert res["success"] is True
    assert res["skipped_zero_unsafe"] is False
    # 数量0 で revise を呼んだことを確認
    args = m.call_args[0]
    assert args[1] == 0, f"数量0 で revise されていない: {args}"


def test_sync_target_zero_oos_off_suppresses(tmp_db):
    """在庫0 + OOS Control OFF → 数量0 revise を抑止 (Defect 防止)."""
    _insert_stock_listing("S4", "stock:01", 0)
    from monitor import inventory_sync
    with patch.object(inventory_sync, "_get_credentials",
                      return_value=_FAKE_CREDS), \
         patch("monitor.ebay_client.get_out_of_stock_control_enabled",
               return_value=False), \
         patch("monitor.ebay_client.revise_inventory_quantity") as m:
        res = inventory_sync.sync_listing_quantity("S4")
    assert res["success"] is False
    assert res["skipped_zero_unsafe"] is True
    m.assert_not_called()  # revise を一切呼ばない
    from monitor.database import get_conn
    with get_conn() as c:
        err = c.execute(
            "SELECT qty_sync_error FROM ebay_listings WHERE ebay_item_id='S4'"
        ).fetchone()["qty_sync_error"]
    assert err is not None and "OOS" in err


def test_sync_target_zero_oos_unknown_suppresses(tmp_db):
    """在庫0 + OOS Control 不明 (API None) → 抑止 (安全側)."""
    _insert_stock_listing("S5", "stock:01", 0)
    from monitor import inventory_sync
    with patch.object(inventory_sync, "_get_credentials",
                      return_value=_FAKE_CREDS), \
         patch("monitor.ebay_client.get_out_of_stock_control_enabled",
               return_value=None), \
         patch("monitor.ebay_client.revise_inventory_quantity") as m:
        res = inventory_sync.sync_listing_quantity("S5")
    assert res["success"] is False
    assert res["skipped_zero_unsafe"] is True
    m.assert_not_called()


def test_sync_missing_listing(tmp_db):
    from monitor import inventory_sync
    res = inventory_sync.sync_listing_quantity("NOPE")
    assert res["success"] is False
    assert "ebay_listings に無い" in res["message"]


def test_sync_null_inventory_skips(tmp_db):
    _insert_stock_listing("S6", "stock:01", None)
    from monitor import inventory_sync
    with patch.object(inventory_sync, "_get_credentials",
                      return_value=_FAKE_CREDS):
        res = inventory_sync.sync_listing_quantity("S6")
    assert res["success"] is False
    assert "未入力" in res["message"]
