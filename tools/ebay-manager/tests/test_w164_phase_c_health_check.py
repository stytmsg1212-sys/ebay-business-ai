"""W164 Phase C 健康診断回帰テスト (2026-05-25).

code-reviewer HIGH-1 (self-error が Discord 通知に乗らず silent fail) と
MEDIUM-1 を HIGH 格上げ (started_at JST naive 保存と SQL UTC datetime() の 9h ずれ) の
2 件修正を回帰テストでロック.

関連 rule:
    `.claude/rules/silent-skip-prevention.md` (Q0 monitoring-of-monitoring 沈黙禁止)
    `.claude/rules/sqlite-timezone.md` + `md-files-can-be-wrong.md` R-1
    (本 table は JST naive 保存、rule 記述の 例外)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from tasks.task_scheduler_health_check import (
    _check_phase_c_health,
    _send_phase_c_alert,
)


def test_self_error_triggers_red_alert():
    """HIGH-1 回帰: db_query_error 単独で通知が fire し色は赤 (0xD84C38)."""
    findings = {
        "intermittent": [], "orphans": [],
        "db_locks": 0, "subprocess_errors": [],
        "db_query_error": "OperationalError: no such table",
    }
    captured = []
    fake_resp = MagicMock(status_code=204)
    with patch("httpx.post", return_value=fake_resp) as mp:
        mp.side_effect = lambda url, json, timeout: (captured.append(json), fake_resp)[1]
        sent = _send_phase_c_alert("https://example.test/webhook", findings)
    assert sent is True, "self-error 単独で通知未発射 = HIGH-1 再発"
    embed = captured[0]["embeds"][0]
    assert embed["color"] == 0xD84C38, "self-error 時は赤色 (R-11 最緊急)"
    assert any("[最緊急]" in f["name"] for f in embed["fields"])


def test_normal_alert_uses_orange():
    """通常 4 検査異常時は橙色 (0xE69138) — self-error と severity 分離."""
    findings = {
        "intermittent": [{"task_key": "x", "count": 3, "last_at": "2026-05-25 10:00:00"}],
        "orphans": [], "db_locks": 0, "subprocess_errors": [],
    }
    captured = []
    fake_resp = MagicMock(status_code=204)
    with patch("httpx.post", return_value=fake_resp) as mp:
        mp.side_effect = lambda url, json, timeout: (captured.append(json), fake_resp)[1]
        _send_phase_c_alert("https://example.test/webhook", findings)
    assert captured[0]["embeds"][0]["color"] == 0xE69138


def test_cutoff_is_jst_naive_string(monkeypatch):
    """MEDIUM-1 → HIGH 回帰: cutoff が JST naive 文字列で bind (UTC datetime('now') 不使用)."""
    captured_params: list = []

    class _MockCur:
        def fetchall(self):
            return []

    class _MockConn:
        def execute(self, sql, params=()):
            captured_params.append((sql[:60], params))
            return _MockCur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr("monitor.database.get_conn", lambda: _MockConn())
    _check_phase_c_health({})

    # 3 query 全てで cutoff が JST naive 形式 ('%Y-%m-%d %H:%M:%S') で bind されている
    assert len(captured_params) >= 3, "3 query が走っていない"
    now_jst = datetime.now()
    expected_24h_hour = (now_jst - timedelta(hours=24)).strftime("%Y-%m-%d %H")
    expected_2h_hour = (now_jst - timedelta(hours=2)).strftime("%Y-%m-%d %H")
    for sql_prefix, params in captured_params[:3]:
        assert params, f"bind params 空 = SQL datetime('now') 使用の可能性: {sql_prefix}"
        assert isinstance(params[0], str)
        assert "T" not in params[0], "ISO 'T' separator は使わない (DB 互換)"
        # 24h or 2h cutoff のいずれかと hour 単位で一致
        assert params[0].startswith(expected_24h_hour) or params[0].startswith(expected_2h_hour), \
            f"cutoff が JST naive と一致せず ({params[0]!r}) = TZ ずれ再発"
