"""依頼ボード #39 Phase A S1 (2026-07-03) — notification_log migration v89 +
CRUD (monitor.notification_log_db) テスト。

対応内容:
  - migration 冪等性 (init_db 2 回でデータ保持 + user_version=89)
  - insert -> get -> mark_read round-trip
  - unread count (全体 / category 別)
  - dedupe (has_recent_dedupe)
  - category/severity whitelist validation (Q0: 不正値は ValueError)

本番 data/monitor.db を汚染しない: 全テストで tmp_path + monkeypatch を使用。
"""
from __future__ import annotations

import sqlite3

import pytest


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """fresh DB を tmp_path に作成し monitor.database.DB_PATH を差し替える."""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    return db_path


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


# ============================================================================
# migration 冪等性
# ============================================================================


def test_v89_table_and_indexes_exist_and_version(tmp_db):
    with sqlite3.connect(str(tmp_db)) as conn:
        assert _table_exists(conn, "notification_log")
        idx_names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='notification_log'"
            ).fetchall()
        }
        assert "idx_notiflog_unread" in idx_names
        assert "idx_notiflog_created" in idx_names
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 89, f"user_version={ver} (期待 >=89)"


def test_v89_idempotent_user_version(tmp_db):
    """init_db 2 回実行後も user_version が変わらない (冪等)。

    v90 (依頼ボード #45 / 2026-07-04): supplier_candidates に
    availability_attempted_at / availability_pending_reject を追加する
    migration が乗り、schema_ver が 89→90 に進んだ。本テストの目的は
    「特定バージョン固定」ではなく「2 回実行で drift しない」冪等性検証
    のため、現行最新バージョンに追従する。
    """
    import monitor.database as db_mod
    db_mod.init_db()  # 2 回目
    with sqlite3.connect(str(tmp_db)) as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 90, f"user_version が 90 でない: {ver}"


def test_v89_idempotent_data_preserved(tmp_db):
    """init_db 2 回実行後も notification_log に挿入したデータが保持される (Q2)。"""
    import monitor.database as db_mod
    from monitor.notification_log_db import insert_notification

    new_id = insert_notification("system", "info", "冪等テスト通知")

    db_mod.init_db()  # 2 回目

    with sqlite3.connect(str(tmp_db)) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM notification_log WHERE id = ?", (new_id,)
        ).fetchone()[0]
    assert cnt == 1, "init_db 2 回目実行でデータが消失した"


# ============================================================================
# insert -> get -> mark_read round-trip
# ============================================================================


def test_insert_and_get_notifications(tmp_db):
    from monitor.notification_log_db import insert_notification, get_notifications

    new_id = insert_notification(
        "inventory", "warning", "在庫切れ商品あり", body="詳細本文",
        link_target="inventory_tab", link_ref="ITEM123",
        discord_sent=True, dedupe_key="inv_ITEM123",
    )
    assert isinstance(new_id, int) and new_id > 0

    rows = get_notifications()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == new_id
    assert row["category"] == "inventory"
    assert row["severity"] == "warning"
    assert row["title"] == "在庫切れ商品あり"
    assert row["body"] == "詳細本文"
    assert row["link_target"] == "inventory_tab"
    assert row["link_ref"] == "ITEM123"
    assert row["discord_sent"] == 1
    assert row["dedupe_key"] == "inv_ITEM123"
    assert row["read_at"] is None
    assert row["created_at"] is not None


def test_get_notifications_unread_only_and_category_filter(tmp_db):
    from monitor.notification_log_db import insert_notification, get_notifications, mark_read

    id1 = insert_notification("inventory", "info", "在庫通知1")
    id2 = insert_notification("rival", "info", "ライバル通知1")
    mark_read([id1])

    unread = get_notifications(unread_only=True)
    assert [r["id"] for r in unread] == [id2]

    rival_only = get_notifications(category="rival")
    assert len(rival_only) == 1
    assert rival_only[0]["id"] == id2

    inventory_only = get_notifications(category="inventory")
    assert len(inventory_only) == 1
    assert inventory_only[0]["id"] == id1


def test_get_notifications_ordering_newest_first(tmp_db):
    from monitor.notification_log_db import insert_notification, get_notifications

    id1 = insert_notification("system", "info", "1件目")
    id2 = insert_notification("system", "info", "2件目")
    id3 = insert_notification("system", "info", "3件目")

    rows = get_notifications()
    assert [r["id"] for r in rows] == [id3, id2, id1]


def test_get_notifications_limit(tmp_db):
    from monitor.notification_log_db import insert_notification, get_notifications

    for i in range(5):
        insert_notification("system", "info", f"通知{i}")

    rows = get_notifications(limit=2)
    assert len(rows) == 2


def test_mark_read_returns_updated_count_and_is_idempotent(tmp_db):
    from monitor.notification_log_db import insert_notification, mark_read, get_unread_count

    id1 = insert_notification("system", "info", "通知1")
    id2 = insert_notification("system", "info", "通知2")

    updated = mark_read([id1, id2])
    assert updated == 2
    assert get_unread_count() == 0

    # 既に既読の id を再度渡しても 0 件更新 (read_at IS NULL 条件で二重加算しない)
    updated_again = mark_read([id1, id2])
    assert updated_again == 0


