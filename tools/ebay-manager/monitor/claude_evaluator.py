#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仕入先候補評価モジュール（Claude API 版）

責務:
  - 「eBay出品中の商品」と「候補として見つかった仕入先商品」の
    同一性を 0-100 の match_score で評価する
  - 画像 + テキストの両方を Claude に渡し、プロンプトキャッシュで
    同一SKUの複数候補評価を低コスト化する

使い方:
    from monitor.claude_evaluator import evaluate_match
    r = evaluate_match(
        ebay_title="Sony WH-1000XM5 Black",
        candidate_title="ソニー WH-1000XM5 ブラック 美品",
        platform="mercari",
        price_jpy=32000,
        url="https://jp.mercari.com/item/mXXXX",
        ebay_image_url="https://i.ebayimg.com/.../s-l640.jpg",
        candidate_image_url="https://static.mercdn.net/item/.../1.jpg",
    )
    # r.match_score, r.reasoning
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time as _time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

# ebay-manager root (monitor/ の親) の .env を明示ロード。
# CWD に依存せず app.py / daily_scheduler.py など任意の呼び出し元から同じ結果を得るため。
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

logger = logging.getLogger(__name__)

# 2026-04-25 Opus 4.7 に切替 (深い推論で精度向上を狙う).
# モデル変遷:
#   Haiku 4.5 (~$0.007/call、user 一致率 10%) → 精度不足
#   → Opus 4.7 (~$0.04/call、深い思考) → 採用率 0.6% で過剰、月 ~$391 のコスト
#   → Sonnet 4.6 (2026-05-05〜、~$0.008/call、Opus と大差なし、user 公認の費用対効果優先).
# A/B test (W86, 5/1) では Opus / Sonnet の判定差は僅少で、コスト差を許容できないと user 判断.
# A/B 評価は supplier_candidates.eval_model で記録継続 → user 一致率追跡.
# IMPORTANT (2026-05-05 user 要望): 将来 Opus 等の高精度モデル価格が下がったら即切替検討.
#   詳細: feedback_opus_price_watch.md
# ⚠️ 訂正 (2026-05-29): 上記「月 ~$391」「コスト 5x 差」は誤算定。Opus を $15/$75 と
#   誤認していたが実際は 4.5 以降ずっと $5/$25 = Sonnet ($3/$15) の ~1.67x のみ。
#   実 Opus コストは ~$130/月 (3 倍過大だった)。Sonnet 据え置きは user 決定で有効だが、
#   再評価時は訂正値を使う (新トークナイザで Opus は同一テキスト最大 +35% token も考慮)。
CLAUDE_MODEL = "claude-sonnet-4-6"

# ────────── Tier 1 Rate Limit 保護 ──────────
# Anthropic Tier 1 制限: 50 req/min、Haiku=50K input tokens/min、Sonnet=30K。
# 2026-04-22 18:00 UTC に 160K tokens/min の burst で penalty を受けた教訓から、
# 内部で保守的な rate limit を強制する。
#
# モデル別 tokens/min 上限 (Tier 1):
_TIER1_INPUT_TOKENS_PER_MIN: dict[str, int] = {
    "claude-haiku-4-5-20251001": 50_000,
    "claude-sonnet-4-6": 30_000,
    "claude-opus-4-7": 30_000,
}
_SAFETY_FACTOR = 0.70  # 限界の 70% で運用 (30K Sonnet → 21K 目安)
_RATE_LIMIT_WINDOW_SEC = 60.0

# グローバル (プロセス内) トークン消費トラッキング
_rate_lock = threading.Lock()
_recent_calls: deque[tuple[float, int]] = deque()  # (timestamp, input_tokens)


