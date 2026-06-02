#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W209 Phase 2: AI ニュース関連度スコアリング (Haiku 4.5).

各記事タイトル + ソースから、eBay 物販自動化への関連度を 0-100 で採点し、
4 軸 (a/b/c/d) のいずれかへ分類する。

軸定義 (user 確定、2026-06-02):
- a: Claude / Codex / MCP / Agent 等の技術
- b: LLM / Agent の新能力を出品文 / 価格最適化 / 仕入れ監視 / CS に応用
- c: eBay / 越境 EC / 関税制度
- d: スクレイピング / anti-bot

採点指針 (プロンプトに明示):
- 既存モジュール (ebay_lister / supplier_apply / claude_evaluator / customs_draft_generator
  / scrapers / lowest_price / image_composer 等) への直接的な改善余地が見えるほど高得点。
- 純粋な研究 / 政策 / 安全性 / 雑談は低得点 (深掘り対象外 = none)。

Phase 3 (deep_dive) は relevance_score >= 60 のみを対象とする。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# .env ロード (claude_summarizer と同じパターン)
try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

MODEL = "claude-haiku-4-5-20251001"

RELEVANCE_SYSTEM = """あなたは越境EC物販ツール (eBay Manager) の技術参謀です。
AI / Tech ニュースを読み、本ツールの自動化機能 (個別出品 / 価格最適化 /
仕入れ監視 / カスタマーサポート / 通関書類生成 / スクレイピング) への
関連度を 0-100 で採点します。

関連度の 4 軸:
- a: Claude / Codex / MCP / Agent SDK / Anthropic API 等の技術更新
- b: LLM / Agent の新能力 (vision / tool use / batch 等) を 出品文生成 /
     価格最適化 / 仕入れ監視 / CS 応答 / 通関ドラフト に応用できる話
- c: eBay / 越境 EC / 米国関税制度 (Section 232 / DDP) の動向
- d: スクレイピング / anti-bot 技術 (Playwright / Cloudflare 突破 / 画像 OCR 等)

出力は厳密な JSON のみ (```json フェンス禁止、余分なテキスト禁止):
{
  "relevance_score": 0-100 の整数,
  "axis": "a" | "b" | "c" | "d" | "none",
  "reason_ja": "なぜそのスコア/軸か 1 文 (どのモジュールに効きそうか具体的に)"
}

スコア目安:
- 80-100: 既存モジュール (ebay_lister / supplier_apply 等) を直接書き換えるレベル
- 60-79:  新機能で採用余地あり (深掘り対象)
- 40-59:  間接的に参考 (深掘りは見送り)
- 1-39:   ほぼ無関係 (純研究 / 政策 / 雑談)
- 0:      完全に無関係 → axis="none"

axis="none" は relevance_score=0 と同義 (深掘り対象から外す)。
日本語以外で reason_ja を返さない。"""


def _strip_fenced_json(text: str) -> Optional[str]:
    """Claude が ```json フェンスを付けた場合の抽出 (claude_summarizer と同じパターン)."""
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if fence:
        return fence.group(1)
    greedy = re.search(r'\{[\s\S]*\}', text)
    if greedy:
        return greedy.group(0)
    open_brace = re.search(r'\{[\s\S]*$', text)
    if open_brace:
        return open_brace.group(0).rstrip() + "}"
    return None


def score_relevance(title: str, summary: str = "", source: str = "") -> dict:
    """ニュース 1 件の関連度を Haiku で採点する。

    Args:
        title: 記事タイトル (必須).
        summary: 既存の日本語要約 (あれば context 強化に使う、無くて OK).
        source: 'Anthropic News' / 'r/ClaudeAI' / 'HN: claude opus' 等.

    Returns:
        {'relevance_score': int(0-100), 'axis': 'a'/'b'/'c'/'d'/'none',
         'reason_ja': str}
        失敗時は {'relevance_score': 0, 'axis': 'none',
                 'reason_ja': '<error message>'} を返し、深掘り対象から自然除外する
        (Q0: silent skip ではなく痕跡を残す = reason_ja で可視化)。
    """
    title = (title or "").strip()
    if not title:
        return {
            "relevance_score": 0, "axis": "none",
            "reason_ja": "title 空 = 採点不能",
        }

    if not _ANTHROPIC_OK:
        logger.warning("anthropic package not installed (score_relevance)")
        return {
            "relevance_score": 0, "axis": "none",
            "reason_ja": "anthropic SDK 未インストール",
        }
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY missing (score_relevance)")
        return {
            "relevance_score": 0, "axis": "none",
            "reason_ja": "ANTHROPIC_API_KEY 未設定",
        }

    from monitor.api_logger import log_anthropic_response, _Timer
    from monitor.database import add_api_cost
    from monitor.api_logger import _estimate_cost_usd

    user_text = (
        f"Source: {source}\n"
        f"Title: {title}\n"
    )
    if summary:
        user_text += f"既存要約 (日本語): {summary[:500]}\n"
    user_text += "\n上記を JSON で採点してください。"

    client = anthropic.Anthropic()
    msg = None
    cost_usd = 0.0
    try:
        with _Timer() as t:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=300,
                system=[
                    {"type": "text", "text": RELEVANCE_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": user_text}],
            )
        log_anthropic_response(
            "news_relevance", MODEL, msg, duration_ms=t.duration_ms, success=True,
        )
        # add_api_cost で context='news_relevance' を sub-budget 集計に使う
        in_tok = int(getattr(msg.usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(msg.usage, "output_tokens", 0) or 0)
        cache_r = int(getattr(msg.usage, "cache_read_input_tokens", 0) or 0)
        cache_w = int(getattr(msg.usage, "cache_creation_input_tokens", 0) or 0)
        cost_usd = _estimate_cost_usd(MODEL, in_tok, out_tok, cache_r, cache_w)
        try:
            add_api_cost("anthropic", cost_usd, context="news_relevance")
        except Exception as e:  # noqa: BLE001
            # budget 集計失敗は本処理を止めない (warning で痕跡)
            logger.warning(f"add_api_cost (news_relevance) 失敗: {e}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"score_relevance Claude API error: {e}")
        log_anthropic_response(
            "news_relevance", MODEL, None, duration_ms=None,
            success=False, error_message=str(e)[:500],
        )
        return {
            "relevance_score": 0, "axis": "none",
            "reason_ja": f"Claude API エラー: {type(e).__name__}",
        }

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    cand = _strip_fenced_json(text)
    if not cand:
        logger.warning(f"score_relevance: no JSON in response: {text[:100]!r}")
        return {
            "relevance_score": 0, "axis": "none",
            "reason_ja": "Claude 応答に JSON 無し",
        }
    try:
        parsed = json.loads(cand)
    except json.JSONDecodeError as e:
        logger.warning(f"score_relevance JSON decode: {e}, raw={text[:120]!r}")
        return {
            "relevance_score": 0, "axis": "none",
            "reason_ja": f"JSON decode 失敗: {e}",
        }

    # 値の正規化 + sanity check
    try:
        rs = int(parsed.get("relevance_score") or 0)
    except (TypeError, ValueError):
        rs = 0
    rs = max(0, min(100, rs))
    ax = str(parsed.get("axis") or "none").lower().strip()
    if ax not in ("a", "b", "c", "d", "none"):
        ax = "none"
    if rs == 0 and ax != "none":
        ax = "none"  # 整合性 (rs=0 のときは必ず none)
    reason = str(parsed.get("reason_ja") or "").strip()[:300]

    return {
        "relevance_score": rs,
        "axis": ax,
        "reason_ja": reason,
    }
