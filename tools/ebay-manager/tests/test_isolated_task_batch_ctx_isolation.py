"""_run_isolated_task の _batch_ctx save/restore 動作 regression test.

2026-05-05 daily_relist 6 日 silent skip 事故対策:
  並行 daily batch (= execute_daily_tasks の長時間 batch) が 02:30 〜 03:13 で
  動作している間、03:00 に order_alert_check (= _run_isolated_task) が並行発火し、
  _batch_ctx["hour"] を 3 に上書き → 03:13 daily_relist の should_task_run 判定で
  batch_hour=3 not in [2] と誤判定して silent skip。
  本 test は _run_isolated_task が _batch_ctx を save/restore することを保証し、
  外側の daily batch context が hijack されないことを確認する。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daily_scheduler


def test_isolated_task_restores_batch_ctx_after_run():
    """_run_isolated_task 完了後に _batch_ctx が呼び出し前状態に戻ること.

    2026-05-05 事故再発防止: order_alert_check が daily batch context を hijack
    した状態で残ると、後続の daily_relist 等の should_task_run 判定が破壊される.
    """
    # 模擬 daily batch context をセット (= execute_daily_tasks 02:30 batch 開始相当)
    daily_scheduler._batch_ctx["id"] = "20260505_02sched"
    daily_scheduler._batch_ctx["hour"] = 2
    daily_scheduler._batch_ctx["started_at"] = "MOCK_DT"

    # _run_isolated_task を 03:00 order_alert_check 相当で発火
    def _runner():
        # runner 内で _batch_ctx を読むと isolated 側の値 (hour=3)
        assert daily_scheduler._batch_ctx["hour"] == 3
        # batch_id format: f"{date}_{bh:02d}sched_{task_key[:12]}" = "...sched_order_alert_"
        assert daily_scheduler._batch_ctx["id"].endswith("sched_order_alert_")
        return {"success": True, "message": "ok"}

    daily_scheduler._run_isolated_task(
        task_key="order_alert_check",
        display_name="W7-A 注文アラート",
        runner=_runner,
        scheduled_hour=3,  # 03:00 cron 想定
    )

    # 完了後 daily batch の context が完全に戻っていること
    assert daily_scheduler._batch_ctx["id"] == "20260505_02sched"
    assert daily_scheduler._batch_ctx["hour"] == 2
    assert daily_scheduler._batch_ctx["started_at"] == "MOCK_DT"


def test_isolated_task_restores_batch_ctx_even_on_runner_exception():
    """runner が例外で落ちても _batch_ctx が restore されること."""
    daily_scheduler._batch_ctx["id"] = "20260505_02sched"
    daily_scheduler._batch_ctx["hour"] = 2
    daily_scheduler._batch_ctx["started_at"] = "MOCK_DT"

    def _runner():
        raise RuntimeError("simulated task crash")

    # 例外を呑んで処理続行する設計 (内部 try/except でラップ済)
    daily_scheduler._run_isolated_task(
        task_key="order_alert_check",
        display_name="W7-A 注文アラート",
        runner=_runner,
        scheduled_hour=3,
    )

    # 例外があっても context restore されている
    assert daily_scheduler._batch_ctx["hour"] == 2
    assert daily_scheduler._batch_ctx["id"] == "20260505_02sched"


def test_isolated_task_no_outer_ctx_then_restores_initial_state():
    """daily batch が走ってない時 (= 初期 None 状態) も restore で None に戻ること."""
    # 初期化
    daily_scheduler._batch_ctx["id"] = None
    daily_scheduler._batch_ctx["hour"] = None
    daily_scheduler._batch_ctx["started_at"] = None

    def _runner():
        # 内部では isolated 値が見える
        assert daily_scheduler._batch_ctx["hour"] == 5
        return {"success": True}

    daily_scheduler._run_isolated_task(
        task_key="x_news_check",
        display_name="W13",
        runner=_runner,
        scheduled_hour=5,
    )

    # 完了後 None に戻る
    assert daily_scheduler._batch_ctx["id"] is None
    assert daily_scheduler._batch_ctx["hour"] is None
    assert daily_scheduler._batch_ctx["started_at"] is None


def test_isolated_task_scheduled_hour_none_uses_wallclock():
    """30 分ごと task で scheduled_hour=None のとき wall-clock hour を使うこと."""
    daily_scheduler._batch_ctx["id"] = "20260505_02sched"
    daily_scheduler._batch_ctx["hour"] = 2
    daily_scheduler._batch_ctx["started_at"] = "MOCK_DT"

    captured_isolated_hour = []

    def _runner():
        captured_isolated_hour.append(daily_scheduler._batch_ctx["hour"])
        return {"success": True}

    # scheduled_hour=None で発火 (W7-A 30 分 cron 想定)
    daily_scheduler._run_isolated_task(
        task_key="order_alert_check",
        display_name="W7-A 注文アラート",
        runner=_runner,
        scheduled_hour=None,
    )

    # isolated 中は wall-clock hour (= 0-23 のいずれか) が使われた
    assert isinstance(captured_isolated_hour[0], int)
    assert 0 <= captured_isolated_hour[0] <= 23

    # 完了後 outer の hour=2 に restore される
    assert daily_scheduler._batch_ctx["hour"] == 2