def _rate_limit_wait(planned_input_tokens: int) -> None:
    """次の API call 予定 input tokens を渡す → 必要なら sleep して limit 内に収める."""
    model_limit = _TIER1_INPUT_TOKENS_PER_MIN.get(CLAUDE_MODEL, 30_000)
    soft_limit = int(model_limit * _SAFETY_FACTOR)

    with _rate_lock:
        now = _time.monotonic()
        # 60s より古い record を破棄
        while _recent_calls and (now - _recent_calls[0][0]) > _RATE_LIMIT_WINDOW_SEC:
            _recent_calls.popleft()

        current_sum = sum(tokens for _, tokens in _recent_calls)
        if current_sum + planned_input_tokens <= soft_limit:
            _recent_calls.append((now, planned_input_tokens))
            return

        # limit 超過 → 最古の record が window 外に出るまで待つ
        if _recent_calls:
            oldest_ts = _recent_calls[0][0]
            wait_sec = _RATE_LIMIT_WINDOW_SEC - (now - oldest_ts) + 1.0
            wait_sec = max(1.0, min(wait_sec, 60.0))
            logger.info(
                f"Claude rate limit approaching ({current_sum:,} + "
                f"{planned_input_tokens:,} > {soft_limit:,}), sleeping {wait_sec:.1f}s"
            )
        else:
            wait_sec = 2.0  # safety fallback

    _time.sleep(wait_sec)
    # 再帰で再チェック (sleep 後に新規計算)
    _rate_limit_wait(planned_input_tokens)


def _estimate_input_tokens(text_chars: int, image_count: int) -> int:
    """大まかな input tokens 推定. 1 文字 ≒ 0.5 token (日本語混在), 1 画像 ≒ 1500 token."""
    return int(text_chars * 0.5) + image_count * 1500

