"""W229 Phase 2: task_research_harvest テスト (mock のみ、CDP/本番DB アクセス禁止).

設計書: .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md §10 Phase 2 DoD

テスト対象:
  - enabled=false skip 経路 (痕跡が残ること)
  - クォータ不足時の縮退/中断経路
  - 正常経路: harvest mock 5 商品 → detail mock → gate → insert 呼出検証
  - dedup (run 内 + DB 既存 gate_decision 確定)
  - harvest 失敗時に success=False + Discord 通知が呼ばれること
  - anti-bot 停止経路

全テストは tmp DB + monkeypatch を使用する。本番 DB・CDP・実機は一切触らない。
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

# ── テスト対象モジュールを import ──────────────────────────────────────
from tasks.task_research_harvest import (
    _normalize_keyword,
    run_research_harvest,
)


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_config(enabled: bool = True, max_items: int = 50, max_pages: int = 2) -> dict:
    """テスト用 config を生成する."""
    return {
        "discord": {"webhook_url": "http://fake-webhook.example.com"},
        "tasks_enabled": {
            "research_harvest": {
                "enabled": enabled,
                "seed_queries": [
                    {
                        "label": "test",
                        "query": "(-abcd) (-Card)",
                        "category_id": 0,
                        "min_price": 100,
                    }
                ],
                "max_items_per_run": max_items,
                "max_pages": max_pages,
            }
        },
    }


def _make_harvested_product(title: str = "Sony WH-1000XM5") -> MagicMock:
    """HarvestedProduct のモックを作る."""
    p = MagicMock()
    p.title = title
    p.avg_sold_price_usd = 248.0
    p.total_sold_count = 12
    p.date_last_sold = datetime.date(2026, 6, 10)
    p.research_url = "https://www.ebay.com/sh/research?foo=bar"
    p.image_url = None
    p.avg_shipping_cost_usd = None
    return p


def _make_harvest_result(products=None, success: bool = True, error: Optional[str] = None):
    """HarvestResult のモックを作る."""
    r = MagicMock()
    r.success = success
    r.error = error
    r.products = products or []
    r.pages_loaded = 1 if success else 0
    return r


def _make_gate_data(
    sold_90d: int = 5,
    has_active: bool = False,
    listing_start: Optional[str] = None,
    sold_1_2yr: int = 0,
    avg_price: float = 248.0,
    success: bool = True,
    error: Optional[str] = None,
    navigates_used: int = 1,
    worldwide_active_count: int = -1,
    worldwide_sold_90d: int = -1,
) -> MagicMock:
    """ProductGateData のモックを作る."""
    gd = MagicMock()
    gd.sold_90d = sold_90d
    gd.has_active_listing = has_active
    gd.listing_start_date = listing_start
    gd.sold_1_2yr = sold_1_2yr
    gd.avg_sold_price_usd = avg_price
    gd.success = success
    gd.error = error
    # H-2: navigates_used は実消費 navigate 回数 (Q6 skip=1, フル=3)
    gd.navigates_used = navigates_used
    # 依頼ボード#23 (2026-06-15): 全世界グラット除外シグナル (既定 -1=未取得)。
    # MagicMock の属性は JSON 非直列化のため、明示的に int を set する。
    gd.worldwide_active_count = worldwide_active_count
    gd.worldwide_sold_90d = worldwide_sold_90d
    return gd


# ---------------------------------------------------------------------------
# Test: enabled=false skip
# ---------------------------------------------------------------------------


class TestEnabledFalseSkip:
    """enabled=false 時に skip し、Q0 痕跡 (log + log_task_skip) が残ることを確認."""

    def test_skip_returns_success_true(self):
        """enabled=false なら success=True で早期返却 (skip = 正常終了)."""
        config = _make_config(enabled=False)
        with patch("tasks.task_research_harvest._check_cdp_available") as mock_cdp, \
             patch("tasks.task_research_harvest._send_discord") as mock_discord:
            result = run_research_harvest(config)

        assert result["success"] is True
        mock_cdp.assert_not_called()  # CDP まで到達しない

    def test_skip_does_not_send_discord(self):
        """enabled=false の skip は Discord 通知を送らない (MEDIUM-3 修正後)."""
        config = _make_config(enabled=False)
        with patch("tasks.task_research_harvest._send_discord") as mock_discord:
            result = run_research_harvest(config)

        # MEDIUM-3 修正: enabled=false パスから _send_discord が削除された
        assert mock_discord.call_count == 0
        assert result["success"] is True

    def test_skip_message_contains_enabled_false(self):
        """返却 message に enabled=false の旨が含まれる."""
        config = _make_config(enabled=False)
        with patch("tasks.task_research_harvest._send_discord"):
            result = run_research_harvest(config)
        assert "enabled=false" in result["message"].lower() or "skip" in result["message"].lower()

    def test_skip_logs_task_skip_with_batch_ctx(self):
        """enabled=false + batch context あり → log_task_skip で DB 痕跡が残る (Q0)."""
        config = _make_config(enabled=False)
        with patch("daily_scheduler._batch_ctx") as mock_ctx, \
             patch("monitor.task_execution_log.log_task_skip") as mock_skip:
            mock_ctx.get.side_effect = lambda k, d=None: {"id": "b1", "hour": 3}.get(k, d)
            result = run_research_harvest(config)

        assert result["success"] is True
        mock_skip.assert_called_once()
        assert mock_skip.call_args.kwargs["task_key"] == "research_harvest"
        assert mock_skip.call_args.kwargs["skip_kind"] == "skip_disabled"
        assert mock_skip.call_args.kwargs["reason"] == "disabled_by_config"


# ---------------------------------------------------------------------------
# Test: CDP 不在
# ---------------------------------------------------------------------------


class TestCdpUnavailable:
    """CDP が起動していない場合は failure."""

    def test_cdp_unavailable_returns_failure(self):
        """CDP 不在なら success=False."""
        config = _make_config()
        with patch("tasks.task_research_harvest._check_cdp_available", return_value=False), \
             patch("tasks.task_research_harvest._send_discord") as mock_discord:
            result = run_research_harvest(config)

        assert result["success"] is False
        assert mock_discord.call_count >= 1


# ---------------------------------------------------------------------------
# Test: クォータ不足
# ---------------------------------------------------------------------------


class TestQuotaInsufficient:
    """クォータ上限到達時の縮退・中断."""

    def test_quota_exhausted_returns_success_skip(self):
        """クォータ上限到達 (remaining=0) → success=True で skip."""
        config = _make_config()
        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=250), \
             patch("tasks.task_research_harvest._send_discord") as mock_discord:
            result = run_research_harvest(config)

        assert result["success"] is True
        assert mock_discord.call_count >= 1
        # harvest_product_list が呼ばれないこと
        # (quota 0 なので CDP には一切触れない)

    def test_quota_low_reduces_max_items(self):
        """クォータ残量 < 20 のとき max_items が縮退される."""
        config = _make_config(max_items=50)
        products = [_make_harvested_product(f"Product {i}") for i in range(10)]
        harvest_result = _make_harvest_result(products=products)

        gate_data = _make_gate_data(sold_90d=5)
        insert_calls = []

        def fake_insert(title, **kwargs):
            insert_calls.append(title)
            return len(insert_calls)

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=235), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=gate_data), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", side_effect=fake_insert), \
             patch("tasks.task_research_harvest.save_gate_decision", return_value=True), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        # remaining=15 → effective_max_items=min(5, 50)=5
        assert len(insert_calls) <= 5


# ---------------------------------------------------------------------------
# Test: 正常経路
# ---------------------------------------------------------------------------


class TestNormalFlow:
    """正常経路: harvest → detail → gate → insert 呼出検証."""

    def test_normal_5_products_gate_passed(self):
        """5 商品が harvest され、全件 gate_passed (sold_90d=5) で DB に着地する."""
        config = _make_config(max_items=50)
        products = [_make_harvested_product(f"Product {i}") for i in range(5)]
        harvest_result = _make_harvest_result(products=products)
        gate_data = _make_gate_data(sold_90d=5)  # target_instock → gate_passed

        inserted_rc_ids = []

        def fake_insert(title, **kwargs):
            rc_id = len(inserted_rc_ids) + 1
            inserted_rc_ids.append(rc_id)
            return rc_id

        save_gate_calls = []

        def fake_save_gate(rc_id, decision, reason, inputs_dict, *, move_status):
            save_gate_calls.append({
                "rc_id": rc_id,
                "decision": decision,
                "move_status": move_status,
            })
            return True

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=gate_data), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", side_effect=fake_insert), \
             patch("tasks.task_research_harvest.save_gate_decision", side_effect=fake_save_gate), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        assert result["success"] is True
        assert result["gate_passed"] == 5
        assert result["gate_rejected"] == 0
        # save_gate_decision が 5 回呼ばれた
        assert len(save_gate_calls) == 5
        # 全て move_status=True
        for c in save_gate_calls:
            assert c["move_status"] is True

    def test_gate_rejected_for_no_demand_product(self):
        """sold_90d=0 / sold_1_2yr=0 / has_active=False → reject_no_demand → gate_rejected."""
        config = _make_config(max_items=50)
        products = [_make_harvested_product("Dead product")]
        harvest_result = _make_harvest_result(products=products)
        gate_data = _make_gate_data(
            sold_90d=0,
            has_active=False,
            sold_1_2yr=0,
        )

        saved = []

        def fake_save_gate(rc_id, decision, reason, inputs_dict, *, move_status):
            saved.append(decision)
            return True

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=gate_data), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", return_value=1), \
             patch("tasks.task_research_harvest.save_gate_decision", side_effect=fake_save_gate), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        assert result["gate_passed"] == 0
        assert result["gate_rejected"] == 1
        assert "reject_no_demand" in saved

    def test_harvest_pattern_fresh_24h_assigned(self):
        """fresh_24h パターン収穫の商品には harvest_pattern='fresh_24h' が渡される."""
        config = _make_config()
        prod = _make_harvested_product("Sony Product")
        harvest_result = _make_harvest_result(products=[prod])
        gate_data = _make_gate_data(sold_90d=5)
        insert_kwargs = {}

        def fake_insert(title, **kwargs):
            insert_kwargs.update(kwargs)
            return 1

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=gate_data), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", side_effect=fake_insert), \
             patch("tasks.task_research_harvest.save_gate_decision", return_value=True), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            run_research_harvest(config)

        # fresh_24h パターン由来なので harvest_pattern='fresh_24h'
        assert insert_kwargs.get("harvest_pattern") == "fresh_24h"


# ---------------------------------------------------------------------------
# Test: dedup
# ---------------------------------------------------------------------------


class TestDedup:
    """重複排除: run 内 + DB 既存 gate_decision 確定."""

    def test_run_internal_dedup(self):
        """同一タイトルが複数回収穫された場合、1 件のみ処理される.

        コードの動作: harvest_product_list は seed × 2 pattern で計 2 回呼ばれる。
        毎回同一 products=[prod1, prod2] を返すので fresh=2件, echo=2件, combined=4件。
        dedup で同一タイトルが排除され insert は 1 回だけ、skipped_dedup >= 1。
        """
        config = _make_config()
        # 同一タイトルを複数用意 (fresh + echo 両方から収穫のシナリオ)
        prod1 = _make_harvested_product("Sony WH-1000XM5")
        prod2 = _make_harvested_product("Sony WH-1000XM5")  # 重複
        harvest_result = _make_harvest_result(products=[prod1, prod2])
        gate_data = _make_gate_data(sold_90d=5)
        insert_calls = []

        def fake_insert(title, **kwargs):
            insert_calls.append(title)
            return len(insert_calls)

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=gate_data), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", side_effect=fake_insert), \
             patch("tasks.task_research_harvest.save_gate_decision", return_value=True), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        # 同一タイトルなので insert は 1 件のみ
        assert len(insert_calls) == 1
        # 残りはすべて dedup でスキップ (seed × 2 pattern で同一タイトルが複数 collected)
        assert result["skipped_dedup"] >= 1

    def test_db_existing_gate_decision_skip(self):
        """DB に gate_decision='reject_no_demand' 確定済の候補は skip される."""
        config = _make_config()
        prod = _make_harvested_product("Dead Product")
        harvest_result = _make_harvest_result(products=[prod])
        nk = _normalize_keyword("Dead Product")
        existing_map = {
            nk: {
                "rc_id": 99,
                "harvest_keyword": nk,
                "gate_decision": "reject_no_demand",
                "gate_reason": "過去も需要なし",
                "gate_inputs_json": "{}",
                "listing_start_date": None,
                "status": "gate_rejected",
            }
        }
        insert_calls = []

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", side_effect=lambda t, **kw: insert_calls.append(t) or len(insert_calls)), \
             patch("tasks.task_research_harvest.save_gate_decision", return_value=True), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value=existing_map), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        assert len(insert_calls) == 0
        assert result["skipped_dedup"] >= 1

    def test_needs_review_null_gate_decision_no_duplicate_insert(self):
        """同一キーワードの needs_review (gate_decision=NULL) 既存行がある状態で
        再 harvest しても重複 INSERT されないこと (MEDIUM-8 修正検証)."""
        config = _make_config()
        prod = _make_harvested_product("Needs Review Product")
        harvest_result = _make_harvest_result(products=[prod])
        nk = _normalize_keyword("Needs Review Product")
        # gate_decision=NULL の needs_review 行 (scrape 失敗後の残骸)
        existing_map = {
            nk: {
                "rc_id": 77,
                "harvest_keyword": nk,
                "gate_decision": None,  # NULL = needs_review
                "gate_reason": None,
                "gate_inputs_json": None,
                "listing_start_date": None,
                "status": "needs_review",
            }
        }
        insert_calls = []

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=_make_gate_data(sold_90d=5)), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", side_effect=lambda t, **kw: insert_calls.append(t) or len(insert_calls)), \
             patch("tasks.task_research_harvest.save_gate_decision", return_value=True), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value=existing_map), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        # needs_review (NULL gate_decision) 既存行があっても新規 INSERT は発生しない
        assert len(insert_calls) == 0, (
            f"MEDIUM-8: needs_review 既存行があるのに重複 INSERT が {len(insert_calls)} 件発生"
        )

    def test_skip_too_new_triggers_rejudgement(self):
        """DB に gate_decision='skip_too_new' 確定済の候補は再判定される (スキップされない)."""
        config = _make_config()
        prod = _make_harvested_product("New Product")
        harvest_result = _make_harvest_result(products=[prod])
        nk = _normalize_keyword("New Product")
        existing_map = {
            nk: {
                "rc_id": 42,
                "harvest_keyword": nk,
                "gate_decision": "skip_too_new",
                "gate_reason": "出品 30 日未満",
                "gate_inputs_json": "{}",
                "listing_start_date": "2026-05-20",
                "status": "gate_rejected",
            }
        }
        gate_data = _make_gate_data(sold_90d=5)  # 今回は通過
        update_calls = []

        def fake_update_status(rc_id, new_status, *, needs_review_reason=None):
            update_calls.append((rc_id, new_status))
            return True

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=gate_data), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", return_value=99), \
             patch("tasks.task_research_harvest.save_gate_decision", return_value=True), \
             patch("tasks.task_research_harvest.update_status", side_effect=fake_update_status), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value=existing_map), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        # insert_research_candidate は呼ばれず (既存行を再利用)
        # update_status が harvested → gate_passed の遷移で呼ばれる
        # seed × 2 pattern で "New Product" が 2 回収穫される。
        # 1 回目: skip_too_new → 再判定対象として to_process に追加
        # 2 回目: run-internal dedup で skipped_dedup=1 に計上
        assert result["skipped_dedup"] >= 1  # 2 回目の収穫は dedup スキップ (これは正常)
        # update_status の呼び出しで rc_id=42 が含まれる (1 回目は再判定処理済)
        rc_ids_called = [c[0] for c in update_calls]
        assert 42 in rc_ids_called


# ---------------------------------------------------------------------------
# Test: harvest 失敗
# ---------------------------------------------------------------------------


class TestHarvestFailure:
    """harvest_product_list 失敗時の挙動."""

    def test_harvest_failure_discord_notified(self):
        """harvest 失敗時に Discord 通知が呼ばれる."""
        config = _make_config()
        fail_result = _make_harvest_result(success=False, error="CDP error")

        discord_calls = []

        def fake_discord(cfg, message, severity="info"):
            discord_calls.append((message, severity))
            return True

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=fail_result), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord", side_effect=fake_discord):
            result = run_research_harvest(config)

        # harvest は 2 パターンで呼ばれ、両方失敗しても success=True (0 件正常終了) or
        # エラーが errors リストに追加されることを確認
        assert len(discord_calls) >= 1
        # 収穫 0 件で Discord 通知が出る
        summary_notified = any(
            "収穫" in msg or "0 件" in msg or "harvest" in msg.lower()
            for msg, _ in discord_calls
        )
        assert summary_notified

    def test_anti_bot_stops_immediately(self):
        """eBay error redirect (anti-bot) 検知時に即停止する."""
        config = _make_config()
        fail_result = _make_harvest_result(
            success=False,
            error="eBay error redirect: https://www.ebay.com/error/...",
        )
        discord_calls = []

        def fake_discord(cfg, message, severity="info"):
            discord_calls.append((message, severity))
            return True

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=fail_result), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord", side_effect=fake_discord):
            result = run_research_harvest(config)

        # anti-bot = 即停止 → success=False
        assert result["success"] is False
        # error severity の Discord 通知
        error_notifs = [c for c in discord_calls if c[1] == "error"]
        assert len(error_notifs) >= 1

    def test_consecutive_scrape_failures_stop_batch(self):
        """scrape_product_detail の連続失敗 (5 件) でバッチが停止する."""
        config = _make_config(max_items=50)
        # 10 商品を収穫して、全て scrape 失敗
        products = [_make_harvested_product(f"Product {i}") for i in range(10)]
        harvest_result = _make_harvest_result(products=products)
        fail_gate = _make_gate_data(success=False, error="scrape error")

        discord_calls = []

        def fake_discord(cfg, message, severity="info"):
            discord_calls.append((message, severity))
            return True

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=fail_gate), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord", side_effect=fake_discord), \
             patch("tasks.task_research_harvest.insert_research_candidate", return_value=1), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        # 5 件連続失敗で停止 → errors が 5 件以上
        assert len(result["errors"]) >= 5
        # anti-bot 停止 Discord 通知が出た
        error_notifs = [c for c in discord_calls if c[1] == "error"]
        assert len(error_notifs) >= 1


# ---------------------------------------------------------------------------
# Test: _normalize_keyword
# ---------------------------------------------------------------------------


class TestNormalizeKeyword:
    """_normalize_keyword の基本動作."""

    def test_lowercase(self):
        assert _normalize_keyword("SONY WH-1000XM5") == "sony wh-1000xm5"

    def test_whitespace_normalization(self):
        assert _normalize_keyword("  Sony  Product  ") == "sony product"

    def test_same_result_for_equivalent_titles(self):
        a = _normalize_keyword("Sony WH-1000XM5")
        b = _normalize_keyword("  Sony  WH-1000XM5  ")
        assert a == b


# ---------------------------------------------------------------------------
# Test: api_call_log 記録 (_record_navigate)
# ---------------------------------------------------------------------------


class TestRecordNavigate:
    """navigate 記録の呼び出し確認."""

    def test_record_navigate_called_per_harvest(self):
        """harvest_product_list 1 回につき _record_navigate が 1 回呼ばれる."""
        config = _make_config()
        products = [_make_harvested_product("Test")]
        harvest_result = _make_harvest_result(products=products)
        gate_data = _make_gate_data(sold_90d=5)
        record_calls = []

        def fake_record(success, error_message=None):
            record_calls.append({"success": success, "error": error_message})

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=gate_data), \
             patch("tasks.task_research_harvest._record_navigate", side_effect=fake_record), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", return_value=1), \
             patch("tasks.task_research_harvest.save_gate_decision", return_value=True), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        # 2 パターン × 1 query = 2 harvest navigate + 1 商品 scrape (navigates_used=1) = 合計 3
        # H-2: scrape navigate は gate_data.navigates_used 分だけ記録される
        assert len(record_calls) >= 3
        assert result["quota_used_this_run"] == len(record_calls)

    def test_harvest_pages_loaded_2_records_2_navigates(self):
        """pages_loaded=2 の harvest 1 呼出で _record_navigate が 2 回呼ばれ、
        quota_used_this_run に 2 が計上される (HIGH 修正検証)."""
        config = _make_config(max_items=50)
        products = [_make_harvested_product("Multi-Page Product")]
        # pages_loaded=2 のハーベスト結果
        harvest_result = _make_harvest_result(products=products, success=True)
        harvest_result.pages_loaded = 2
        gate_data = _make_gate_data(sold_90d=5, navigates_used=1)
        record_calls = []

        def fake_record(success, error_message=None):
            record_calls.append({"success": success, "error": error_message})

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=gate_data), \
             patch("tasks.task_research_harvest._record_navigate", side_effect=fake_record), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", return_value=1), \
             patch("tasks.task_research_harvest.save_gate_decision", return_value=True), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        # 2 pattern × pages_loaded=2 = 4 harvest navigate + 1 scrape (navigates_used=1) = 合計 5
        # 各 harvest 呼出で 2 回の _record_navigate が呼ばれる (pages_loaded=2 のため)
        # quota_used_this_run は len(record_calls) と一致する
        assert result["quota_used_this_run"] == len(record_calls), (
            f"quota_used_this_run={result['quota_used_this_run']} が "
            f"record_calls={len(record_calls)} と不一致"
        )
        # 2 pattern の harvest で pages_loaded=2 ずつ = 4 harvest navigate
        # + 1 scrape = 5 合計
        assert len(record_calls) == 5, (
            f"pages_loaded=2 × 2 pattern + 1 scrape = 5 を期待, 実際 {len(record_calls)}"
        )

    def test_navigates_used_3_counts_3_records(self):
        """navigates_used=3 (フル経路) の場合、scrape 分が 3 件記録される."""
        config = _make_config()
        products = [_make_harvested_product("Test Full Path")]
        harvest_result = _make_harvest_result(products=products)
        gate_data = _make_gate_data(sold_90d=0, has_active=True, sold_1_2yr=3, navigates_used=3)
        record_calls = []

        def fake_record(success, error_message=None):
            record_calls.append({"success": success, "error": error_message})

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=gate_data), \
             patch("tasks.task_research_harvest._record_navigate", side_effect=fake_record), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", return_value=1), \
             patch("tasks.task_research_harvest.save_gate_decision", return_value=True), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        # 2 harvest + 3 scrape navigate = 合計 5
        assert result["quota_used_this_run"] == len(record_calls)
        scrape_records = len(record_calls) - 2  # harvest 分は 2
        assert scrape_records == 3, f"navigates_used=3 なのに scrape 記録が {scrape_records} 件"


# ---------------------------------------------------------------------------
# Test: C-1 新規候補の DB 着地 (実 state machine)
# ---------------------------------------------------------------------------


class TestNewCandidateLandsWithRealStateMachine:
    """C-1: 新規候補が 'gate_passed' に着地し 'new' のまま残らないこと."""

    def test_new_candidate_lands_with_real_status_machine(self, tmp_path, monkeypatch):
        """実 research_candidates_db (tmp DB) を使い、
        新規候補が 'gate_passed' に着地して result["errors"] が空であること."""
        import sys
        # プロジェクトルートを sys.path に追加
        import os
        _ROOT = tmp_path.parent.parent.parent.parent  # tests/../.. = project root
        # 実際のプロジェクトルートを直接指定
        from pathlib import Path
        _PROJECT = Path(__file__).resolve().parent.parent
        if str(_PROJECT) not in sys.path:
            sys.path.insert(0, str(_PROJECT))

        import monitor.database as db_mod
        db_path = tmp_path / "monitor_test.db"
        monkeypatch.setattr(db_mod, "DB_PATH", db_path)
        db_mod.init_db()

        from tasks.task_research_harvest import run_research_harvest, _normalize_keyword
        from monitor.research_candidates_db import (
            insert_research_candidate, save_gate_decision, update_status,
            STATUS_NEW, STATUS_GATE_PASSED,
        )

        config = _make_config(max_items=1)
        prod = _make_harvested_product("Sony MDR-ZX110 Headphones")
        harvest_result = _make_harvest_result(products=[prod])
        # sold_90d=5 → target_instock → gate_passed (実 evaluate_sourcing_gate を使う)

        gate_data = _make_gate_data(sold_90d=5, navigates_used=1)

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=gate_data), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        assert result["errors"] == [], f"errors があってはいけない: {result['errors']}"
        assert result["gate_passed"] == 1
        assert result["success"] is True

        # DB を直接確認: 行が 'new' のまま残っていないこと
        from monitor.database import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT status FROM research_candidates"
            ).fetchall()
        statuses = [r[0] for r in rows]
        assert STATUS_NEW not in statuses, (
            f"行が 'new' のまま残っている (C-1 regression): {statuses}"
        )
        assert STATUS_GATE_PASSED in statuses, (
            f"gate_passed が存在しない: {statuses}"
        )


# ---------------------------------------------------------------------------
# Test: C-2 anti-bot break は success=False
# ---------------------------------------------------------------------------


class TestAntibotBreakReturnsSuccessFalse:
    """C-2: 連続 5 失敗 break 後 result['success'] is False かつ message に停止理由が残る."""

    def test_antibot_break_returns_success_false(self):
        """5 件連続 scrape 失敗 → success=False / message に停止理由 / summary 上書きなし."""
        config = _make_config(max_items=50)
        products = [_make_harvested_product(f"Product {i}") for i in range(10)]
        harvest_result = _make_harvest_result(products=products)
        fail_gate = _make_gate_data(success=False, error="anti-bot timeout")

        with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
             patch("tasks.task_research_harvest._get_today_terapeak_quota_used", return_value=0), \
             patch("tasks.task_research_harvest.harvest_product_list", return_value=harvest_result), \
             patch("tasks.task_research_harvest.scrape_product_detail", return_value=fail_gate), \
             patch("tasks.task_research_harvest._record_navigate"), \
             patch("tasks.task_research_harvest._send_discord"), \
             patch("tasks.task_research_harvest.insert_research_candidate", return_value=1), \
             patch("tasks.task_research_harvest.update_status", return_value=True), \
             patch("tasks.task_research_harvest._get_existing_gate_decisions", return_value={}), \
             patch("tasks.task_research_harvest._update_harvest_meta"):
            result = run_research_harvest(config)

        assert result["success"] is False, (
            f"C-2: anti-bot break 後も success=True になっている (偽装成功)"
        )
        # message に停止理由が残る (summary に上書きされない)
        assert "停止" in result["message"] or "anti-bot" in result["message"].lower(), (
            f"C-2: message に停止理由が含まれない: {result['message']!r}"
        )


# ---------------------------------------------------------------------------
# Test: H-1 Discord は env webhook を使う
# ---------------------------------------------------------------------------


class TestDiscordUsesEnvWebhook:
    """H-1: config webhook 空 + env DISCORD_WEBHOOK_URL 設定で通知送出される."""

    def test_discord_uses_env_webhook(self):
        """config webhook='' (本番同等) + env DISCORD_WEBHOOK_URL 設定で通知が送出される.

        MEDIUM-3 修正後: enabled=false では Discord を送らないため、
        seed_queries 空 (enabled=true) の警告経路で env webhook を使うことを確認。
        """
        config = {
            "discord": {"webhook_url": ""},  # 空 (本番同等)
            "tasks_enabled": {
                "research_harvest": {
                    "enabled": True,
                    "seed_queries": [],  # 空 → seed_queries 空の警告経路へ
                }
            },
        }
        sent_messages = []

        import os
        env_backup = os.environ.get("DISCORD_WEBHOOK_URL")
        os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/test/token"

        try:
            from notifiers.discord_notifier import DiscordNotifier

            def fake_send(self, message, embed=None):
                sent_messages.append({"url": self.webhook_url, "embed": embed})
                return True

            with patch("tasks.task_research_harvest._check_cdp_available", return_value=True), \
                 patch.object(DiscordNotifier, "send_message", fake_send):
                run_research_harvest(config)
        finally:
            if env_backup is None:
                os.environ.pop("DISCORD_WEBHOOK_URL", None)
            else:
                os.environ["DISCORD_WEBHOOK_URL"] = env_backup

        assert len(sent_messages) >= 1, "env webhook 経由で通知が送出されなかった"
        assert sent_messages[0]["url"] == "https://discord.com/api/webhooks/test/token"

    def test_discord_both_empty_logs_warning(self, caplog):
        """config webhook='' + env 未設定 → logger.warning が出る.

        MEDIUM-3 修正後: enabled=false では Discord を送らないため、
        seed_queries 空 (enabled=true) の警告経路で warning が出ることを確認。
        """
        import logging
        import os

        config = {
            "discord": {"webhook_url": ""},
            "tasks_enabled": {
                "research_harvest": {
                    "enabled": True,
                    "seed_queries": [],  # 空 → logger.warning が出る
                }
            },
        }
        env_backup = os.environ.get("DISCORD_WEBHOOK_URL")
        os.environ.pop("DISCORD_WEBHOOK_URL", None)

        try:
            with patch("tasks.task_research_harvest._check_cdp_available", return_value=True):
                with caplog.at_level(logging.WARNING):
                    run_research_harvest(config)
        finally:
            if env_backup is not None:
                os.environ["DISCORD_WEBHOOK_URL"] = env_backup

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "webhook" in m.lower() or "discord" in m.lower() or "seed" in m.lower()
            for m in warning_msgs
        ), (
            f"seed_queries 空でも warning が出なかった: {warning_msgs}"
        )
