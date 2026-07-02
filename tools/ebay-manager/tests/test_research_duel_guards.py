"""回帰テスト: W286 リサーチ対戦アリーナ — 総点検 4 件修正の検証.

HIGH: save_ai_picks の状態ガード — user_done/completed/invalidated の round は
      削除・上書きを拒否する (採点確定後に夜間タスクが再実行されても採点前提データを
      無警告で消さない)。ai_pending/ai_done への保存 (同日リトライ) は引き続き許可。

無印: task_research_duel._invalidate_stale_rounds — 過去日付のまま ai_pending/ai_done で
      放置された round を、新ラウンド開始時に invalidated へ自動遷移させる。

LOW-a: tabs.tab_research_duel._REASON_REQUIRED_BELOW が
       monitor.research_duel_db._REASON_REQUIRED_BELOW の import であること (重複定義排除)。

テスト設計: hermetic (tmp DB、実 Opus/実 CDP/実 harvest なし)。
"""
from __future__ import annotations

import pytest


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """fresh DB を tmp_path に作成し、monitor.database.DB_PATH を差し替える."""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    return db_path


def _make_ai_done_round(jst_date: str = "2099-01-10") -> int:
    """ai_done 状態 (AI picks 保存済) の throwaway round を作り round_id を返す."""
    import monitor.research_duel_db as ddb
    rid = ddb.create_round(jst_date=jst_date, pattern="new")
    ddb.save_ai_picks(rid, [
        {"rc_id": None, "rank": i, "title_ja": f"テスト品{i}"}
        for i in range(1, 6)
    ])
    return rid


# ============================================================================
# save_ai_picks 状態ガード (HIGH)
# ============================================================================

def test_save_ai_picks_allowed_when_ai_pending(tmp_db):
    """ai_pending の round は保存でき、status が ai_done に自動前進する."""
    import monitor.research_duel_db as ddb
    rid = ddb.create_round(jst_date="2099-01-01", pattern="new")
    assert ddb.get_round(rid)["status"] == ddb.STATUS_AI_PENDING

    ok = ddb.save_ai_picks(rid, [
        {"rc_id": None, "rank": 1, "title_ja": "品A"},
    ])
    assert ok is True
    assert ddb.get_round(rid)["status"] == ddb.STATUS_AI_DONE


def test_save_ai_picks_allowed_when_ai_done_retry(tmp_db):
    """ai_done (同日リトライ) の round は上書き保存できる (正常経路)."""
    import monitor.research_duel_db as ddb
    from monitor.database import get_conn

    rid = _make_ai_done_round()
    assert ddb.get_round(rid)["status"] == ddb.STATUS_AI_DONE

    ok = ddb.save_ai_picks(rid, [
        {"rc_id": None, "rank": 1, "title_ja": "リトライ後の品"},
    ])
    assert ok is True
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title_ja FROM duel_ai_picks WHERE round_id=?", (rid,)
        ).fetchall()
    assert [r[0] for r in rows] == ["リトライ後の品"]


@pytest.mark.parametrize("target_status", ["user_done", "completed", "invalidated"])
def test_save_ai_picks_blocked_when_locked(tmp_db, target_status, caplog):
    """user_done/completed/invalidated の round は削除・上書きを拒否し、既存データを守る."""
    import monitor.research_duel_db as ddb
    from monitor.database import get_conn

    rid = _make_ai_done_round()
    # 既存 (採点前提) picks のタイトルを記録しておく (上書きされていないことを確認する用)。
    with get_conn() as conn:
        before = sorted(
            r[0] for r in conn.execute(
                "SELECT title_ja FROM duel_ai_picks WHERE round_id=?", (rid,)
            ).fetchall()
        )
    assert before  # ai_done 時点で 5 件保存されている

    # 対象 status まで正規の遷移で進める。
    if target_status == "user_done":
        ddb.update_round_status(rid, ddb.STATUS_USER_DONE)
    elif target_status == "completed":
        ddb.update_round_status(rid, ddb.STATUS_USER_DONE)
        ddb.update_round_status(rid, ddb.STATUS_COMPLETED)
    else:  # invalidated
        ddb.invalidate_round(rid, reason="test: force invalidated")

    assert ddb.get_round(rid)["status"] == getattr(ddb, f"STATUS_{target_status.upper()}")

    caplog.set_level("WARNING")
    ok = ddb.save_ai_picks(rid, [
        {"rc_id": None, "rank": 1, "title_ja": "無警告で消えてはいけない上書き試行"},
    ])

    assert ok is False, f"status={target_status} で保存が拒否されなかった"
    # Q0: 拒否理由が warning ログに残ること (silent skip 禁止)。
    assert any(
        "上書きを拒否" in rec.message and str(rid) in rec.message
        for rec in caplog.records
    ), "save_ai_picks 拒否時の logger.warning が出ていない (Q0 違反)"

    # 既存 (採点前提) データが消えていないこと。
    with get_conn() as conn:
        after = sorted(
            r[0] for r in conn.execute(
                "SELECT title_ja FROM duel_ai_picks WHERE round_id=?", (rid,)
            ).fetchall()
        )
    assert after == before, "拒否されたはずの save_ai_picks が既存データを変更した"


