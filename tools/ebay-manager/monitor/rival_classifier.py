#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W301 AI 店長 Phase1 S2: 競合分類エンジン (ハード除外 → スコア足切り → グレーのみ Claude 判定).

設計書: .company/engineering/docs/2026-06-24-ai-manager-phase1-design.md §5 (rival_classifier.py /
  rival_ai_judge.py コンポーネント設計)。本タスク指示により両者を本ファイル 1 本に統合実装
  (設計書は S4 で rival_ai_judge.py を別ファイル分割予定だったが、依頼された実装スコープが
  「monitor/rival_classifier.py 新規 + 必要なら小さな helper」1 本のみのため統合。設計書との
  差異として報告に明記)。
議事録: .company/engineering/docs/2026-06-22-ai-manager-hearing-minutes.md §3 (門前払いフィルタ) /
  §13.1 (variant_risk 3 トラップ)。

責務:
  1. ハード除外 (国≠JP確定 / 自社動作品×相手JUNK・AS-IS / 売切れ / DDU ブラックリスト)
     → AI を呼ばず noise 直行 (exclude_reason 必須)。評価稼ぎ farmer は Phase1 は
     snapshot 未取得のため review へ (安全弁、設計書 §5 L27)。
  2. スコア足切り (タイトル類似度 Jaccard + 型番一致ブースト / 価格比) で明白な noise を弾く。
  3. グレーのみ Claude (Haiku 4.5) で商品同一性 (same_product) / variant_risk / condition /
     confidence を判定 (構造化 JSON、パース失敗は fail-closed で review)。
  4. 3 分岐 (real / review / noise) を confidence 閾値で決定。
  5. Shadow: shadow_mode=True (Phase1 固定) では pricing_eligible を一切立てない。
     would_be_eligible にのみ「real 相当だったら 1」を記録する。本モジュールは
     competitor_products テーブルに一切書き込まない (pricing_eligible=1 になる経路を
     作らない、設計書 L63 逐語)。
  6. fail-closed: AI error / JSON パース失敗 / API key 未設定 / max_ai_calls_per_run
     超過は全て review + 痕跡 (Q0 silent-skip-prevention)。
  7. 警告ブランド watchlist 一致は分類結果の reason に flag するのみ (アクションは Phase2)。
  8. 全判定を rival_classifications に INSERT (listing 識別は ebay_item_id /
     competitor_item_id、SKU は使わない。sku-rules.md 準拠)。

SKU 規約: 本モジュールは SKU を一切参照しない (listing 識別は ebay_item_id /
  competitor_item_id のみ)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time as _time
from dataclasses import dataclass, replace
from typing import Optional

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────
# 定数 (design doc §5: 「閾値はconfigから(ハードコード禁止)」→ 本モジュールでは
# 名前付き定数 + thresholds dict で一元管理、呼出側/テストから override 可能にする。
# 数値は user 承認済み確定値 (real/review confidence 境界のみ、task 指示で明示) と、
# それ以外は初期値 (Shadow 実測で調整前提、設計書 §11 Q3 と同じ位置づけ)。
# ────────────────────────────────────────────────────────────────

# 3 分岐の confidence 境界 (task 指示で明示、user 承認済み確定値)。
AI_CONFIDENCE_REAL_MIN = 0.85
AI_CONFIDENCE_REVIEW_MIN = 0.6

# ⚠️ 以下は初期値 (Shadow 実測後に調整前提、設計書 §11 Q3 未確定と同じ位置づけの assumption)。
FARMER_FEEDBACK_SCORE_MAX = 50       # 評価数がこれ以下 = 弱小セラーの可能性
FARMER_PRICE_RATIO_MAX = 0.5         # 自社価格比でこれ未満 = 赤字的極端安値
SCORE_NOISE_SIMILARITY_MAX = 0.15    # タイトル類似度がこれ未満 = 明白な別商品
SCORE_NOISE_PRICE_RATIO_MIN = 0.2    # 価格比がこの範囲外 = 明白な価格乖離
SCORE_NOISE_PRICE_RATIO_MAX = 5.0
DEFAULT_MAX_AI_CALLS_PER_RUN = 50    # 1 run (task_rival_classify 1 回分) の AI 呼出上限

