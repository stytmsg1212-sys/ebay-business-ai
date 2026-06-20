# -*- coding: utf-8 -*-
"""W284 Phase 3: task_ebaymag_relist のユニットテスト。

テスト対象:
  A. feature flag OFF → 即 skip (success=True, skipped_reason='feature_flag_off')
  B. eBaymag 商品のみ選定 — '出さない' は対象外、'全国'/'優先国'/'カスタム' のみ
  C. relist 失敗 (EndItem 失敗 / Relist 全失敗) → needs_manual
  D. relist 成功 → inherit → discover 未発見 → Phase2 委譲 (success=False, discover_delegated=True)
  E. relist 成功 → inherit → discover 成功 → apply → success=True

識別キー: ebay_item_id (SKU 禁止)。
CDP/eBaymag/ebay API は全て monkeypatch。conftest _isolate_monitor_db で本番 DB 遮断。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


# ──────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────

def _seed_listing(
    eid: str,
    title: str = "Test Product",
    segment: str = "全国",
    desired_sites: list | None = None,
    rank: str = "E",
    watch_count: int = 0,
    qty: int = 1,
    is_ended: int = 0,
) -> None:
    """テスト用 listing を DB に投入。"""
    from monitor.database import init_db, get_conn
    init_db()
    sites_json = json.dumps(desired_sites or ["UK", "DE"])
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO ebay_listings
                (ebay_item_id, title, sku, rank, watch_count, quantity_ebay,
                 is_ended, ebaymag_segment,
                 ebaymag_desired_sites_json, ebaymag_desired_updated_at,
                 current_price, last_synced_at)
            VALUES (?, ?, 'stock:01', ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 100.0, CURRENT_TIMESTAMP)
            """,
            (eid, title, rank, watch_count, qty, is_ended, segment, sites_json),
        )


_DUMMY_CREDS = {
    "app_id": "test_app",
    "dev_id": "test_dev",
    "cert_id": "test_cert",
    "user_token": "test_token",
}

_BASE_CONFIG = {
    "tasks_enabled": {
        "ebaymag_relist": {"enabled": True, "max_per_run": 3},
        "daily_relist": {"cooldown_days": 10, "sleep_between_sec": 0},
    }
}

_FLAG_OFF_CONFIG = {
    "tasks_enabled": {
        "ebaymag_relist": {"enabled": False},
    }
}


# ──────────────────────────────────────────────────────────────
# テスト A: feature flag OFF → 即 skip
# ──────────────────────────────────────────────────────────────

class TestFlagOff:
    def test_flag_off_returns_skip(self):
        """feature flag OFF の場合は CDP probe も呼ばずに即 skip。"""
        from tasks.task_ebaymag_relist import run_ebaymag_relist
        result = run_ebaymag_relist(_FLAG_OFF_CONFIG)
        assert result["success"] is True
        assert result["skipped_reason"] == "feature_flag_off"
        assert result["processed"] == 0

    def test_flag_default_false(self):
        """tasks_enabled に ebaymag_relist キーがない場合も skip (既定 False)。"""
        from tasks.task_ebaymag_relist import run_ebaymag_relist
        result = run_ebaymag_relist({"tasks_enabled": {}})
        assert result["skipped_reason"] == "feature_flag_off"


# ──────────────────────────────────────────────────────────────
# テスト B: eBaymag 商品のみ選定
# ──────────────────────────────────────────────────────────────

