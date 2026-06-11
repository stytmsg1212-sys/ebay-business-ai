"""W212-supplier-card-cleanup (2026-06-04): supplier candidate カード HTML helper.

純関数 ``render_supplier_card_html`` の表示出力 unit test.

money-direct path (採用/不採用/eBay ReviseItem) は対象外. ここでは:
- score 帯による色分け (>=80 緑 / >=60 黄 / <60 赤)
- 採算 2 軸 (eBay $ / 仕入 ¥) 表示
- 利益正負での 3 色クラス (sc-profit-pos / sc-profit-neg / sc-profit-na)
- model badge (Opus / Sonnet / Haiku / 不明)
- 仕入先復活警告 (parent_status='在庫有') 出力
- 別SKU出品機会 (alt_listing_possible=1) の利益 N/A 表示
- HTML escape (XSS 防御)
を検証する.
"""
from __future__ import annotations

import pytest

from tabs._supplier_card_html import (
    _model_badge,
    _score_color,
    render_supplier_card_html,
)


# ---------------------------------------------------------------------------
# 基本: 純関数として呼べる + style/CSS 含有
# ---------------------------------------------------------------------------

def _base_row(**overrides) -> dict:
    """テスト用最小 row dict."""
    row = {
        "id": 42,
        "ebay_item_id": "123456789012",
        "sku": "ebayyh_p1221413657",
        "candidate_url": "https://example.com/item/1",
        "candidate_title": "Test Item",
        "candidate_price_jpy": 10000,
        "match_score": 70,
        "source_platform": "yahoo_shopping",
        "match_reasoning": "",
        "alt_listing_note": "",
        "junk_likely_untested": 0,
        "profitable": 1,
        "alt_listing_possible": 0,
        "status": "pending",
        "eval_model": "claude-haiku-4-5",
        "profit_jpy": 5000,
    }
    row.update(overrides)
    return row


def test_render_returns_string_with_card_and_css():
    html = render_supplier_card_html(
        row=_base_row(),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert isinstance(html, str)
    assert "<style>" in html  # self-contained CSS 同梱
    assert 'class="sc-card"' in html
    assert ".sc-card" in html  # CSS class 定義


# ---------------------------------------------------------------------------
# score 色 (3 帯)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score, expected_color", [
    (95, "#0e4f4b"),   # >=80: ティール (W261 light theme)
    (80, "#0e4f4b"),
    (75, "#b8860b"),   # 60-79: 琥珀 (W261 light theme)
    (60, "#b8860b"),
    (59, "#a8341b"),   # <60: 赤 (W261 light theme)
    (0,  "#a8341b"),
])
def test_score_color_threshold(score, expected_color):
    assert _score_color(score) == expected_color