DEFAULT_THRESHOLDS: dict = {
    "ai_confidence_real_min": AI_CONFIDENCE_REAL_MIN,
    "ai_confidence_review_min": AI_CONFIDENCE_REVIEW_MIN,
    "farmer_feedback_score_max": FARMER_FEEDBACK_SCORE_MAX,
    "farmer_price_ratio_max": FARMER_PRICE_RATIO_MAX,
    "score_noise_similarity_max": SCORE_NOISE_SIMILARITY_MAX,
    "score_noise_price_ratio_min": SCORE_NOISE_PRICE_RATIO_MIN,
    "score_noise_price_ratio_max": SCORE_NOISE_PRICE_RATIO_MAX,
    "max_ai_calls_per_run": DEFAULT_MAX_AI_CALLS_PER_RUN,
}

# 状態キーワード (議事録§3: eBay の Condition 欄は見ず、タイトル文字列の NEW/USED/AS-IS/JUNK
# で判定。競合 listing は自社 listing と異なり画像確認ステップが無い Phase1 のため
# タイトル文字列のみで判定する)。
_JUNK_KEYWORDS = ("JUNK", "ジャンク", "AS-IS", "AS IS", "現状渡し", "部品取り")
_SOLD_OUT_KEYWORDS = ("SOLD OUT", "売り切れ", "売切れ", "終了しました", "在庫切れ")
_NON_WORKING_RANKS = {"PO", "AS-IS"}  # CLAUDE.md コンディションランク 8 段階 (tools/ebay-manager/CLAUDE.md)

# 判定モデル (設計書 §5: グレーのみ Claude Haiku、コスト ≈$0.007/件)。
HAIKU_MODEL = "claude-haiku-4-5-20251001"


# ────────────────────────────────────────────────────────────────
# データ構造
# ────────────────────────────────────────────────────────────────

@dataclass
class ClassifyResult:
    """1 件の分類結果 (rival_classifications 1 行に対応)."""
    classification: str  # 'real' | 'noise' | 'review'
    route: str            # hard_exclude / farmer_safety_valve / score / ai / ai_cap_exceeded / ai_error / ai_parse_error / ai_key_missing
    exclude_reason: Optional[str] = None
    title_similarity: Optional[float] = None
    price_ratio: Optional[float] = None
    needs_ai: bool = False
    same_product: Optional[bool] = None
    variant_risk: Optional[str] = None
    ai_condition: Optional[str] = None
    confidence: Optional[float] = None
    reason: str = ""
    ai_model: Optional[str] = None
    warning_brand_flag: Optional[str] = None


@dataclass
class AIJudgeResult:
    """judge_rival() の戻り値 (Claude 構造化 JSON 由来)."""
    same_product: Optional[bool] = None
    variant_risk: Optional[str] = None   # none/voltage/cable/language/accessory/unknown
    condition: Optional[str] = None      # NEW/USED/AS-IS/JUNK
    confidence: Optional[float] = None
    reason: str = ""
    ai_model: Optional[str] = None
    error: Optional[str] = None
    route: str = "ai"  # 'ai' | 'ai_error' | 'ai_parse_error' | 'ai_key_missing'


# ────────────────────────────────────────────────────────────────
# タイトル類似度 (Jaccard + 型番一致ブースト、embedding 不使用 = K1)
# ────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
# 型番らしきトークン (英字+数字混在。例: WH-1000XM5 / GM-D8)。
_MODEL_TOKEN_RE = re.compile(
    r"\b(?:[A-Za-z]+[0-9][A-Za-z0-9\-]*|[0-9]+[A-Za-z][A-Za-z0-9\-]*)\b"
)


def _tokenize(text: str) -> set:
    if not text:
        return set()
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _model_tokens(text: str) -> set:
    if not text:
        return set()
    return {t.upper() for t in _MODEL_TOKEN_RE.findall(text)}


def compute_title_similarity(title_a: Optional[str], title_b: Optional[str]) -> Optional[float]:
    """Jaccard 類似度 (0.0-1.0)。型番トークンが両者に共通して存在すれば 0.9 に底上げ。

    どちらかのタイトルが欠落している場合は None (判定不能、noise 確定に使わない)。
    """
    if not title_a or not title_b:
        return None
    tokens_a = _tokenize(title_a)
    tokens_b = _tokenize(title_b)
    if not tokens_a or not tokens_b:
        return None
    union = tokens_a | tokens_b
    intersection = tokens_a & tokens_b
    jaccard = len(intersection) / len(union) if union else 0.0

    common_model_tokens = _model_tokens(title_a) & _model_tokens(title_b)
    if common_model_tokens:
        jaccard = max(jaccard, 0.9)
    return round(jaccard, 4)


