# -*- coding: utf-8 -*-
"""W284 Phase2-3: eBaymag 送料ポリシー付替 (assign_policy) のユニットテスト。

設計書: .company/engineering/docs/2026-06-21-ebaymag-shipping-policy-automation-design.md
  §15 HIGH-1 (状態駆動 2 軸) / HIGH-2 (案b policy 信号を列で) / §8 ライフサイクル。

テスト対象 (2026-07-05 軸2 を本番稼働中の MAG_ 方式へ統一 — token = `MAG_{band}_{1day|7day}`、
dispatch 真実源 = US 本体 listing の DispatchTimeMax。旧 DDP_ token / ebaymag_shipping_policies
テーブルは残置 = class A は後方互換テストのみ):
  A. 旧 get_canonical_policy_token の後方互換 (残置関数、本番の解決は _resolve_mag_policy_token)
  B. 状態駆動の付替要否 (applied != MAG_ token で assign、一致なら no-op)
  C. desired_sites 空 (国未指定) では国トグルを一切走らせない (HIGH-1 二次災害防止)
  D. shipping_policy reason でも国は desired==実態なら no-op
  E. US 発送日数 (DispatchTimeMax) 取得失敗 → needs_manual + 通知 (Q0 fail-closed、SKU に
     silent フォールバックしない)
  F. feature flag OFF 時は付替 skip (痕跡ログのみ、CDP 付替 / US GetItem しない)
  G. band 設定+enqueue の冪等性 (同一トランザクション)
  H. weight 変更ライフサイクルフック (旧 band≠新 band で enqueue)
  I. _resolve_mag_policy_token 単体 (US=1day/7day、SKU conflict でも US 優先、取得失敗)
  J. MAG title GraphQL 事前確認 (候補 token 不在 → needs_manual)

識別キー: ebay_item_id (SKU 禁止、conftest の DB 隔離適用)。
mutation (eBaymag/CDP) は全て mock。本番 mutation は一切しない。
"""
from __future__ import annotations

import json

import pytest


# ──────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────

