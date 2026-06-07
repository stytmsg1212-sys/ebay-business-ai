#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 商品リサーチ自動化 フェーズB MVP PoC (ビルド順 step1-2).

スコープ:
  1 商品 手入力 (title_ja + 概算重量) → フリマ (メルカリ/ヤフオク/PayPay) で同一
  商品+同状態を探索 → claude_evaluator で同一性 judge → research_candidates
  テーブルに着地。

絶対にしないこと (out-of-scope):
  - eBay 出品 (verify/draft 含む)
  - キーワード新着監視への登録
  - 仕入購入の実行
  - 売れ行きゲート (Terapeak ACTIVE/SOLD 連携) ※フェーズA / W229

レビュー指摘の反映 (.company/engineering/docs/2026-06-07-product-research-automation-spec.md §8):
  - P0-1 「既存 task_supplier_candidate_search の流用は無理筋」: あの関数は
    `get_ebay_listing_by_item_id` で listing 必須 + `_estimate_profit_for_candidate`
    が current_price (eBay 売値) を必須にしているため、未出品 research_candidate を
    流すと 1 行目で None で抜ける。アダプタ層として薄い新関数を本ファイルに置く。
    実態: 流用できたのは「mercari_search / yahoo_search / paypay_search の検索 helper
    のみ」≈ ローレベル fetch だけ (全体ロジックの 1〜2 割)。
  - P0-2 状態機械: insert (new) → 探索 → sourced / not_found / needs_review に
    遷移。silent skip 防止のため reason 必須 (`research_candidates_db.update_status`)。
  - P1-1 利益: weight 欠落 = clip 0g で偽黒字を作らない。`compute_breakeven_price_usd`
    は weight<=0 で None を返す仕様 → ここで needs_review に落とす。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import research_candidates_db as rc_db

logger = logging.getLogger(__name__)

# 同一性判定の保存閾値。`task_supplier_candidate_search` の 60 と同水準だが、
# PoC では「最終一致判定は人間」(§2-B) なので閾値で reject せず、全件保存して
# 人間に提示する。本定数は将来の UI フィルタ用に置くだけ。
MATCH_SCORE_SUGGESTED_FLOOR = 60

# フリマ探索プラットフォーム (Amazon/楽天は対象外 / 設計書 §2-B)。
DEFAULT_PLATFORMS: tuple[str, ...] = ("mercari", "yahoo_auctions", "paypay_furima")


@dataclass
class FreemarketHit:
    """フリマ探索 1 件 (mercari/yahoo/paypay の正規化形式)."""
    source_platform: str
    url: str
    title: str
    price_jpy: Optional[int]
    image_url: Optional[str] = None


def _search_freemarket(
    platform: str, keyword: str, max_results: int = 5
) -> list[FreemarketHit]:
    """フリマ 1 platform 検索 (mercari/yahoo/paypay)。

    既存 `tasks.task_supplier_candidate_search.search_candidates_on_platform` と
    同流儀 (薄い dispatcher)。本 PoC では同関数を直接呼ばず、専用 dispatcher を
    置く理由は (1) test で個別 mock しやすくする、(2) 仕入先候補探索 task が
    将来 W223 のように在庫 gate や availability check を内側で増やしても本 PoC
    が巻き込まれないようにするため (依存方向の分離)。
    """
    try:
        if platform == "mercari":
            from .mercari_search import search_mercari

            raw = search_mercari(keyword, max_results=max_results)
            return [
                FreemarketHit(
                    source_platform="mercari",
                    url=h.url,
                    title=h.title or "",
                    price_jpy=h.price_jpy,
                    image_url=h.image_url,
                )
                for h in raw
            ]
        if platform == "yahoo_auctions":
            from .yahoo_search import search_yahoo

            raw = search_yahoo(keyword, max_results=max_results)
            return [
                FreemarketHit(
                    source_platform="yahoo_auctions",
                    url=h.url,
                    title=h.title or "",
                    price_jpy=h.price_jpy,
                    image_url=h.image_url,
                )
                for h in raw
            ]
        if platform == "paypay_furima":
            from .paypay_search import search_paypay

            raw = search_paypay(keyword, max_results=max_results)
            return [
                FreemarketHit(
                    source_platform="paypay_furima",
                    url=h.url,
                    title=h.title or "",
                    price_jpy=h.price_jpy,
                    image_url=h.image_url,
                )
                for h in raw
            ]
    except Exception as e:
        # ⚠️ 例外を握り潰すと P2「取得エラー」と「在庫なし(0 件)」が区別不能になる。
        # caller (`evaluate_product`) で別扱いするため、例外は re-raise する。
        # ここではログに platform だけ残す (本 PoC では Q0 「fake success」を作らない)。
        logger.warning(f"freemarket search error: platform={platform} err={e}")
        raise
    logger.warning(f"freemarket search: unknown platform {platform!r}")
    return []


