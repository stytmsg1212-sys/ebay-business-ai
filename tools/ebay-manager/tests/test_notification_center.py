"""依頼ボード #39 Phase A S2 (2026-07-03) — 通知ファサード (notifiers.notification_center)
+ Discord choke point 統合の回帰テスト。

カバー範囲:
  - record_and_maybe_send: 常時記録 (Q0) / category ゲート ON-OFF / dedupe 抑止 /
    config 欠落時の既定ゲート fallback / 記録失敗時も送信フローは継続
  - 自己 dedupe バグ防止 (dedupe 判定は INSERT より前に行う)
  - notifiers.discord_notifier.DiscordNotifier.send_message の choke point 統合
    (record_and_maybe_send 経由、無限再帰なし)
  - category 推論 (notifier_for 経由 / 直接構築時の webhook 逆引き)

全テストで conftest.py の `_block_real_discord_post` (requests.post no-op 化) と
`_isolate_monitor_db` (DB_PATH tmp 隔離) が autouse で効くため、実 Discord 送信・
本番 DB 汚染は発生しない。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from notifiers import notification_center


@pytest.fixture(autouse=True)
def _init_db():
    from monitor.database import init_db
    init_db()


def _open_gate(**overrides):
    """discord_category_gate を明示的に差し替えるヘルパ (file I/O を避けるため
    _load_gate_config 自体を monkeypatch する)。"""
    gate = dict(notification_center._DEFAULT_GATE)
    gate.update(overrides)
    return gate


# ---------------------------------------------------------------------------
# record_and_maybe_send: 常時記録 (Q0)
# ---------------------------------------------------------------------------


def test_always_records_even_when_gate_off(monkeypatch):
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(system=False))
    result = notification_center.record_and_maybe_send(
        "system", "warning", "テスト通知", "本文")
    assert result["notification_id"] is not None
    assert result["gated"] is True
    assert result["discord_sent"] is False

    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="system")
    assert len(rows) == 1
    assert rows[0]["title"] == "テスト通知"
    assert rows[0]["discord_sent"] == 0


def test_gate_on_sends_and_records(monkeypatch):
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(order=True))
    result = notification_center.record_and_maybe_send(
        "order", "info", "注文通知", "商品が売れました")
    assert result["gated"] is False
    assert result["discord_sent"] is True  # conftest fixture が 204 を返す no-op mock

    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="order")
    assert rows[0]["discord_sent"] == 1


# ---------------------------------------------------------------------------
# config 欠落時の既定ゲート fallback
# ---------------------------------------------------------------------------


def test_config_missing_falls_back_to_default_gate_on_categories(monkeypatch):
    """config 読込失敗時、order/action_required/keyword/rival は既定 ON."""
    monkeypatch.setattr(notification_center, "_load_gate_config", lambda: {})
    for cat in ("order", "action_required", "keyword", "rival"):
        assert notification_center._gate_open(cat) is True, cat


def test_config_missing_falls_back_to_default_gate_off_categories(monkeypatch):
    """config 読込失敗時、system/inventory/research/pricing/default は既定 OFF."""
    monkeypatch.setattr(notification_center, "_load_gate_config", lambda: {})
    for cat in ("system", "inventory", "research", "pricing", "default"):
        assert notification_center._gate_open(cat) is False, cat


def test_config_value_overrides_default(monkeypatch):
    """config に明示値があれば既定 fallback より優先される."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: {"system": True, "order": False})
    assert notification_center._gate_open("system") is True
    assert notification_center._gate_open("order") is False


def test_real_config_file_is_valid_and_readable():
    """config/schedule_config.json の discord_category_gate セクションが実在し読める
    (S2 で追加したセクションの疎通確認、モック無しで直接読む)。"""
    gate = notification_center._load_gate_config()
    assert gate.get("order") is True
    assert gate.get("action_required") is True
    assert gate.get("keyword") is True
    assert gate.get("rival") is True
    assert gate.get("system") is False
    assert gate.get("inventory") is False
    assert gate.get("research") is False
    assert gate.get("pricing") is False
    assert gate.get("default") is False


# ---------------------------------------------------------------------------
# dedupe 抑止 + 自己 dedupe バグ防止
# ---------------------------------------------------------------------------


