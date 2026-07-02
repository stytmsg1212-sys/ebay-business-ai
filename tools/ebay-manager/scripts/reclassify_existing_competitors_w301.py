#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W301 AI 店長 Phase1 S5: 既存採用競合の一括 AI 再判定 (one-shot script).

背景 (user 承認済み条件 1、2026-07-02):
  既存の active 採用ライバル (`competitor_products`、約178件) は人間が採用した
  ものなので S1 backfill (`scripts/backfill_pricing_eligible_w301.py`) で
  pricing_eligible=1 に温存する。ただし過去採用分に偽ライバル (JUNK/海外/DDU
  ブラックリスト等) が混ざっている可能性があるため、本 script は S2
  (`monitor/rival_classifier.py`) で裏から一括再判定し、「疑い分だけ」を
  user 提示用の markdown に出力する。

  **eligible は一切変更しない** (competitor_products は 1 バイトも書き込まない。
  外すかどうかは user が UI で判断する)。

対象抽出: S3 (`tasks/task_rival_pricing.py::_get_listings_with_active_competitors`)
  と全く同じゲート `is_active=1 AND COALESCE(pricing_eligible,0)=1` を使う
  (S1 backfill 実行後、既存採用分はこのゲートを満たす想定)。

⚠️ 重要な発見 (K0 Think Before Coding、仮定を明示):
  `competitor_products` テーブルには competitor_title / competitor_country /
  competitor_seller_feedback_score / is_sold_out のいずれの列も存在しない
  (id, our_item_id, our_sku, competitor_item_id, competitor_seller,
  seller_location, price_rule, min_price, max_discount, is_active, added_at,
  updated_at, competitor_price_usd, competitor_shipping_usd, last_priced_at,
  min_delivery_date, max_delivery_date, pricing_eligible のみ)。
  タイトルが無いと `monitor/rival_classifier.classify_discovery` の
  国/売切れ/JUNK ハード除外もタイトル類似度も機能せず、Claude へ空タイトルで
  判定を投げる羽目になる (無意味 + AI コスト浪費)。
  そこで本 script は `listing_rival_discoveries` (W153 ライバル検出の発見ログ)
  を `competitor_item_id` で LEFT JOIN し、直近の competitor_title /
  competitor_seller / competitor_price_usd を可能な限り補完する
  (2026-07-02 時点の実 DB 調査: active 178 件中 118 件は discoveries に一致行
  あり、60 件 (34%) は一致行なし = タイトル取得不能)。
  それでも競合タイトルが得られない行は **AI を呼ばず** `route="no_title_data"`
  / `classification="review"` として記録し、疑いリストで「目視確認推奨」と
  明示する (Q0: データ欠落を偽装せず可視化。データが無いことを AI に
  推測させて偽の real/noise 判定を作らない)。

Shadow: `shadow_mode=True` 固定。`rival_classifications` への記録のみ、
  `competitor_products` には一切書き込まない (would_be_eligible は「もし
  立てたら」の記録専用、実際に pricing_eligible を書き換える経路は存在しない)。

SKU 規約: 本 script は SKU を一切参照しない (listing 識別は our_item_id
  (=ebay_item_id) / competitor_item_id のみ、sku-rules.md 準拠)。

⚠️ 本 script は作成 (+ テスト) のみ。実 AI 呼出・本番 DB 書込を伴う実行判断は
  main / user に委ねる (このタスクでは --apply を実行しない)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Optional

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# scripts/ 直下実行時に repo(tools/ebay-manager) を import path へ (backfill_w301 と同パターン、2026-07-02 fix)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn, get_ddu_seller_ids, get_warning_brand_names
from monitor.rival_classifier import (
    DEFAULT_THRESHOLDS,
    ClassifyResult,
    _merge_ai_result,
    classify_discovery,
    judge_rival,
    save_rival_classification,
)
from tasks.task_rival_classify import _derive_our_rank

# Haiku 概算コスト (monitor/rival_classifier.py コメント準拠: 「コスト ≈$0.007/件」)
AI_COST_PER_CALL_USD = 0.007
# S1 backfill 対象 (約178件) 前提の既定上限。dry-run/apply 双方で引数化 (Q0)。
DEFAULT_MAX_AI_CALLS = 200

NO_TITLE_REASON = (
    "competitor_title データなし (competitor_products / listing_rival_discoveries "
    "いずれにも記録なし) のため AI 判定不能。目視確認推奨。"
)

