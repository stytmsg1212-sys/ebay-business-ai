#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
学習済動画の知識検索（research / supplier_finder 用の共通ヘルパー）

キーワード一致で knowledge_index → videos_learned を引き、
関連する動画要約・insight・pricing_hint をプロンプト注入用にまとめる。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from monitor.database import get_conn

logger = logging.getLogger(__name__)


def _extract_candidate_keywords(text: str) -> list[str]:
    """eBay タイトル等から、KB 検索に使える候補キーワードを抽出。

    簡易実装: 英大文字から始まる単語（ブランド/型番）＋ 3文字以上の英数字トークン。
    """
    if not text:
        return []
    # ブランド・型番っぽいトークン（連続大文字2文字以上、または英数混合）
    tokens = set()
    for m in re.findall(r'\b[A-Z][A-Za-z0-9\-]{2,}\b', text):
        tokens.add(m)
    # 日本語カタカナブランド（3文字以上連続）
    for m in re.findall(r'[ァ-ヶー]{3,}', text):
        tokens.add(m)
    return list(tokens)


def find_related_knowledge(
    query_text: str, max_videos: int = 3, max_keywords: int = 10,
) -> list[dict]:
    """query_text（商品タイトル・検索キーワード等）に関連する学習済動画を返す。

    Returns: [{video_id, title, channel, summary_ja, matched_keywords, key_insights,
               pricing_hints, actionable_steps}, ...] (最大 max_videos 件)
    """
    if not query_text:
        return []

    candidates = _extract_candidate_keywords(query_text)[:max_keywords]
    if not candidates:
        return []

    # knowledge_index で case-insensitive 一致検索
    with get_conn() as conn:
        placeholders = ",".join("?" * len(candidates))
        # keyword LIKE クエリは遅いので、完全一致 + LOWER で試す
        rows = conn.execute(
            f"""SELECT ki.video_id, ki.keyword
                FROM knowledge_index ki
                WHERE LOWER(ki.keyword) IN ({placeholders})""",
            [c.lower() for c in candidates],
        ).fetchall()

        # video_id 毎に matched keywords を集約
        matches: dict[str, list[str]] = {}
        for r in rows:
            matches.setdefault(r["video_id"], []).append(r["keyword"])

        if not matches:
            return []

        # matched keywords 数の多い順
        top_video_ids = sorted(matches.keys(), key=lambda v: -len(matches[v]))[:max_videos]

        # video 詳細取得（done のみ）
        vplaceholders = ",".join("?" * len(top_video_ids))
        videos = conn.execute(
            f"""SELECT video_id, title, channel, summary_ja, key_insights,
                       pricing_hints, actionable_steps, topics
                FROM videos_learned
                WHERE video_id IN ({vplaceholders}) AND status='done'""",
            top_video_ids,
        ).fetchall()

    result = []
    for v in videos:
        d = dict(v)
        d["matched_keywords"] = matches.get(d["video_id"], [])
        d["key_insights"] = json.loads(d.get("key_insights") or "[]")
        d["pricing_hints"] = json.loads(d.get("pricing_hints") or "[]")
        d["actionable_steps"] = json.loads(d.get("actionable_steps") or "[]")
        result.append(d)

    # matched keyword 数順でソート
    result.sort(key=lambda x: -len(x.get("matched_keywords") or []))
    return result


def format_knowledge_for_prompt(videos: list[dict], max_chars: int = 3000) -> str:
    """related_knowledge の結果を Claude プロンプトに注入可能な文字列に整形。

    長すぎて context 圧迫しないよう max_chars で切り詰め。
    """
    if not videos:
        return ""

    parts = []
    parts.append("## 過去の動画学習から関連知識\n")
    for v in videos:
        parts.append(f"\n### 動画: {v.get('title','')[:80]} ({v.get('channel','')})")
        parts.append(f"マッチキーワード: {', '.join(v.get('matched_keywords') or [])}")
        if v.get("summary_ja"):
            parts.append(f"要約: {v['summary_ja'][:300]}")
        insights = v.get("key_insights") or []
        if insights:
            parts.append("関連 insight:")
            for i in insights[:5]:
                parts.append(f"- {str(i)[:200]}")
        pricing = v.get("pricing_hints") or []
        if pricing:
            parts.append("価格ヒント:")
            for p in pricing[:3]:
                if isinstance(p, dict):
                    parts.append(
                        f"- {p.get('product','?')}: {p.get('range','?')} "
                        f"（{p.get('reasoning','')[:100]}）"
                    )

    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(省略)"
    return text


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    query = " ".join(sys.argv[1:]) or "Speedpak Economy 出品戦略"
    print(f"query: {query!r}")
    print(f"keywords: {_extract_candidate_keywords(query)}")
    videos = find_related_knowledge(query)
    print(f"\nfound {len(videos)} related videos")
    for v in videos:
        print(f"\n--- {v['title']} ---")
        print(f"matched: {v['matched_keywords']}")
        print(f"summary: {v['summary_ja'][:150]}")
    print("\n=== formatted for prompt ===")
    print(format_knowledge_for_prompt(videos))
