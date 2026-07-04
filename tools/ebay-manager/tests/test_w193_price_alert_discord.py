# -*- coding: utf-8 -*-
"""W193 (2026-05-30): 仕入先 価格変動 Discord 通知.

user 確定要件:
  - 基準 = 最初に記録した価格 (baseline)。前回値比較ではない。
  - 基準から ±5% を超えて変動したら 1 回通知。圏内 (normal/restock) → 圏外 (surge/drop)
    へ遷移した瞬間のみ。圏内復帰まで再通知しない。値上がり/値下がり両方。
  - 閾値は dashboard と統一して ±5%。

検証スコープ:
  - _update_price_and_evaluate_alert の戻り値 (old_state, new_state, baseline)
  - ±5% 境界 (4.9%=通知なし / 5.0%=通知 / 5.1%=通知)
  - 基準確立 (baseline None→normal) は通知対象外
  - _fetch_and_store_prices の遷移収集 → Discord バッチ送信
  - Q0: Discord 送信失敗でも DB state は更新される (sticky 再送防止)
  - _send_price_alert_discord の 1 retry / 失敗時 False
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert_mi(sku, source_url, last_status="unknown", baseline=None,
               current=None, alert_state=None, title=None, ebay_item_id=None):
    from monitor.database import get_conn
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO monitored_items
               (sku, source_url, title, ebay_item_id, last_status, is_active,
                baseline_price_jpy, current_price_jpy, price_alert_state)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (sku, source_url, title, ebay_item_id, last_status,
             baseline, current, alert_state),
        )
        return cur.lastrowid


class _FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


# =============================================================================
# _update_price_and_evaluate_alert の戻り値
# =============================================================================

def test_eval_returns_tuple_on_surge(tmp_db):
    """surge 遷移時 (old='normal', new='surge', baseline) を返す."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    item_id = _insert_mi("ebayAM_t", "https://amzn/t",
                         baseline=10000, current=10000, alert_state="normal")
    result = _update_price_and_evaluate_alert(item_id, 10600)  # +6%
    assert result == ("normal", "surge", 10000)


def test_eval_returns_tuple_on_drop(tmp_db):
    """drop 遷移時 (old='normal', new='drop', baseline) を返す."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    item_id = _insert_mi("ebayRT_t", "https://item.rakuten/t",
                         baseline=10000, current=10000, alert_state="normal")
    result = _update_price_and_evaluate_alert(item_id, 9400)  # -6%
    assert result == ("normal", "drop", 10000)


def test_eval_baseline_establishment_returns_normal(tmp_db):
    """基準確立 (baseline None) は new_state='normal' を返す (通知対象外になる)."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    item_id = _insert_mi("ebayYS_t", "https://shopping.yahoo.co.jp/t")
    result = _update_price_and_evaluate_alert(item_id, 5000)
    assert result is not None
    old, new, baseline = result
    assert new == "normal"
    assert baseline == 5000


def test_eval_restock_returns_restock(tmp_db):
    """restock state は保持され new_state='restock' を返す."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    item_id = _insert_mi("ebayAM_t", "https://amzn/t",
                         baseline=10000, current=10000, alert_state="restock")
    result = _update_price_and_evaluate_alert(item_id, 10600)  # +6% でも restock 保持
    assert result == ("restock", "restock", 10000)


def test_eval_missing_item_returns_none(tmp_db):
    """存在しない item_id は None を返す (例外なし)."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    assert _update_price_and_evaluate_alert(999999, 1000) is None


# =============================================================================
# ±5% 境界
# =============================================================================

@pytest.mark.parametrize("price,expected_new", [
    (10490, "normal"),  # +4.9% (圏内)
    (10500, "surge"),   # +5.0% ちょうど (圏外)
    (10510, "surge"),   # +5.1% (圏外)
    (9510, "normal"),   # -4.9% (圏内)
    (9500, "drop"),     # -5.0% ちょうど (圏外)
    (9490, "drop"),     # -5.1% (圏外)
])
def test_five_percent_boundary(tmp_db, price, expected_new):
    """±5% 境界: 4.9%=圏内 / 5.0%=圏外 / 5.1%=圏外 (値上げ・値下げ両方)."""
    from tasks.task_inventory_check import _update_price_and_evaluate_alert
    item_id = _insert_mi("ebayAM_t", "https://amzn/t",
                         baseline=10000, current=10000, alert_state="normal")
    _, new, _ = _update_price_and_evaluate_alert(item_id, price)
    assert new == expected_new


