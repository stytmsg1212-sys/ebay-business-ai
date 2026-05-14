"""W84 候補 D: 4 区分 primary_market 別の eBay 出品 XML / 送料 expected の整合テスト.

`reference_shipping_tariff_logic.md` v1.0 § 4.2 / § 5.3 マトリクス準拠.
関税額は post-tariff 期暫定として `price * 0.20` 近似値 (W89 で strict 化予定).
"""
import pytest

from monitor.ebay_lister import (
    _compute_shipping_override_for_market,
    _build_shipping_service_cost_override_list_xml,
)
from monitor.rank_calculator import check_shipping_cost


# ============================================================================
# _compute_shipping_override_for_market : 4 区分マトリクスの値計算
# ============================================================================

class TestComputeShippingOverrideForMarket:
    """4 区分 primary_market 別の (adjusted_price, us_override) tuple."""

    def test_us_only_includes_tariff_in_price_and_us_free(self):
        """US_only: 商品価格に関税包含 + US 送料 $0 Free."""
        adjusted, us_override = _compute_shipping_override_for_market(100.0, "US_only")
        assert adjusted == 120.0, "$100 + 関税 $20 = $120"
        assert us_override == 0.0, "US Free Shipping"

    def test_global_only_us_free_but_no_tariff_in_price(self):
        """global_only: 商品代のみ + US 送料 $0 (自腹許容)."""
        adjusted, us_override = _compute_shipping_override_for_market(100.0, "global_only")
        assert adjusted == 100.0, "商品代のみ"
        assert us_override == 0.0, "US Free Shipping (自腹リスク許容)"

    def test_mixed_global_tariff_in_us_shipping(self):
        """mixed_global: 商品代のみ + US 送料に関税近似値."""
        adjusted, us_override = _compute_shipping_override_for_market(100.0, "mixed_global")
        assert adjusted == 100.0
        assert us_override == 20.0, "US 送料に DDP 関税 $20"

    def test_unknown_defaults_to_mixed_global(self):
        """unknown: mixed_global と同じ default (商品代 + 送料関税)."""
        adjusted, us_override = _compute_shipping_override_for_market(100.0, "unknown")
        assert adjusted == 100.0
        assert us_override == 20.0

    def test_none_market_defaults_to_mixed_global(self):
        """None / 未知値: 後方互換で mixed_global default."""
        adjusted, us_override = _compute_shipping_override_for_market(100.0, None)
        assert adjusted == 100.0
        assert us_override == 20.0

    def test_unknown_string_value_defaults_to_mixed_global(self):
        """invalid market 文字列: default 動作 (silent fallback)."""
        adjusted, us_override = _compute_shipping_override_for_market(100.0, "FOO_BAR")
        assert adjusted == 100.0
        assert us_override == 20.0

    def test_case_insensitive_market_string(self):
        """市場名は大文字小文字を許容."""
        a1, u1 = _compute_shipping_override_for_market(100.0, "US_ONLY")
        a2, u2 = _compute_shipping_override_for_market(100.0, "us_only")
        assert (a1, u1) == (a2, u2) == (120.0, 0.0)

    def test_zero_price_returns_zero_with_no_override(self):
        """price=0 / None / 負値: override=None で安全側 fallback."""
        assert _compute_shipping_override_for_market(0.0, "US_only") == (0.0, None)
        assert _compute_shipping_override_for_market(None, "US_only") == (0.0, None)
        assert _compute_shipping_override_for_market(-50.0, "mixed_global") == (-50.0, None)

    def test_custom_tariff_ratio_propagates(self):
        """tariff_ratio override で関税近似値が変わる."""
        adjusted, us_override = _compute_shipping_override_for_market(
            100.0, "US_only", tariff_ratio=0.15,
        )
        assert adjusted == 115.0, "$100 + 15% = $115"

    def test_us_only_rounds_to_two_decimals(self):
        """商品価格 + 関税 は 小数第 2 位で丸め (USD cents)."""
        adjusted, _ = _compute_shipping_override_for_market(99.99, "US_only", tariff_ratio=0.20)
        # 99.99 * 0.20 = 19.998 → round=20.00, total=119.99 (after rounding twice)
        assert adjusted == pytest.approx(119.99, abs=0.02)


# ============================================================================
# _build_shipping_service_cost_override_list_xml: International entry 拡張
# ============================================================================

