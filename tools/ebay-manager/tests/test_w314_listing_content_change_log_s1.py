"""W314 商品仕上げパネル Phase1 S1 (2026-07-03): migration v88 + 監査ログ API.

設計書: .company/engineering/docs/2026-07-03-finishing-panel-design.md §6

Q2 db-migration-rules:
  - fresh DB → init_db で listing_content_change_log テーブルが存在 +
    user_version>=88
  - init_db を 2 回連続実行してもデータ保持・version drift なし (冪等)
  - v87 以前の DB からの upgrade パスで単一 init_db 呼出で v88 まで完走する

sku-rules.md: 本テーブルは ebay_item_id のみで識別、sku 列を持たない。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---- migration 冪等性 ----

def test_v88_table_and_index_exist_and_version(tmp_db):
    from monitor.database import get_conn
    with get_conn() as c:
        assert _table_exists(c, "listing_content_change_log")
        cols = _cols(c, "listing_content_change_log")
        assert cols == {
            "id", "ebay_item_id", "field", "before_value", "after_value",
            "source_tab", "candidate_id", "success", "ebay_ack", "changed_at",
        }
        idx = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_lccl_eid'"
        ).fetchone()
        assert idx is not None, "idx_lccl_eid が作成されていない"
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 88, f"user_version={ver} (期待 >=88)"


def test_v88_no_sku_column(tmp_db):
    """sku-rules.md: listing 識別は ebay_item_id、sku 列を持たない."""
    from monitor.database import get_conn
    with get_conn() as c:
        cols = _cols(c, "listing_content_change_log")
        assert "sku" not in cols


def test_v88_idempotent_data_preserved(tmp_db):
    """init_db 2 回実行でデータ保持 (Q2 冪等性必須テスト)."""
    from monitor.database import get_conn, init_db
    with get_conn() as c:
        c.execute(
            "INSERT INTO listing_content_change_log "
            "(ebay_item_id, field, before_value, after_value, success) "
            "VALUES (?, ?, ?, ?, ?)",
            ("110000000001", "title", "old title", "new title", 1),
        )
    init_db()  # 2 回目
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 88, f"version drift: {ver}"
        row = c.execute(
            "SELECT after_value FROM listing_content_change_log "
            "WHERE ebay_item_id='110000000001'"
        ).fetchone()
        assert row is not None and row[0] == "new title", (
            "listing_content_change_log データ消失 (Q2 冪等性違反)"
        )
        assert c.execute(
            "SELECT COUNT(*) FROM listing_content_change_log"
        ).fetchone()[0] == 1


def test_v88_forced_reentry_no_crash_and_no_duplicate(tmp_db):
    """user_version を 87 に戻して再 init → ALTER/CREATE 重複でも落ちない."""
    from monitor.database import get_conn, init_db
    with get_conn() as c:
        c.execute(
            "INSERT INTO listing_content_change_log "
            "(ebay_item_id, field, before_value, after_value) "
            "VALUES ('KEEP1', 'quantity', '1', '2')"
        )
        c.execute("PRAGMA user_version = 87")
    init_db()  # v88 block 再突入、落ちないこと
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 88
        row = c.execute(
            "SELECT after_value FROM listing_content_change_log "
            "WHERE ebay_item_id='KEEP1'"
        ).fetchone()
        assert row is not None and row[0] == "2", "既存データ消失"
        assert c.execute(
            "SELECT COUNT(*) FROM listing_content_change_log"
        ).fetchone()[0] == 1, "再突入で重複挿入されていないこと"


def test_v88_upgrade_path_from_v87(tmp_path, monkeypatch):
    """v87 以前の DB からの upgrade パスが単一 init_db 呼出で完走する."""
    db_path = tmp_path / "v87.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()  # まず最新まで作成
    with db_mod.get_conn() as c:
        c.execute("PRAGMA user_version = 85")  # v85 相当まで巻き戻す
    db_mod.init_db()  # v86 -> v87 -> v88 と連続適用されること
    with db_mod.get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 88
        assert _table_exists(c, "listing_content_change_log")
        assert _table_exists(c, "pricing_eligible_change_log")


# ---- log_content_change / get_content_changes API ----

def test_log_content_change_basic_roundtrip(tmp_db):
    from monitor.listing_content_change_log import (
        log_content_change, get_content_changes,
    )
    new_id = log_content_change(
        "110000000001", "title", "Old Title", "New Title",
        source_tab="product_management", success=True, ebay_ack="Success",
    )
    assert new_id is not None and new_id > 0

    rows = get_content_changes("110000000001")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == new_id
    assert row["ebay_item_id"] == "110000000001"
    assert row["field"] == "title"
    assert row["before_value"] == "Old Title"
    assert row["after_value"] == "New Title"
    assert row["source_tab"] == "product_management"
    assert row["success"] is True
    assert row["ebay_ack"] == "Success"
    assert row["changed_at"] is not None


def test_log_content_change_images_serialized_as_json(tmp_db):
    from monitor.listing_content_change_log import (
        log_content_change, get_content_changes,
    )
    before = ["https://example.com/a.jpg", "https://example.com/b.jpg"]
    after = ["https://example.com/c.jpg"]
    log_content_change("110000000002", "images", before, after, success=True)

    rows = get_content_changes("110000000002")
    assert len(rows) == 1
    assert json.loads(rows[0]["before_value"]) == before
    assert json.loads(rows[0]["after_value"]) == after


def test_log_content_change_default_success_false_and_none_values(tmp_db):
    from monitor.listing_content_change_log import (
        log_content_change, get_content_changes,
    )
    log_content_change("110000000003", "description", None, "new desc")
    rows = get_content_changes("110000000003")
    assert rows[0]["before_value"] is None
    assert rows[0]["after_value"] == "new desc"
    assert rows[0]["success"] is False
    assert rows[0]["candidate_id"] is None
    assert rows[0]["ebay_ack"] is None


def test_log_content_change_with_candidate_id(tmp_db):
    from monitor.listing_content_change_log import (
        log_content_change, get_content_changes,
    )
    log_content_change(
        "110000000004", "rank", "B", "A", candidate_id=42, success=True,
    )
    rows = get_content_changes("110000000004")
    assert rows[0]["candidate_id"] == 42


def test_get_content_changes_ordered_newest_first_and_limit(tmp_db):
    from monitor.listing_content_change_log import (
        log_content_change, get_content_changes,
    )
    for i in range(5):
        log_content_change("110000000005", "quantity", str(i), str(i + 1))

    rows = get_content_changes("110000000005", limit=3)
    assert len(rows) == 3
    # id は AUTOINCREMENT 昇順で採番されるため、新しい順 (id 降順) を検証
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids, reverse=True)


def test_get_content_changes_filters_by_ebay_item_id(tmp_db):
    from monitor.listing_content_change_log import (
        log_content_change, get_content_changes,
    )
    log_content_change("ITEM_A", "title", "a1", "a2")
    log_content_change("ITEM_B", "title", "b1", "b2")

    rows_a = get_content_changes("ITEM_A")
    assert len(rows_a) == 1
    assert rows_a[0]["ebay_item_id"] == "ITEM_A"


# ---- field validation (Q0: 不正値は ValueError で確実に落とす) ----

def test_log_content_change_rejects_invalid_field(tmp_db):
    from monitor.listing_content_change_log import log_content_change
    with pytest.raises(ValueError):
        log_content_change("110000000001", "not_a_valid_field", "x", "y")


def test_log_content_change_rejects_empty_ebay_item_id(tmp_db):
    from monitor.listing_content_change_log import log_content_change
    with pytest.raises(ValueError):
        log_content_change("", "title", "x", "y")
    with pytest.raises(ValueError):
        log_content_change(None, "title", "x", "y")


@pytest.mark.parametrize(
    "field", ["title", "description", "images", "rank", "quantity"]
)
def test_log_content_change_accepts_all_valid_fields(tmp_db, field):
    from monitor.listing_content_change_log import log_content_change
    new_id = log_content_change("110000000009", field, "before", "after")
    assert new_id > 0
