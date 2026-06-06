#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W226 product_resolver / html_fetcher / ai_html_parser の単体テスト。

実ネットワーク / 実 Anthropic API は呼ばない (全て monkeypatch で隔離)。
検証対象:
  - resolve_product_from_url の振り分け (専用フリマ → scrape / 汎用 → AI)
  - build_title_only_product (捏造しない)
  - fetch_page_html の httpx → playwright escalation 判定
  - parse_html_to_product の fail-closed (API 不在 / 空 title)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.supplier_scraper import ScrapedProduct  # noqa: E402
from monitor import product_resolver  # noqa: E402
from monitor.product_resolver import (  # noqa: E402
    resolve_product_from_url,
    build_title_only_product,
    TITLE_ONLY_DEFAULT_RANK,
)
from monitor import html_fetcher  # noqa: E402
from monitor import ai_html_parser  # noqa: E402


# =========================================================================
# build_title_only_product
# =========================================================================

class TestBuildTitleOnly:
    def test_basic(self):
        p = build_title_only_product("Anker PowerCore 10000 モバイルバッテリー")
        assert p.platform == "title_only"
        assert p.title_ja == "Anker PowerCore 10000 モバイルバッテリー"
        assert p.url == ""
        # 捏造禁止: title 以外は埋めない
        assert p.description_ja is None
        assert p.condition_ja is None
        assert p.price_jpy is None
        assert p.scrape_error is None

    def test_empty_title_errors(self):
        p = build_title_only_product("   ")
        assert p.scrape_error == "empty_title"
        assert p.title_ja is None

    def test_truncates_long_title(self):
        p = build_title_only_product("あ" * 400)
        assert len(p.title_ja) == 300

    def test_default_rank_constant(self):
        # 2026-06-06 決定4: title-only 既定ランクは N
        assert TITLE_ONLY_DEFAULT_RANK == "N"


# =========================================================================
# resolve_product_from_url — 振り分け
# =========================================================================

class TestResolveDispatch:
    def test_empty_url(self):
        p = resolve_product_from_url("")
        assert p.scrape_error == "empty_url"

    @pytest.mark.parametrize("url", [
        "https://auctions.yahoo.co.jp/jp/auction/x123",
        "https://jp.mercari.com/item/m123",
        "https://paypayfleamarket.yahoo.co.jp/item/abc",
    ])
    def test_dedicated_platforms_go_to_scraper(self, url, monkeypatch):
        """フリマ系は scrape_supplier_url に委譲 (回帰ゼロ)。"""
        called = {}

        def _fake_scrape(u, timeout_sec=15):
            called["url"] = u
            return ScrapedProduct(url=u, platform="mercari", title_ja="dummy")

        monkeypatch.setattr(product_resolver, "scrape_supplier_url", _fake_scrape)
        # AI 経路に行ったら失敗させる (呼ばれてはいけない)
        def _boom(*a, **k):
            raise AssertionError("AI 経路に行ってはいけない")
        monkeypatch.setattr(html_fetcher, "fetch_page_html", _boom)

        p = resolve_product_from_url(url)
        assert called["url"] == url
        assert p.title_ja == "dummy"

    @pytest.mark.parametrize("url", [
        "https://www.amazon.co.jp/dp/B0XXXX",
        "https://item.rakuten.co.jp/shop/code/",
        "https://store.shopping.yahoo.co.jp/shop/item.html",
        "https://fril.jp/item/12345",  # ラクマ → AI (決定3)
    ])
    def test_generic_sites_go_to_ai(self, url, monkeypatch):
        """汎用 EC は fetch + AI 解析に流す。"""
        # scrape は呼ばれてはいけない
        def _boom_scrape(*a, **k):
            raise AssertionError("汎用サイトが scrape に行ってはいけない")
        monkeypatch.setattr(product_resolver, "scrape_supplier_url", _boom_scrape)

        monkeypatch.setattr(
            html_fetcher, "fetch_page_html",
            lambda u, timeout_sec=20: ("<html>body</html>", None),
        )
        monkeypatch.setattr(
            ai_html_parser, "parse_html_to_product",
            lambda u, h: ScrapedProduct(url=u, platform="ai_html", title_ja="AI商品"),
        )
        p = resolve_product_from_url(url)
        assert p.platform == "ai_html"
        assert p.title_ja == "AI商品"

    def test_fetch_failure_sets_scrape_error(self, monkeypatch):
        monkeypatch.setattr(
            html_fetcher, "fetch_page_html",
            lambda u, timeout_sec=20: (None, "http_404"),
        )
        p = resolve_product_from_url("https://www.amazon.co.jp/dp/X")
        assert p.platform == "ai_html"
        assert "fetch_failed" in (p.scrape_error or "")
        assert "http_404" in (p.scrape_error or "")