class TestShippingOverrideListXmlMultiEntry:
    """W84: Domestic + International 両方の entry が出力可能."""

    def test_domestic_only_legacy_behavior_preserved(self):
        """旧 signature (cost / additional のみ) は Domestic 1 entry のみで後方互換."""
        xml = _build_shipping_service_cost_override_list_xml(cost=20.0, additional=20.0)
        assert '<ShippingServiceType>Domestic</ShippingServiceType>' in xml
        assert '<ShippingServiceType>International</ShippingServiceType>' not in xml

    def test_international_only_emits_intl_entry(self):
        """intl_cost のみで Domestic は省略・International のみ entry 出力."""
        xml = _build_shipping_service_cost_override_list_xml(
            cost=None, additional=None,
            intl_cost=12.0, intl_additional=12.0,
        )
        assert '<ShippingServiceType>International</ShippingServiceType>' in xml
        assert '<ShippingServiceType>Domestic</ShippingServiceType>' not in xml
        assert '<ShippingServiceCost currencyID="USD">12.00</ShippingServiceCost>' in xml

    def test_both_domestic_and_international_entries(self):
        """両方指定で 2 entry が並ぶ."""
        xml = _build_shipping_service_cost_override_list_xml(
            cost=0.0, additional=0.0,
            intl_cost=12.0, intl_additional=12.0,
        )
        assert xml.count('<ShippingServiceCostOverride>') == 2
        assert '<ShippingServiceType>Domestic</ShippingServiceType>' in xml
        assert '<ShippingServiceType>International</ShippingServiceType>' in xml
        # entry 順序は Domestic → International (XSD 規約)
        domestic_idx = xml.find('<ShippingServiceType>Domestic</ShippingServiceType>')
        intl_idx = xml.find('<ShippingServiceType>International</ShippingServiceType>')
        assert domestic_idx < intl_idx

    def test_all_none_returns_empty(self):
        """全 None で空文字 (BP 完全踏襲)."""
        xml = _build_shipping_service_cost_override_list_xml(
            cost=None, additional=None, intl_cost=None, intl_additional=None,
        )
        assert xml == ''


# ============================================================================
# check_shipping_cost: 4 区分別 expected で WARNING / OK 判定が正しく分岐
# ============================================================================

class TestCheckShippingCost4Market:
    """W84: primary_market で expected が分岐."""

    def test_us_only_expects_zero(self):
        """US_only: 送料 $0 が expected. $0 actual で OK."""
        r = check_shipping_cost(price=100.0, shipping_cost=0.0, primary_market="US_only")
        assert r['expected'] == 0.0
        assert r['is_valid'] is True
        assert r['status'] == "OK"

    def test_us_only_warning_if_shipping_set(self):
        """US_only: 送料 > $0 は WARNING (関税は商品価格包含のはず)."""
        r = check_shipping_cost(price=100.0, shipping_cost=20.0, primary_market="US_only")
        assert r['expected'] == 0.0
        assert r['is_valid'] is False
        assert r['status'] == "WARNING"
        assert "関税包含" in r['message'] or "自腹" in r['message']

    def test_global_only_expects_zero(self):
        """global_only: 送料 $0 が expected (自腹リスク許容)."""
        r = check_shipping_cost(price=100.0, shipping_cost=0.0, primary_market="global_only")
        assert r['expected'] == 0.0
        assert r['is_valid'] is True

    def test_mixed_global_expects_20pct_of_price(self):
        """mixed_global: price * 0.20 ±15% が expected."""
        r = check_shipping_cost(price=100.0, shipping_cost=20.0, primary_market="mixed_global")
        assert r['expected'] == 20.0
        assert r['is_valid'] is True

    def test_mixed_global_warning_if_zero(self):
        """mixed_global: 送料 $0 は WARNING (関税が送料欄に乗ってない)."""
        r = check_shipping_cost(price=100.0, shipping_cost=0.0, primary_market="mixed_global")
        assert r['is_valid'] is False
        assert r['status'] == "WARNING"

    def test_unknown_defaults_to_mixed_global(self):
        """unknown: mixed_global と同じ expected."""
        r = check_shipping_cost(price=100.0, shipping_cost=20.0, primary_market="unknown")
        assert r['expected'] == 20.0
        assert r['is_valid'] is True

    def test_none_market_backward_compatible(self):
        """primary_market=None: 旧仕様 price * 0.20 expected (後方互換)."""
        r = check_shipping_cost(price=100.0, shipping_cost=20.0, primary_market=None)
        assert r['expected'] == 20.0
        assert r['is_valid'] is True

    def test_case_insensitive_market(self):
        """大小文字混在でも正しく分岐."""
        r1 = check_shipping_cost(100.0, 0.0, primary_market="US_ONLY")
        r2 = check_shipping_cost(100.0, 0.0, primary_market="us_only")
        assert r1['expected'] == r2['expected'] == 0.0
        assert r1['is_valid'] == r2['is_valid'] is True


