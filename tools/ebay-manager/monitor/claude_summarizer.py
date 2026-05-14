#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Haiku による要約ヘルパー（メール / ニュース共通）

出力は必ず厳密な JSON 形式。プロンプトキャッシュで共通指示をキャッシュ化し、
大量呼び出し時のコスト/レイテンシを最小化する。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# .env ロード
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

EMAIL_SYSTEM = """あなたは越境ECセラーの秘書です。eBay関連の英文メールを読み、
日本語で要約して何をすべきか提示します。

出力は厳密な JSON のみ（```json フェンス禁止、余分なテキスト禁止）:
{
  "category": "buyer_message" | "sale" | "offer" | "return" | "payment" | "feedback" | "promo" | "other",
  "priority": "urgent" | "high" | "normal" | "low",
  "summary_ja": "1〜2文で内容要約（何が起きたか）",
  "action_ja": "次にやるべきアクションを一文で（具体的に）",
  "buyer_message_ja": "バイヤーの実際の問い合わせ/返信本文があれば日本語訳。無ければ空文字"
}

優先度の目安:
- urgent: 返品/クレーム/Defect率に影響するもの
- high: 新規問い合わせ（24h以内返信）、オファー受領、売上通知（発送準備）
- normal: 返信の続き、支払い確認、フィードバック受領
- low: eBayからの自動プロモ/お知らせ

カテゴリ判定は subject と body の両方を見る。eBay プロモメールは promo。
summary_ja は「誰が何を」を具体的に（例: "iPhone Pro購入希望者からの値下げ交渉"）。
テンプレ的な文言は避け、**実際の会話内容を反映** する。"""


NEWS_SYSTEM = """あなたは越境ECセラーの技術参謀です。AIニュースを読み、
eBay物販ビジネス（Claude API/MCP/エージェントSDK/自動化ツール）への影響を日本語で解説します。

出力は厳密な JSON のみ（```json フェンス禁止、余分なテキスト禁止）:
{
  "summary_ja": "ニュース内容を2〜3文で日本語要約",
  "impact_ja": "eBay物販ビジネスにどう影響するか一文で（具体的に）",
  "impact_level": "high" | "medium" | "low" | "none",
  "categories": ["api_change" | "new_feature" | "pricing" | "mcp" | "agent" | "sdk" | "research" など複数可"]
}

impact_level の目安:
- high: 既存コード(ebay_sync/claude_evaluator/supplier_apply等)に直接影響、破壊的変更、料金改定
- medium: 新機能で採用余地あり（MCPサーバ新設、新モデル、Agent SDK機能追加）
- low: 参考情報、研究寄り
- none: 無関係（安全性研究、政策など）

summary_ja は**記事の具体内容**を日本語化。テンプレ文言禁止。"""


def _strip_fenced_json(text: str) -> Optional[str]:
    """Claude がたまに ```json フェンスを付ける/切れて返す場合に備え、JSONらしきブロックを抽出。"""
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if fence:
        return fence.group(1)
    greedy = re.search(r'\{[\s\S]*\}', text)
    if greedy:
        return greedy.group(0)
    # 閉じ } 切れの補完
    open_brace = re.search(r'\{[\s\S]*$', text)
    if open_brace:
        return open_brace.group(0).rstrip() + "}"
    return None


def _call_claude(system: str, user: str, max_tokens: int = 600,
                 operation: str = "summarizer") -> Optional[dict]:
    if not _ANTHROPIC_OK:
        logger.warning("anthropic package not installed")
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY missing in .env")
        return None

    from monitor.api_logger import log_anthropic_response, _Timer

    client = anthropic.Anthropic()
    msg = None
    try:
        with _Timer() as t:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=[
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": user}],
            )
        log_anthropic_response(operation, MODEL, msg, duration_ms=t.duration_ms, success=True)
    except Exception as e:
        logger.warning(f"Claude API error: {e}")
        log_anthropic_response(operation, MODEL, None, duration_ms=None,
                               success=False, error_message=str(e)[:500])
        return None

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    cand = _strip_fenced_json(text)
    if not cand:
        logger.warning(f"no JSON in response: {text[:100]!r}")
        return None
    try:
        return json.loads(cand)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON decode: {e}, raw={text[:120]!r}")
        return None


def summarize_email(subject: str, sender: str, body: str) -> Optional[dict]:
    """メールを日本語で要約・分類・優先度付け。失敗時 None。"""
    user = (
        f"Subject: {subject}\n"
        f"From: {sender}\n"
        f"Body (抜粋):\n{(body or '')[:2500]}\n\n"
        f"上記を JSON で要約してください。"
    )
    return _call_claude(EMAIL_SYSTEM, user, max_tokens=700, operation="email_summary")


def summarize_news(title: str, body: str = "", source: str = "") -> Optional[dict]:
    """ニュース記事を日本語で要約＋eBay物販への影響判定。"""
    user = (
        f"Source: {source}\n"
        f"Title: {title}\n"
    )
    if body:
        user += f"Body (抜粋):\n{body[:3000]}\n"
    user += "\n上記を JSON で要約してください。"
    return _call_claude(NEWS_SYSTEM, user, max_tokens=700, operation="news_summary")
