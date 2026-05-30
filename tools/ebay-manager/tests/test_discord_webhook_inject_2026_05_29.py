"""2026-05-29 通知漏れ修正の回帰テスト.

背景: 2026-05-25 .env 移行 (commit 8473103) で schedule_config.json から webhook_url を
撤去 → config['discord']['webhook_url'] が空になり、各タスクの通知ガードが construct 前に
early return して silent skip (Q0). `notifiers.discord_notifier.inject_webhook_into_config`
を各エントリポイント (daily_scheduler / run_task / keyword_watch subprocess) で呼んで復活.

code-reviewer HIGH-1: keyword_watch は subprocess 起動で自前 _load_config を持つため、
親の注入済 config を引き継げない → _load_config 自身も注入する必要がある.
"""
from __future__ import annotations

import pytest

from notifiers.discord_notifier import inject_webhook_into_config

_FAKE_WH = "https://discord.com/api/webhooks/000000/test-token"


def test_inject_into_empty_config(monkeypatch):
    """空 config に env webhook が注入される (5/25 移行後の通知ガード復活)."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", _FAKE_WH)
    cfg = inject_webhook_into_config({})
    assert cfg["discord"]["webhook_url"] == _FAKE_WH


def test_inject_respects_existing_value(monkeypatch):
    """冪等性: config に既存値があれば env で上書きしない."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", _FAKE_WH)
    existing = "https://discord.com/api/webhooks/111111/existing"
    cfg = inject_webhook_into_config({"discord": {"webhook_url": existing}})
    assert cfg["discord"]["webhook_url"] == existing


def test_inject_noop_when_env_absent(monkeypatch):
    """env に webhook が無ければ config を変更しない (新たな偽値を作らない)."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    cfg = inject_webhook_into_config({"discord": {"webhook_url": ""}})
    assert cfg["discord"]["webhook_url"] == ""


def test_keyword_watch_load_config_injects_env_webhook(monkeypatch):
    """HIGH-1 回帰: subprocess 経路の _load_config も .env webhook を注入する.

    schedule_config.json が空でも env があれば config['discord']['webhook_url'] が埋まり、
    新着ヒット通知ガードが silent skip しない.
    """
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", _FAKE_WH)
    from tasks.task_keyword_watch_crawl import _load_config
    cfg = _load_config()
    wh = (cfg.get("discord") or {}).get("webhook_url") or ""
    assert wh.strip(), "subprocess _load_config が env webhook を注入していない (HIGH-1 silent skip 残存)"