def compute_price_ratio(our_price_usd: Optional[float],
                         competitor_price_usd: Optional[float]) -> Optional[float]:
    """competitor / our の価格比。どちらか欠落 or our<=0 なら None."""
    if our_price_usd is None or competitor_price_usd is None:
        return None
    if our_price_usd <= 0:
        return None
    return round(competitor_price_usd / our_price_usd, 4)


def _contains_keyword(text: Optional[str], keywords: tuple) -> bool:
    if not text:
        return False
    upper = text.upper()
    return any(kw.upper() in upper for kw in keywords)


def _matches_warning_brand(our_title: Optional[str], competitor_title: Optional[str],
                            warning_brands) -> Optional[str]:
    """our/competitor タイトルに warning_brand_watchlist ブランド名が含まれるか (大小無視)."""
    if not warning_brands:
        return None
    haystack = f"{our_title or ''} {competitor_title or ''}".upper()
    for brand in warning_brands:
        if brand and brand.upper() in haystack:
            return brand
    return None


# ────────────────────────────────────────────────────────────────
# ハード除外 + スコア足切り (純ロジック、AI 不使用)
# ────────────────────────────────────────────────────────────────

def classify_discovery(
    signals: dict,
    dou_blacklist=frozenset(),
    warning_brands=frozenset(),
    thresholds: Optional[dict] = None,
    self_item_ids=frozenset(),
) -> ClassifyResult:
    """ハード除外 → スコア足切りのみを行う純ロジック (AI 不使用)。

    signals の想定キー (全て Optional、無ければ該当チェックを保守的にスキップ):
      ebay_item_id, competitor_item_id (識別、呼出側で必須付与)
      our_title, competitor_title
      our_price_usd, competitor_price_usd
      our_rank (CLAUDE.md 8 段階: N/S/A/B/C/D/PO/As-Is)
      competitor_seller (str, DDU blacklist 照合用)
      competitor_country (str, "JP" 以外で確定除外。None/空 = 不明 = 除外しない)
      competitor_seller_feedback_score (int, farmer 判定用。Phase1 は取得元未実装のため
        通常 None、Phase2 GetItem snapshot 導入後に供給される想定)
      is_sold_out (bool)

    dou_blacklist: DDU セラー ID の集合 (ddu_sellers テーブルから呼出側が読んで渡す)。
    warning_brands: warning_brand_watchlist のブランド名集合 (呼出側が読んで渡す)。
    thresholds: DEFAULT_THRESHOLDS を上書きする dict (省略時デフォルト)。
    self_item_ids: 自社 ebay_listings.ebay_item_id の集合 (W308: 自己マッチ遮断用、
      呼出側が `monitor.database.get_self_ebay_item_ids()` で読んで渡す)。

    戻り値の classification は 'noise' (hard-exclude/score 起因) / 'review'
    (farmer safety valve) / None 相当 (needs_ai=True、呼出側が AI 判定へ進める)。
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    our_title = signals.get("our_title")
    competitor_title = signals.get("competitor_title")
    our_price = signals.get("our_price_usd")
    competitor_price = signals.get("competitor_price_usd")

    title_similarity = compute_title_similarity(our_title, competitor_title)
    price_ratio = compute_price_ratio(our_price, competitor_price)

    warning_brand_flag = _matches_warning_brand(our_title, competitor_title, warning_brands)

    # 0. 自社出品との自己マッチ (W308): competitor_item_id が自社 ebay_listings に
    #    実在する = 100% 自社出品 (同一 listing が Browse API 検索結果に自社の
    #    出品として混入したケース)。セラー名 (competitor_seller) には依存しない
    #    decisive 判定 — セラー名照合は表記揺れ / config 未設定で機能しないリスクが
    #    あるが、item_id 一致は確実 (K0: 実コード調査で config['ebay']['seller_id']
    #    が本番未設定と判明、既存の task_rival_detection.py セラー名除外が無力化
    #    していたことが 77 件混入の根本原因)。国判定より前 = 最優先で弾く。
    competitor_item_id = signals.get("competitor_item_id")
    if competitor_item_id and competitor_item_id in self_item_ids:
        return ClassifyResult(
            classification="noise", route="hard_exclude",
            exclude_reason="self_listing",
            title_similarity=title_similarity, price_ratio=price_ratio,
            reason=(
                f"competitor_item_id が自社 ebay_listings と一致 "
                f"(self match, item_id={competitor_item_id})"
            ),
            warning_brand_flag=warning_brand_flag,
        )

    # 1. 国 ≠ JP 確定 (不明は除外しない = 保守的)
    country = (signals.get("competitor_country") or "").strip().upper()
    if country and country != "JP":
        return ClassifyResult(
            classification="noise", route="hard_exclude",
            exclude_reason="country_not_jp",
            title_similarity=title_similarity, price_ratio=price_ratio,
            reason=f"出品国が JP 以外と確定 (country={country})",
            warning_brand_flag=warning_brand_flag,
        )

    # 2. 売切れ (明示フラグ優先、無ければタイトルキーワードで判定)
    is_sold_out = signals.get("is_sold_out")
    if is_sold_out is None:
        is_sold_out = _contains_keyword(competitor_title, _SOLD_OUT_KEYWORDS)
    if is_sold_out:
        return ClassifyResult(
            classification="noise", route="hard_exclude",
            exclude_reason="sold_out",
            title_similarity=title_similarity, price_ratio=price_ratio,
            reason="競合が売切れ",
            warning_brand_flag=warning_brand_flag,
        )

    # 3. 自社が動作品 (workable rank) なのに相手が JUNK/AS-IS 表記
    our_rank = signals.get("our_rank")
    our_workable = bool(our_rank) and str(our_rank).strip().upper() not in _NON_WORKING_RANKS
    if our_workable and _contains_keyword(competitor_title, _JUNK_KEYWORDS):
        return ClassifyResult(
            classification="noise", route="hard_exclude",
            exclude_reason="competitor_junk_vs_our_working",
            title_similarity=title_similarity, price_ratio=price_ratio,
            reason=f"自社は動作品 (rank={our_rank}) だが競合は JUNK/AS-IS 表記",
            warning_brand_flag=warning_brand_flag,
        )

    # 4. DDU セラーブラックリスト一致
    competitor_seller = signals.get("competitor_seller")
    if competitor_seller and competitor_seller in dou_blacklist:
        return ClassifyResult(
            classification="noise", route="hard_exclude",
            exclude_reason="ddu_blacklist_seller",
            title_similarity=title_similarity, price_ratio=price_ratio,
            reason=f"DDU セラーブラックリスト一致 (seller={competitor_seller})",
            warning_brand_flag=warning_brand_flag,
        )

    # 5. 評価稼ぎ farmer 安全弁: Phase1 は実売 snapshot 未取得のため noise ではなく
    #    review へ (真ライバルを誤って捨てない、設計書 §5 L27)。
    feedback_score = signals.get("competitor_seller_feedback_score")
    if (feedback_score is not None and price_ratio is not None
            and feedback_score <= th["farmer_feedback_score_max"]
            and price_ratio <= th["farmer_price_ratio_max"]):
        return ClassifyResult(
            classification="review", route="farmer_safety_valve",
            title_similarity=title_similarity, price_ratio=price_ratio,
            reason=(
                f"評価稼ぎ farmer 疑い (feedback_score={feedback_score}, "
                f"price_ratio={price_ratio}) — Phase1 は実売 snapshot 未取得のため "
                f"保守的に review へ (真ライバルを捨てない安全弁)"
            ),
            warning_brand_flag=warning_brand_flag,
        )

    # 6. スコア足切り: タイトル類似度が極端に低い、または価格比が極端な外れ値 →
    #    明白な noise (それ以外は AI 判定が必要な「グレー」)。
    if title_similarity is not None and title_similarity < th["score_noise_similarity_max"]:
        return ClassifyResult(
            classification="noise", route="score",
            exclude_reason="score_low_similarity",
            title_similarity=title_similarity, price_ratio=price_ratio,
            reason=f"タイトル類似度が閾値未満 ({title_similarity} < {th['score_noise_similarity_max']})",
            warning_brand_flag=warning_brand_flag,
        )
    if price_ratio is not None and not (
        th["score_noise_price_ratio_min"] <= price_ratio <= th["score_noise_price_ratio_max"]
    ):
        return ClassifyResult(
            classification="noise", route="score",
            exclude_reason="score_price_outlier",
            title_similarity=title_similarity, price_ratio=price_ratio,
            reason=f"価格比が正常範囲外 (price_ratio={price_ratio})",
            warning_brand_flag=warning_brand_flag,
        )

    # ここまでで確定しない = グレー → AI 判定が必要
    return ClassifyResult(
        classification="review", route="pending_ai",
        title_similarity=title_similarity, price_ratio=price_ratio,
        needs_ai=True,
        warning_brand_flag=warning_brand_flag,
    )


# ────────────────────────────────────────────────────────────────
# グレーのみ Claude (Haiku) 判定
# ────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
あなたは eBay セラーのライバル分析エキスパートです。
自社の eBay 出品と、検索結果で見つかった競合セラーの出品が「同一商品か」を判定します。

【重要】eBay の Condition (コンディション) 欄は見ません。判定は必ずタイトル文字列
(NEW / USED / AS-IS / JUNK 等の表記) のみで行ってください。

【見抜きにくい仕様差 (variant_risk)】
タイトルだけでは判別できない可能性がある仕様差を疑ってください:
  - voltage: 電圧切替スイッチの有無 (海外向け/国内向けの違い)
  - cable: 付属ケーブルの長さ違い
  - language: 言語/地域版の違い (日本語版 vs 英語版など、メニュー言語が変わらない機種もある)
  - accessory: 付属品の有無違い
  - none: 上記の懸念が無い
  - unknown: 情報不足で判断できない

出力は以下の JSON のみ。コードブロック・前置き不要。
{
  "same_product": true | false,
  "variant_risk": "none" | "voltage" | "cable" | "language" | "accessory" | "unknown",
  "condition": "NEW" | "USED" | "AS-IS" | "JUNK",
  "confidence": 0.0-1.0の小数 (この判定にどれだけ自信があるか),
  "reason": "判定理由（日本語、1-2文）"
}
"""

