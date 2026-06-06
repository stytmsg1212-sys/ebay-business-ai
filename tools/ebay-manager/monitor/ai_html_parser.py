#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W226 (2026-06-06): 汎用 EC サイトの HTML から商品情報を Claude Haiku で抽出し、
既存 ScrapedProduct を構築するモジュール.

なぜ AI 抽出か:
  - ヤフオク / メルカリ / PayPay は DOM 構造が安定し専用 CSS パーサ
    (supplier_scraper) で十分。一方 Amazon / 楽天 / Yahoo!ショッピング /
    ラクマ / その他無数の EC サイトは構造がバラバラで、サイトごとに CSS
    セレクタを書くのは破綻する。テキスト理解は LLM の得意領域なので Haiku に
    委ねる。
  - 画像 URL だけは LLM の hallucination リスクが高い (存在しない URL を作る)
    ため、BeautifulSoup で決定的に抽出する (og:image + <img>、ブランド/ロゴ除外は
    supplier_scraper のフィルタを流用)。

fail-closed (Q0):
  - ANTHROPIC_API_KEY 未設定 / API 失敗 / JSON 不正 / title 空 → ScrapedProduct に
    scrape_error を必ずセットして返す (呼出側が title-only フォールバックへ誘導)。
  - 仕様 (重量/寸法/付属品) は明記された情報のみ。捏造禁止。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass

try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    pass

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

from monitor.supplier_scraper import (
    ScrapedProduct,
    _normalize_image_url,
    _dedupe_ordered,
    _extract_weight_hint,
    _extract_dimensions,
    _extract_includes,
    _MAX_IMAGES,
)

logger = logging.getLogger(__name__)

# Haiku で十分: テキスト構造化抽出は有界タスク (rank_classifier / product_attrs と同方針)
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# AI に渡す本文の上限 (token / コスト抑制)。商品ページ本文はこれで十分。
_MAX_TEXT_CHARS = 9000

_STABLE_SYSTEM_PROMPT = """あなたは EC サイトの商品ページ本文から、出品に必要な商品情報を構造化抽出する専門家です。

## タスク

入力された商品ページのテキスト (Amazon / 楽天 / Yahoo!ショッピング / ラクマ 等) から、
**明記されている情報のみ** を JSON で返します。推測や hallucination は絶対禁止 — 不明なら null。

## 出力フォーマット (JSON のみ、コードブロック禁止)

{
  "title_ja": "商品タイトル (日本語、ブランド名・型番を含めてよい)",
  "price_jpy": 12800,            // 税込価格 (整数)。不明なら null
  "condition_ja": "新品",        // 商品の状態 (新品 / 中古 / 未使用 等)。不明なら null
  "includes_ja": "本体、リモコン、説明書",  // 付属品。不明なら null
  "description_ja": "商品の特徴・仕様をまとめた説明文 (日本語、5〜15行程度)"
}

## 厳守ルール

1. **title_ja は必須**: ページが商品ページなら必ず商品名を返す。商品ページでない
   (ショップトップ / カテゴリ一覧 / エラーページ / ログイン画面) と判断したら
   title_ja を null にする (捏造禁止)。

2. **description_ja**: ページ本文に書かれている商品の特徴・スペック・状態を日本語で
   要約する。ページに無い情報を足さない。販売者の宣伝文句 (送料無料/ポイント等) や
   サイト共通のナビゲーション文言は除外する。

3. **condition_ja**: ページに状態表記があればそのまま。新品サイト (Amazon 等で
   "新品" 明記、または明らかに新品販売) は "新品"。中古明記なら "中古" + 程度。
   状態の手がかりが全く無ければ null (勝手に新品と決めない)。

4. **price_jpy**: 税込価格を優先。複数候補があれば主たる販売価格。不明なら null。

5. **ハルシネーション禁止**: 型番が読み取れないのに作らない。重量や寸法は
   description_ja に原文があれば含めてよいが、無ければ書かない。

## 具体例

入力(抜粋): "Anker PowerCore 10000 モバイルバッテリー 大容量 ... 新品 ¥2,990 ... PSE技術基準適合"
出力: {"title_ja": "Anker PowerCore 10000 モバイルバッテリー 大容量", "price_jpy": 2990,
       "condition_ja": "新品", "includes_ja": null,
       "description_ja": "Anker の大容量モバイルバッテリー。容量10000mAh。PSE技術基準適合。"}

入力(抜粋): "404 Not Found ページが見つかりません 楽天市場トップへ"
出力: {"title_ja": null, "price_jpy": null, "condition_ja": null, "includes_ja": null,
       "description_ja": null}
"""