# S3 (task_rival_pricing._get_listings_with_active_competitors) と同一ゲート。
# competitor_products 自体に競合タイトルが無いため listing_rival_discoveries を
# competitor_item_id で LEFT JOIN し、直近 (MAX(first_seen_at)) の
# competitor_title/competitor_seller/competitor_price_usd を補完する
# (SQLite の「単一 MAX() 集約時は bare column を同一入力行から取る」仕様に依拠)。
_TARGET_QUERY = """
SELECT
    cp.id AS row_id,
    cp.our_item_id,
    cp.our_sku,
    cp.competitor_item_id,
    cp.competitor_seller AS cp_competitor_seller,
    cp.competitor_price_usd AS cp_competitor_price_usd,
    cp.is_active,
    cp.pricing_eligible,
    el.title AS our_title,
    el.current_price AS our_price_usd,
    el.ebay_condition_id AS our_ebay_condition_id,
    el.condition_rank AS our_condition_rank,
    lrd.competitor_title AS lrd_competitor_title,
    lrd.lrd_seller,
    lrd.lrd_price_usd
FROM competitor_products cp
LEFT JOIN ebay_listings el ON el.ebay_item_id = cp.our_item_id
LEFT JOIN (
    SELECT competitor_item_id,
           competitor_title,
           competitor_seller AS lrd_seller,
           competitor_price_usd AS lrd_price_usd,
           MAX(first_seen_at) AS _mx_seen
    FROM listing_rival_discoveries
    GROUP BY competitor_item_id
) lrd ON lrd.competitor_item_id = cp.competitor_item_id
WHERE cp.is_active = 1 AND COALESCE(cp.pricing_eligible, 0) = 1
ORDER BY cp.id
"""


def _fetch_targets(limit: int = 0) -> list[dict]:
    """S3 と同一ゲートで対象行 (dict のリスト、生 DB 列) を返す。"""
    with get_conn() as conn:
        rows = conn.execute(_TARGET_QUERY).fetchall()
    targets = [dict(r) for r in rows]
    if limit:
        targets = targets[:limit]
    return targets


def _row_to_signals(row: dict) -> dict:
    """DB 行 → rival_classifier.classify_discovery/judge_rival 用の signals dict。"""
    competitor_title = row.get("lrd_competitor_title")
    competitor_seller = row.get("cp_competitor_seller") or row.get("lrd_seller")
    competitor_price_usd = row.get("cp_competitor_price_usd")
    if competitor_price_usd is None:
        competitor_price_usd = row.get("lrd_price_usd")
    our_rank = _derive_our_rank(
        row.get("our_ebay_condition_id"), row.get("our_condition_rank")
    )
    return {
        "row_id": row["row_id"],
        "ebay_item_id": row["our_item_id"],
        "competitor_item_id": row["competitor_item_id"],
        "our_title": row.get("our_title"),
        "competitor_title": competitor_title,
        "our_price_usd": row.get("our_price_usd"),
        "competitor_price_usd": competitor_price_usd,
        "our_rank": our_rank,
        "competitor_seller": competitor_seller,
        # competitor_country / competitor_seller_feedback_score / is_sold_out:
        # competitor_products にも listing_rival_discoveries にも保持されて
        # いないため常に None (残存リスク、報告に明記)。
    }


def _classify_one(
    signals: dict,
    dou_blacklist,
    warning_brands,
    thresholds: dict,
    ai_calls_used: int,
) -> tuple[ClassifyResult, bool]:
    """1 件を分類する (rival_classifier.classify_rival と同型のオーケストレーション
    + no_title_data 分岐を追加したもの、S2 モジュール自体は変更しない = K2)。

    戻り値: (ClassifyResult, ai_attempted)。ai_attempted=True は実際に Claude
    API 呼出を試みたこと (成功/失敗問わず) を示す。
    """
    pre = classify_discovery(signals, dou_blacklist, warning_brands, thresholds)

    if not pre.needs_ai:
        result = pre
        ai_attempted = False
    elif not signals.get("competitor_title"):
        result = replace(
            pre,
            classification="review",
            route="no_title_data",
            reason=NO_TITLE_REASON,
            needs_ai=False,
        )
        ai_attempted = False
    elif ai_calls_used >= thresholds["max_ai_calls_per_run"]:
        result = replace(
            pre,
            classification="review",
            route="ai_cap_exceeded",
            reason=(
                f"max_ai_calls_per_run={thresholds['max_ai_calls_per_run']} 超過のため "
                f"AI 判定をスキップ (review へ、Q0 痕跡)"
            ),
            needs_ai=False,
        )
        ai_attempted = False
    else:
        ai = judge_rival(signals)
        result = _merge_ai_result(pre, ai, thresholds)
        ai_attempted = True

    if pre.warning_brand_flag:
        result = replace(
            result,
            reason=(result.reason or "") + f" [warning_brand:{pre.warning_brand_flag}]",
        )
    return result, ai_attempted