# =========================================================================
# html_fetcher
# =========================================================================

class TestFetchPageHtml:
    def test_invalid_url(self):
        html, err = html_fetcher.fetch_page_html("not-a-url")
        assert html is None
        assert err == "invalid_url"

    def test_httpx_success_no_escalation(self, monkeypatch):
        good = "<html>" + ("x" * 3000) + "</html>"
        monkeypatch.setattr(html_fetcher, "_fetch_httpx",
                            lambda u, t: (good, None))
        def _boom(*a, **k):
            raise AssertionError("escalation 不要のはず")
        monkeypatch.setattr(html_fetcher, "_fetch_playwright", _boom)
        html, err = html_fetcher.fetch_page_html("https://example.com")
        assert err is None
        assert html == good

    def test_httpx_blocked_escalates_to_playwright(self, monkeypatch):
        blocked = "<html>Robot Check please solve</html>"
        good = "<html>" + ("y" * 3000) + "</html>"
        monkeypatch.setattr(html_fetcher, "_fetch_httpx",
                            lambda u, t: (blocked, None))
        monkeypatch.setattr(html_fetcher, "_fetch_playwright",
                            lambda u, t: (good, None))
        html, err = html_fetcher.fetch_page_html("https://www.amazon.co.jp/dp/X")
        assert err is None
        assert html == good

    def test_both_fail_returns_error(self, monkeypatch):
        monkeypatch.setattr(html_fetcher, "_fetch_httpx",
                            lambda u, t: (None, "httpx_timeout"))
        monkeypatch.setattr(html_fetcher, "_fetch_playwright",
                            lambda u, t: (None, "playwright_goto_timeout"))
        html, err = html_fetcher.fetch_page_html("https://slow.example.com")
        assert html is None
        assert err == "playwright_goto_timeout"

    def test_looks_blocked(self):
        assert html_fetcher._looks_blocked(None) is True
        assert html_fetcher._looks_blocked("short") is True
        assert html_fetcher._looks_blocked("x" * 2000) is False
        assert html_fetcher._looks_blocked("Robot Check " + "x" * 2000) is True


# =========================================================================
# ai_html_parser — fail-closed
# =========================================================================

