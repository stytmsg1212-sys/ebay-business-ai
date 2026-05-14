"""W86 拡張: Hybrid Escalation gate 精度向上のための stratified A/B test (50 listings + knowledge_block enable).

v2 変更点 (vs v1 run_supplier_ab_test_2026_05_01.py):
  1. サンプル数 10 → 50 (4:3:3 random 30 + Hi-Fi 10 + Industrial 10)
  2. knowledge_block enable (test_mode=False) = production 相当
  3. 前回 run (W86 run_id=20260501_2227) の listings を exclude (重複排除)

仕様: Hybrid Escalation gate (W91) の threshold/keyword tuning 用 data 収集.
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
from tasks.task_supplier_candidate_search import search_candidates_on_platform

# v1 から再利用
from scripts.run_supplier_ab_test_2026_05_01 import (
    init_test_table, scrape_candidates_for_listing,
    estimate_cost_for_call, insert_test_row, MODELS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ab_test_v2")

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M") + "_v2"

# Stratified categories (memory: feedback_condition_by_brand.md)
HIFI_BRANDS = [
    "Audio-Technica", "PIONEER", "Pioneer", "DENON", "TEAC",
    "Astell", "Sennheiser", "FOSTEX", "Marantz", "Onkyo",
    "Yamaha", "JBL", "Bose",
]
INDUSTRIAL_BRANDS = [
    "KEYENCE", "OMRON", "Omron", "Panasonic", "ADVANTEST", "Advantest",
    "GRAPHTEC", "HIROSE", "Mitutoyo", "KIKUSUI", "Kikusui",
    "YASKAWA", "Yaskawa", "Mitsubishi", "FANUC", "YOKOGAWA", "Yokogawa",
]


def pick_stratified_listings(
    n_random: int = 30,
    n_hifi: int = 10,
    n_industrial: int = 10,
    exclude_run_id: str | None = "20260501_2227",
) -> list[dict]:
    """Stratified サンプル抽出.

    Phase 1: 4:3:3 random baseline 30 件
    Phase 2: HIFI_BRANDS keyword match 10 件
    Phase 3: INDUSTRIAL_BRANDS keyword match 10 件

    重複は ebay_item_id で排除. 過去 run (W86) の listings も exclude_run_id 指定で除外.
    """
    used_ids: set[str] = set()
    if exclude_run_id:
        with get_conn() as conn:
            for r in conn.execute(
                "SELECT DISTINCT ebay_item_id FROM supplier_ab_test_runs WHERE run_id=?",
                (exclude_run_id,),
            ):
                used_ids.add(r["ebay_item_id"])
        logger.info(f"exclude run_id={exclude_run_id}: {len(used_ids)} listings")

    out: list[dict] = []

    # Phase 1: random 4:3:3
    quotas_random = [("ebayyh_", 12), ("ebayme_", 9), ("ebayPF_", 9)]
    for prefix, limit in quotas_random:
        with get_conn() as conn:
            placeholders = ','.join('?' * len(used_ids)) if used_ids else "''"
            sql = (
                "SELECT ebay_item_id, sku, title FROM ebay_listings "
                "WHERE sku LIKE ? "
                "  AND COALESCE(is_ended, 0) != 1 "
                "  AND COALESCE(quantity_ebay, -1) = 0 "
                f"  AND ebay_item_id NOT IN ({placeholders}) "
                "ORDER BY RANDOM() LIMIT ?"
            )
            params = [f"{prefix}%"] + list(used_ids) + [limit]
            rows = conn.execute(sql, params).fetchall()
            for r in rows:
                d = dict(r)
                d["category"] = "random"
                d["prefix"] = prefix
                out.append(d)
                used_ids.add(r["ebay_item_id"])

    # Phase 2: Hi-Fi
    out.extend(_pick_brand_match(HIFI_BRANDS, "hifi", n_hifi, used_ids))

    # Phase 3: Industrial
    out.extend(_pick_brand_match(INDUSTRIAL_BRANDS, "industrial", n_industrial, used_ids))

    return out


def _pick_brand_match(brands: list[str], category: str,
                       limit: int, used_ids: set[str]) -> list[dict]:
    """ブランド keyword で title 部分一致する listings を抽出."""
    where_clauses = " OR ".join(["title LIKE ?" for _ in brands])
    with get_conn() as conn:
        placeholders = ','.join('?' * len(used_ids)) if used_ids else "''"
        sql = (
            "SELECT ebay_item_id, sku, title FROM ebay_listings "
            "WHERE sku LIKE 'ebay%' "
            "  AND COALESCE(is_ended, 0) != 1 "
            "  AND COALESCE(quantity_ebay, -1) = 0 "
            f"  AND ({where_clauses}) "
            f"  AND ebay_item_id NOT IN ({placeholders}) "
            "ORDER BY RANDOM() LIMIT ?"
        )
        params = [f"%{b}%" for b in brands] + list(used_ids) + [limit]
        rows = conn.execute(sql, params).fetchall()
    picked: list[dict] = []
    for r in rows:
        d = dict(r)
        d["category"] = category
        d["prefix"] = (r["sku"] or "").split("_", 1)[0] + "_"
        picked.append(d)
        used_ids.add(r["ebay_item_id"])
    return picked


def run_one_evaluation(listing: dict, cand: dict, cand_idx: int, model: str) -> dict:
    """1 候補 × 1 model. v2 = test_mode=False (knowledge_block enable)."""
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
        test_mode=False,  # ★ v2: knowledge_block 注入
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


def main() -> int:
    init_test_table()
    samples = pick_stratified_listings(n_random=30, n_hifi=10, n_industrial=10)
    logger.info(f"=== A/B test v2 (stratified + knowledge enable) run_id={RUN_ID} ===")
    logger.info(f"sampled listings: {len(samples)}")

    from collections import Counter
    by_cat = Counter(s["category"] for s in samples)
    logger.info(f"category 内訳: {dict(by_cat)}")

    for s in samples:
        logger.info(
            f"  [{s['category']:11s}] {s['ebay_item_id']:14s} "
            f"sku={s['sku']:25s} title={(s['title'] or '')[:55]}"
        )

    # scrape
    snapshots: list[dict] = []
    for i, listing in enumerate(samples, 1):
        logger.info(
            f"[{i}/{len(samples)}] scrape {listing['ebay_item_id']} "
            f"({listing['category']}) title={listing['title'][:50]}"
        )
        hits = scrape_candidates_for_listing(listing["title"])
        logger.info(f"  → {len(hits)} candidates")
        snapshots.append({"listing": listing, "candidates": hits})

    # eval
    total_evals = 0
    for snap in snapshots:
        listing = snap["listing"]
        for cand_idx, cand in enumerate(snap["candidates"], 1):
            for model_id, model_label in MODELS:
                eval_data = run_one_evaluation(listing, cand, cand_idx, model_id)
                with get_conn() as conn:
                    insert_test_row(conn, listing, cand, cand_idx, model_id, eval_data)
                total_evals += 1
                if eval_data["error"]:
                    logger.warning(
                        f"  err listing={listing['ebay_item_id']} cand={cand_idx} "
                        f"model={model_label}: {eval_data['error']}"
                    )
                else:
                    logger.info(
                        f"  ev listing={listing['ebay_item_id']} cand={cand_idx} "
                        f"model={model_label} score={eval_data['match_score']} "
                        f"cache_r={eval_data['cache_read_tokens']} cost=${eval_data['cost_usd']:.5f}"
                    )

    logger.info(f"=== A/B test v2 完了 run_id={RUN_ID} total_evals={total_evals} ===")
    print(f"\nrun_id={RUN_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