_DYNAMIC_TEMPLATE = """\
【自社出品タイトル】
{our_title}
価格: {our_price}

【競合出品タイトル】
{competitor_title}
価格: {competitor_price}
"""

_VALID_VARIANT_RISK = {"none", "voltage", "cable", "language", "accessory", "unknown"}
_VALID_CONDITION = {"NEW", "USED", "AS-IS", "JUNK"}


def _get_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key)


def _parse_ai_json(text: str) -> Optional[dict]:
    """堅牢 JSON 抽出 (claude_evaluator._parse_response と同型: fence → greedy → 補完)."""
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text or "")
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    greedy = re.search(r'\{[\s\S]*\}', text or "")
    if greedy:
        candidates.append(greedy.group(0))
    if greedy is None:
        open_brace = re.search(r'\{[\s\S]*$', text or "")
        if open_brace:
            candidates.append(open_brace.group(0).rstrip() + "}")
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


def judge_rival(signals: dict, model: str = HAIKU_MODEL) -> AIJudgeResult:
    """グレー判定を Claude Haiku に判定させる。fail-closed (エラー時は route で明示)。

    Haiku 4.5 は effort 非対応 (claude_evaluator._EFFORT_SUPPORTED_MODELS 参照) の
    ため output_config は付与しない。
    """
    client = _get_client()
    if client is None:
        return AIJudgeResult(
            error="ANTHROPIC_API_KEY not set",
            reason="ANTHROPIC_API_KEY 未設定",
            route="ai_key_missing",
        )

    our_title = signals.get("our_title") or "(不明)"
    competitor_title = signals.get("competitor_title") or "(不明)"
    our_price = signals.get("our_price_usd")
    competitor_price = signals.get("competitor_price_usd")
    user_text = _DYNAMIC_TEMPLATE.format(
        our_title=our_title,
        our_price=f"${our_price:.2f}" if our_price is not None else "不明",
        competitor_title=competitor_title,
        competitor_price=f"${competitor_price:.2f}" if competitor_price is not None else "不明",
    )

    from monitor.api_logger import log_anthropic_response, _Timer

    import anthropic
    try:
        with _Timer() as t:
            msg = client.messages.create(
                model=model,
                max_tokens=500,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_text}],
            )
        log_anthropic_response("rival_ai_judge", model, msg,
                                duration_ms=t.duration_ms, success=True)
    except anthropic.APIError as e:
        logger.warning(f"judge_rival API error: {e}")
        log_anthropic_response("rival_ai_judge", model, None,
                                success=False, error_message=str(e)[:500])
        return AIJudgeResult(
            error=str(e), reason=f"AI 呼出エラー: {e}"[:300],
            ai_model=model, route="ai_error",
        )
    except Exception as e:
        logger.warning(f"judge_rival unexpected error: {e}")
        log_anthropic_response("rival_ai_judge", model, None,
                                success=False, error_message=str(e)[:500])
        return AIJudgeResult(
            error=str(e), reason=f"AI 呼出エラー (unexpected): {e}"[:300],
            ai_model=model, route="ai_error",
        )

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    data = _parse_ai_json(text)
    if data is None:
        return AIJudgeResult(
            error="json parse failed",
            reason=f"AI 応答の JSON パース失敗: {(text or '')[:200]}",
            ai_model=model, route="ai_parse_error",
        )

    # same_product / confidence は 3 分岐の根幹。型不正・欠落は fail-closed.
    raw_same_product = data.get("same_product")
    raw_confidence = data.get("confidence")
    if not isinstance(raw_same_product, bool) or not isinstance(raw_confidence, (int, float)):
        return AIJudgeResult(
            error="missing or invalid same_product/confidence",
            reason=f"AI 応答に same_product/confidence が不足または型不正: {data}"[:300],
            ai_model=model, route="ai_parse_error",
        )
    confidence = max(0.0, min(1.0, float(raw_confidence)))

    variant_risk = data.get("variant_risk")
    if variant_risk not in _VALID_VARIANT_RISK:
        variant_risk = "unknown"
    condition = data.get("condition")
    if condition not in _VALID_CONDITION:
        condition = None
    reason = str(data.get("reason", ""))[:500]

    return AIJudgeResult(
        same_product=raw_same_product,
        variant_risk=variant_risk,
        condition=condition,
        confidence=confidence,
        reason=reason,
        ai_model=model,
        route="ai",
    )


