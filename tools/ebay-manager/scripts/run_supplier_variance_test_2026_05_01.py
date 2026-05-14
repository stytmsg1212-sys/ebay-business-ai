"""W86 補完: borderline 5 candidates × 3 runs × 2 models = 30 calls で score 分散測定.

目的: Opus / Sonnet の確率変動 (temperature) が gate(a) borderline range tuning に
       与える影響を測定. 標準偏差 5 点未満なら単発評価 OK、10 点超なら multi-run
       平均化 or gate(a) 範囲拡大 (40-60 → 35-65) の signal.

run_id format: <yyyymmdd_HHMM>_var (variance) で識別.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from monitor.database import get_conn
from monitor.claude_evaluator import evaluate_match
from scripts.run_supplier_ab_test_2026_05_01 import (
    init_test_table, estimate_cost_for_call, MODELS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("variance")

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M") + "_var"
N_RUNS = 3   # 各 candidate × 各 model で何回 evaluate するか


def pick_borderline_candidates(n: int = 5) -> list[dict]:
    """過去 supplier_ab_test_runs から Sonnet score 40-60 の borderline を抽出."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ebay_item_id, ebay_title, ebay_sku, candidate_index,
                   candidate_title, candidate_url, candidate_image_url,
                   candidate_price_jpy, candidate_platform, match_score
            FROM supplier_ab_test_runs
            WHERE model='claude-sonnet-4-6'
              AND match_score BETWEEN 40 AND 60
              AND error IS NULL
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]


def run_eval(target: dict, model: str, run_idx: int) -> dict:
    """1 候補 × 1 model × 1 試行."""
    listing = {
        "ebay_item_id": target["ebay_item_id"],
        "title": target["ebay_title"],
        "sku": target["ebay_sku"],
    }
    cand = {
        "title": target["candidate_title"],
        "url": target["candidate_url"],
        "image_url": target["candidate_image_url"],
        "price_jpy": target["candidate_price_jpy"],
        "platform": target["candidate_platform"],
    }
    t0 = time.time()
    r = evaluate_match(
        ebay_title=listing["title"],
        candidate_title=cand["title"],
        platform=cand["platform"],
        price_jpy=cand["price_jpy"],
        url=cand["url"],
        ebay_image_url=None,
        candidate_image_url=cand.get("image_url"),
        sku=listing["sku"],
        ebay_item_id=listing["ebay_item_id"],
        model=model,
        test_mode=True,  # variance 純粋測定 (knowledge_block 動的影響を排除)
    )
    duration_ms = int((time.time() - t0) * 1000)
    cost_estimate = estimate_cost_for_call(
        model,
        in_tok=200 if r.cache_read > 0 else 2200,
        out_tok=150,
        cache_r=r.cache_read,
        cache_w=r.cache_write,
    )
    return {
        "match_score": r.match_score,
        "reasoning": r.reasoning,
        "junk_likely_untested": int(r.junk_likely_untested),
        "alt_listing_possible": int(r.alt_listing_possible),
        "alt_listing_note": r.alt_listing_note,
        "cache_read_tokens": r.cache_read,
        "cache_write_tokens": r.cache_write,
        "duration_ms": duration_ms,
        "cost_usd": cost_estimate,
        "error": r.error,
    }


def insert_var_row(conn, target: dict, run_idx: int, model: str, eval_data: dict):
    """variance 結果を supplier_ab_test_runs に保存 (candidate_index に run_idx を加味)."""
    # candidate_index に run_idx 1-3 を suffix-encode (元 idx * 100 + run_idx)
    encoded_idx = (target["candidate_index"] or 1) * 100 + run_idx
    conn.execute(
        """
        INSERT INTO supplier_ab_test_runs (
          run_id, ebay_item_id, ebay_title, ebay_sku, ebay_image_url,
          candidate_index, candidate_title, candidate_url, candidate_image_url,
          candidate_price_jpy, candidate_platform,
          model, match_score, reasoning,
          junk_likely_untested, alt_listing_possible, alt_listing_note,
          cache_read_tokens, cache_write_tokens, duration_ms, cost_usd, error
        ) VALUES (?, ?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?,  ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?, ?)
        """,
        (
            RUN_ID, target["ebay_item_id"], target["ebay_title"], target["ebay_sku"],
            None, encoded_idx,
            target["candidate_title"], target["candidate_url"], target["candidate_image_url"],
            target["candidate_price_jpy"], target["candidate_platform"],
            model, eval_data["match_score"], eval_data["reasoning"],
            eval_data["junk_likely_untested"], eval_data["alt_listing_possible"],
            eval_data["alt_listing_note"],
            eval_data["cache_read_tokens"], eval_data["cache_write_tokens"],
            eval_data["duration_ms"], eval_data["cost_usd"], eval_data["error"],
        ),
    )


def main() -> int:
    init_test_table()
    targets = pick_borderline_candidates(n=5)
    if not targets:
        logger.error("borderline candidates 0 件 (Sonnet score 40-60). 先に v1/v2 test 実行してください.")
        return 1

    logger.info(f"=== variance test run_id={RUN_ID} (5 candidates × 3 runs × 2 models = 30 calls) ===")
    for i, t in enumerate(targets, 1):
        logger.info(
            f"  target {i}: listing={t['ebay_item_id']} cand_idx={t['candidate_index']} "
            f"prev_sonnet={t['match_score']} title={(t['candidate_title'] or '')[:50]}"
        )

    total_evals = 0
    for t in targets:
        for run_idx in range(1, N_RUNS + 1):
            for model_id, model_label in MODELS:
                ev = run_eval(t, model_id, run_idx)
                with get_conn() as conn:
                    insert_var_row(conn, t, run_idx, model_id, ev)
                total_evals += 1
                logger.info(
                    f"  listing={t['ebay_item_id']} run={run_idx} model={model_label} "
                    f"score={ev['match_score']} cost=${ev['cost_usd']:.5f}"
                )

    logger.info(f"=== variance test 完了 run_id={RUN_ID} total_evals={total_evals} ===")
    print(f"\nrun_id={RUN_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
