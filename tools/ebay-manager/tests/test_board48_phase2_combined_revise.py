"""依頼ボード #48 Phase 2 (combined revise) の回帰テスト.

対象: scripts/fix_handling_policy_board48_phase2.py の
`build_combined_revise_plans` 純関数。money-direct のため実 eBay API を伴う
経路 (enrich_with_snapshot_and_priority / _execute_combined) は pytest 対象外。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "fix_handling_policy_board48_phase2.py"
)


@pytest.fixture()
def phase2():
    spec = importlib.util.spec_from_file_location(
        "fix_handling_policy_board48_phase2", _SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _enriched_row(**overrides):
    base = {
        "ebay_item_id": "358052065626",
        "sku": "ebayYD_1",
        "title": "Test",
        "weight_g": 450.0,
        "current_policy_id": "377279123023",
        "correct_policy_id": "376828925023",
        "correct_policy_label": "Out-of-stock 0-500g",
        "live_ok": True,
        "live_error": None,
        "live_shipping_profile_id": "377279123023",
        "live_payment_profile_id": "PAY1",
        "live_return_profile_id": "RET1",
        "ship_cost_usd": 52.0,
        "ship_additional_usd": 52.0,
        "ship_override_present": True,
        "ship_override_priority": 1,
        "target_bp_priority": 1,
        "target_bp_priority_reason": "single-domestic",
        "start_price_usd": 100.0,
        "db_matches_live": True,
    }
    base.update(overrides)
    return base


def test_plans_preserve_override_ship_cost_and_delta_zero(phase2):
    """HIGH-1 (task-2): combined revise は現行 override 値を再送するため
    実行後の buyer-facing 送料 = 現行 ship_cost で維持される (delta=0)."""
    plans = phase2.build_combined_revise_plans([_enriched_row(ship_cost_usd=52.0)])
    assert len(plans) == 1
    p = plans[0]
    assert p["can_execute"] is True
    assert p["send_ship_cost_usd"] == 52.0
    assert p["expected_buyer_ship_after"] == 52.0
    assert p["buyer_ship_delta"] == 0.0
    # SellerProfiles 3 ID 揃う
    assert p["send_seller_profiles"] == {
        "payment_id": "PAY1", "return_id": "RET1",
        "shipping_id": "376828925023",
    }
    assert p["send_ship_priority"] == 1


def test_plans_handle_zero_dollar_override_special_case(phase2):
    """特異ケース: 356700630309 の $0 送料無料 override も現状維持する
    (BP default $30 にリセットされない、user 判断は別途)."""
    plans = phase2.build_combined_revise_plans([
        _enriched_row(ebay_item_id="356700630309", ship_cost_usd=0.0,
                       ship_additional_usd=0.0),
    ])
    p = plans[0]
    assert p["send_ship_cost_usd"] == 0.0
    assert p["send_ship_additional_usd"] == 0.0
    assert p["expected_buyer_ship_after"] == 0.0
    assert p["buyer_ship_delta"] == 0.0
    assert p["can_execute"] is True


def test_plans_default_additional_to_ship_cost_when_snapshot_missing(phase2):
    """+each が snapshot に無い (None) 場合、ship_cost と同値を送る
    (eBay 慣習「未設定 = 単品と同額」と等価)."""
    plans = phase2.build_combined_revise_plans([
        _enriched_row(ship_cost_usd=35.0, ship_additional_usd=None),
    ])
    p = plans[0]
    assert p["send_ship_cost_usd"] == 35.0
    assert p["send_ship_additional_usd"] == 35.0  # ship_cost と同値で補完


def test_plans_abort_when_ship_cost_none_ddp_buffer_protection(phase2):
    """HIGH-1 統一安全ガード: ship_cost が snapshot に無い = 不確定 →
    combined revise 送信で 0.00 に潰す経路になり DDP buffer 喪失 → abort."""
    plans = phase2.build_combined_revise_plans([
        _enriched_row(ship_cost_usd=None, ship_additional_usd=None),
    ])
    p = plans[0]
    assert p["can_execute"] is False
    assert any("ship_cost_usd" in r for r in p["abort_reasons"])


def test_plans_abort_when_getitem_failed(phase2):
    """GetItem 失敗 = 実 eBay 不明 → 送信禁止."""
    plans = phase2.build_combined_revise_plans([
        _enriched_row(live_ok=False, live_error="timeout"),
    ])
    p = plans[0]
    assert p["can_execute"] is False
    assert any("GetItem 失敗" in r for r in p["abort_reasons"])


def test_plans_abort_when_db_live_mismatch(phase2):
    """DB と実 eBay の BP 不一致 = 状況変化 → 送信禁止."""
    plans = phase2.build_combined_revise_plans([
        _enriched_row(db_matches_live=False, live_shipping_profile_id="OTHER_BP"),
    ])
    p = plans[0]
    assert p["can_execute"] is False
    assert any("不一致" in r for r in p["abort_reasons"])


def test_plans_abort_when_missing_payment_or_return_profile(phase2):
    """payment/return profile ID 欠落 = 3ID 不完全 → 送信禁止 (money/account リスク)."""
    plans = phase2.build_combined_revise_plans([
        _enriched_row(live_payment_profile_id=None),
    ])
    p = plans[0]
    assert p["can_execute"] is False
    assert any("3ID" in r for r in p["abort_reasons"])


def test_plans_abort_when_target_bp_priority_unresolved(phase2):
    """是正後 BP の priority 未解決 → override 無音失敗リスク → 送信禁止."""
    plans = phase2.build_combined_revise_plans([
        _enriched_row(target_bp_priority=None,
                       target_bp_priority_reason="multi-domestic-ambiguous"),
    ])
    p = plans[0]
    assert p["can_execute"] is False
    assert any("priority" in r for r in p["abort_reasons"])


def test_plans_multiple_rows_are_independent(phase2):
    """複数 row の can_execute / plan 生成が独立."""
    plans = phase2.build_combined_revise_plans([
        _enriched_row(ebay_item_id="A", ship_cost_usd=20.0),
        _enriched_row(ebay_item_id="B", ship_cost_usd=None),  # abort
        _enriched_row(ebay_item_id="C", ship_cost_usd=35.0),
    ])
    a, b, c = plans
    assert a["can_execute"] is True and a["send_ship_cost_usd"] == 20.0
    assert b["can_execute"] is False
    assert c["can_execute"] is True and c["send_ship_cost_usd"] == 35.0


def test_plans_send_xml_includes_current_override_value(phase2):
    """override 再送 XML の入力: send_ship_cost_usd が現行 override 値と一致
    (BP default にリセットされない証拠)."""
    for cost in [0.0, 20.0, 35.0, 52.0]:
        plans = phase2.build_combined_revise_plans([_enriched_row(ship_cost_usd=cost)])
        p = plans[0]
        assert p["send_ship_cost_usd"] == cost, (
            f"cost={cost}: send_ship_cost_usd が現行 override 値と乖離 "
            f"(BP default にリセットされる恐れ)"
        )
        assert p["expected_buyer_ship_after"] == cost
