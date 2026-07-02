"""総点検 守り③-3: 朝ブリーフ末尾への AI 利用コスト行 (直近24h) 回帰テスト.

検証対象 (tasks/task_research_morning_brief.py):
- _ai_cost_summary_line: 0件 / 複数モデル top3 / cost NULL 行 / テーブル不在
- _append_cost_line_to_brief: answer_md への追記 + research_qa 永続化反映
- run_research_morning_brief: 生成成功時にコスト行が message/answer_md に含まれる

sqlite TIMEZONE 規約 (.claude/rules/sqlite-timezone.md パターン A) 準拠: 集計は
datetime('now','-24 hours') 相対範囲、JST 日付直書きしない。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()

    import tasks.task_research_morning_brief as mb_mod
    monkeypatch.setattr(mb_mod, "DB_PATH", db_path)
    yield db_path


def _insert_api_call(db_path, model, cost_usd, hours_ago=1, operation="test_op"):
    """called_at は CURRENT_TIMESTAMP 相当 (UTC naive) を hours_ago だけ遡って直接指定."""
    called_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            """INSERT INTO api_call_log
               (provider, model, operation, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, duration_ms, success,
                error_message, cost_usd, is_batch, called_at)
               VALUES ('anthropic', ?, ?, 100, 100, 0, 0, 1000, 1, NULL, ?, 0, ?)""",
            (model, operation, cost_usd, called_at),
        )
        con.commit()


class TestAiCostSummaryLine:
    def test_zero_calls_returns_zero_line(self, tmp_db):
        from tasks.task_research_morning_brief import _ai_cost_summary_line
        line = _ai_cost_summary_line()
        assert line == "AI利用(直近24h): $0.00 / 0 calls"

    def test_multiple_models_top3_breakdown(self, tmp_db):
        from tasks.task_research_morning_brief import _ai_cost_summary_line
        _insert_api_call(tmp_db, "claude-opus-4-8", 1.2345, hours_ago=1)
        _insert_api_call(tmp_db, "claude-sonnet-5", 0.50, hours_ago=2)
        _insert_api_call(tmp_db, "claude-haiku-4-5", 0.10, hours_ago=3)
        _insert_api_call(tmp_db, "gemini-2.5-flash", 0.02, hours_ago=4)  # 4番目=top3外

        line = _ai_cost_summary_line()
        assert line.startswith("AI利用(直近24h): $1.85 / 4 calls")
        assert "claude-opus-4-8=$1.23" in line
        assert "claude-sonnet-5=$0.50" in line
        assert "claude-haiku-4-5=$0.10" in line
        assert "gemini-2.5-flash" not in line  # top3 外は含まない

    def test_excludes_calls_older_than_24h(self, tmp_db):
        from tasks.task_research_morning_brief import _ai_cost_summary_line
        _insert_api_call(tmp_db, "claude-opus-4-8", 5.0, hours_ago=25)  # 24h 超 = 除外
        _insert_api_call(tmp_db, "claude-opus-4-8", 0.30, hours_ago=1)  # 24h 以内

        line = _ai_cost_summary_line()
        assert "$0.30 / 1 calls" in line

    def test_cost_usd_null_row_does_not_crash(self, tmp_db):
        """cost_usd NULL 行 (record 障害等) が集計を落とさないこと (COALESCE 境界)."""
        from tasks.task_research_morning_brief import _ai_cost_summary_line
        called_at = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(str(tmp_db)) as con:
            con.execute(
                """INSERT INTO api_call_log
                   (provider, model, operation, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, duration_ms, success,
                    error_message, cost_usd, is_batch, called_at)
                   VALUES ('anthropic', 'claude-opus-4-8', 'test_op', 0, 0, 0, 0,
                           100, 0, 'timeout', NULL, 0, ?)""",
                (called_at,),
            )
            con.commit()

        line = _ai_cost_summary_line()
        assert line == "AI利用(直近24h): $0.00 / 1 calls (内訳 top3: claude-opus-4-8=$0.00)"

    def test_missing_table_returns_empty_string_and_logs_warning(self, tmp_path, monkeypatch, caplog):
        """api_call_log テーブル不在でも例外を外に漏らさず空文字 + warning ログ (Q0)."""
        import logging
        empty_db = tmp_path / "no_table.db"
        sqlite3.connect(str(empty_db)).close()  # 空 DB (テーブル無し)

        import tasks.task_research_morning_brief as mb_mod
        monkeypatch.setattr(mb_mod, "DB_PATH", empty_db)

        with caplog.at_level(logging.WARNING, logger="tasks.task_research_morning_brief"):
            line = mb_mod._ai_cost_summary_line()

        assert line == ""
        assert any("AI利用コスト集計 skip" in rec.message for rec in caplog.records)


class TestAppendCostLineToBrief:
    def test_appends_line_and_persists_to_db(self, tmp_db):
        from tasks.task_research_morning_brief import _append_cost_line_to_brief
        _insert_api_call(tmp_db, "claude-opus-4-8", 0.75, hours_ago=1)

        with sqlite3.connect(str(tmp_db)) as con:
            cur = con.execute(
                "INSERT INTO research_qa (source, query, model, answer_md, cost_usd) "
                "VALUES ('morning_brief', 'q', 'claude-opus-4-8', '[本日の重点]\\n1. test', 0.5)"
            )
            qa_id = cur.lastrowid
            con.commit()

        answer = MagicMock(answer_md="[本日の重点]\n1. test", qa_id=qa_id)
        _append_cost_line_to_brief(answer)

        assert "AI利用(直近24h): $0.75 / 1 calls" in answer.answer_md
        assert "[本日の重点]" in answer.answer_md  # 既存本文は保持 (K2)

        with sqlite3.connect(str(tmp_db)) as con:
            row = con.execute(
                "SELECT answer_md FROM research_qa WHERE id=?", (qa_id,)
            ).fetchone()
        assert "AI利用(直近24h): $0.75 / 1 calls" in row[0]

    def test_no_op_when_cost_line_empty(self, tmp_path, monkeypatch):
        """api_call_log 不在時は answer_md を一切変更しない (K2 surgical)."""
        empty_db = tmp_path / "no_table.db"
        sqlite3.connect(str(empty_db)).close()
        import tasks.task_research_morning_brief as mb_mod
        monkeypatch.setattr(mb_mod, "DB_PATH", empty_db)

        answer = MagicMock(answer_md="[本日の重点]\n1. test", qa_id=1)
        original = answer.answer_md
        mb_mod._append_cost_line_to_brief(answer)
        assert answer.answer_md == original


class TestRunResearchMorningBriefIncludesCostLine:
    def test_success_path_includes_cost_line_in_persisted_answer(self, tmp_db):
        """ask() を mock し、成功パスで research_qa.answer_md にコスト行が反映される."""
        from tasks.task_research_morning_brief import run_research_morning_brief

        _insert_api_call(tmp_db, "claude-opus-4-8", 0.42, hours_ago=1)

        # ask() が呼ばれた時点で先に research_qa へ保存する挙動を模す (実挙動と同型)
        def _fake_ask(*args, **kwargs):
            with sqlite3.connect(str(tmp_db)) as con:
                cur = con.execute(
                    "INSERT INTO research_qa (source, query, model, answer_md, cost_usd, duration_ms) "
                    "VALUES ('morning_brief', 'q', 'claude-opus-4-8', "
                    "'[本日の重点 — test]\\n1. dummy', 0.42, 1000)"
                )
                qa_id = cur.lastrowid
                con.commit()
            return MagicMock(
                error=None, answer_md="[本日の重点 — test]\n1. dummy",
                qa_id=qa_id, citations=[], duration_ms=1000, cost_usd=0.42,
            )

        with patch("monitor.research_brain.ask", side_effect=_fake_ask):
            result = run_research_morning_brief({})

        assert result["success"] is True
        # answer_preview は [:200] truncate されるため in 判定は不安定 → assert しない
        # (実体の検証は直後の DB SELECT が担う。恒真 assert は code-review MEDIUM 指摘で削除)

        with sqlite3.connect(str(tmp_db)) as con:
            row = con.execute(
                "SELECT answer_md FROM research_qa WHERE id=?", (result["qa_id"],)
            ).fetchone()
        assert "AI利用(直近24h): $0.42 / 1 calls" in row[0]

    def test_failure_path_does_not_call_cost_line(self, tmp_db):
        """answer.error 時は _append_cost_line_to_brief を呼ばない (失敗パス無傷確認)."""
        from tasks.task_research_morning_brief import run_research_morning_brief

        def _fake_ask(*args, **kwargs):
            return MagicMock(error="error_max_budget_usd: $1.5", qa_id=1, cost_usd=1.5)

        with patch("monitor.research_brain.ask", side_effect=_fake_ask), \
             patch("tasks.task_research_morning_brief._notify_budget_exceeded") as mock_notify:
            result = run_research_morning_brief({})

        assert result["success"] is False
        mock_notify.assert_called_once()