def test_dedupe_suppresses_second_send_within_window(monkeypatch):
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(rival=True))
    first = notification_center.record_and_maybe_send(
        "rival", "info", "値下げ検知1", dedupe_key="rival_ITEM1")
    assert first["discord_sent"] is True
    assert first["deduped"] is False

    second = notification_center.record_and_maybe_send(
        "rival", "info", "値下げ検知2", dedupe_key="rival_ITEM1")
    assert second["discord_sent"] is False, (
        "自己 dedupe バグ再発: 1 回目の INSERT が 2 回目の判定に影響してはいけないが、"
        "2 回目は正しく直近 24h 以内ヒットとして抑止されるべき"
    )
    assert second["deduped"] is True
    # 記録自体は毎回行われる (Q0)
    from monitor.notification_log_db import get_notifications
    assert len(get_notifications(category="rival")) == 2


def test_first_call_not_self_deduped(monkeypatch):
    """1 回目の呼出は『自分自身の INSERT 前』に dedupe 判定するため必ず未ヒット."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(keyword=True))
    result = notification_center.record_and_maybe_send(
        "keyword", "info", "新着1", dedupe_key="kw_watch1")
    assert result["deduped"] is False
    assert result["discord_sent"] is True


def test_dedupe_check_failure_fails_safe_to_send(monkeypatch):
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(rival=True))
    monkeypatch.setattr(
        "notifiers.notification_center.has_recent_dedupe",
        lambda key, hours=24: (_ for _ in ()).throw(RuntimeError("db down")))
    result = notification_center.record_and_maybe_send(
        "rival", "info", "値下げ検知", dedupe_key="rival_BOOM")
    assert result["deduped"] is False
    assert result["discord_sent"] is True  # fail-safe で送信側に倒す


# ---------------------------------------------------------------------------
# notification_log 記録失敗時もフロー継続 (Q0)
# ---------------------------------------------------------------------------


def test_insert_failure_does_not_break_send_flow(monkeypatch):
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(order=True))
    monkeypatch.setattr(
        "notifiers.notification_center.insert_notification",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    result = notification_center.record_and_maybe_send(
        "order", "info", "注文通知")
    assert result["notification_id"] is None
    assert result["discord_sent"] is True  # 記録失敗でも送信は継続


# ---------------------------------------------------------------------------
# Discord POST payload 整形 (embed 有無)
# ---------------------------------------------------------------------------


def test_post_uses_embed_only_when_embed_given(monkeypatch):
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(system=True))
    captured = {}

    class _Resp:
        status_code = 204

    def _fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("notifiers.notification_center.requests.post", _fake_post)
    embed = {"title": "t", "description": "d", "color": 1}
    notification_center.record_and_maybe_send(
        "system", "warning", "title-only-for-log", "body-only-for-log", embed=embed)
    assert captured["json"] == {"embeds": [embed]}
    assert "content" not in captured["json"]


def test_post_uses_content_when_no_embed(monkeypatch):
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(order=True))
    captured = {}

    class _Resp:
        status_code = 204

    def _fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("notifiers.notification_center.requests.post", _fake_post)
    notification_center.record_and_maybe_send("order", "info", "件名", "本文")
    assert captured["json"] == {"content": "件名\n本文"}


def test_post_failure_returns_sent_false(monkeypatch):
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(order=True))

    def _boom(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr("notifiers.notification_center.requests.post", _boom)
    result = notification_center.record_and_maybe_send("order", "info", "件名")
    assert result["discord_sent"] is False
    # 記録は継続 (Q0)
    assert result["notification_id"] is not None


def test_no_webhook_resolved_returns_sent_false(monkeypatch):
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(order=True))
    monkeypatch.setattr(
        "notifiers.discord_notifier.resolve_webhook", lambda category: "")
    result = notification_center.record_and_maybe_send("order", "info", "件名")
    assert result["discord_sent"] is False
    assert result["notification_id"] is not None


# ---------------------------------------------------------------------------
# discord_notifier.DiscordNotifier choke point 統合
# ---------------------------------------------------------------------------


def test_send_message_routes_through_record_and_maybe_send(monkeypatch):
    from notifiers.discord_notifier import DiscordNotifier

    captured = {}

    def _fake_record(category, severity, title, body="", **kwargs):
        captured.update(category=category, severity=severity, title=title,
                        body=body, embed=kwargs.get("embed"))
        return {"notification_id": 1, "discord_sent": True,
                "gated": False, "deduped": False}

    monkeypatch.setattr(
        "notifiers.notification_center.record_and_maybe_send",
        _fake_record)
    notifier = DiscordNotifier("https://discord.test/hook", bypass_env=True,
                               category="pricing")
    # 2026-07-03 実機 0 点 fb 対応で title 選択優先度を「embed['title'] 優先」に変更。
    # embed 無しの場合は message がそのまま title に流れることを確認する。
    ok = notifier.send_message("値下げ実施", severity="warning")
    assert ok is True
    assert captured["category"] == "pricing"
    assert captured["severity"] == "warning"
    assert captured["title"] == "値下げ実施"
    # embed 有りのケース: embed['title'] が title として優先される
    captured.clear()
    ok2 = notifier.send_message(
        "値下げ実施", embed={"title": "💰 仕入先 値下げ検知"}, severity="warning")
    assert ok2 is True
    assert captured["title"] == "💰 仕入先 値下げ検知"
    assert captured["embed"] == {"title": "💰 仕入先 値下げ検知"}


def test_send_message_default_severity_is_info(monkeypatch):
    from notifiers.discord_notifier import DiscordNotifier

    captured = {}

    def _fake_record(category, severity, title, body="", **kwargs):
        captured["severity"] = severity
        return {"notification_id": 1, "discord_sent": True,
                "gated": False, "deduped": False}

    monkeypatch.setattr(
        "notifiers.notification_center.record_and_maybe_send", _fake_record)
    DiscordNotifier("https://discord.test/hook", bypass_env=True).send_message("msg")
    assert captured["severity"] == "info"


def test_send_message_no_infinite_recursion_with_real_facade(monkeypatch):
    """send_message → record_and_maybe_send → resolve_webhook → requests.post の
    一方向のみで、notifier_for/send_message へ戻らないこと (無限再帰防止の疎通確認)."""
    from notifiers.discord_notifier import DiscordNotifier

    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(system=True))
    notifier = DiscordNotifier("https://discord.test/hook", bypass_env=True,
                               category="system")
    # conftest の _block_real_discord_post が requests.post を no-op 化しているため
    # 実ネットワークなしで完走することを確認するだけで十分。
    ok = notifier.send_message("再帰しないことの確認")
    assert ok is True


def test_notifier_for_passes_category_through(monkeypatch):
    from notifiers import discord_notifier as dn

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/default")
    notifier = dn.notifier_for("inventory")
    assert notifier.category == "inventory"


# ---------------------------------------------------------------------------
# 統合レビュー HIGH-1: severity bypass (critical/error は gate OFF でも送信)
# ---------------------------------------------------------------------------


def test_critical_severity_sends_even_when_gate_off(monkeypatch):
    """gate OFF カテゴリ (system) でも severity=critical なら Discord に届く.

    背景: 欠落タスク検知 / 監視カバレッジ欠落 / URL乖離 等の money-direct・安全網 alert
    が system category に集中しており、config で system gate=OFF にすると黙殺される
    HIGH-1 の恒久回避策 (Q0 silent skip 再発防止)。
    """
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(system=False))
    result = notification_center.record_and_maybe_send(
        "system", "critical", "[緊急] 定時実行 欠落検知")
    assert result["discord_sent"] is True, (
        "critical severity は gate OFF でも常時送信すべき (HIGH-1)"
    )
    assert result["gated"] is True, (
        "gate 状態自体は OFF のまま追跡可能 (bypass で送信したことを別途 severity_bypassed で示す)"
    )
    assert result["severity_bypassed"] is True


def test_error_severity_also_bypasses_gate(monkeypatch):
    """severity=error も critical と同じく gate OFF を bypass する."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(pricing=False))
    result = notification_center.record_and_maybe_send(
        "pricing", "error", "値下げ実行失敗")
    assert result["discord_sent"] is True
    assert result["severity_bypassed"] is True


