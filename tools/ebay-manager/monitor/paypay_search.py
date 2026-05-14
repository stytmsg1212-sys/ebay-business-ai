#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayPayフリマ検索モジュール（Playwright版）

Mercari/Yahoo 版と同形式で、仕入先候補探索に使う。

機能:
  - キーワードで PayPayフリマ 在庫ありの商品を N 件取得
  - 各商品から title/price/url/image_url を抽出

ドメイン: paypayfleamarket.yahoo.co.jp
  - 検索URL: /search/{keyword}?status=selling (在庫あり絞り込み)
  - 商品URL: /item/{ITEM_ID}

制約:
  - React SPA で class 名が難読化されている。セレクタは href の pattern と
    data-* 属性を優先し、class名に極力頼らない設計とする。
  - sync_playwright はスレッド非安全（Pattern 1 async では subprocess 経由）。
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

logger = logging.getLogger(__name__)

PAYPAY_BASE = "https://paypayfleamarket.yahoo.co.jp"

# カード候補: /item/ を含むリンクを持つ要素を祖先から辿る方が安定する
_ITEM_LINK_SELECTOR = 'a[href*="/item/"]'

# タイトル/価格候補（fallback）: class 難読化対策として属性ベースを優先
_TITLE_SELECTORS = [
    '[data-testid*="title"]',
    'h3',
    'p',
]
_PRICE_SELECTORS = [
    '[data-testid*="price"]',
    '[class*="Price"]',
    'span',
]
_IMG_SELECTORS = ['img']


@dataclass
class PayPayHit:
    url: str                   # https://paypayfleamarket.yahoo.co.jp/item/XXXX
    title: str
    price_jpy: Optional[int]
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


def _extract_price_from_link(link) -> Optional[int]:
    """リンク内のテキストから価格を抽出。¥/円付きの最初の数字を採用。"""
    try:
        txt = link.inner_text(timeout=500)
    except PWTimeoutError:
        return None
    m = re.search(r'[¥￥]\s*([\d,]+)|([\d,]+)\s*円', txt)
    if not m:
        return None
    return _parse_price(m.group(1) or m.group(2) or '')


def search_paypay(
    keyword: str,
    max_results: int = 5,  # 2026-05-05 cost 最適化: 10 → 5
    headless: bool = True,
    timeout_ms: int = 15000,
    sort: str = "new",  # "new" | "price_asc"
) -> list[PayPayHit]:
    """
    PayPayフリマで在庫ありの商品をキーワード検索して N 件返す。

    Args:
        keyword: 検索キーワード
        max_results: 返却最大件数
        headless: False でブラウザ可視化（デバッグ用）
        timeout_ms: タイムアウト
        sort: "new"=新着順降順, "price_asc"=安い順

    Returns:
        PayPayHit のリスト。失敗時は空リスト。
    """
    # PayPayフリマ検索はパスベース URL のみ有効（/search?keyword=... は空ページを返す）
    # status=selling / sort はクエリで付与可能だが、デフォルトが既に在庫あり＋新着順
    # なので指定なくても意図通りの結果になる（2026-04-19 検証）。
    sort_param = {"new": "new", "price_asc": "pa"}.get(sort, "new")
    path = urllib.parse.quote(keyword, safe='')
    url = f"{PAYPAY_BASE}/search/{path}?status=selling&sort={sort_param}"

    hits: list[PayPayHit] = []
    seen_urls: set[str] = set()

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

            # PayPay は遅延描画。商品リンクが現れるまで待つ。
            try:
                page.wait_for_selector(_ITEM_LINK_SELECTOR, timeout=timeout_ms)
            except PWTimeoutError:
                logger.warning(
                    f"PayPay search: no /item/ links for {keyword!r}. "
                    "Selector or search URL may have changed."
                )
                browser.close()
                return []

            links = page.locator(_ITEM_LINK_SELECTOR)
            total = links.count()

            for i in range(total):
                if len(hits) >= max_results:
                    break
                link = links.nth(i)
                try:
                    href = link.get_attribute("href") or ""
                    if href.startswith("/"):
                        href = PAYPAY_BASE + href
                    # /item/XXXX 以外を除外（/item/ 単独や /item/promotion 等を捨てる）
                    m = re.search(r'/item/([A-Za-z0-9]+)(?:[?#/].*)?$', href)
                    if not m:
                        continue
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)

                    # PayPay は aria-label/title が空。img の alt にタイトル、
                    # link の inner_text に価格＋プロモ文言が入っている。
                    # link スコープ内で全部取れるので、ancestor を辿る必要はない。
                    title = ""
                    img_url = None
                    try:
                        img = link.locator("img").first
                        title = img.get_attribute("alt") or ""
                        img_url = img.get_attribute("src") or img.get_attribute("data-src")
                    except (PWTimeoutError, Exception):
                        pass
                    title = re.sub(r'のサムネイル\s*$', '', (title or '').strip())[:200]

                    price_jpy = _extract_price_from_link(link)

                    hits.append(PayPayHit(
                        url=href,
                        title=title,
                        price_jpy=price_jpy,
                        image_url=img_url,
                    ))
                except Exception as e:
                    logger.debug(f"link[{i}] parse error: {e}")
                    continue

            browser.close()
    except PWTimeoutError as e:
        logger.warning(f"PayPay search timeout for {keyword!r}: {e}")
    except Exception as e:
        logger.warning(f"PayPay search failed for {keyword!r}: {e}")

    logger.info(f"paypay search: keyword={keyword!r} -> {len(hits)} hits")
    return hits


if __name__ == "__main__":
    # 手動テスト: python -m monitor.paypay_search "KEYENCE LR-XH50"
    import sys
    logging.basicConfig(level=logging.INFO)
    kw = sys.argv[1] if len(sys.argv) > 1 else "KEYENCE LR-XH50"
    for h in search_paypay(kw, max_results=5):
        print(f"  {h.price_jpy}円 | {h.title[:60]}")
        print(f"    {h.url}")