def _write_suspicious_markdown(suspicious: list[dict], output_dir: str) -> Path:
    """疑いリスト (classification != 'real') を markdown 出力。SKU は使わず
    自社商品タイトル + ebay_item_id 末尾4桁で呼称 (CLAUDE.md 商品呼称規約準拠)。
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    path = out_dir / f"w301_suspicious_rivals_{today}.md"

    lines = [
        f"# W301 既存採用競合 疑いリスト ({today})",
        "",
        f"対象: is_active=1 かつ pricing_eligible=1 の既存採用競合のうち、AI 再判定で "
        f"noise/review となったもの ({len(suspicious)} 件)。",
        "pricing_eligible は本 script では一切変更していません "
        "(このリストは目視確認用、外すかどうかは user 判断)。",
        "",
        "| 自社商品 | ebay_item_id 末尾4桁 | 競合 item_id | 判定 | route | confidence | 理由 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in suspicious:
        item_id = str(item["ebay_item_id"] or "")
        last4 = item_id[-4:] if len(item_id) >= 4 else item_id
        confidence = (
            f"{item['confidence']:.2f}" if item["confidence"] is not None else "-"
        )
        reason = (item["reason"] or "").replace("|", "\\|").replace("\n", " ")
        title = (item["our_title"] or "(タイトル不明)").replace("|", "\\|")
        lines.append(
            f"| {title} | {last4} | {item['competitor_item_id']} | "
            f"{item['classification']} | {item['route']} | {confidence} | {reason} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run(*, apply: bool, limit: int, max_ai_calls: int, output_dir: str) -> dict:
    rows = _fetch_targets(limit=limit)
    signals_list = [_row_to_signals(r) for r in rows]

    thresholds = dict(DEFAULT_THRESHOLDS)
    thresholds["max_ai_calls_per_run"] = max_ai_calls

    dou_blacklist = get_ddu_seller_ids()
    warning_brands = get_warning_brand_names()

    summary: dict = {"target_count": len(signals_list), "apply": apply}

    if not apply:
        # dry-run: AI を一切呼ばず classify_discovery (純ロジック) のみで見込みを算出.
        resolved_without_ai = 0
        needs_ai_no_title = 0
        needs_ai_with_title = 0
        for signals in signals_list:
            pre = classify_discovery(signals, dou_blacklist, warning_brands, thresholds)
            if not pre.needs_ai:
                resolved_without_ai += 1
            elif not signals.get("competitor_title"):
                needs_ai_no_title += 1
            else:
                needs_ai_with_title += 1
        would_call = min(needs_ai_with_title, max_ai_calls)
        would_cap = max(0, needs_ai_with_title - max_ai_calls)
        summary.update({
            "resolved_without_ai": resolved_without_ai,
            "needs_ai_no_title_data": needs_ai_no_title,
            "needs_ai_with_title": needs_ai_with_title,
            "estimated_ai_calls": would_call,
            "estimated_ai_calls_capped": would_cap,
            "estimated_cost_usd": round(would_call * AI_COST_PER_CALL_USD, 4),
            "max_ai_calls_per_run": max_ai_calls,
        })
        return summary

    # apply: 実際に判定 + persist (rival_classifications のみ、competitor_products 不変)
    ai_calls_used = 0
    counts = {"real": 0, "noise": 0, "review": 0}
    route_counts: dict = {}
    suspicious: list[dict] = []
    for signals in signals_list:
        result, ai_attempted = _classify_one(
            signals, dou_blacklist, warning_brands, thresholds, ai_calls_used
        )
        if ai_attempted:
            ai_calls_used += 1
        save_rival_classification(
            result,
            discovery_id=None,
            ebay_item_id=signals["ebay_item_id"],
            competitor_item_id=signals["competitor_item_id"],
            shadow_mode=True,
        )
        counts[result.classification] += 1
        route_counts[result.route] = route_counts.get(result.route, 0) + 1
        if result.classification != "real":
            suspicious.append({
                "our_title": signals.get("our_title"),
                "ebay_item_id": signals["ebay_item_id"],
                "competitor_item_id": signals["competitor_item_id"],
                "classification": result.classification,
                "route": result.route,
                "confidence": result.confidence,
                "reason": result.reason,
            })

    md_path = _write_suspicious_markdown(suspicious, output_dir)
    summary.update({
        "ai_calls_used": ai_calls_used,
        "max_ai_calls_per_run": max_ai_calls,
        "counts": counts,
        "route_counts": route_counts,
        "suspicious_count": len(suspicious),
        "suspicious_markdown_path": str(md_path),
    })
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(
        description="W301 AI 店長 Phase1 S5: 既存採用競合の一括 AI 再判定 (one-shot)"
    )
    ap.add_argument("--db", default=None, help="対象 DB path (未指定は data/monitor.db)")
    ap.add_argument("--limit", type=int, default=0, help="0=全件、N=先頭N件のみ (段階実行用)")
    ap.add_argument(
        "--apply", action="store_true",
        help="未指定なら dry-run (AI 呼出/DB 書込ゼロ、見込み表示のみ)",
    )
    ap.add_argument(
        "--max-ai-calls", type=int, default=DEFAULT_MAX_AI_CALLS,
        help=f"この run の AI 呼出上限 (既定 {DEFAULT_MAX_AI_CALLS}、超過分は review へ回し残数を報告)",
    )
    ap.add_argument(
        "--output-dir", default="data/tmp",
        help="疑いリスト md の出力先ディレクトリ (既定 data/tmp)",
    )
    args = ap.parse_args()

    if args.db:
        import monitor.database as db_mod
        db_mod.DB_PATH = Path(args.db)

    summary = _run(
        apply=args.apply,
        limit=args.limit,
        max_ai_calls=args.max_ai_calls,
        output_dir=args.output_dir,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"[reclassify_existing_competitors_w301] elapsed {time.time()-t0:.1f}s")