# =============================================================================
# _fetch_and_store_prices: 遷移収集 → Discord バッチ送信
# =============================================================================

def test_fetch_collects_crossing_and_sends_discord(tmp_db, monkeypatch):
    """+6% で surge 遷移 → crossings に 1 件収集 → _send_price_alert_discord が呼ばれる."""
    import httpx
    import tasks.task_inventory_check as tic

    item_id = _insert_mi("ebayAM_x", "https://www.amazon.co.jp/dp/B0X",
                         baseline=10000, current=10000, alert_state="normal",
                         title="Test Camera")
    # Amazon 価格 HTML を返す fake httpx.get (+6% = 10600)
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _FakeResp('<span class="a-offscreen">￥10,600</span>'),
    )
    captured = {}

    def _fake_send(webhook, crossings):
        captured["webhook"] = webhook
        captured["crossings"] = crossings
        return True

    monkeypatch.setattr(tic, "_send_price_alert_discord", _fake_send)

    results = [{"id": item_id, "sku": "ebayAM_x"}]
    config = {"discord": {"webhook_url": "https://discord.test/webhook"}}
    n = tic._fetch_and_store_prices(results, config)

    assert n == 1
    assert "crossings" in captured, "遷移があるのに Discord 送信が呼ばれていない"
    assert len(captured["crossings"]) == 1
    c0 = captured["crossings"][0]
    assert c0["state"] == "surge"
    assert c0["current"] == 10600
    assert c0["baseline"] == 10000
    assert c0["title"] == "Test Camera"


def test_fetch_baseline_establishment_no_discord(tmp_db, monkeypatch):
    """基準未確立 (baseline None) の初回取得は遷移ではない → Discord 送信なし."""
    import httpx
    import tasks.task_inventory_check as tic

    item_id = _insert_mi("ebayAM_new", "https://www.amazon.co.jp/dp/B0Y")  # baseline None
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _FakeResp('<span class="a-offscreen">￥5,000</span>'),
    )
    sent = {"called": False}
    monkeypatch.setattr(
        tic, "_send_price_alert_discord",
        lambda w, c: sent.__setitem__("called", True) or True,
    )
    tic._fetch_and_store_prices([{"id": item_id, "sku": "ebayAM_new"}], {})
    assert sent["called"] is False, "基準確立で通知してはいけない"


def test_fetch_discord_failure_still_updates_db_state(tmp_db, monkeypatch):
    """Q0: Discord 送信が失敗しても DB の price_alert_state は surge に更新される."""
    import httpx
    import tasks.task_inventory_check as tic
    from monitor.database import get_conn

    item_id = _insert_mi("ebayRT_x", "https://item.rakuten.co.jp/s/x",
                         baseline=10000, current=10000, alert_state="normal")
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _FakeResp('<meta itemprop="price" content="11000">'),  # +10%
    )
    monkeypatch.setattr(tic, "_send_price_alert_discord", lambda w, c: False)  # 送信失敗

    tic._fetch_and_store_prices(
        [{"id": item_id, "sku": "ebayRT_x"}],
        {"discord": {"webhook_url": "https://discord.test/wh"}},
    )
    with get_conn() as c:
        row = c.execute(
            "SELECT price_alert_state, current_price_jpy FROM monitored_items WHERE id=?",
            (item_id,),
        ).fetchone()
    assert row["price_alert_state"] == "surge", "送信失敗で state が更新されていない (sticky 再送リスク)"
    assert row["current_price_jpy"] == 11000


def test_fetch_url_domain_target_no_prefix(tmp_db, monkeypatch):
    """W183 手動 URL 仕入先 (prefix が ebayXX_ でない) でも URL ドメインで価格対象になる."""
    import httpx
    import tasks.task_inventory_check as tic
    from monitor.database import get_conn

    # SKU は手動 URL の元 SKU (prefix 非対象) だが source_url が Yahoo ドメイン
    item_id = _insert_mi("stock:99", "https://store.shopping.yahoo.co.jp/s/x.html",
                         baseline=10000, current=10000, alert_state="normal")
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _FakeResp('<meta property="product:price:amount" content="10800"/>'),
    )
    monkeypatch.setattr(tic, "_send_price_alert_discord", lambda w, c: True)
    n = tic._fetch_and_store_prices([{"id": item_id, "sku": "stock:99"}], {})
    assert n == 1, "URL ドメイン経路で価格対象にならなかった"
    with get_conn() as c:
        row = c.execute(
            "SELECT price_alert_state FROM monitored_items WHERE id=?", (item_id,)
        ).fetchone()
    assert row["price_alert_state"] == "surge"  # +8%


