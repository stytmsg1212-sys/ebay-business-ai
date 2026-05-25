"""W164-pm regression test: morning_brief 予算超過時 Discord 明示通知.

2026-05-25 19:00 health check で 5/25 03:39 / 08:37 の 2 連続 failed (claude exit 1,
error_max_budget_usd) を検出. 既定 $0.50 budget を超過. fix:
  - max_budget_usd=1.0 (実コスト max=$0.36 / today $0.5099 + 100% buffer)
  - error_max_budget_usd 検出時 Discord 明示通知 (silent skip 防止、R-11)

code-reviewer HIGH-2 対応の regression coverage.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tasks.task_research_morning_brief import _notify_budget_exceeded


def test_notify_budget_exceeded_calls_discord():
    """answer.error が error_max_budget_usd の時、Discord.send_message が呼ばれる."""
    answer = MagicMock(error="error_max_budget_usd: $0.5099", cost_usd=0.5099, qa_id=42)
    mock_notifier = MagicMock(webhook_url="https://discord.com/api/webhooks/TEST")
    with patch("notifiers.discord_notifier.DiscordNotifier", return_value=mock_notifier):
        _notify_budget_exceeded({}, answer)
    mock_notifier.send_message.assert_called_once()
    msg = mock_notifier.send_message.call_args[0][0]
    assert "予算超過" in msg
    assert "0.5099" in msg
    assert "qa_id=42" in msg


def test_notify_budget_exceeded_logs_error_when_no_webhook(caplog):
    """webhook 未設定時は logger.error で痕跡 (silent skip 防止)."""
    import logging
    answer = MagicMock(error="error_max_budget_usd", cost_usd=0.51, qa_id=1)
    mock_notifier = MagicMock(webhook_url="")
    with caplog.at_level(logging.ERROR, logger="tasks.task_research_morning_brief"), \
         patch("notifiers.discord_notifier.DiscordNotifier", return_value=mock_notifier):
        _notify_budget_exceeded({}, answer)
    mock_notifier.send_message.assert_not_called()
    assert any("webhook_url 空" in rec.message for rec in caplog.records)


def test_notify_budget_exceeded_logs_when_send_fails(caplog):
    """Discord 送信戻り値 False (HTTP 非 2xx) も logger.error で痕跡."""
    import logging
    answer = MagicMock(error="error_max_budget_usd", cost_usd=0.51, qa_id=1)
    mock_notifier = MagicMock(webhook_url="https://discord.com/api/webhooks/TEST")
    mock_notifier.send_message.return_value = False  # 送信失敗
    with caplog.at_level(logging.ERROR, logger="tasks.task_research_morning_brief"), \
         patch("notifiers.discord_notifier.DiscordNotifier", return_value=mock_notifier):
        _notify_budget_exceeded({}, answer)
    assert any("Discord 送信失敗" in rec.message for rec in caplog.records)
