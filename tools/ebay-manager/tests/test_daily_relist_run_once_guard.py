"""daily_relist 二重実行ガード — Layer1 + Layer2 + Layer3 の回帰テスト.

Layer1 (汎用 / task_health_autofix):
  _handle_tier1_rerun が再実行直前に is_completed_or_running_today を呼び、
  completed(success=1) または in-flight (started/NULL) なら skip &
  autofix_attempt_log に 'skipped' を記録する。

Layer2 (money セーフティネット / task_daily_relist):
  run_daily_relist 冒頭で is_completed_today を呼び、当日 completed(success=1) なら
  即 return (relist せず)。in-flight は見ない (自己検出回避)。

Layer3 (プロセス間排他ロック / task_daily_relist):
  Layer2 通過後、cdp_lock.acquire(blocking=False, lock_path=_DAILY_RELIST_LOCK) で
  別プロセス/スレッドが同時に relist 本体に突入するのを物理排除する。
  LockBusy → skipped:True を返す (relist 実処理は呼ばない)。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta


# -----------------------------------------------------------------------
# 共通セットアップ
# -----------------------------------------------------------------------

def _init() -> None:
    from monitor.database import init_db
    init_db()


def _log_started(task_key: str = "daily_relist") -> int:
    """task_execution_log に status='started' / finished_at=NULL を INSERT し log_id を返す。"""
    from monitor.task_execution_log import log_task_start
    return log_task_start(task_key, "DR display", "batch1", 2)


def _log_completed(log_id: int) -> None:
    from monitor.task_execution_log import log_task_finish
    log_task_finish(log_id, True)


def _log_failed(log_id: int) -> None:
    from monitor.task_execution_log import log_task_finish
    log_task_finish(log_id, False)


# -----------------------------------------------------------------------
# is_completed_or_running_today helper (Layer1 専用)
# -----------------------------------------------------------------------

def test_layer1_helper_completed():
    """当日 completed(success=1) → True。"""
    _init()
    log_id = _log_started()
    _log_completed(log_id)
    from monitor.task_execution_log import is_completed_or_running_today
    assert is_completed_or_running_today("daily_relist") is True


def test_layer1_helper_inflight():
    """当日 started / finished_at=NULL (in-flight) → True。"""
    _init()
    _log_started()  # finish しない
    from monitor.task_execution_log import is_completed_or_running_today
    assert is_completed_or_running_today("daily_relist") is True


def test_layer1_helper_failed():
    """当日 failed → False (正当な再実行は維持)。"""
    _init()
    log_id = _log_started()
    _log_failed(log_id)
    from monitor.task_execution_log import is_completed_or_running_today
    assert is_completed_or_running_today("daily_relist") is False


def test_layer1_helper_no_row():
    """当日ログなし → False。"""
    _init()
    from monitor.task_execution_log import is_completed_or_running_today
    assert is_completed_or_running_today("daily_relist") is False


def test_layer1_helper_yesterday_completed():
    """昨日 completed → False (当日判定)。"""
    _init()
    from monitor.database import get_conn
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 02:30:00")
    with get_conn() as c:
        c.execute(
            "INSERT INTO task_execution_log "
            "(task_key, display_name, batch_id, batch_hour, status, "
            "started_at, finished_at, success, expected_today) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("daily_relist", "DR", "b0", 2, "completed",
             yesterday, yesterday, 1, 1),
        )
    from monitor.task_execution_log import is_completed_or_running_today
    assert is_completed_or_running_today("daily_relist") is False


# -----------------------------------------------------------------------
# is_completed_today helper (Layer2 専用 — in-flight 除外 = 自己検出回避)
# -----------------------------------------------------------------------

def test_layer2_helper_completed():
    """当日 completed(success=1) → True。"""
    _init()
    log_id = _log_started()
    _log_completed(log_id)
    from monitor.task_execution_log import is_completed_today
    assert is_completed_today("daily_relist") is True


def test_layer2_helper_inflight_returns_false():
    """当日 in-flight → False (自己検出回避: in-flight は見ない)。"""
    _init()
    _log_started()  # finish しない
    from monitor.task_execution_log import is_completed_today
    assert is_completed_today("daily_relist") is False


def test_layer2_helper_failed():
    """当日 failed → False。"""
    _init()
    log_id = _log_started()
    _log_failed(log_id)
    from monitor.task_execution_log import is_completed_today
    assert is_completed_today("daily_relist") is False


def test_layer2_helper_no_row():
    """当日ログなし → False。"""
    _init()
    from monitor.task_execution_log import is_completed_today
    assert is_completed_today("daily_relist") is False


def test_layer2_helper_yesterday_completed():
    """昨日 completed → False (当日判定)。"""
    _init()
    from monitor.database import get_conn
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 02:30:00")
    with get_conn() as c:
        c.execute(
            "INSERT INTO task_execution_log "
            "(task_key, display_name, batch_id, batch_hour, status, "
            "started_at, finished_at, success, expected_today) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("daily_relist", "DR", "b0", 2, "completed",
             yesterday, yesterday, 1, 1),
        )
    from monitor.task_execution_log import is_completed_today
    assert is_completed_today("daily_relist") is False


# -----------------------------------------------------------------------
# Layer1: _handle_tier1_rerun での二重実行ガード
# -----------------------------------------------------------------------

def _missed(task_key: str = "daily_relist") -> dict:
    """missed finding を持つ health_result スタブ。"""
    return {
        "success": True, "missed_count": 1,
        "missed": [{"task_key": task_key, "expected_hour": 2}],
        "coverage": {"coverable": 0, "dlq": 0, "dlq_skus": [],
                     "coverage_alert_sent": False},
        "url_divergence": {"divergence_count": 0, "divergent_ids": [],
                           "alert_sent": False},
        "phase_c": {"intermittent": [], "orphans": [], "db_locks": 0,
                    "subprocess_errors": [], "alert_sent": False},
    }


def _patch_notify(monkeypatch) -> None:
    monkeypatch.setattr(
        "tasks.task_health_autofix._notify_autofix_summary",
        lambda config, new_actions, fix_diffs=None: True,
    )


def _statuses(finding_hash: str) -> list[str]:
    from monitor.database import get_conn
    with get_conn() as c:
        return [r[0] for r in c.execute(
            "SELECT status FROM autofix_attempt_log WHERE finding_hash=? "
            "ORDER BY id", (finding_hash,)).fetchall()]


def test_layer1_skips_when_completed_today(monkeypatch):
    """当日 completed → _rerun_task を呼ばず autofix_attempt_log に 'skipped' を記録。"""
    _init()
    log_id = _log_started()
    _log_completed(log_id)

    _patch_notify(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: calls.append(a) or {"success": True},
    )

    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("daily_relist"))

    assert calls == []
    assert any(
        x["reason"] == "already_completed_or_inflight" for x in s["skipped"]
    )
    from monitor.health_autofix_log import make_finding_hash
    fh = make_finding_hash("missed_task", "daily_relist", None)
    assert _statuses(fh) == ["skipped"]


def test_layer1_skips_when_inflight(monkeypatch):
    """当日 in-flight → _rerun_task を呼ばず 'skipped' を記録。"""
    _init()
    _log_started()  # finish しない

    _patch_notify(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: calls.append(a) or {"success": True},
    )

    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("daily_relist"))

    assert calls == []
    assert any(x["reason"] == "already_completed_or_inflight" for x in s["skipped"])


def test_layer1_reruns_when_failed_today(monkeypatch):
    """当日 failed → 正当な再実行は維持 (_rerun_task が呼ばれる)。"""
    _init()
    log_id = _log_started()
    _log_failed(log_id)

    _patch_notify(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: calls.append(a) or {"success": True},
    )

    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("daily_relist"))

    assert len(calls) == 1
    assert s["reran"] == [{"task_key": "daily_relist"}]


def test_layer1_reruns_when_no_row(monkeypatch):
    """当日ログなし → 正当な再実行 (_rerun_task が呼ばれる)。"""
    _init()

    _patch_notify(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: calls.append(a) or {"success": True},
    )

    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("daily_relist"))

    assert len(calls) == 1


def test_layer1_generic_supplier_sweep(monkeypatch):
    """汎用ガード: supplier_sweep も完了済なら skip (daily_relist 専用でない)。"""
    _init()
    log_id = _log_started("supplier_sweep")
    _log_completed(log_id)

    _patch_notify(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: calls.append(a) or {"success": True},
    )

    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("supplier_sweep"))

    assert calls == []
    assert any(x["reason"] == "already_completed_or_inflight" for x in s["skipped"])


# -----------------------------------------------------------------------
# Layer2: run_daily_relist の run-once guard
# -----------------------------------------------------------------------

def _setup_creds(monkeypatch) -> None:
    monkeypatch.setattr(
        "tasks.task_daily_relist.get_ebay_credentials",
        lambda c: {"app_id": "x", "dev_id": "x", "cert_id": "x", "user_token": "x"},
    )
    monkeypatch.setattr(
        "tasks.task_daily_relist.ebay_credentials_ok",
        lambda c: True,
    )


def test_layer2_skips_when_completed_today(monkeypatch):
    """当日 completed → run_daily_relist が即 return (relist 処理を呼ばない)。"""
    _init()
    log_id = _log_started()
    _log_completed(log_id)

    _setup_creds(monkeypatch)
    select_calls: list = []
    monkeypatch.setattr(
        "tasks.task_daily_relist._select_relist_targets",
        lambda *a, **k: select_calls.append(a) or [],
    )

    from tasks.task_daily_relist import run_daily_relist
    result = run_daily_relist({})

    assert result["success"] is True
    assert result.get("skipped") is True
    assert result["processed"] == 0
    assert select_calls == []  # relist 処理を呼ばない


def test_layer2_proceeds_when_not_completed(monkeypatch):
    """当日 completed なし → 正常実行 (対象なしケース)。"""
    _init()

    _setup_creds(monkeypatch)
    monkeypatch.setattr(
        "tasks.task_daily_relist._select_relist_targets",
        lambda *a, **k: [],
    )

    from tasks.task_daily_relist import run_daily_relist
    result = run_daily_relist({})

    assert result.get("skipped") is None
    assert result["message"] == "対象listingなし"


def test_layer2_self_detection_avoidance(monkeypatch):
    """in-flight (started/NULL) があっても Layer2 はスキップしない (自己検出回避)。"""
    _init()
    _log_started()  # in-flight、finish しない

    _setup_creds(monkeypatch)
    monkeypatch.setattr(
        "tasks.task_daily_relist._select_relist_targets",
        lambda *a, **k: [],
    )

    from tasks.task_daily_relist import run_daily_relist
    result = run_daily_relist({})

    assert result.get("skipped") is None  # NOT skipped (in-flight は Layer2 対象外)
    assert result["message"] == "対象listingなし"


def test_layer2_proceeds_when_failed_today(monkeypatch):
    """当日 failed → Layer2 はスキップしない (正当な再実行を妨げない)。"""
    _init()
    log_id = _log_started()
    _log_failed(log_id)

    _setup_creds(monkeypatch)
    monkeypatch.setattr(
        "tasks.task_daily_relist._select_relist_targets",
        lambda *a, **k: [],
    )

    from tasks.task_daily_relist import run_daily_relist
    result = run_daily_relist({})

    assert result.get("skipped") is None
    assert result["message"] == "対象listingなし"


# -----------------------------------------------------------------------
# Layer3: プロセス間排他ロック (並行二重 relist 防止)
# -----------------------------------------------------------------------

def test_layer3_lock_held_skips(monkeypatch, tmp_path):
    """Layer3: daily_relist.lock を別スレッドが保持中 → relist を実行せず skipped:True を返す。"""
    _init()
    import monitor.cdp_lock as cl
    import tasks.task_daily_relist as _dr

    lock_path = tmp_path / "dr_test.lock"
    monkeypatch.setattr(_dr, "_DAILY_RELIST_LOCK", lock_path)

    ready = threading.Event()
    release = threading.Event()

    def _hold_lock() -> None:
        with cl.acquire(blocking=True, timeout=5.0, lock_path=lock_path):
            ready.set()
            release.wait(timeout=5.0)

    t = threading.Thread(target=_hold_lock, daemon=True)
    t.start()
    ready.wait(timeout=3.0)

    try:
        _setup_creds(monkeypatch)
        select_calls: list = []
        monkeypatch.setattr(
            "tasks.task_daily_relist._select_relist_targets",
            lambda *a, **k: select_calls.append(a) or [],
        )

        result = _dr.run_daily_relist({})

        assert result["success"] is True
        assert result.get("skipped") is True
        assert "lock" in result.get("reason", "").lower()
        assert select_calls == []  # relist 本体を呼ばない
    finally:
        release.set()
        t.join(timeout=3.0)


def test_layer3_lock_released_proceeds(monkeypatch, tmp_path):
    """Layer3: lock が解放された状態では正常に実行される (lock_path 差替後の回帰)。"""
    _init()
    import tasks.task_daily_relist as _dr

    lock_path = tmp_path / "dr_released.lock"
    monkeypatch.setattr(_dr, "_DAILY_RELIST_LOCK", lock_path)

    _setup_creds(monkeypatch)
    monkeypatch.setattr(
        "tasks.task_daily_relist._select_relist_targets",
        lambda *a, **k: [],
    )

    result = _dr.run_daily_relist({})

    assert result.get("skipped") is None
    assert result["message"] == "対象listingなし"


# -----------------------------------------------------------------------
# cdp_lock: lock_path 引数の後方互換 + 独立性テスト
# -----------------------------------------------------------------------

def test_cdp_lock_different_paths_independent(tmp_path):
    """lock_path=A と lock_path=B は互いに干渉しない (CDP lock と daily_relist lock が独立)。"""
    import monitor.cdp_lock as cl

    path_a = tmp_path / "lock_a.lock"
    path_b = tmp_path / "lock_b.lock"

    # path_a を保持しながら path_b を non-blocking で取得できる
    with cl.acquire(blocking=True, timeout=5.0, lock_path=path_a):
        # path_b は path_a と無関係 → LockBusy にならない
        with cl.acquire(blocking=False, lock_path=path_b):
            pass  # 両方同時保持 = 別パス間に排他は存在しない


def test_cdp_lock_default_path_compat(tmp_path, monkeypatch):
    """lock_path 省略時は CDP_LOCK_FILE を使う (既存 CDP caller の後方互換)。"""
    import monitor.cdp_lock as cl

    default_path = tmp_path / "cdp_default.lock"
    monkeypatch.setattr(cl, "CDP_LOCK_FILE", default_path)

    # lock_path 引数なし → CDP_LOCK_FILE が使用される (既存 caller と同じ呼び出し形式)
    with cl.acquire(blocking=True, timeout=5.0):
        assert default_path.exists(), "デフォルト CDP_LOCK_FILE が作成されていない"


# -----------------------------------------------------------------------
# L1 fix: autofix が skipped:True 結果を "resolved" でなく "skipped" に分類する
# -----------------------------------------------------------------------

def test_l1_autofix_skipped_result_records_as_skipped(monkeypatch):
    """L1 fix: _rerun_task が skipped:True を返した場合、autofix は 'resolved' でなく 'skipped' を記録する。"""
    _init()

    _patch_notify(monkeypatch)
    # _rerun_task が lock-held skip を返すようにモック
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: {
            "success": True, "skipped": True,
            "reason": "another daily_relist run in progress (lock held)",
            "processed": 0,
        },
    )

    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("daily_relist"))

    # reran (resolved) には入らない
    assert s["reran"] == [], f"reran should be empty, got {s['reran']}"
    # rerun_failed にも入らない
    assert s["rerun_failed"] == [], f"rerun_failed should be empty, got {s['rerun_failed']}"
    # skipped に入る (reason="rerun_skipped")
    assert any(x.get("reason") == "rerun_skipped" for x in s["skipped"]), \
        f"'rerun_skipped' not found in skipped: {s['skipped']}"