def _get_client() -> Optional["anthropic.Anthropic"]:
    if not _ANTHROPIC_OK:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _extract_json(text: str) -> Optional[str]:
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence:
        return fence.group(1)
    greedy = re.search(r"\{[\s\S]*\}", text)
    if greedy:
        return greedy.group(0)
    return None


# 解析/トラッキングピクセル URL の痕跡 (商品画像でない)。2026-06-06 W226 実機検証で
# Amazon の uedata ビーコン (fls-fe.amazon.co.jp/.../batch/.../uedata) が先頭画像に
# 混入し eBay にゴミ画像が入る事故を捕捉 → 明示除外する。
_TRACKING_IMG_MARKERS = (
    # 解析/トラッキングビーコン
    "uedata", "/batch/", "fls-fe.amazon", "fls-na.amazon",
    "beacon", "pixel", "/px.gif", "1x1", "doubleclick",
    "google-analytics", "googletagmanager", "/collect?",
    "scoreboard", "/rd/uedata", "analytics",
    # サイト UI クローム (ナビ/スプライト/ゲートウェイバナー = 商品画像でない)
    "nav-sprite", "/sprites/", "/gno/", "audibleweb",
    "/x-site/", "gateway", "/swm/",
)


def _is_tracking_image(url: str) -> bool:
    """URL が解析/トラッキングビーコン (商品画像でない) か判定。"""
    if not url:
        return True
    low = url.lower()
    return any(m in low for m in _TRACKING_IMG_MARKERS)


def _extract_images(soup, base_url: str) -> list[str]:
    """og:image + <img> から画像 URL を決定的に抽出 (LLM 非依存)。

    ブランド/ロゴ/バナー除外と絶対 URL 化は supplier_scraper のフィルタを流用 +
    W226 でトラッキングピクセル除外を追加。
    """
    images: list[str] = []
    for og in soup.find_all("meta", property="og:image"):
        normalized = _normalize_image_url(og.get("content"), base_url)
        if normalized and not _is_tracking_image(normalized):
            images.append(normalized)
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        normalized = _normalize_image_url(src, base_url)
        if normalized and not _is_tracking_image(normalized):
            images.append(normalized)
    return _dedupe_ordered(images)[:_MAX_IMAGES]


def _extract_text_for_ai(soup) -> str:
    """AI に渡すためにページ本文テキストを抽出 (script/style 除去 + 圧縮)。"""
    # og / meta / title を冒頭に置く (要点が先頭に来るとモデルが拾いやすい)
    head_bits: list[str] = []
    for prop in ("og:title", "og:description"):
        tag = soup.find("meta", property=prop)
        if tag and tag.get("content"):
            head_bits.append(f"[{prop}] {tag['content'].strip()}")
    if soup.title and soup.title.string:
        head_bits.append(f"[title] {soup.title.string.strip()}")
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        head_bits.append(f"[meta description] {md['content'].strip()}")

    # script / style / noscript を除去してから可視テキスト
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    body_text = soup.get_text(separator="\n", strip=True)
    # 連続する空行を圧縮
    body_text = re.sub(r"\n{2,}", "\n", body_text)

    combined = "\n".join(head_bits) + "\n----\n" + body_text
    return combined[:_MAX_TEXT_CHARS]


