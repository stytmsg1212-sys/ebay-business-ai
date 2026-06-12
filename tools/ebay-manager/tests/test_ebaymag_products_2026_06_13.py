# -*- coding: utf-8 -*-
"""ebaymag_products (v75) + ebaymag_driver の回帰テスト (依頼ボード#10).

DB 層 (upsert / get / record_apply) と driver の純粋ロジック
(_label_is_on / _states_from_verify / 入力検証) を検証。
CDP/playwright 実機経路は Q1 実機 verify で別途 (テストでは触らない)。
"""
from __future__ import annotations

from monitor.database import (
    get_conn,
    get_ebaymag_product,
    get_ebaymag_products,
    init_db,
    record_ebaymag_apply,
    upsert_ebaymag_product,
)
from monitor import ebaymag_driver


class TestEbaymagProductsDb:
    def test_upsert_and_get_roundtrip(self):
        init_db()
        upsert_ebaymag_product("351234567890", "718000001",
                               {"UK": True, "DE": False})
        row = get_ebaymag_product("351234567890")
        assert row is not None
        assert row["product_id"] == "718000001"
        assert row["site_states"] == {"UK": True, "DE": False}
        assert row["last_synced_at"] is not None

    def test_get_missing_returns_none(self):
        init_db()
        assert get_ebaymag_product("000000000000") is None

    def test_upsert_without_states_keeps_existing_states(self):
        """states=None の upsert (mapping 更新のみ) が既存キャッシュを消さない."""
        init_db()
        upsert_ebaymag_product("351234567891", "718000002", {"FR": True})
        upsert_ebaymag_product("351234567891", "718000099", None)
        row = get_ebaymag_product("351234567891")
        assert row["product_id"] == "718000099"
        assert row["site_states"] == {"FR": True}  # COALESCE で温存
        assert row["last_synced_at"] is not None   # 温存 (新規 sync ではない)

    def test_upsert_requires_ids(self):
        init_db()
        import pytest
        with pytest.raises(ValueError):
            upsert_ebaymag_product("", "718000001")
        with pytest.raises(ValueError):
            upsert_ebaymag_product("351234567890", "")

    def test_get_many_with_filter(self):
        init_db()
        upsert_ebaymag_product("351111111111", "1")
        upsert_ebaymag_product("352222222222", "2")
        all_rows = get_ebaymag_products()
        assert {r["ebay_item_id"] for r in all_rows} >= {
            "351111111111", "352222222222"}
        sub = get_ebaymag_products(["351111111111"])
        assert [r["ebay_item_id"] for r in sub] == ["351111111111"]

    def test_record_apply_updates_result_and_states(self):
        init_db()
        upsert_ebaymag_product("353333333333", "3", {"UK": False})
        ok = record_ebaymag_apply("353333333333", "ok", {"UK": True, "CA": True})
        assert ok is True
        row = get_ebaymag_product("353333333333")
        assert row["last_apply_result"] == "ok"
        assert row["last_applied_at"] is not None
        assert row["site_states"] == {"UK": True, "CA": True}

    def test_record_apply_failure_keeps_states(self):
        """失敗記録 (states=None) は state キャッシュを汚さない."""
        init_db()
        upsert_ebaymag_product("354444444444", "4", {"DE": True})
        ok = record_ebaymag_apply("354444444444", "itm 照合 NG", None)
        assert ok is True
        row = get_ebaymag_product("354444444444")
        assert row["last_apply_result"] == "itm 照合 NG"
        assert row["site_states"] == {"DE": True}

    def test_record_apply_unknown_listing_returns_false(self):
        init_db()
        assert record_ebaymag_apply("999999999999", "ok") is False

    def test_bad_json_states_returns_empty_dict(self):
        """site_states_json 不正値は {} に落とす (Q0: 例外で UI を殺さない)."""
        init_db()
        upsert_ebaymag_product("355555555555", "5")
        with get_conn() as conn:
            conn.execute(
                "UPDATE ebaymag_products SET site_states_json='not-json' "
                "WHERE ebay_item_id='355555555555'")
        row = get_ebaymag_product("355555555555")
        assert row["site_states"] == {}

    def test_init_db_idempotent_keeps_rows(self):
        """Q2: init_db 再実行でデータ保持 (v75 冪等性)."""
        init_db()
        upsert_ebaymag_product("356666666666", "6", {"AU": True})
        init_db()
        row = get_ebaymag_product("356666666666")
        assert row is not None and row["site_states"] == {"AU": True}


