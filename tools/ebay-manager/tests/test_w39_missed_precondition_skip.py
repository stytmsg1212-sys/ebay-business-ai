"""依頼ボード #39 Phase A S3 回帰テスト: missed 誤判定の根治.

根因: CDP Chrome 不在で正当スキップするタスクが success=False のまま
log_task_skip を呼ばないため、find_missed_tasks (completed success=1 のみ
充足判定) が毎日「欠落」と誤検知していた.

修正:
  1. 検知層 (monitor/task_execution_log.find_missed_tasks): slot 窓内に
     status LIKE 'skip%' の行があれば充足扱いにする.
  2. タスク層 (task_research_harvest / task_research_duel): CDP 不在経路で
     log_task_skip(skip_kind='skip_other', reason='cdp_absent (port 9222)')
     を呼び、skip 痕を残す (success フラグの意味は変えない).

関連: `.claude/rules/sqlite-timezone.md` (started_at は JST naive 保存).
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from monitor.database import init_db, get_conn
from monitor.task_execution_log import find_missed_tasks, log_task_skip


def _insert_skip(task_key: str, started_at_iso: str, batch_hour: int, status: str) -> None:
    """任意 status ('skip_time'/'skip_other' 等) の skip 行を直接 INSERT (test fixture).

    log_task_skip の skip_kind allowlist に含まれない status を意図的に注入したい時
    (skip_time は既存事故検知経路につき、生成源は execute_daily_tasks の should_task_run
    False 経路。テストでは status 文字列を直接 INSERT して window 突合を検証する).
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO task_execution_log
              (task_key, display_name, batch_id, batch_hour, status,
               started_at, message, expected_today)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (task_key, task_key, f"test_{batch_hour}",
             batch_hour, status, started_at_iso, f"test {status}"),
        )


def _insert_completion(task_key: str, started_at_iso: str, batch_hour: int) -> None:
    """completed(success=1) の DB row を直接 INSERT (test fixture)."""
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


_CONFIG = {"execution_schedule": {"times": ["02:30", "11:00", "15:00", "18:00", "22:00"]}}


# ---------------------------------------------------------------------------
# (a) skip 行がある slot は missed にならない
# ---------------------------------------------------------------------------


def test_skip_trace_satisfies_slot_not_missed():
    """research_harvest の CDP 不在 skip 痕 (status='skip_other') がある slot は
    missed に含まれないこと (find_missed_tasks の新充足条件)."""
    init_db()
    # research_harvest は hours=[3] (独立 cron). skip 痕を batch_hour=3 で INSERT.
    log_task_skip(
        task_key="research_harvest",
        display_name="research_harvest",
        batch_id="test_batch_3",
        batch_hour=3,
        reason="cdp_absent (port 9222)",
        skip_kind="skip_other",
    )

    now = datetime(2026, 7, 3, 12, 0, 0)  # 03:00 slot の後
    missed = find_missed_tasks(now=now, config=_CONFIG)

    missed_keys = [m["task_key"] for m in missed]
    assert "research_harvest" not in missed_keys, \
        f"skip 痕があるのに missed 誤検知: {missed_keys}"


# ---------------------------------------------------------------------------
# (b) 何の痕跡も無い slot は従来通り missed
# ---------------------------------------------------------------------------


def test_no_trace_still_missed():
    """success も skip 痕も一切無い slot は従来通り missed のまま (真の欠落は検知維持)."""
    init_db()
    # research_duel は hours=[5]. 何も INSERT しない.

    now = datetime(2026, 7, 3, 12, 0, 0)  # 05:00 slot の後
    missed = find_missed_tasks(now=now, config=_CONFIG)

    missed_keys = [m["task_key"] for m in missed]
    assert "research_duel" in missed_keys, \
        "真の欠落 (痕跡ゼロ) が missed 検知から漏れている = 検知能力低下 regression"


# ---------------------------------------------------------------------------
# HIGH-2 (統合レビュー 2026-07-03): skip_time は充足扱いにしない
# 出典: 2026-04-25 daily_relist 5 日 silent skip / 2026-05-16 thread 跨ぎ clobber
#       事故。skip_time は「時刻ドリフトで誤スキップされた」signal であり、これを
#       充足扱いにすると再発時に欠落検知が沈黙する。
# ---------------------------------------------------------------------------


def test_skip_time_does_not_mask_missed():
    """slot 窓内に skip_time 行があっても missed のままであること (欠落検知維持)。

    daily_relist は hours=[2]。同 slot に skip_time 痕を注入しても、find_missed_tasks
    は「daily_relist が今日欠落」と検知しつづけなければならない (再発検知の沈黙防止)。
    """
    init_db()
    _insert_skip("daily_relist", "2026-07-03 02:30:00.000000", 2, "skip_time")

    now = datetime(2026, 7, 3, 12, 0, 0)  # 02:00 slot の後
    missed = find_missed_tasks(now=now, config=_CONFIG)

    missed_keys = [m["task_key"] for m in missed]
    assert "daily_relist" in missed_keys, (
        f"skip_time が充足扱いされて欠落検知が沈黙している "
        f"(2026-04-25/05-16 事故の検知経路劣化): missed={missed_keys}"
    )