# ─────────────────────────────────────────────
# プロンプト（ユーザー貢献ポイント — Learning Mode）
# ─────────────────────────────────────────────
# この判定基準が、仕入先候補の採否を左右する唯一のロジックです。
# 5-10行を書き換えるだけで、採用される候補の性質が変わります。
#
# 検討材料:
#  (a) タイトル完全一致と画像一致、どちらを重視するか
#  (b) ブランド違い / カラー違い / サイズ違い / 容量違い をどう扱うか
#  (c) 付属品（箱・説明書・保証書）の欠落をどう減点するか
#  (d) 新品 / 中古 / ジャンクの状態違いをどうスコアリングするか
#  (e) 並行輸入品・海外版の扱い
#
# 下のプロンプトは「骨格のみ」です。あなたの仕入経験値を反映してください。
# 2026-05-01 W86: cache_control を機能させるため STABLE 部分を system parameter に
# 移動 + 1024 token 最低長を超えるサイズに拡張。listing_generator.py のパターン
# (system=[{cache_control: ephemeral}]) に整合。
# 旧版 STABLE_PROMPT_TEMPLATE は ebay_title を含み 619 tokens で min 未達 → cache=0。
_SYSTEM_PROMPT = """\
あなたはeBayセラーの仕入担当エキスパートです。
日本国内の仕入先 (Yahoo Auctions / Mercari / PayPay フリマ等) から、
eBay の出品商品と「仕入先として置き換え可能な完全同一物」を厳密に判定します。

【入力フォーマット】
ユーザーメッセージには以下が順に含まれます:
- 1 枚目画像: eBay 出品中の商品
- 出品中タイトル (テキスト)
- 2 枚目画像: 仕入先候補の商品
- 候補商品の情報 (タイトル / 価格 / プラットフォーム / URL)
- 過去の同一 listing 判断履歴 (あれば、参考)
- 関連動画学習知識 (あれば、参考)

【match_score の意味】
match_score は「置き換え可能な完全同一物か」の度合い (0-100 の整数)。
仕入先として採用するには「同じSKUで差し替えても顧客に影響しない」レベルが必要。

  80-100: 型番・色・容量/サイズ・付属品・状態すべて一致。差し替え可能。
  60-79 : 同一モデルで、セラーの表記揺れ（「美品/極美品」「動作確認済/新品同様」等）のみの差。
  0-59  : いずれか1つでも以下に該当 → 仕入先としては除外
            - 色違い
            - 容量・サイズ違い
            - 付属品の有無が違う（箱/説明書/ケーブル等）
            - 新品/中古/ジャンク等の状態違い
            - 別モデル/別ブランド

【「ジャンク」表記の特別判断】
候補が「ジャンク」「難あり」「動作未確認」と表記されていても：
  - セラーの文面が「動作確認が面倒でジャンク扱いにしている」ニュアンス（例:
    「動作確認していない」「通電のみ確認」「ノークレームノーリターン」のみで
    具体的な故障内容への言及なし）なら実質的に動作品の可能性が高い。
    → junk_likely_untested=true とし、reasoning にその旨を記す。
      match_score は本体が同一なら 60-79、別状態として扱う場合は 0-59。
  - 具体的な故障内容が書かれている（「液晶割れ」「電源入らない」「異音」等）
    → 本当にジャンク。junk_likely_untested=false、match_score < 40。

【別SKU出品機会の検出】
match_score < 60 で除外される候補でも、以下のケースは「別SKUとして新規出品する価値がある」
機会として拾ってください：
  - 付属品欠落版（箱なし/説明書なし）を別SKUとして出品可能
  - 色違いや容量違いを追加SKUとして拡販可能
  - 「動作未確認ジャンク」を承知で仕入れ、テスト後に出品可能
このとき alt_listing_possible=true、alt_listing_note に具体的な出品提案を記す。
（並行輸入品・海外版など判断が付きにくい場合は alt_listing_note にその旨のみ）

【ブランド・カテゴリ別の追加注意】

(1) Audio/AV ブランド (PIONEER / Audio-Technica / Sony / DENON / TEAC など)
- 年式・世代違い (例: PIONEER GM-D8 と GM-D9000) は別 SKU。型番末尾まで一致確認必須。
- ヴィンテージ機 (PIONEER Lonesome Carboy 系等) は動作確認必須、ジャンク表記は即 As-Is 扱い
  で仕入れ不可推奨。
- ヘッドホン/イヤホンは 新品/開封品/中古 で価値が大きく変わるため、状態の表記揺れに留意。

(2) 産業計測機器 (KEYENCE / Omron / Panasonic / ADVANTEST / GRAPHTEC / HIROSE / A&D など)
- センサー単体 / 本体ユニットの個別判定: KEYENCE センサー単体ならジャンク表記でも仕入れ後
  テストで B/C 判定可。
- 本体ユニット (PLC / メーター本体 / データロガー) は通電確認できないジャンクは除外推奨。
- 校正期限切れ品 (ADVANTEST 系等) は別 SKU 扱い、通常品と区別。

(3) 時計・楽器・カメラ
- ムーブメント種別 / レンズマウント / 弦数等の主要属性違いは別 SKU。
- 限定版 (Anniversary / Limited Edition / 周年モデル) は通常版と別 SKU として扱う。
- カメラ系はフィルム/デジタル区別、レンズはフランジバック (FX/DX 等) も主要属性。

(4) 並行輸入 vs 国内正規品
- 価格差を無視できない場合、alt_listing_note に明記。
- VeRO ブランド (Apple / Nintendo / SONY 等) は並行輸入で defect リスクあり、慎重判断。

【売却済み・在庫切れ判定】
候補タイトルや状態説明に以下が含まれる場合 match_score = 0 とする:
  - 「終了」「売切」「sold out」「終了しました」
  - 「在庫切れ」「out of stock」
これらは仕入先として使えないため除外。

【reasoning の書き方】
判定理由は「型番: X→Y / 色: X→Y / 状態: X→Y / 付属品: あり/なし」のような構造化形式を推奨。
1-2 文で簡潔に、決定的な差分のみ言及。判定根拠の不確実性がある場合 (画像不鮮明、情報不足等)
は reasoning に明示して match_score を中央値 (50) より低めに振る。

出力は以下のJSONのみ。コードブロック・前置き不要。
{
  "match_score": 0-100の整数,
  "reasoning": "判定理由（日本語、1-2文）",
  "junk_likely_untested": true | false,
  "alt_listing_possible": true | false,
  "alt_listing_note": "別SKU出品機会があれば具体的な提案、なければ空文字"
}
"""

# DYNAMIC_PROMPT_TEMPLATE: 候補ごとに変化 → キャッシュ対象外。ebay_title もここに含める
# (システムプロンプトを完全 stable に保つため)。
DYNAMIC_PROMPT_TEMPLATE = """\
【出品中タイトル】
{ebay_title}

【候補商品】
プラットフォーム: {platform}
タイトル: {candidate_title}
価格: {price_jpy} 円
URL: {url}
"""


@dataclass
class EvaluationResult:
    match_score: int
    reasoning: str
    junk_likely_untested: bool = False
    alt_listing_possible: bool = False
    alt_listing_note: str = ""
    error: Optional[str] = None
    cache_read: int = 0
    cache_write: int = 0


