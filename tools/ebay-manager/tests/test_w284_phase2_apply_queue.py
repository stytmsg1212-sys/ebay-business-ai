# -*- coding: utf-8 -*-
"""W284 Phase 2: task_ebaymag_apply_queue / task_ebaymag_sync_audit のユニットテスト。

テスト対象:
  A. CDP不在時に Discord 通知 + skip (success=True)
  B. mapping なし → discover → awaiting_import
  C. desired 差分を driver に渡して applied
  D. 全失敗経路で status/attempts/last_error が必ず更新されること

識別キー: ebay_item_id (SKU 禁止、conftest の DB 隔離適用)。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# conftest.py の _isolate_monitor_db により本番 DB は使わない


# ──────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────

def _seed_listing(eid: str, title: str, segment: str, desired_sites: list) -> None:
    """テスト用 listing + desired_sites を DB に投入。"""
    from monitor.database import init_db, get_conn
    import json
    init_db()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO ebay_listings
                (ebay_item_id, title, sku, ebaymag_segment,
                 ebaymag_desired_sites_json, ebaymag_desired_updated_at)
            VALUES (?, ?, 'stock:01', ?, ?, CURRENT_TIMESTAMP)
            """,
            (eid, title, segment, json.dumps(desired_sites)),
        )


def _seed_queue_job(eid: str, reason: str = "new_listing") -> int:
    """テスト用 ebaymag_apply_queue を投入し job_id を返す。"""
    from monitor.database import init_db, get_conn, enqueue_ebaymag_apply
    init_db()
    enqueue_ebaymag_apply(eid, reason)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM ebaymag_apply_queue WHERE ebay_item_id=? AND status='pending'",
            (eid,),
        ).fetchone()
    assert row is not None, "enqueue_ebaymag_apply で job が作られていない"
    return row["id"]


def _get_job(job_id: int) -> dict:
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ebaymag_apply_queue WHERE id=?", (job_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


# ──────────────────────────────────────────────────────────────
# A. CDP 不在時 — skip + Discord 通知 (throttle なし = 初回)
# ──────────────────────────────────────────────────────────────

class TestCdpAbsent:
    def test_cdp_absent_returns_success_with_pending_count(self, monkeypatch):
        """CDP 不在時は success=True で skip し、Discord 通知を 1 回送る。"""
        from monitor.database import init_db
        init_db()
        # DB にキューを 2 件投入
        _seed_listing("111111111111", "Test Item 1", "全国", ["UK", "DE"])
        _seed_listing("222222222222", "Test Item 2", "優先国", ["UK"])
        _seed_queue_job("111111111111")
        _seed_queue_job("222222222222")

        discord_calls: list[str] = []

        def _fake_probe():
            return False, "CDP not reachable"

        def _fake_notify(config, msg, **_kw):
            # **_kw: 本番 _discord_notify(config, message, *, severity=...) の
            # severity kwarg を受け流す (依頼ボード#45 severity 付き呼出し追従)
            discord_calls.append(msg)

        def _fake_should_notify(config, n_pending):
            return True  # throttle を通過させる

        def _fake_record_notified(config, n_pending):
            pass

        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag", _fake_probe
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify", _fake_notify
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._should_send_cdp_absent_notify",
            _fake_should_notify,
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._record_cdp_absent_notified",
            _fake_record_notified,
        )

        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue({})

        assert result["success"] is True
        assert result["processed"] == 0
        assert len(discord_calls) == 1, f"Discord 通知が 1 回呼ばれるべき: {discord_calls}"
        assert "2" in discord_calls[0] or "反映待ち" in discord_calls[0]

    def test_cdp_absent_no_pending_no_notify(self, monkeypatch):
        """CDP 不在でも pending 0 件なら Discord 通知しない。"""
        from monitor.database import init_db
        init_db()

        discord_calls: list[str] = []

        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag",
            lambda: (False, "CDP not reachable"),
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify",
            lambda config, msg, **_kw: discord_calls.append(msg),
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._should_send_cdp_absent_notify",
            lambda config, n_pending: True,
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._record_cdp_absent_notified",
            lambda config, n_pending: None,
        )

        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue({})

        assert result["success"] is True
        assert len(discord_calls) == 0, "pending 0 件なら通知しない"


# ──────────────────────────────────────────────────────────────
# B. discover → awaiting_import
# ──────────────────────────────────────────────────────────────

