# -*- coding: utf-8 -*-
"""W122 tab_morning_discovery: _render_discovery_economics_html unit tests.

設計 (2026-06-04): 採算ブロックを純関数化し 2 軸 (仕入¥→eBay想定$ / 想定粗利$・類似sold・JP競合)
で表示する。表示のみ、money-direct ロジック不変。

検証ポイント:
  1. profit_usd > 0 → 緑系 (positive 色クラス) + 金額表示
  2. profit_usd == 0 → "見積不能" 表現 (W129 保持、$0 を赤字と誤読させない)
  3. profit_usd < 0 → 赤系 (negative 色クラス) + 金額表示
  4. None / 非数値 → "—" 表示で graceful fallback
  5. 全 5 引数とも None 受理 (出力に "—" が含まれる)
"""
from __future__ import annotations

import pytest

from tabs.tab_morning_discovery import _render_discovery_economics_html


class TestProfitPositive:
    """profit_usd > 0 → 緑系 + 金額."""

    def test_positive_profit_contains_amount(self):
        html = _render_discovery_economics_html(
            sup_jpy=5000, ebay_usd=80, profit_usd=25, sold_30d=12, comp_jp=3
        )
        assert "$25" in html
        # W129 sentinel が混ざっていないこと
        assert "見積不能" not in html

    def test_positive_profit_uses_positive_color(self):
        """profit > 0 で緑系トーン (#2e7d5b、W261-fix 92b0f4a でコントラスト改訂) を含む."""
        html = _render_discovery_economics_html(
            sup_jpy=5000, ebay_usd=80, profit_usd=25, sold_30d=12, comp_jp=3
        )
        # 緑系 (旧 #7ac17a/#9bbf9b → W261-fix で #2e7d5b に変更)
        assert "#2e7d5b" in html

    def test_positive_profit_shows_supplier_and_ebay(self):
        html = _render_discovery_economics_html(
            sup_jpy=5000, ebay_usd=80, profit_usd=25, sold_30d=12, comp_jp=3
        )
        assert "¥5,000" in html
        assert "$80" in html
        assert "12" in html  # sold 30d
        assert "3" in html  # competitor jp


class TestProfitZeroEstimationFailed:
    """profit_usd == 0 → "見積不能" シグナル (W129 / L135-143 保持)."""

    def test_zero_profit_shows_estimation_failed_label(self):
        html = _render_discovery_economics_html(
            sup_jpy=5000, ebay_usd=80, profit_usd=0, sold_30d=8, comp_jp=2
        )
        assert "見積不能" in html

    def test_zero_profit_does_not_show_dollar_zero(self):
        """$0 表示は赤字と誤読されるため出さない."""
        html = _render_discovery_economics_html(
            sup_jpy=5000, ebay_usd=80, profit_usd=0, sold_30d=8, comp_jp=2
        )
        assert "$0" not in html

    def test_zero_profit_does_not_use_negative_red(self):
        """profit==0 は「赤字」ではないため赤系トーンに染めない."""
        html = _render_discovery_economics_html(
            sup_jpy=5000, ebay_usd=80, profit_usd=0, sold_30d=8, comp_jp=2
        )
        # 想定粗利の値自体に赤系を当てない (見出し他に赤系トーンが入る可能性は許容)
        # → "見積不能" の直前後に赤系 (#a8341b、W261-fix 改訂後) が密接していないことを近似チェック
        idx = html.find("見積不能")
        assert idx >= 0
        nearby = html[max(0, idx - 80) : idx + 80]
        assert "#a8341b" not in nearby


class TestProfitNegative:
    """profit_usd < 0 → 赤系 + 金額."""

    def test_negative_profit_contains_amount(self):
        html = _render_discovery_economics_html(
            sup_jpy=10000, ebay_usd=60, profit_usd=-15, sold_30d=4, comp_jp=8
        )
        # -$15 形式 (ASCII ハイフン) で表現される
        assert "-$15" in html

    def test_negative_profit_uses_negative_color(self):
        html = _render_discovery_economics_html(
            sup_jpy=10000, ebay_usd=60, profit_usd=-15, sold_30d=4, comp_jp=8
        )
        # 赤系 (旧 #d05858 → W261-fix 92b0f4a で #a8341b に変更)
        assert "#a8341b" in html


class TestGracefulFallback:
    """None / 非数値 → "—" 表示."""

    def test_all_none_renders_em_dashes(self):
        html = _render_discovery_economics_html(
            sup_jpy=None, ebay_usd=None, profit_usd=None,
            sold_30d=None, comp_jp=None,
        )
        # em dash が含まれる
        assert "—" in html
        # crash しない (HTML 文字列が返ること)
        assert isinstance(html, str)
        assert len(html) > 0

    def test_string_inputs_do_not_crash(self):
        """型エラー時も "—" fallback で graceful."""
        html = _render_discovery_economics_html(
            sup_jpy="abc", ebay_usd="xyz", profit_usd="qqq",
            sold_30d="aaa", comp_jp="bbb",
        )
        assert isinstance(html, str)
        # 数値表記が紛れ込まないこと
        assert "$abc" not in html
        assert "¥abc" not in html


class TestHtmlStructure:
    """HTML 構造 (self-contained / 2 軸整理) の最低限の検査."""

    def test_returns_string(self):
        html = _render_discovery_economics_html(
            sup_jpy=5000, ebay_usd=80, profit_usd=25, sold_30d=12, comp_jp=3
        )
        assert isinstance(html, str)

    def test_contains_div_wrapper(self):
        html = _render_discovery_economics_html(
            sup_jpy=5000, ebay_usd=80, profit_usd=25, sold_30d=12, comp_jp=3
        )
        # 2 軸 (仕入→eBay / 粗利+sold+competitor) なので div が複数あること
        assert html.count("<div") >= 2

    def test_no_external_css_class_dependency(self):
        """self-contained: pm-* 共有 CSS class に依存しない (商品管理タブと独立)."""
        html = _render_discovery_economics_html(
            sup_jpy=5000, ebay_usd=80, profit_usd=25, sold_30d=12, comp_jp=3
        )
        assert 'class="pm-' not in html
        assert "class='pm-" not in html


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