def test_save_ai_picks_missing_round_raises(tmp_db):
    """存在しない round_id は従来通り ValueError (状態ガードとは別経路)."""
    import monitor.research_duel_db as ddb
    with pytest.raises(ValueError):
        ddb.save_ai_picks(999999, [{"rc_id": None, "rank": 1, "title_ja": "x"}])


# ============================================================================
# 死んだラウンドの自動 invalidated 遷移
# ============================================================================

def test_invalidate_stale_rounds_transitions_past_ai_done(tmp_db):
    """過去日付の ai_done round は invalidated へ自動遷移する (round_id=4 型の対策)."""
    import datetime as _dt
    import monitor.research_duel_db as ddb
    from tasks.task_research_duel import _invalidate_stale_rounds

    stale_rid = _make_ai_done_round(jst_date="2026-06-28")
    assert ddb.get_round(stale_rid)["status"] == ddb.STATUS_AI_DONE

    today = _dt.date(2026, 7, 2)
    invalidated = _invalidate_stale_rounds(today)

    assert stale_rid in invalidated
    assert ddb.get_round(stale_rid)["status"] == ddb.STATUS_INVALIDATED


def test_invalidate_stale_rounds_ignores_ai_pending_past_round(tmp_db):
    """過去日付の ai_pending round も同様に invalidated へ自動遷移する."""
    import datetime as _dt
    import monitor.research_duel_db as ddb
    from tasks.task_research_duel import _invalidate_stale_rounds

    rid = ddb.create_round(jst_date="2026-06-20", pattern="echo")
    assert ddb.get_round(rid)["status"] == ddb.STATUS_AI_PENDING

    invalidated = _invalidate_stale_rounds(_dt.date(2026, 7, 2))

    assert rid in invalidated
    assert ddb.get_round(rid)["status"] == ddb.STATUS_INVALIDATED


def test_invalidate_stale_rounds_does_not_touch_today_or_completed(tmp_db):
    """当日の round と、既に終端 (completed/invalidated) の round は触らない."""
    import datetime as _dt
    import monitor.research_duel_db as ddb
    from tasks.task_research_duel import _invalidate_stale_rounds

    today = _dt.date(2026, 7, 2)

    # 当日 (today) の ai_pending round — 触ってはいけない。
    today_rid = ddb.create_round(jst_date=today.isoformat(), pattern="new")

    # 過去だが既に completed の round — 状態機械の許容遷移外なので触らない。
    completed_rid = _make_ai_done_round(jst_date="2026-06-25")
    ddb.update_round_status(completed_rid, ddb.STATUS_USER_DONE)
    ddb.update_round_status(completed_rid, ddb.STATUS_COMPLETED)

    invalidated = _invalidate_stale_rounds(today)

    assert today_rid not in invalidated
    assert completed_rid not in invalidated
    assert ddb.get_round(today_rid)["status"] == ddb.STATUS_AI_PENDING
    assert ddb.get_round(completed_rid)["status"] == ddb.STATUS_COMPLETED


def test_run_research_duel_invalidates_stale_before_harvest(tmp_db, monkeypatch):
    """run_research_duel 実行時に stale round 無効化が呼ばれる (統合的な配線確認).

    CDP 未接続 (テスト環境では起動していない) で harvest 手前で return するため、
    stale 無効化だけが副作用として確認できる (K1: harvest 本体はモックしない)。
    """
    import datetime as _dt
    import monitor.research_duel_db as ddb
    from tasks.task_research_duel import run_research_duel

    stale_rid = _make_ai_done_round(jst_date="2026-06-28")

    cfg = {
        "tasks_enabled": {
            "research_duel": {"enabled": True, "cycle_categories": [
                {"query": "test", "category_id": 1, "label": "テスト"},
            ]},
        },
    }
    # CDP 未接続を明示的に強制 (実 Chrome プロセスに依存しないテストにする)。
    import tasks.task_research_harvest as harvest_mod
    monkeypatch.setattr(harvest_mod, "_check_cdp_available", lambda: False)

    run_research_duel(cfg, today=_dt.date(2026, 7, 2))

    assert ddb.get_round(stale_rid)["status"] == ddb.STATUS_INVALIDATED


# ============================================================================
# LOW-a: _REASON_REQUIRED_BELOW の重複定義排除 (import 一致)
# ============================================================================

def test_tab_reason_required_below_imports_db_constant():
    """tabs.tab_research_duel._REASON_REQUIRED_BELOW は
    monitor.research_duel_db の同名定数を import したものであること (値の二重管理排除)."""
    import monitor.research_duel_db as ddb
    import tabs.tab_research_duel as tab

    assert tab._REASON_REQUIRED_BELOW is ddb._REASON_REQUIRED_BELOW
    assert tab._REASON_REQUIRED_BELOW == 60
