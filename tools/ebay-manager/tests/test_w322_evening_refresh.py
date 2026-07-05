"""W322 夕方 refresh (19:30 JST): monitor/evening_digest.py + tasks/task_evening_refresh.py.

設計書: .company/engineering/docs/2026-07-04-daily-workflow-design.md §4/§6

カバレッジ:
  - get_evening_price_candidates: 値下げ/売れた/在庫増の3シグナルを直近2スナップショット
    比較で検出、比較対象なし (snapshot 1件のみ) は対象外
  - format_digest_body: 0件時「本日は対応候補なし」
  - run_evening_refresh: kill switch / 既存 task 関数の再利用 / digest 通知
  - TASK_SCHEDULE に evening_refresh が hours=[19] で登録されている (Q0 新規 task 4要件)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from monitor.database import get_conn, insert_competitor_snapshot


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _seed_listing(ebay_item_id: str, title: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, current_price, ebay_condition_id, condition_rank)
               VALUES (?, 'stock01', ?, 100.0, '3000', 'B')""",
            (ebay_item_id, title),
        )


# ────────────────────────────────────────────────────────────────
# get_evening_price_candidates
# ────────────────────────────────────────────────────────────────

def test_single_snapshot_has_no_comparison_target(tmp_db):
    """比較対象 (前回 snapshot) が無い競合は候補に入らない (Q0: 誤検知回避)."""
    from monitor.evening_digest import get_evening_price_candidates

    insert_competitor_snapshot(
        competitor_item_id="COMP1", our_item_id="OUR1",
        quantity_sold=5, quantity_available=3, price_usd=100.0,
    )
    assert get_evening_price_candidates() == []


def test_price_drop_detected(tmp_db):
    from monitor.evening_digest import get_evening_price_candidates

    _seed_listing("OUR1", "Sony WH-1000XM5 Wireless Headphones")
    insert_competitor_snapshot(
        competitor_item_id="COMP1", our_item_id="OUR1",
        quantity_sold=5, quantity_available=3, price_usd=100.0,
    )
    insert_competitor_snapshot(
        competitor_item_id="COMP1", our_item_id="OUR1",
        quantity_sold=5, quantity_available=3, price_usd=89.99,
    )
    cands = get_evening_price_candidates()
    assert len(cands) == 1
    c = cands[0]
    assert c["price_drop"] == pytest.approx(10.01, abs=0.01)
    assert c["sold_delta"] == 0
    assert c["avail_delta"] == 0
    assert "値下げ" in c["line"]
    assert "Sony WH-1000XM5" in c["line"]


def test_sold_and_restock_detected(tmp_db):
    from monitor.evening_digest import get_evening_price_candidates

    _seed_listing("OUR_ITEM_5678", "Random Product Title")
    insert_competitor_snapshot(
        competitor_item_id="COMP2", our_item_id="OUR_ITEM_5678",
        quantity_sold=2, quantity_available=1, price_usd=50.0,
    )
    insert_competitor_snapshot(
        competitor_item_id="COMP2", our_item_id="OUR_ITEM_5678",
        quantity_sold=5, quantity_available=4, price_usd=50.0,
    )
    cands = get_evening_price_candidates()
    assert len(cands) == 1
    c = cands[0]
    assert c["sold_delta"] == 3
    assert c["avail_delta"] == 3
    assert c["price_drop"] == 0
    assert "3個 売れた" in c["line"]
    assert "在庫 +3" in c["line"]
    assert "(5678)" in c["line"]  # ebay_item_id 末尾4桁で区別 (CLAUDE.md 商品の呼称)


def test_no_signal_excluded(tmp_db):
    """価格・売上・在庫すべて変化なしの競合は候補に入らない."""
    from monitor.evening_digest import get_evening_price_candidates

    insert_competitor_snapshot(
        competitor_item_id="COMP3", our_item_id="OUR1",
        quantity_sold=5, quantity_available=3, price_usd=100.0,
    )
    insert_competitor_snapshot(
        competitor_item_id="COMP3", our_item_id="OUR1",
        quantity_sold=5, quantity_available=3, price_usd=100.0,
    )
    assert get_evening_price_candidates() == []


def test_price_increase_not_treated_as_drop(tmp_db):
    """値上げは候補に含めない (price_drop 判定は下落のみ)."""
    from monitor.evening_digest import get_evening_price_candidates

    insert_competitor_snapshot(
        competitor_item_id="COMP4", our_item_id="OUR1",
        quantity_sold=5, quantity_available=3, price_usd=100.0,
    )
    insert_competitor_snapshot(
        competitor_item_id="COMP4", our_item_id="OUR1",
        quantity_sold=5, quantity_available=3, price_usd=120.0,
    )
    assert get_evening_price_candidates() == []


