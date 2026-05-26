"""W153-UX (2026-05-26): keywords NULL を errors ではなく skipped として扱うテスト.

背景: Codex 2 段 review (2026-05-26 PM) の推奨で rival_watch_enabled=1 だが
rival_search_keywords=NULL の listing は failure ではなく **未設定 (skipped)** として
扱う。毎朝 first_err 化 (Q0 silent skip 防止機構を逆手に取った騒音化) を回避。

カバー:
  T1 per-listing: keywords NULL で success=True, errors=0, skipped_keywords_null=1
  T2 aggregator: skipped_keywords_null が listing 全体で集計される
  T3 aggregator: skipped_keywords_null のみ (errors=0) で task success=True
  T4 既存挙動回帰: keywords ありなら errors=0 / new=N / success=True
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="w153_skipped_test_")
    db_path = Path(tmpdir) / "monitor.db"
    import monitor.database as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db()
    yield db_path
    try:
        db_path.unlink()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    try:
        Path(tmpdir).rmdir()
    except OSError:
        pass


def _seed_listing(ebay_item_id, sku, title, *,
                  rival_watch_enabled=1, rival_search_keywords=None):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings "
            "(ebay_item_id, sku, title, quantity_ebay, is_ended, "
            "rival_watch_enabled, rival_search_keywords) "
            "VALUES (?,?,?,1,0,?,?)",
            (ebay_item_id, sku, title, rival_watch_enabled,
             rival_search_keywords),
        )


class TestKeywordsNullSkipped:
    """T1: per-listing keywords NULL = skipped 計上."""

    def test_per_listing_keywords_null_is_skipped(self, tmp_db):
        from tasks.task_rival_detection import (
            run_rival_per_listing_detection_one,
        )
        _seed_listing("E_KW_1", "ebayyh_test1", "Test Listing 1",
                      rival_search_keywords=None)
        result = run_rival_per_listing_detection_one(
            "E_KW_1", config={"enabled": True})
        assert result["success"] is True, "skipped は failure ではない"
        assert result["errors"] == 0, "errors にカウントしない"
        assert result["skipped_keywords_null"] == 1
        assert "keywords NULL" in result["message"]


class TestAggregator:
    """T2-T3: aggregator (top-level) 集計."""

    def test_aggregator_sums_skipped_keywords_null(self, tmp_db):
        from tasks.task_rival_detection import run_rival_detection
        _seed_listing("E_AG_1", "ebayyh_a1", "L1", rival_search_keywords=None)
        _seed_listing("E_AG_2", "ebayyh_a2", "L2", rival_search_keywords=None)
        _seed_listing("E_AG_3", "ebayyh_a3", "L3", rival_search_keywords=None)
        result = run_rival_detection(
            {"enabled": True, "max_listings_per_run": 10,
             "max_requests_per_run": 50})
        assert result["skipped_keywords_null"] == 3
        assert result["errors"] == 0
        assert result["listings_processed"] == 3

    def test_only_skipped_yields_task_success_true(self, tmp_db):
        """T3 K3 核心: skip だけなら task success=True (毎朝 first_err 騒音 0)."""
        from tasks.task_rival_detection import run_rival_detection
        _seed_listing("E_SU_1", "ebayyh_s1", "L1", rival_search_keywords=None)
        _seed_listing("E_SU_2", "ebayyh_s2", "L2", rival_search_keywords=None)
        result = run_rival_detection(
            {"enabled": True, "max_listings_per_run": 10,
             "max_requests_per_run": 50})
        assert result["success"] is True, (
            "skip だけで failure 扱いされると毎朝 Discord errors_alert "
            "発火 (W153-UX 修正前の旧挙動)"
        )
        assert result["errors"] == 0
        # message 形式の検証 (skip_kw_null= 含む)
        assert "skip_kw_null=2" in result["message"]


class TestKeywordsNullReminder:
    """HIGH-4 (code-reviewer 2026-05-26): skip_rate >= 30% で週次 reminder."""

    def test_high_skip_rate_triggers_weekly_reminder(self, tmp_db):
        """T4: 全 2 listing 中 2 件 NULL (rate=100%) → reminder claim 試行."""
        from tasks.task_rival_detection import (
            _maybe_remind_user_of_keywords_null,
        )
        # claim_alert_dedupe を mock し、claim 試行を観測
        with patch("tasks.task_rival_detection.claim_alert_dedupe",
                   return_value=False) as mock_claim:
            summary = {
                "listings_processed": 2,
                "skipped_keywords_null": 2,
            }
            cfg = {"discord": {"webhook_url": "https://discord.fake/wh"}}
            _maybe_remind_user_of_keywords_null(cfg, summary)
        assert mock_claim.called, (
            "skip_rate=100% で reminder claim が試行されない "
            "(HIGH-4 silent skip 補完経路欠落)"
        )
        # task_key 検証
        args, kwargs = mock_claim.call_args
        assert kwargs.get("task_key") == "w153_keywords_null_weekly"

    def test_low_skip_rate_does_not_remind(self, tmp_db):
        """T5: skip_rate < 30% は reminder 発射しない (騒音抑制)."""
        from tasks.task_rival_detection import (
            _maybe_remind_user_of_keywords_null,
        )
        with patch("tasks.task_rival_detection.claim_alert_dedupe",
                   return_value=True) as mock_claim:
            summary = {
                "listings_processed": 100,
                "skipped_keywords_null": 5,  # 5%
            }
            cfg = {"discord": {"webhook_url": "https://discord.fake/wh"}}
            _maybe_remind_user_of_keywords_null(cfg, summary)
        assert not mock_claim.called, (
            "skip_rate=5% で reminder 発射 = noise 過多"
        )

    def test_no_webhook_skips_claim(self, tmp_db):
        """T6: webhook 未設定なら claim 消費せず return (永続失効防止)."""
        from tasks.task_rival_detection import (
            _maybe_remind_user_of_keywords_null,
        )
        with patch("tasks.task_rival_detection.claim_alert_dedupe",
                   return_value=True) as mock_claim:
            summary = {"listings_processed": 2, "skipped_keywords_null": 2}
            _maybe_remind_user_of_keywords_null(
                {"discord": {"webhook_url": ""}}, summary)
        assert not mock_claim.called, (
            "webhook 未設定で claim 消費 = 永続 reminder 失効"
        )
