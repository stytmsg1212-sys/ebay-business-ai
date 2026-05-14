#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
listing_generator の単体テスト。

Claude API 呼出しは unittest.mock.patch で全モック化。
placeholder 置換 / build_* ヘルパ / detect_mode / エンドツーエンドを検証。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.listing_generator import (  # noqa: E402
    GADGET_BRANDS,
    GADGET_CATEGORIES,
    GeneratedListing,
    _compose_placeholder_values,
    build_includes_rows,
    build_spec_strip_rows,
    build_specs_rows,
    detect_mode,
    generate_listing,
    render_description,
)


# =========================================================================
# テスト用 ダミー dataclass (循環 import 回避で軽量版)
# =========================================================================

@dataclass
class _ScrapedProduct:
    url: str = ""
    platform: str = "mercari"
    title_ja: Optional[str] = None
    price_jpy: Optional[int] = None
    condition_ja: Optional[str] = None
    includes_ja: Optional[str] = None
    description_ja: Optional[str] = None
    weight_hint_g: Optional[int] = None
    image_urls: list = None  # type: ignore

    def __post_init__(self):
        if self.image_urls is None:
            self.image_urls = []


@dataclass
class _ReferenceListing:
    item_id: str = ""
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    item_specifics_keys: list = None  # type: ignore
    title_sample: Optional[str] = None
    condition_id: Optional[str] = None

    def __post_init__(self):
        if self.item_specifics_keys is None:
            self.item_specifics_keys = []


@dataclass
class _Rank:
    rank_code: str = "A"
    rank_label: str = "Excellent"
    rank_jp: str = "Tested \u00b7 Minor Wear"
    ebay_condition_id: str = "3000"
    confidence: float = 0.9
    reasoning: str = "test"


def _make_claude_response(payload: dict) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps(payload, ensure_ascii=False)
    resp = MagicMock()
    resp.content = [block]
    usage = MagicMock()
    usage.input_tokens = 500
    usage.output_tokens = 300
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0
    resp.usage = usage
    return resp


# =========================================================================
# render_description
# =========================================================================

class TestRenderDescription:
    def test_basic_replacement(self):
        tpl = "<div>{{product_name}}</div>"
        out = render_description(tpl, {"product_name": "Sony WH-1000XM5"})
        assert out == "<div>Sony WH-1000XM5</div>"

    def test_missing_key_becomes_empty(self):
        tpl = "<div>{{product_name}} | {{missing}}</div>"
        out = render_description(tpl, {"product_name": "X"})
        assert out == "<div>X | </div>"

    def test_single_braces_preserved(self):
        """CSS `{ ... }` は破壊されないこと。"""
        tpl = ".cls { color: red; }"
        out = render_description(tpl, {})
        assert out == ".cls { color: red; }"

    def test_multiple_placeholders(self):
        tpl = "{{a}}-{{b}}-{{a}}"
        out = render_description(tpl, {"a": "X", "b": "Y"})
        assert out == "X-Y-X"

    def test_empty_template(self):
        assert render_description("", {"a": "b"}) == ""

    def test_roundtrip_full_v4_placeholders(self):
        # 14 種の placeholder 全てを含むミニテンプレでの往復
        tpl = (
            "<div class='wrap {{mode_class}}'>"
            "<h1>{{product_name}}</h1>"
            "<p class='sub'>{{product_sub}}</p>"
            "<span class='rank'>{{rank}} / {{rank_label}} / {{rank_jp}}</span>"
            "<div class='notes'>{{quick_notes}}</div>"
            "<div class='inc'>{{includes_rows}}</div>"
            "<table>{{specs_rows}}</table>"
            "<div class='strip'>{{spec_strip_rows}}</div>"
            "<div class='ship'>"
            "{{shipping_origin}}|{{shipping_carrier}}|{{shipping_handling}}|"
            "{{shipping_delivery_us}}|{{shipping_packaging}}|{{shipping_notes}}"
            "</div></div>"
        )
        values = {
            "mode_class": "default",
            "product_name": "A", "product_sub": "B",
            "rank": "A", "rank_label": "Excellent", "rank_jp": "JP",
            "quick_notes": "N",
            "includes_rows": "<i>inc</i>",
            "specs_rows": "<s>spec</s>",
            "spec_strip_rows": "<st>trio</st>",
            "shipping_origin": "Tokyo",
            "shipping_carrier": "DHL",
            "shipping_handling": "1-3",
            "shipping_delivery_us": "6-10",
            "shipping_packaging": "Box",
            "shipping_notes": "",
        }
        out = render_description(tpl, values)
        # placeholder が 14 種全て消えていること
        assert "{{" not in out
        assert "Tokyo" in out
        assert "<i>inc</i>" in out