class TestTargetSelection:
    def test_only_ebaymag_segments_selected(self, monkeypatch):
        """'全国'/'優先国'/'カスタム' は選定対象、'出さない'/NULL は対象外。"""
        from monitor.database import init_db
        init_db()
        _seed_listing("111111111111", segment="全国")
        _seed_listing("222222222222", segment="優先国")
        _seed_listing("333333333333", segment="カスタム")
        _seed_listing("444444444444", segment="出さない")  # 対象外
        # rank≠E (daily_relist 対象) も対象外
        _seed_listing("555555555555", segment="全国", rank="A")  # 対象外

        from tasks.task_ebaymag_relist import _select_ebaymag_relist_targets
        targets = _select_ebaymag_relist_targets(limit=10)
        eids = {t["ebay_item_id"] for t in targets}

        assert "111111111111" in eids
        assert "222222222222" in eids
        assert "333333333333" in eids
        assert "444444444444" not in eids  # 出さない = daily_relist 対象 = 本タスク対象外
        assert "555555555555" not in eids  # rank≠E

    def test_cooldown_excludes_recently_relisted(self, monkeypatch):
        """relist_history にある item はクールダウン期間内は除外される。"""
        from monitor.database import init_db, get_conn
        init_db()
        _seed_listing("666666666666", segment="全国")
        # relist_history に 2 日前の成功 relist を記録
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO relist_history
                   (old_item_id, new_item_id, sku, title, end_reason, success, created_at)
                   VALUES (?, '999999999999', 'stock:01', 'T', 'Incorrect', 1,
                           datetime('now', '-2 days'))""",
                ("666666666666",),
            )

        from tasks.task_ebaymag_relist import _select_ebaymag_relist_targets
        # cooldown_days=10 → 2 日前は除外
        targets = _select_ebaymag_relist_targets(limit=10, cooldown_days=10)
        eids = {t["ebay_item_id"] for t in targets}
        assert "666666666666" not in eids


# ──────────────────────────────────────────────────────────────
# テスト C: 途中失敗 → needs_manual (Discord 通知)
# ──────────────────────────────────────────────────────────────

class TestFailurePaths:
    def test_end_item_failure_returns_error(self, monkeypatch):
        """EndItem が失敗した場合は success=False で即 return (needs_manual Discord 不要)。"""
        from monitor.database import init_db
        init_db()
        _seed_listing("eid_end_fail", segment="全国")

        target = {
            "ebay_item_id": "eid_end_fail",
            "sku": "stock:01",
            "title": "Test",
            "current_price": 100.0,
            "ebaymag_segment": "全国",
            "ebaymag_desired_sites_json": '["UK"]',
        }

        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.end_item",
            lambda *a, **kw: {"success": False, "message": "EndItem API エラー"},
        )
        discord_calls: list[str] = []
        monkeypatch.setattr(
            "tasks.task_ebaymag_relist._discord_notify",
            lambda config, msg: discord_calls.append(msg),
        )

        from tasks.task_ebaymag_relist import _process_single_relist
        res = _process_single_relist(target, _DUMMY_CREDS, _BASE_CONFIG)

        assert res["success"] is False
        assert res["new_item_id"] is None
        assert "EndItem" in res["error_message"]
        # EndItem 失敗は listing が生存しているので Discord 通知不要
        assert not discord_calls

    def test_relist_all_attempts_fail_notifies_discord(self, monkeypatch):
        """Relist 全試行失敗 → success=False + Discord 通知 (needs_manual)。"""
        from monitor.database import init_db
        init_db()

        target = {
            "ebay_item_id": "eid_relist_fail",
            "sku": "stock:01",
            "title": "Test Relist Fail",
            "current_price": 100.0,
            "ebaymag_segment": "全国",
            "ebaymag_desired_sites_json": '["UK"]',
        }

        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.end_item",
            lambda *a, **kw: {"success": True},
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.relist_item",
            lambda *a, **kw: {"success": False, "message": "Relist API エラー"},
        )
        # time.sleep をスキップ
        monkeypatch.setattr("tasks.task_ebaymag_relist.time.sleep", lambda s: None)

        discord_calls: list[str] = []
        monkeypatch.setattr(
            "tasks.task_ebaymag_relist._discord_notify",
            lambda config, msg: discord_calls.append(msg),
        )

        from tasks.task_ebaymag_relist import _process_single_relist
        res = _process_single_relist(target, _DUMMY_CREDS, _BASE_CONFIG)

        assert res["success"] is False
        assert res["new_item_id"] is None
        # needs_manual の Discord 通知が発行されること
        assert len(discord_calls) == 1
        assert "needs_manual" in discord_calls[0]


# ──────────────────────────────────────────────────────────────
# テスト D: relist 成功 → discover 未発見 → Phase2 委譲
# ──────────────────────────────────────────────────────────────

class TestDiscoverDelegated:
    def test_discover_not_found_enqueues_phase2(self, monkeypatch):
        """discover 未発見 → enqueue_ebaymag_apply(reason='relist_relink') が呼ばれる。"""
        from monitor.database import init_db, get_conn
        init_db()
        _seed_listing("eid_discover_miss", title="Product X", segment="全国")

        target = {
            "ebay_item_id": "eid_discover_miss",
            "sku": "stock:01",
            "title": "Product X",
            "current_price": 100.0,
            "ebaymag_segment": "全国",
            "ebaymag_desired_sites_json": '["UK", "DE"]',
        }

        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.end_item",
            lambda *a, **kw: {"success": True},
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.relist_item",
            lambda *a, **kw: {"success": True, "new_item_id": "new_eid_111"},
        )
        monkeypatch.setattr("tasks.task_ebaymag_relist.time.sleep", lambda s: None)

        # inherit_listing_on_relist をモック (DB 副作用を避けつつ成功させる)
        def _fake_inherit(old_item_id, new_item_id, sku, title, current_price, end_reason):
            # 新 item_id を ebay_listings に INSERT (desired_sites_json 継承テスト用)
            from monitor.database import get_conn
            with get_conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO ebay_listings
                       (ebay_item_id, sku, title, ebaymag_segment, current_price, last_synced_at)
                       VALUES (?, 'stock:01', ?, '全国', 100.0, CURRENT_TIMESTAMP)""",
                    (new_item_id, title),
                )
            return {"inherited_columns": 1, "competitor_rows": 0, "supplier_rows": 0,
                    "monitored_rows": 0, "note_rows": 0, "keyword_watch_rows": 0}

        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.inherit_listing_on_relist",
            _fake_inherit,
        )

        # discover → 未発見
        fake_disc = MagicMock()
        fake_disc.ok = False
        fake_disc.product_id = None
        fake_disc.error = "候補なし"
        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.discover_product_id",
            lambda query, expected_itm: fake_disc,
        )

        monkeypatch.setattr(
            "tasks.task_ebaymag_relist._discord_notify",
            lambda config, msg: None,
        )

        enqueue_calls: list[tuple] = []
        original_enqueue = None
        from monitor.database import enqueue_ebaymag_apply as _orig
        original_enqueue = _orig

        def _fake_enqueue(eid, reason):
            enqueue_calls.append((eid, reason))
            _orig(eid, reason)

        monkeypatch.setattr("tasks.task_ebaymag_relist.enqueue_ebaymag_apply", _fake_enqueue)

        from tasks.task_ebaymag_relist import _process_single_relist
        res = _process_single_relist(target, _DUMMY_CREDS, _BASE_CONFIG)

        # discover 失敗 → Phase2 委譲
        assert res["discover_delegated"] is True
        assert res["new_item_id"] == "new_eid_111"
        # enqueue_ebaymag_apply が reason='relist_relink' で呼ばれた
        assert any(eid == "new_eid_111" and reason == "relist_relink"
                   for eid, reason in enqueue_calls)

        # ebaymag_apply_queue に job が積まれている
        with get_conn() as conn:
            row = conn.execute(
                "SELECT reason FROM ebaymag_apply_queue WHERE ebay_item_id='new_eid_111'",
            ).fetchone()
        assert row is not None
        assert row["reason"] == "relist_relink"