# ============================================================================
# Section 232 早期警告 (HIGH-3 fix): 該当 HS code で warning log 発火
# ============================================================================

class TestSection232EarlyWarning:
    """W84 HIGH-3 fix: hs_code が Section 232 該当時に warning log 出力."""

    def test_is_section_232_hs_iron_steel(self):
        from monitor.ebay_lister import _is_section_232_hs
        assert _is_section_232_hs("7208.10") is True  # 熱間圧延鋼板 I-A 50% (Chapter 72)
        assert _is_section_232_hs("7213") is True  # 鉄鋼線材 I-A (Chapter 72)
        assert _is_section_232_hs("7321.11.30") is True  # 鉄鋼ストーブ I-A 50%
        assert _is_section_232_hs("7323") is True  # 鉄鋼台所用品
        assert _is_section_232_hs("7400") is True  # 銅製品 I-A
        assert _is_section_232_hs("7615") is True  # アルミ調理器具 I-A

    def test_is_section_232_hs_appliances(self):
        from monitor.ebay_lister import _is_section_232_hs
        assert _is_section_232_hs("8516.60.40") is True  # 電気炊飯器 I-B 25%
        assert _is_section_232_hs("8418.10.00") is True  # 冷蔵庫 I-B
        assert _is_section_232_hs("8708.99") is True  # 自動車部品 I-B

    def test_is_section_232_hs_negative(self):
        from monitor.ebay_lister import _is_section_232_hs
        assert _is_section_232_hs(None) is False
        assert _is_section_232_hs("") is False
        assert _is_section_232_hs("9001.50") is False  # 光学機器、対象外
        assert _is_section_232_hs("8525") is False  # カメラ、対象外

    def test_us_only_with_section_232_hs_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="monitor.ebay_lister"):
            adjusted, override = _compute_shipping_override_for_market(
                100.0, "us_only", hs_code="7321.11",  # 鉄鋼ストーブ I-A 50%
            )
        assert adjusted == 120.0  # 関税近似値で計算は実行
        assert override == 0.0
        assert any("Section 232" in m for m in caplog.messages), "Section 232 warning が出ていない"

    def test_us_only_without_hs_no_warn(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="monitor.ebay_lister"):
            _compute_shipping_override_for_market(100.0, "us_only", hs_code=None)
        assert not any("Section 232" in m for m in caplog.messages), "hs_code=None で warn 出ない"

    def test_mixed_global_with_section_232_hs_warns(self, caplog):
        """mixed_global / unknown も US 送料欄に tariff_approx が乗るので警告対象 (HIGH-2 fix).

        現実の F1 配線では新規 W9 listing は primary_market='unknown' default = mixed_global
        等価動作のため、ここで警告しないと Section 232 警告の実効カバレッジが極小化する.
        """
        import logging
        with caplog.at_level(logging.WARNING, logger="monitor.ebay_lister"):
            _compute_shipping_override_for_market(100.0, "mixed_global", hs_code="7321")
        assert any("Section 232" in m for m in caplog.messages)

    def test_unknown_with_section_232_hs_warns(self, caplog):
        """unknown も mixed_global default 動作 → 警告対象 (HIGH-2 fix)."""
        import logging
        with caplog.at_level(logging.WARNING, logger="monitor.ebay_lister"):
            _compute_shipping_override_for_market(100.0, "unknown", hs_code="7208")
        assert any("Section 232" in m for m in caplog.messages)

    def test_global_only_with_section_232_hs_no_warn(self, caplog):
        """global_only は user 自腹覚悟で送料 $0 (赤字判断済) → 警告不要 (HIGH-2 fix)."""
        import logging
        with caplog.at_level(logging.WARNING, logger="monitor.ebay_lister"):
            _compute_shipping_override_for_market(100.0, "global_only", hs_code="7321")
        assert not any("Section 232" in m for m in caplog.messages)