def test_fetch_anti_bot_page_skipped_no_baseline(tmp_db, monkeypatch):
    """anti-bot ページ (Amazon Robot Check) は価格抽出させず skip.

    CAPTCHA HTML に紛れた price-like 文字列を baseline に誤確立すると、
    以降の正常値で永続 surge/drop 誤通知 (baseline 再記録しない) = 金銭直結 (HIGH-1).
    """
    import httpx
    import tasks.task_inventory_check as tic
    from monitor.database import get_conn

    item_id = _insert_mi("ebayAM_bot", "https://www.amazon.co.jp/dp/B0BOT")  # baseline None
    # Robot Check ページに related items の価格が紛れている状況を模す
    captcha_html = (
        '<title>Robot Check</title>'
        '<form action="/errors/validateCaptcha"></form>'
        '<span class="a-offscreen">￥99,999</span>'
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(captcha_html))
    monkeypatch.setattr(tic, "_send_price_alert_discord", lambda w, c: True)

    n = tic._fetch_and_store_prices([{"id": item_id, "sku": "ebayAM_bot"}], {})
    assert n == 0, "anti-bot ページから価格を抽出してしまった"
    with get_conn() as c:
        row = c.execute(
            "SELECT baseline_price_jpy, price_alert_state FROM monitored_items WHERE id=?",
            (item_id,),
        ).fetchone()
    assert row["baseline_price_jpy"] is None, "CAPTCHA 価格で baseline を誤確立した"


# =============================================================================
# _send_price_alert_discord: 1 retry / 成否
# =============================================================================

class _FakeNotifier:
    def __init__(self, webhook, results):
        self._results = list(results)
        self.calls = 0

    def send_message(self, content, embed=None, *, severity="info"):
        # severity kwarg: 本番 DiscordNotifier.send_message(..., severity=...)
        # の signature に追従 (notification_log 記録用、fake では不使用)。
        self.calls += 1
        return self._results.pop(0) if self._results else False


def _patch_notifier(monkeypatch, results):
    import notifiers.discord_notifier as dn
    holder = {}

    def _factory(webhook, *, bypass_env=False):
        # W284(#22): 価格アラート送信が DiscordNotifier(..., bypass_env=True) を渡すため
        # 実 DiscordNotifier.__init__ と同じ signature を mock も受ける (値は fake では不使用)。
        n = _FakeNotifier(webhook, results)
        holder["notifier"] = n
        return n

    monkeypatch.setattr(dn, "DiscordNotifier", _factory)
    return holder


def test_send_discord_success_single_call(tmp_db, monkeypatch):
    """1 回目成功なら retry なし、True."""
    import tasks.task_inventory_check as tic
    holder = _patch_notifier(monkeypatch, [True])
    crossings = [{"title": "X", "sku": "ebayAM_a", "url": "u",
                  "state": "surge", "current": 10600, "baseline": 10000}]
    assert tic._send_price_alert_discord("https://wh", crossings) is True
    assert holder["notifier"].calls == 1


def test_send_discord_retry_then_success(tmp_db, monkeypatch):
    """1 回目失敗 → backoff → 2 回目成功で True (機会 lost 防止)."""
    import time
    import tasks.task_inventory_check as tic
    monkeypatch.setattr(time, "sleep", lambda s: None)
    holder = _patch_notifier(monkeypatch, [False, True])
    crossings = [{"title": "X", "sku": "ebayAM_a", "url": "u",
                  "state": "drop", "current": 9000, "baseline": 10000}]
    assert tic._send_price_alert_discord("https://wh", crossings) is True
    assert holder["notifier"].calls == 2


def test_send_discord_both_fail_returns_false(tmp_db, monkeypatch):
    """2 回とも失敗で False (呼び側が DB state を保つので sticky 再送なし)."""
    import time
    import tasks.task_inventory_check as tic
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _patch_notifier(monkeypatch, [False, False])
    crossings = [{"title": "X", "sku": "ebayAM_a", "url": "u",
                  "state": "surge", "current": 10600, "baseline": 10000}]
    assert tic._send_price_alert_discord("https://wh", crossings) is False


def test_send_discord_empty_webhook_returns_false():
    """webhook 空なら False (送信せず)."""
    from tasks.task_inventory_check import _send_price_alert_discord
    crossings = [{"title": "X", "sku": "ebayAM_a", "url": "u",
                  "state": "surge", "current": 10600, "baseline": 10000}]
    assert _send_price_alert_discord("", crossings) is False