def _get_client() -> Optional[anthropic.Anthropic]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _extract_brand_tokens(title: str) -> list[str]:
    """商品タイトルから有力なブランド/型番候補を抽出。
    英大文字で始まる単語 + カタカナ語を候補とする。
    """
    import re as _re
    if not title:
        return []
    tokens = set()
    # 英大文字始まり（KEYENCE, Pioneer, Mitutoyo 等）
    for m in _re.findall(r'\b[A-Z][A-Za-z0-9-]{2,}\b', title):
        tokens.add(m)
    # カタカナ 3文字以上
    for m in _re.findall(r'[ァ-ヶー]{3,}', title):
        tokens.add(m)
    return list(tokens)


def _build_past_judgments_block(sku: Optional[str], ebay_title: str,
                                max_same_sku: int = 5, max_brand: int = 5,
                                ebay_item_id: Optional[str] = None) -> str:
    """過去のユーザー判断履歴を Few-shot 形式でプロンプト用文字列に整形。

    優先順位:
      1. 同一 listing (ebay_item_id) の accept/reject 履歴（最重要）
      2. 同じブランド/型番を含む accept/reject 履歴（ブランド別傾向学習）

    2026-05-01 W81 fix: 旧版は SKU で listing 識別 → stock:01 (61 listings 共有) で
    他 listing の判定が混入する Phase 1 学習汚染. ebay_item_id 主導に修正.
    sku 引数は brand sql2 の自己除外 (sku != ?) でのみ使用.

    なければ空文字を返す（プロンプトへの注入をスキップ）。
    """
    try:
        from monitor.database import get_conn
    except Exception:
        return ""

    brand_tokens = _extract_brand_tokens(ebay_title)
    rows_same_sku: list[dict] = []
    rows_by_brand: list[dict] = []

    try:
        with get_conn() as conn:
            # 1. 同一 listing (ebay_item_id) で accept/reject された候補
            #    auto_rejected=1 は除外 = ユーザー判断のみ
            #    SKU rule 準拠: listing 識別は ebay_item_id を canonical key として使う
            if ebay_item_id:
                sql1 = (
                    "SELECT candidate_title, match_score, status, match_reasoning, "
                    "       alt_listing_possible, junk_likely_untested "
                    "FROM supplier_candidates "
                    "WHERE ebay_item_id=? AND status IN ('accepted','applied','rejected') "
                    "  AND COALESCE(auto_rejected, 0) = 0 "
                    "ORDER BY user_action_at DESC LIMIT ?"
                )
                rows_same_sku = [dict(r) for r in conn.execute(sql1, (ebay_item_id, max_same_sku)).fetchall()]

            # 2. ブランド/型番が一致する他 listing 履歴（auto_rejected=1 除外＝ユーザー判断のみ）
            if brand_tokens:
                like_clauses = " OR ".join(["candidate_title LIKE ?" for _ in brand_tokens])
                clauses = [
                    "status IN ('accepted','applied','rejected')",
                    "COALESCE(auto_rejected, 0) = 0",
                ]
                bind: list = []
                if ebay_item_id:
                    clauses.append("ebay_item_id != ?")
                    bind.append(ebay_item_id)
                clauses.append(f"({like_clauses})")
                bind.extend([f"%{t}%" for t in brand_tokens])
                sql2 = (
                    "SELECT candidate_title, match_score, status, match_reasoning, "
                    "       alt_listing_possible, junk_likely_untested, sku "
                    "FROM supplier_candidates "
                    "WHERE " + " AND ".join(clauses) + " "
                    "ORDER BY user_action_at DESC LIMIT ?"
                )
                bind.append(max_brand)
                rows_by_brand = [dict(r) for r in conn.execute(sql2, bind).fetchall()]
    except Exception as e:
        logger.debug(f"past judgments query failed: {e}")
        return ""

    if not rows_same_sku and not rows_by_brand:
        return ""

    def _fmt(row: dict, show_sku: bool = False) -> str:
        status = row.get("status") or "?"
        status_label = {
            "accepted": "✅ 採用",
            "applied": "✅ 反映済",
            "rejected": "❌ 不採用",
        }.get(status, status)
        title = (row.get("candidate_title") or "")[:70]
        score = row.get("match_score") or 0
        flags = []
        if row.get("alt_listing_possible"):
            flags.append("alt_listing")
        if row.get("junk_likely_untested"):
            flags.append("junk_untested")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        prefix = f"[{row.get('sku','?')}] " if show_sku else ""
        reasoning = (row.get("match_reasoning") or "")[:120]
        return f"  - {prefix}\"{title}\" score={score}{flag_str} → {status_label}\n    判定理由: {reasoning}"

    lines = [
        "\n## ユーザーの過去判断履歴（この判定の参考にする）\n",
        "このユーザーは過去に以下のように候補を採用/不採用してきました。",
        "**暗黙の判断パターン**（商品特性による動作確認の必須度、junk許容度、型番厳密度など）を読み取り、",
        "今回の候補評価に反映してください。\n",
    ]
    if rows_same_sku:
        # 2026-05-01 W81: ラベルを「同一 listing」に修正 (実態 ebay_item_id 絞り込み).
        # 旧「同一SKU」表記は Claude プロンプトに誤情報を渡し、stock:01 等の
        # 在庫プール SKU で「同 SKU = 同 listing」との誤学習リスクがあった.
        lines.append("### 同一 listing (この出品) の過去履歴")
        for r in rows_same_sku:
            lines.append(_fmt(r, show_sku=False))
        lines.append("")
    if rows_by_brand:
        lines.append("### 同じブランド/型番を含む他SKUの履歴")
        for r in rows_by_brand:
            lines.append(_fmt(r, show_sku=True))
        lines.append("")
    lines.append(
        "**注意**: 上記はユーザー個別の判断。一般論より **このユーザーの傾向** を優先して match_score を決定してください。"
    )
    return "\n".join(lines)