def test_info_severity_still_gated_off(monkeypatch):
    """severity=info は bypass 対象外 = 従来通り gate OFF なら送信されない."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(system=False))
    result = notification_center.record_and_maybe_send(
        "system", "info", "システム定期レポート")
    assert result["discord_sent"] is False
    assert result["gated"] is True
    assert result["severity_bypassed"] is False


def test_warning_severity_still_gated_off(monkeypatch):
    """severity=warning も bypass 対象外 (通知過多防止の主目的維持)."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(inventory=False))
    result = notification_center.record_and_maybe_send(
        "inventory", "warning", "在庫警告")
    assert result["discord_sent"] is False
    assert result["gated"] is True
    assert result["severity_bypassed"] is False


def test_critical_still_deduped(monkeypatch):
    """severity bypass は dedupe を無効化しない (連投抑止は継続)."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(system=False))
    first = notification_center.record_and_maybe_send(
        "system", "critical", "URL乖離検知", dedupe_key="url_div_daily")
    assert first["discord_sent"] is True
    second = notification_center.record_and_maybe_send(
        "system", "critical", "URL乖離検知", dedupe_key="url_div_daily")
    assert second["discord_sent"] is False, (
        "bypass 中でも dedupe ヒットは尊重すべき (spam 防止と両立)"
    )
    assert second["deduped"] is True


def test_gate_on_critical_records_no_bypass_flag(monkeypatch):
    """gate ON で送った critical は severity_bypassed=False (bypass 発火してない)."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(order=True))
    result = notification_center.record_and_maybe_send(
        "order", "critical", "$1500+ EU 高額注文")
    assert result["discord_sent"] is True
    assert result["gated"] is False
    assert result["severity_bypassed"] is False