def test_score_html_contains_value():
    html = render_supplier_card_html(
        row=_base_row(match_score=85),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert ">85</span>" in html


# ---------------------------------------------------------------------------
# 採算 2 軸表示
# ---------------------------------------------------------------------------

def test_money_row_contains_ebay_usd_jpy_and_cost():
    html = render_supplier_card_html(
        row=_base_row(candidate_price_jpy=8000),
        ebay_price_usd=99.5,
        ebay_price_jpy=14925,
        profit_jpy=6925,
        parent_status="",
    )
    # eBay 出品額
    assert "eBay出品 $99.50" in html
    assert "(¥14,925)" in html
    # 仕入 ¥
    assert "仕入 ¥8,000" in html


def test_money_row_when_ebay_price_unknown():
    html = render_supplier_card_html(
        row=_base_row(),
        ebay_price_usd=None,
        ebay_price_jpy=None,
        profit_jpy=5000,
        parent_status="",
    )
    assert "eBay出品: 未取得" in html


def test_money_row_when_cost_price_missing():
    html = render_supplier_card_html(
        row=_base_row(candidate_price_jpy=None),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=None,
        parent_status="",
    )
    assert "仕入: 不明" in html


# ---------------------------------------------------------------------------
# 利益正負での 3 色クラス
# ---------------------------------------------------------------------------

def test_profit_positive_uses_pos_class():
    html = render_supplier_card_html(
        row=_base_row(candidate_price_jpy=10000),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,  # +5000円 = +50%
        parent_status="",
    )
    assert 'class="sc-profit-pos"' in html
    assert "利益 +¥5,000" in html
    assert "(50%)" in html
    assert 'class="sc-profit-neg"' not in html


def test_profit_negative_uses_neg_class():
    html = render_supplier_card_html(
        row=_base_row(candidate_price_jpy=10000),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=-2000,  # -2000円 = -20%
        parent_status="",
    )
    assert 'class="sc-profit-neg"' in html
    assert "利益 ¥-2,000" in html
    assert "(-20%)" in html
    assert 'class="sc-profit-pos"' not in html


def test_profit_alt_listing_uses_na_class():
    html = render_supplier_card_html(
        row=_base_row(alt_listing_possible=1, candidate_price_jpy=10000),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=None,
        parent_status="",
    )
    assert 'class="sc-profit-na"' in html
    assert "別SKU出品機会" in html


def test_profit_unknown_uses_na_class():
    html = render_supplier_card_html(
        row=_base_row(alt_listing_possible=0, candidate_price_jpy=10000),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=None,
        parent_status="",
    )
    assert 'class="sc-profit-na"' in html
    assert "算出不可" in html


# ---------------------------------------------------------------------------
# model badge
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("eval_model, expected_label", [
    ("claude-opus-4-8", "Opus 4.7"),  # opus → Opus 4.7 label
    ("claude-sonnet-4-6", "Sonnet 4.6"),
    ("claude-haiku-4-5", "Haiku 4.5"),
])
def test_model_badge_known(eval_model, expected_label):
    badge = _model_badge(eval_model)
    assert expected_label in badge
    assert 'class="sc-badge"' in badge


def test_model_badge_unknown_returns_empty():
    assert _model_badge("") == ""
    assert _model_badge("gpt-5") == ""
    assert _model_badge(None) == ""  # caller が None 渡しても safe


# ---------------------------------------------------------------------------
# 仕入先復活警告 (parent_status='在庫有')
# ---------------------------------------------------------------------------

def test_parent_in_stock_shows_recovered_warning():
    html = render_supplier_card_html(
        row=_base_row(),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="在庫有",
    )
    assert "仕入先復活" in html
    assert 'class="sc-recovered"' in html


def test_parent_in_stock_accepted_warns_revert():
    html = render_supplier_card_html(
        row=_base_row(status="accepted"),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="在庫有",
    )
    assert "採用済ですが反映前に「不採用」に戻すことを推奨" in html


def test_no_recovered_when_parent_out_of_stock():
    html = render_supplier_card_html(
        row=_base_row(),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert "仕入先復活" not in html


# ---------------------------------------------------------------------------
# 状態 (status) 表示
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status, expected_ja", [
    ("pending",  "未判定"),
    ("accepted", "採用済"),
    ("rejected", "不採用"),
    ("applied",  "反映済"),
])
def test_status_japanese_label(status, expected_ja):
    html = render_supplier_card_html(
        row=_base_row(status=status),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert expected_ja in html


# ---------------------------------------------------------------------------
# HTML escape (XSS 防御)
# ---------------------------------------------------------------------------

def test_html_escape_in_title():
    html = render_supplier_card_html(
        row=_base_row(candidate_title="<script>alert(1)</script>"),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_html_escape_in_reasoning():
    html = render_supplier_card_html(
        row=_base_row(match_reasoning="A&B <test>"),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert "A&amp;B" in html
    assert "&lt;test&gt;" in html


# ---------------------------------------------------------------------------
# 採算バッジ (profitable=1/0)
# ---------------------------------------------------------------------------

def test_profitable_badge_ok():
    html = render_supplier_card_html(
        row=_base_row(profitable=1),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert "採算OK" in html
    assert 'class="sc-badge sc-badge-good"' in html


def test_profitable_badge_warn():
    html = render_supplier_card_html(
        row=_base_row(profitable=0),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=-1000,
        parent_status="",
    )
    assert "採算注意" in html
    assert 'class="sc-badge sc-badge-warn"' in html


# ---------------------------------------------------------------------------
# ジャンク / 別出品提案 / 判定理由
# ---------------------------------------------------------------------------

def test_junk_flag_renders_warning():
    html = render_supplier_card_html(
        row=_base_row(junk_likely_untested=1),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert "動作未確認ジャンク" in html
    assert "sc-note-junk" in html


def test_alt_note_renders():
    html = render_supplier_card_html(
        row=_base_row(alt_listing_note="別商品として新規出品可"),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert "別出品提案: 別商品として新規出品可" in html
    assert "sc-note-alt" in html


def test_reasoning_renders():
    html = render_supplier_card_html(
        row=_base_row(match_reasoning="型番一致"),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert "判定: 型番一致" in html


def test_empty_optional_fields_omit_sections():
    html = render_supplier_card_html(
        row=_base_row(match_reasoning="", alt_listing_note="", junk_likely_untested=0),
        ebay_price_usd=100.0,
        ebay_price_jpy=15000,
        profit_jpy=5000,
        parent_status="",
    )
    assert "判定:" not in html
    assert "別出品提案:" not in html
    assert "動作未確認ジャンク" not in html
