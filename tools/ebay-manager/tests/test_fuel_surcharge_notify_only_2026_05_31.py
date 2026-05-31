"""通知専用化 (2026-05-31): 燃料サーチャージ task が settings.json を書かないこと。

出典: 旧 task は FedEx/DHL 小売ページを scrape し差分<5pt で settings.json に自動反映して
いたが、利益計算は CPaSS 便を price するため「小売値 ≠ CPaSS値」で正しい値を誤上書きする
money-direct リスクがあった。user 選択で通知専用化 (週次 CPaSS 手動更新リマインダー)。

本テストは:
- run_fuel_surcharge_check が settings.json を 1 バイトも書き換えないこと (最重要)
- 成功扱い (success) / changed=False の後方互換契約
- webhook 有無での success / reminder_sent の意味づけ
- webhook 未設定 / 送信失敗が logger.error で痕跡を残すこと (Q0 silent skip 防止、code-reviewer HIGH-1)
- 鮮度に応じた文面エスカレーション (>30日 rotating_light / >14日 warning / fresh は無し)
- リマインダー文面が CPaSS 手動更新 (小売値を出さない) を案内すること
を永続固定する (将来 task を触っても自動書込が復活しないこと)。

通知経路は DiscordNotifier (.env 優先解決) に統一済。本テストは T.DiscordNotifier を
_FakeNotifier に差し替え、解決済 webhook / 送信成否を制御してネットワークを叩かない。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pytest

import tasks.task_fuel_surcharge_check as T


class _FakeNotifier:
    """T.DiscordNotifier 差し替え。resolved_url で .env 優先解決をシミュレートする。

    - resolved_url: DiscordNotifier が最終的に持つ webhook (空 = どこにも未設定)
    - send_ok: send_message の戻り値
    - last_init_url: run が DiscordNotifier(webhook_url=...) に渡した値 (解決前)
    - last_message: 送信メッセージ
    """
    resolved_url = "https://discord.example/webhook"
    send_ok = True
    last_init_url: object = None
    last_message: object = None

    def __init__(self, webhook_url: str = ""):
        _FakeNotifier.last_init_url = webhook_url
        self.webhook_url = _FakeNotifier.resolved_url

    def send_message(self, message: str) -> bool:
        _FakeNotifier.last_message = message
        return _FakeNotifier.send_ok


@pytest.fixture(autouse=True)
def _patch_notifier(monkeypatch):
    _FakeNotifier.resolved_url = "https://discord.example/webhook"
    _FakeNotifier.send_ok = True
    _FakeNotifier.last_init_url = None
    _FakeNotifier.last_message = None
    monkeypatch.setattr(T, "DiscordNotifier", _FakeNotifier)


def _write_settings(tmp_path, *, fedex=49.5, dhl=47.75, last_updated="2026-05-31T20:01:00",
                    webhook=""):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "fuel_surcharge_fedex": fedex,
        "fuel_surcharge_dhl": dhl,
        "fuel_surcharge_last_updated": last_updated,
        "discord_webhook_url": webhook,
        "exchange_rate": 157,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ── 最重要: settings.json を一切書き換えない ──

def test_settings_file_byte_identical_after_run(tmp_path, monkeypatch):
    """run 後に settings.json が 1 バイトも変わらない (自動書込の完全撤去を固定)."""
    p = _write_settings(tmp_path)
    monkeypatch.setattr(T, "SETTINGS_FILE", p)
    before = p.read_bytes()
    T.run_fuel_surcharge_check({})
    assert p.read_bytes() == before, "settings.json が書き換えられた (通知専用化違反)"


def test_no_settings_write_even_when_webhook_present(tmp_path, monkeypatch):
    """webhook 有 + 送信成功でも settings は不変."""
    p = _write_settings(tmp_path, webhook="https://discord.example/abc")
    monkeypatch.setattr(T, "SETTINGS_FILE", p)
    before = p.read_bytes()
    res = T.run_fuel_surcharge_check({})
    assert p.read_bytes() == before
    assert res["changed"] is False


# ── 後方互換契約 (run_task が success を読む) ──

def test_success_true_without_webhook(tmp_path, monkeypatch):
    """webhook 未設定 (test/dev) は『送るものが無い』= 実行成功扱い (health 誤検知防止)."""
    _FakeNotifier.resolved_url = ""  # どこにも webhook 未設定
    p = _write_settings(tmp_path, webhook="")
    monkeypatch.setattr(T, "SETTINGS_FILE", p)
    res = T.run_fuel_surcharge_check({})
    assert res["success"] is True
    assert res["reminder_sent"] is False
    assert res["changed"] is False
    assert res["fedex_rate"] == 49.5
    assert res["dhl_rate"] == 47.75


def test_success_reflects_send_failure_when_webhook_present(tmp_path, monkeypatch):
    """webhook 有で送信失敗 (本番 Discord 障害) は success=False で health に拾わせる."""
    _FakeNotifier.send_ok = False
    p = _write_settings(tmp_path, webhook="https://discord.example/abc")
    monkeypatch.setattr(T, "SETTINGS_FILE", p)
    res = T.run_fuel_surcharge_check({})
    assert res["success"] is False
    assert res["reminder_sent"] is False


def test_success_true_when_send_ok(tmp_path, monkeypatch):
    p = _write_settings(tmp_path, webhook="https://discord.example/abc")
    monkeypatch.setattr(T, "SETTINGS_FILE", p)
    res = T.run_fuel_surcharge_check({})
    assert res["success"] is True
    assert res["reminder_sent"] is True


def test_config_webhook_takes_precedence(tmp_path, monkeypatch):
    """config['discord']['webhook_url'] が settings の空 webhook より優先して _notify に渡る."""
    p = _write_settings(tmp_path, webhook="")
    monkeypatch.setattr(T, "SETTINGS_FILE", p)
    res = T.run_fuel_surcharge_check({"discord": {"webhook_url": "https://cfg.example/x"}})
    assert _FakeNotifier.last_init_url == "https://cfg.example/x"
    assert res["success"] is True


# ── Q0 silent skip 防止 (code-reviewer HIGH-1): webhook 空 / 送信失敗で痕跡を残す ──

def test_notify_empty_webhook_logs_error(caplog):
    """webhook がどこにも無い時は (False, False) を返し ERROR で痕跡を残す (info で埋もれない)."""
    _FakeNotifier.resolved_url = ""
    with caplog.at_level(logging.WARNING, logger="tasks.task_fuel_surcharge_check"):
        has_webhook, sent = T._notify("", "msg")
    assert has_webhook is False and sent is False
    assert any(r.levelno >= logging.WARNING for r in caplog.records), \
        "webhook 空 = user 不達なのに痕跡が info 以下 (silent skip)"


def test_notify_send_failure_logs_error(caplog):
    """送信失敗時も (True, False) を返し ERROR で痕跡を残す (R-11 user 不達)."""
    _FakeNotifier.send_ok = False
    with caplog.at_level(logging.WARNING, logger="tasks.task_fuel_surcharge_check"):
        has_webhook, sent = T._notify("https://x/y", "msg")
    assert has_webhook is True and sent is False
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ── 文面エスカレーション ──

def _message_for(tmp_path, monkeypatch, *, days):
    last = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    p = _write_settings(tmp_path, last_updated=last, webhook="https://x/y")
    monkeypatch.setattr(T, "SETTINGS_FILE", p)
    T.run_fuel_surcharge_check({})
    return _FakeNotifier.last_message


def test_message_fresh_no_escalation(tmp_path, monkeypatch):
    msg = _message_for(tmp_path, monkeypatch, days=0)
    assert ":rotating_light:" not in msg
    assert ":warning:" not in msg


def test_message_over_14_days_warning(tmp_path, monkeypatch):
    msg = _message_for(tmp_path, monkeypatch, days=20)
    assert ":warning:" in msg
    assert ":rotating_light:" not in msg


def test_message_over_30_days_rotating_light(tmp_path, monkeypatch):
    msg = _message_for(tmp_path, monkeypatch, days=40)
    assert ":rotating_light:" in msg


def test_message_guides_cpass_not_retail(tmp_path, monkeypatch):
    """リマインダーは CPaSS 手動更新 + 全体設定タブを案内し、小売値を出さない."""
    msg = _message_for(tmp_path, monkeypatch, days=3)
    assert "CPaSS" in msg
    assert "全体設定" in msg
    # 小売 scrape の名残 (fedex.com / mydhl URL) を文面に出さない
    assert "fedex.com" not in msg
    assert "mydhl" not in msg


def test_unparseable_last_updated_no_crash(tmp_path, monkeypatch):
    """last_updated が壊れていても days_ago=None で安全にリマインダーを出す."""
    p = _write_settings(tmp_path, last_updated="不明", webhook="https://x/y")
    monkeypatch.setattr(T, "SETTINGS_FILE", p)
    res = T.run_fuel_surcharge_check({})
    assert res["days_since_update"] is None
    assert res["success"] is True