def estimate_profit_usd_for_research(
    *,
    terapeak_avg_price_usd: Optional[float],
    purchase_yen: int,
    manual_weight_g: Optional[float],
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    settings: Optional[dict] = None,
) -> tuple[Optional[float], Optional[str]]:
    """PoC 専用 利益見込み: (estimated_profit_usd, needs_review_reason).

    P0-1 / P1-1 反映:
      - 既存 `_estimate_profit_for_candidate` (task_supplier_candidate_search) は
        ebay_listings dict + current_price 前提で動かない。本関数は terapeak 平均
        + 手入力 weight だけで計算する薄いアダプタ。
      - 利益計算は **`compute_breakeven_price_usd` の存在前提仕様** (weight<=0 で
        None を返す = 仕入不能/物理不明) を逆用する: 損益分岐 USD を求めて
        「terapeak 平均 - 損益分岐」を粗い利益見込みとする。weight 欠落で None が
        返ったら needs_review_reason 付きで上位に渡す (P1-1: 0 clip 禁止)。

    Returns:
      (profit_usd, None) 計算成功
      (None, reason)     計算不能 → needs_review に落とす根拠付き
    """
    if not terapeak_avg_price_usd or terapeak_avg_price_usd <= 0:
        return None, "terapeak_avg_price_usd 未入力 (Terapeak 平均が無いと利益計算不可)"
    if not purchase_yen or purchase_yen <= 0:
        return None, "purchase_yen が 0 以下 (フリマ価格未取得 = 仕入価格不明)"
    if not manual_weight_g or manual_weight_g <= 0:
        # P1-1: weight=0 clip 常用は禁止 (送料過小→偽黒字→誤仕入)。
        # 設計書「Terapeak には weight 無し → MVP は人手で概算重量入力」。
        return (
            None,
            "manual_weight_g 未入力 (送料計算不能。0 clip = 偽黒字防止のため "
            "needs_review)",
        )

    if settings is None:
        # K1: 本 PoC は単発計算なので settings 未指定なら calculator のデフォルトを
        # 読む (load_settings)。caller は test で dict 直渡しできる。
        from calculator import load_settings as _load_settings
        try:
            settings = _load_settings()
        except Exception as e:
            return None, f"settings load 失敗: {e}"

    from .lowest_price import compute_breakeven_price_usd

    try:
        breakeven_usd = compute_breakeven_price_usd(
            purchase_yen=float(purchase_yen),
            weight_g=float(manual_weight_g),
            length_cm=float(length_cm or 0),
            width_cm=float(width_cm or 0),
            height_cm=float(height_cm or 0),
            settings=settings,
        )
    except Exception as e:
        # RuntimeError (setup error / settings 不正) や型不正等。silent に黒字にしない。
        return None, f"breakeven 計算失敗: {e}"

    if breakeven_usd is None:
        # `compute_breakeven_price_usd` は weight<=0 / 仕入が極端高 (上限でも赤字)
        # / 計算経路の None で None を返す。「赤字判定」と「物理不明」を区別する
        # 情報は同 helper からは得られないので、両方を needs_review に倒す
        # (P2: 後者は技術失敗、前者は業務判断だが PoC では人間 review に統合)。
        return None, "breakeven 計算が None (仕入価格過大 or 物理データ不足)"

    profit_usd = float(terapeak_avg_price_usd) - float(breakeven_usd)
    return round(profit_usd, 2), None


