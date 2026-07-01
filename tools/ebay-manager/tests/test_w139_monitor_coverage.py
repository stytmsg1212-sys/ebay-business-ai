"""W139: 監視台帳カバレッジ自動補完 (ensure_monitor_coverage) の回帰 test.

本番事故 (2026-05-18, item 358487417178 が monitored_items 未登録で仕入先
OOS 検知不能 → 履行不能注文) の恒久対策。Codex 独立診断収束。

カバー:
- 未登録 active 無在庫 listing が自動登録される (根治)
- 冪等 (2 回連続実行で registered=0)
- qty=0 / is_ended=1 / 有在庫(stock) SKU は対象外
- source_url 生成不能 prefix (site_config 未登録) は DLQ、登録せず Q0 報告、
  success は True (DLQ は task 失敗ではない)
- find_coverage_gaps の coverable / dlq 分割
- SKU 集約: 既登録 (同 sku) は再登録対象に入らない
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="w139_test_")
    db_path = Path(tmpdir) / "monitor.db"
    import monitor.database as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db()  # DEFAULT_SITE_CONFIGS (ebayyh_/ebayme_ 等) も seed
    yield db_path
    try:
        db_path.unlink()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    try:
        Path(tmpdir).rmdir()
    except OSError:
        pass


def _seed_listing(ebay_item_id, sku, *, qty=1, is_ended=0, title="T"):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings "
            "(ebay_item_id, sku, title, quantity_ebay, is_ended) "
            "VALUES (?,?,?,?,?)",
            (ebay_item_id, sku, title, qty, is_ended),
        )


def _monitored_skus():
    from monitor.database import get_conn
    with get_conn() as c:
        return {r[0] for r in c.execute(
            "SELECT sku FROM monitored_items").fetchall()}


class TestEnsureMonitorCoverage:
    def test_unmonitored_no_stock_listing_registered(self, tmp_db):
        from tasks.task_ensure_monitor_coverage import (
            run_ensure_monitor_coverage, find_coverage_gaps)
        _seed_listing("E_W139_1", "ebayyh_testaa001")
        # 前提確認: site_config seed で coverable に入る (DLQ でない)
        gaps = find_coverage_gaps()
        assert any(c["sku"] == "ebayyh_testaa001" for c in gaps["coverable"]), \
            f"ebayyh_ が coverable に入らない (site_config seed 不足?) gaps={gaps}"
        r = run_ensure_monitor_coverage({})
        assert r["success"] is True
        assert r["registered"] >= 1
        assert "ebayyh_testaa001" in _monitored_skus()

    def test_idempotent_second_run_no_reregister(self, tmp_db):
        from tasks.task_ensure_monitor_coverage import run_ensure_monitor_coverage
        _seed_listing("E_W139_2", "ebayyh_testbb002")
        r1 = run_ensure_monitor_coverage({})
        assert r1["registered"] >= 1
        r2 = run_ensure_monitor_coverage({})
        assert r2["registered"] == 0, (
            f"冪等性違反: 2 回目で再登録 {r2}")
        assert r2["success"] is True

    def test_qty0_ended_stock_excluded(self, tmp_db):
        from tasks.task_ensure_monitor_coverage import run_ensure_monitor_coverage
        _seed_listing("E_W139_q0", "ebayyh_qty0xxx", qty=0)
        _seed_listing("E_W139_end", "ebayyh_endedxx", is_ended=1)
        _seed_listing("E_W139_stk", "stock:01")  # 有在庫 SKU
        r = run_ensure_monitor_coverage({})
        mon = _monitored_skus()
        assert "ebayyh_qty0xxx" not in mon, "qty=0 を誤登録"
        assert "ebayyh_endedxx" not in mon, "is_ended=1 を誤登録"
        assert "stock:01" not in mon, "有在庫SKUを誤登録"

    def test_unconvertible_prefix_goes_dlq(self, tmp_db):
        """site_config 未登録 prefix は DLQ。登録せず Q0 報告、success=True."""
        from tasks.task_ensure_monitor_coverage import (
            run_ensure_monitor_coverage, find_coverage_gaps)
        _seed_listing("E_W139_dlq", "ebayZZ_unconfigured1")
        gaps = find_coverage_gaps()
        assert any(d["sku"] == "ebayZZ_unconfigured1" for d in gaps["dlq"])
        r = run_ensure_monitor_coverage({})
        assert "ebayZZ_unconfigured1" in r["dlq_skus"]
        assert r["dlq"] >= 1
        assert "ebayZZ_unconfigured1" not in _monitored_skus(), \
            "URL生成不能を誤登録"
        # DLQ は検出・報告済 = task 失敗ではない (Q0: message に痕跡)
        assert r["success"] is True
        assert "dlq" in r["message"]

    def test_already_monitored_not_in_target(self, tmp_db):
        """同 sku が monitored 済なら再登録対象に入らない (SKU 集約維持)."""
        from monitor.database import upsert_item
        from tasks.task_ensure_monitor_coverage import find_coverage_gaps
        _seed_listing("E_W139_dup", "ebayyh_already001")
        upsert_item(sku="ebayyh_already001", ebay_item_id="E_W139_dup",
                    title="pre")
        gaps = find_coverage_gaps()
        assert not any(c["sku"] == "ebayyh_already001"
                       for c in gaps["coverable"]), "既登録が対象に残存"
        assert not any(d["sku"] == "ebayyh_already001"
                       for d in gaps["dlq"])

    def test_mixed_scan_counts(self, tmp_db):
        from tasks.task_ensure_monitor_coverage import run_ensure_monitor_coverage
        _seed_listing("E_W139_m1", "ebayyh_mix001")
        _seed_listing("E_W139_m2", "ebayme_10000002")
        _seed_listing("E_W139_m3", "ebayZZ_mixdlq")        # DLQ
        _seed_listing("E_W139_m4", "ebayyh_mixq0", qty=0)   # 除外
        r = run_ensure_monitor_coverage({})
        assert r["registered"] == 2  # ebayyh_mix001 + ebayme_10000002
        assert r["dlq"] == 1
        assert r["scanned"] == 3     # coverable2 + dlq1 (qty0 は対象外)
        assert r["failed"] == 0
        assert r["success"] is True


class TestNullDefense:
    """code-reviewer MEDIUM (2026-05-18): NULL is_ended / NULL quantity_ebay
    が coverable から漏れて再盲点化しないこと (anti-silent-skip 自己矛盾防止)."""

    def _seed_raw(self, eid, sku, is_ended, qty):
        from monitor.database import get_conn
        with get_conn() as c:
            c.execute(
                "INSERT INTO ebay_listings "
                "(ebay_item_id, sku, title, quantity_ebay, is_ended) "
                "VALUES (?,?,?,?,?)", (eid, sku, "T", qty, is_ended))

    def test_null_is_ended_treated_as_active_and_covered(self, tmp_db):
        from tasks.task_ensure_monitor_coverage import find_coverage_gaps
        self._seed_raw("E_W139_nie", "ebayyh_nullend1", None, 1)
        gaps = find_coverage_gaps()
        assert any(c["sku"] == "ebayyh_nullend1" for c in gaps["coverable"]), \
            "NULL is_ended が coverable から漏れた (silent gap 再発)"

    def test_null_qty_treated_as_unknown_and_covered(self, tmp_db):
        from tasks.task_ensure_monitor_coverage import find_coverage_gaps
        self._seed_raw("E_W139_nq", "ebayyh_nullqty1", 0, None)
        gaps = find_coverage_gaps()
        assert any(c["sku"] == "ebayyh_nullqty1" for c in gaps["coverable"]), \
            "NULL quantity_ebay が coverable から漏れた (qty同期失敗で再盲点化)"

    def test_explicit_qty0_still_excluded(self, tmp_db):
        """明示 qty=0 は従来通り除外 (確実に販売停止 = RISK でない)."""
        from tasks.task_ensure_monitor_coverage import find_coverage_gaps
        self._seed_raw("E_W139_q0e", "ebayyh_zeroqty1", 0, 0)
        gaps = find_coverage_gaps()
        assert not any(c["sku"] == "ebayyh_zeroqty1"
                       for c in gaps["coverable"])
        assert not any(d["sku"] == "ebayyh_zeroqty1" for d in gaps["dlq"])


class TestComponentBHealthCheckCoverage:
    """Component B: scheduler health check に統合した監視カバレッジ欠落検知."""

    def test_check_coverage_detects_gaps_no_webhook(self, tmp_db, monkeypatch):
        import tasks.task_scheduler_health_check as hc
        from tasks.task_scheduler_health_check import _check_coverage
        # W187: _resolve_webhook_url は config 空時 .env DISCORD_WEBHOOK_URL を
        # fallback 参照するため、webhook 設定済の dev 環境では実 Discord 送信が
        # 走り sent=True で fail していた (env 依存 + 実通知副作用)。webhook 無し
        # 状態を決定的に再現し実送信も防ぐため resolver を空文字に固定する。
        monkeypatch.setattr(hc, "_resolve_webhook_url", lambda config: "")
        _seed_listing("E_W139_b1", "ebayyh_covb001")        # coverable
        _seed_listing("E_W139_b2", "ebayZZ_dlqb001")        # dlq
        cov = _check_coverage({})  # webhook なし → 送信せず算出のみ
        assert cov["coverable"] == 1
        assert cov["dlq"] == 1
        assert "ebayZZ_dlqb001" in cov["dlq_skus"]
        assert cov["coverage_alert_sent"] is False  # webhook 無し

    def test_check_coverage_clean_when_all_monitored(self, tmp_db):
        from monitor.database import upsert_item
        from tasks.task_scheduler_health_check import _check_coverage
        _seed_listing("E_W139_b3", "ebayyh_covb003")
        upsert_item(sku="ebayyh_covb003", ebay_item_id="E_W139_b3",
                    title="x")
        cov = _check_coverage({})
        assert cov["coverable"] == 0
        assert cov["dlq"] == 0
        assert cov["coverage_alert_sent"] is False

    def test_coverage_compute_failure_alerts_not_silent(self, tmp_db,
                                                        monkeypatch):
        """Codex HIGH (2026-05-18) 回帰: find_coverage_gaps 自体が壊れた時、
        log だけで Discord 沈黙しない (盲点検知不能=最緊急、必ず通知試行)."""
        import tasks.task_scheduler_health_check as hc

        def _boom():
            raise RuntimeError("simulated coverage compute failure")

        monkeypatch.setattr(
            "tasks.task_ensure_monitor_coverage.find_coverage_gaps", _boom)
        sent_calls = {}

        def _fake_err_alert(url, err):
            sent_calls["url"] = url
            sent_calls["err"] = err
            return True  # webhook 到達を模擬

        monkeypatch.setattr(hc, "_send_coverage_error_alert", _fake_err_alert)
        cov = hc._check_coverage({"discord": {"webhook_url": "http://x"}})
        assert cov["coverable"] == -1 and cov["dlq"] == -1
        assert "coverage_error" in cov
        assert cov["coverage_error_alert_sent"] is True, \
            "算出失敗時に Discord 通知を試行していない (silent = Q0 違反)"
        assert "simulated coverage compute failure" in sent_calls["err"]

    def test_coverage_error_alert_no_webhook_returns_false(self, tmp_db):
        from tasks.task_scheduler_health_check import _send_coverage_error_alert
        assert _send_coverage_error_alert("", "err") is False

    def test_health_check_return_includes_coverage(self, tmp_db):
        from tasks.task_scheduler_health_check import run_scheduler_health_check
        _seed_listing("E_W139_b4", "ebayZZ_dlqb004")  # DLQ → coverage 検出
        r = run_scheduler_health_check({})
        assert "coverage" in r
        assert r["coverage"]["dlq"] >= 1
        assert "coverage:" in r["message"]
