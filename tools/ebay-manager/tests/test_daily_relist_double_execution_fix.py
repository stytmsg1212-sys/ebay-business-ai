"""daily_relist 二重実行根治テスト (2026-06-30 incident 対応)

6/30 シナリオ:
  03:10 supplier_sweep 開始 (batch_hour=2, in-flight ~04:16)
  04:00 health_check が daily_relist を missed 誤判定 (task_execution_log に started 行なし)
  04:01 autofix が daily_relist を実行 → relist 7 件 (task_execution_log には記録されない)
  04:16 通常バッチが daily_relist を実行 → relist 7 件 = 計 14 件二重実行

修正1 (find_missed_tasks in-flight 検知):
  supplier_sweep が in-flight なら daily_relist を missed に含めない → autofix 発火しない

修正2 (relist_history ガード):
  autofix が先に実行して relist_history に success=1 が残っていれば通常バッチが skip
  (task_execution_log に記録がなくても ground truth で防御)

両修正が合わさって二重実行を完全根治する。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _init() -> None:
    from monitor.database import init_db
    init_db()


# ---------------------------------------------------------------------------
# Fix 1: find_missed_tasks の in-flight バッチ検知
# ---------------------------------------------------------------------------

def _insert_inflight(task_key: str, batch_hour: int, started_at: datetime) -> None:
    """task_execution_log に in-flight 行 (status='started', finished_at=NULL) を挿入。"""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT INTO task_execution_log
               (task_key, display_name, batch_id, batch_hour, status,
                started_at, expected_today)
               VALUES (?, ?, ?, ?, 'started', ?, 1)""",
            (task_key, task_key, "batch_test", batch_hour,
             started_at.strftime("%Y-%m-%d %H:%M:%S")),
        )


def test_find_missed_no_daily_relist_when_inflight():
    """Fix 1: supplier_sweep が in-flight (batch_hour=2, 直近 1h) のとき
    daily_relist が missed に含まれない。

    6/30 シナリオ再現: 04:00 health_check 時点で supplier_sweep が走行中。
    daily_relist の _slot_window(2) = (2, 10)、in-flight batch_hour=2 が窓内
    → daily_relist は missed にならない → autofix が発火しない。
    """
    _init()
    # now = 今日の 10:00 AM (daily_relist expected_hour=2 が "past" になる時刻)
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    # supplier_sweep: 1h 前から in-flight (3h 以内 = fresh)
    inflight_started = now - timedelta(hours=1)
    _insert_inflight("supplier_sweep", 2, inflight_started)

    from monitor.task_execution_log import find_missed_tasks
    missed = find_missed_tasks(now=now)

    missed_keys = {m["task_key"] for m in missed}
    assert "daily_relist" not in missed_keys, (
        f"daily_relist が missed に含まれてしまった: {missed}"
    )


def test_find_missed_includes_daily_relist_when_inflight_stale():
    """Fix 1 orphan 抑制: in-flight 行が 3h 超 (stale/orphan) なら
    daily_relist は依然として missed に含まれる。
    stale orphan は orphan_task 検知経路に委ね、autofix が正当に再実行する。
    """
    _init()
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    # 4h 前に started = 3h bound を超えている → orphan 扱い
    stale_started = now - timedelta(hours=4)
    _insert_inflight("supplier_sweep", 2, stale_started)

    from monitor.task_execution_log import find_missed_tasks
    missed = find_missed_tasks(now=now)

    missed_keys = {m["task_key"] for m in missed}
    assert "daily_relist" in missed_keys, (
        f"stale in-flight でも daily_relist は missed に含まれるべき: {missed}"
    )


def test_find_missed_includes_daily_relist_when_no_inflight():
    """Fix 1 回帰: in-flight 行なし + completed なし = 従来通り daily_relist が missed。"""
    _init()
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    # in-flight 行も completed 行も挿入しない

    from monitor.task_execution_log import find_missed_tasks
    missed = find_missed_tasks(now=now)

    missed_keys = {m["task_key"] for m in missed}
    assert "daily_relist" in missed_keys, (
        f"in-flight なし・completed なしなら daily_relist は missed のはず: {missed}"
    )


