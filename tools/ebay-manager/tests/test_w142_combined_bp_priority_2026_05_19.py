"""W142 (2026-05-19): combined-新BP override + ShippingServicePriority 整合.

固定する設計核心 (money-direct = 送料 DDP buffer / Section 232 数百ドル/件):
  - Q-3 撤廃 → BP変更 ∧ 価格/送料変更 を combined ReviseFixedPriceItem で送信
  - `_build_revise_with_shipping_xml` の ship_priority パラメータ化 (default=1
    = 既存経路 XML 完全不変 = 後方互換の機械的証明)
  - resolve_domestic_priority: 単一 domestic = sortOrder (無ければ記載順 1) /
    複数・0 domestic = None (combined 中止 degrade、Q0 無音失敗させない)
  - preflight abort (payment/return 不足 R2 / policy 取得不能 / 複数 domestic)
    → revise せず success:False + 痕跡 (Ack 偽装しない)
  - 根本原因#5: +each change-detection / +each verify (R7: post に出ねば fail)
  - R4: combined/非combined で ship のみ変更時 +each を 0 に潰さない (再送)
  - snapshot: Domestic override の存在 + ShippingServicePriority parse
  - _sync_db_to_actual: +each None-skip (NULL 上書きで未取得に劣化させない)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ───────────────────────── helpers ─────────────────────────

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


def _pol(policy_id="BP_NEW", dom_count=1, sort_order=1):
    from monitor.ebay_account_policy import (
        ShippingPolicyInfo, ShippingPolicyList,
    )
    return ShippingPolicyList(ok=True, error=None, policies=[
        ShippingPolicyInfo(
            policy_id=policy_id, name=policy_id,
            domestic_service_count=dom_count,
            domestic_sort_order=sort_order)])


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
    seq = [pre, post]
    return patch(
        "monitor.ebay_listing_snapshot.fetch_listing_snapshot",
        side_effect=lambda *a, **k: seq.pop(0),
    ), patch.object(tpm, "get_ebay_credentials", return_value=_CREDS)


# ───────── 1. priority パラメータ化 + 後方互換 (機械的証明) ─────────

def test_priority_param_default_is_1_xml_unchanged():
    from monitor.ebay_client import _build_revise_with_shipping_xml
    xml = _build_revise_with_shipping_xml(
        "IT", None, 29.0, 0.0,
        seller_profiles={"payment_id": "P", "return_id": "R",
                         "shipping_id": "S"})
    assert "<ShippingServicePriority>1</ShippingServicePriority>" in xml


def test_priority_param_passthrough():
    from monitor.ebay_client import _build_revise_with_shipping_xml
    xml = _build_revise_with_shipping_xml(
        "IT", None, 29.0, 0.0,
        seller_profiles={"payment_id": "P", "return_id": "R",
                         "shipping_id": "S"},
        ship_priority=3)
    assert "<ShippingServicePriority>3</ShippingServicePriority>" in xml
    assert "<ShippingServicePriority>1</ShippingServicePriority>" not in xml


def test_revise_fn_default_priority_unchanged():
    """revise_fixed_price_with_shipping を既存形 (priority 指定なし) で
    呼ぶと XML は priority=1 (W136/W137 経路の機械的後方互換)."""
    import monitor.ebay_client as ec
    captured = {}

    def _fake_call(call, xml, *a, **k):
        captured["xml"] = xml
        return {"success": True, "ack": "Success"}

    with patch.object(ec, "_call_trading_api", side_effect=_fake_call):
        ec.revise_fixed_price_with_shipping(
            "IT", 160.0, 29.0, 0.0, "A", "D", "C", "v^T",
            seller_profiles={"payment_id": "P", "return_id": "R",
                             "shipping_id": "S"})
    assert "<ShippingServicePriority>1</ShippingServicePriority>" \
        in captured["xml"]


# ───────── 2. resolve_domestic_priority ─────────

def test_resolve_single_domestic_uses_sortorder():
    from monitor.ebay_account_policy import (
        ShippingPolicyInfo, resolve_domestic_priority,
    )
    p = ShippingPolicyInfo(policy_id="B", name="B",
                           domestic_service_count=1, domestic_sort_order=2)
    prio, reason = resolve_domestic_priority(p)
    assert prio == 2 and reason == "single-domestic"


def test_resolve_single_domestic_missing_sortorder_defaults_1():
    from monitor.ebay_account_policy import (
        ShippingPolicyInfo, resolve_domestic_priority,
    )
    p = ShippingPolicyInfo(policy_id="B", name="B",
                           domestic_service_count=1,
                           domestic_sort_order=None)
    prio, reason = resolve_domestic_priority(p)
    assert prio == 1 and reason == "single-domestic"


def test_resolve_multi_domestic_aborts():
    from monitor.ebay_account_policy import (
        ShippingPolicyInfo, resolve_domestic_priority,
    )
    p = ShippingPolicyInfo(policy_id="B", name="B",
                           domestic_service_count=2)
    prio, reason = resolve_domestic_priority(p)
    assert prio is None and reason == "multi-domestic-ambiguous"


def test_resolve_zero_domestic_aborts():
    from monitor.ebay_account_policy import (
        ShippingPolicyInfo, resolve_domestic_priority,
    )
    p = ShippingPolicyInfo(policy_id="B", name="B",
                           domestic_service_count=0)
    prio, reason = resolve_domestic_priority(p)
    assert prio is None and reason == "no-domestic-service"


# ───────── 3. ShippingPolicyInfo 後方互換 + sortOrder 抽出 ─────────

def test_policy_info_backcompat_minimal_construction():
    """新 field は default 付き = 既存最小構築不変 (frozen 後方互換)."""
    from monitor.ebay_account_policy import ShippingPolicyInfo
    p = ShippingPolicyInfo(policy_id="X", name="X")
    assert p.domestic_sort_order is None
    assert p.domestic_service_codes == ()
    assert p.domestic_service_count == 0


def test_fetch_policies_extracts_domestic_sort_order():
    """REST `sortOrder` (公式名) を単一 domestic で拾い priority に。"""
    import json
    import monitor.ebay_account_policy as m
    body = json.dumps({"fulfillmentPolicies": [{
        "fulfillmentPolicyId": "BP1", "name": "p",
        "shippingOptions": [
            {"optionType": "DOMESTIC", "shippingServices": [
                {"shippingServiceCode": "US_X", "sortOrder": 3}]},
            {"optionType": "INTERNATIONAL", "shippingServices": [
                {"shippingServiceCode": "INTL"}]},
        ]}]})
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = body.encode("utf-8")
    cm.__exit__.return_value = False
    with patch("monitor.ebay_oauth_refresh.get_valid_access_token",
               return_value="T"), \
         patch.object(m.urllib.request, "urlopen", return_value=cm):
        r = m.fetch_shipping_policies({})
    assert r.ok
    pol = r.policies[0]
    assert pol.domestic_service_count == 1
    assert pol.domestic_sort_order == 3
    assert pol.domestic_service_codes == ("US_X",)


# ───────── 4. snapshot: override 存在 + priority parse ─────────

_OVR_XML = """<?xml version="1.0" encoding="utf-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Item>
    <ItemID>IT</ItemID><SKU>stock:01</SKU>
    <StartPrice currencyID="USD">160.0</StartPrice>
    <ShippingServiceCostOverrideList>
      <ShippingServiceCostOverride>
        <ShippingServiceType>Domestic</ShippingServiceType>
        <ShippingServicePriority>2</ShippingServicePriority>
        <ShippingServiceCost currencyID="USD">29.0</ShippingServiceCost>
        <ShippingServiceAdditionalCost currencyID="USD">5.0</ShippingServiceAdditionalCost>
      </ShippingServiceCostOverride>
    </ShippingServiceCostOverrideList>
  </Item>
