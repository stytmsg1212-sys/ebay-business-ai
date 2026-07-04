"""W139-revisit (2026-05-26) url_divergence daily audit のテスト.

背景: listing.source_url != monitored.source_url の状態は『間違った URL を監視中』=
仕入先OOS見逃し = 履行不能リスク。本日午前 19 件発覚 → 緊急 cleanup 完了。
再発防止として scheduler_health_check に daily audit を追加。

カバー:
  T1  divergent listing 1 件で divergence_count=1 検出
  T2  全件一致 = divergence_count=0
  T3  ended listing は対象外
  T4  is_active=0 monitored は対象外
  T5  listing.source_url が NULL/空 = 対象外 (False positive 防止)
  T6  Q2 init_db 2 回連続でデータ保持 + 検出機能維持
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
    tmpdir = tempfile.mkdtemp(prefix="w139_revisit_test_")
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


def _seed_listing(ebay_item_id, sku, source_url, *, qty=1, is_ended=0,
                  title="T"):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings "
            "(ebay_item_id, sku, title, quantity_ebay, is_ended, source_url) "
            "VALUES (?,?,?,?,?,?)",
            (ebay_item_id, sku, title, qty, is_ended, source_url),
        )


def _seed_monitored(ebay_item_id, sku, source_url, *, is_active=1):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO monitored_items "
            "(ebay_item_id, sku, title, source_url, site_config_id, is_active)"
            " VALUES (?,?,?,?,?,?)",
            (ebay_item_id, sku, "T", source_url, None, is_active),
        )


class TestUrlDivergenceDetection:
    """T1-T2 基本検出."""

    def test_divergent_listing_detected(self, tmp_db):
        """T1: listing.source_url != monitored.source_url で 1 件検出."""
        from tasks.task_scheduler_health_check import _check_url_divergence
        _seed_listing("E_DIV_1", "ebayyh_v120",
                      "https://page.auctions.yahoo.co.jp/jp/auction/k122")
        _seed_monitored("E_DIV_1", "ebayyh_v120",
                        "https://page.auctions.yahoo.co.jp/jp/auction/v120")
        # claim_alert_dedupe を mock (dedupe ロジックは別 test)
        with patch("tasks.task_scheduler_health_check._send_url_divergence_alert",
                   return_value=False):
            result = _check_url_divergence({"enabled": True})
        assert result["divergence_count"] == 1
        assert "E_DIV_1" in result["divergent_ids"]

    def test_url_matched_not_detected(self, tmp_db):
        """T2: listing.source_url == monitored.source_url なら検出しない."""
        from tasks.task_scheduler_health_check import _check_url_divergence
        _seed_listing("E_OK_1", "ebayyh_v120",
                      "https://page.auctions.yahoo.co.jp/jp/auction/v120")
        _seed_monitored("E_OK_1", "ebayyh_v120",
                        "https://page.auctions.yahoo.co.jp/jp/auction/v120")
        result = _check_url_divergence({"enabled": True})
        assert result["divergence_count"] == 0
        assert result["divergent_ids"] == []


class TestUrlDivergenceFilters:
    """T3-T5 除外条件."""

    def test_ended_listing_not_detected(self, tmp_db):
        """T3: is_ended=1 listing は対象外 (履行不能リスクなし)."""
        from tasks.task_scheduler_health_check import _check_url_divergence
        _seed_listing("E_END_1", "ebayyh_v120",
                      "https://page.auctions.yahoo.co.jp/jp/auction/k122",
                      is_ended=1)
        _seed_monitored("E_END_1", "ebayyh_v120",
                        "https://page.auctions.yahoo.co.jp/jp/auction/v120")
        result = _check_url_divergence({"enabled": True})
        assert result["divergence_count"] == 0

    def test_inactive_monitored_not_detected(self, tmp_db):
        """T4: is_active=0 monitored は対象外 (停止中の監視に乖離があっても害なし)."""
        from tasks.task_scheduler_health_check import _check_url_divergence
        _seed_listing("E_INA_1", "ebayyh_v120",
                      "https://page.auctions.yahoo.co.jp/jp/auction/k122")
        _seed_monitored("E_INA_1", "ebayyh_v120",
                        "https://page.auctions.yahoo.co.jp/jp/auction/v120",
                        is_active=0)
        result = _check_url_divergence({"enabled": True})
        assert result["divergence_count"] == 0

    def test_null_listing_source_url_not_detected(self, tmp_db):
        """T5: listing.source_url NULL/空 = 対象外 (False positive 防止).
        SKU 派生のみで監視している正常 listing が誤検知されないこと."""
        from tasks.task_scheduler_health_check import _check_url_divergence
        _seed_listing("E_NUL_1", "ebayyh_v120", None)
        _seed_monitored("E_NUL_1", "ebayyh_v120",
                        "https://page.auctions.yahoo.co.jp/jp/auction/v120")
        result = _check_url_divergence({"enabled": True})
        assert result["divergence_count"] == 0


class TestSelfErrorAlert:
    """HIGH-1 (code-reviewer 2026-05-26): audit 自身の DB error が
    Phase C 経路で最緊急 alert に乗ること (Q0 silent skip 再発防止)."""

    def test_db_error_returns_minus_one_with_error_field(self, tmp_db,
                                                          monkeypatch):
        """T7: detection 失敗時に divergence_count=-1 + divergence_error 立つ."""
        from tasks.task_scheduler_health_check import _check_url_divergence
        import tasks.task_scheduler_health_check as mod

        def boom():
            raise sqlite3.OperationalError("simulated DB lock")

        monkeypatch.setattr(mod, "get_conn", boom, raising=False)
        # _check_url_divergence は内部で from monitor.database import get_conn
        # する直接 import なのでこの monkeypatch 経路では効かない場合がある.
        # 安全のため monitor.database.get_conn も差し替え.
        import monitor.database as db_mod
        monkeypatch.setattr(db_mod, "get_conn", boom)

        result = _check_url_divergence({"enabled": True})
        assert result["divergence_count"] == -1
        assert "simulated" in result.get("divergence_error", "")

    def test_phase_c_alert_includes_url_divergence_error(self, tmp_db,
                                                          monkeypatch):
        """T8: _send_phase_c_alert が url_divergence_error を field 化."""
        from tasks.task_scheduler_health_check import _send_phase_c_alert
        from unittest.mock import patch
        import notifiers.discord_notifier as dn
        findings = {
            "intermittent": [], "orphans": [], "db_locks": 0,
            "subprocess_errors": [],
            "url_divergence_error": "simulated lock"
        }
        # 依頼ボード#39 Phase A S2 (2026-07-03) choke point 化により、実送信は
        # notification_center.record_and_maybe_send → resolve_webhook →
        # requests.post 経由になった (httpx ではない)。resolve_webhook が空文字
        # を返すと record_and_maybe_send が早期 False になり検証にならないため
        # webhook 解決先を mock する。
        monkeypatch.setattr(
            dn, "resolve_webhook",
            lambda category="default": "https://discord.fake/webhook",
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            result = _send_phase_c_alert("https://discord.fake/webhook",
                                         findings)
        # send 試行があれば has_alert=True となった証拠
        assert mock_post.called, (
            "url_divergence_error 単体で Phase C alert が発火しない "
            "(HIGH-1 修正前の旧挙動)"
        )


class TestMultiRowMonitored:
    """MED-4 (code-reviewer 2026-05-26): 同 ebay_item_id で active+inactive 共存時の
    divergent count 水増し防止."""

    def test_active_and_inactive_monitored_dedupe(self, tmp_db):
        """T9: monitored で同 ebay_item_id active + inactive 共存 → 1 件カウント."""
        from tasks.task_scheduler_health_check import _check_url_divergence
        _seed_listing("E_MR_1", "ebayyh_v120",
                      "https://page.auctions.yahoo.co.jp/jp/auction/k122")
        _seed_monitored("E_MR_1", "ebayyh_v120",
                        "https://page.auctions.yahoo.co.jp/jp/auction/v120",
                        is_active=1)
        _seed_monitored("E_MR_1", "ebayyh_v120",
                        "https://page.auctions.yahoo.co.jp/jp/auction/old",
                        is_active=0)
        result = _check_url_divergence({"enabled": True})
        assert result["divergence_count"] == 1, (
            "multi-row monitored で count 水増し = DISTINCT 不足"
        )


class TestQ2Idempotency:
    """T6 Q2: init_db 2 回連続でデータ保持."""

    def test_init_db_twice_preserves_data(self, tmp_db):
        from monitor.database import init_db, get_conn
        from tasks.task_scheduler_health_check import _check_url_divergence
        _seed_listing("E_Q2_1", "ebayyh_v120",
                      "https://page.auctions.yahoo.co.jp/jp/auction/k122")
        _seed_monitored("E_Q2_1", "ebayyh_v120",
                        "https://page.auctions.yahoo.co.jp/jp/auction/v120")
        init_db()  # 2 回目
        with get_conn() as c:
            count = c.execute(
                "SELECT COUNT(*) FROM ebay_listings").fetchone()[0]
        assert count >= 1, "init_db 再実行でデータ消失"
        # 検出機能も維持
        result = _check_url_divergence({"enabled": True})
        assert result["divergence_count"] == 1
