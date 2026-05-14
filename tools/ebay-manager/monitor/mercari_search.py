#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
メルカリ検索モジュール（Playwright版）

機能:
  - キーワードでメルカリ在庫ありの商品をN件取得
  - 各商品から title/price/url/image_url を抽出

制約（Pattern 1 async 実装時の注意）:
  - sync_playwright はスレッド安全ではない。
    threading.Thread から呼ぶと crash する可能性がある。
  - Pattern 1 では `subprocess` 経由か `asyncio + async_playwright` を使う前提。
  - batch 用途(task_supplier_sweep)ではメインスレッドで順次実行するので問題なし。
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

logger = logging.getLogger(__name__)

MERCARI_SEARCH_URL = "https://jp.mercari.com/search"

# 商品カードのセレクタ（2026-04時点）
# li要素に data-testid="item-cell" が付与されている。構造変化時はここを更新。
_CARD_SELECTOR = '[data-testid="item-cell"]'
_PRICE_SELECTOR = '[class*="price"]'
_IMG_SELECTOR = 'img'


@dataclass
class MercariHit:
    url: str                   # https://jp.mercari.com/item/XXXXXXXX
    title: str
    price_jpy: Optional[int]
    image_url: Optional[str]


def _parse_price(text: str) -> Optional[int]:
    """価格テキスト "¥1,234" → 1234 に変換"""
    if not text:
        return None
    digits = re.sub(r'[^\d]', '', text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def search_mercari(
    keyword: str,
    max_results: int = 5,  # 2026-05-05 cost 最適化: 10 → 5 (採用率 0.6% で過剰評価のため)
    headless: bool = True,
    timeout_ms: int = 15000,
) -> list[MercariHit]:
    """
    メルカリで在庫ありの商品をキーワード検索して N 件返す。

    Args:
        keyword: 検索キーワード（日本語可）
        max_results: 返却最大件数
        headless: ヘッドレスモード（デバッグ時 False にすると可視化）
        timeout_ms: 全体タイムアウト

    Returns:
        MercariHit のリスト。失敗時は空リストを返す（ログに理由）。
    """
    q = urllib.parse.urlencode({
        "keyword": keyword,
        "status": "on_sale",
        "sort": "created_time",
        "order": "desc",
    })
    url = f"{MERCARI_SEARCH_URL}?{q}"

    hits: list[MercariHit] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ja-JP",
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            try:
                page.wait_for_selector(_CARD_SELECTOR, timeout=timeout_ms)
            except PWTimeoutError:
                logger.warning(
                    f"Mercari search: no product cards found for {keyword!r}. "
                    "Selector may have changed."
                )
                browser.close()
                return []

            cards = page.locator(_CARD_SELECTOR)
            n = min(cards.count(), max_results)
            for i in range(n):
                card = cards.nth(i)
                try:
                    link = card.locator("a").first
                    href = link.get_attribute("href") or ""
                    if href.startswith("/"):
                        href = "https://jp.mercari.com" + href

                    # タイトル: 画像の alt 属性が最も信頼できる（Mercariは商品名を設定）
                    # フォールバック: link の aria-label → title 属性
                    img_el = card.locator(_IMG_SELECTOR).first
                    img_url = None
                    title = ""
                    try:
                        img_url = img_el.get_attribute("src")
                        title = img_el.get_attribute("alt") or ""
                    except PWTimeoutError:
                        pass
                    if not title:
                        title = (
                            link.get_attribute("aria-label")
                            or link.get_attribute("title")
                            or ""
                        )

                    price_text = ""
                    try:
                        price_text = card.locator(_PRICE_SELECTOR).first.inner_text(timeout=500)
                    except PWTimeoutError:
                        pass

                    # アクセシビリティ用の「のサムネイル」サフィックスを除去
                    clean_title = re.sub(r'のサムネイル\s*$', '', (title or '').strip())[:200]

                    if href and "mercari.com/item" in href:
                        hits.append(MercariHit(
                            url=href,
                            title=clean_title,
                            price_jpy=_parse_price(price_text),
                            image_url=img_url,
                        ))
                except Exception as e:
                    logger.debug(f"card[{i}] parse error: {e}")
                    continue

            browser.close()
    except PWTimeoutError as e:
        logger.warning(f"Mercari search timeout for {keyword!r}: {e}")
    except Exception as e:
        logger.warning(f"Mercari search failed for {keyword!r}: {e}")

    logger.info(f"mercari search: keyword={keyword!r} -> {len(hits)} hits")
    return hits
