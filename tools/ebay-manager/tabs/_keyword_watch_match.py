"""W#33 — キーワード新着監視 × 商品管理 類似突合 helper.

純関数として切り出し、単体テスト可能。UI 依存なし。
sku-rules.md: listing 識別は ebay_item_id のみ (title はマッチング判定にのみ使用)。
"""
from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class LegacyEntry(NamedTuple):
    legacy_id: int
    site: str
    search_url: str
    keyword: str
    price_min_jpy: int | None
    price_max_jpy: int | None


class ListingEntry(NamedTuple):
    ebay_item_id: str
    title: str


class MatchResult(NamedTuple):
    legacy: LegacyEntry
    listing: ListingEntry
    score: float          # 0.0 – 1.0
    matched_tokens: list[str]   # debug 用


# ---------------------------------------------------------------------------
# Text normalizer
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """全角→半角・小文字・非英数除去の正規化."""
    # NFKC: 全角英数→半角、合成文字を分解
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    # 記号・括弧類を空白に変換
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    # 連続空白を 1 つに
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokenize(text: str) -> list[str]:
    """正規化済みテキストをトークン分割. 1 文字トークンは除外 (ノイズ)."""
    tokens = _normalize(text).split()
    return [t for t in tokens if len(t) >= 2]


# ---------------------------------------------------------------------------
# Core matching
# ---------------------------------------------------------------------------

def _score(legacy_tokens: list[str], listing_tokens: list[str]) -> tuple[float, list[str]]:
    """legacy keyword のトークンが listing title に何割含まれるかを計算.

    scoring:
    - legacy_tokens が listing_tokens に何個 partial match するか / len(legacy_tokens)
    - partial match: listing の任意トークンに legacy トークンが含まれる (部分一致)
    - 完全一致ボーナス: +0.1 per token (max 0.2)

    legacy_tokens が空の場合は 0.0 を返す (ガード).
    """
    if not legacy_tokens:
        return 0.0, []

    matched: list[str] = []
    bonus = 0.0
    for lt in legacy_tokens:
        found = False
        for lst in listing_tokens:
            if lt in lst:            # partial: lt が lst に含まれる
                found = True
                if lt == lst:        # 完全一致ボーナス
                    bonus += 0.1
                break
        if found:
            matched.append(lt)

    base_score = len(matched) / len(legacy_tokens)
    score = min(1.0, base_score + min(0.2, bonus))
    return round(score, 4), matched


def match_legacy_to_listings(
    legacy_entries: list[LegacyEntry],
    listing_entries: list[ListingEntry],
    *,
    score_threshold: float = 0.0,
) -> list[MatchResult]:
    """旧リスト × eBay 出品 を総当たりで突合し、各 legacy に最良 1 listing を返す.

    - 既存 keyword_watches.ebay_item_id 紐付き listing は呼び出し元で除外しておくこと。
    - score_threshold > 0 を指定すると、それ未満は結果から除外される。
    - 返値は score 降順ソート。

    sku-rules: マッチング判定に title を使うが、返値の ListingEntry.ebay_item_id
    を登録キーとして使う (title は登録キーにしない)。
    """
    if not listing_entries:
        return []

    # 全 listing のトークンをキャッシュ (O(N*M) を抑制)
    listing_token_cache: dict[str, list[str]] = {
        le.ebay_item_id: _tokenize(le.title)
        for le in listing_entries
    }

    results: list[MatchResult] = []

    for leg in legacy_entries:
        kw_tokens = _tokenize(leg.keyword)
        if not kw_tokens:
            continue

        best_score = -1.0
        best_listing: ListingEntry | None = None
        best_matched: list[str] = []

        for le in listing_entries:
            lst_tokens = listing_token_cache[le.ebay_item_id]
            sc, matched = _score(kw_tokens, lst_tokens)
            if sc > best_score:
                best_score = sc
                best_listing = le
                best_matched = matched

        if best_listing is None:
            continue
        if best_score < score_threshold:
            continue

        results.append(MatchResult(
            legacy=leg,
            listing=best_listing,
            score=best_score,
            matched_tokens=best_matched,
        ))

    results.sort(key=lambda r: -r.score)
    return results