def test_mark_read_empty_list_is_noop(tmp_db):
    from monitor.notification_log_db import mark_read

    assert mark_read([]) == 0


def test_mark_category_read(tmp_db):
    from monitor.notification_log_db import (
        insert_notification, mark_category_read, get_unread_count_by_category,
    )

    insert_notification("inventory", "info", "在庫通知1")
    insert_notification("inventory", "info", "在庫通知2")
    insert_notification("rival", "info", "ライバル通知1")

    updated = mark_category_read("inventory")
    assert updated == 2

    counts = get_unread_count_by_category()
    assert "inventory" not in counts
    assert counts["rival"] == 1


def test_mark_all_read(tmp_db):
    from monitor.notification_log_db import (
        insert_notification, mark_all_read, get_unread_count,
    )

    insert_notification("inventory", "info", "在庫通知1")
    insert_notification("rival", "warning", "ライバル通知1")
    insert_notification("system", "error", "システム通知1")

    updated = mark_all_read()
    assert updated == 3
    assert get_unread_count() == 0


# ============================================================================
# 未読集計
# ============================================================================


def test_get_unread_count(tmp_db):
    from monitor.notification_log_db import insert_notification, get_unread_count, mark_read

    assert get_unread_count() == 0
    id1 = insert_notification("system", "info", "通知1")
    insert_notification("system", "info", "通知2")
    assert get_unread_count() == 2
    mark_read([id1])
    assert get_unread_count() == 1


def test_get_unread_count_by_category(tmp_db):
    from monitor.notification_log_db import insert_notification, get_unread_count_by_category

    insert_notification("inventory", "info", "在庫通知1")
    insert_notification("inventory", "warning", "在庫通知2")
    insert_notification("rival", "info", "ライバル通知1")

    counts = get_unread_count_by_category()
    assert counts == {"inventory": 2, "rival": 1}


# ============================================================================
# dedupe
# ============================================================================


def test_has_recent_dedupe_true_within_window(tmp_db):
    from monitor.notification_log_db import insert_notification, has_recent_dedupe

    insert_notification("rival", "info", "値下げ検知", dedupe_key="rival_ITEM999")
    assert has_recent_dedupe("rival_ITEM999", hours=24) is True


def test_has_recent_dedupe_false_when_no_match(tmp_db):
    from monitor.notification_log_db import insert_notification, has_recent_dedupe

    insert_notification("rival", "info", "値下げ検知", dedupe_key="rival_ITEM999")
    assert has_recent_dedupe("rival_OTHER", hours=24) is False


def test_has_recent_dedupe_false_outside_window(tmp_db):
    """dedupe_key はあるが created_at がウィンドウ外 (直接 UPDATE で過去日時に書き換え)。"""
    from monitor.notification_log_db import insert_notification, has_recent_dedupe
    import monitor.database as db_mod

    insert_notification("rival", "info", "古い値下げ検知", dedupe_key="rival_OLD")
    with db_mod.get_conn() as conn:
        conn.execute(
            "UPDATE notification_log SET created_at = datetime('now', '-48 hours') "
            "WHERE dedupe_key = 'rival_OLD'"
        )

    assert has_recent_dedupe("rival_OLD", hours=24) is False
    assert has_recent_dedupe("rival_OLD", hours=72) is True


def test_has_recent_dedupe_empty_key_returns_false(tmp_db):
    from monitor.notification_log_db import has_recent_dedupe

    assert has_recent_dedupe("", hours=24) is False
    assert has_recent_dedupe(None, hours=24) is False


# ============================================================================
# whitelist validation (Q0)
# ============================================================================


def test_insert_notification_invalid_category_raises(tmp_db):
    from monitor.notification_log_db import insert_notification

    with pytest.raises(ValueError):
        insert_notification("not_a_real_category", "info", "不正カテゴリ")


def test_insert_notification_invalid_severity_raises(tmp_db):
    from monitor.notification_log_db import insert_notification

    with pytest.raises(ValueError):
        insert_notification("system", "not_a_real_severity", "不正severity")


def test_insert_notification_empty_title_raises(tmp_db):
    from monitor.notification_log_db import insert_notification

    with pytest.raises(ValueError):
        insert_notification("system", "info", "")
    with pytest.raises(ValueError):
        insert_notification("system", "info", "   ")


def test_get_notifications_invalid_category_raises(tmp_db):
    from monitor.notification_log_db import get_notifications

    with pytest.raises(ValueError):
        get_notifications(category="bogus")


def test_mark_category_read_invalid_category_raises(tmp_db):
    from monitor.notification_log_db import mark_category_read

    with pytest.raises(ValueError):
        mark_category_read("bogus")


# ============================================================================
# get_nav_badge_counts への notifications_unread 追加
# ============================================================================


def test_get_nav_badge_counts_includes_notifications_unread(tmp_db):
    from monitor.database import get_nav_badge_counts
    from monitor.notification_log_db import insert_notification

    insert_notification("system", "info", "バッジテスト通知")

    counts = get_nav_badge_counts()
    assert "notifications_unread" in counts
    assert counts["notifications_unread"] == 1
