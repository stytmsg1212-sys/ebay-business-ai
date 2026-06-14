"""依頼ボード#22 (2026-06-14): Discord 通知カテゴリ別ルーティング。

resolve_webhook(category) は専用 env があればそれを、なければ DISCORD_WEBHOOK_URL に
fallback する。これにより通知種別ごとに別チャンネルへ振り分けつつ、未作成チャンネルは
既定 ch に届いて silent drop しない (Q0)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT = "https://discord.com/api/webhooks/DEFAULT/xxx"
INV = "https://discord.com/api/webhooks/INVENTORY/yyy"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    # 全カテゴリ env をクリアしてからテストごとに必要分だけ設定
    from notifiers.discord_notifier import WEBHOOK_CATEGORY_ENV
    for env_name in WEBHOOK_CATEGORY_ENV.values():
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)


def test_category_specific_env_used_when_set(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DEFAULT)
    monkeypatch.setenv("DISCORD_INVENTORY_WEBHOOK_URL", INV)
    from notifiers.discord_notifier import resolve_webhook
    assert resolve_webhook("inventory") == INV
    # 他カテゴリ未設定 → 既定にfallback
    assert resolve_webhook("research") == DEFAULT


def test_fallback_to_default_when_category_env_unset(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DEFAULT)
    from notifiers.discord_notifier import resolve_webhook
    assert resolve_webhook("inventory") == DEFAULT
    assert resolve_webhook("order") == DEFAULT
    assert resolve_webhook("system") == DEFAULT


def test_unknown_category_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DEFAULT)
    from notifiers.discord_notifier import resolve_webhook
    assert resolve_webhook("nonexistent") == DEFAULT
    assert resolve_webhook() == DEFAULT  # default 引数


def test_notifier_for_uses_resolved_webhook(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DEFAULT)
    monkeypatch.setenv("DISCORD_INVENTORY_WEBHOOK_URL", INV)
    from notifiers.discord_notifier import notifier_for
    # bypass_env=True で resolve 結果を直接使う = INV になる
    assert notifier_for("inventory").webhook_url == INV
    assert notifier_for("research").webhook_url == DEFAULT


def test_all_categories_have_distinct_env_names():
    from notifiers.discord_notifier import WEBHOOK_CATEGORY_ENV
    names = list(WEBHOOK_CATEGORY_ENV.values())
    assert len(names) == len(set(names)), "env 名が重複している"
    assert all(n.startswith("DISCORD_") and n.endswith("_WEBHOOK_URL") for n in names)