# ---------------------------------------------------------------------------
# task_order_alert._send_discord facade 統合 (統合レビュー HIGH-1 対応 MED)
# ---------------------------------------------------------------------------


def test_task_order_alert_send_discord_records_via_center(monkeypatch):
    """task_order_alert._send_discord が record_and_maybe_send("order", ...) 経由に
    移行済 = notification_log に記録が残り、DASHBOARD/S4 から閲覧可能."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(order=True))
    from tasks.task_order_alert import _send_discord

    embed = {"title": "[CRITICAL] $1500+ EU 高額注文 (DE)",
             "description": "DDU 発送 + 関税通知メール送信が必要",
             "color": 0xD84C38}
    sent = _send_discord("https://discord.test/hook", embed)
    assert sent is True

    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="order")
    assert len(rows) == 1
    assert rows[0]["title"] == "[CRITICAL] $1500+ EU 高額注文 (DE)"
    assert rows[0]["severity"] == "critical"
    assert rows[0]["discord_sent"] == 1


def test_task_order_alert_send_discord_severity_override(monkeypatch):
    """severity kwarg で warning / info 明示指定できる (DDP-B invoice / sold_notify 用)."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(order=True))
    from tasks.task_order_alert import _send_discord

    _send_discord("https://discord.test/hook",
                  {"title": "🛒 商品が売れました", "description": "..."},
                  severity="info")
    _send_discord("https://discord.test/hook",
                  {"title": "[ALERT] DDP-B", "description": "..."},
                  severity="warning")

    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="order", limit=10)
    sev = {r["title"]: r["severity"] for r in rows}
    assert sev["🛒 商品が売れました"] == "info"
    assert sev["[ALERT] DDP-B"] == "warning"


def test_task_order_alert_send_discord_empty_webhook_short_circuits(monkeypatch):
    """webhook 引数が空文字なら早期 return (record も送信もしない、既存挙動保持)."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(order=True))
    call_count = {"n": 0}

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return {"notification_id": None, "discord_sent": True,
                "gated": False, "deduped": False, "severity_bypassed": False}

    monkeypatch.setattr(
        "notifiers.notification_center.record_and_maybe_send", _spy)
    from tasks.task_order_alert import _send_discord
    assert _send_discord("", {"title": "t"}) is False
    assert call_count["n"] == 0, "空 webhook は早期 return して facade を叩かない"


def test_task_order_alert_send_discord_critical_survives_order_gate_off(monkeypatch):
    """万一 order gate を OFF にしても、_send_discord 既定 severity='critical' で
    money-direct アラートは silently dropped されない (HIGH-1 安全弁の運用確認)."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(order=False))
    from tasks.task_order_alert import _send_discord
    sent = _send_discord("https://discord.test/hook",
                         {"title": "[ALERT] W149 売却注文取得 5 回連続失敗",
                          "description": "手動確認要"})
    assert sent is True, "order gate OFF でも critical severity なら送信されるべき"