def _apply_confidence_branch(same_product: Optional[bool], confidence: Optional[float],
                              th: dict) -> str:
    """3 分岐ロジック (same_product=True かつ confidence>=0.85 → real /
    0.6-0.85 → review / それ未満 or same_product=False → noise)."""
    if same_product is True:
        if confidence is not None and confidence >= th["ai_confidence_real_min"]:
            return "real"
        if confidence is not None and confidence >= th["ai_confidence_review_min"]:
            return "review"
        return "noise"
    if same_product is False:
        return "noise"
    # same_product が None (通常は起きない、fail-closed 経路は judge_rival 側で処理済)
    return "review"


def _merge_ai_result(pre: ClassifyResult, ai: AIJudgeResult, th: dict) -> ClassifyResult:
    if ai.error is not None:
        # fail-closed: AI error / パース失敗 / API key 未設定 → 必ず review
        return replace(
            pre,
            classification="review",
            route=ai.route,
            reason=ai.reason,
            ai_model=ai.ai_model,
            same_product=None,
            variant_risk=None,
            ai_condition=None,
            confidence=None,
            needs_ai=False,
        )
    classification = _apply_confidence_branch(ai.same_product, ai.confidence, th)
    return replace(
        pre,
        classification=classification,
        route="ai",
        reason=ai.reason,
        ai_model=ai.ai_model,
        same_product=ai.same_product,
        variant_risk=ai.variant_risk,
        ai_condition=ai.condition,
        confidence=ai.confidence,
        needs_ai=False,
    )


