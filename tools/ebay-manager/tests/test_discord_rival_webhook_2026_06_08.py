"""W153 ライバル検出 専用 Discord チャンネル分離の回帰テスト (2026-06-08).

出典: user 報告「商品管理でライバル登録しているが Discord に通知が来ない」
→ 調査で W153 通知が既定 #bot通知 チャンネルに埋もれていた + UI 経路は通知なし
が判明。専用チャンネル (DISCORD_RIVAL_WEBHOOK_URL) に分離。

W207 (DISCORD_KEYWORD_WEBHOOK_URL) と同一パターン。inject の分岐増加 (env_rv)
と _resolve_rival_webhook の専用優先/既定 fallback を固定化する (Q0: 通知先消失防止)。
"""
import pytest

from notifiers.discord_notifier import inject_webhook_into_config
from tasks.task_rival_detection import _resolve_rival_webhook

_RIVAL_WH = "https://discord.com/api/webhooks/rival/xxx"
_DEFAULT_WH = "https://discord.com/api/webhooks/default/yyy"


def test_inject_rival_webhook_set(monkeypatch):
    """env 設定時 config['discord']['rival_webhook_url'] に注入される。"""
    monkeypatch.setenv("DISCORD_RIVAL_WEBHOOK_URL", _RIVAL_WH)
    cfg = inject_webhook_into_config({})
    assert cfg["discord"].get("rival_webhook_url") == _RIVAL_WH


def test_inject_rival_webhook_unset_no_key(monkeypatch):
    """全 env 未設定なら rival key を作らない (caller fallback に委ねる)。"""
    monkeypatch.delenv("DISCORD_RIVAL_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_KEYWORD_WEBHOOK_URL", raising=False)
    cfg = inject_webhook_into_config({"discord": {}})
    assert not (cfg.get("discord") or {}).get("rival_webhook_url")


def test_inject_rival_idempotent_existing_respected(monkeypatch):
    """既存 rival_webhook_url があれば env で上書きしない (idempotent)。"""
    monkeypatch.setenv("DISCORD_RIVAL_WEBHOOK_URL", "https://discord.com/api/webhooks/env/z")
    cfg = inject_webhook_into_config({"discord": {"rival_webhook_url": _RIVAL_WH}})
    assert cfg["discord"]["rival_webhook_url"] == _RIVAL_WH


def test_inject_rival_only_env_set(monkeypatch):
    """env_rv のみ設定 (既定/keyword なし) でも rival が注入される (分岐カバー)。"""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_KEYWORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_RIVAL_WEBHOOK_URL", _RIVAL_WH)
    cfg = inject_webhook_into_config({"discord": {}})
    assert cfg["discord"].get("rival_webhook_url") == _RIVAL_WH


def test_resolve_prefers_dedicated():
    """専用 webhook が既定より優先される。"""
    cfg = {"discord": {"webhook_url": _DEFAULT_WH, "rival_webhook_url": _RIVAL_WH}}
    assert _resolve_rival_webhook(cfg) == _RIVAL_WH


def test_resolve_fallback_to_default():
    """専用未設定なら既定 webhook へ fallback (Q0: 通知先消失しない)。"""
    cfg = {"discord": {"webhook_url": _DEFAULT_WH}}
    assert _resolve_rival_webhook(cfg) == _DEFAULT_WH


def test_resolve_empty_when_neither():
    """専用も既定も無ければ空文字 (送信側 if not webhook: return で safe skip)。"""
    assert _resolve_rival_webhook({"discord": {}}) == ""
    assert _resolve_rival_webhook({}) == ""
