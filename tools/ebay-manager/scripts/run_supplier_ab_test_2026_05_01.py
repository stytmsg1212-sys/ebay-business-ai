"""W86 Supplier model A/B test (Opus 4.7 vs Sonnet 4.6).

仕様:
- qty=0 復活候補対象 から SKU prefix 4:3:3 (ebayyh_:ebayme_:ebayPF_) で 10 件抽出
- 各 listing に対して 1 回 scrape し、その結果を 2 model で独立評価
- past_judgments / knowledge_block は両モデルとも bypass (test_mode=True)
- 結果を supplier_ab_test_runs テーブルに保存 (Opus / Sonnet 各行で 2 倍件数)
- MonoDeck UI で並列カード比較 + コストサマリ render

K2 surgical: 既存 search_candidates_on_platform() を再利用、scrape ロジック重複なし.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from monitor.database import get_conn
from monitor.claude_evaluator import evaluate_match
from tasks.task_supplier_candidate_search import search_candidates_on_platform

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ab_test")

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M")
PLATFORMS = ["yahoo_auctions", "mercari", "paypay_furima"]
SKU_PREFIX_QUOTAS = [("ebayyh_", 4), ("ebayme_", 3), ("ebayPF_", 3)]
MODELS = [
    ("claude-opus-4-7",   "Opus 4.7"),
    ("claude-sonnet-4-6", "Sonnet 4.6"),
]


# === Schema (idempotent) ===

def init_test_table() -> None:
    """supplier_ab_test_runs を作成 (idempotent、try/except OperationalError)."""
    with get_conn() as conn:
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS supplier_ab_test_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  ebay_item_id TEXT NOT NULL,
                  ebay_title TEXT NOT NULL,
                  ebay_sku TEXT,
                  ebay_image_url TEXT,
                  candidate_index INTEGER NOT NULL,
                  candidate_title TEXT,
                  candidate_url TEXT,
                  candidate_image_url TEXT,
                  candidate_price_jpy INTEGER,
                  candidate_platform TEXT,
                  model TEXT NOT NULL,
                  match_score INTEGER,
                  reasoning TEXT,
                  junk_likely_untested INTEGER,
                  alt_listing_possible INTEGER,
                  alt_listing_note TEXT,
                  cache_read_tokens INTEGER,
                  cache_write_tokens INTEGER,
                  duration_ms INTEGER,
                  cost_usd REAL,
                  error TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ab_run_id "
                "ON supplier_ab_test_runs(run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ab_run_listing "
                "ON supplier_ab_test_runs(run_id, ebay_item_id, candidate_index, model)"
            )
        except sqlite3.OperationalError as e:
            logger.warning(f"init schema OperationalError (ignored): {e}")


# === Sample selection ===

def pick_sample_listings() -> list[dict]:
    """SKU prefix 4:3:3 で qty=0 listings をランダム抽出."""
    # 注: ebay_listings に画像 URL カラムなし → ebay_image_url は None で運用
    # (production code task_supplier_candidate_search L326-328 も同じ).
    sql_template = (
        "SELECT ebay_item_id, sku, title, source_url "
        "FROM ebay_listings "
        "WHERE sku LIKE ? "
        "  AND COALESCE(is_ended, 0) != 1 "
        "  AND COALESCE(quantity_ebay, -1) = 0 "
        "ORDER BY RANDOM() LIMIT ?"
    )
    out: list[dict] = []
    with get_conn() as conn:
        for prefix, limit in SKU_PREFIX_QUOTAS:
            rows = conn.execute(sql_template, (f"{prefix}%", limit)).fetchall()
            for r in rows:
                d = dict(r)
                d["prefix"] = prefix
                out.append(d)
    return out


# === Scrape ===

def scrape_candidates_for_listing(ebay_title: str) -> list[dict]:
    """全 platform で scrape、normalized list of candidates 返す."""
    all_hits: list[dict] = []
    for plat in PLATFORMS:
        try:
            hits = search_candidates_on_platform(plat, ebay_title)
            for h in hits:
                all_hits.append({
                    "title": getattr(h, "title", "") or "",
                    "url": getattr(h, "url", "") or "",
                    "image_url": getattr(h, "image_url", None),
                    "price_jpy": getattr(h, "price_jpy", None),
                    "platform": plat,
                })
        except Exception as e:
            logger.warning(f"scrape {plat} failed: {e}")
    return all_hits


# === Cost calc ===

# ⚠️ 訂正 (2026-05-29): opus-4-7 の $15/$75 は誤値。Opus は 4.5 以降ずっと $5/$25。
#   本 script は 2026-05-01 の frozen 成果物のため当時の出力数値は 3 倍過大 (Opus 側のみ)。
PRICING = {
    "claude-opus-4-7":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-sonnet-4-6": {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
}

def estimate_cost_for_call(model: str, in_tok: int, out_tok: int,
                            cache_r: int, cache_w: int) -> float:
    p = PRICING.get(model, {})
    if not p:
        return 0.0
    return (
        in_tok * p["input"] / 1_000_000
        + out_tok * p["output"] / 1_000_000
        + cache_r * p["cache_read"] / 1_000_000
        + cache_w * p["cache_write"] / 1_000_000
    )


# === Eval loop ===

def run_one_evaluation(listing: dict, cand: dict, cand_idx: int, model: str) -> dict:
    """1 候補 × 1 model の evaluate_match 呼出 + DB record データ整形."""
    t0 = time.time()
    r = evaluate_match(
        ebay_title=listing["title"],
        candidate_title=cand["title"],
        platform=cand["platform"],
        price_jpy=cand["price_jpy"],
        url=cand["url"],
        ebay_image_url=None,  # production と同じ (ebay_listings に画像 URL なし)
        candidate_image_url=cand.get("image_url"),
        sku=listing["sku"],
        ebay_item_id=listing["ebay_item_id"],
        model=model,
        test_mode=True,  # past_judgments / knowledge OFF
    )
    duration_ms = int((time.time() - t0) * 1000)
    # cost 推定: r.cache_read / r.cache_write は EvaluationResult から取れる
    # input/output token は api_call_log から後で join 可能 (本 script では推定)
    cost_estimate = estimate_cost_for_call(
        model,
        in_tok=200 if r.cache_read > 0 else 2200,  # cache hit 時は input は dynamic 部分のみ
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


def insert_test_row(conn, listing: dict, cand: dict, cand_idx: int,
                     model: str, eval_data: dict) -> None:
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
            RUN_ID, listing["ebay_item_id"], listing["title"], listing["sku"],
            None,  # ebay_image_url (運用上 None、ebay_listings に画像 URL カラムなし)
            cand_idx, cand["title"], cand["url"], cand.get("image_url"),
            cand["price_jpy"], cand["platform"],
            model, eval_data["match_score"], eval_data["reasoning"],
            eval_data["junk_likely_untested"], eval_data["alt_listing_possible"],
            eval_data["alt_listing_note"],
            eval_data["cache_read_tokens"], eval_data["cache_write_tokens"],
            eval_data["duration_ms"], eval_data["cost_usd"], eval_data["error"],
        ),
    )


# === Main ===

def main() -> int:
    init_test_table()
    samples = pick_sample_listings()
    logger.info(f"=== A/B test run_id={RUN_ID} ===")
    logger.info(f"sampled listings: {len(samples)}")
    for s in samples:
        logger.info(f"  {s['ebay_item_id']:14s} sku={s['sku']:25s} title={(s['title'] or '')[:60]}")

    # 全 listing scrape (1 回ずつ)、結果を snapshot
    snapshots: list[dict] = []
    for i, listing in enumerate(samples, 1):
        logger.info(f"[{i}/{len(samples)}] scrape {listing['ebay_item_id']} title={listing['title'][:50]}")
        hits = scrape_candidates_for_listing(listing["title"])
        logger.info(f"  → {len(hits)} candidates")
        snapshots.append({"listing": listing, "candidates": hits})

    # 各 candidate × 各 model で evaluate
    total_evals = 0
    for snap in snapshots:
        listing = snap["listing"]
        for cand_idx, cand in enumerate(snap["candidates"], 1):
            for model_id, model_label in MODELS:
                logger.info(
                    f"  evaluate listing={listing['ebay_item_id']} cand={cand_idx} "
                    f"model={model_label}"
                )
                eval_data = run_one_evaluation(listing, cand, cand_idx, model_id)
                with get_conn() as conn:
                    insert_test_row(conn, listing, cand, cand_idx, model_id, eval_data)
                total_evals += 1
                if eval_data["error"]:
                    logger.warning(f"    error: {eval_data['error']}")
                else:
                    logger.info(
                        f"    score={eval_data['match_score']} "
                        f"cache_r={eval_data['cache_read_tokens']} "
                        f"cost=${eval_data['cost_usd']:.5f}"
                    )

    logger.info(f"=== A/B test 完了 run_id={RUN_ID} total_evals={total_evals} ===")
    print(f"\nrun_id={RUN_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