# ────────────────────────────────────────────────────────────────
# オーケストレーション (hard-exclude/score → 必要なら AI → 3分岐 → 保存)
# ────────────────────────────────────────────────────────────────

def classify_rival(
    signals: dict,
    *,
    dou_blacklist=frozenset(),
    warning_brands=frozenset(),
    thresholds: Optional[dict] = None,
    ai_calls_used: int = 0,
    discovery_id: Optional[int] = None,
    shadow_mode: bool = True,
    persist: bool = True,
    self_item_ids=frozenset(),
) -> ClassifyResult:
    """1 件分を分類し (必要なら Claude 判定)、persist=True なら rival_classifications へ保存。

    ai_calls_used: この run (1 回の task_rival_classify 実行) で既に消費した AI 呼出数。
      呼出側 (classify_batch や task_rival_classify.py) が管理して渡す。
      max_ai_calls_per_run に達している場合は AI を呼ばず review + 痕跡 (Q0)。
    self_item_ids: W308 自己マッチ遮断用 (classify_discovery へ透過)。
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    pre = classify_discovery(signals, dou_blacklist, warning_brands, th, self_item_ids=self_item_ids)

    if not pre.needs_ai:
        result = pre
    elif ai_calls_used >= th["max_ai_calls_per_run"]:
        result = replace(
            pre,
            classification="review",
            route="ai_cap_exceeded",
            reason=(
                f"max_ai_calls_per_run={th['max_ai_calls_per_run']} 超過のため "
                f"AI 判定をスキップ (review へ、Q0 痕跡)"
            ),
            needs_ai=False,
        )
        logger.warning(
            f"[rival_classifier] AI cap 超過: ebay_item_id="
            f"{signals.get('ebay_item_id')} competitor_item_id="
            f"{signals.get('competitor_item_id')} → review"
        )
    else:
        ai = judge_rival(signals)
        result = _merge_ai_result(pre, ai, th)

    if pre.warning_brand_flag:
        flag_suffix = f" [warning_brand:{pre.warning_brand_flag}]"
        result = replace(result, reason=(result.reason or "") + flag_suffix)

    if persist:
        save_rival_classification(
            result,
            discovery_id=discovery_id,
            ebay_item_id=signals.get("ebay_item_id"),
            competitor_item_id=signals.get("competitor_item_id"),
            shadow_mode=shadow_mode,
        )
    return result


def classify_batch(
    discoveries: list,
    *,
    dou_blacklist=frozenset(),
    warning_brands=frozenset(),
    thresholds: Optional[dict] = None,
    shadow_mode: bool = True,
    persist: bool = True,
    self_item_ids=frozenset(),
) -> list:
    """複数件を 1 run として分類。AI 呼出数を run 全体で累積カウントし cap を適用する。

    discoveries: 各要素は signals dict に加えて discovery_id キーを含めてよい
      (例: {"discovery_id": 1, "ebay_item_id": ..., ...})。
    self_item_ids: W308 自己マッチ遮断用 (classify_rival へ透過)。
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    results = []
    ai_calls_used = 0
    _ai_attempt_routes = ("ai", "ai_error", "ai_parse_error", "ai_key_missing")
    for signals in discoveries:
        discovery_id = signals.get("discovery_id")
        result = classify_rival(
            signals,
            dou_blacklist=dou_blacklist,
            warning_brands=warning_brands,
            thresholds=th,
            ai_calls_used=ai_calls_used,
            discovery_id=discovery_id,
            shadow_mode=shadow_mode,
            persist=persist,
            self_item_ids=self_item_ids,
        )
        if result.route in _ai_attempt_routes:
            ai_calls_used += 1
        results.append(result)
    return results


