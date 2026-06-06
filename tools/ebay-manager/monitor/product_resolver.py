#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W226 (2026-06-06): URL → ScrapedProduct の上位ディスパッチャ.

役割:
  - 仕入先フリマ (ヤフオク / メルカリ / PayPay) は既存 supplier_scraper の専用
    DOM パーサに流す (回帰ゼロ)。
  - それ以外の汎用 EC サイト (Amazon / 楽天 / Yahoo!ショッピング / ラクマ / その他)
    は html_fetcher で HTML を取得し ai_html_parser で AI 解析する。
  - URL が無い (商品タイトルだけ) ケースは build_title_only_product で title-only
    の ScrapedProduct を組む (捏造せず、title だけを器に詰める)。

設計判断 (2026-06-06 user 確定ロック):
  - ラクマ (fril / rakuma) は専用パーサが無いため AI 解析に倒す (決定3)。
  - メルカリ / ヤフオク / PayPay の既存挙動は 1 ミリも変えない (supplier_scraper
    の _detect_platform が認識するものだけ専用パーサ、それ以外は全て AI)。

呼出側 (UI 3 箇所) は scrape_supplier_url(url) を resolve_product_from_url(url) に
差し替えるだけ。戻り値型は同じ ScrapedProduct なので下流 (rank_classifier /
generate_listing) は無改造で動く。
"""
from __future__ import annotations

import logging
from typing import Optional

from monitor.supplier_scraper import (
    ScrapedProduct,
    scrape_supplier_url,
    _detect_platform,
)

logger = logging.getLogger(__name__)

# supplier_scraper が専用 DOM パーサを持つプラットフォーム (= AI に回さない)。
_DEDICATED_PLATFORMS = ("yahoo_auctions", "mercari", "paypay")

# title-only フォールバック既定ランク (2026-06-06 user 確定ロック 決定4)。
# resolver 自体は rank を決めないが、呼出側 UI がこの定数を参照できるよう公開。
TITLE_ONLY_DEFAULT_RANK = "N"


def resolve_product_from_url(
    url: str, timeout_sec: int = 20,
) -> ScrapedProduct:
    """URL からプラットフォームを判定し、適切な経路で ScrapedProduct を得る。

    - 専用パーサ対象 (ヤフオク/メルカリ/PayPay): scrape_supplier_url に委譲。
    - それ以外 (Amazon/楽天/Yahoo!ショッピング/ラクマ/その他): fetch + AI 解析。

    失敗時も例外を投げず ScrapedProduct.scrape_error に理由を格納して返す。

    Args:
        url: 仕入先 / 引用元の商品ページ URL
        timeout_sec: 取得タイムアウト (秒)

    Returns:
        ScrapedProduct (部分取得可、scrape_error に失敗理由)
    """
    if not url or not url.strip():
        return ScrapedProduct(url=url or "", platform="unknown",
                              scrape_error="empty_url")
    url = url.strip()

    platform = _detect_platform(url)
    if platform in _DEDICATED_PLATFORMS:
        # 既存フリマ経路 — 挙動を一切変えない (回帰ゼロ)。
        return scrape_supplier_url(url, timeout_sec=timeout_sec)

    # 汎用 EC サイト → HTML 取得 + AI 解析。
    # import は関数内 (Streamlit 起動コスト / playwright import を遅延)。
    from monitor.html_fetcher import fetch_page_html
    from monitor.ai_html_parser import parse_html_to_product

    html, err = fetch_page_html(url, timeout_sec=timeout_sec)
    if err or not html:
        return ScrapedProduct(
            url=url, platform="ai_html",
            scrape_error=f"fetch_failed: {err or 'no_html'}",
        )
    product = parse_html_to_product(url, html)

    # 楽天等は httpx HTML が長くても JS 描画前で本文不足 (_looks_blocked が長さで
    # 検知できない) → AI 抽出が空になる。この場合のみ Playwright で再取得して
    # 1 回だけ再解析する (2026-06-06 W226 実機検証で発覚した content-poor ギャップ)。
    if _is_ai_parse_empty(product.scrape_error):
        html2, err2 = fetch_page_html(
            url, timeout_sec=timeout_sec, force_playwright=True,
        )
        if html2 and not err2:
            product2 = parse_html_to_product(url, html2)
            if not product2.scrape_error:
                return product2
    return product


def _is_ai_parse_empty(scrape_error: Optional[str]) -> bool:
    """AI が本文不足で抽出できなかった (= Playwright 再取得で救える) エラーか。

    fetch 失敗系 (fetch_failed) や API 不在 (ai_unavailable) は Playwright で
    取り直しても改善しないため対象外。
    """
    if not scrape_error:
        return False
    return any(
        marker in scrape_error
        for marker in ("ai_parse_empty", "page_text_too_short", "ai_no_json")
    )


def build_title_only_product(title_ja: str) -> ScrapedProduct:
    """商品タイトルだけから ScrapedProduct を構築する (URL 無しケース)。

    title 以外の情報は一切捏造しない (Q0)。description_ja / condition_ja は None の
    まま残し、generate_listing の Claude が title だけを根拠に listing を組む。
    ランク (新品 N 等) の決定は呼出側 UI が担う (本関数は商品状態を推定しない)。

    Args:
        title_ja: 商品タイトル (日本語、ebay_listings.title 等)

    Returns:
        ScrapedProduct (platform='title_only')
    """
    t = (title_ja or "").strip()
    if not t:
        return ScrapedProduct(
            url="", platform="title_only",
            scrape_error="empty_title",
        )
    return ScrapedProduct(
        url="",
        platform="title_only",
        title_ja=t[:300],
    )