# ────────────────────────────────────────────────────────────────
# format_digest_body
# ────────────────────────────────────────────────────────────────

def test_format_digest_body_empty():
    from monitor.evening_digest import format_digest_body
    assert format_digest_body([]) == "本日は対応候補なし"


def test_format_digest_body_joins_lines():
    from monitor.evening_digest import format_digest_body
    cands = [{"line": "• A — 値下げ"}, {"line": "• B — 3個 売れた"}]
    body = format_digest_body(cands)
    assert body == "• A — 値下げ\n• B — 3個 売れた"


# ────────────────────────────────────────────────────────────────
# run_evening_refresh
# ────────────────────────────────────────────────────────────────

def test_kill_switch_disabled_skips(tmp_db):
    from tasks.task_evening_refresh import run_evening_refresh
    config = {"tasks_enabled": {"evening_refresh": {"enabled": False}}}
    result = run_evening_refresh(config)
    assert result["success"] is True
    assert "enabled=false" in result["message"]


def test_reuses_existing_task_functions_and_sends_digest(tmp_db):
    """既存 run_competitor_snapshot / run_rival_classify をそのまま呼び出し、
    その後 digest 抽出 + Discord 通知が行われること."""
    from tasks.task_evening_refresh import run_evening_refresh

    calls = {"snapshot": 0, "classify": 0}

    def _fake_snapshot(config):
        calls["snapshot"] += 1
        return {"success": True, "message": "snapshot ok"}

    def _fake_classify(config):
        calls["classify"] += 1
        return {"success": True, "message": "classify ok"}

    notified = {}

    def _fake_record(*args, **kwargs):
        notified["args"] = args
        notified["kwargs"] = kwargs
        return {"discord_sent": True}

    with patch("tasks.task_competitor_snapshot.run_competitor_snapshot", _fake_snapshot), \
         patch("tasks.task_rival_classify.run_rival_classify", _fake_classify), \
         patch("notifiers.notification_center.record_and_maybe_send", _fake_record):
        result = run_evening_refresh({})

    assert calls["snapshot"] == 1
    assert calls["classify"] == 1
    assert result["success"] is True
    assert result["digest_candidates"] == 0

    assert notified["args"][0] == "rival"
    assert notified["args"][1] == "info"
    assert "今夜の価格対応候補" in notified["args"][2]
    body = notified["args"][3]
    assert body == "本日は対応候補なし"
    from datetime import date
    assert date.today().isoformat() in notified["kwargs"].get("dedupe_key", "")


def test_partial_failure_still_sends_digest_and_reports_false(tmp_db):
    """snapshot/classify いずれか失敗でも digest 送信は継続し、success=False で報告 (Q0)."""
    from tasks.task_evening_refresh import run_evening_refresh

    def _fake_snapshot(config):
        return {"success": False, "message": "eBay 認証情報未設定"}

    def _fake_classify(config):
        return {"success": True, "message": "classify ok"}

    with patch("tasks.task_competitor_snapshot.run_competitor_snapshot", _fake_snapshot), \
         patch("tasks.task_rival_classify.run_rival_classify", _fake_classify), \
         patch("notifiers.notification_center.record_and_maybe_send") as mock_send:
        result = run_evening_refresh({})

    assert result["success"] is False
    mock_send.assert_called_once()


def test_digest_notification_failure_does_not_break_result(tmp_db):
    from tasks.task_evening_refresh import run_evening_refresh

    def _fake_snapshot(config):
        return {"success": True, "message": "ok"}

    def _fake_classify(config):
        return {"success": True, "message": "ok"}

    with patch("tasks.task_competitor_snapshot.run_competitor_snapshot", _fake_snapshot), \
         patch("tasks.task_rival_classify.run_rival_classify", _fake_classify), \
         patch("notifiers.notification_center.record_and_maybe_send",
               side_effect=RuntimeError("discord down")):
        result = run_evening_refresh({})

    assert result["success"] is True


# ────────────────────────────────────────────────────────────────
# Q0 新規 scheduled task 4 要件: TASK_SCHEDULE 登録確認
# ────────────────────────────────────────────────────────────────

def test_task_schedule_registered():
    from monitor.task_execution_log import TASK_SCHEDULE_BY_KEY
    entry = TASK_SCHEDULE_BY_KEY.get("evening_refresh")
    assert entry is not None
    assert entry["hours"] == [19]