def test_skip_other_still_satisfies_alongside_skip_time():
    """同一 task に skip_time と skip_other が両方あれば skip_other 側で充足扱い。

    (skip_time 単独は充足しないが、正当な skip_other が同 slot 窓内にあれば充足する
    ことを保証。skip_time 除外が過度に厳しくならないことの確認。)
    """
    init_db()
    _insert_skip("research_harvest", "2026-07-03 03:30:00.000000", 3, "skip_time")
    _insert_skip("research_harvest", "2026-07-03 03:31:00.000000", 3, "skip_other")

    now = datetime(2026, 7, 3, 12, 0, 0)
    missed = find_missed_tasks(now=now, config=_CONFIG)

    missed_keys = [m["task_key"] for m in missed]
    assert "research_harvest" not in missed_keys, (
        f"skip_other 痕があるのに missed 誤検知: {missed_keys}"
    )


# ---------------------------------------------------------------------------
# (c) completed success=1 は従来通り充足
# ---------------------------------------------------------------------------


def test_completed_success_still_satisfies():
    """success=1 の completed 行は従来通り missed に含まれないこと (既存挙動維持)."""
    init_db()
    _insert_completion("research_harvest", "2026-07-03 03:35:00.000000", 3)

    now = datetime(2026, 7, 3, 12, 0, 0)
    missed = find_missed_tasks(now=now, config=_CONFIG)

    missed_keys = [m["task_key"] for m in missed]
    assert "research_harvest" not in missed_keys


# ---------------------------------------------------------------------------
# (d) research_harvest の CDP 不在経路が log_task_skip を呼ぶ
# ---------------------------------------------------------------------------


def test_harvest_cdp_absent_calls_log_task_skip():
    """CDP 不在時、research_harvest が log_task_skip(skip_kind='skip_other') を呼ぶこと."""
    from tasks.task_research_harvest import run_research_harvest

    config = {
        "discord": {"webhook_url": "http://fake-webhook.example.com"},
        "tasks_enabled": {
            "research_harvest": {
                "enabled": True,
                "seed_queries": [
                    {"label": "test", "query": "widget", "category_id": 0, "min_price": 100}
                ],
                "max_items_per_run": 50,
                "max_pages": 2,
            }
        },
    }

    with patch("tasks.task_research_harvest._check_cdp_available", return_value=False), \
         patch("tasks.task_research_harvest._send_discord") as mock_discord, \
         patch("daily_scheduler._batch_ctx") as mock_ctx, \
         patch("monitor.task_execution_log.log_task_skip") as mock_skip:
        mock_ctx.get.side_effect = lambda k, d=None: {"id": "b1", "hour": 3}.get(k, d)
        result = run_research_harvest(config)

    # success フラグの意味は変えない (CDP 不在は引き続き failure 扱い)
    assert result["success"] is False
    assert mock_discord.call_count >= 1
    mock_skip.assert_called_once()
    assert mock_skip.call_args.kwargs["task_key"] == "research_harvest"
    assert mock_skip.call_args.kwargs["skip_kind"] == "skip_other"
    assert "cdp_absent" in mock_skip.call_args.kwargs["reason"]


def test_duel_cdp_absent_calls_log_task_skip():
    """CDP 不在時、research_duel が log_task_skip(skip_kind='skip_other') を呼ぶこと."""
    from tasks.task_research_duel import run_research_duel

    init_db()  # duel_rounds テーブル (_invalidate_stale_rounds が CDP チェック前に参照)
    config = {
        "tasks_enabled": {
            "research_duel": {"enabled": True},
        },
    }

    with patch("tasks.task_research_harvest._check_cdp_available", return_value=False), \
         patch("daily_scheduler._batch_ctx") as mock_ctx, \
         patch("monitor.task_execution_log.log_task_skip") as mock_skip:
        mock_ctx.get.side_effect = lambda k, d=None: {"id": "b1", "hour": 5}.get(k, d)
        result = run_research_duel(config)

    # success フラグの意味は変えない (CDP 不在は引き続き failure 扱い)
    assert result["success"] is False
    mock_skip.assert_called_once()
    assert mock_skip.call_args.kwargs["task_key"] == "research_duel"
    assert mock_skip.call_args.kwargs["skip_kind"] == "skip_other"
    assert "cdp_absent" in mock_skip.call_args.kwargs["reason"]
