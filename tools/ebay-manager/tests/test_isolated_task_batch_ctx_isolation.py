"""_run_isolated_task の _batch_ctx save/restore 動作 regression test.

2026-05-05 daily_relist 6 日 silent skip 事故対策:
  並行 daily batch (= execute_daily_tasks の長時間 batch) が 02:30 〜 03:13 で
  動作している間、03:00 に order_alert_check (= _run_isolated_task) が並行発火し、
  _batch_ctx["hour"] を 3 に上書き → 03:13 daily_relist の should_task_run 判定で
  batch_hour=3 not in [2] と誤判定して silent skip。
  本 test は _run_isolated_task が _batch_ctx を save/restore することを保証し、
  外側の daily batch context が hijack されないことを確認する。

2026-05-18 追記 (silent skip 再発の根治):
  上記 save/restore は **同一 thread** でしか正しくない。APScheduler は各 job を
  別 worker thread で並行実行するため、03:00 daily_codex_lint (別 thread・最大
  300s) が hour=3 を set して滞留する間、まだ走行中の 02:30 batch thread が
  clobbered hour=3 を読み daily_relist 等 execution_times=[2] task を毎日
  silent skip していた (5/16〜)。根治 = `_batch_ctx` の thread-local 化。
  既存 4 test (単一 thread) は thread-local 下でも save/restore がそのまま
  機能するため不変。下記 Concurrent 系が並行 thread 分離の regression。
"""
from __future__ import annotations

import sys
import threading
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


# ════════════════════════════════════════════════════════════════════
# Concurrent (別 worker thread) regression — 2026-05-18 silent skip 根治
# 旧 shared-global save/restore では下記が FAIL (thread B の hour=3 が
# thread A から見えて daily_relist が毎日 silent skip)。thread-local で PASS。
# ════════════════════════════════════════════════════════════════════

def test_concurrent_isolated_task_does_not_clobber_main_batch_hour():
    """別 thread の長時間 isolated task (03:00 codex_lint 相当) が hour=3 を
    set して滞留中でも、02:30 batch thread の should_task_run('daily_relist')
    が hour=2 のまま True を返すこと (2026-04-25/05-05/05-18 事故の核心)."""
    cfg = {"tasks_enabled": {"daily_relist": {"enabled": True,
                                              "execution_times": [2]}}}
    # main (= 02:30 batch) thread context
    daily_scheduler._batch_ctx["id"] = "20260518_02sched"
    daily_scheduler._batch_ctx["hour"] = 2
    daily_scheduler._batch_ctx["started_at"] = "MOCK"

    other_set = threading.Event()
    main_done = threading.Event()
    seen: dict = {}

    def _isolated_thread():
        # 別 worker thread (codex_lint 相当): hour=3 を set して滞留
        daily_scheduler._batch_ctx["hour"] = 3
        daily_scheduler._batch_ctx["id"] = "codexlint"
        seen["other_hour"] = daily_scheduler._batch_ctx["hour"]
        other_set.set()
        assert main_done.wait(timeout=5), "main thread timeout"

    t = threading.Thread(target=_isolated_thread)
    t.start()
    assert other_set.wait(timeout=5), "isolated thread start timeout"
    # ★核心: 別 thread が hour=3 set 滞留中でも main は hour=2 不可侵
    assert daily_scheduler._batch_ctx["hour"] == 2, (
        "thread-local 破れ: 別 thread の hour=3 が main batch を clobber "
        "(2026-05-18 silent skip 再発)"
    )
    assert daily_scheduler.should_task_run("daily_relist", cfg) is True, (
        "daily_relist が誤 skip (W1 SEO 中核の毎日機会損失 = 事故再発)"
    )
    main_done.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert seen["other_hour"] == 3  # 別 thread 側は自分の 3 を見る


def test_new_thread_gets_isolated_default_ctx():
    """main で hour=2 set 後に生成した別 thread は既定 (None) を見る
    = thread-local の独立性。main 側は維持される."""
    daily_scheduler._batch_ctx["hour"] = 2
    daily_scheduler._batch_ctx["id"] = "main_only"
    got: dict = {}

    def _w():
        got["hour"] = daily_scheduler._batch_ctx.get("hour")
        got["id"] = daily_scheduler._batch_ctx.get("id")

    t = threading.Thread(target=_w)
    t.start()
    t.join(timeout=5)
    assert got["hour"] is None and got["id"] is None, (
        f"thread 分離破れ: 別 thread が main の値を観測 {got}"
    )
    # main thread 側は維持
    assert daily_scheduler._batch_ctx["hour"] == 2
    assert daily_scheduler._batch_ctx["id"] == "main_only"


def test_dict_conversion_and_clear_update_still_work():
    """_run_isolated_task が依存する dict(_batch_ctx)/clear/update が
    thread-local proxy でも動作すること (後方互換)."""
    daily_scheduler._batch_ctx["id"] = "x"
    daily_scheduler._batch_ctx["hour"] = 9
    daily_scheduler._batch_ctx["started_at"] = "t"
    snap = dict(daily_scheduler._batch_ctx)
    assert snap == {"id": "x", "hour": 9, "started_at": "t"}
    daily_scheduler._batch_ctx.clear()
    assert daily_scheduler._batch_ctx.get("hour") is None
    daily_scheduler._batch_ctx.update(snap)
    assert daily_scheduler._batch_ctx["hour"] == 9
    assert "hour" in daily_scheduler._batch_ctx