class TestDriverPureLogic:
    def test_label_is_on(self):
        assert ebaymag_driver._label_is_on("リストされている") is True
        assert ebaymag_driver._label_is_on("掲載されている") is True
        assert ebaymag_driver._label_is_on("完売") is True  # qty=0 で ON 定着
        assert ebaymag_driver._label_is_on("掲載されていません") is False
        assert ebaymag_driver._label_is_on(None) is False
        assert ebaymag_driver._label_is_on("") is False

    def test_states_from_verify_maps_domains_to_codes(self):
        verify = {"sites": [
            {"site": "ebay.co.uk", "on": "リストされている"},
            {"site": "ebay.com.au", "on": "掲載されていません"},
            {"site": "ebay.de", "on": "完売"},
            {"site": "ebay.com", "on": "リストされている"},  # US は対象外
            {"site": "unknown.example", "on": "リストされている"},
        ]}
        states = ebaymag_driver._states_from_verify(verify)
        assert states == {"UK": True, "AU": False, "DE": True}

    def test_apply_rejects_unknown_site_code(self):
        res = ebaymag_driver.apply_site_changes(
            "718000001", expected_itm="351234567890",
            turn_on=["US"], turn_off=[])  # US はトグル対象外
        assert res.ok is False
        assert "未知の site code" in (res.error or "")

    def test_apply_rejects_empty_changes(self):
        res = ebaymag_driver.apply_site_changes(
            "718000001", expected_itm="351234567890", turn_on=[], turn_off=[])
        assert res.ok is False
        assert "変更対象なし" in (res.error or "")

    def test_fetch_without_playwright_returns_error(self, monkeypatch):
        monkeypatch.setattr(ebaymag_driver, "PLAYWRIGHT_AVAILABLE", False)
        res = ebaymag_driver.fetch_site_states("718000001", "351234567890")
        assert res.ok is False
        assert "playwright" in (res.error or "")

    def test_apply_without_playwright_returns_error(self, monkeypatch):
        monkeypatch.setattr(ebaymag_driver, "PLAYWRIGHT_AVAILABLE", False)
        res = ebaymag_driver.apply_site_changes(
            "718000001", expected_itm="351234567890",
            turn_on=["UK"], turn_off=[])
        assert res.ok is False
        assert "playwright" in (res.error or "")


class TestSubprocessIsolation:
    """Streamlit (Windows) 配下の event loop 衝突回避 (2026-06-13 Q1 実機で発覚)."""

    def test_should_isolate_env_override(self, monkeypatch):
        monkeypatch.setenv("EBAYMAG_DRIVER_SUBPROCESS", "1")
        assert ebaymag_driver._should_isolate() is True
        monkeypatch.setenv("EBAYMAG_DRIVER_SUBPROCESS", "0")
        assert ebaymag_driver._should_isolate() is False

    def test_should_isolate_false_outside_streamlit(self, monkeypatch):
        monkeypatch.delenv("EBAYMAG_DRIVER_SUBPROCESS", raising=False)
        # pytest = streamlit script ctx なし → in-process でよい
        assert ebaymag_driver._should_isolate() is False

    def test_fetch_routes_to_isolated_when_under_streamlit(self, monkeypatch):
        sentinel = ebaymag_driver.EbaymagResult(ok=True, site_states={"UK": True})
        calls = {}

        def fake_isolated(func_name, kwargs, timeout_sec):
            calls["args"] = (func_name, kwargs, timeout_sec)
            return sentinel

        monkeypatch.setattr(ebaymag_driver, "_should_isolate", lambda: True)
        monkeypatch.setattr(ebaymag_driver, "_run_isolated", fake_isolated)
        res = ebaymag_driver.fetch_site_states("718000001", "351234567890")
        assert res is sentinel
        assert calls["args"][0] == "fetch_site_states"
        assert calls["args"][1] == {"product_id": "718000001",
                                    "expected_itm": "351234567890"}

    def test_apply_routes_to_isolated_after_validation(self, monkeypatch):
        sentinel = ebaymag_driver.EbaymagResult(ok=True)
        monkeypatch.setattr(ebaymag_driver, "_should_isolate", lambda: True)
        monkeypatch.setattr(
            ebaymag_driver, "_run_isolated",
            lambda func_name, kwargs, timeout_sec: sentinel)
        # 入力検証は親プロセス側で先に走る (未知 code は subprocess に行かない)
        bad = ebaymag_driver.apply_site_changes(
            "718000001", expected_itm="x", turn_on=["US"], turn_off=[])
        assert bad.ok is False and "未知の site code" in bad.error
        ok = ebaymag_driver.apply_site_changes(
            "718000001", expected_itm="x", turn_on=["UK"], turn_off=["DE"])
        assert ok is sentinel

    def test_run_isolated_real_roundtrip(self):
        """実 subprocess 往復 (子 import 成立 / argv JSON escape / 最終行 parse)。

        CDP / playwright に触れない経路 (子側の入力検証 error) を使うため
        副作用ゼロ。reviewer M2。
        """
        res = ebaymag_driver._run_isolated(
            "apply_site_changes",
            {"product_id": "0", "expected_itm": "0",
             "turn_on": [], "turn_off": []},
            timeout_sec=120)
        assert res.ok is False
        assert "変更対象なし" in (res.error or "")

    def test_error_message_not_empty_for_blank_str_exception(self, monkeypatch):
        """str(e) が空の例外 (NotImplementedError) でも型名が表示される。

        2026-06-13 Q1 実機で「状態取得失敗:」(空メッセージ) になった実害バグの
        回帰テスト。reviewer M2。
        """
        def boom(*a, **k):
            raise NotImplementedError()

        monkeypatch.setattr(ebaymag_driver, "_should_isolate", lambda: False)
        monkeypatch.setattr(ebaymag_driver, "sync_playwright", boom)
        res = ebaymag_driver.fetch_site_states("1", "2")
        assert res.ok is False
        assert "NotImplementedError" in (res.error or "")