# ────────────────────────────────────────────────────────────────
# 永続化 (rival_classifications への INSERT。SKU 不使用)
# ────────────────────────────────────────────────────────────────

def save_rival_classification(
    result: ClassifyResult,
    *,
    discovery_id: Optional[int],
    ebay_item_id: Optional[str],
    competitor_item_id: Optional[str],
    shadow_mode: bool = True,
) -> int:
    """1 件の分類結果を rival_classifications に INSERT する。

    would_be_eligible: classification=='real' の場合のみ 1 (Shadow 時、本番切替時の
    pricing_eligible=1 相当を「もし立てたら」で記録するだけ。本関数は
    competitor_products には一切書き込まない = pricing_eligible を変更する経路を
    作らない (設計書 L63 逐語遵守)。

    created_at は SQLite の CURRENT_TIMESTAMP default (UTC 保存、sqlite-timezone.md 準拠)。
    """
    from monitor.database import get_conn

    would_be_eligible = 1 if result.classification == "real" else 0
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO rival_classifications
               (discovery_id, ebay_item_id, competitor_item_id, classification,
                route, exclude_reason, title_similarity, price_ratio,
                same_product, variant_risk, ai_condition, confidence, reason,
                ai_model, shadow_mode, would_be_eligible)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                discovery_id, ebay_item_id, competitor_item_id,
                result.classification, result.route, result.exclude_reason,
                result.title_similarity, result.price_ratio,
                None if result.same_product is None else int(result.same_product),
                result.variant_risk, result.ai_condition, result.confidence,
                result.reason, result.ai_model,
                1 if shadow_mode else 0, would_be_eligible,
            ),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
