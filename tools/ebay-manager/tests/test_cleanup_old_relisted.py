"""task_cleanup_old_relisted の回帰テスト.

検証対象 (Q1 DoD: DB SELECT 直接 verify):
- 91 日経過 + ended_reason='daily_relist_seo' のみ DELETE (true positive)
- 89 日経過は残す (境界値、false positive 防止)
- ended_reason='not_in_active_list' は永続保持 (qty=0 復活機能と互換)
- is_ended=0 の active row は触らない (false positive 防止)
- relist_history は影響を受けない (履歴消失なし、設計の根幹)

過去事故 2026-04-25 W14 v18 migration 95 件消失の再発防止のため、
WHERE 条件の境界値テストに重点。

2026-05-01 W77 fix: `today = datetime.now().date()` (local time = JST) と
SQLite `date('now')` (UTC) の 9 時間ズレで早朝 JST 実行時に境界 d91 が
SQL threshold (UTC 90 日前 date) と一致して `<` 不成立で test fail 発生.
全境界基準を UTC に統一して時刻依存性を排除.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """一時 DB を作って monitor.database.DB_PATH を差し替え + init_db で schema 作成"""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert_listing(conn, ebay_item_id, is_ended, ended_reason, ended_at):
    conn.execute(
        "INSERT INTO ebay_listings (ebay_item_id, sku, title, is_ended, "
        "ended_reason, ended_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            ebay_item_id,
            f"stock:{ebay_item_id[-2:]}",
            "Test listing",
            is_ended,
            ended_reason,
            ended_at,
        ),
    )


def test_cleanup_deletes_only_91day_daily_relist(tmp_db):
    """91 日経過 daily_relist のみ DELETE、89 日 / not_in_active_list / active は残る"""
    from monitor.database import get_conn
    from tasks.task_cleanup_old_relisted import run_cleanup_old_relisted

    today = datetime.now(timezone.utc).date()
    d91 = today - timedelta(days=91)
    d89 = today - timedelta(days=89)
    d100 = today - timedelta(days=100)

    with get_conn() as c:
        _insert_listing(c, "AAAA0001", 1, "daily_relist_seo", f"{d91} 00:00:00")  # DELETE
        _insert_listing(c, "AAAA0002", 1, "daily_relist_seo", f"{d89} 00:00:00")  # KEEP (境界)
        _insert_listing(c, "AAAA0003", 1, "not_in_active_list", f"{d100} 00:00:00")  # KEEP (永続)
        _insert_listing(c, "AAAA0004", 0, None, None)  # KEEP (active)

    result = run_cleanup_old_relisted({})

    assert result["success"] is True
    assert result["deleted_count"] == 1

    with get_conn() as c:
        ids = [
            row[0]
            for row in c.execute(
                "SELECT ebay_item_id FROM ebay_listings ORDER BY ebay_item_id"
            ).fetchall()
        ]
    assert ids == ["AAAA0002", "AAAA0003", "AAAA0004"]


def test_cleanup_preserves_relist_history(tmp_db):
    """relist_history は cleanup で消えない (履歴消失なしの設計の根幹)"""
    from monitor.database import get_conn
    from tasks.task_cleanup_old_relisted import run_cleanup_old_relisted

    today = datetime.now(timezone.utc).date()
    d100 = today - timedelta(days=100)

    with get_conn() as c:
        _insert_listing(c, "OLD001", 1, "daily_relist_seo", f"{d100} 00:00:00")
        c.execute(
            "INSERT INTO relist_history (old_item_id, new_item_id, sku, title, "
            "end_reason, success) VALUES (?, ?, ?, ?, ?, 1)",
            ("OLD001", "NEW001", "stock:01", "Test", "Incorrect"),
        )

    result = run_cleanup_old_relisted({})

    assert result["success"] is True
    assert result["deleted_count"] == 1

    with get_conn() as c:
        rh_count = c.execute("SELECT COUNT(*) FROM relist_history").fetchone()[0]
        ebay_count = c.execute("SELECT COUNT(*) FROM ebay_listings").fetchone()[0]
    assert rh_count == 1, "relist_history は cleanup で消えてはいけない (系譜保持)"
    assert ebay_count == 0, "ebay_listings の対象 row は DELETE される"


def test_cleanup_zero_when_no_match(tmp_db):
    """対象 row なしでも success=True / deleted_count=0 (silent failure ではない)"""
    from tasks.task_cleanup_old_relisted import run_cleanup_old_relisted

    result = run_cleanup_old_relisted({})

    assert result["success"] is True
    assert result["deleted_count"] == 0
    assert "0 件" in result["message"]


def test_cleanup_does_not_touch_other_reasons(tmp_db):
    """ended_reason が daily_relist_seo 以外なら 100 日経過でも消さない"""
    from monitor.database import get_conn
    from tasks.task_cleanup_old_relisted import run_cleanup_old_relisted

    today = datetime.now(timezone.utc).date()
    d100 = today - timedelta(days=100)

    with get_conn() as c:
        _insert_listing(c, "BBBB0001", 1, "not_in_active_list", f"{d100} 00:00:00")
        _insert_listing(c, "BBBB0002", 1, "manual", f"{d100} 00:00:00")
        _insert_listing(c, "BBBB0003", 1, None, f"{d100} 00:00:00")

    result = run_cleanup_old_relisted({})

    assert result["success"] is True
    assert result["deleted_count"] == 0

    with get_conn() as c:
        count = c.execute("SELECT COUNT(*) FROM ebay_listings").fetchone()[0]
    assert count == 3, "ended_reason が daily_relist_seo 以外は永続保持"
