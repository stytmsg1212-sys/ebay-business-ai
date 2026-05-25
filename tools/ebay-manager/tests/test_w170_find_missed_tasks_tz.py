"""W170 (2026-05-25) 回帰テスト: find_missed_tasks の +9h 過剰補正バグ.

旧コード `WHERE DATE(started_at, '+9 hours') = ?` は JST naive 保存の started_at に
追加で +9h shift して比較していた → JST 15:00 以降の completion を翌日扱いにし、
当日の missed 判定で False Negative (false alarm) を生んでいた.

修正: `WHERE DATE(started_at) = ?` で JST naive 同士の比較.

関連:
    `.claude/rules/sqlite-timezone.md` (Python `datetime.now()` bind 系は JST naive)
    `.claude/rules/md-files-can-be-wrong.md` R-1
"""
from __future__ import annotations

from datetime import datetime

from monitor.database import init_db, get_conn
from monitor.task_execution_log import find_missed_tasks, log_task_start, log_task_finish


def _insert_completion(task_key: str, started_at_iso: str, batch_hour: int) -> None:
    """完了済 task の DB row を直接 INSERT (test fixture)."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO task_execution_log
              (task_key, display_name, batch_id, batch_hour, status,
               started_at, finished_at, duration_sec, success, expected_today)
            VALUES (?, ?, ?, ?, 'completed', ?, ?, 1.0, 1, 1)
            """,
            (task_key, task_key, f"test_{batch_hour}",
             batch_hour, started_at_iso, started_at_iso),
        )


def test_jst_15_completion_not_treated_as_tomorrow(monkeypatch):
    """JST 15:00 completion が翌日扱いで missed と誤判定されないこと (W170 本丸)."""
    init_db()
    # rival_pricing_refresh は expected_hours=[0, 6, 12, 18]、今 19:00 にチェック
    # 12:00 batch で完了済を挿入 (JST naive 形式)
    _insert_completion("rival_pricing_refresh", "2026-05-25 12:00:00.000000", 12)
    # 18:00 batch も完了済 (JST 18:00 = 旧コードだと DATE+9h=2026-05-26 で漏れる)
    _insert_completion("rival_pricing_refresh", "2026-05-25 18:00:00.000000", 18)
    # 0:00 と 6:00 は完了無し (本物の missed)

    config = {"execution_schedule": {"times": ["02:30", "11:00", "15:00", "18:00", "22:00"]}}
    now = datetime(2026, 5, 25, 19, 30, 0)  # 18:00 完了後
    missed = find_missed_tasks(now=now, config=config)

    missed_rival = [m for m in missed if m["task_key"] == "rival_pricing_refresh"]
    missed_hours = sorted(m["expected_hour"] for m in missed_rival)
    # 12, 18 は完了 → missed に含まれない
    assert 12 not in missed_hours, \
        f"JST 12:00 completion が誤って missed 扱い: {missed_hours}"
    assert 18 not in missed_hours, \
        f"JST 18:00 completion が誤って missed 扱い (W170 +9h bug 再発): {missed_hours}"
    # 0, 6 は本物の missed (今日まだ実行されていない)
    assert 0 in missed_hours
    assert 6 in missed_hours


def test_jst_15_main_slot_completion_within_today(monkeypatch):
    """JST 15:00 main slot completion が「今日」として認識されること.

    旧 bug = DATE(started_at, '+9 hours') で '2026-05-25 15:00' → '2026-05-26' 翌日扱い、
    completed_by_task に積まれず 19:00 health check で expected_hour=15 が missed と誤判定.
    """
    init_db()
    # ebay_sync hours=None = 全 main_slots [2,11,15,18,22] で expected
    _insert_completion("ebay_sync", "2026-05-25 15:30:00.000000", 15)

    config = {"execution_schedule": {"times": ["02:30", "11:00", "15:00", "18:00", "22:00"]}}
    now = datetime(2026, 5, 25, 16, 0, 0)  # 15:00 batch 完了後、19 batch 直前
    missed = find_missed_tasks(now=now, config=config)

    missed_sync = [m for m in missed if m["task_key"] == "ebay_sync"]
    missed_hours = sorted(m["expected_hour"] for m in missed_sync)
    # _slot_window(15) = [15, 17]、15:00 completion (batch_hour=15) が [15,17] に含まれる
    # → 15 slot 充足 → missed に含まれない
    assert 15 not in missed_hours, \
        f"JST 15:00 completion が翌日扱い (W170 +9h bug 再発): missed_hours={missed_hours}"


def test_log_task_start_stores_jst_naive():
    """log_task_start が JST naive 形式で保存する前提を固定 (W170 / sqlite-timezone exception)."""
    init_db()
    log_id = log_task_start("test_tz_check", "test", "test_batch", 16)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT started_at FROM task_execution_log WHERE id=?", (log_id,)
        ).fetchone()
    stored = row[0]
    now_jst = datetime.now()
    # JST naive なら現在 JST 時刻と数秒以内に一致するはず
    stored_dt = datetime.strptime(stored[:19], "%Y-%m-%d %H:%M:%S")
    diff_seconds = abs((stored_dt - now_jst).total_seconds())
    assert diff_seconds < 10, \
        f"started_at が JST naive 保存ではない (diff={diff_seconds}s) = UTC 化 regression"