def parse_html_to_product(url: str, html: str) -> ScrapedProduct:
    """汎用 EC サイトの HTML を Claude Haiku で解析し ScrapedProduct を構築する。

    画像 URL は BeautifulSoup で決定的抽出、テキスト項目は Haiku で抽出する。
    失敗時も例外を投げず scrape_error に理由を格納して返す (fail-closed)。

    Args:
        url: 取得元 URL (画像 URL の絶対化 base にも使う)
        html: fetch_page_html で取得した生 HTML

    Returns:
        ScrapedProduct (platform='ai_html')
    """
    product = ScrapedProduct(url=url, platform="ai_html")

    if not html or not html.strip():
        product.scrape_error = "empty_html"
        return product

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        product.scrape_error = "missing_bs4"
        return product

    soup = BeautifulSoup(html, "html.parser")

    # 画像は LLM を介さず決定的に抽出
    product.image_urls = _extract_images(soup, url)

    text_for_ai = _extract_text_for_ai(soup)
    if len(text_for_ai.strip()) < 50:
        product.scrape_error = "page_text_too_short"
        return product

    client = _get_client()
    if not client:
        product.scrape_error = "ai_unavailable (ANTHROPIC_API_KEY 未設定)"
        return product

    from monitor.api_logger import log_anthropic_response, _Timer

    msg = None
    try:
        with _Timer() as t:
            msg = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=900,
                system=[
                    {
                        "type": "text",
                        "text": _STABLE_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{
                    "role": "user",
                    "content": (
                        f"以下の EC サイト商品ページから商品情報を JSON 抽出してください。\n"
                        f"URL: {url}\n\n---\n{text_for_ai}\n---"
                    ),
                }],
            )
        log_anthropic_response(
            "ai_html_parse", CLAUDE_MODEL, msg,
            duration_ms=t.duration_ms, success=True,
        )
    except anthropic.APIError as e:
        logger.warning("ai_html_parse API error: %s", e)
        log_anthropic_response(
            "ai_html_parse", CLAUDE_MODEL, None,
            success=False, error_message=str(e)[:500],
        )
        product.scrape_error = f"ai_api_error: {str(e)[:200]}"
        return product
    except Exception as e:  # noqa: BLE001
        logger.warning("ai_html_parse unexpected: %s", e)
        product.scrape_error = f"ai_unexpected: {type(e).__name__}: {e}"
        return product

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    cand = _extract_json(text)
    if not cand:
        product.scrape_error = "ai_no_json"
        return product
    try:
        data = json.loads(cand)
    except json.JSONDecodeError as e:
        product.scrape_error = f"ai_json_decode: {e}"
        return product

    title = (data.get("title_ja") or "").strip()
    if not title:
        # 商品ページでない / 抽出不能。Q0: 捏造せず明示エラー。
        product.scrape_error = "ai_parse_empty (商品ページとして認識できず)"
        return product

    product.title_ja = title[:300]

    price = data.get("price_jpy")
    if price is not None:
        try:
            pv = int(price)
            if 0 < pv <= 100_000_000:
                product.price_jpy = pv
        except (TypeError, ValueError):
            pass

    cond = (data.get("condition_ja") or "").strip()
    if cond:
        product.condition_ja = cond[:200]

    includes = (data.get("includes_ja") or "").strip()
    if includes:
        product.includes_ja = includes[:300]

    desc = (data.get("description_ja") or "").strip()
    if desc:
        product.description_ja = desc[:10000]
        # 重量 / 寸法 / 付属品は原文 (description) からのみ決定的に補完
        product.weight_hint_g = _extract_weight_hint(desc)
        if not product.includes_ja:
            product.includes_ja = _extract_includes(desc)
        l, w, d = _extract_dimensions(desc)
        product.length_mm = l
        product.width_mm = w
        product.depth_mm = d

    return product


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    args = parser.parse_args()
    from monitor.html_fetcher import fetch_page_html

    _html, _err = fetch_page_html(args.url)
    if _err:
        print(f"fetch ERROR: {_err}")
        sys.exit(1)
    _p = parse_html_to_product(args.url, _html)
    print(json.dumps({
        "title_ja": _p.title_ja,
        "price_jpy": _p.price_jpy,
        "condition_ja": _p.condition_ja,
        "includes_ja": _p.includes_ja,
        "image_count": len(_p.image_urls),
        "scrape_error": _p.scrape_error,
        "description_ja": (_p.description_ja or "")[:300],
    }, ensure_ascii=False, indent=2))