def test_find_missed_multiple_tasks_same_batch():
    """Fix 1: supplier_sweep と inventory_check が同一 batch_hour=2 で in-flight のとき、
    その窓内にある daily_relist/cleanup_old_relisted 等が全て missed にならない。"""
    _init()
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    inflight_started = now - timedelta(hours=1)
    _insert_inflight("supplier_sweep", 2, inflight_started)

    from monitor.task_execution_log import find_missed_tasks
    missed = find_missed_tasks(now=now)

    missed_keys = {m["task_key"] for m in missed}
    # supplier_sweep/daily_relist/cleanup_old_relisted はいずれも窓[2,10]内
    for key in ("daily_relist", "cleanup_old_relisted"):
        assert key not in missed_keys, (
            f"{key} が in-flight 中に missed に含まれてしまった: {missed}"
        )


def test_find_missed_inflight_different_slot_does_not_suppress():
    """Fix 1 安全性: in-flight の batch_hour が別スロット (例: 11) なら
    hour=2 の daily_relist 窓 [2,10] とは重ならず missed に残る。"""
    _init()
    now = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
    # hour=11 で in-flight (全く別の batch)
    inflight_started = now - timedelta(hours=1)
    _insert_inflight("email_pickup", 11, inflight_started)

    from monitor.task_execution_log import find_missed_tasks
    missed = find_missed_tasks(now=now)

    missed_keys = {m["task_key"] for m in missed}
    # daily_relist (窓[2,10]) は batch_hour=11 の in-flight と重ならない → missed に残る
    assert "daily_relist" in missed_keys, (
        f"別スロット in-flight は daily_relist を保護しないはず: {missed}"
    )


# ---------------------------------------------------------------------------
# Fix 2: relist_history ガードの run_daily_relist
# ---------------------------------------------------------------------------

def _setup_creds(monkeypatch) -> None:
    monkeypatch.setattr(
        "tasks.task_daily_relist.get_ebay_credentials",
        lambda c: {"app_id": "x", "dev_id": "x", "cert_id": "x", "user_token": "x"},
    )
    monkeypatch.setattr(
        "tasks.task_daily_relist.ebay_credentials_ok",
        lambda c: True,
    )


def _insert_relist_history_today(n: int = 7) -> None:
    """本日 JST の relist_history success=1 行を n 件挿入。
    created_at は DEFAULT CURRENT_TIMESTAMP = UTC。
    autofix が先行実行して 7 件 relist 済の状態を模擬。
    task_execution_log には記録しない (autofix の実際の挙動)。
    """
    from monitor.database import get_conn
    with get_conn() as c:
        for i in range(n):
            c.execute(
                """INSERT INTO relist_history
                   (old_item_id, new_item_id, sku, title, end_reason, success)
                   VALUES (?, ?, 'stock1', 'Test Item', 'Incorrect', 1)""",
                (f"old_{i}", f"new_{i}"),
            )


def test_relist_history_guard_blocks_second_run_6_30_scenario(monkeypatch):
    """Fix 2 / 6/30 シナリオ再現:
    autofix が 04:01 に daily_relist を実行 → relist_history に 7 件 success=1 記録
    (task_execution_log には記録なし) →
    04:16 通常バッチが run_daily_relist を呼ぶ →
    relist_history ガードで即 skip → 二重実行しない。
    """
    _init()
    # autofix 実行済を模擬: relist_history success=1 あり / task_execution_log 空
    _insert_relist_history_today(7)

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
    assert "relist_history" in result.get("reason", ""), (
        f"reason にガード種別が含まれない: {result}"
    )
    assert select_calls == [], "relist 本体 (_select_relist_targets) が呼ばれてしまった"


def test_relist_history_guard_proceeds_when_no_history(monkeypatch):
    """Fix 2 回帰: relist_history が空 → 正常実行 (対象なしケース)。"""
    _init()
    # relist_history には何も挿入しない

    _setup_creds(monkeypatch)
    monkeypatch.setattr(
        "tasks.task_daily_relist._select_relist_targets",
        lambda *a, **k: [],
    )

    from tasks.task_daily_relist import run_daily_relist
    result = run_daily_relist({})

    assert result.get("skipped") is None, "relist_history 空なのに skip されてしまった"
    assert result["message"] == "対象listingなし"