def _best_hit(hits: list[FreemarketHit]) -> Optional[FreemarketHit]:
    """探索結果の代表 1 件を選ぶ. PoC では「価格が分かっている中で最安」.

    K1: 同一性スコアでソートするロジックは本 PoC では人間 review 前提のため不要。
    最安 1 件を保存し、人間が UI で複数比較する形に統合するのは将来。
    """
    priced = [h for h in hits if h.price_jpy is not None and h.price_jpy > 0]
    if not priced:
        return None
    return min(priced, key=lambda h: h.price_jpy)


def evaluate_product(
    title_ja: str,
    *,
    manual_weight_g: Optional[float] = None,
    terapeak_avg_price_usd: Optional[float] = None,
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    platforms: Optional[tuple[str, ...]] = None,
    max_results_per_platform: int = 5,
    settings: Optional[dict] = None,
) -> dict:
    """PoC エントリポイント. 1 商品の手入力からフリマ探索 + 同一性提示までを実行.

    フロー:
      1. research_candidates に new で 1 行 INSERT (status=new)
      2. status を sourcing に遷移
      3. 各 platform でフリマ探索. 例外は 1 つでも捕まえ needs_review に落とす
         (P2: 技術失敗と業務判断 0 件を区別)。
      4. 全 platform 合算で 0 件 → not_found
      5. 最有力 1 件で claude_evaluator.evaluate_match を呼び match_score 取得
      6. terapeak 平均 + 手入力 weight + フリマ実価格で利益見込み計算
         (利益計算不能 = needs_review_reason 付きで落とす)
      7. 全部成功 → sourced

    Returns:
      {
        "rc_id": <int>,
        "status": <str>,
        "match_score": <int|None>,
        "match_reason": <str|None>,
        "estimated_profit_usd": <float|None>,
        "needs_review_reason": <str|None>,
        "found_url": <str|None>,
        "found_price_jpy": <int|None>,
        "source_platform": <str|None>,
        "search_errors": <list[str]>,  # 取得 error が出た platform 名
        "hits_count_total": <int>,
      }
    """
    if not title_ja or not title_ja.strip():
        raise ValueError("title_ja is required")
    platforms = platforms or DEFAULT_PLATFORMS

    # Step 1: insert
    rc_id = rc_db.insert_research_candidate(
        title_ja=title_ja.strip(),
        manual_weight_g=manual_weight_g,
        length_cm=length_cm,
        width_cm=width_cm,
        height_cm=height_cm,
        terapeak_avg_price_usd=terapeak_avg_price_usd,
    )

    # Step 2: sourcing 遷移 (new → sourcing)
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)

    # Step 3: フリマ探索 (P2: error と 0 件を区別)
    all_hits: list[FreemarketHit] = []
    search_errors: list[str] = []
    for plat in platforms:
        try:
            hits = _search_freemarket(
                plat, title_ja.strip(), max_results=max_results_per_platform
            )
            all_hits.extend(hits)
        except Exception as e:
            # 取得エラー = 後で needs_review に落とす根拠。silent skip しない。
            search_errors.append(f"{plat}: {type(e).__name__}: {e}")
            logger.warning(
                f"[research_poc] search error rc_id={rc_id} platform={plat}: {e}"
            )

    if search_errors:
        # 1 platform でも取得失敗 = 全結果が信頼できない可能性 (中断 / 検閲 /
        # anti-bot 等)。Q0: needs_review で人間に再試行させる。
        reason = "フリマ探索で取得エラー: " + " / ".join(search_errors)
        rc_db.update_research_candidate_result(
            rc_id,
            new_status=rc_db.STATUS_NEEDS_REVIEW,
            needs_review_reason=reason,
        )
        return {
            "rc_id": rc_id,
            "status": rc_db.STATUS_NEEDS_REVIEW,
            "match_score": None,
            "match_reason": None,
            "estimated_profit_usd": None,
            "needs_review_reason": reason,
            "found_url": None,
            "found_price_jpy": None,
            "source_platform": None,
            "search_errors": search_errors,
            "hits_count_total": len(all_hits),
        }

    # Step 4: ヒット 0 件 = 在庫なし (= 仕入先実在せず) → not_found (業務判断)。
    # Codex 2段指摘#1: ヒットはあるが全件価格が取れない (価格欄パース失敗) のを
    # not_found に畳むと「実在せず」と「取得不完全」を混同し候補を silent に失う。
    # ヒットがあるのに代表が選べない場合は needs_review (取得不完全) に落とす。
    best = _best_hit(all_hits)
    if best is None:
        if all_hits:
            reason = (
                f"フリマ {len(all_hits)} 件ヒットしたが全件 価格が取得できず "
                "(価格欄パース失敗 = 取得不完全)。再探索 / 手動確認が必要。"
            )
            rc_db.update_research_candidate_result(
                rc_id,
                new_status=rc_db.STATUS_NEEDS_REVIEW,
                needs_review_reason=reason,
            )
            return {
                "rc_id": rc_id,
                "status": rc_db.STATUS_NEEDS_REVIEW,
                "match_score": None,
                "match_reason": None,
                "estimated_profit_usd": None,
                "needs_review_reason": reason,
                "found_url": None,
                "found_price_jpy": None,
                "source_platform": None,
                "search_errors": [],
                "hits_count_total": len(all_hits),
            }
        rc_db.update_status(rc_id, rc_db.STATUS_NOT_FOUND)
        return {
            "rc_id": rc_id,
            "status": rc_db.STATUS_NOT_FOUND,
            "match_score": None,
            "match_reason": None,
            "estimated_profit_usd": None,
            "needs_review_reason": None,
            "found_url": None,
            "found_price_jpy": None,
            "source_platform": None,
            "search_errors": [],
            "hits_count_total": len(all_hits),
        }

    # Step 5: 同一性判定 (claude_evaluator)。保存のみ、最終確定は人間 (§2-B)。
    # API エラーは EvaluationResult.error にメッセージが入る (match_score=0)。
    from .claude_evaluator import evaluate_match

    eval_result = evaluate_match(
        ebay_title=title_ja.strip(),
        candidate_title=best.title or "",
        platform=best.source_platform,
        price_jpy=best.price_jpy,
        url=best.url,
        # ebay_image_url は未出品 research_candidate なので None (= 比較画像なし)。
        ebay_image_url=None,
        candidate_image_url=best.image_url,
        # sku / ebay_item_id は持たないので渡さない (Few-shot 学習は出品済 entity のみ)。
        sku=None,
        ebay_item_id=None,
    )
    eval_error_reason: Optional[str] = (
        eval_result.error if getattr(eval_result, "error", None) else None
    )

    # Step 6: 利益見込み計算 (P1-1: weight 欠落 = needs_review)
    purchase_yen = int(best.price_jpy) if best.price_jpy else 0
    profit_usd, profit_reason = estimate_profit_usd_for_research(
        terapeak_avg_price_usd=terapeak_avg_price_usd,
        purchase_yen=purchase_yen,
        manual_weight_g=manual_weight_g,
        length_cm=length_cm,
        width_cm=width_cm,
        height_cm=height_cm,
        settings=settings,
    )

    # Step 7: 着地 status 決定
    # needs_review 条件: AI エラー or 利益計算不能。両者ある場合は両方併記。
    needs_review_reason_parts: list[str] = []
    if eval_error_reason:
        needs_review_reason_parts.append(
            f"claude_evaluator エラー: {eval_error_reason}"
        )
    if profit_reason:
        needs_review_reason_parts.append(profit_reason)

    if needs_review_reason_parts:
        final_status = rc_db.STATUS_NEEDS_REVIEW
        needs_review_reason = " / ".join(needs_review_reason_parts)
    else:
        final_status = rc_db.STATUS_SOURCED
        needs_review_reason = None

    rc_db.update_research_candidate_result(
        rc_id,
        found_url=best.url,
        found_price_jpy=best.price_jpy,
        match_score=eval_result.match_score,
        match_reason=eval_result.reasoning,
        estimated_profit_usd=profit_usd,
        new_status=final_status,
        needs_review_reason=needs_review_reason,
    )

    return {
        "rc_id": rc_id,
        "status": final_status,
        "match_score": eval_result.match_score,
        "match_reason": eval_result.reasoning,
        "estimated_profit_usd": profit_usd,
        "needs_review_reason": needs_review_reason,
        "found_url": best.url,
        "found_price_jpy": best.price_jpy,
        "source_platform": best.source_platform,
        "search_errors": [],
        "hits_count_total": len(all_hits),
    }
