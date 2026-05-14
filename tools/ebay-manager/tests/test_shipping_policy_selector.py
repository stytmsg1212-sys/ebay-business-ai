#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shipping_policy_selector の単体テスト。

実 settings.json は読まず、各テストで最小限の config dict を組み立てて投入する。
9×N パターンの重量境界値 + in_stock True/False + None をカバー。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.shipping_policy_selector import (  # noqa: E402
    _build_range_label,
    _parse_range_label,
    _sorted_ranges,
    select_shipping_policy,
)


# =========================================================================
# テスト用 config fixture
# =========================================================================

def _make_config() -> dict:
    """settings.json の ebay_business_policies に準拠した最小 config。"""
    return {
        "ebay_business_policies": {
            "payment_policy_id": "359244671023",
            "return_policy_id": "359243687023",
            "shipping_weight_mapping_in_stock": {
                "0-500":     "IN_0_500",
                "500-1000":  "IN_500_1000",
                "1000-2000": "IN_1000_2000",
                "2000-3000": "IN_2000_3000",
                "3000-4000": "IN_3000_4000",
                "4000-5000": "IN_4000_5000",
                "5000-6000": "IN_5000_6000",
                "6000-8000": "IN_6000_8000",
                "8000-10000":  "IN_8000_10000",
                "10000-20000": "IN_10000_20000",
            },
            "shipping_weight_mapping_no_stock": {
                "0-500":     "NS_0_500",
                "500-1000":  "NS_500_1000",
                "1000-2000": "NS_1000_2000",
                "2000-3000": "NS_2000_3000",
                "3000-4000": "NS_3000_4000",
                "4000-5000": "NS_4000_5000",
                "5000-6000": "NS_5000_6000",
                "6000-8000": "NS_6000_8000",
                "8000-10000":  "NS_8000_10000",
                "10000-20000": "NS_10000_20000",
            },
        }
    }


# =========================================================================
# _parse_range_label / _sorted_ranges
# =========================================================================

class TestParseRangeLabel:
    def test_simple(self):
        assert _parse_range_label("0-500") == (0, 500)

    def test_with_spaces(self):
        assert _parse_range_label("  500 - 1000  ") == (500, 1000)

    def test_invalid_format(self):
        assert _parse_range_label("not-a-range") is None

    def test_reversed_range(self):
        assert _parse_range_label("500-100") is None

    def test_empty(self):
        assert _parse_range_label("") is None

    def test_none(self):
        assert _parse_range_label(None) is None  # type: ignore[arg-type]


class TestSortedRanges:
    def test_sorts_ascending(self):
        mapping = {
            "500-1000": "B",
            "0-500": "A",
            "1000-2000": "C",
        }
        ranges = _sorted_ranges(mapping)
        assert [r[2] for r in ranges] == ["A", "B", "C"]

    def test_skips_invalid_entries(self):
        mapping = {
            "0-500": "OK",
            "invalid": "BAD",
            "500-1000": 123,  # non-string policy_id
            "1000-2000": "",   # empty policy_id
            "2000-3000": "GOOD",
        }
        ranges = _sorted_ranges(mapping)
        assert [r[2] for r in ranges] == ["OK", "GOOD"]

    def test_non_dict_input(self):
        assert _sorted_ranges(None) == []  # type: ignore[arg-type]
        assert _sorted_ranges("bad") == []  # type: ignore[arg-type]


class TestBuildRangeLabel:
    def test_readable_g_suffix(self):
        assert _build_range_label(0, 500) == "0-500g"


# =========================================================================
# select_shipping_policy: 境界値
# =========================================================================

class TestSelectShippingPolicyBoundary:
    def test_minimum_in_stock(self):
        cfg = _make_config()
        pid, lbl = select_shipping_policy(1, True, cfg)
        assert pid == "IN_0_500"
        assert "In-stock" in lbl
        assert "0-500" in lbl

    def test_499g_in_stock(self):
        cfg = _make_config()
        pid, _ = select_shipping_policy(499, True, cfg)
        assert pid == "IN_0_500"

    def test_500g_next_range(self):
        # 半開区間: [500, 1000) に 500g は含まれる
        cfg = _make_config()
        pid, _ = select_shipping_policy(500, True, cfg)
        assert pid == "IN_500_1000"

    def test_501g_in_stock(self):
        cfg = _make_config()
        pid, _ = select_shipping_policy(501, True, cfg)
        assert pid == "IN_500_1000"

    def test_999g_boundary(self):
        cfg = _make_config()
        pid, _ = select_shipping_policy(999, True, cfg)
        assert pid == "IN_500_1000"

    def test_1000g_next_range(self):
        cfg = _make_config()
        pid, _ = select_shipping_policy(1000, True, cfg)
        assert pid == "IN_1000_2000"

    def test_max_boundary_closed_interval(self):
        # 最後のレンジは閉区間 [10000, 20000] として扱う
        cfg = _make_config()
        pid, _ = select_shipping_policy(20000, True, cfg)
        assert pid == "IN_10000_20000"

    def test_exceeds_max_uses_largest(self):
        cfg = _make_config()
        pid, lbl = select_shipping_policy(25000, True, cfg)
        assert pid == "IN_10000_20000"
        assert "exceeded" in lbl