# =========================================================================
# build_includes_rows
# =========================================================================

class TestBuildIncludesRows:
    def test_basic(self):
        out = build_includes_rows([
            {"label": "Main Unit", "detail": "Sony WH-1000XM5"},
        ])
        assert "mh-inc" in out
        assert "Main Unit" in out
        assert "Sony WH-1000XM5" in out

    def test_html_escape(self):
        out = build_includes_rows([
            {"label": "Box <script>", "detail": "Original & authentic"},
        ])
        # 入力に含まれる <script> が literal として残らず escape される
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        # & は &amp; にエスケープされる
        assert "&amp;" in out
        # 構造タグ <strong> は build_* が出力する HTML として残っていて良い
        assert '<div class="mh-inc"><strong>' in out

    def test_empty_list(self):
        assert build_includes_rows([]) == ""

    def test_skips_invalid_items(self):
        out = build_includes_rows([
            {"label": "", "detail": ""},           # 両方空
            {"label": "Good", "detail": "Item"},    # 正常
            "not a dict",                           # 無効
        ])
        assert "Good" in out
        assert out.count("mh-inc") == 1


# =========================================================================
# build_specs_rows
# =========================================================================

class TestBuildSpecsRows:
    def test_tuple_input(self):
        out = build_specs_rows([("Brand", "Sony"), ("Model", "WH-1000XM5")])
        assert "<tr><td>Brand</td><td>Sony</td></tr>" in out
        assert "<tr><td>Model</td><td>WH-1000XM5</td></tr>" in out

    def test_dict_input(self):
        out = build_specs_rows([{"key": "Color", "value": "Black"}])
        assert "Color" in out and "Black" in out

    def test_dict_name_fallback(self):
        # claude_data.specs が {"name":..., "value":...} 形式でも動く
        out = build_specs_rows([{"name": "Type", "value": "Wireless"}])
        assert "Type" in out

    def test_skip_empty_keys(self):
        out = build_specs_rows([("", "val"), ("OK", "v")])
        assert out.count("<tr>") == 1

    def test_html_escape(self):
        out = build_specs_rows([("<key>", "<val>")])
        assert "<key>" not in out
        assert "&lt;key&gt;" in out

    def test_empty(self):
        assert build_specs_rows([]) == ""


# =========================================================================
# build_spec_strip_rows
# =========================================================================

class TestBuildSpecStripRows:
    def test_three_items(self):
        out = build_spec_strip_rows([
            ("BATTERY", "30h"), ("ANC", "Active"), ("BT", "5.2"),
        ])
        assert out.count("class=\"k\"") == 3
        assert out.count("class=\"v\"") == 3

    def test_truncates_to_three(self):
        out = build_spec_strip_rows([
            ("A", "1"), ("B", "2"), ("C", "3"), ("D", "4"),
        ])
        assert out.count("class=\"k\"") == 3
        assert "D" not in out

    def test_empty(self):
        assert build_spec_strip_rows([]) == ""


# =========================================================================
# detect_mode
# =========================================================================

class TestDetectMode:
    def test_gadget_by_category_consumer_electronics(self):
        assert detect_mode("293", None, 0) == "gadget"

    def test_gadget_by_category_cameras(self):
        assert detect_mode("625", None, 0) == "gadget"

    def test_gadget_by_category_business_industrial(self):
        assert detect_mode("12576", None, 0) == "gadget"

    def test_gadget_by_brand_keyence(self):
        assert detect_mode(None, "KEYENCE", 0) == "gadget"

    def test_gadget_by_brand_sony(self):
        assert detect_mode("99999", "Sony", 0) == "gadget"

    def test_gadget_by_specs_count(self):
        assert detect_mode("9999", "Unknown Brand", 3) == "gadget"

    def test_gadget_by_specs_count_large(self):
        assert detect_mode(None, None, 5) == "gadget"

    def test_default_fashion_category(self):
        # ファッションカテゴリっぽい偽 id
        assert detect_mode("11450", "NoBrand", 2) == "default"

    def test_default_no_inputs(self):
        assert detect_mode(None, None, 0) == "default"

    def test_specs_count_non_int(self):
        # 非数値 specs_count は default 側に倒れる (brand/category も non-gadget)
        assert detect_mode(None, None, "bad") == "default"  # type: ignore[arg-type]

    def test_category_with_slash_prefix(self):
        # 正源ルール: category_id.split('/')[0] が gadget set に含まれれば gadget
        assert detect_mode("293/sub/leaf", None, 0) == "gadget"

    def test_brand_whitespace_trimmed(self):
        assert detect_mode(None, "  Canon  ", 0) == "gadget"

    def test_gadget_categories_frozenset(self):
        # 定数が frozenset であること (変更不可)
        assert "293" in GADGET_CATEGORIES
        assert "KEYENCE" in GADGET_BRANDS


