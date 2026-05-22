"""W153 (2026-05-22): per-listing 同期 keyword 生成 (Claude Haiku 4.5, title-only).

UI の「🤖 Claude 生成」ボタンから同期呼び出し. cron 経路では呼ばない
(user 編集を尊重、勝手に上書きしない).

ebay_listings に Brand / MPN 列が無いため、本 W は title のみで生成
(user 合意済 2026-05-22). Brand/MPN 列追加は別 W (M-codex-10 admit).

設計書: .company/engineering/docs/2026-05-22-W153-rival-per-listing-detection-design.md
"""
import logging
import os
import re
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

KEYWORD_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 200  # 3-5 候補 × 30 token 程度

_PROMPT_TEMPLATE = """You are an eBay search keyword generator for a Japan->US cross-border seller.

Given an eBay listing's title, output 3-5 search keyword candidates that find direct competitor listings on eBay.

# Rules
- Each candidate: 3-6 words, separated by spaces (URL-friendly)
- Include any brand/model number that you can extract from the title (narrowing is important)
- Add differentiating attributes (color / capacity / size / variant) when extractable
- Skip filler words: condition (NEW/Used/Mint), year, packaging, region tags ("from Japan", "F/S")
- Output ONE candidate per line, no numbering, no quotes, no explanations, no apologies
- If the title is in Japanese, keep brand names in English / Latin script when possible
- Output ONLY the candidates, nothing else

# Title
{title}

# Output (3-5 lines)"""

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
) -> list[str]:
    """eBay 競合検索用 keyword 候補 3-5 件を Claude Haiku で生成 (title-only).

    Args:
        title: ebay_listings.title (必須).
        brand/mpn/specifics: 将来拡張用 (Brand/MPN 列追加 W で復活).

    Returns:
        list[str]: 3-5 entries, each 3-6 words.

    Raises:
        RuntimeError: API key 未設定.
        ValueError: Haiku output が異常 (<3 valid candidates after filter).
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

    candidates: list[str] = []
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
        words = s.split()
        if not (3 <= len(words) <= 6):
            logger.warning(
                f"[W153 generator] rejected wrong-word-count line: {s[:60]!r}"
            )
            continue
        candidates.append(s)

    # H-F: <3 valid でも raise (0 だけでなく)
    if len(candidates) < 3:
        raise ValueError(
            f"Haiku returned only {len(candidates)} valid candidates "
            f"(need >=3). raw={raw!r}"
        )

    return candidates[:5]