class TestAiHtmlParser:
    def test_empty_html(self):
        p = ai_html_parser.parse_html_to_product("https://x.com", "")
        assert p.scrape_error == "empty_html"

    def test_api_unavailable_fail_closed(self, monkeypatch):
        # API キー無し → 捏造せず scrape_error
        monkeypatch.setattr(ai_html_parser, "_get_client", lambda: None)
        html = "<html><body>" + ("商品説明テキスト " * 50) + "</body></html>"
        p = ai_html_parser.parse_html_to_product("https://x.com", html)
        assert p.scrape_error is not None
        assert "ai_unavailable" in p.scrape_error
        assert p.title_ja is None

    def test_empty_title_fail_closed(self, monkeypatch):
        """AI が title_ja=null (非商品ページ) → 捏造せず scrape_error。"""
        class _Block:
            type = "text"
            text = '{"title_ja": null, "price_jpy": null, "condition_ja": null, "includes_ja": null, "description_ja": null}'

        class _Msg:
            content = [_Block()]

        class _FakeClient:
            class messages:
                @staticmethod
                def create(**k):
                    return _Msg()

        monkeypatch.setattr(ai_html_parser, "_get_client", lambda: _FakeClient())
        html = "<html><body>" + ("ナビゲーション " * 50) + "</body></html>"
        p = ai_html_parser.parse_html_to_product("https://x.com/404", html)
        assert "ai_parse_empty" in (p.scrape_error or "")
        assert p.title_ja is None

    def test_successful_parse_builds_product(self, monkeypatch):
        class _Block:
            type = "text"
            text = (
                '{"title_ja": "Anker PowerCore 10000", "price_jpy": 2990, '
                '"condition_ja": "新品", "includes_ja": "本体、ケーブル", '
                '"description_ja": "大容量モバイルバッテリー。重量 180g。"}'
            )

        class _Msg:
            content = [_Block()]

        class _FakeClient:
            class messages:
                @staticmethod
                def create(**k):
                    return _Msg()

        monkeypatch.setattr(ai_html_parser, "_get_client", lambda: _FakeClient())
        html = (
            '<html><head><meta property="og:image" content="https://img.example/p.jpg">'
            "</head><body>" + ("商品ページ本文 " * 50) + "</body></html>"
        )
        p = ai_html_parser.parse_html_to_product("https://www.amazon.co.jp/dp/X", html)
        assert p.scrape_error is None
        assert p.title_ja == "Anker PowerCore 10000"
        assert p.price_jpy == 2990
        assert p.condition_ja == "新品"
        assert p.includes_ja == "本体、ケーブル"
        # 画像は決定的抽出 (og:image)
        assert "https://img.example/p.jpg" in p.image_urls
        # 重量は description から決定的に補完
        assert p.weight_hint_g == 180


    def test_tracking_pixel_filtered(self):
        """W226: Amazon uedata ビーコン等のトラッキング画像を除外。"""
        assert ai_html_parser._is_tracking_image(
            "https://fls-fe.amazon.co.jp/1/batch/1/OP/x:uedata") is True
        assert ai_html_parser._is_tracking_image(
            "https://m.media-amazon.com/images/G/09/gno/sprites/nav-sprite.png") is True
        assert ai_html_parser._is_tracking_image(
            "https://m.media-amazon.com/images/I/51Sxnimf6ZL._AC_.jpg") is False

    def test_extract_images_drops_tracking(self):
        from bs4 import BeautifulSoup
        html = (
            '<html><body>'
            '<img src="https://fls-fe.amazon.co.jp/1/batch/OP/uedata">'
            '<img src="https://m.media-amazon.com/images/I/realproduct._AC_.jpg">'
            "</body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        imgs = ai_html_parser._extract_images(soup, "https://www.amazon.co.jp/dp/X")
        assert "https://m.media-amazon.com/images/I/realproduct._AC_.jpg" in imgs
        assert all("uedata" not in u for u in imgs)


# =========================================================================
# W226 content-poor escalation (楽天等 httpx 不足 → Playwright 再取得)
# =========================================================================

class TestContentPoorEscalation:
    def test_is_ai_parse_empty(self):
        assert product_resolver._is_ai_parse_empty("ai_parse_empty (...)") is True
        assert product_resolver._is_ai_parse_empty("page_text_too_short") is True
        assert product_resolver._is_ai_parse_empty("fetch_failed: http_404") is False
        assert product_resolver._is_ai_parse_empty("ai_unavailable") is False
        assert product_resolver._is_ai_parse_empty(None) is False

    def test_force_playwright_skips_httpx(self, monkeypatch):
        def _boom_httpx(*a, **k):
            raise AssertionError("force_playwright で httpx を呼んではいけない")
        monkeypatch.setattr(html_fetcher, "_fetch_httpx", _boom_httpx)
        monkeypatch.setattr(html_fetcher, "_fetch_playwright",
                            lambda u, t: ("<html>" + "z" * 3000 + "</html>", None))
        html, err = html_fetcher.fetch_page_html(
            "https://item.rakuten.co.jp/x/", force_playwright=True)
        assert err is None
        assert "z" * 3000 in html

    def test_resolver_escalates_on_empty_parse(self, monkeypatch):
        """httpx HTML で AI 空 → Playwright 再取得で成功するシナリオ。"""
        monkeypatch.setattr(product_resolver, "scrape_supplier_url",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

        calls = {"fetch": 0}

        def _fake_fetch(u, timeout_sec=20, force_playwright=False):
            calls["fetch"] += 1
            # 1 回目 (httpx) は content-poor HTML、2 回目 (force) は良好 HTML
            return ("<poor>", None) if not force_playwright else ("<rich>", None)

        def _fake_parse(u, h):
            if h == "<rich>":
                return ScrapedProduct(url=u, platform="ai_html", title_ja="楽天商品")
            return ScrapedProduct(url=u, platform="ai_html",
                                  scrape_error="ai_parse_empty (...)")

        monkeypatch.setattr(html_fetcher, "fetch_page_html", _fake_fetch)
        monkeypatch.setattr(ai_html_parser, "parse_html_to_product", _fake_parse)
        p = resolve_product_from_url("https://item.rakuten.co.jp/shop/code/")
        assert p.title_ja == "楽天商品"
        assert calls["fetch"] == 2  # httpx → force_playwright の 2 回

    def test_resolver_no_escalation_on_fetch_fail(self, monkeypatch):
        """fetch 自体が失敗 (404 等) は Playwright で取り直さない。"""
        calls = {"fetch": 0}

        def _fake_fetch(u, timeout_sec=20, force_playwright=False):
            calls["fetch"] += 1
            return (None, "http_404")

        monkeypatch.setattr(html_fetcher, "fetch_page_html", _fake_fetch)
        p = resolve_product_from_url("https://www.amazon.co.jp/dp/X")
        assert "fetch_failed" in (p.scrape_error or "")
        assert calls["fetch"] == 1  # 再取得しない


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