</GetItemResponse>"""

_NOOVR_XML = """<?xml version="1.0" encoding="utf-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Item>
    <ItemID>IT</ItemID><SKU>stock:01</SKU>
    <StartPrice currencyID="USD">160.0</StartPrice>
    <ShippingDetails><ShippingServiceOptions>
      <ShippingServiceCost currencyID="USD">31.6</ShippingServiceCost>
    </ShippingServiceOptions></ShippingDetails>
  </Item>
</GetItemResponse>"""


def _resp(text):
    r = MagicMock()
    r.text = text
    r.raise_for_status = MagicMock(return_value=None)
    return r


def test_snapshot_override_present_and_priority_parsed():
    import monitor.ebay_listing_snapshot as m
    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post", return_value=_resp(_OVR_XML)):
        s = m.fetch_listing_snapshot("IT", "A", "D", "C", "v^T")
    assert s.ok
    assert s.ship_override_present is True
    assert s.ship_override_priority == 2
    assert s.ship_cost_usd == 29.0       # 既存 override 優先 parse 不変
    assert s.ship_additional_usd == 5.0


def test_snapshot_no_override_present_false():
    import monitor.ebay_listing_snapshot as m
    with patch.object(m, "_resolve_active_token", side_effect=lambda t: t), \
         patch.object(m.httpx, "post", return_value=_resp(_NOOVR_XML)):
        s = m.fetch_listing_snapshot("IT", "A", "D", "C", "v^T")
    assert s.ok
    assert s.ship_override_present is False
    assert s.ship_override_priority is None
    assert s.ship_cost_usd == 31.6       # fallback 不変


# ───────── 5. preflight abort (Q0 無音失敗させない) ─────────

def test_preflight_abort_when_payment_profile_missing(tpm):
    """R2: combined だが pre-snapshot に payment ID 無 → revise せず
    success:False + account リスク痕跡."""
    pre = _snap(shipping_profile_id="BP_OLD", payment_profile_id=None,
                start_price_usd=148.0)
    post = _snap()
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "_cached_shipping_policies",
                      return_value=_pol()), \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m_ps, \
         patch.object(tpm, "revise_shipping_profile") as m_bp:
        res = tpm._apply_to_ebay(
            "IT", {"new_bp_id": "BP_NEW", "new_ebay_price": 160.0},
            {}, current_sku="x")
    m_ps.assert_not_called()
    m_bp.assert_not_called()
    assert res["success"] is False
    assert "account" in res["message"] or "中止" in res["message"]


def test_preflight_abort_when_policy_not_found(tpm):
    pre = _snap(shipping_profile_id="BP_OLD", start_price_usd=148.0)
    post = _snap()
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "_cached_shipping_policies",
                      return_value=_pol(policy_id="OTHER")), \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m_ps:
        res = tpm._apply_to_ebay(
            "IT", {"new_bp_id": "BP_NEW", "new_ebay_price": 160.0},
            {}, current_sku="x")
    m_ps.assert_not_called()
    assert res["success"] is False
    assert "中止" in res["message"]


def test_preflight_abort_multi_domestic(tpm):
    pre = _snap(shipping_profile_id="BP_OLD", start_price_usd=148.0)
    post = _snap()
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "_cached_shipping_policies",
                      return_value=_pol(dom_count=2, sort_order=None)), \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m_ps:
        res = tpm._apply_to_ebay(
            "IT", {"new_bp_id": "BP_NEW", "new_ebay_price": 160.0},
            {}, current_sku="x")
    m_ps.assert_not_called()
    assert res["success"] is False
    assert "priority を解決できません" in res["message"]
    assert "multi-domestic-ambiguous" in res["message"]


# ───────── 6. combined ケース i (R4: +each を 0 に潰さない) ─────────

def test_combined_case_i_resends_current_override_not_zeroed(tpm):
    """BP変更+価格変更、送料欄未操作。pre override ship=29/+each=5 を
    新BPに維持して combined 送信 (R4: +each を None→0 に潰さない)."""
    pre = _snap(shipping_profile_id="BP_OLD", start_price_usd=148.0,
                ship_cost_usd=29.0, ship_additional_usd=5.0)
    post = _snap(shipping_profile_id="BP_NEW", start_price_usd=160.0,
                 ship_cost_usd=29.0, ship_additional_usd=5.0,
                 ship_override_present=True, ship_override_priority=1)
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "_cached_shipping_policies",
                      return_value=_pol()), \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}) as m_ps, \
         patch.object(tpm, "revise_shipping_profile") as m_bp:
        res = tpm._apply_to_ebay(
            "IT", {"new_bp_id": "BP_NEW", "new_ebay_price": 160.0},
            {}, current_sku="x")
    m_bp.assert_not_called()
    kw = m_ps.call_args.kwargs
    assert kw["ship_cost_usd"] == 29.0       # 現 override 維持
    assert kw["ship_additional_usd"] == 5.0  # R4: +each を 0 に潰さない
    assert kw["seller_profiles"]["shipping_id"] == "BP_NEW"
    assert kw["ship_priority"] == 1
    assert res["success"] is True


# ───────── 7. 根本原因#5a: +each のみ変更 (非combined) ─────────

def test_rootcause5a_add_only_triggers_revise(tpm):
    """+each のみ変更 (Buyer pays 不変、BP 不変) でも revise が走り、
    override block を出すため現 ship_cost を再送 (旧: 永久 no-diff)."""
    pre = _snap(ship_cost_usd=29.0, ship_additional_usd=0.0)
    post = _snap(ship_cost_usd=29.0, ship_additional_usd=3.0)
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}) as m_ps:
        res = tpm._apply_to_ebay(
            "IT", {"new_ship_additional": 3.0}, {}, current_sku="x")
    kw = m_ps.call_args.kwargs
    assert kw["ship_cost_usd"] == 29.0       # 現 ship 再送 (override 出す)
    assert kw["ship_additional_usd"] == 3.0
    assert "差分なし" not in res["message"]
    assert res["add_ok"] is True             # 根本原因#5c verify


# ───────── 8. R7: +each verify (post に出ねば fail、偽装しない) ─────────

def test_r7_add_verify_none_in_post_fails(tpm):
    """add_changed だが post-snapshot に +each が出ない → 不明 = fail
    (Ack を success に偽装しない / silent-skip-prevention)."""
    pre = _snap(ship_cost_usd=29.0, ship_additional_usd=0.0)
    post = _snap(ship_cost_usd=29.0, ship_additional_usd=None)
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}):
        res = tpm._apply_to_ebay(
            "IT", {"new_ship_additional": 3.0}, {}, current_sku="x")
    assert res["success"] is False
    assert res["add_ok"] is False
    assert "verify 不能" in res["message"]


def test_rootcause5c_add_verify_mismatch_fails(tpm):
    """post +each が期待と不一致 → add_ok False + success False + 実値併記."""
    pre = _snap(ship_cost_usd=29.0, ship_additional_usd=0.0)
    post = _snap(ship_cost_usd=29.0, ship_additional_usd=9.99)
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}):
        res = tpm._apply_to_ebay(
            "IT", {"new_ship_additional": 3.0}, {}, current_sku="x")
    assert res["success"] is False
    assert res["add_ok"] is False
    assert "9.99" in res["message"]


# ───────── 9. combined override 無音失敗 post-state 検出 ─────────

def test_combined_override_absent_post_detected_as_fail(tpm):
    """combined で override 送ったが post に override 不在 = W136 無音
    失敗 → success:False + DDP buffer 喪失痕跡 (Ack 偽装しない)."""
    pre = _snap(shipping_profile_id="BP_OLD", start_price_usd=148.0,
                ship_cost_usd=29.0, ship_additional_usd=0.0)
    post = _snap(shipping_profile_id="BP_NEW", start_price_usd=160.0,
                 ship_cost_usd=29.0, ship_additional_usd=0.0,
                 ship_override_present=False)
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "_cached_shipping_policies",
                      return_value=_pol()), \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}):
        res = tpm._apply_to_ebay(
            "IT", {"new_bp_id": "BP_NEW", "new_ebay_price": 160.0},
            {}, current_sku="x")
    assert res["success"] is False
    assert "無音失敗" in res["message"]


# ───────── 10. _sync_db_to_actual: +each None-skip (R4) ─────────

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    return db_path


def test_sync_writes_each_when_present(tpm, tmp_db):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute("INSERT INTO ebay_listings (ebay_item_id, sku, title) "
                  "VALUES ('S1','stock:01','T')")
    tpm._sync_db_to_actual("S1", _snap(item_id="S1",
                                       ship_additional_usd=4.5))
    with get_conn() as c:
        row = c.execute("SELECT shipping_additional_cost, "
                        "shipping_additional_fetched_at FROM ebay_listings "
                        "WHERE ebay_item_id='S1'").fetchone()
    assert row[0] == 4.5 and row[1] is not None


def test_sync_skips_each_when_none_not_overwrite(tpm, tmp_db):
    """snap.ship_additional_usd None なら既知 DB 値を NULL 上書きしない
    (R4 状態リセット防止)."""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute("INSERT INTO ebay_listings (ebay_item_id, sku, title, "
                  "shipping_additional_cost) VALUES ('S2','stock:01','T',7.0)")
    tpm._sync_db_to_actual("S2", _snap(item_id="S2",
                                       ship_additional_usd=None))
    with get_conn() as c:
        row = c.execute("SELECT shipping_additional_cost FROM ebay_listings "
                        "WHERE ebay_item_id='S2'").fetchone()
    assert row[0] == 7.0    # 既知値保持 (NULL 上書きしない)


# ───── 11. HIGH-1 (内部review): combined + override無 listing で新BP送信 ─────

def test_xml_force_seller_profiles_emits_bp_without_override():
    """combined-新BP で custom 送料を持たない (ship_cost None) listing でも
    force_seller_profiles=True なら <SellerProfiles>+<ShippingProfileID> が
    出力され override block は出ない (新BP が無音欠落しない、HIGH-1 fix)."""
    from monitor.ebay_client import _build_revise_with_shipping_xml
    xml = _build_revise_with_shipping_xml(
        "IT", 160.0, None, None,
        seller_profiles={"payment_id": "P", "return_id": "R",
                         "shipping_id": "BP_NEW"},
        force_seller_profiles=True)
    assert "<SellerProfiles>" in xml
    assert "<ShippingProfileID>BP_NEW</ShippingProfileID>" in xml
    assert "<ShippingServiceCostOverrideList>" not in xml  # override 無


def test_xml_default_no_force_omits_seller_profiles_when_no_ship():
    """後方互換 (D1): force=False ∧ ship_cost None → SellerProfiles 非同梱
    (price-only revise の W136 既存挙動、機械的不変)."""
    from monitor.ebay_client import _build_revise_with_shipping_xml
    xml = _build_revise_with_shipping_xml(
        "IT", 160.0, None, None,
        seller_profiles={"payment_id": "P", "return_id": "R",
                         "shipping_id": "BP_NEW"})
    assert "<SellerProfiles>" not in xml


def test_combined_indeterminate_base_aborts_safe(tpm):
    """W142 Codex-R3 HIGH-3 (spec 進化、Q0 痕跡): combined (BP+価格) で
    pre-snapshot ship_cost None = 送料状態不確定。旧 spec は
    force_seller_profiles で BP 強行送信だったが、None は『custom 送料
    無』でなく『GetItem が送料を返さず不確定』であり、見えない override
    (DDP buffer) を黙って BP-default 化し得る。統一安全ガードで revise を
    一切呼ばず明示 abort (旧: test_combined_no_override_listing_still_
    sends_new_bp)."""
    pre = _snap(shipping_profile_id="BP_OLD", start_price_usd=148.0,
                ship_cost_usd=None, ship_additional_usd=None)
    post = _snap(shipping_profile_id="BP_NEW", start_price_usd=160.0)
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "_cached_shipping_policies",
                      return_value=_pol()), \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m_ps, \
         patch.object(tpm, "revise_shipping_profile") as m_bp:
        res = tpm._apply_to_ebay(
            "IT", {"new_bp_id": "BP_NEW", "new_ebay_price": 160.0},
            {}, current_sku="x")
    m_ps.assert_not_called()       # money-changing revise を実行しない
    m_bp.assert_not_called()
    assert res["success"] is False
    assert "不確定" in res["message"]
    assert "中止" in res["message"]
    assert "Buyer pays" in res["message"]


# ───── 12. HIGH-2 (内部review): +each 表示 source の read back 経路 ─────

def test_fetch_all_products_returns_shipping_additional(tmp_db):
    """根本原因#5(b): _fetch_all_products SELECT が shipping_additional_cost
    /fetched_at/last_synced_at を返す (UI の +each 表示 source、書込→読出
    経路成立)."""
    from monitor.database import get_conn
    from tabs.tab_product_management import _fetch_all_products
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, "
            "shipping_additional_cost, shipping_additional_fetched_at, "
            "last_synced_at) VALUES ('E1','stock:01','T',5.0,"
            "datetime('now'),datetime('now'))")
    rows = _fetch_all_products()
    r = next(x for x in rows if x["ebay_item_id"] == "E1")
    assert r["shipping_additional_cost"] == 5.0
    assert r["shipping_additional_fetched_at"] is not None
    assert r["last_synced_at"] is not None


def test_refresh_bp_from_ebay_writes_each_none_skip(tpm, tmp_db):
    """↻ 再取得が +each も書く (None-skip: snap に出た時のみ、設計
    コメント database.py v43「更新元は単発 ↻ 再取得のみ」と整合)."""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute("INSERT INTO ebay_listings (ebay_item_id, sku, title) "
                  "VALUES ('R1','stock:01','T')")
    snap = _snap(item_id="R1", shipping_profile_id="BP_X",
                 ship_additional_usd=6.5)
    with patch.object(tpm, "get_ebay_credentials",
                      return_value={"app_id": "A", "dev_id": "D",
                                    "cert_id": "C", "user_token": "T"}), \
         patch("monitor.ebay_listing_snapshot.fetch_listing_snapshot",
               return_value=snap):
        tpm._refresh_bp_from_ebay("R1", {})
    with get_conn() as c:
        row = c.execute("SELECT shipping_profile_id, "
                        "shipping_additional_cost FROM ebay_listings "
                        "WHERE ebay_item_id='R1'").fetchone()
    assert row[0] == "BP_X"
    assert row[1] == 6.5


# ───── 13. HIGH-A (内部review 2周目): +each base 無 honest 処理 ─────

def test_revise_gate_allows_bp_swap_only():
    """ebay_client 早期ゲート: new_price/ship_cost None でも
    force_seller_profiles=True ∧ shipping_id あり は変更対象 (BP差替を
    無音棄却しない、HIGH-A fix①)."""
    from monitor.ebay_client import revise_fixed_price_with_shipping
    with patch("monitor.ebay_client._call_trading_api",
               return_value={"success": True, "ack": "Success"}):
        r = revise_fixed_price_with_shipping(
            "IT", None, None, None, "A", "D", "C", "T",
            seller_profiles={"payment_id": "P", "return_id": "R",
                             "shipping_id": "BP_NEW"},
            force_seller_profiles=True)
    assert r.get("success") is True
    assert "変更対象がない" not in (r.get("message") or "")


def test_revise_gate_still_rejects_truly_empty():
    """後方互換: price/ship None ∧ BP差替意図無 は従来通り棄却
    (W136/W137 経路の判定式が原型と同値)."""
    from monitor.ebay_client import revise_fixed_price_with_shipping
    r = revise_fixed_price_with_shipping(
        "IT", None, None, None, "A", "D", "C", "T")
    assert r.get("success") is False
    assert "変更対象がない" in r.get("message")


def test_combined_indeterminate_add_aborts_safe(tpm):
    """W142 Codex-R3 HIGH-1/3 (spec 進化、Q0 痕跡): combined で +each 入力
    したが pre-snapshot ship_cost None = base 不確定。旧 spec は BP 送信+
    +each 未反映 message だったが、見えない override を黙って失う恐れが
    あるため統一安全ガードで revise を呼ばず明示 abort (旧:
    test_combined_no_custom_shipping_add_only_no_silent_drop)."""
    pre = _snap(shipping_profile_id="BP_OLD", start_price_usd=148.0,
                ship_cost_usd=None, ship_additional_usd=None)
    post = _snap(shipping_profile_id="BP_NEW", start_price_usd=148.0)
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "_cached_shipping_policies",
                      return_value=_pol()), \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m_ps, \
         patch.object(tpm, "revise_shipping_profile") as m_bp:
        res = tpm._apply_to_ebay(
            "IT", {"new_bp_id": "BP_NEW", "new_ship_additional": 3.0},
            {}, current_sku="x")
    m_bp.assert_not_called()
    m_ps.assert_not_called()       # 不確定で money-changing revise しない
    assert res["success"] is False
    assert "不確定" in res["message"] and "中止" in res["message"]
    assert "Buyer pays" in res["message"]


def test_noncombined_indeterminate_add_aborts_safe(tpm):
    """W142 Codex-R3 (spec 進化): 非combined・+each のみ・base 不確定
    (snap.ship_cost None) → 統一安全ガードで revise 呼ばず明示 abort。
    旧誤誘導 'revise API 失敗' を出さない (旧:
    test_noncombined_add_only_no_base_skips_revise_honest)."""
    pre = _snap(ship_cost_usd=None, ship_additional_usd=None,
                shipping_profile_id="BP_S")
    post = _snap(ship_cost_usd=None, ship_additional_usd=None,
                 shipping_profile_id="BP_S")
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "revise_fixed_price_with_shipping") as m_ps:
        res = tpm._apply_to_ebay(
            "IT", {"new_ship_additional": 3.0}, {}, current_sku="x")
    m_ps.assert_not_called()
    assert res["success"] is False
    assert "不確定" in res["message"]
    assert "Buyer pays" in res["message"]
    assert "revise API 失敗" not in res["message"]  # 誤誘導しない


def test_combined_normal_base_proceeds_not_aborted(tpm):
    """統一ガードが正常 listing (snap が base/+each 確定値) を誤 abort
    しないこと: pre ship_cost=29/+each=0.0 (本 system override は明示
    0.00 を持つ) + BP+価格変更 → ガード通過し combined revise 実行."""
    pre = _snap(shipping_profile_id="BP_OLD", start_price_usd=148.0,
                ship_cost_usd=29.0, ship_additional_usd=0.0)
    post = _snap(shipping_profile_id="BP_NEW", start_price_usd=160.0,
                 ship_cost_usd=29.0, ship_additional_usd=0.0,
                 ship_override_present=True, ship_override_priority=1)
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "_cached_shipping_policies",
                      return_value=_pol()), \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}) as m_ps:
        res = tpm._apply_to_ebay(
            "IT", {"new_bp_id": "BP_NEW", "new_ebay_price": 160.0},
            {}, current_sku="x")
    assert m_ps.called               # 正常 listing は abort されない
    assert "不確定" not in res["message"]
    assert res["success"] is True


def test_resolve_priority_out_of_range_aborts():
    """W142 Codex-R3 MEDIUM: eBay domestic priority は 1-4。範囲外
    (0/負/5+) は preflight abort 用に None+理由を返す (不正値を XML に
    出さない、silent-skip-prevention)."""
    from monitor.ebay_account_policy import (
        ShippingPolicyInfo, resolve_domestic_priority,
    )
    for bad in (0, -1, 5, 99):
        p = ShippingPolicyInfo(policy_id="B", name="B",
                               domestic_service_count=1,
                               domestic_sort_order=bad)
        prio, reason = resolve_domestic_priority(p)
        assert prio is None, f"sortOrder={bad} should abort"
        assert "invalid-sort-order" in reason
    # 範囲内 (1-4) は通る
    for ok in (1, 2, 3, 4):
        p = ShippingPolicyInfo(policy_id="B", name="B",
                               domestic_service_count=1,
                               domestic_sort_order=ok)
        prio, reason = resolve_domestic_priority(p)
        assert prio == ok and reason == "single-domestic"


def test_add_dirty_flag_displayed_db_value_not_treated_as_edit(tpm):
    """W142 Codex-R3 HIGH-2: 表示中の DB +each 初期値を user 未操作で
    submit (= add_render_initial と同値) し、snap.ship_additional_usd が
    None でも、+each を『変更』扱いせず stale DB 値を実 eBay へ上書き
    しない (BP の Codex#1 dirty-flag と同型)。price のみ変更で検証."""
    pre = _snap(ship_cost_usd=29.0, ship_additional_usd=None,
                start_price_usd=148.0, shipping_profile_id="BP_S")
    post = _snap(ship_cost_usd=29.0, ship_additional_usd=None,
                 start_price_usd=160.0, shipping_profile_id="BP_S")
    s_p, c_p = _patch(tpm, pre, post)
    with s_p, c_p, \
         patch.object(tpm, "revise_fixed_price_with_shipping",
                      return_value={"success": True}) as m_ps:
        # add_render_initial == new_ship_additional (5.0) = user 無操作
        res = tpm._apply_to_ebay(
            "IT", {"new_ebay_price": 160.0,
                   "new_ship_additional": 5.0,
                   "add_render_initial": 5.0}, {}, current_sku="x")
    # +each は dirty でない → add_changed False → 統一ガード非該当
    # (price のみ) → revise は price のみで実行、+each を送らない
    assert m_ps.called
    kw = m_ps.call_args.kwargs
    assert kw["ship_additional_usd"] is None   # stale DB 5.0 を送らない
    assert "不確定" not in res["message"]
    assert res["success"] is True