class TestDiscoverAwaitingImport:
    def test_discover_none_sets_awaiting_import(self, monkeypatch):
        """discover が None を返した場合 → awaiting_import + next_attempt_at 設定。"""
        from monitor.database import init_db
        init_db()
        _seed_listing("333333333333", "Sony WH-1000XM5", "全国", ["UK", "DE"])
        job_id = _seed_queue_job("333333333333")

        # CDP alive
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag",
            lambda: (True, ""),
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify",
            lambda config, msg, **_kw: None,
        )

        # ebaymag_driver: discover 失敗
        from monitor.ebaymag_driver import EbaymagResult
        def _fake_discover(query, expected_itm):
            r = EbaymagResult()
            r.ok = False
            r.error = "候補なし"
            return r

        # product mapping なし (get_ebaymag_product は None)
        monkeypatch.setattr(
            "monitor.database.get_ebaymag_product", lambda eid: None
        )
        monkeypatch.setattr(
            "monitor.ebaymag_driver.discover_product_id", _fake_discover
        )

        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue({})

        assert result["success"] is True  # 失敗でも success=True (discover ラグは正常 skip)
        assert result["awaiting_import"] == 1

        job = _get_job(job_id)
        assert job["status"] == "awaiting_import"
        assert job["attempts"] == 1
        assert job["last_error"] is not None
        assert job["next_attempt_at"] is not None

    def test_discover_max_attempts_becomes_needs_manual(self, monkeypatch):
        """discover が MAX_ATTEMPTS 回失敗後 → needs_manual + Discord 通知。"""
        from monitor.database import init_db, get_conn, enqueue_ebaymag_apply
        init_db()
        _seed_listing("444444444444", "Panasonic Camera", "全国", ["UK"])
        enqueue_ebaymag_apply("444444444444", "new_listing")
        # attempts を閾値 (5) に設定
        from tasks.task_ebaymag_apply_queue import _AWAITING_IMPORT_MAX_ATTEMPTS
        with get_conn() as conn:
            conn.execute(
                "UPDATE ebaymag_apply_queue SET attempts=?, status='awaiting_import' "
                "WHERE ebay_item_id=?",
                (_AWAITING_IMPORT_MAX_ATTEMPTS, "444444444444"),
            )
            row = conn.execute(
                "SELECT id FROM ebaymag_apply_queue WHERE ebay_item_id=?",
                ("444444444444",),
            ).fetchone()
        job_id = row["id"]

        discord_calls: list[str] = []
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag",
            lambda: (True, ""),
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify",
            lambda config, msg, **_kw: discord_calls.append(msg),
        )

        from monitor.ebaymag_driver import EbaymagResult
        def _fake_discover(query, expected_itm):
            r = EbaymagResult()
            r.ok = False
            r.error = "not found"
            return r

        monkeypatch.setattr("monitor.database.get_ebaymag_product", lambda eid: None)
        monkeypatch.setattr("monitor.ebaymag_driver.discover_product_id", _fake_discover)

        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        run_ebaymag_apply_queue({})

        job = _get_job(job_id)
        assert job["status"] == "needs_manual"
        assert len(discord_calls) >= 1


# ──────────────────────────────────────────────────────────────
# C. 差分適用 → applied
# ──────────────────────────────────────────────────────────────

class TestApplied:
    def test_desired_diff_calls_apply_and_marks_done(self, monkeypatch):
        """desired=[UK,DE] / actual={UK:True, DE:False, FR:False} → turn_on=[DE]。
        apply_site_changes が呼ばれ、job が done になること。
        """
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "555555555555"
        _seed_listing(eid, "Canon EOS R6", "全国", ["UK", "DE"])
        job_id = _seed_queue_job(eid)
        # product mapping をあらかじめ登録 (discover 不要)
        upsert_ebaymag_product(eid, product_id="prod_555", site_states={"UK": True, "DE": False})

        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag",
            lambda: (True, ""),
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify",
            lambda config, msg, **_kw: None,
        )

        # fetch_site_states: 実態 UK=on, DE=off (desired と差あり)
        from monitor.ebaymag_driver import EbaymagResult

        def _fake_fetch(product_id, expected_itm):
            r = EbaymagResult()
            r.ok = True
            r.site_states = {"UK": True, "DE": False, "FR": False, "IT": False, "ES": False, "CA": False, "AU": False}
            return r

        apply_calls: list[dict] = []

        def _fake_apply(product_id, expected_itm, turn_on, turn_off):
            apply_calls.append({"turn_on": turn_on, "turn_off": turn_off})
            r = EbaymagResult()
            r.ok = True
            r.site_states = {"UK": True, "DE": True, "FR": False, "IT": False, "ES": False, "CA": False, "AU": False}
            return r

        monkeypatch.setattr("monitor.ebaymag_driver.fetch_site_states", _fake_fetch)
        monkeypatch.setattr("monitor.ebaymag_driver.apply_site_changes", _fake_apply)

        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue({})

        assert result["applied"] == 1
        assert result["failed"] == 0
        assert len(apply_calls) == 1
        assert apply_calls[0]["turn_on"] == ["DE"]
        assert apply_calls[0]["turn_off"] == []

        job = _get_job(job_id)
        assert job["status"] == "done"

    def test_no_diff_marks_done_without_apply(self, monkeypatch):
        """desired=[UK] / actual={UK:True} → 差分なし → apply 呼ばず done。"""
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "666666666666"
        _seed_listing(eid, "Nikon Z7II", "優先国", ["UK"])
        job_id = _seed_queue_job(eid)
        upsert_ebaymag_product(eid, product_id="prod_666", site_states={"UK": True})

        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag",
            lambda: (True, ""),
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify",
            lambda config, msg, **_kw: None,
        )

        from monitor.ebaymag_driver import EbaymagResult

        def _fake_fetch(product_id, expected_itm):
            r = EbaymagResult()
            r.ok = True
            r.site_states = {"UK": True, "DE": False, "FR": False, "IT": False, "ES": False, "CA": False, "AU": False}
            return r

        apply_calls: list[dict] = []

        def _fake_apply(*args, **kwargs):
            apply_calls.append(args)
            r = EbaymagResult()
            r.ok = True
            r.site_states = {}
            return r

        monkeypatch.setattr("monitor.ebaymag_driver.fetch_site_states", _fake_fetch)
        monkeypatch.setattr("monitor.ebaymag_driver.apply_site_changes", _fake_apply)

        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue({})

        assert len(apply_calls) == 0, "差分なし時は apply を呼ばない"
        job = _get_job(job_id)
        assert job["status"] == "done"