def evaluate_match(
    ebay_title: str,
    candidate_title: str,
    platform: str,
    price_jpy: Optional[int],
    url: str,
    ebay_image_url: Optional[str] = None,
    candidate_image_url: Optional[str] = None,
    sku: Optional[str] = None,
    ebay_item_id: Optional[str] = None,
    model: Optional[str] = None,
    test_mode: bool = False,
) -> EvaluationResult:
    """
    Claude API で仕入先候補の一致度を評価。

    STABLE部分(ebay_image + 判定基準)にプロンプトキャッシュを効かせる。
    同一SKUのN候補を評価する場合、2件目以降は cache hit でコスト約1/4。

    Phase 1 学習: sku を渡すと過去の accept/reject 判断履歴をプロンプトに注入し、
    ユーザー個別の判断パターンを反映させる。

    API未設定・失敗時は match_score=0 を返し、error にメッセージを格納。
    """
    client = _get_client()
    if not client:
        return EvaluationResult(
            match_score=0,
            reasoning="ANTHROPIC_API_KEY 未設定",
            error="ANTHROPIC_API_KEY not set",
        )

    dynamic_text = DYNAMIC_PROMPT_TEMPLATE.format(
        ebay_title=ebay_title,
        platform=platform,
        candidate_title=candidate_title or "(不明)",
        price_jpy=price_jpy if price_jpy is not None else "不明",
        url=url,
    )

    # Phase 1 学習: 過去のユーザー判断を Few-shot 形式で注入
    # （これにより「PIONEER Lonesome Carboy はジャンク不可」等の暗黙知を学習）
    # 2026-05-01 W81: ebay_item_id 主導 (旧 sku ベースは stock:01 等で他 listing 判定混入)
    # 2026-05-01 W86: test_mode=True で A/B test 用に bypass (model 間で知識共有しない)
    if test_mode:
        past_judgments_block = ""
    else:
        past_judgments_block = _build_past_judgments_block(sku, ebay_title, ebay_item_id=ebay_item_id)

    # 学習済動画からの関連知識を注入（マッチすれば評価精度UP）
    knowledge_block = ""
    if not test_mode:
        try:
            from monitor.knowledge_lookup import find_related_knowledge, format_knowledge_for_prompt
            _related = find_related_knowledge(
                f"{ebay_title} {candidate_title or ''}", max_videos=2,
            )
            if _related:
                knowledge_block = "\n\n" + format_knowledge_for_prompt(_related, max_chars=1500)
                logger.debug(f"KB matched {len(_related)} videos for evaluation")
        except Exception as e:
            logger.debug(f"knowledge lookup skipped: {e}")

    # 2026-05-01 W86: STABLE 部分は system parameter に移動 (cache_control 機能化)
    # user content には dynamic 要素のみ: 画像 2 枚 + dynamic_text + past_judgments + knowledge
    content: list[dict] = []
    if ebay_image_url:
        content.append({
            "type": "image",
            "source": {"type": "url", "url": ebay_image_url},
        })
    if candidate_image_url:
        content.append({
            "type": "image",
            "source": {"type": "url", "url": candidate_image_url},
        })
    full_dynamic = dynamic_text + past_judgments_block + knowledge_block
    content.append({"type": "text", "text": full_dynamic})

    from monitor.api_logger import log_anthropic_response, _Timer
    msg = None

    # レート制限保護: Tier 1 の tokens/min 上限に達する前に sleep
    # _SYSTEM_PROMPT + full_dynamic の char 数 + 画像 2 枚想定で概算
    _planned_tokens = _estimate_input_tokens(
        text_chars=len(_SYSTEM_PROMPT) + len(full_dynamic),
        image_count=(1 if ebay_image_url else 0) + (1 if candidate_image_url else 0),
    )
    _rate_limit_wait(_planned_tokens)

    # 2026-05-01 W86: model override (A/B test 用)。指定なければ CLAUDE_MODEL.
    _model_used = model or CLAUDE_MODEL
    try:
        with _Timer() as _t:
            msg = client.messages.create(
                model=_model_used,
                max_tokens=800,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": content}],
            )
        log_anthropic_response("candidate_evaluate", _model_used, msg,
                               duration_ms=_t.duration_ms, success=True)
    except anthropic.APIError as e:
        logger.warning(f"Claude API error: {e}")
        log_anthropic_response("candidate_evaluate", _model_used, None,
                               success=False, error_message=str(e)[:500])
        return EvaluationResult(match_score=0, reasoning="API error", error=str(e))
    except Exception as e:
        logger.warning(f"evaluate_match unexpected: {e}")
        log_anthropic_response("candidate_evaluate", _model_used, None,
                               success=False, error_message=str(e)[:500])
        return EvaluationResult(match_score=0, reasoning="unknown error", error=str(e))

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    result = _parse_response(text)
    usage = msg.usage
    result.cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    result.cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return result