# =========================================================================
# _compose_placeholder_values (内部関数)
# =========================================================================

class TestComposePlaceholderValues:
    def test_default_shipping_fallbacks(self):
        """Claude が shipping フィールドを返さない場合、固定デフォルトが入る。"""
        rank = _Rank()
        data = {"title": "Hello", "includes_items": [], "specs": []}
        values = _compose_placeholder_values(data, rank)
        assert "Tokyo" in values["shipping_origin"]
        assert "DHL" in values["shipping_carrier"]
        assert "1" in values["shipping_handling"]  # "1–3 business days" 系

    def test_rank_fields_filled_from_rank_dataclass(self):
        rank = _Rank(rank_code="PO", rank_label="Power-On Only", rank_jp="Powers On")
        data = {"title": "X", "includes_items": [], "specs": []}
        values = _compose_placeholder_values(data, rank)
        assert values["rank"] == "PO"
        assert values["rank_label"] == "Power-On Only"
        assert values["rank_jp"] == "Powers On"

    def test_html_escape_product_name(self):
        data = {"title": "<script>alert(1)</script>", "includes_items": [], "specs": []}
        values = _compose_placeholder_values(data, _Rank())
        assert "<script>" not in values["product_name"]
        assert "&lt;script&gt;" in values["product_name"]

    def test_shipping_override_in_stock(self):
        """in_stock override (Ships within 1 day) が Claude デフォルトより優先される。"""
        rank = _Rank()
        data = {
            "title": "X", "includes_items": [], "specs": [],
            "shipping_handling": "1-3 business days",  # Claude が返してきた値
            "shipping_delivery_us": "6-10 business days typical",
        }
        values = _compose_placeholder_values(
            data, rank,
            shipping_override=("Ships within 1 business day", "7-12 business days"),
        )
        assert values["shipping_handling"] == "Ships within 1 business day"
        assert values["shipping_delivery_us"] == "7-12 business days"

    def test_shipping_override_out_of_stock(self):
        """out_of_stock override は 7 days handling を注入する。"""
        rank = _Rank()
        data = {"title": "X", "includes_items": [], "specs": []}
        values = _compose_placeholder_values(
            data, rank,
            shipping_override=("Ships within 7 business days", "13-20 business days"),
        )
        assert "7" in values["shipping_handling"]
        assert "13" in values["shipping_delivery_us"]

    def test_shipping_override_empty_falls_back_to_claude(self):
        """override が空タプルなら Claude 返却値にフォールバック。"""
        rank = _Rank()
        data = {
            "title": "X", "includes_items": [], "specs": [],
            "shipping_handling": "CLAUDE_HANDLING",
        }
        values = _compose_placeholder_values(
            data, rank, shipping_override=("", ""),
        )
        assert values["shipping_handling"] == "CLAUDE_HANDLING"