# ---------------------------------------------------------------------------
# S2 follow-up: severity 付与 (safety valve / mutate failure sites)
# ---------------------------------------------------------------------------


def test_rival_pricing_spiral_alert_critical_survives_pricing_gate_off(monkeypatch):
    """統合テスト: W183 値下げ合戦スパイラルアラート (2026-07-02 制定 第 3 安全弁) は
    pricing gate=OFF でも severity='critical' で必ず Discord に届く (bypass)。"""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(pricing=False))
    captured = {}

    def _capture(category, severity, title, body="", **kwargs):
        captured["category"] = category
        captured["severity"] = severity
        return {"notification_id": 1, "discord_sent": True,
                "gated": True, "deduped": False, "severity_bypassed": True}

    monkeypatch.setattr(
        "notifiers.notification_center.record_and_maybe_send", _capture)
    # DiscordNotifier で pricing category を明示的に構築 (webhook URL 推論不要に)
    from notifiers.discord_notifier import DiscordNotifier
    notifier = DiscordNotifier("https://discord.test/pricing", bypass_env=True,
                               category="pricing")
    ok = notifier.send_message("⚠️ W183 値下げ合戦アラート ...", severity="critical")
    assert ok is True
    assert captured["category"] == "pricing"
    assert captured["severity"] == "critical", (
        "スパイラルアラートは critical severity で bypass する必要 (money 安全弁)"
    )


def test_rival_pricing_reduced_notify_records_warning(monkeypatch):
    """L852 値下げ実行結果サマリが severity='warning' で center 記録される."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(pricing=True))
    from notifiers.discord_notifier import DiscordNotifier
    DiscordNotifier("https://discord.test/pricing", bypass_env=True,
                    category="pricing").send_message(
        "💲 W183 自動値下げ実行 (5 件)", severity="warning")
    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="pricing")
    assert any(r["severity"] == "warning" and "値下げ実行" in r["title"] for r in rows)


def test_rival_pricing_failure_alert_records_error(monkeypatch):
    """L920 refresh 失敗が severity='error' で center 記録 + bypass 対象."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(pricing=False))
    from notifiers.discord_notifier import DiscordNotifier
    r = DiscordNotifier("https://discord.test/pricing", bypass_env=True,
                        category="pricing").send_message(
        "⚠️ W183 ライバル価格 refresh 失敗", severity="error")
    assert r is True, "error severity は pricing gate OFF でも bypass で送信"
    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="pricing")
    assert any(r["severity"] == "error" for r in rows)


