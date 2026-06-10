#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
supplier_scraper の単体テスト。

実ネットワークアクセスは行わない。platform 判定 / 重量抽出 / dataclass 構造のみ検証。
"""
from __future__ import annotations

import sys
from pathlib import Path

# tools/ebay-manager/ を sys.path に追加 (tests/ 配下から monitor/ を import するため)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.supplier_scraper import (  # noqa: E402
    ScrapedProduct,
    _detect_platform,
    _dedupe_ordered,
    _extract_includes,
    _extract_weight_hint,
    _normalize_image_url,
    _parse_price,
    _strip_mercari_title_suffix,
    scrape_supplier_url,
)


# =========================================================================
# _detect_platform
# =========================================================================

class TestDetectPlatform:
    def test_yahoo_auctions_item(self):
        assert _detect_platform('https://auctions.yahoo.co.jp/jp/auction/x1234567') == 'yahoo_auctions'

    def test_yahoo_auctions_www_variant(self):
        assert _detect_platform('https://page.auctions.yahoo.co.jp/jp/auction/u123') == 'yahoo_auctions'

    def test_mercari_jp(self):
        assert _detect_platform('https://jp.mercari.com/item/m12345') == 'mercari'

    def test_mercari_global(self):
        assert _detect_platform('https://www.mercari.com/item/abc') == 'mercari'

    def test_paypay(self):
        assert _detect_platform('https://paypayfleamarket.yahoo.co.jp/item/abc123') == 'paypay'

    def test_paypay_preferred_over_yahoo(self):
        # PayPay は yahoo.co.jp のサブドメインだが paypay 判定が優先されること
        assert _detect_platform('https://paypayfleamarket.yahoo.co.jp/item/xyz') == 'paypay'

    def test_unknown_rakuten(self):
        assert _detect_platform('https://item.rakuten.co.jp/abc/123') == 'unknown'

    def test_unknown_amazon(self):
        assert _detect_platform('https://www.amazon.co.jp/dp/B0XXX') == 'unknown'

    def test_empty(self):
        assert _detect_platform('') == 'unknown'

    def test_malformed(self):
        assert _detect_platform('not-a-url') == 'unknown'


# =========================================================================
# _extract_weight_hint
# =========================================================================

class TestExtractWeightHint:
    def test_simple_gram(self):
        assert _extract_weight_hint('重量 500g') == 500

    def test_kanji_grams(self):
        assert _extract_weight_hint('重量は約 1200 グラムです') == 1200

    def test_kg_to_g(self):
        assert _extract_weight_hint('重量: 1.5kg') == 1500

    def test_weight_english(self):
        assert _extract_weight_hint('Weight: 750 g') == 750

    def test_approx_kg(self):
        assert _extract_weight_hint('約 2.3 kg') == 2300

    def test_no_match(self):
        assert _extract_weight_hint('軽くて持ち運び便利です') is None

    def test_empty(self):
        assert _extract_weight_hint('') is None

    def test_none(self):
        assert _extract_weight_hint(None) is None

    def test_kg_priority_over_g(self):
        # kg が先に書かれていれば kg が採用される (g 表記より優先)
        assert _extract_weight_hint('重量 1.2kg (1200 g)') == 1200


# =========================================================================
# _parse_price
# =========================================================================

class TestParsePrice:
    def test_yen_symbol(self):
        assert _parse_price('¥1,234') == 1234

    def test_en_mark(self):
        assert _parse_price('￥3,000') == 3000

    def test_en_suffix(self):
        assert _parse_price('1,500円') == 1500

    def test_plain_number(self):
        assert _parse_price('10000') == 10000

    def test_empty(self):
        assert _parse_price('') is None

    def test_none(self):
        assert _parse_price(None) is None

    def test_no_digits(self):
        assert _parse_price('価格はお問い合わせください') is None


# =========================================================================
# _dedupe_ordered
# =========================================================================

class TestDedupeOrdered:
    def test_preserves_order(self):
        assert _dedupe_ordered(['a', 'b', 'a', 'c', 'b']) == ['a', 'b', 'c']

    def test_drops_empty(self):
        assert _dedupe_ordered(['a', '', None, 'b']) == ['a', 'b']

    def test_empty_input(self):
        assert _dedupe_ordered([]) == []


# =========================================================================
# _normalize_image_url
# =========================================================================

class TestNormalizeImageUrl:
    def test_absolute(self):
        assert _normalize_image_url(
            'https://cdn.example.com/img.jpg',
            'https://example.com/page',
        ) == 'https://cdn.example.com/img.jpg'

    def test_relative(self):
        assert _normalize_image_url(
            '/img/a.jpg',
            'https://example.com/page',
        ) == 'https://example.com/img/a.jpg'

    def test_data_uri_excluded(self):
        assert _normalize_image_url(
            'data:image/png;base64,iVBORw==',
            'https://example.com/',
        ) is None

    def test_svg_excluded(self):
        assert _normalize_image_url(
            'https://example.com/icon.svg',
            'https://example.com/',
        ) is None

    def test_placeholder_excluded(self):
        assert _normalize_image_url(
            'https://example.com/placeholder-img.png',
            'https://example.com/',
        ) is None

    def test_none(self):
        assert _normalize_image_url(None, 'https://example.com/') is None


# =========================================================================
# _extract_includes
# =========================================================================

class TestExtractIncludes:
    def test_japanese_label(self):
        assert _extract_includes('付属品：箱、説明書、ケーブル') == '箱、説明書、ケーブル'

    def test_set_contents(self):
        result = _extract_includes('セット内容: 本体、充電器、マニュアル')
        assert result == '本体、充電器、マニュアル'

    def test_includes_english(self):
        result = _extract_includes('Includes: Box, cable, manual')
        assert result == 'Box, cable, manual'

    def test_no_match(self):
        assert _extract_includes('この商品は美品です') is None

    def test_empty(self):
        assert _extract_includes('') is None

    # -----------------------------------------------------------------
    # 2026-04-22 拡張: 自然文 / 括弧ラベル / 同梱品バリエーション
    # -----------------------------------------------------------------
    def test_bracket_label(self):
        """【付属品】型の括弧ラベル"""
        assert _extract_includes('【付属品】リモコン、説明書、ケーブル') == 'リモコン、説明書、ケーブル'

    def test_dousyapin(self):
        """同梱品 バリエーション"""
        assert _extract_includes('同梱品: 元箱、保証書') == '元箱、保証書'

    def test_narrative_with_付き(self):
        """「X、Y 付き」の自然文"""
        r = _extract_includes('リモコン、説明書、ACアダプター付き')
        assert r is not None
        assert 'リモコン' in r

    def test_narrative_with_同梱(self):
        """「X、Y を同梱」の自然文"""
        r = _extract_includes('元箱、元付属品を同梱します')
        assert r is not None

    # -----------------------------------------------------------------
    # code-reviewer HIGH-1 (2026-04-22): 演算子優先順位バグ回帰防止
    # -----------------------------------------------------------------
    def test_long_narrative_with_dot_rejected(self):
        """100字超で '・' を含む長文は、付き/同梱の自然文でも拒否される"""
        # 90字以上の長文に '・' を大量に含ませる (誤抽出防止の境界)
        long_text = (
            'これはとても長い商品説明で、様々な情報が含まれています・例えば色や素材・'
            'サイズ感・使用感・発送方法・支払い方法などが書かれている中で'
            'リモコン・説明書付き'
        )
        result = _extract_includes(long_text)
        # 「リモコン・説明書」部分だけ短く拾うか、完全に None を返すべき
        # (少なくとも 200+ 字の巨大文字列を返してはいけない)
        if result is not None:
            assert len(result) <= 80, (
                f'演算子優先順位バグの再発: 長文 ({len(result)}字) を許可している'
            )

    def test_long_narrative_with_comma_rejected(self):
        """',' 区切りの長文も拒否"""
        long_text = (
            'Various description text including colors, materials, '
            'sizes, usage, shipping options, payment methods, and more '
            'details about the product remote, manual included'
        )
        result = _extract_includes(long_text)
        if result is not None:
            assert len(result) <= 80


# =========================================================================
# _extract_weight_hint 拡張パターン (2026-04-22)
# =========================================================================

class TestExtractWeightHintExtended:
    def test_hontai_juuryou(self):
        """「本体重量約 1.5kg」"""
        from monitor.supplier_scraper import _extract_weight_hint
        assert _extract_weight_hint('本体重量約 1.5kg') == 1500

    def test_sitsuryou(self):
        """「質量」ラベル"""
        from monitor.supplier_scraper import _extract_weight_hint
        assert _extract_weight_hint('質量: 800g') == 800

    def test_omosa(self):
        """「重さ」ラベル"""
        from monitor.supplier_scraper import _extract_weight_hint
        assert _extract_weight_hint('重さ 1.2kg') == 1200

    def test_out_of_range_rejected(self):
        """範囲外 (0g or 100kg) は拒否"""
        from monitor.supplier_scraper import _extract_weight_hint
        # 100kg = 100000g は範囲外 (>50000g)
        assert _extract_weight_hint('重量 100kg') is None
        # 0g は範囲外
        assert _extract_weight_hint('重量 0g') is None

    def test_mass_english(self):
        """Mass ラベル (英語)"""
        from monitor.supplier_scraper import _extract_weight_hint
        assert _extract_weight_hint('Mass: 2.5kg') == 2500

    # -----------------------------------------------------------------
    # code-reviewer HIGH-2 (2026-04-22): G 単体で GB/MB/TB を誤抽出しない
    # -----------------------------------------------------------------
    def test_storage_gb_not_weight(self):
        """「500 GB」を 500g と誤抽出しないこと (実際は 500GB ストレージ)"""
        from monitor.supplier_scraper import _extract_weight_hint
        assert _extract_weight_hint('本機は約 500 GB のストレージ搭載') is None

    def test_memory_gb_not_weight(self):
        """「8 GB メモリ」を 8g と誤抽出しない"""
        from monitor.supplier_scraper import _extract_weight_hint
        assert _extract_weight_hint('約 8 GBメモリ') is None

    def test_storage_gbytes_not_weight(self):
        """「Weight: 500 GBytes」のような変則表記も g として捕獲しない"""
        from monitor.supplier_scraper import _extract_weight_hint
        assert _extract_weight_hint('Weight: 500 GBytes 対応') is None

    def test_kg_not_confused_with_kgb(self):
        """「2.5 kGbps」のような偽単位を kg として捕獲しない"""
        from monitor.supplier_scraper import _extract_weight_hint
        # 想定: "2.5 kG" の後に "bps" が続くので単語境界で弾く
        assert _extract_weight_hint('転送速度 2.5 kGbps') is None

    def test_actual_weight_still_works_despite_later_gb(self):
        """重量記載と GB 表記が併存する場合、正しい重量だけ拾う"""
        from monitor.supplier_scraper import _extract_weight_hint
        text = '本体重量約 1.2kg。内蔵ストレージ 500GB。'
        assert _extract_weight_hint(text) == 1200

    def test_tb_not_as_weight(self):
        """TB (テラバイト) 単位の誤捕獲もしない"""
        from monitor.supplier_scraper import _extract_weight_hint
        assert _extract_weight_hint('HDD 容量 約 4 TB') is None


# =========================================================================
# _extract_dimensions (2026-04-22 新設)
# =========================================================================

class TestExtractDimensions:
    def test_triple_cm(self):
        from monitor.supplier_scraper import _extract_dimensions
        l, w, d = _extract_dimensions('サイズ: 30×20×10 cm')
        assert (l, w, d) == (300, 200, 100)

    def test_triple_mm(self):
        from monitor.supplier_scraper import _extract_dimensions
        l, w, d = _extract_dimensions('サイズ 300x200x100mm')
        assert (l, w, d) == (300, 200, 100)

    def test_japanese_individual(self):
        from monitor.supplier_scraper import _extract_dimensions
        l, w, d = _extract_dimensions('幅 30cm 奥行 20cm 高さ 10cm')
        assert l == 300 and w == 200 and d == 100

    def test_wdh_prefix(self):
        from monitor.supplier_scraper import _extract_dimensions
        l, w, d = _extract_dimensions('W30×D20×H10cm')
        assert (l, w, d) == (300, 200, 100)

    def test_no_match(self):
        from monitor.supplier_scraper import _extract_dimensions
        assert _extract_dimensions('スピーカーです') == (None, None, None)

    def test_out_of_range_rejected(self):
        """10m などの異常値は拒否"""
        from monitor.supplier_scraper import _extract_dimensions
        # 10000 mm = 10m は範囲外 (>5000mm)
        l, w, d = _extract_dimensions('サイズ 10000x10000x10000mm')
        assert (l, w, d) == (None, None, None)

    def test_empty(self):
        from monitor.supplier_scraper import _extract_dimensions
        assert _extract_dimensions(None) == (None, None, None)
        assert _extract_dimensions('') == (None, None, None)


# =========================================================================
# _enrich_product_attrs_via_llm (LLM fallback、API mock)
# =========================================================================

class TestEnrichProductAttrsViaLLM:
    def test_skips_short_description(self, monkeypatch):
        """30字未満の説明では LLM を呼ばない (コスト節約)"""
        from monitor.supplier_scraper import _enrich_product_attrs_via_llm
        p = ScrapedProduct(
            url='x', platform='mercari',
            description_ja='短い説明',  # 5 chars
            weight_hint_g=None,
        )
        called = {'n': 0}

        def _mock(*args, **kwargs):
            called['n'] += 1
            return {'weight_g': 9999}

        monkeypatch.setattr(
            'monitor.product_attrs_extractor.extract_product_attrs', _mock,
        )
        _enrich_product_attrs_via_llm(p)
        assert called['n'] == 0
        assert p.weight_hint_g is None

    def test_skips_when_all_populated(self, monkeypatch):
        """既に全項目取れていれば LLM を呼ばない"""
        from monitor.supplier_scraper import _enrich_product_attrs_via_llm
        p = ScrapedProduct(
            url='x', platform='mercari',
            description_ja='x' * 100,
            weight_hint_g=1000,
            includes_ja='リモコン',
            length_mm=100, width_mm=100, depth_mm=100,
        )
        called = {'n': 0}

        def _mock(*args, **kwargs):
            called['n'] += 1
            return {}

        monkeypatch.setattr(
            'monitor.product_attrs_extractor.extract_product_attrs', _mock,
        )
        _enrich_product_attrs_via_llm(p)
        assert called['n'] == 0

    def test_llm_fills_missing_weight(self, monkeypatch):
        """regex で weight=None のとき LLM が補完"""
        from monitor.supplier_scraper import _enrich_product_attrs_via_llm
        p = ScrapedProduct(
            url='x', platform='mercari',
            description_ja='本体は約 1.5kg 前後の軽量設計です。' + 'x' * 50,
            weight_hint_g=None,
        )
        monkeypatch.setattr(
            'monitor.product_attrs_extractor.extract_product_attrs',
            lambda d: {
                'weight_g': 1500, 'length_mm': None,
                'width_mm': None, 'depth_mm': None, 'includes_list': [],
            },
        )
        _enrich_product_attrs_via_llm(p)
        assert p.weight_hint_g == 1500

    def test_llm_does_not_overwrite_regex_hit(self, monkeypatch):
        """regex で取れた項目は LLM で上書きしない"""
        from monitor.supplier_scraper import _enrich_product_attrs_via_llm
        p = ScrapedProduct(
            url='x', platform='mercari',
            description_ja='x' * 100,
            weight_hint_g=2000,  # regex で取得済
        )
        monkeypatch.setattr(
            'monitor.product_attrs_extractor.extract_product_attrs',
            lambda d: {
                'weight_g': 9999,  # LLM が別の値
                'length_mm': None, 'width_mm': None, 'depth_mm': None,
                'includes_list': [],
            },
        )
        _enrich_product_attrs_via_llm(p)
        # regex 優先なので 2000 のまま (9999 で上書きしない)
        assert p.weight_hint_g == 2000

    def test_llm_fills_includes_list(self, monkeypatch):
        from monitor.supplier_scraper import _enrich_product_attrs_via_llm
        p = ScrapedProduct(
            url='x', platform='mercari',
            description_ja='x' * 100,
        )
        monkeypatch.setattr(
            'monitor.product_attrs_extractor.extract_product_attrs',
            lambda d: {
                'weight_g': None,
                'length_mm': None, 'width_mm': None, 'depth_mm': None,
                'includes_list': ['リモコン', 'ACアダプター', '説明書'],
            },
        )
        _enrich_product_attrs_via_llm(p)
        assert p.includes_ja is not None
        assert 'リモコン' in p.includes_ja
        assert 'ACアダプター' in p.includes_ja

    def test_llm_fills_dimensions(self, monkeypatch):
        from monitor.supplier_scraper import _enrich_product_attrs_via_llm
        p = ScrapedProduct(
            url='x', platform='mercari',
            description_ja='x' * 100,
        )
        monkeypatch.setattr(
            'monitor.product_attrs_extractor.extract_product_attrs',
            lambda d: {
                'weight_g': None,
                'length_mm': 300, 'width_mm': 200, 'depth_mm': 100,
                'includes_list': [],
            },
        )
        _enrich_product_attrs_via_llm(p)
        assert p.length_mm == 300
        assert p.width_mm == 200
        assert p.depth_mm == 100

    def test_llm_exception_safe(self, monkeypatch):
        """LLM が例外投げても scraper が落ちない"""
        from monitor.supplier_scraper import _enrich_product_attrs_via_llm
        p = ScrapedProduct(
            url='x', platform='mercari',
            description_ja='x' * 100,
        )

        def _raise(*a, **k):
            raise RuntimeError('API down')

        monkeypatch.setattr(
            'monitor.product_attrs_extractor.extract_product_attrs', _raise,
        )
        # 例外で終了しないこと
        _enrich_product_attrs_via_llm(p)
        assert p.weight_hint_g is None  # 変化なし


# =========================================================================
# ScrapedProduct dataclass
# =========================================================================

class TestScrapedProductDataclass:
    def test_default_initialization(self):
        p = ScrapedProduct(url='https://example.com', platform='unknown')
        assert p.url == 'https://example.com'
        assert p.platform == 'unknown'
        assert p.title_ja is None
        assert p.price_jpy is None
        assert p.image_urls == []
        assert p.scrape_error is None

    def test_full_initialization(self):
        p = ScrapedProduct(
            url='https://auctions.yahoo.co.jp/jp/auction/xxx',
            platform='yahoo_auctions',
            title_ja='テスト商品',
            price_jpy=5000,
            condition_ja='中古',
            includes_ja='箱あり',
            image_urls=['https://img.example/1.jpg'],
            description_ja='商品説明です',
            seller_name='test_seller',
            weight_hint_g=1200,
        )
        assert p.title_ja == 'テスト商品'
        assert p.price_jpy == 5000
        assert len(p.image_urls) == 1
        assert p.weight_hint_g == 1200

    def test_image_urls_independent_instances(self):
        # default_factory が共有されていないこと
        p1 = ScrapedProduct(url='a', platform='unknown')
        p2 = ScrapedProduct(url='b', platform='unknown')
        p1.image_urls.append('x')
        assert p2.image_urls == []


# =========================================================================
# scrape_supplier_url (unknown platform は即 return — ネットワーク不要)
# =========================================================================

class TestScrapeSupplierUrlUnknown:
    def test_unknown_returns_error(self):
        result = scrape_supplier_url('https://example.com/unknown-page')
        assert result.platform == 'unknown'
        assert result.scrape_error == 'unsupported_platform'
        assert result.title_ja is None

    def test_empty_url(self):
        result = scrape_supplier_url('')
        assert result.platform == 'unknown'
        assert result.scrape_error == 'unsupported_platform'


# =========================================================================
# _strip_mercari_title_suffix (2026-06-11 M-2 og:title fallback 用)
# =========================================================================

class TestStripMercariTitleSuffix:
    def test_by_mercari_suffix(self):
        """「by メルカリ」suffix を除去する."""
        raw = 'TRIFIELD METER 100XE 電磁波測定器 ケース付 動作確認済 by メルカリ'
        assert _strip_mercari_title_suffix(raw) == 'TRIFIELD METER 100XE 電磁波測定器 ケース付 動作確認済'

    def test_hyphen_mercari_suffix(self):
        """「- メルカリ」suffix を除去する."""
        raw = 'TRIFIELD METER 100XE 電磁波測定器 ケース付 動作確認済 - メルカリ'
        assert _strip_mercari_title_suffix(raw) == 'TRIFIELD METER 100XE 電磁波測定器 ケース付 動作確認済'

    def test_no_suffix(self):
        """suffix がない場合はそのまま返す."""
        raw = 'TRIFIELD METER 100XE 電磁波測定器 ケース付 動作確認済'
        assert _strip_mercari_title_suffix(raw) == raw
