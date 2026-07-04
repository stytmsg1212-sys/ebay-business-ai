"""依頼ボード #46: AI 店長 (W301 rival_classifier, Shadow 稼働中) の日次サマリ通知.

「動きが見えない」への対応。tasks/task_rival_classify.py の
_send_daily_summary / _fetch_review_backlog を、既存 notification_center choke
point (record_and_maybe_send) 経由の呼び出し引数で検証する (Discord 実送信は
mock、test_keyword_watch_notification_content_2026_07_03.py と同じ流儀)。

カバレッジ:
  - 分類 0 件でも「実行済」を明示したサマリが 1 通発行される (Q0 可視化)
  - real/noise/review 判定の日次カウント + review 滞留 (累計) が正しく反映される
  - kill switch (enabled=false) では発行されない (task 自体が動いていないため)
  - category='rival' (既存 discord_category_gate、新カテゴリを増やさない) +
    dedupe_key に当日日付が入る
  - 通知送信失敗が run_rival_classify 自体の成否 (result) を壊さない
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from monitor.database import get_conn
from monitor.rival_classifier import AIJudgeResult


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _seed_listing_and_discovery(
    *,
    ebay_item_id: str = "OUR1",
    competitor_item_id: str = "COMP1",
    our_title: str = "Sony WH-1000XM5 Wireless Headphones Black",
    competitor_title: str = "ソニー WH-1000XM5 ワイヤレスヘッドホン ブラック 美品",
    our_price: float = 200.0,
    competitor_price: float = 190.0,
) -> int:
    """1 listing + 1 discovery を新規 seed する (ebay_item_id は毎回ユニーク想定)."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, current_price, ebay_condition_id, condition_rank)
               VALUES (?, 'stock01', ?, ?, '3000', 'B')""",
            (ebay_item_id, our_title, our_price),
        )
        conn.execute(
            """INSERT INTO listing_rival_discoveries
               (ebay_item_id, competitor_seller, competitor_item_id,
                competitor_title, competitor_price_usd, search_keyword, status)
               VALUES (?, 'jp_seller_1', ?, ?, ?, 'sony headphones', 'new')""",
            (ebay_item_id, competitor_item_id, competitor_title, competitor_price),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _seed_discovery_for_existing_listing(
    *, ebay_item_id: str, competitor_item_id: str,
    competitor_title: str, competitor_price: float = 190.0,
) -> int:
    """既存 listing に対する追加 discovery のみを seed する (listing は重複 INSERT しない)."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO listing_rival_discoveries
               (ebay_item_id, competitor_seller, competitor_item_id,
                competitor_title, competitor_price_usd, search_keyword, status)
               VALUES (?, 'jp_seller_1', ?, ?, ?, 'sony headphones', 'new')""",
            (ebay_item_id, competitor_item_id, competitor_title, competitor_price),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ────────────────────────────────────────────────────────────────
# 0 件でも「実行済」を明示して 1 通発行 (Q0)
# ────────────────────────────────────────────────────────────────

def test_zero_discoveries_sends_summary_with_executed_marker(tmp_db):
    from tasks.task_rival_classify import run_rival_classify

    called = {}

    def fake_record(*args, **kwargs):
        called.update(kwargs)
        called["_args"] = args
        return {"notification_id": 1, "discord_sent": True, "gated": False,
                "deduped": False, "severity_bypassed": False}

    with patch("notifiers.notification_center.record_and_maybe_send", fake_record):
        result = run_rival_classify({})

    assert result["success"] is True
    assert called, "record_and_maybe_send が呼ばれていない (0 件でもサマリ必須)"
    args = called["_args"]
    assert args[0] == "rival"
    assert args[1] == "info"
    assert "AI店長" in args[2]
    body = args[3] if len(args) > 3 else called.get("body", "")
    assert "実行済" in body
    assert "real 0 件" in body
    assert "noise 0 件" in body


# ────────────────────────────────────────────────────────────────
# 正常分類 (real/noise) → カウント反映 + review 滞留 (累計)
# ────────────────────────────────────────────────────────────────

def test_summary_reflects_real_noise_counts_and_review_backlog(tmp_db, monkeypatch):
    _seed_listing_and_discovery(ebay_item_id="OUR1", competitor_item_id="COMP1")
    _seed_discovery_for_existing_listing(
        ebay_item_id="OUR1", competitor_item_id="COMP2",
        competitor_title="全く関係ない掃除機のパーツセット",
    )
    # 3 件目は review のまま残す (confidence 中間で AI へ)
    _seed_discovery_for_existing_listing(
        ebay_item_id="OUR1", competitor_item_id="COMP3",
        competitor_title="ソニー WH-1000XM5 ワイヤレスヘッドホン ブラック 美品",
    )

    def _fake_judge_rival(signals, model="unused"):
        if signals["competitor_item_id"] == "COMP1":
            return AIJudgeResult(
                same_product=True, variant_risk="none", condition="USED",
                confidence=0.95, reason="同一商品", ai_model="claude-haiku-4-5-20251001",
                route="ai",
            )
        return AIJudgeResult(
            same_product=True, variant_risk="unknown", condition="USED",
            confidence=0.7, reason="やや不確か", ai_model="claude-haiku-4-5-20251001",
            route="ai",
        )

    import monitor.rival_classifier as rc
    monkeypatch.setattr(rc, "judge_rival", _fake_judge_rival)

    from tasks.task_rival_classify import run_rival_classify

    called = {}

    def fake_record(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        return {"discord_sent": True}

    with patch("notifiers.notification_center.record_and_maybe_send", fake_record):
        result = run_rival_classify({})

    assert result["real"] == 1
    assert result["noise"] == 1
    assert result["review"] == 1

    body = called["args"][3]
    assert "real 1 件" in body
    assert "noise 1 件" in body
    # review 滞留 (累計) = status='new' で残っている件数 (COMP3 のみ)
    assert "review 滞留 1 件" in body
    assert "累計" in body
    assert "Shadow" in body


# ────────────────────────────────────────────────────────────────
# kill switch 無効時は日次サマリも発行しない (task 自体が実行されていない)
# ────────────────────────────────────────────────────────────────

def test_kill_switch_disabled_does_not_send_summary(tmp_db):
    from tasks.task_rival_classify import run_rival_classify

    with patch("notifiers.notification_center.record_and_maybe_send") as mock_send:
        config = {"tasks_enabled": {"rival_classify": {"enabled": False}}}
        result = run_rival_classify(config)

    assert result["success"] is True
    mock_send.assert_not_called()


# ────────────────────────────────────────────────────────────────
# category='rival' (既存ゲート流用) + dedupe_key に当日日付
# ────────────────────────────────────────────────────────────────

def test_summary_uses_rival_category_and_date_dedupe_key(tmp_db):
    from datetime import date
    from tasks.task_rival_classify import run_rival_classify

    called = {}

    def fake_record(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        return {"discord_sent": True}

    with patch("notifiers.notification_center.record_and_maybe_send", fake_record):
        run_rival_classify({})

    assert called["args"][0] == "rival"
    dedupe_key = called["kwargs"].get("dedupe_key", "")
    assert date.today().isoformat() in dedupe_key


# ────────────────────────────────────────────────────────────────
# 通知送信失敗が run_rival_classify の成否を壊さない
# ────────────────────────────────────────────────────────────────

def test_summary_send_failure_does_not_break_task_result(tmp_db):
    from tasks.task_rival_classify import run_rival_classify

    with patch("notifiers.notification_center.record_and_maybe_send",
               side_effect=RuntimeError("discord down")):
        result = run_rival_classify({})

    assert result["success"] is True
    assert "0 discoveries" in result["message"]
