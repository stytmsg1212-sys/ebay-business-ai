"""仕入元サイトの価格抽出 (W120 / 2026-05-12).

HTML 文字列を入力に JPY int を返す. 抽出不能なら None (silent skip でなく明示).
SKU prefix で site 振り分け:
  - ebayAM_*  → Amazon.co.jp
  - ebayRT_*  → 楽天市場

ロジック分離理由:
  - K2 Surgical: 既存 scrapers.py (3 段 fallback) を 1 行も触らない
  - 単体テスト容易性: HTML fixture で site 別 selector を独立検証可

詳細設計: code-architect ブループリント (W120+W121 統合設計) 参照.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# 楽天市場 (item.rakuten.co.jp) の価格抽出.
# meta[itemprop="price"] は schema.org 標準で楽天が広く採用、shop 独自テンプレでも残るケース多.
# fallback として class="price" 内の数値も読む.
_RAKUTEN_META_PRICE = re.compile(
    r'<meta\s+itemprop="price"\s+content="(\d+)"',
    re.IGNORECASE,
)
_RAKUTEN_CLASS_PRICE = re.compile(
    r'class="price2"[\s\S]{0,300}?([\d,]+)[\s\S]{0,50}?円',
    re.IGNORECASE,
)


def extract_price_rakuten(html: str) -> Optional[int]:
    """楽天市場 商品ページ HTML から税込価格 (JPY int) を抽出.

    優先: meta[itemprop="price"] > class="price2"
    抽出不能なら None.
    H2 fix (2026-05-12): value > 0 防御を追加. 0 を baseline 確定すると永久 sticky silent skip.
    """
    m = _RAKUTEN_META_PRICE.search(html)
    if m:
        try:
            value = int(m.group(1))
            if value > 0:
                return value
        except ValueError:
            pass
    m = _RAKUTEN_CLASS_PRICE.search(html)
    if m:
        try:
            value = int(m.group(1).replace(",", ""))
            if value > 0:
                return value
        except ValueError:
            pass
    return None


# Amazon.co.jp の価格抽出.
# 商品ページの price 表示は category / 状態で変動するため、複数 selector を順次試行.
# 優先: aok-offscreen (a-offscreen) > a-price-whole > JSON priceAmount
_AMAZON_PATTERNS = [
    # SR/Tooltip 内 ¥XX,XXX
    re.compile(r'<span\s+class="a-offscreen">\s*￥([\d,]+)', re.IGNORECASE),
    re.compile(r'<span\s+class="a-offscreen">\s*¥([\d,]+)', re.IGNORECASE),
    # メイン a-price-whole (decimal なし、整数のみ)
    re.compile(r'class="a-price-whole">([\d,]+)<', re.IGNORECASE),
    # JSON-LD priceAmount
    re.compile(r'"priceAmount"\s*:\s*(\d+(?:\.\d+)?)', re.IGNORECASE),
]


def extract_price_amazon(html: str) -> Optional[int]:
    """Amazon.co.jp 商品ページ HTML から価格 (JPY int) を抽出.

    複数 selector を順次試行、最初に match した値を返す.
    抽出不能なら None (bot 検知の空 HTML / 一時的 layout 変化 等).
    """
    for pat in _AMAZON_PATTERNS:
        m = pat.search(html)
        if m:
            try:
                value = float(m.group(1).replace(",", ""))
                if value > 0:
                    return int(value)
            except ValueError:
                continue
    return None


def extract_price(html: str, sku: str) -> Optional[int]:
    """SKU prefix で site 振り分け. 対象外 site は None (= 既存挙動維持)."""
    if not html or not sku:
        return None
    if sku.startswith("ebayAM_"):
        return extract_price_amazon(html)
    if sku.startswith("ebayRT_"):
        return extract_price_rakuten(html)
    return None  # Amazon / 楽天 以外は対象外
