"""W153 (2026-05-22 改訂 v2): per-listing 同期 keyword 生成 (Claude Haiku 4.5, title-only).

UI の「🤖 Claude 生成」ボタンから同期呼び出し. cron 経路では呼ばない
(user 編集を尊重、勝手に上書きしない).

ebay_listings に Brand / MPN 列が無いため、本 W は title のみで生成
(user 合意済 2026-05-22). Brand/MPN 列追加は別 W (M-codex-10 admit).

【v2 2026-05-22 PM】: user 視認で「Black 単独で 50 件 noise hit」発覚、
複数 candidate を別 query で union していた当初設計を **空白区切り 1 query**
で AND 検索に変更. Haiku 出力も「ONE best query, 3-8 words space-separated」.

設計書: .company/engineering/docs/2026-05-22-W153-rival-per-listing-detection-design.md
"""
import logging
import os
import re
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

KEYWORD_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 80  # v2: 1 query × ~30 token で十分

_PROMPT_TEMPLATE = """You are an eBay search keyword generator for a Japan->US cross-border seller.

Given an eBay listing's title, output ONE search query that finds direct competitor listings on eBay for the same product.

# Output rules
- ONE query only, on a single line, 3 to 8 words separated by single spaces
- The query will be sent to eBay Browse API as an AND search (every word must match)
- Include brand and any model/part number from the title (these are the strongest narrowing terms)
- Add 1-2 product-category words (e.g. "Cassette Player", "Headphones", "Mini Cocotte")
- Skip color/size/capacity unless they are catalog-bound (e.g. always include "Rose Gold" only if the model name itself depends on it)
- Skip filler: condition (NEW/Used/Mint), year, packaging tags, region tags ("from Japan", "F/S")
- No quotes, no numbering, no explanation, no apologies — output ONLY the query line

# Title
{title}

# Output (single line, 3-8 words)"""

# H-F: apology / explanation / numbering pattern reject
_APOLOGY_PATTERN = re.compile(
    r"(?i)(I (cannot|can'?t|am sorry|apolog|don'?t know|need)"
    r"|sorry|here are|note:|please)"
)
_NUMBERED_PATTERN = re.compile(r"^[0-9]+[\.\)]")


def _resolve_api_key() -> str:
    """H-F: EBAY_ANTHROPIC_KEY 優先 → ANTHROPIC_API_KEY fallback."""
    key = os.environ.get("EBAY_ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "Anthropic API key not set "
            "(checked EBAY_ANTHROPIC_KEY, ANTHROPIC_API_KEY)"
        )
    return key


def generate_keywords(
    *,
    title: str,
    brand: Optional[str] = None,   # 将来拡張用 signature 保持 (本 W では未使用)
    mpn: Optional[str] = None,
    specifics: Optional[dict] = None,
) -> str:
    """eBay 競合検索用 query (3-8 word 空白区切り) を Claude Haiku で生成 (title-only).

    【v2 2026-05-22 PM】: 返り値 list[str] → str に変更. 当初の複数 candidate union
    設計は「Black 単独で 50 件 noise」を生み、空白区切り 1 query AND 検索に統一.

    Args:
        title: ebay_listings.title (必須).
        brand/mpn/specifics: 将来拡張用 (Brand/MPN 列追加 W で復活).

    Returns:
        str: 1 query, 3-8 words space-separated.

    Raises:
        RuntimeError: API key 未設定.
        ValueError: Haiku output が異常 (no valid query line after filter).
        anthropic.APIError: API 障害 (caller handles, UI で st.error).
    """
    api_key = _resolve_api_key()
    client = anthropic.Anthropic(api_key=api_key)

    prompt = _PROMPT_TEMPLATE.format(title=title or "(empty)")

    msg = client.messages.create(
        model=KEYWORD_MODEL,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text if msg.content else ""

    # v2: 最初の valid line を採用 (Haiku が複数行返した場合の defensive)
    for line in raw.split("\n"):
        s = line.strip().strip('"\'.,!?;:')
        if not s:
            continue
        if len(s) > 100:
            continue
        # H-F output filter: apology → numbered → word-count の順
        if _APOLOGY_PATTERN.search(s):
            logger.warning(
                f"[W153 generator] rejected apology-like line: {s[:60]!r}"
            )
            continue
        if _NUMBERED_PATTERN.match(s):
            logger.warning(
                f"[W153 generator] rejected numbered line: {s[:60]!r}"
            )
            continue
        # v2: 連続空白 collapse (Haiku が "maxell  MXCP-P100" のように 2 空白返す対策)
        s = re.sub(r"\s+", " ", s)
        words = s.split(" ")
        if not (3 <= len(words) <= 8):
            logger.warning(
                f"[W153 generator] rejected wrong-word-count line "
                f"(words={len(words)}): {s[:60]!r}"
            )
            continue
        return s

    raise ValueError(
        f"Haiku returned no valid query line (need 3-8 words). raw={raw!r}"
    )