def _parse_response(text: str) -> EvaluationResult:
    """Claude 出力から {match_score, reasoning} を抽出。

    Claude は ```json ...``` フェンスや素 JSON を混在で返す。かつ max_tokens 不足で
    閉じ `}` が欠けるケースがあるため、貪欲抽出→フォールバック順で堅牢に処理する。
    """
    # Step 1: ```json ... ``` フェンスがあれば中身優先
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text or "")
    candidates: list[str] = []
    if fence:
        candidates.append(fence.group(1))

    # Step 2: 最初の { から最後の } まで（貪欲）
    greedy = re.search(r'\{[\s\S]*\}', text or "")
    if greedy:
        candidates.append(greedy.group(0))

    # Step 3: 閉じ } が無く切れているケース → 末尾に } を補って再試行
    if greedy is None:
        open_brace = re.search(r'\{[\s\S]*$', text or "")
        if open_brace:
            candidates.append(open_brace.group(0).rstrip() + "}")

    last_err: Optional[str] = None
    for cand in candidates:
        try:
            data = json.loads(cand)
            break
        except json.JSONDecodeError as e:
            last_err = str(e)
            continue
    else:
        return EvaluationResult(
            match_score=0,
            reasoning=(text or "")[:120],
            error=last_err and f"JSON decode: {last_err}" or "no JSON in response",
        )

    raw_score = data.get("match_score", 0)
    try:
        score = int(raw_score)
    except (ValueError, TypeError):
        score = 0
    score = max(0, min(100, score))
    reasoning = str(data.get("reasoning", ""))[:500]
    return EvaluationResult(
        match_score=score,
        reasoning=reasoning,
        junk_likely_untested=bool(data.get("junk_likely_untested", False)),
        alt_listing_possible=bool(data.get("alt_listing_possible", False)),
        alt_listing_note=str(data.get("alt_listing_note", ""))[:500],
    )
