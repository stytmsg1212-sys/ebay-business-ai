"""回帰テスト: W286 リサーチ対戦アリーナ — 完了フロー HIGH 2 件修正の検証.

HIGH-1: user_done round で run_completion_learning が research_brain.ask を実呼出し、
        status が completed に進む (tab が先に completed に進めて no-op になる偽装成功を防ぐ)。

HIGH-2: run_completion_learning の戻り dict に summary_md キーが含まれる
        (tab が _res.get("summary_md") で表示できる契約)。

(c): already-completed round は no-op で冪等 (memory/rubric の二重生成なし)。

テスト設計:
- hermetic: 実 Opus/実 memory 書込なし。research_brain.ask と MEMORY_DIR を monkeypatch。
- データ層は monitor.research_duel_db (throwaway round)。
- fake ask の answer_md に <<<RUBRIC_JSON>>>[]<<<END_RUBRIC_JSON>>> を含める。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from types import SimpleNamespace
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


@pytest.fixture
def tmp_memory_dir(tmp_path, monkeypatch):
    """学習モジュールの MEMORY_DIR を tmp_path/memory に差し替え (実 memory dir を汚染しない)."""
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    import monitor.research_duel_learning as lrn
    monkeypatch.setattr(lrn, "MEMORY_DIR", mem_dir)
    monkeypatch.setattr(lrn, "RUBRIC_PATH", mem_dir / "reference_research_rubric.md")
    return mem_dir


def _make_user_done_round(tmp_db) -> int:
    """user_done 状態の throwaway round を作り round_id を返す."""
    import monitor.research_duel_db as ddb
    from monitor.database import get_conn

    rid = ddb.create_round(jst_date="2099-01-01", pattern="new")

    # AI picks を保存 (save_ai_picks は ai_pending → ai_done へ自動前進)
    ddb.save_ai_picks(rid, [
        {"rc_id": None, "rank": i, "title_ja": f"テスト品{i}"}
        for i in range(1, 6)
    ])

    # user picks を保存
    ddb.save_user_picks(rid, [
        {"rank": 1, "title_ja": "オーナー選品1", "why_md": "利益が出る"}
    ])

    # 全 AI picks を採点 (score 65 = 失点閾値 60 を超える → fb_md 任意)
    with get_conn() as conn:
        pick_ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM duel_ai_picks WHERE round_id=? ORDER BY rank", (rid,)
            ).fetchall()
        ]
    for pid in pick_ids:
        ddb.score_ai_pick(pid, user_score=65, user_fb_md="良品")

    # ai_done → user_done へ前進
    ddb.update_round_status(rid, ddb.STATUS_USER_DONE)
    return rid


def _fake_ask_answer() -> str:
    """Opus の fake 回答 (RUBRIC_JSON マーカー付き、パース可能)."""
    return (
        "## 総括\n\n今回の AI pick は概ね適切でした。低得点の共通要因は原価率の過大見積り。\n\n"
        "<<<RUBRIC_JSON>>>\n"
        '[{"rule": "eBay 手数料 + 送料込みで粗利 30% 以上を確保する", "scope": "general", "note": ""}]\n'
        "<<<END_RUBRIC_JSON>>>"
    )


# ============================================================================
# テスト (a): HIGH-1 ガード
# ============================================================================

def test_high1_ask_is_called_and_status_advances(tmp_db, tmp_memory_dir, monkeypatch):
    """user_done round で run_completion_learning が research_brain.ask を実呼出し、
    status が completed に進む (no-op 素通り防止)。"""
    ask_called = {"count": 0}

    # fake ResearchAnswer (research_brain.ask の戻り値)
    fake_answer = SimpleNamespace(
        qa_id=9999,
        answer_md=_fake_ask_answer(),
        error=None,
        via="opus",
        model_used="claude-opus-4-8",
        cost_usd=0.10,
    )

    def fake_ask(*args: Any, **kwargs: Any):
        ask_called["count"] += 1
        return fake_answer

    import monitor.research_brain as rb
    monkeypatch.setattr(rb, "ask", fake_ask)

    rid = _make_user_done_round(tmp_db)

    from monitor.research_duel_learning import run_completion_learning
    import monitor.research_duel_db as ddb

    # 学習前 status は user_done
    assert ddb.get_round(rid)["status"] == ddb.STATUS_USER_DONE

    result = run_completion_learning(rid)

    # research_brain.ask が実際に呼ばれた (Opus 学習が no-op でなかった証明)
    assert ask_called["count"] == 1, (
        "HIGH-1: research_brain.ask が呼ばれなかった。"
        "status を先に completed にして no-op になっている可能性あり。"
    )

    # 学習後 status が completed に進んだ
    assert ddb.get_round(rid)["status"] == ddb.STATUS_COMPLETED, (
        "HIGH-1: run_completion_learning 後も status が completed に進まなかった。"
    )

    assert result["success"] is True
    assert result["completed"] is True


# ============================================================================
# テスト (b): HIGH-2 ガード
# ============================================================================

def test_high2_summary_md_in_result(tmp_db, tmp_memory_dir, monkeypatch):
    """run_completion_learning の戻り dict に summary_md キーが含まれる."""
    fake_answer = SimpleNamespace(
        qa_id=1,
        answer_md=_fake_ask_answer(),
        error=None,
        via="opus",
        model_used="claude-opus-4-8",
        cost_usd=0.05,
    )

    import monitor.research_brain as rb
    monkeypatch.setattr(rb, "ask", lambda *a, **k: fake_answer)

    rid = _make_user_done_round(tmp_db)

    from monitor.research_duel_learning import run_completion_learning
    result = run_completion_learning(rid)

    assert result["success"] is True
    assert "summary_md" in result, (
        "HIGH-2: 戻り dict に summary_md キーがない。tab の _res.get('summary_md') が常に None になる。"
    )
    assert result["summary_md"], "HIGH-2: summary_md が空文字列。"
    # summary_path も残っている (後方互換)
    assert "summary_path" in result


# ============================================================================
# テスト (c): already-completed は no-op で冪等
# ============================================================================

def test_already_completed_is_noop(tmp_db, tmp_memory_dir, monkeypatch):
    """already-completed round は no-op で success=True を返し、ask を呼ばない
    (memory/rubric の二重生成なし)。"""
    ask_called = {"count": 0}

    fake_answer = SimpleNamespace(
        qa_id=2,
        answer_md=_fake_ask_answer(),
        error=None,
        via="opus",
        model_used="claude-opus-4-8",
        cost_usd=0.05,
    )

    import monitor.research_brain as rb
    monkeypatch.setattr(rb, "ask", lambda *a, **k: (ask_called.__setitem__("count", ask_called["count"] + 1), fake_answer)[1])

    rid = _make_user_done_round(tmp_db)

    from monitor.research_duel_learning import run_completion_learning
    import monitor.research_duel_db as ddb

    # 1 回目: 正常完了
    r1 = run_completion_learning(rid)
    assert r1["success"] is True
    assert ddb.get_round(rid)["status"] == ddb.STATUS_COMPLETED

    # summary ファイル書込数を記録
    mem_files_after_first = list(tmp_memory_dir.glob("feedback_research_duel_*.md"))
    assert len(mem_files_after_first) == 1

    ask_count_after_first = ask_called["count"]

    # 2 回目: already-completed は no-op
    r2 = run_completion_learning(rid)
    assert r2["success"] is True
    assert r2["reason"] == "already completed (no-op)"
    assert r2["completed"] is True

    # ask は追加で呼ばれていない (memory 二重生成なし)
    assert ask_called["count"] == ask_count_after_first, (
        "already-completed round で ask が再度呼ばれた (memory 二重生成リスク)。"
    )

    # memory ファイル数も変わっていない
    mem_files_after_second = list(tmp_memory_dir.glob("feedback_research_duel_*.md"))
    assert len(mem_files_after_second) == len(mem_files_after_first)


# ============================================================================
# テスト (d): save_user_pick (per-rank upsert / W299 方式A)
# ============================================================================

def test_save_user_pick_no_cross_rank_delete(tmp_db):
    """1 件 upsert で他 rank が消えないこと。"""
    import monitor.research_duel_db as ddb
    from monitor.database import get_conn

    rid = ddb.create_round(jst_date="2099-07-01", pattern="new")

    # rank 1 と rank 3 を一括保存 (既存 save_user_picks で土台を作る)
    ddb.save_user_picks(rid, [
        {"rank": 1, "title_ja": "オーナー品1"},
        {"rank": 3, "title_ja": "オーナー品3"},
    ])

    # rank 2 だけ per-rank upsert
    ddb.save_user_pick(rid, 2, "オーナー品2")

    with get_conn() as conn:
        rows = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT rank, title_ja FROM duel_user_picks"
                " WHERE round_id=? ORDER BY rank",
                (rid,),
            ).fetchall()
        }

    assert rows.get(1) == "オーナー品1", "rank 1 が消えた"
    assert rows.get(2) == "オーナー品2", "rank 2 が保存されていない"
    assert rows.get(3) == "オーナー品3", "rank 3 が消えた"


def test_save_user_pick_overwrite_same_rank(tmp_db):
    """同 rank 再保存で上書きされること。"""
    import monitor.research_duel_db as ddb
    from monitor.database import get_conn

    rid = ddb.create_round(jst_date="2099-07-02", pattern="new")

    ddb.save_user_pick(rid, 1, "最初のタイトル", why_md="最初の理由")
    ddb.save_user_pick(rid, 1, "上書きタイトル", why_md="上書き理由")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT title_ja, why_md FROM duel_user_picks WHERE round_id=? AND rank=?",
            (rid, 1),
        ).fetchone()

    assert row is not None
    assert row[0] == "上書きタイトル"
    assert row[1] == "上書き理由"


def test_save_user_pick_invalid_rank(tmp_db):
    """rank 範囲外 (0, 6) で ValueError。"""
    import monitor.research_duel_db as ddb

    rid = ddb.create_round(jst_date="2099-07-03", pattern="new")

    with pytest.raises(ValueError):
        ddb.save_user_pick(rid, 0, "テスト品")

    with pytest.raises(ValueError):
        ddb.save_user_pick(rid, 6, "テスト品")


def test_save_user_pick_empty_title(tmp_db):
    """title_ja が空文字 / 空白のみで ValueError。"""
    import monitor.research_duel_db as ddb

    rid = ddb.create_round(jst_date="2099-07-04", pattern="new")

    with pytest.raises(ValueError):
        ddb.save_user_pick(rid, 1, "")

    with pytest.raises(ValueError):
        ddb.save_user_pick(rid, 1, "   ")
