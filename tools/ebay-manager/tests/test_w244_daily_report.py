#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W244: 日次 Discord レポートの data-driven 化テスト。

旧 _format_task_status はハードコード 11 キー dict で
 - 廃止済み task ('research'/'news') の幽霊行が毎回「スキップ」表示
 - 新規 task (W139/W148/W153 以降) が永遠にレポートに載らない
 - results キー 'email' と registry キー 'email_pickup' の不一致
を抱えていた。本テストは data-driven 版の振る舞いを固定する。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notifiers.discord_notifier import DiscordNotifier


@pytest.fixture()
def notifier():
    # webhook 未設定でも __init__ は通る (送信はしない)
    return DiscordNotifier(webhook_url="https://discord.test/hook", bypass_env=True)


class TestFormatTaskStatus:
    def test_empty_results(self, notifier):
        assert notifier._format_task_status({}) == "(この batch で実行されたタスクなし)"

    def test_known_key_uses_registry_display_name(self, notifier):
        out = notifier._format_task_status({"ebay_sync": {"success": True}})
        assert "eBay連携同期" in out
        assert "✅ 完了" in out

    def test_unknown_key_falls_back_to_key(self, notifier):
        out = notifier._format_task_status({"some_future_task": {"success": True}})
        assert "some_future_task" in out

    def test_failure_marked(self, notifier):
        out = notifier._format_task_status({"ebay_sync": {"success": False}})
        assert "❌ エラー" in out

    def test_no_ghost_rows(self, notifier):
        """旧実装の幽霊キー ('research'/'news'/'email') が results に無ければ出ない。"""
        out = notifier._format_task_status({"ebay_sync": {"success": True}})
        assert "research" not in out
        assert "スキップ" not in out  # skip 行自体を出さない (可視化は health check 側)

    def test_email_pickup_key_resolves(self, notifier):
        """W244 で統一した results キー 'email_pickup' が registry 表示名で出る。"""
        out = notifier._format_task_status({"email_pickup": {"success": True}})
        assert "メール取得" in out

    def test_rival_seller_sweep_key_resolves(self, notifier):
        """W244 で結線した rival_seller_sweep が registry 表示名で出る。"""
        out = notifier._format_task_status({"rival_seller_sweep": {"success": True}})
        assert "ライバルセラー" in out


class _FakeDateTime:
    """datetime.now() を固定するスタブ (notifiers.discord_notifier.datetime に patch)."""
    fixed = datetime(2026, 6, 10, 12, 0, 0)

    @classmethod
    def now(cls):
        return cls.fixed

    @classmethod
    def set(cls, dt):
        cls.fixed = dt


class TestNextExecutionTime:
    """実 schedule_config.json (times=[2,11,15,18,22], minutes={2:30}) を読む前提。"""

    def _patch_now(self, monkeypatch, dt):
        import notifiers.discord_notifier as dn
        _FakeDateTime.set(dt)
        monkeypatch.setattr(dn, "datetime", _FakeDateTime)

    def test_midday_points_to_15(self, notifier, monkeypatch):
        self._patch_now(monkeypatch, datetime(2026, 6, 10, 12, 0))
        assert notifier._next_execution_time() == "今日 15:00"

    def test_late_night_points_to_tomorrow_0230(self, notifier, monkeypatch):
        self._patch_now(monkeypatch, datetime(2026, 6, 10, 23, 0))
        assert notifier._next_execution_time() == "明日 02:30"

    def test_just_before_0230(self, notifier, monkeypatch):
        self._patch_now(monkeypatch, datetime(2026, 6, 10, 2, 15))
        assert notifier._next_execution_time() == "今日 02:30"

    def test_never_returns_stale_17(self, notifier, monkeypatch):
        """旧ハードコード [5,11,17,22] の 17:00 が出ないこと (16:00 時点 → 18:00)。"""
        self._patch_now(monkeypatch, datetime(2026, 6, 10, 16, 0))
        assert notifier._next_execution_time() == "今日 18:00"


class TestRegistryConsistency:
    def test_order_alert_check_registered_as_interval(self):
        from monitor.task_execution_log import TASK_SCHEDULE_BY_KEY
        entry = TASK_SCHEDULE_BY_KEY.get("order_alert_check")
        assert entry is not None, "W244: order_alert_check が TASK_SCHEDULE 未登録"
        assert entry.get("kind") == "interval"
        assert entry.get("interval_minutes") == 5  # v81: 30→5 (売却同期高頻度化)

    def test_rival_seller_sweep_config_wired(self):
        """schedule_config.json に rival_seller_sweep が enabled で存在 (dispatch 結線の前提)。"""
        import json
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        entry = cfg["tasks_enabled"].get("rival_seller_sweep")
        assert entry is not None
        assert entry["enabled"] is True
        assert entry["execution_times"] == [2]