def _seed_listing(
    eid: str,
    title: str = "Test Item",
    *,
    segment: str | None = None,
    desired_sites: list | None = None,  # None = desired 未設定 (国軸 走らせない)
    band: str | None = None,
    applied_token: str | None = None,
    weight_g: float | None = None,
    sku: str = "stock:01",  # 既定 stock%→dispatch=1day (MAG_ token 解決の在庫種別判定)
) -> None:
    from monitor.database import init_db, get_conn
    init_db()
    sites_json = json.dumps(desired_sites) if desired_sites is not None else None
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO ebay_listings
                (ebay_item_id, title, sku, ebaymag_segment,
                 ebaymag_desired_sites_json, ebaymag_desired_updated_at,
                 ebaymag_shipping_band, ebaymag_applied_policy_token, weight_g)
            VALUES (?, ?, ?, ?, ?,
                    CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END,
                    ?, ?, ?)
            """,
            (eid, title, sku, segment, sites_json, sites_json, band, applied_token, weight_g),
        )


def _seed_policy(band: str, token: str | None, status: str = "live") -> None:
    from monitor.database import init_db, get_conn
    init_db()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO ebaymag_shipping_policies
                (band, policy_title, ebaymag_policy_token, status,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (band, f"DDP_{band}", token, status),
        )


def _seed_queue_job(eid: str, reason: str = "shipping_policy") -> int:
    from monitor.database import init_db, enqueue_ebaymag_apply, get_conn
    init_db()
    enqueue_ebaymag_apply(eid, reason)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM ebaymag_apply_queue WHERE ebay_item_id=? AND status='pending'",
            (eid,),
        ).fetchone()
    assert row is not None
    return row["id"]


def _get_job(job_id: int) -> dict:
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ebaymag_apply_queue WHERE id=?", (job_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


def _mock_cdp_alive(monkeypatch):
    monkeypatch.setattr(
        "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag", lambda: (True, "")
    )
    monkeypatch.setattr(
        "tasks.task_ebaymag_apply_queue._discord_notify", lambda config, msg, **kwargs: None
    )
    # MAG title GraphQL 事前確認は本ファイルの大半のテストでは対象外 (fail-open =
    # 存在チェック skip、既存の候補 token をそのまま使う)。GraphQL 確認そのものの
    # 挙動は TestMagTitleGraphQLValidation で個別に検証する。
    monkeypatch.setattr(
        "tasks.task_ebaymag_apply_queue._build_mag_titles_if_needed",
        lambda config: None,
    )


def _mock_us_dispatch(monkeypatch, series="1day", sku="stock:01", err=None):
    """US 本体 DispatchTimeMax (dispatch 真実源) の GetItem 経路を mock する。

    _resolve_mag_policy_token は inventory_sync._get_credentials +
    ebaymag_dispatch_mirror.us_info を関数内 import するため source module 側を patch。
    series=None + err で「US 取得失敗」(fail-closed → needs_manual) を模擬できる。
    """
    monkeypatch.setattr(
        "monitor.inventory_sync._get_credentials",
        lambda: ("app", "dev", "cert", "tok"),
    )
    monkeypatch.setattr(
        "monitor.ebaymag_dispatch_mirror.us_info",
        lambda item_id, creds: (series, sku, err),
    )


# ──────────────────────────────────────────────────────────────
# A. band → token 解決 (旧 DDP_ 方式)
# ──────────────────────────────────────────────────────────────

class TestBandTokenResolve:
    """残置関数 get_canonical_policy_token の後方互換テスト
    (本番の解決は _resolve_mag_policy_token = MAG_ 方式、本関数は消化経路から不使用)。"""
    def test_live_token_preferred(self):
        from monitor.database import get_canonical_policy_token
        _seed_policy("6-8kg", "DRAFT_TOKEN", status="draft")
        _seed_policy("6-8kg", "LIVE_TOKEN", status="live")
        assert get_canonical_policy_token("6-8kg") == "LIVE_TOKEN"

    def test_draft_fallback_when_no_live(self):
        from monitor.database import get_canonical_policy_token
        _seed_policy("2-3kg", "DRAFT_ONLY", status="draft")
        assert get_canonical_policy_token("2-3kg") == "DRAFT_ONLY"

    def test_null_token_returns_none(self):
        from monitor.database import get_canonical_policy_token
        _seed_policy("1-2kg", None, status="draft")
        assert get_canonical_policy_token("1-2kg") is None

    def test_unknown_band_returns_none(self):
        from monitor.database import init_db, get_canonical_policy_token
        init_db()
        assert get_canonical_policy_token("nonexistent") is None
        assert get_canonical_policy_token("") is None


# ──────────────────────────────────────────────────────────────
# B. 状態駆動の付替要否
# ──────────────────────────────────────────────────────────────

class TestPolicyAssignDecision:
    def test_assign_when_applied_differs_from_live(self, monkeypatch):
        """band から解決した MAG_ token != applied token かつ flag ON →
        assign_policy 実行 + done。US DispatchTimeMax=1 (真実源) → dispatch=1day。"""
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "100000000001"
        _seed_listing(eid, band="6-8kg", applied_token="OLD_TOKEN")  # desired 未設定
        job_id = _seed_queue_job(eid)
        upsert_ebaymag_product(eid, product_id="prod_1", site_states={"UK": True})

        _mock_cdp_alive(monkeypatch)
        _mock_us_dispatch(monkeypatch, series="1day")

        assign_calls = []
        from monitor.ebaymag_driver import EbaymagResult

        def _fake_assign(product_id, expected_itm, target_policy_token):
            assign_calls.append(target_policy_token)
            r = EbaymagResult()
            r.ok = True
            return r

        monkeypatch.setattr("monitor.ebaymag_driver.assign_policy", _fake_assign)

        cfg = {"tasks_enabled": {"ebaymag_policy_assign": {"enabled": True}}}
        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue(cfg)

        assert assign_calls == ["MAG_6-8kg_1day"]
        assert result["applied"] == 1
        job = _get_job(job_id)
        assert job["status"] == "done"

        # applied_token が MAG_ token に更新され、次回 no-op になること
        from monitor.database import get_ebaymag_policy_state
        st = get_ebaymag_policy_state(eid)
        assert st["applied_token"] == "MAG_6-8kg_1day"

    def test_no_assign_when_applied_equals_live(self, monkeypatch):
        """applied == 解決済み MAG_ token → 付替不要 (assign 呼ばず no_change)。"""
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "100000000002"
        _seed_listing(eid, band="6-8kg", applied_token="MAG_6-8kg_1day")
        job_id = _seed_queue_job(eid)
        upsert_ebaymag_product(eid, product_id="prod_2", site_states={"UK": True})

        _mock_cdp_alive(monkeypatch)
        _mock_us_dispatch(monkeypatch, series="1day")

        assign_calls = []
        monkeypatch.setattr(
            "monitor.ebaymag_driver.assign_policy",
            lambda *a, **k: assign_calls.append(a),
        )

        cfg = {"tasks_enabled": {"ebaymag_policy_assign": {"enabled": True}}}
        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue(cfg)

        assert assign_calls == [], "applied==live なら assign を呼ばない"
        job = _get_job(job_id)
        assert job["status"] == "done"


# ──────────────────────────────────────────────────────────────
# C. desired_sites 空 (国未指定) では国トグルしない (HIGH-1 二次災害防止)
# ──────────────────────────────────────────────────────────────

class TestNoSiteToggleWhenDesiredUnset:
    def test_shipping_policy_job_does_not_toggle_sites(self, monkeypatch):
        """band のみ設定 / desired 未設定の listing で fetch_site_states/apply_site_changes
        を一切呼ばない (国軸を走らせない = 全 OFF 出品消失を防ぐ)。"""
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "100000000003"
        _seed_listing(eid, band="6-8kg", applied_token=None)  # desired 未設定
        job_id = _seed_queue_job(eid, reason="shipping_policy")
        upsert_ebaymag_product(eid, product_id="prod_3", site_states={"UK": True})

        _mock_cdp_alive(monkeypatch)
        _mock_us_dispatch(monkeypatch, series="1day")

        fetch_calls, apply_calls, assign_calls = [], [], []
        from monitor.ebaymag_driver import EbaymagResult

        def _fake_fetch(*a, **k):
            fetch_calls.append(a)
            r = EbaymagResult(); r.ok = True; r.site_states = {"UK": True}
            return r

        def _fake_apply(*a, **k):
            apply_calls.append(a)
            r = EbaymagResult(); r.ok = True; r.site_states = {"UK": True}
            return r

        def _fake_assign(product_id, expected_itm, target_policy_token):
            assign_calls.append(target_policy_token)
            r = EbaymagResult(); r.ok = True
            return r

        monkeypatch.setattr("monitor.ebaymag_driver.fetch_site_states", _fake_fetch)
        monkeypatch.setattr("monitor.ebaymag_driver.apply_site_changes", _fake_apply)
        monkeypatch.setattr("monitor.ebaymag_driver.assign_policy", _fake_assign)

        cfg = {"tasks_enabled": {"ebaymag_policy_assign": {"enabled": True}}}
        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        run_ebaymag_apply_queue(cfg)

        assert fetch_calls == [], "desired 未設定で fetch_site_states を呼んではならない"
        assert apply_calls == [], "desired 未設定で apply_site_changes を呼んではならない"
        assert assign_calls == ["MAG_6-8kg_1day"], "送料軸 (assign) は走るべき"
        job = _get_job(job_id)
        assert job["status"] == "done"


# ──────────────────────────────────────────────────────────────
# D. desired 設定済だが実態一致 → 国トグルしない (shipping_policy 起因でも no-op)
# ──────────────────────────────────────────────────────────────

class TestNoSiteToggleWhenAlreadyMatch:
    def test_desired_matches_actual_no_apply(self, monkeypatch):
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "100000000004"
        _seed_listing(eid, desired_sites=["UK"], band="6-8kg", applied_token=None)
        _seed_queue_job(eid, reason="shipping_policy")
        upsert_ebaymag_product(eid, product_id="prod_4", site_states={"UK": True})

        _mock_cdp_alive(monkeypatch)
        _mock_us_dispatch(monkeypatch, series="1day")

        apply_calls, assign_calls = [], []
        from monitor.ebaymag_driver import EbaymagResult

        def _fake_fetch(*a, **k):
            r = EbaymagResult(); r.ok = True
            r.site_states = {"UK": True, "DE": False, "FR": False, "IT": False,
                             "ES": False, "CA": False, "AU": False}
            return r

        monkeypatch.setattr("monitor.ebaymag_driver.fetch_site_states", _fake_fetch)
        monkeypatch.setattr(
            "monitor.ebaymag_driver.apply_site_changes",
            lambda *a, **k: apply_calls.append(a),
        )

        def _fake_assign(product_id, expected_itm, target_policy_token):
            assign_calls.append(target_policy_token)
            r = EbaymagResult(); r.ok = True
            return r

        monkeypatch.setattr("monitor.ebaymag_driver.assign_policy", _fake_assign)

        cfg = {"tasks_enabled": {"ebaymag_policy_assign": {"enabled": True}}}
        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        run_ebaymag_apply_queue(cfg)

        assert apply_calls == [], "desired==実態なら国トグルを呼ばない"
        assert assign_calls == ["MAG_6-8kg_1day"], "送料軸は走る"


# ──────────────────────────────────────────────────────────────
# E. US 発送日数 (DispatchTimeMax) 取得失敗 → needs_manual + 通知 (Q0 fail-closed)
# ──────────────────────────────────────────────────────────────

class TestUsDispatchFetchFailure:
    def test_us_fetch_failure_needs_manual(self, monkeypatch):
        """US 本体 DispatchTimeMax (dispatch 真実源) が取得できない → SKU に silent
        フォールバックせず fail-closed → needs_manual + 通知 (Q0)。"""
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "100000000005"
        _seed_listing(eid, band="6-8kg", applied_token=None)  # desired 未設定
        job_id = _seed_queue_job(eid)
        upsert_ebaymag_product(eid, product_id="prod_5", site_states={"UK": True})

        _mock_cdp_alive(monkeypatch)
        # GetItem 失敗 (series=None + err) を模擬
        _mock_us_dispatch(monkeypatch, series=None, err="GetItem失敗: Invalid item ID")
        notify_calls = []
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify",
            lambda config, msg, **kwargs: notify_calls.append(msg),
        )
        assign_calls = []
        monkeypatch.setattr(
            "monitor.ebaymag_driver.assign_policy",
            lambda *a, **k: assign_calls.append(a),
        )

        cfg = {"tasks_enabled": {"ebaymag_policy_assign": {"enabled": True}}}
        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue(cfg)

        assert assign_calls == [], "US dispatch 不明では付替を試みない (fail-closed / Q0)"
        assert result["failed"] == 1
        job = _get_job(job_id)
        assert job["status"] == "needs_manual"
        assert "US 発送日数" in (job["last_error"] or "")
        assert len(notify_calls) >= 1

    def test_credentials_missing_needs_manual_reason(self, monkeypatch):
        """eBay 認証情報なし → (None, 'US 発送日数を確認できないため保留 ...')。"""
        from monitor.database import init_db
        from tasks.task_ebaymag_apply_queue import _resolve_mag_policy_token
        init_db()
        monkeypatch.setattr("monitor.inventory_sync._get_credentials", lambda: None)
        token, reason = _resolve_mag_policy_token("100000000098", "6-8kg")
        assert token is None
        assert "US 発送日数" in reason


# ──────────────────────────────────────────────────────────────
# F. feature flag OFF → 付替 skip (痕跡のみ、CDP 付替しない)
# ──────────────────────────────────────────────────────────────

class TestFeatureFlagOff:
    def test_flag_off_skips_assign(self, monkeypatch):
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "100000000006"
        _seed_listing(eid, band="6-8kg", applied_token="OLD")  # desired 未設定
        _seed_policy("6-8kg", "NEW", status="live")
        job_id = _seed_queue_job(eid)
        upsert_ebaymag_product(eid, product_id="prod_6", site_states={"UK": True})

        _mock_cdp_alive(monkeypatch)
        assign_calls = []
        monkeypatch.setattr(
            "monitor.ebaymag_driver.assign_policy",
            lambda *a, **k: assign_calls.append(a),
        )

        # flag 省略 = OFF (fail-safe)
        cfg = {"tasks_enabled": {}}
        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        run_ebaymag_apply_queue(cfg)

        assert assign_calls == [], "flag OFF では assign_policy を呼ばない"
        job = _get_job(job_id)
        assert job["status"] == "done", "国軸 no-op + 送料 skip でも job は done"
        # applied_token は変えない (付替していないため)
        from monitor.database import get_ebaymag_policy_state
        assert get_ebaymag_policy_state(eid)["applied_token"] == "OLD"

    def test_policy_assign_enabled_helper(self):
        from tasks.task_ebaymag_apply_queue import _policy_assign_enabled
        assert _policy_assign_enabled({}) is False
        assert _policy_assign_enabled(
            {"tasks_enabled": {"ebaymag_policy_assign": {"enabled": True}}}
        ) is True
        assert _policy_assign_enabled(
            {"tasks_enabled": {"ebaymag_policy_assign": {"enabled": False}}}
        ) is False


# ──────────────────────────────────────────────────────────────
# G. band 設定+enqueue の冪等性 (同一トランザクション)
# ──────────────────────────────────────────────────────────────

class TestBandEnqueueIdempotent:
    def test_set_band_and_enqueue_creates_one_job(self):
        from monitor.database import (
            init_db, get_conn, set_ebaymag_shipping_band_and_enqueue,
        )
        init_db()
        eid = "100000000007"
        _seed_listing(eid)
        assert set_ebaymag_shipping_band_and_enqueue(eid, "6-8kg") is True
        # 2 回目: 既存 active job に集約 (重複 INSERT しない)
        assert set_ebaymag_shipping_band_and_enqueue(eid, "10-20kg") is True

        with get_conn() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) c FROM ebaymag_apply_queue WHERE ebay_item_id=?"
                " AND status NOT IN ('done','needs_manual')",
                (eid,),
            ).fetchone()["c"]
            band = conn.execute(
                "SELECT ebaymag_shipping_band b FROM ebay_listings WHERE ebay_item_id=?",
                (eid,),
            ).fetchone()["b"]
        assert cnt == 1, "active job は 1 件に集約される (冪等)"
        assert band == "10-20kg", "band は最新値に更新される"

    def test_set_band_missing_listing_returns_false(self):
        from monitor.database import init_db, set_ebaymag_shipping_band_and_enqueue
        init_db()
        assert set_ebaymag_shipping_band_and_enqueue("999999999999", "6-8kg") is False


# ──────────────────────────────────────────────────────────────
# H. weight 変更ライフサイクルフック
# ──────────────────────────────────────────────────────────────

class TestWeightLifecycleHook:
    def test_weight_change_sets_band_and_enqueues(self):
        from monitor.database import (
            init_db, get_conn, get_ebaymag_policy_state,
        )
        from monitor.ebaymag_policy_lifecycle import sync_shipping_band_for_listing
        init_db()
        eid = "100000000008"
        _seed_listing(eid, band=None)
        # 7000g → 6-8kg
        new_band = sync_shipping_band_for_listing(eid, 7000)
        assert new_band == "6-8kg"
        st = get_ebaymag_policy_state(eid)
        assert st["band"] == "6-8kg"
        with get_conn() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) c FROM ebaymag_apply_queue WHERE ebay_item_id=?"
                " AND reason='shipping_policy'",
                (eid,),
            ).fetchone()["c"]
        assert cnt == 1

    def test_no_change_when_band_same(self):
        from monitor.database import init_db, get_conn
        from monitor.ebaymag_policy_lifecycle import sync_shipping_band_for_listing
        init_db()
        eid = "100000000009"
        _seed_listing(eid, band="6-8kg")
        # 7000g も同じ 6-8kg → enqueue しない
        result = sync_shipping_band_for_listing(eid, 7000)
        assert result is None
        with get_conn() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) c FROM ebaymag_apply_queue WHERE ebay_item_id=?",
                (eid,),
            ).fetchone()["c"]
        assert cnt == 0, "band 変化なしなら enqueue しない"

    def test_weight_none_skips(self):
        from monitor.ebaymag_policy_lifecycle import sync_shipping_band_for_listing
        from monitor.database import init_db
        init_db()
        eid = "100000000010"
        _seed_listing(eid)
        assert sync_shipping_band_for_listing(eid, None) is None

    def test_invalid_weight_skips_with_warning(self, caplog):
        from monitor.ebaymag_policy_lifecycle import sync_shipping_band_for_listing
        from monitor.database import init_db
        init_db()
        eid = "100000000011"
        _seed_listing(eid)
        # 0 / 負値は band 不能 → None (silent skip せず warning)
        assert sync_shipping_band_for_listing(eid, 0) is None
        assert sync_shipping_band_for_listing(eid, -5) is None

    def test_update_weight_estimate_triggers_hook(self):
        """update_ebay_listing_weight_estimate 経由で band フックが発火すること。"""
        from monitor.database import (
            init_db, get_conn, update_ebay_listing_weight_estimate,
            get_ebaymag_policy_state,
        )
        init_db()
        eid = "100000000012"
        _seed_listing(eid, band=None)
        update_ebay_listing_weight_estimate(eid, 1500, "high")  # → 1-2kg
        st = get_ebaymag_policy_state(eid)
        assert st["band"] == "1-2kg"
        with get_conn() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) c FROM ebaymag_apply_queue WHERE ebay_item_id=?",
                (eid,),
            ).fetchone()["c"]
        assert cnt == 1


# ──────────────────────────────────────────────────────────────
# I. _resolve_mag_policy_token 単体 (US=1day/7day / conflict は US 優先 / 取得失敗)
# ──────────────────────────────────────────────────────────────

class TestResolveMagPolicyToken:
    """本番稼働中の MAG_ 方式への統一の単体テスト。
    dispatch 真実源 = US 本体 DispatchTimeMax (us_info 再利用)、SKU は監査のみ。"""

    def test_us_1day_resolves_1day(self, monkeypatch):
        from monitor.database import init_db
        from tasks.task_ebaymag_apply_queue import _resolve_mag_policy_token
        init_db()
        _mock_us_dispatch(monkeypatch, series="1day", sku="stock:01")
        token, reason = _resolve_mag_policy_token("200000000001", "6-8kg")
        assert (token, reason) == ("MAG_6-8kg_1day", None)

    def test_us_7day_resolves_7day(self, monkeypatch):
        from monitor.database import init_db
        from tasks.task_ebaymag_apply_queue import _resolve_mag_policy_token
        init_db()
        _mock_us_dispatch(monkeypatch, series="7day", sku="ebayyh_p1221413657")
        token, reason = _resolve_mag_policy_token("200000000002", "6-8kg")
        assert (token, reason) == ("MAG_6-8kg_7day", None)

    def test_axis2_dispatch_follows_us_not_sku_when_conflict(self, monkeypatch, caplog):
        """sku_conflict (US=1day・SKU=ebay%→rule 7day) では **US に従う** + warning 痕跡。

        SKU-series ≠ US-series は本番で実在する既知条件 (dispatch_mirror sku_conflicts
        追跡)。SKU を真実源にすると mirror (US 真実源) と逆ポリシーの綱引きになる。"""
        import logging
        from monitor.database import init_db
        from tasks.task_ebaymag_apply_queue import _resolve_mag_policy_token
        init_db()
        _mock_us_dispatch(monkeypatch, series="1day", sku="ebayyh_p1221413657")
        with caplog.at_level(logging.WARNING, logger="tasks.task_ebaymag_apply_queue"):
            token, reason = _resolve_mag_policy_token("200000000003", "6-8kg")
        assert (token, reason) == ("MAG_6-8kg_1day", None), "US 真実源に従う (SKU 無視)"
        assert any("sku_conflict" in r.message for r in caplog.records), \
            "SKU 矛盾の監査痕跡 (warning) を残す"

    def test_us_fetch_failure_returns_none_with_reason(self, monkeypatch):
        """US DispatchTimeMax 取得失敗 → fail-closed (None, 保留理由)。"""
        from monitor.database import init_db
        from tasks.task_ebaymag_apply_queue import _resolve_mag_policy_token
        init_db()
        _mock_us_dispatch(monkeypatch, series=None, err="DispatchTimeMax=3 (想定外、要user判断)")
        token, reason = _resolve_mag_policy_token("200000000004", "6-8kg")
        assert token is None
        assert "US 発送日数を確認できないため保留" in reason


# ──────────────────────────────────────────────────────────────
# J. MAG title GraphQL 事前確認 (fetch_mag_policy_titles モック)
# ──────────────────────────────────────────────────────────────

class TestMagTitleGraphQLValidation:
    """該当 MAG_ ポリシーが GraphQL に見つからない場合の解決失敗 → needs_manual (Q0)。"""

    def test_token_present_in_graphql_titles_resolves(self, monkeypatch):
        from monitor.database import init_db
        from tasks.task_ebaymag_apply_queue import _resolve_mag_policy_token
        init_db()
        _mock_us_dispatch(monkeypatch, series="1day")
        mag_titles = {"MAG_6-8kg_1day", "MAG_6-8kg_7day"}
        token, reason = _resolve_mag_policy_token("200000000005", "6-8kg", mag_titles)
        assert (token, reason) == ("MAG_6-8kg_1day", None)

    def test_token_absent_from_graphql_titles_returns_none(self, monkeypatch):
        """GraphQL 確認済 title 集合に候補 token が無い (= ポリシー未作成/削除) → None。"""
        from monitor.database import init_db
        from tasks.task_ebaymag_apply_queue import _resolve_mag_policy_token
        init_db()
        _mock_us_dispatch(monkeypatch, series="1day")
        mag_titles = {"MAG_2-3kg_1day", "MAG_2-3kg_7day"}  # 6-8kg 帯が無い
        token, reason = _resolve_mag_policy_token("200000000006", "6-8kg", mag_titles)
        assert token is None
        assert "MAG_ ポリシー不在" in reason

    def test_mag_titles_none_skips_existence_check(self, monkeypatch):
        """mag_titles=None (GraphQL 到達不能/未構築) → 存在チェック skip、候補 token を返す。"""
        from monitor.database import init_db
        from tasks.task_ebaymag_apply_queue import _resolve_mag_policy_token
        init_db()
        _mock_us_dispatch(monkeypatch, series="1day")
        token, reason = _resolve_mag_policy_token("200000000007", "6-8kg", None)
        assert (token, reason) == ("MAG_6-8kg_1day", None)

    def test_full_queue_needs_manual_when_graphql_confirms_title_missing(self, monkeypatch):
        """run_ebaymag_apply_queue 経由: GraphQL が候補 token 不在を確認 → needs_manual。"""
        from monitor.database import init_db, upsert_ebaymag_product
        init_db()
        eid = "200000000008"
        _seed_listing(eid, band="6-8kg", applied_token=None)
        job_id = _seed_queue_job(eid)
        upsert_ebaymag_product(eid, product_id="prod_7", site_states={"UK": True})

        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._probe_cdp_ebaymag", lambda: (True, "")
        )
        _mock_us_dispatch(monkeypatch, series="1day")
        notify_calls = []
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._discord_notify",
            lambda config, msg, **kwargs: notify_calls.append(msg),
        )
        # GraphQL 確認済だが 6-8kg 帯の MAG_ ポリシーが存在しない状況を模擬
        monkeypatch.setattr(
            "tasks.task_ebaymag_apply_queue._build_mag_titles_if_needed",
            lambda config: {"MAG_2-3kg_1day", "MAG_2-3kg_7day"},
        )
        assign_calls = []
        monkeypatch.setattr(
            "monitor.ebaymag_driver.assign_policy",
            lambda *a, **k: assign_calls.append(a),
        )

        cfg = {"tasks_enabled": {"ebaymag_policy_assign": {"enabled": True}}}
        from tasks.task_ebaymag_apply_queue import run_ebaymag_apply_queue
        result = run_ebaymag_apply_queue(cfg)

        assert assign_calls == [], "GraphQL で不在確認済の token では付替を試みない (Q0)"
        assert result["failed"] == 1
        job = _get_job(job_id)
        assert job["status"] == "needs_manual"
        assert "MAG_ ポリシー不在" in (job["last_error"] or "")
        assert len(notify_calls) >= 1