# ──────────────────────────────────────────────────────────────
# D. 全失敗経路で status/attempts/last_error が更新されること
# ──────────────────────────────────────────────────────────────

class TestFailurePaths:
    def test_listing_missing_marks_needs_manual(self, monkeypatch):
        """listing が DB に存在しない job → needs_manual。"""
        from monitor.database import init_db, get_conn, enqueue_ebaymag_apply
        init_db()
        # ebay_listings に追加せずにキューだけ投入
        eid = "777777777777"
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO ebaymag_apply_queue "
                "(ebay_item_id, reason, status, attempts, created_at, updated_at) "
                "VALUES (?, 'new_listing', 'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (eid,),
            )
            job_id = conn.execute(
                "SELECT id FROM ebaymag_apply_queue WHERE ebay_item_id=?", (eid,)
            ).fetchone()["id"]

        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag",
            lambda: (True, ""),
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify",
            lambda config, msg, **_kw: None,
        )

        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        run_ebaymag_apply_queue({})

        job = _get_job(job_id)
        assert job["status"] == "needs_manual"
        assert job["last_error"] is not None

    def test_fetch_site_states_failure_marks_failed(self, monkeypatch):
        """fetch_site_states が ok=False → job.status=failed, last_error 設定。"""
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "888888888888"
        _seed_listing(eid, "Roland Piano", "全国", ["UK", "DE"])
        job_id = _seed_queue_job(eid)
        upsert_ebaymag_product(eid, product_id="prod_888", site_states={"UK": True})

        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag",
            lambda: (True, ""),
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify",
            lambda config, msg, **_kw: None,
        )

        from monitor.ebaymag_driver import EbaymagResult

        def _fake_fetch(product_id, expected_itm):
            r = EbaymagResult()
            r.ok = False
            r.error = "UI structure changed"
            return r

        monkeypatch.setattr("monitor.ebaymag_driver.fetch_site_states", _fake_fetch)

        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue({})

        assert result["failed"] == 1
        job = _get_job(job_id)
        assert job["status"] == "failed"
        assert job["attempts"] == 1
        assert "UI structure changed" in (job["last_error"] or "")

    def test_empty_site_states_after_fetch_marks_failed(self, monkeypatch):
        """fetch_site_states が ok=True but site_states={} → failed (Q0: 偽装成功禁止)。"""
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "999999999999"
        _seed_listing(eid, "Yamaha YZF-R1", "全国", ["UK"])
        job_id = _seed_queue_job(eid)
        upsert_ebaymag_product(eid, product_id="prod_999", site_states={})

        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag",
            lambda: (True, ""),
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify",
            lambda config, msg, **_kw: None,
        )

        from monitor.ebaymag_driver import EbaymagResult

        def _fake_fetch(product_id, expected_itm):
            r = EbaymagResult()
            r.ok = True
            r.site_states = {}  # 空! Q0 検証対象
            return r

        monkeypatch.setattr("monitor.ebaymag_driver.fetch_site_states", _fake_fetch)

        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue({})

        assert result["failed"] == 1
        job = _get_job(job_id)
        assert job["status"] == "failed"
        assert job["last_error"] is not None


# ──────────────────────────────────────────────────────────────
# E. TASK_SCHEDULE 登録確認
# ──────────────────────────────────────────────────────────────

class TestTaskScheduleRegistration:
    def test_ebaymag_apply_queue_registered(self):
        from monitor.task_execution_log import TASK_SCHEDULE_BY_KEY
        assert "ebaymag_apply_queue" in TASK_SCHEDULE_BY_KEY, (
            "ebaymag_apply_queue が TASK_SCHEDULE に未登録"
        )
        entry = TASK_SCHEDULE_BY_KEY["ebaymag_apply_queue"]
        assert entry["owner"] == "ebaymag_apply"

    def test_ebaymag_sync_audit_registered(self):
        from monitor.task_execution_log import TASK_SCHEDULE_BY_KEY
        assert "ebaymag_sync_audit" in TASK_SCHEDULE_BY_KEY, (
            "ebaymag_sync_audit が TASK_SCHEDULE に未登録"
        )
        entry = TASK_SCHEDULE_BY_KEY["ebaymag_sync_audit"]
        assert entry["owner"] == "ebaymag_apply"
