#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W13 X ベース AI ニュース取得: dedupe + classifier pipeline

code-reviewer H-2 指摘対応:
  - L2 dedupe は title 類似度 (stdlib difflib SequenceMatcher, 軽量)
    → embedding/cosine は追加依存を避けるため Phase 2 留保
  - L3 dedupe LLM は impact=high 候補のみ、batch 化
  - classifier は Claude Haiku 4.5 で batch 判定 (10 件/call)

SRP 整理:
  - NewsRaw (fetchers) -> dedupe -> classify -> save_news_item_v2 に流す
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

from monitor.x_news_fetchers import NewsRaw

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# L1-L2 dedupe
# ─────────────────────────────────────────────

_TITLE_SIMILARITY_THRESHOLD = 0.70


def _normalize_title(t: str) -> str:
    """比較用タイトル正規化 (大小無視, 連続空白, 記号除去)."""
    t = (t or "").lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


def dedupe_local(items: list[NewsRaw]) -> list[NewsRaw]:
    """L1 (URL 一致) + L2 (Title 類似度) でローカル重複除去.

    同一 URL: 完全一致で除外、engagement は最大値を保持.
    類似 title: SequenceMatcher ratio >= 0.70 で同一視、engagement 高い方を残す.
    """
    # Phase 1: URL 完全一致 dedupe
    by_url: dict[str, NewsRaw] = {}
    no_url: list[NewsRaw] = []
    for it in items:
        if it.url:
            cur = by_url.get(it.url)
            if cur is None:
                by_url[it.url] = it
            else:
                # engagement 高い方を残す
                if it.engagement_count > cur.engagement_count:
                    by_url[it.url] = it
        else:
            no_url.append(it)
    candidates = list(by_url.values()) + no_url

    # Phase 2: Title 類似度 dedupe (O(N^2) だが 100 件程度想定)
    kept: list[NewsRaw] = []
    for it in sorted(candidates, key=lambda x: -x.engagement_count):
        dup = False
        for k in kept:
            if _title_similarity(it.title, k.title) >= _TITLE_SIMILARITY_THRESHOLD:
                # 既に同系統が残っている → スキップ
                dup = True
                break
        if not dup:
            kept.append(it)
    return kept


# ─────────────────────────────────────────────
# Classifier (Claude Haiku 4.5)
# ─────────────────────────────────────────────

_CLASSIFIER_SYSTEM = """あなたは eBay 越境 EC セラー向けの AI ニュース翻訳者・影響度判定者です。
各ニュース項目について:
1. 日本語で 1〜2 文の要約 (summary_ja)
2. 影響度 (impact_level): "high" (モデル新リリース/重大な API 変更/大規模ツール発表)
                          / "medium" (機能拡張/アップデート/有力ツール話題)
                          / "low" (日常アップデート/バグ修正/意見)
                          / "noise" (関係ない/スパム的/重複に近い雑情報)
3. カテゴリ (category): "model-release" / "dev-tool" / "e-commerce" / "tariff" / "other"
4. eBay 物販への影響 (impact_ja): 1 文 (無ければ空文字)

英語/日本語混在 OK. 出力は必ず JSON 配列のみ。コードブロック・前置き・説明不要.

入力例 (ID 付き):
[
  {"id": 1, "title": "...", "text": "...", "source": "x"},
  ...
]

出力例:
[
  {"id": 1, "summary_ja": "...", "impact_level": "high", "category": "model-release", "impact_ja": "..."},
  ...
]
"""


@dataclass
class Classified(NewsRaw):
    summary_ja: str = ""
    impact_level: str = "low"
    category: str = "other"
    impact_ja: str = ""