# ============================================================================
# E2E: build_draft_params_from_phase3 で primary_market が貫通するか (HIGH-1 fix)
# ============================================================================

class TestEnd2EndPrimaryMarketWiring:
    """W84 HIGH-1 fix: primary_market が listing 経由 / 明示引数経由 で draft_params に出る."""

    def _minimal_args(self):
        """build_draft_params_from_phase3 への最小引数."""
        class _R:
            rank_code = "B"
            rank_label = "Good"
            ebay_condition_id = "3000"
            quick_notes = "Tested working"
        class _L:
            ebay_title = "Test Title"
            ebay_description = "Test desc"
            ebay_category_id = "12345"
            ebay_category_name = "Test Cat"
            item_specifics = {}
        return dict(
            product=None, reference=None, rank=_R(), listing=_L(),
            shipping_policy_id="PROFILE_ID",
            sku="stock:01",
            listing_price_usd=100.0,
            image_urls=["http://example.com/img.jpg"],
            config={"defaults": {}},
        )

    def test_explicit_primary_market_us_only(self):
        """明示引数 primary_market='US_only' で adjusted price + US Free."""
        from monitor.ebay_lister import build_draft_params_from_phase3
        params = build_draft_params_from_phase3(**self._minimal_args(), primary_market="US_only")
        assert params['primary_market'] == 'US_only'
        assert params['listing_price_usd'] == 120.0  # $100 + 20% 関税近似
        assert params['shipping_cost_usd_override'] == 0.0  # US Free Shipping

    def test_explicit_primary_market_mixed_global(self):
        from monitor.ebay_lister import build_draft_params_from_phase3
        params = build_draft_params_from_phase3(**self._minimal_args(), primary_market="mixed_global")
        assert params['primary_market'] == 'mixed_global'
        assert params['listing_price_usd'] == 100.0  # 商品代のみ
        assert params['shipping_cost_usd_override'] == 20.0  # 送料に関税近似

    def test_explicit_primary_market_global_only(self):
        from monitor.ebay_lister import build_draft_params_from_phase3
        params = build_draft_params_from_phase3(**self._minimal_args(), primary_market="global_only")
        assert params['primary_market'] == 'global_only'
        assert params['listing_price_usd'] == 100.0
        assert params['shipping_cost_usd_override'] == 0.0  # US 自腹許容で Free

    def test_explicit_primary_market_unknown_defaults_to_mixed_global(self):
        from monitor.ebay_lister import build_draft_params_from_phase3
        params = build_draft_params_from_phase3(**self._minimal_args(), primary_market="unknown")
        assert params['primary_market'] == 'unknown'
        assert params['listing_price_usd'] == 100.0
        assert params['shipping_cost_usd_override'] == 20.0  # mixed_global default

    def test_listing_attr_fallback_when_no_explicit_arg(self):
        """明示引数 None なら listing オブジェクトの primary_market から取る."""
        from monitor.ebay_lister import build_draft_params_from_phase3
        args = self._minimal_args()
        # listing に primary_market 属性を生やす
        args['listing'].primary_market = "US_only"
        params = build_draft_params_from_phase3(**args)
        assert params['primary_market'] == 'US_only'
        assert params['listing_price_usd'] == 120.0

    def test_explicit_arg_overrides_listing_attr(self):
        """明示引数 > listing.primary_market の優先順."""
        from monitor.ebay_lister import build_draft_params_from_phase3
        args = self._minimal_args()
        args['listing'].primary_market = "US_only"
        params = build_draft_params_from_phase3(**args, primary_market="global_only")
        assert params['primary_market'] == 'global_only'  # 明示優先

    def test_no_primary_market_anywhere_defaults_to_mixed_global_behavior(self):
        """明示引数なし、listing 属性なし → unknown default → mixed_global 動作."""
        from monitor.ebay_lister import build_draft_params_from_phase3
        params = build_draft_params_from_phase3(**self._minimal_args())
        assert params['primary_market'] == 'unknown'
        assert params['listing_price_usd'] == 100.0
        assert params['shipping_cost_usd_override'] == 20.0
