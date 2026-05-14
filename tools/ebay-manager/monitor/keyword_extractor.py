"""W7-A 市場戦略: SKU title から Terapeak 検索用 keyword 抽出.

Haiku 4.5 を使って以下を抽出:
  - brand (例: Audio-Technica, HIOKI, Pioneer)
  - model_number (例: ATH-CKS330NC, 9694, Lonesome-Carboy)
  - 余分なノイズ語 (ジャンク, 美品, used 等) は除去

入力:  "Audio-Technica ATH-CKS330NC ATH CKS330NC ワイヤレス [美品]"
出力:  "Audio-Technica ATH-CKS330NC"

API call:
  - Haiku $0.0005/件 × 239 SKU = 約 $0.12/全件
  - cache 不可 (SKU 毎に異なる) → cache_control なし
  - 5/3 W25 ヒアリングで品質確認予定
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = """You are a search-keyword extractor for eBay Terapeak (Research products).
Given an eBay listing title in Japanese or English, return a clean search keyword
consisting of brand + model number that maximizes Terapeak search hit rate.

Rules:
- Output JSON: {"keyword": "Brand ModelNumber"} only
- Remove condition words (ジャンク, 美品, used, new, mint, etc.)
- Remove parentheses content like (中古) (S2)
- Remove duplicate phrases (e.g. "ATH-CKS330NC ATH CKS330NC" → "ATH-CKS330NC")
- Keep brand exactly (Audio-Technica, HIOKI, Pioneer, KEYENCE, etc.)
- Prefer specific model numbers over generic words
- If no clear model number, use brand + main product noun
- Max 60 chars
"""

_USER_TEMPLATE = """Title: {title}

Output the search keyword as JSON: {{"keyword": "..."}}"""


def _fallback_extract(title: str) -> str:
    """API 失敗時の fallback: タイトル先頭 50 文字 + 重複除去."""
    if not title:
        return ""
    # 余分な記号 / 括弧除去
    cleaned = re.sub(r'\[[^\]]*\]|\([^)]*\)', '', title)
    cleaned = re.sub(r'[／/、,]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # 重複 token 除去 (順序保持)
    tokens = cleaned.split()
    seen = set()
    out = []
    for t in tokens:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            out.append(t)
    result = " ".join(out[:8])  # 最大 8 token
    return result[:60]


def extract_keyword(title: str, *, use_ai: bool = True) -> str:
    """SKU title から Terapeak 検索 keyword を抽出.

    Args:
        title: 自社 listing title
        use_ai: True なら Haiku 呼出, False なら fallback のみ

    Returns:
        検索 keyword 文字列 (失敗時は fallback 結果)
    """
    if not title or not title.strip():
        return ""

    if not use_ai:
        return _fallback_extract(title)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY 未設定. fallback 使用")
        return _fallback_extract(title)

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK 未インストール. fallback 使用")
        return _fallback_extract(title)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=200,
            system=_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": _USER_TEMPLATE.format(title=title[:300])}
            ],
        )
        content = resp.content[0].text if resp.content else "{}"
        # JSON 部分のみ抽出 (前後に説明文混在対策)
        m = re.search(r'\{[^}]*"keyword"[^}]*\}', content, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            kw = data.get("keyword", "").strip()
            if kw:
                # api_logger に課金記録 (任意 — 既存パターン follow)
                try:
                    from monitor.api_logger import log_api_call
                    log_api_call(
                        model=HAIKU_MODEL,
                        operation="keyword_extract",
                        input_tokens=resp.usage.input_tokens,
                        output_tokens=resp.usage.output_tokens,
                    )
                except Exception:
                    pass
                return kw[:60]
    except (anthropic.APIError, json.JSONDecodeError, KeyError, AttributeError) as e:
        logger.warning(f"Haiku keyword extract failed: {e}")

    return _fallback_extract(title)


def batch_extract(titles: list[str], *, use_ai: bool = True) -> list[str]:
    """複数 title を一括抽出. 個別エラーは fallback に降格.

    Args:
        titles: title 配列
        use_ai: AI 利用フラグ

    Returns:
        keyword 配列 (titles と同じ順序・長さ)
    """
    return [extract_keyword(t, use_ai=use_ai) for t in titles]