def classify_batch(items: list[NewsRaw], *,
                   daily_cap_usd: float = 1.0) -> list[Classified]:
    """Claude Haiku で 10 件ずつ batch 判定.

    budget cap 到達時は残り全 item を impact='low' default で埋めて返す (run 継続).
    """
    try:
        import anthropic  # type: ignore
    except ImportError:
        logger.warning("anthropic SDK not installed, falling back to defaults")
        return [_passthrough(i) for i in items]

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, falling back to defaults")
        return [_passthrough(i) for i in items]

    from monitor.database import add_api_cost, get_todays_api_cost

    client = anthropic.Anthropic(api_key=api_key)
    classified: list[Classified] = []
    batch_size = 10

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        # budget check before call
        spent = get_todays_api_cost("anthropic")
        # Haiku 4.5 概算: $0.25/$1.25 per 1M tokens. 10 件 ≒ 4K tokens ≒ $0.005
        est_cost = 0.01
        if spent + est_cost > daily_cap_usd:
            logger.warning(
                f"Anthropic budget exceeded (spent=${spent:.4f}), "
                f"passing through {len(items) - i} items as defaults"
            )
            classified.extend([_passthrough(it) for it in items[i:]])
            break

        payload = [
            {
                "id": j,
                "title": it.title,
                "text": it.raw_content[:500],
                "source": it.source_type,
            }
            for j, it in enumerate(batch)
        ]
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                system=_CLASSIFIER_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            )
            text = "".join(
                b.text for b in resp.content if hasattr(b, "text")
            ).strip()
            # JSON 抽出 (モデルが余計な文字を混ぜる場合に備え)
            m = re.search(r"\[.*\]", text, re.S)
            parsed = json.loads(m.group(0) if m else text)
            # cost recording
            in_tok = getattr(resp.usage, "input_tokens", 0) if resp.usage else 0
            out_tok = getattr(resp.usage, "output_tokens", 0) if resp.usage else 0
            actual_cost = in_tok * 0.25e-6 + out_tok * 1.25e-6
            add_api_cost("anthropic", actual_cost, "x_news_classifier")
        except (
            anthropic.APIError, anthropic.APIConnectionError,
            anthropic.RateLimitError, anthropic.APIStatusError,
            json.JSONDecodeError, ValueError, KeyError, AttributeError,
        ) as e:
            # H-W13-3 対応: bare except 規約違反を除去. 既知の SDK/parse 例外のみ catch.
            logger.warning(f"classifier batch {i//batch_size} failed: {type(e).__name__}: {e}")
            classified.extend([_passthrough(it) for it in batch])
            continue

        # 各 item にマージ
        by_id = {int(p.get("id", -1)): p for p in parsed if isinstance(p, dict)}
        for j, it in enumerate(batch):
            p = by_id.get(j, {})
            classified.append(_merge(it, p))

    return classified


def _passthrough(it: NewsRaw) -> Classified:
    """classifier スキップ時のデフォルト詰め.

    H-W13-2 対応: budget 枯渇時の未分類アイテムを DB に low で流し込むと
    ダッシュボードが未分類記事で汚染されるため、impact_level="noise" を返して
    task 側で DB 保存を skip させる. extra に `classifier_skipped=True` を付け、
    将来の再 classify 実装 (Phase 2) のための目印にする.
    """
    merged_extra = dict(it.extra)
    merged_extra["classifier_skipped"] = True
    return Classified(
        source_type=it.source_type, source_handle=it.source_handle,
        url=it.url, title=it.title, raw_content=it.raw_content,
        engagement_count=it.engagement_count, published_at=it.published_at,
        extra=merged_extra,
        summary_ja=it.title[:160], impact_level="noise",
        category="other", impact_ja="",
    )


def _merge(it: NewsRaw, p: dict) -> Classified:
    impact = str(p.get("impact_level") or "low").lower()
    if impact not in ("high", "medium", "low", "noise"):
        impact = "low"
    category = str(p.get("category") or "other").lower()
    return Classified(
        source_type=it.source_type, source_handle=it.source_handle,
        url=it.url, title=it.title, raw_content=it.raw_content,
        engagement_count=it.engagement_count, published_at=it.published_at,
        extra=it.extra,
        summary_ja=str(p.get("summary_ja") or it.title[:160]),
        impact_level=impact, category=category,
        impact_ja=str(p.get("impact_ja") or ""),
    )


# ─────────────────────────────────────────────
# 統合 pipeline
# ─────────────────────────────────────────────

def run_pipeline(items: list[NewsRaw], *,
                 anthropic_cap_usd: float = 1.0) -> list[Classified]:
    """fetch 済みアイテムに dedupe + classify を流す."""
    if not items:
        return []
    logger.info(f"pipeline start: {len(items)} raw items")
    deduped = dedupe_local(items)
    logger.info(f"  after local dedupe: {len(deduped)} items")
    # noise 候補と low engagement を classifier 前に削る
    filtered = [i for i in deduped if i.title.strip()]
    classified = classify_batch(filtered, daily_cap_usd=anthropic_cap_usd)
    logger.info(f"  after classify: {len(classified)} items")
    return classified


__all__ = [
    "dedupe_local", "classify_batch", "run_pipeline",
    "Classified", "_normalize_title", "_title_similarity",
]
