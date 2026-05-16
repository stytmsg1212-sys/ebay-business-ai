"""W137 (2026-05-17) 回帰: 商品管理「eBay 反映」正確化.

設計核心の固定:
  - A1: 変更検出 = form vs 実 eBay GetItem (DB 不参照)
  - W136: 送料 revise に seller_profiles (3 ID) 同梱
  - B1: 反映後 GetItem 実値一致で成功判定 (Ack でなく実値、fake success 排除)
  - pre/post snapshot 失敗時の Q0 (不明を成功/変更なしと偽らない)
  - _sync_db_to_actual: DB を実 eBay 値へ同期 (乖離の構造排除)
  - ebay_client._build_revise_with_shipping_xml の SellerProfiles 同梱
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def _snap(**kw):
    """ListingSnapshot を部分指定で生成 (未指定は安全 default)."""
    from monitor.ebay_listing_snapshot import ListingSnapshot
    d = dict(
        item_id="ITEM1", sku="stock:01", start_price_usd=148.0,
        ship_cost_usd=29.0, ship_additional_usd=0.0,
        payment_profile_id="PAY1", return_profile_id="RET1",
        shipping_profile_id="SHIP1", ack="Success", ok=True, error=None,
    )
    d.update(kw)
    return ListingSnapshot(**d)


@pytest.fixture
def tpm(monkeypatch):
    import streamlit as st

    class _S(dict):
        pass
    monkeypatch.setattr(st, "session_state", _S())
    from tabs import tab_product_management as _tpm
    return _tpm


_CREDS = {"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "v^T"}


def _patch(tpm, pre, post):
    """fetch_listing_snapshot を pre→post の順で返すよう patch."""
    seq = [pre, post]
    return patch(
        "monitor.ebay_listing_snapshot.fetch_listing_snapshot",
        side_effect=lambda *a, **k: seq.pop(0),
    ), patch.object(tpm, "get_ebay_credentials", return_value=_CREDS)


# ── A1: 変更検出は実 eBay 比較 ──

def test_no_diff_vs_real_ebay_skips_revise(tpm):
    """form が実 eBay と一致 → revise せず '差分なし' (DB は無視)."""
    pre = _snap(sku="stock:01", start_price_usd=148.0, ship_cost_usd=29.0)
    s_patch, c_patch = _patch(tpm, pre, pre)
    editing = {"sku": "stock:01", "new_ebay_price": 148.0,
               "new_ship_cost": 29.0}
    with s_patch, c_patch, \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m_ps, \
         patch.object(tpm, "revise_item_sku") as m_sku:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="STOCK")
    m_ps.assert_not_called()
    m_sku.assert_not_called()
    assert res["success"] is False
    assert "差分なし" in res["message"]


def test_db_ebay_divergence_detected_via_real_ebay(tpm):
    """DB='STOCK' でも実 eBay='stock:01' と form 'stock' の差で SKU 変更検出."""
    pre = _snap(sku="stock:01")
    post = _snap(sku="stock")
    s_patch, c_patch = _patch(tpm, pre, post)
    editing = {"sku": "stock"}
    with s_patch, c_patch, \
         patch.object(tpm, "revise_item_sku",
                      return_value={"success": True, "message": "ok"}) as m:
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="STOCK")
    m.assert_called_once()
    assert m.call_args[0][1] == "stock"
    assert res["success"] is True and res["sku_ok"] is True


# ── W136: 送料 revise に seller_profiles 同梱 ──

def test_ship_change_passes_seller_profiles(tpm):
    """送料変更時 revise_fixed_price_with_shipping に pre-snapshot の
    3 profile ID が seller_profiles で渡る (W136 真因 fix)."""
    pre = _snap(ship_cost_usd=31.6, payment_profile_id="PAY9",
                return_profile_id="RET9", shipping_profile_id="SHIP9")
    post = _snap(ship_cost_usd=29.0)
    s_patch, c_patch = _patch(tpm, pre, post)
    editing = {"sku": "stock:01", "new_ship_cost": 29.0}
    with s_patch, c_patch, \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}) as m:
        tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="x")
    sp = m.call_args.kwargs["seller_profiles"]
    assert sp == {"payment_id": "PAY9", "return_id": "RET9",
                  "shipping_id": "SHIP9"}


# ── B1: fake success 排除 (Ack でなく実値 verify) ──

def test_fake_success_killed_when_real_value_unchanged(tpm):
    """revise が Ack 成功でも post-snapshot 実値が変わらなければ ❌
    (W136 無音失敗の fake success を排除)."""
    pre = _snap(ship_cost_usd=31.6)
    post = _snap(ship_cost_usd=31.6)   # override 効かず実値変わらず
    s_patch, c_patch = _patch(tpm, pre, post)
    editing = {"sku": "stock:01", "new_ship_cost": 29.0}
    with s_patch, c_patch, \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True, "ack": "Success"}):
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="x")
    assert res["success"] is False
    assert res["price_ship_ok"] is False
    assert "実値不一致" in res["message"]
    assert "31.6" in res["message"]   # 実値併記 (Q0 透明)


def test_price_verified_success(tpm):
    pre = _snap(start_price_usd=148.0)
    post = _snap(start_price_usd=160.0)
    s_patch, c_patch = _patch(tpm, pre, post)
    editing = {"sku": "stock:01", "new_ebay_price": 160.0}
    with s_patch, c_patch, \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}):
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="x")
    assert res["success"] is True and res["price_ship_ok"] is True


# ── Q0: snapshot 失敗時に成功/変更なしと偽らない ──

def test_pre_snapshot_fail_aborts_no_revise(tpm):
    pre = _snap(ok=False, error="通信エラー: boom")
    post = _snap()
    s_patch, c_patch = _patch(tpm, pre, post)
    with s_patch, c_patch, \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m:
        res = tpm._apply_to_ebay(
            "ITEM1", {"new_ebay_price": 999.0}, {}, current_sku="x")
    m.assert_not_called()
    assert res["success"] is False
    assert "反映前 GetItem 失敗" in res["message"]


def test_post_snapshot_fail_not_claimed_success(tpm):
    pre = _snap(start_price_usd=148.0)
    post = _snap(ok=False, error="parse fail")
    s_patch, c_patch = _patch(tpm, pre, post)
    with s_patch, c_patch, \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}):
        res = tpm._apply_to_ebay(
            "ITEM1", {"new_ebay_price": 160.0}, {}, current_sku="x")
    assert res["success"] is False
    assert "実反映不明" in res["message"]
    assert res["post_snapshot"] is None


def test_revise_api_failure_surfaced_in_message(tpm):
    """revise が success:False を返したら原因 message が結果に出る (HIGH-1).

    実値 verify が最終 gate だが、token 失効等の eBay ErrorCode を握り
    潰すと W136 無音失敗と区別できない → message 合流必須。
    """
    pre = _snap(start_price_usd=148.0)
    post = _snap(start_price_usd=148.0)   # 送信拒否で実値変わらず
    s_patch, c_patch = _patch(tpm, pre, post)
    editing = {"sku": "stock:01", "new_ebay_price": 160.0}
    with s_patch, c_patch, \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": False,
                                    "message": "[931] auth token expired"}):
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="x")
    assert res["success"] is False
    assert "auth token expired" in res["message"]
    assert "API" in res["message"]


def test_sku_revise_failure_not_marked_pushed(tpm):
    """revise_item_sku が success:False なら sku_pushed=True にしない (HIGH-1)."""
    pre = _snap(sku="stock:01")
    post = _snap(sku="stock:01")
    s_patch, c_patch = _patch(tpm, pre, post)
    with s_patch, c_patch, \
         patch.object(tpm, "revise_item_sku",
                      return_value={"success": False,
                                    "message": "API エラー: blocked"}):
        res = tpm._apply_to_ebay(
            "ITEM1", {"sku": "stock"}, {}, current_sku="x")
    assert res["success"] is False
    assert res["sku_pushed"] is False
    assert "blocked" in res["message"]


def test_ship_change_without_shipping_id_leaves_trace(tpm):
    """pre-snapshot に shipping_profile_id 無し時、W136 再発の痕跡が残る
    (HIGH-2: silent fallback 禁止)."""
    pre = _snap(ship_cost_usd=31.6, shipping_profile_id=None)
    post = _snap(ship_cost_usd=31.6)   # SellerProfiles 非同梱で無音失敗
    s_patch, c_patch = _patch(tpm, pre, post)
    editing = {"sku": "stock:01", "new_ship_cost": 29.0}
    with s_patch, c_patch, \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}):
        res = tpm._apply_to_ebay("ITEM1", editing, {}, current_sku="x")
    assert res["success"] is False
    assert "shipping profile ID" in res["message"]


def test_offspec_sku_suppressed(tpm):
    pre = _snap(sku="stock:01")
    post = _snap(sku="stock:01")
    s_patch, c_patch = _patch(tpm, pre, post)
    with s_patch, c_patch, \
         patch.object(tpm, "revise_item_sku") as m:
        res = tpm._apply_to_ebay(
            "ITEM1", {"sku": "STOCK"}, {}, current_sku="x")
    m.assert_not_called()
    assert res["success"] is False
    assert "規約外" in res["message"]


# ── _sync_db_to_actual: DB := 実 eBay 値 ──

def test_sync_db_to_actual_writes_snapshot_values(tpm, monkeypatch):
    captured = {}

    class _Conn:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(tpm, "get_conn", lambda: _Conn())
    monkeypatch.setattr(tpm, "bump_db_version", lambda: None)
    tpm._sync_db_to_actual(
        "ITEM1", _snap(start_price_usd=160.0, ship_cost_usd=31.6,
                       sku="stock"))
    assert "current_price=?" in captured["sql"]
    assert "shipping_cost=?" in captured["sql"]
    assert "sku=?" in captured["sql"]
    assert 160.0 in captured["params"] and 31.6 in captured["params"]
    assert "stock" in captured["params"]
    assert captured["params"][-1] == "ITEM1"


# ── ebay_client: SellerProfiles 同梱 (W136 XML) ──

def test_revise_xml_includes_seller_profiles_when_given():
    from monitor.ebay_client import _build_revise_with_shipping_xml
    xml = _build_revise_with_shipping_xml(
        "I1", None, 29.0, 0.0,
        seller_profiles={"payment_id": "P", "return_id": "R",
                         "shipping_id": "S"},
    )
    assert "<SellerProfiles>" in xml
    assert "<ShippingProfileID>S</ShippingProfileID>" in xml
    assert "<PaymentProfileID>P</PaymentProfileID>" in xml
    assert "<ShippingServiceCostOverrideList>" in xml


def test_revise_xml_omits_seller_profiles_backward_compat():
    """seller_profiles 無し = 旧挙動 (既存テスト不変、D1)."""
    from monitor.ebay_client import _build_revise_with_shipping_xml
    xml = _build_revise_with_shipping_xml("I1", None, 29.0, 0.0)
    assert "<SellerProfiles>" not in xml
    assert "<ShippingServiceCostOverrideList>" in xml
    assert "<ShippingServiceType>Domestic</ShippingServiceType>" in xml