def test_relist_history_guard_only_success1_triggers(monkeypatch):
    """Fix 2: relist_history に success=0 (失敗) だけあっても skip しない。
    失敗 relist で再試行の機会を奪わない。
    """
    _init()
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT INTO relist_history
               (old_item_id, new_item_id, sku, title, end_reason, success)
               VALUES ('old1', NULL, 'stock1', 'Failed Item', 'Incorrect', 0)"""
        )

    _setup_creds(monkeypatch)
    monkeypatch.setattr(
        "tasks.task_daily_relist._select_relist_targets",
        lambda *a, **k: [],
    )

    from tasks.task_daily_relist import run_daily_relist
    result = run_daily_relist({})

    assert result.get("skipped") is None, "success=0 行だけで skip されてしまった"
    assert result["message"] == "対象listingなし"


def test_relist_history_guard_skips_on_daily_relist_source(monkeypatch):
    """source='daily_relist' 行があれば skip する (v85 明示 source)。"""
    _init()
    from monitor.database import record_relist
    record_relist("old_x", "new_x", "stock1", "Title X", "Incorrect", True,
                  source='daily_relist')

    _setup_creds(monkeypatch)
    select_calls: list = []
    monkeypatch.setattr(
        "tasks.task_daily_relist._select_relist_targets",
        lambda *a, **k: select_calls.append(a) or [],
    )

    from tasks.task_daily_relist import run_daily_relist
    result = run_daily_relist({})

    assert result.get("skipped") is True, "daily_relist source で skip されなかった"
    assert select_calls == [], "_select_relist_targets が呼ばれてしまった"


def test_relist_history_guard_proceeds_on_ebaymag_source(monkeypatch):
    """ebaymag_relist が先行して source='ebaymag' 行を作っても daily_relist は skip しない。

    v85 根治の核心テスト: ebaymag_relist が enabled になり 02:xx 前に走っても
    daily_relist が恒久 skip する silent-skip を再現しない。
    """
    _init()
    from monitor.database import record_relist
    # ebaymag_relist 経路が先行して書いた行 (source='ebaymag')
    record_relist("old_emag", "new_emag", "stock1", "eBaymag Item", "Incorrect", True,
                  source='ebaymag')

    _setup_creds(monkeypatch)
    select_called: list = []
    monkeypatch.setattr(
        "tasks.task_daily_relist._select_relist_targets",
        lambda *a, **k: select_called.append(True) or [],
    )

    from tasks.task_daily_relist import run_daily_relist
    result = run_daily_relist({})

    assert result.get("skipped") is None, (
        "ebaymag source 行で daily_relist が誤 skip された (v85 根治失敗)"
    )
    assert select_called, "_select_relist_targets まで到達しなかった (ガード誤発火)"


def test_v85_migration_idempotency():
    """v85 migration 冪等性: init_db を 2 回連続実行してもデータ保持 + source 列存在。

    Q2 必須テスト (db-migration-rules.md): init_db 2 回連続でデータ消失しない。
    """
    _init()
    from monitor.database import get_conn, record_relist, init_db

    # v85 migration 済の状態でデータを挿入
    row_id = record_relist("idem_old", "idem_new", "stock1", "Idempotency Test",
                           "Incorrect", True, source='daily_relist')
    assert row_id is not None and row_id > 0

    # 2 回目の init_db
    init_db()

    with get_conn() as c:
        # データ保持確認
        count = c.execute(
            "SELECT COUNT(*) FROM relist_history WHERE old_item_id='idem_old'"
        ).fetchone()[0]
        assert count == 1, f"init_db 2 回目でデータが消失: count={count}"

        # source 列存在確認
        cols = {r[1] for r in c.execute("PRAGMA table_info(relist_history)").fetchall()}
        assert "source" in cols, "source 列が存在しない (v85 migration 失敗)"

        # source 値確認
        src = c.execute(
            "SELECT source FROM relist_history WHERE old_item_id='idem_old'"
        ).fetchone()[0]
        assert src == 'daily_relist', f"source 値が予期しない: {src}"


def test_relist_history_guard_self_detection_avoidance(monkeypatch):
    """Fix 2 自己検出回避: run_daily_relist 開始時点で relist_history が空なら
    ガードを通過して正常実行 (初回 run が自己 skip しない)。

    処理順: guard (relist_history check) → relist 本体 → relist_history INSERT
    ガードは INSERT より前なので 1 回目は常に 0 件 → skip しない。
    """
    _init()
    # relist_history 空 = 初回実行の状態

    _setup_creds(monkeypatch)
    select_called = []
    monkeypatch.setattr(
        "tasks.task_daily_relist._select_relist_targets",
        lambda *a, **k: select_called.append(True) or [],
    )

    from tasks.task_daily_relist import run_daily_relist
    result = run_daily_relist({})

    assert result.get("skipped") is None, "初回実行が自己 skip してしまった"
    # _select_relist_targets まで到達したことを確認 (= ガードを通過)
    assert select_called, "_select_relist_targets が呼ばれなかった (ガードが誤発火)"
