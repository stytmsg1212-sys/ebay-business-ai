#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W223 step4: 仕入先候補の一覧テキストスキャン ranker (Haiku 4.5).

人間の探索ワークフロー (一覧をざっと見て「これは詳しく見る価値がありそう」を数件選ぶ)
を模倣し、候補一覧 (タイトル + 価格) を安価な Haiku に渡して「詳細 vision 評価すべき
index を最大 N 件」返させる。選ばれた候補だけが高価な vision 評価 (Opus/Sonnet) に
進むため、明らかな非一致への vision コストを削減する。

⚠️ 安全弁 (取りこぼし=機会損失 を防ぐ):
  - 候補数 <= max_keep のときは ranker を呼ばず全件通す (間引く意味がない)。
  - anthropic 未導入 / API キー無し / API エラー / JSON parse 失敗 = **全件通す** (fail-open)。
  - 返ってきた index は妥当性検証し、空なら全件通す。
  - 捨てた候補は logger.info で必ず可視化 (silent drop 禁止、Q0)。

最終採用判定は依然 vision 評価が行う (本 ranker は「どれを vision で精査するか」の
triage であって採用/却下の最終判定ではない)。Haiku 自体が AI のため「型番一致でも
AI スキップ禁止」制約 (= 非 AI ハードフィルタ禁止) には抵触しない。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

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
DEFAULT_MAX_KEEP = 3

RANKER_SYSTEM = """あなたは越境EC物販 (eBay 輸出) の仕入れ担当アシスタントです。
eBay で売れている商品 1 点に対し、日本のフリマ/オークションで見つかった候補商品の
一覧 (タイトル + 価格) を渡します。あなたの仕事は「同一商品の可能性が高く、詳しく
画像まで精査する価値がある候補」を最大 N 件まで index で選ぶことです。

判断指針:
- 型番 / ブランド / モデル名がタイトルで一致 or 近いものを優先。
- ジャンク/部品取り/付属品なしでも、同一商品なら候補に残す (最終判定は別工程)。
- 明らかに別商品 (別型番 / 別カテゴリ / アクセサリのみ) は選ばない。
- 迷ったら残す側に倒す (取りこぼし回避)。ただし上限 N 件は厳守。

出力は厳密な JSON のみ (```json フェンス禁止、余分なテキスト禁止):
{"keep_indices": [選んだ候補の index を昇順で、最大 N 件]}
候補が全て無関係なら {"keep_indices": []} を返す。"""


def _strip_json(text: str) -> Optional[str]:
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if fence:
        return fence.group(1)
    greedy = re.search(r'\{[\s\S]*\}', text)
    if greedy:
        return greedy.group(0)
    return None


def rank_candidates_for_vision(
    ebay_title: str,
    candidates: list[dict],
    max_keep: int = DEFAULT_MAX_KEEP,
) -> list[int]:
    """vision 精査すべき候補の index を最大 max_keep 件返す。

    Args:
        ebay_title: eBay 出品中商品のタイトル。
        candidates: [{'title': str, 'price_jpy': int|None}, ...] (順序が index)。
        max_keep: 残す最大件数 (保守値、これ以上は vision に回さない)。

    Returns:
        vision 評価に進める candidates の index list (昇順)。
        安全弁発動時 (件数<=max_keep / API 不可 / 失敗) は全 index を返す。
    """
    n = len(candidates)
    all_idx = list(range(n))
    if n <= max_keep:
        return all_idx
    if not _ANTHROPIC_OK or not os.environ.get("ANTHROPIC_API_KEY"):
        logger.info(
            f"[ranker] fail-open(API不可): 全 {n} 件を vision に通す (ebay={ebay_title[:40]!r})"
        )
        return all_idx

    lines = []
    for i, c in enumerate(candidates):
        title = (c.get("title") or "").strip() or "(タイトル不明)"
        price = c.get("price_jpy")
        price_s = f"¥{price}" if price is not None else "価格不明"
        lines.append(f"[{i}] {title} ({price_s})")
    user_text = (
        f"eBay 商品: {ebay_title}\n\n"
        f"候補一覧 (最大 {max_keep} 件を選ぶ):\n" + "\n".join(lines) +
        f"\n\n同一商品の可能性が高い候補を最大 {max_keep} 件、JSON で返してください。"
    )

    try:
        from monitor.api_logger import log_anthropic_response, _Timer
    except ImportError as e:
        logger.warning(f"[ranker] api_logger 不在, fail-open 全 {n} 件通す: {e}")
        return all_idx

    # client 構築も try 内 (コンストラクタ例外も fail-open。sweep ループ中断=機会損失を防ぐ)
    try:
        client = anthropic.Anthropic()
        with _Timer() as t:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=120,
                system=[{"type": "text", "text": RANKER_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_text}],
            )
        log_anthropic_response(
            "supplier_candidate_rank", MODEL, msg,
            duration_ms=t.duration_ms, success=True,
        )
    except Exception as e:  # noqa: BLE001 API 例外多様、fail-open
        logger.warning(f"[ranker] API error, fail-open 全 {n} 件通す: {e}")
        return all_idx

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    raw = _strip_json(text)
    if not raw:
        logger.warning(f"[ranker] JSON parse 不能, fail-open 全 {n} 件通す: {text[:120]!r}")
        return all_idx
    try:
        keep = json.loads(raw).get("keep_indices", [])
    except (ValueError, AttributeError):
        logger.warning(f"[ranker] JSON decode 失敗, fail-open 全 {n} 件通す: {raw[:120]!r}")
        return all_idx
    # keep_indices が list でない (null / 数値 / 文字列等) と下の内包表記で TypeError →
    # sweep ループ中断 (機会損失) になるため、非 list は全件 fail-open に倒す。
    if not isinstance(keep, list):
        logger.warning(f"[ranker] keep_indices が list でない, fail-open 全 {n} 件通す: {keep!r}")
        return all_idx

    # 妥当性検証: int かつ範囲内のみ、昇順 dedup、上限 max_keep。
    # `type(i) is int` で bool を除外 (Python は bool が int サブクラス → isinstance だと
    # Haiku の {"keep_indices":[true]} が index 1 扱いされ他候補が誤って ranked_out =
    # schema 崩れで候補消失。bool は弾いて valid 空 → 全件 fail-open に倒す)。
    valid = sorted({i for i in keep if type(i) is int and 0 <= i < n})[:max_keep]
    if not valid:
        # Haiku が「全て無関係」と判断 → 取りこぼし回避のため全件通す側に倒す
        logger.info(
            f"[ranker] keep 空 (全件無関係判定)、安全側で全 {n} 件 vision に通す "
            f"(ebay={ebay_title[:40]!r})"
        )
        return all_idx

    dropped = [i for i in all_idx if i not in valid]
    if dropped:
        logger.info(
            f"[ranker] vision {len(valid)}/{n} 件に絞込 (ebay={ebay_title[:40]!r}); "
            f"捨てた候補: " + "; ".join(
                f"[{i}]{(candidates[i].get('title') or '')[:50]!r}" for i in dropped
            )
        )
    return valid


__all__ = ["rank_candidates_for_vision", "DEFAULT_MAX_KEEP"]