class TestResolveShippingTiming:
    """_resolve_shipping_timing (settings.json からの切替) の単体試験。"""
    from monitor.listing_generator import _resolve_shipping_timing

    def test_in_stock_returns_in_stock_tier(self):
        from monitor.listing_generator import _resolve_shipping_timing
        cfg = {
            "shipping_timing": {
                "in_stock": {
                    "handling_label": "Ships within 1 business day",
                    "delivery_label": "7-12 days",
                },
                "out_of_stock": {
                    "handling_label": "Ships within 7 business days",
                    "delivery_label": "13-20 days",
                },
            }
        }
        handling, delivery = _resolve_shipping_timing(cfg, in_stock=True)
        assert handling == "Ships within 1 business day"
        assert delivery == "7-12 days"

    def test_out_of_stock_returns_out_of_stock_tier(self):
        from monitor.listing_generator import _resolve_shipping_timing
        cfg = {
            "shipping_timing": {
                "in_stock": {"handling_label": "1d", "delivery_label": "7-12"},
                "out_of_stock": {"handling_label": "7d", "delivery_label": "13-20"},
            }
        }
        handling, delivery = _resolve_shipping_timing(cfg, in_stock=False)
        assert handling == "7d"
        assert delivery == "13-20"

    def test_missing_config_returns_empty_tuple(self):
        from monitor.listing_generator import _resolve_shipping_timing
        assert _resolve_shipping_timing(None, in_stock=True) == ("", "")
        assert _resolve_shipping_timing({}, in_stock=False) == ("", "")
        assert _resolve_shipping_timing({"shipping_timing": "not a dict"}, True) == ("", "")

    def test_missing_tier_returns_empty(self):
        from monitor.listing_generator import _resolve_shipping_timing
        cfg = {"shipping_timing": {"in_stock": {"handling_label": "1d"}}}
        # out_of_stock が欠落
        assert _resolve_shipping_timing(cfg, in_stock=False) == ("", "")


# =========================================================================
# generate_listing (Claude mock によるエンドツーエンド)
# =========================================================================

