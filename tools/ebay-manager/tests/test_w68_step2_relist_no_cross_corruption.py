"""W68 Step 2 regression test: task_daily_relist の monitored_items UPDATE が
SKU 経由ではなく ebay_item_id 経由で行われることを保証する。

検証対象 (`tasks/task_daily_relist.py:274-277`):
- 旧コード: `UPDATE monitored_items SET ebay_item_id=? WHERE sku=?` (sku, new_item_id)
- 新コード: `UPDATE monitored_items SET ebay_item_id=? WHERE ebay_item_id=?` (old_item_id, new_item_id)

バグ: 有在庫 SKU (stock:01 等) を共有する複数 listing のうち relist 対象外の listing が
monitored_items に追跡されている場合、SKU 経由 UPDATE は無関係の追跡を破壊する。
monitored_items は UNIQUE(sku) 制約 (W7-A 以前の旧設計残存) のため 1 SKU = 1 行で
クロス listing 破壊は発生しないが、「追跡対象 listing が relist 対象外に切り替わる」
データ汚染が起こる。

過去事故: 2026-04-29 W7-A SKU 主キー崩壊 / 2026-04-30 SKU 一意性誤推論 (連続違反 = 品質事故)。
詳細: `.claude/rules/sku-rules.md` / `feedback_sku_misuse_repeat_offense.md`
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """一時 DB を作って monitor.database.DB_PATH を差し替え + init_db で schema 作成"""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


@pytest.fixture
def patched_ebay_apis(monkeypatch):
    """eBay API を mock。successful relist with new_item_id をシミュレート"""
    from tasks import task_daily_relist

    def _new_item_id_value():
        return "ITEM_A2"

    monkeypatch.setattr(
        task_daily_relist, "verify_relist_item",
        lambda *a, **k: {"success": True, "fees": []},
    )
    monkeypatch.setattr(
        task_daily_relist, "end_item",
        lambda *a, **k: {"success": True},
    )
    monkeypatch.setattr(
        task_daily_relist, "relist_item",
        lambda *a, **k: {"success": True, "new_item_id": _new_item_id_value()},
    )
    monkeypatch.setattr(task_daily_relist.time, "sleep", lambda *a, **k: None)
    yield _new_item_id_value


def _insert_ebay_listing(conn, ebay_item_id: str, sku: str, title: str = "Test"):
    conn.execute(
        "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
        "quantity_ebay, is_ended) VALUES (?, ?, ?, ?, ?, ?)",
        (ebay_item_id, sku, title, 100.0, 1, 0),
    )


def _insert_monitored_item(conn, ebay_item_id: str, sku: str, title: str = "Test"):
    conn.execute(
        "INSERT INTO monitored_items (ebay_item_id, sku, title, source_url) "
        "VALUES (?, ?, ?, ?)",
        (ebay_item_id, sku, title, f"http://test/{ebay_item_id}"),
    )


def test_relist_preserves_unrelated_monitored_item_tracking(tmp_db, patched_ebay_apis):
    """
    Bug 再現: stock:01 を共有する 2 listing (ITEM_A, ITEM_B) のうち
    monitored_items が ITEM_A のみを追跡中、ITEM_B を relist すると、
    SKU 経由 UPDATE は ITEM_A の追跡を ITEM_B の新 ItemID に上書きする (= 追跡破壊)。

    Fix: WHERE ebay_item_id=? で ITEM_B の追跡が monitored_items に存在しない場合、
    UPDATE は 0 件で完了し、ITEM_A の追跡が保護される。
    """
    from monitor.database import get_conn
    from tasks.task_daily_relist import process_single_relist

    # Setup: stock:01 を共有する 2 ebay_listings、monitored_items は ITEM_A のみ追跡
    with get_conn() as c:
        _insert_ebay_listing(c, "ITEM_A", "stock:01", "Listing A")
        _insert_ebay_listing(c, "ITEM_B", "stock:01", "Listing B")
        _insert_monitored_item(c, "ITEM_A", "stock:01", "Listing A")

    # Action: ITEM_B を relist (mock 返却 new_item_id="ITEM_A2")
    target = {
        "ebay_item_id": "ITEM_B",
        "sku": "stock:01",
        "title": "Listing B",
        "current_price": 100.0,
    }
    creds = {"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}
    result = process_single_relist(target, creds, dry_run=False, skip_verify=True)

    assert result["success"] is True
    assert result["new_item_id"] == "ITEM_A2"

    # Verify: ITEM_A の追跡は保護されているか
    with get_conn() as c:
        rows = c.execute(
            "SELECT ebay_item_id FROM monitored_items WHERE sku='stock:01'"
        ).fetchall()

    assert len(rows) == 1, f"monitored_items の行数異常: {rows}"
    assert rows[0][0] == "ITEM_A", (
        f"Cross-listing tracking corruption: monitored_items[stock:01] が "
        f"ITEM_A から {rows[0][0]} に誤更新された (relist 対象は ITEM_B のはず)"
    )


def test_relist_correctly_updates_tracked_listing(tmp_db, patched_ebay_apis):
    """
    通常ケース: relist 対象がそのまま monitored_items に追跡されている場合、
    新 ItemID への更新が正しく機能する (回帰防止)。
    """
    from monitor.database import get_conn
    from tasks.task_daily_relist import process_single_relist

    # Setup: ITEM_A のみ (monitored_items も追跡)
    with get_conn() as c:
        _insert_ebay_listing(c, "ITEM_A", "stock:01", "Listing A")
        _insert_monitored_item(c, "ITEM_A", "stock:01", "Listing A")

    # Action: ITEM_A を relist
    target = {
        "ebay_item_id": "ITEM_A",
        "sku": "stock:01",
        "title": "Listing A",
        "current_price": 100.0,
    }
    creds = {"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}
    result = process_single_relist(target, creds, dry_run=False, skip_verify=True)

    assert result["success"] is True
    assert result["new_item_id"] == "ITEM_A2"

    with get_conn() as c:
        rows = c.execute(
            "SELECT ebay_item_id FROM monitored_items WHERE sku='stock:01'"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "ITEM_A2", (
        f"Tracking 更新が機能していない: monitored_items[stock:01] = {rows[0][0]} "
        f"(期待: ITEM_A2)"
    )