class TestSelectShippingPolicyStock:
    def test_in_stock_vs_no_stock(self):
        cfg = _make_config()
        pid_in, _ = select_shipping_policy(100, True, cfg)
        pid_out, _ = select_shipping_policy(100, False, cfg)
        assert pid_in == "IN_0_500"
        assert pid_out == "NS_0_500"

    def test_out_of_stock_label_says_so(self):
        cfg = _make_config()
        _, lbl = select_shipping_policy(2000, False, cfg)
        assert "Out-of-stock" in lbl

    def test_multiple_weights_in_stock(self):
        """3x3 in_stock + weight 行列を網羅的に検証。"""
        cfg = _make_config()
        weights_ids = [
            (200, "IN_0_500"),
            (600, "IN_500_1000"),
            (1500, "IN_1000_2000"),
            (2500, "IN_2000_3000"),
            (3500, "IN_3000_4000"),
            (4500, "IN_4000_5000"),
            (5500, "IN_5000_6000"),
            (7000, "IN_6000_8000"),
            (9000, "IN_8000_10000"),
            (15000, "IN_10000_20000"),
        ]
        for w, expected_pid in weights_ids:
            pid, _ = select_shipping_policy(w, True, cfg)
            assert pid == expected_pid, f"{w}g → {pid} != {expected_pid}"

    def test_multiple_weights_no_stock(self):
        cfg = _make_config()
        weights_ids = [
            (200, "NS_0_500"),
            (700, "NS_500_1000"),
            (3000, "NS_3000_4000"),
            (12000, "NS_10000_20000"),
        ]
        for w, expected_pid in weights_ids:
            pid, _ = select_shipping_policy(w, False, cfg)
            assert pid == expected_pid, f"{w}g (no_stock) → {pid} != {expected_pid}"


class TestSelectShippingPolicyWeightNone:
    def test_weight_none_in_stock(self):
        """weight_g=None は最小レンジに fallback。"""
        cfg = _make_config()
        pid, lbl = select_shipping_policy(None, True, cfg)
        assert pid == "IN_0_500"
        assert "In-stock" in lbl

    def test_weight_none_no_stock(self):
        cfg = _make_config()
        pid, _ = select_shipping_policy(None, False, cfg)
        assert pid == "NS_0_500"

    def test_weight_zero(self):
        cfg = _make_config()
        pid, _ = select_shipping_policy(0, True, cfg)
        assert pid == "IN_0_500"

    def test_weight_negative_fallback(self):
        cfg = _make_config()
        pid, _ = select_shipping_policy(-100, True, cfg)
        assert pid == "IN_0_500"

    def test_weight_non_int_string(self):
        cfg = _make_config()
        pid, _ = select_shipping_policy("not a number", True, cfg)  # type: ignore[arg-type]
        assert pid == "IN_0_500"

    def test_weight_float_coerced(self):
        cfg = _make_config()
        pid, _ = select_shipping_policy(500.5, True, cfg)
        # 500.5 → int(500) → [500, 1000) に入る
        assert pid == "IN_500_1000"


# =========================================================================
# select_shipping_policy: 設定欠落時の例外
# =========================================================================

class TestSelectShippingPolicyConfigErrors:
    def test_missing_business_policies(self):
        with pytest.raises(ValueError, match="ebay_business_policies"):
            select_shipping_policy(100, True, {})

    def test_missing_mapping(self):
        cfg = {"ebay_business_policies": {"payment_policy_id": "x"}}
        with pytest.raises(ValueError, match="shipping_weight_mapping_in_stock"):
            select_shipping_policy(100, True, cfg)

    def test_missing_no_stock_mapping(self):
        cfg = {
            "ebay_business_policies": {
                "shipping_weight_mapping_in_stock": {"0-500": "X"},
                # no_stock 欠落
            }
        }
        with pytest.raises(ValueError, match="shipping_weight_mapping_no_stock"):
            select_shipping_policy(100, False, cfg)

    def test_empty_mapping(self):
        cfg = {
            "ebay_business_policies": {
                "shipping_weight_mapping_in_stock": {},
            }
        }
        with pytest.raises(ValueError):
            select_shipping_policy(100, True, cfg)

    def test_all_invalid_entries(self):
        cfg = {
            "ebay_business_policies": {
                "shipping_weight_mapping_in_stock": {
                    "invalid": "X",
                    "also-bad": "Y",
                },
            }
        }
        with pytest.raises(ValueError, match="no valid weight ranges"):
            select_shipping_policy(100, True, cfg)

    def test_config_not_dict(self):
        with pytest.raises(ValueError, match="config must be a dict"):
            select_shipping_policy(100, True, "not a dict")  # type: ignore[arg-type]


# =========================================================================
# settings.json 実構造との整合性
# =========================================================================

class TestRealSettingsJsonStructure:
    """実 settings.json が読める場合、構造が期待どおりか検証。

    settings.json が存在しない CI 環境では skip。
    """

    def _load(self):
        p = _PROJECT_ROOT / "settings.json"
        if not p.exists():
            pytest.skip("settings.json not available in this environment")
        import json
        return json.loads(p.read_text(encoding="utf-8"))

    def test_real_in_stock_100g(self):
        cfg = self._load()
        if "ebay_business_policies" not in cfg:
            pytest.skip("no ebay_business_policies in settings.json")
        pid, lbl = select_shipping_policy(100, True, cfg)
        assert pid  # 何らかの policy_id を返す
        assert "In-stock" in lbl

    def test_real_no_stock_5000g(self):
        cfg = self._load()
        if "ebay_business_policies" not in cfg:
            pytest.skip("no ebay_business_policies in settings.json")
        pid, lbl = select_shipping_policy(5000, False, cfg)
        assert pid
        assert "Out-of-stock" in lbl

    def test_real_weight_none(self):
        cfg = self._load()
        if "ebay_business_policies" not in cfg:
            pytest.skip("no ebay_business_policies in settings.json")
        pid, _ = select_shipping_policy(None, True, cfg)
        assert pid