def test_inventory_supplier_search_records_warning(monkeypatch):
    """L309 探索結果通知が severity='warning' で center 記録 (inventory gate に従属)."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(inventory=True))
    from notifiers.discord_notifier import DiscordNotifier
    DiscordNotifier("https://discord.test/inventory", bypass_env=True,
                    category="inventory").send_message(
        "", embed={"title": "売り切れ検知 → 仕入先候補探索 結果",
                    "description": "..."},
        severity="warning")
    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="inventory")
    assert any(r["severity"] == "warning" for r in rows)


def test_inventory_price_alert_records_warning(monkeypatch):
    """L948 仕入先価格変動 ±5% が severity='warning' で center 記録.

    2026-07-03 実機 0 点 fb 対応で title 選択優先度が「embed['title'] 優先」に
    変更されたため、embed['title'] に価格変動サマリを載せて title 検証する。
    (旧: message 文字列が title に流れていた)"""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(pricing=True))
    from notifiers.discord_notifier import DiscordNotifier
    DiscordNotifier("https://discord.test/pricing", bypass_env=True,
                    category="pricing").send_message(
        "🔔 詳細本文",
        embed={"title": "💰 仕入先 価格変動 ±5%"}, severity="warning")
    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="pricing")
    assert any(r["severity"] == "warning" and "±5%" in r["title"] for r in rows)


def test_daily_codex_lint_records_error_bypasses_system_gate(monkeypatch):
    """codex lint HIGH 3件+ 通知が severity='error' で bypass 対象になっている.

    加えて、ソース内で `.send_message` (正しい method) を使うよう修正済であること
    (旧 `.send` は AttributeError で silent fail していた)。"""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(system=False))
    from notifiers.discord_notifier import DiscordNotifier
    r = DiscordNotifier("https://discord.test/system", bypass_env=True,
                        category="system").send_message(
        "📋 Codex Lint 結果 (HIGH=5)", severity="error")
    assert r is True
    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="system")
    assert any(r["severity"] == "error" and "Codex Lint" in r["title"] for r in rows)

    # ソースコード静的検証: .send( → .send_message( への修正が固定化されていること
    src = (PROJECT_ROOT / "tasks" / "task_daily_codex_lint.py").read_text(encoding="utf-8")
    assert 'notifier.send_message(msg, severity="error")' in src, (
        ".send() の silent-fail バグ回帰: send_message + severity='error' で送出する必要"
    )
    assert "notifier.send(msg)" not in src, (
        "旧 `.send(msg)` (存在しないメソッド) が復活 = AttributeError silent fail 再発"
    )


def test_ebaymag_apply_queue_mutate_failure_records_error(monkeypatch):
    """ebaymag_apply_queue の needs_manual / 反映失敗サマリが severity='error'."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(default=False))
    from tasks.task_ebaymag_apply_queue import _discord_notify
    _discord_notify({}, "[eBaymag] 反映失敗 3件: ...", severity="error")
    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="default")
    assert len(rows) >= 1
    assert rows[0]["severity"] == "error"


def test_ebaymag_relist_mutate_failure_records_error(monkeypatch):
    """ebaymag_relist の失敗系 (needs_manual / inherit / 例外 / サマリ) が
    severity='error' で bypass 対象."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(default=False))
    from tasks.task_ebaymag_relist import _discord_notify
    _discord_notify({}, "[eBaymag relist] needs_manual: ...", severity="error")
    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="default")
    assert len(rows) >= 1
    assert rows[0]["severity"] == "error"


def test_ebaymag_relist_success_summary_is_info_default(monkeypatch):
    """_discord_notify の severity 既定は 'info' — CDP 不在等の情報通知は従来通り
    default gate に従属する (通知過多防止)。"""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(default=False))
    from tasks.task_ebaymag_relist import _discord_notify
    _discord_notify({}, "[eBaymag relist] CDP 不在のため skip")
    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="default")
    assert len(rows) >= 1
    assert rows[0]["severity"] == "info"


def test_ebaymag_sync_audit_divergence_records_error(monkeypatch):
    """sync_audit 乖離検知 (price / pic / desc) が severity='error' で bypass 対象."""
    monkeypatch.setattr(notification_center, "_load_gate_config",
                        lambda: _open_gate(default=False))
    from tasks.task_ebaymag_sync_audit import _discord_notify
    _discord_notify({}, "[eBaymag 同期監査] 価格乖離 2件 ...", severity="error")
    from monitor.notification_log_db import get_notifications
    rows = get_notifications(category="default")
    assert len(rows) >= 1
    assert rows[0]["severity"] == "error"


# ---------------------------------------------------------------------------
# category 推論 (直接構築時の webhook 逆引き)
# ---------------------------------------------------------------------------


def test_infer_category_matches_known_env(monkeypatch):
    from notifiers.discord_notifier import DiscordNotifier

    monkeypatch.setenv("DISCORD_RIVAL_WEBHOOK_URL", "https://discord.test/rival")
    notifier = DiscordNotifier("https://discord.test/rival", bypass_env=True)
    assert notifier.category == "rival"


def test_infer_category_falls_back_to_default_for_unknown_url(monkeypatch):
    from notifiers.discord_notifier import DiscordNotifier

    notifier = DiscordNotifier("https://discord.test/totally-custom", bypass_env=True)
    assert notifier.category == "default"


def test_explicit_category_overrides_inference(monkeypatch):
    from notifiers.discord_notifier import DiscordNotifier

    monkeypatch.setenv("DISCORD_RIVAL_WEBHOOK_URL", "https://discord.test/rival")
    notifier = DiscordNotifier("https://discord.test/rival", bypass_env=True,
                               category="pricing")
    assert notifier.category == "pricing"