class TestGenerateListing:
    def _tpl(self) -> str:
        return (
            "<div class='mh-wrap {{mode_class}}'>"
            "<h1>{{product_name}}</h1>"
            "<p>{{quick_notes}}</p>"
            "<div>{{includes_rows}}</div>"
            "<table>{{specs_rows}}</table>"
            "<footer>{{shipping_origin}}</footer>"
            "</div>"
        )

    def test_with_reference_category_override(self):
        """参考 listing の category_id が Claude 戻り値より優先される。"""
        product = _ScrapedProduct(
            title_ja="ソニー WH-1000XM5 ブラック 美品", price_jpy=32000,
            condition_ja="中古 美品",
        )
        reference = _ReferenceListing(
            item_id="123",
            category_id="293",
            category_name="Consumer Electronics",
            item_specifics_keys=["Brand", "Model", "Type"],
        )
        rank = _Rank()

        claude_payload = {
            "title": "Sony WH-1000XM5 Wireless Headphones Black Excellent",
            "product_sub": "Flagship model",
            "quick_notes": "Tested and working",
            "includes_items": [{"label": "Headphones", "detail": "Sony WH-1000XM5"}],
            "specs": [
                {"key": "Brand", "value": "Sony"},
                {"key": "Model", "value": "WH-1000XM5"},
                {"key": "Type", "value": "Wireless"},
            ],
            "spec_strip": [],
            "category_id": "99999",  # Claude が誤推定 → 参考 listing で上書きされるはず
            "category_name": "Wrong Category",
            "category_candidates": [],
            "item_specifics": {"Brand": "Sony", "Model": "WH-1000XM5", "Type": "Wireless"},
            "shipping_origin": "Tokyo, Japan",
            "shipping_carrier": "DHL SpeedPAK",
            "shipping_handling": "1-3 business days",
            "shipping_delivery_us": "6-10 business days",
            "shipping_packaging": "Double-boxed",
            "shipping_notes": "",
            "product_name": "Sony WH-1000XM5 Wireless Headphones Black Excellent",
        }
        resp = _make_claude_response(claude_payload)
        fake_client = MagicMock()
        fake_client.messages.create.return_value = resp

        # 2026-04-22 v2: Taxonomy API を mock。reference の 293 が Taxonomy 結果に
        # 含まれるようにして、v2 の「reference match」分岐を通す。
        fake_taxonomy = [
            {"category_id": "293", "category_name": "Consumer Electronics",
             "ancestors_names": [], "ancestors": [], "is_leaf": True,
             "category_tree_node_level": 1},
            {"category_id": "112529", "category_name": "Headphones",
             "ancestors_names": ["Portable Audio"], "ancestors": ["15052"],
             "is_leaf": True, "category_tree_node_level": 3},
        ]
        with patch("monitor.listing_generator._get_client", return_value=fake_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                with patch(
                    "monitor.ebay_taxonomy.get_category_suggestions",
                    return_value=fake_taxonomy,
                ):
                    result = generate_listing(product, reference, rank, self._tpl())

        assert result.generate_error is None
        assert result.ebay_title == "Sony WH-1000XM5 Wireless Headphones Black Excellent"
        # Category は Taxonomy v2 で reference match → 293
        assert result.ebay_category_id == "293"
        assert result.ebay_category_name == "Consumer Electronics"
        # Item specifics keys 反映
        assert result.item_specifics.get("Brand") == "Sony"
        # Gadget Mode: category 293 が GADGET_CATEGORIES
        assert result.mode_class == "gadget"
        # Description: placeholder 置換完了
        assert "Sony WH-1000XM5" in result.ebay_description
        assert "{{" not in result.ebay_description  # 未置換 placeholder 残存なし
        assert "Tokyo, Japan" in result.ebay_description

    def test_without_reference_uses_candidates(self):
        """参考 listing なしの場合、Claude 提案 category がそのまま採用される。"""
        product = _ScrapedProduct(title_ja="謎商品", price_jpy=1000)
        rank = _Rank()

        claude_payload = {
            "title": "Mystery Item",
            "product_sub": "",
            "quick_notes": "Tested working",
            "includes_items": [],
            "specs": [{"key": "Brand", "value": "Unbranded"}],
            "spec_strip": [],
            "category_id": "11450",
            "category_name": "Clothing",
            "category_candidates": [
                {"category_id": "11450", "category_name": "Clothing", "reasoning": "candidate 1"},
                {"category_id": "267", "category_name": "Books", "reasoning": "candidate 2"},
                {"category_id": "888", "category_name": "Collectibles", "reasoning": "candidate 3"},
            ],
            "item_specifics": {"Brand": "Unbranded"},
            "product_name": "Mystery Item",
        }
        resp = _make_claude_response(claude_payload)
        fake_client = MagicMock()
        fake_client.messages.create.return_value = resp

        # 2026-04-22: Taxonomy API を mock (real API 呼出しを防止)。
        # 空リストを返すことで Claude の category_id がそのまま採用される。
        with patch("monitor.listing_generator._get_client", return_value=fake_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                with patch(
                    "monitor.ebay_taxonomy.get_category_suggestions",
                    return_value=[],
                ):
                    result = generate_listing(product, None, rank, self._tpl())

        assert result.generate_error is None
        assert result.ebay_category_id == "11450"
        assert len(result.category_candidates) == 3
        assert result.mode_class == "default"

    def test_missing_api_key(self):
        product = _ScrapedProduct(title_ja="X")
        rank = _Rank()
        with patch("monitor.listing_generator._get_client", return_value=None):
            result = generate_listing(product, None, rank, self._tpl())
        assert result.generate_error is not None
        assert "ANTHROPIC_API_KEY" in result.generate_error
        assert result.ebay_title == ""

    def test_taxonomy_candidates_not_overwritten_by_claude(self):
        """HIGH-1 regression (2026-04-22): Taxonomy v2 が設定した category_candidates が
        後続の Claude データで上書きされないこと。上書きされると UI ラジオが無効 ID を
        提示して AddItem 失敗 → 金銭損失。"""
        product = _ScrapedProduct(title_ja="Test", price_jpy=5000)
        rank = _Rank()
        claude_payload = {
            "title": "Test Item",
            "product_sub": "",
            "quick_notes": "Tested",
            "includes_items": [],
            "specs": [{"key": "Brand", "value": "TestBrand"}],
            "spec_strip": [],
            # Claude が勝手な category 候補を返す (古い/架空の ID 含む可能性)
            "category_id": "FAKE_CLAUDE_ID",
            "category_name": "Claude Fake Category",
            "category_candidates": [
                {"category_id": "99999", "category_name": "Fake1",
                 "reasoning": "Claude の予想 (無効の可能性)"},
                {"category_id": "88888", "category_name": "Fake2",
                 "reasoning": "Claude の予想 (無効の可能性)"},
            ],
            "item_specifics": {"Brand": "TestBrand"},
            "product_name": "Test Item",
        }
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _make_claude_response(claude_payload)
        # Taxonomy v2 が有効 leaf 3 件を返す
        fake_taxonomy = [
            {"category_id": "14990", "category_name": "Home Speakers & Subwoofers",
             "ancestors_names": ["Home Audio", "Consumer Electronics"],
             "ancestors": ["14969", "293"], "is_leaf": True,
             "category_tree_node_level": 4},
            {"category_id": "111694", "category_name": "Audio Docks & Mini Speakers",
             "ancestors_names": ["Portable Audio"], "ancestors": ["15052"],
             "is_leaf": True, "category_tree_node_level": 3},
            {"category_id": "47091", "category_name": "Speakers",
             "ancestors_names": ["Pro Audio Equipment"], "ancestors": ["180014"],
             "is_leaf": True, "category_tree_node_level": 3},
        ]
        with patch("monitor.listing_generator._get_client", return_value=fake_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                with patch(
                    "monitor.ebay_taxonomy.get_category_suggestions",
                    return_value=fake_taxonomy,
                ):
                    result = generate_listing(product, None, rank, self._tpl())

        # Taxonomy の 3 件で上書きされ、Claude の 2 件は反映されない
        assert len(result.category_candidates) == 3, (
            "Taxonomy の候補が Claude の値で上書きされてはいけない"
        )
        tx_ids = [c["category_id"] for c in result.category_candidates]
        assert tx_ids == ["14990", "111694", "47091"]
        # Claude の架空 ID が残存していないこと
        assert "99999" not in tx_ids
        assert "88888" not in tx_ids
        # reasoning も Taxonomy のマーカー文言
        for c in result.category_candidates:
            assert c["reasoning"].startswith("eBay Taxonomy API"), (
                f"reasoning が Claude の値で上書きされた: {c}"
            )
        # 最終採用 ID も Taxonomy (Claude の 14990 が偶然一致はせず、chosen=top[0])
        assert result.ebay_category_id == "14990"

    def test_empty_template_body(self):
        product = _ScrapedProduct(title_ja="X")
        rank = _Rank()
        # client モックは入らずに早期 return
        result = generate_listing(product, None, rank, "")
        assert result.generate_error == "template_body is empty"

    def test_none_product(self):
        rank = _Rank()
        result = generate_listing(None, None, rank, "tpl")
        assert result.generate_error == "product is None"

    def test_none_rank(self):
        product = _ScrapedProduct(title_ja="X")
        result = generate_listing(product, None, None, "tpl")
        assert result.generate_error == "rank is None"

    def test_claude_non_json_response(self):
        product = _ScrapedProduct(title_ja="X")
        rank = _Rank()

        block = MagicMock()
        block.type = "text"
        block.text = "I cannot generate a listing."
        resp = MagicMock()
        resp.content = [block]
        resp.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = resp

        with patch("monitor.listing_generator._get_client", return_value=fake_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                result = generate_listing(product, None, rank, self._tpl())
        assert result.generate_error is not None
        assert "no JSON" in result.generate_error

    def test_claude_api_exception(self):
        product = _ScrapedProduct(title_ja="X")
        rank = _Rank()

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = RuntimeError("boom")

        with patch("monitor.listing_generator._get_client", return_value=fake_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                result = generate_listing(product, None, rank, self._tpl())
        assert result.generate_error is not None
        assert "boom" in result.generate_error or "unexpected" in result.generate_error

    def test_title_truncated_to_80_chars(self):
        product = _ScrapedProduct(title_ja="X")
        rank = _Rank()
        long_title = "A" * 200
        claude_payload = {
            "title": long_title,
            "quick_notes": "n", "includes_items": [], "specs": [],
            "product_name": long_title,
            "item_specifics": {},
        }
        resp = _make_claude_response(claude_payload)
        fake_client = MagicMock()
        fake_client.messages.create.return_value = resp

        with patch("monitor.listing_generator._get_client", return_value=fake_client):
            with patch("monitor.listing_generator.log_anthropic_response", create=True):
                result = generate_listing(product, None, rank, self._tpl())
        assert len(result.ebay_title) == 80


# =========================================================================
# GeneratedListing dataclass
# =========================================================================

class TestGeneratedListingDataclass:
    def test_defaults(self):
        g = GeneratedListing()
        assert g.ebay_title == ""
        assert g.ebay_description == ""
        assert g.ebay_category_id is None
        assert g.item_specifics == {}
        assert g.category_candidates == []
        assert g.mode_class == "default"
        assert g.generate_error is None

    def test_full_init(self):
        g = GeneratedListing(
            ebay_title="T",
            ebay_description="D",
            ebay_category_id="293",
            ebay_category_name="CE",
            item_specifics={"Brand": "Sony"},
            mode_class="gadget",
        )
        assert g.item_specifics["Brand"] == "Sony"