# ──────────────────────────────────────────────────────────────
# テスト E: relist 成功 → discover 成功 → apply → success=True
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# テスト F: TASK_SCHEDULE 登録確認
# ──────────────────────────────────────────────────────────────

class TestTaskScheduleRegistration:
    def test_ebaymag_relist_registered_in_task_schedule(self):
        """TASK_SCHEDULE に ebaymag_relist が登録されている。"""
        from monitor.task_execution_log import TASK_SCHEDULE_BY_KEY
        assert "ebaymag_relist" in TASK_SCHEDULE_BY_KEY, (
            "ebaymag_relist が TASK_SCHEDULE に未登録"
        )
        entry = TASK_SCHEDULE_BY_KEY["ebaymag_relist"]
        assert entry["owner"] == "ebaymag_apply"
        # flag OFF 既定なので hours は存在するが実行は skip される
        assert 11 in entry["hours"]


class TestFullSuccess:
    def test_full_success_flow(self, monkeypatch):
        """relist→inherit→discover→apply が全成功 → success=True + sites_applied 返却。"""
        from monitor.database import init_db, get_conn
        init_db()
        _seed_listing("eid_full_success", title="Product Y", segment="優先国",
                      desired_sites=["UK", "DE"])

        target = {
            "ebay_item_id": "eid_full_success",
            "sku": "stock:01",
            "title": "Product Y",
            "current_price": 200.0,
            "ebaymag_segment": "優先国",
            "ebaymag_desired_sites_json": '["UK", "DE"]',
        }

        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.end_item",
            lambda *a, **kw: {"success": True},
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.relist_item",
            lambda *a, **kw: {"success": True, "new_item_id": "new_eid_222"},
        )
        monkeypatch.setattr("tasks.task_ebaymag_relist.time.sleep", lambda s: None)

        def _fake_inherit(old_item_id, new_item_id, sku, title, current_price, end_reason):
            from monitor.database import get_conn
            with get_conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO ebay_listings
                       (ebay_item_id, sku, title, ebaymag_segment,
                        ebaymag_desired_sites_json, current_price, last_synced_at)
                       VALUES (?, 'stock:01', ?, '優先国', '["UK","DE"]', 200.0, CURRENT_TIMESTAMP)""",
                    (new_item_id, title),
                )
                conn.execute(
                    """INSERT INTO relist_history
                       (old_item_id, new_item_id, sku, title, end_reason, success)
                       VALUES (?, ?, 'stock:01', ?, 'Incorrect', 1)""",
                    (old_item_id, new_item_id, title),
                )
            return {"inherited_columns": 1, "competitor_rows": 0, "supplier_rows": 0,
                    "monitored_rows": 0, "note_rows": 0, "keyword_watch_rows": 0}

        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.inherit_listing_on_relist",
            _fake_inherit,
        )

        # discover → 成功
        fake_disc = MagicMock()
        fake_disc.ok = True
        fake_disc.product_id = "prod_abc123"
        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.discover_product_id",
            lambda query, expected_itm: fake_disc,
        )

        # fetch_site_states → 現在 UK=True, DE=False (DE が希望なので turn_on)
        fake_fetch = MagicMock()
        fake_fetch.ok = True
        fake_fetch.site_states = {"UK": True, "DE": False, "AU": False, "FR": False,
                                  "IT": False, "ES": False, "CA": False}
        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.fetch_site_states",
            lambda product_id, expected_itm: fake_fetch,
        )

        # apply_site_changes → 成功
        fake_apply = MagicMock()
        fake_apply.ok = True
        fake_apply.site_states = {"UK": True, "DE": True, "AU": False, "FR": False,
                                  "IT": False, "ES": False, "CA": False}
        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.apply_site_changes",
            lambda product_id, eid, turn_on, turn_off: fake_apply,
        )

        monkeypatch.setattr(
            "tasks.task_ebaymag_relist.SITE_MAP",
            {"UK": "...", "DE": "...", "AU": "...", "FR": "...",
             "IT": "...", "ES": "...", "CA": "..."},
        )

        monkeypatch.setattr(
            "tasks.task_ebaymag_relist._discord_notify",
            lambda config, msg: None,
        )

        from tasks.task_ebaymag_relist import _process_single_relist
        res = _process_single_relist(target, _DUMMY_CREDS, _BASE_CONFIG)

        assert res["success"] is True
        assert res["new_item_id"] == "new_eid_222"
        assert "UK" in res["sites_applied"]
        assert "DE" in res["sites_applied"]
        assert res["discover_delegated"] is False

        # ebaymag_products に新 item_id が登録されている
        from monitor.database import get_ebaymag_product
        mapping = get_ebaymag_product("new_eid_222")
        assert mapping is not None
        assert mapping["product_id"] == "prod_abc123"

    def test_run_ebaymag_relist_no_targets(self, monkeypatch):
        """対象 listing がない場合は success=True, skipped_reason='no_targets'。"""
        from monitor.database import init_db
        init_db()
        # rank='A' の listing のみ (relist 対象外)
        _seed_listing("eid_rank_a", segment="全国", rank="A")

        monkeypatch.setattr(
            "tasks.task_ebaymag_relist._probe_cdp_ebaymag",
            lambda: (True, ""),
        )
        monkeypatch.setattr(
            "tasks.task_ebaymag_relist._get_ebay_credentials",
            lambda config: _DUMMY_CREDS,
        )

        from tasks.task_ebaymag_relist import run_ebaymag_relist
        result = run_ebaymag_relist(_BASE_CONFIG)

        assert result["success"] is True
        assert result["skipped_reason"] == "no_targets"
        assert result["processed"] == 0


def test_relist_records_history_for_cooldown(monkeypatch):
    """HIGH-1 回帰 (code-reviewer 2026-06-20): relist 成功で relist_history に old/new が
    記録され、新 item_id が cooldown 除外される。inherit は履歴を書かない(本番挙動)前提で、
    task 側 record_relist が cooldown を成立させる責務を検証 (未記録だと多重relist暴走)。"""
    import tasks.task_ebaymag_relist as mod
    from monitor.database import get_conn

    _seed_listing("eid_cd_old", segment="全国", desired_sites=[])
    target = {
        "ebay_item_id": "eid_cd_old", "sku": "stock:01", "title": "CD Test",
        "current_price": 100.0, "ebaymag_segment": "全国",
        "ebaymag_desired_sites_json": "[]",
    }

    monkeypatch.setattr(mod, "end_item", lambda *a, **kw: {"success": True})
    monkeypatch.setattr(mod, "relist_item",
                        lambda *a, **kw: {"success": True, "new_item_id": "eid_cd_new"})
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod, "_discord_notify", lambda c, m: None)

    def _inherit_no_history(old_item_id, new_item_id, **kw):
        # 本番 inherit_listing_on_relist は relist_history を書かない (継承のみ) — 忠実再現
        with get_conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO ebay_listings "
                "(ebay_item_id, sku, title, ebaymag_segment, rank, quantity_ebay, "
                " watch_count, is_ended, ebaymag_desired_sites_json, current_price, "
                " last_synced_at) VALUES (?, 'stock:01', ?, '全国', 'E', 1, 0, 0, '[]', "
                "100.0, CURRENT_TIMESTAMP)",
                (new_item_id, kw.get("title", "")),
            )
    monkeypatch.setattr(mod, "inherit_listing_on_relist", _inherit_no_history)

    class _FakeDisc:
        ok = True
        error = ""
        product_id = "PD_CD"
        site_states: dict = {}
    monkeypatch.setattr(mod, "discover_product_id", lambda q, expected_itm: _FakeDisc())

    res = mod._process_single_relist(target, _DUMMY_CREDS, _BASE_CONFIG)
    assert res["success"] is True

    with get_conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM relist_history WHERE old_item_id='eid_cd_old' "
            "AND new_item_id='eid_cd_new' AND success=1"
        ).fetchone()[0]
    assert n == 1, "relist_history 未記録 → cooldown不発で多重relist暴走 (HIGH-1)"

    eids = {t["ebay_item_id"]
            for t in mod._select_ebaymag_relist_targets(limit=10, cooldown_days=10)}
    assert "eid_cd_new" not in eids, "新item_idがcooldown除外されず即再relist対象 (HIGH-1)"
