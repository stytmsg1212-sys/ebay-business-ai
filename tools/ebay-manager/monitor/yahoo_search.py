#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Yahoo!オークション検索モジュール（Playwright版）

Mercari 版(`mercari_search.py`)と同形式で、仕入先候補探索に使う。

機能:
  - キーワードで Yahoo!オークション在庫ありの商品を N 件取得
  - 各商品から title/price/url/image_url を抽出
  - 即決価格があればそれを優先（仕入れ予算確定のため）、無ければ現在価格

制約:
  - sync_playwright はスレッド安全ではない（Pattern 1 async では subprocess 経由）
  - 検索結果の URL は落札ページ /jp/auction/{AUCTION_ID} 形式

セレクタ注意:
  Yahoo はしばしば DOM 構造を変える。失敗時は `_CARD_SELECTOR` 以下の
  セレクタ候補を複数リスト化し、順次 fallback する設計にしてある。
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

logger = logging.getLogger(__name__)

YAHOO_SEARCH_URL = "https://auctions.yahoo.co.jp/search/search"

# 商品カードは複数バージョンが混在しうる。見つかった順に使う。
_CARD_SELECTOR_CANDIDATES = [
    'li.Product',
    '.Product',
    '[class*="Product__"][class*="item"]',
]

# 単一カード内で使うサブセレクタ。先頭優先で fallback。
_TITLE_SELECTORS = ['.Product__titleLink', 'a[class*="title"]', 'h3 a']
_PRICE_SELECTORS = ['.Product__priceValue', '[class*="price"]']
_BUYNOW_SELECTORS = ['.Product__priceValue--buynow', '[class*="buynow"]', '[class*="即決"]']
_IMG_SELECTORS = ['.Product__imageData', 'img']


@dataclass
class YahooHit:
    url: str                   # https://auctions.yahoo.co.jp/jp/auction/XXXXXXXX
    title: str
    price_jpy: Optional[int]   # 即決優先、無ければ現在価格
    image_url: Optional[str]


def _parse_price(text: str) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r'[^\d]', '', text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _first_matching_locator(card, selectors: list[str]):
    """複数セレクタ候補から最初に見つかった Locator を返す。"""
    for sel in selectors:
        loc = card.locator(sel).first
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def search_yahoo(
    keyword: str,
    max_results: int = 5,  # 2026-05-05 cost 最適化: 10 → 5
    headless: bool = True,
    timeout_ms: int = 15000,
    search_url: Optional[str] = None,
) -> list[YahooHit]:
    """
    Yahoo!オークションで在庫ありの商品をキーワード検索して N 件返す。

    並び順: 新着順降順（s1=new, o1=d）。

    Args:
        search_url: 指定時はこの URL を直接使う (W148 AlertCrawler 移植: 価格範囲 /
            category_id / 除外語など URL に焼かれた filter を保持するため). None なら
            keyword から汎用 URL を再構築 (既存呼出元の挙動を維持).

    Returns:
        YahooHit のリスト。失敗時は空リスト。
    """
    if search_url:
        url = search_url
    else:
        q = urllib.parse.urlencode({
            "p": keyword,
            "auccat": "",
            "aq": "-1",
            "oq": "",
            "s1": "new",    # sort by new listings
            "o1": "d",      # descending
            "n": max(max_results, 20),  # Yahoo 最小20件のことが多い
            "b": 1,
        })
        url = f"{YAHOO_SEARCH_URL}?{q}"

    hits: list[YahooHit] = []

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

            # カード選択: 見つかった最初のセレクタを使う
            card_selector: Optional[str] = None
            for sel in _CARD_SELECTOR_CANDIDATES:
                try:
                    page.wait_for_selector(sel, timeout=5000)
                    if page.locator(sel).count() > 0:
                        card_selector = sel
                        break
                except PWTimeoutError:
                    continue

            if not card_selector:
                logger.warning(
                    f"Yahoo search: no product cards for {keyword!r}. "
                    f"Tried selectors: {_CARD_SELECTOR_CANDIDATES}"
                )
                browser.close()
                return []

            cards = page.locator(card_selector)
            n = min(cards.count(), max_results)
            for i in range(n):
                card = cards.nth(i)
                try:
                    link = _first_matching_locator(card, _TITLE_SELECTORS)
                    if not link:
                        continue
                    href = link.get_attribute("href") or ""
                    if href.startswith("/"):
                        href = "https://auctions.yahoo.co.jp" + href

                    title = (link.inner_text(timeout=500) or "").strip()[:200]

                    # 即決価格優先、無ければ現在価格
                    buynow_loc = _first_matching_locator(card, _BUYNOW_SELECTORS)
                    price_text = ""
                    if buynow_loc:
                        try:
                            price_text = buynow_loc.inner_text(timeout=500)
                        except PWTimeoutError:
                            pass
                    if not price_text:
                        cur_loc = _first_matching_locator(card, _PRICE_SELECTORS)
                        if cur_loc:
                            try:
                                price_text = cur_loc.inner_text(timeout=500)
                            except PWTimeoutError:
                                pass

                    img_loc = _first_matching_locator(card, _IMG_SELECTORS)
                    img_url = None
                    if img_loc:
                        try:
                            img_url = (
                                img_loc.get_attribute("src")
                                or img_loc.get_attribute("data-src")
                            )
                        except PWTimeoutError:
                            pass

                    if href and "auctions.yahoo.co.jp" in href:
                        hits.append(YahooHit(
                            url=href,
                            title=title,
                            price_jpy=_parse_price(price_text),
                            image_url=img_url,
                        ))
                except Exception as e:
                    logger.debug(f"card[{i}] parse error: {e}")
                    continue

            browser.close()
    except PWTimeoutError as e:
        logger.warning(f"Yahoo search timeout for {keyword!r}: {e}")
    except Exception as e:
        logger.warning(f"Yahoo search failed for {keyword!r}: {e}")

    logger.info(f"yahoo search: keyword={keyword!r} -> {len(hits)} hits")
    return hits


if __name__ == "__main__":
    # 手動テスト: python -m monitor.yahoo_search "KEYENCE LR-XH50"
    import sys
    logging.basicConfig(level=logging.INFO)
    kw = sys.argv[1] if len(sys.argv) > 1 else "KEYENCE LR-XH50"
    for h in search_yahoo(kw, max_results=5):
        print(f"  {h.price_jpy}円 | {h.title[:60]}")
        print(f"    {h.url}")
