"""W86 A/B test (v1: run_id=20260501_2227) の Opus 評価結果を supplier_candidates に migrate.

仕様:
  - source: supplier_ab_test_runs (model='claude-opus-4-7' のみ、より高精度)
  - filter: error IS NULL + (match_score >= 60 OR (alt_listing_possible AND match_score >= 20))
  - profit calc: 既存 _estimate_profit_for_candidate + check_supplier_candidate_profitable
  - INSERT: add_supplier_candidate (INSERT OR IGNORE で sku+candidate_url 重複除外)
  - status: 'pending' (user 承認待ち)
  - eval_model: 'claude-opus-4-7'
  - discovered_via: 'w86_ab_test_v1_migration'

v2 (現在 bg 動作中) は本 script 対象外. 完了後に同様の script で別途 migrate.
"""
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.database import (
    get_conn, add_supplier_candidate, get_ebay_listing_by_item_id,
)
from tasks.task_supplier_candidate_search import _estimate_profit_for_candidate
from calculator import check_supplier_candidate_profitable, load_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ab_migrate")

RUN_ID = "20260501_2227"  # W86 v1
SOURCE_MODEL = "claude-opus-4-7"
DISCOVERED_VIA = "w86_ab_test_v1_migration"
ALT0_THRESHOLD = 60   # 仕入候補
ALT1_THRESHOLD = 20   # 別 SKU 機会

def main():
    settings = load_settings()
    stats = {
        "total_rows": 0,
        "low_score": 0,        # match_score < ALT1
        "alt_low_score": 0,    # alt_listing AND score < 20
        "missing_listing": 0,  # ebay_listings に該当なし
        "inserted_main": 0,
        "inserted_alt": 0,
        "skipped_duplicate": 0,
        "errors": 0,
    }

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ebay_item_id, ebay_sku, candidate_index,
                   candidate_title, candidate_url, candidate_price_jpy,
                   candidate_platform, match_score, reasoning,
                   junk_likely_untested, alt_listing_possible, alt_listing_note
            FROM supplier_ab_test_runs
            WHERE run_id=? AND model=? AND error IS NULL
            ORDER BY ebay_item_id, candidate_index
            """,
            (RUN_ID, SOURCE_MODEL),
        ).fetchall()
    rows = [dict(r) for r in rows]
    stats["total_rows"] = len(rows)
    logger.info(f"=== migrate {len(rows)} ab_test rows (Opus, run_id={RUN_ID}) ===")

    for r in rows:
        score = r["match_score"]
        is_alt = bool(r["alt_listing_possible"])

        # filter
        if score < ALT1_THRESHOLD:
            stats["low_score"] += 1
            continue
        if score < ALT0_THRESHOLD and not is_alt:
            stats["low_score"] += 1
            continue
        # alt=1 で score < ALT1: 念のため (上記 score < ALT1 で既に弾かれる)
        if is_alt and score < ALT1_THRESHOLD:
            stats["alt_low_score"] += 1
            continue

        # ebay_listings 取得
        listing = get_ebay_listing_by_item_id(r["ebay_item_id"])
        if not listing:
            stats["missing_listing"] += 1
            logger.warning(f"  listing missing: ebay_item_id={r['ebay_item_id']}")
            continue

        # profit calc
        profit_jpy = None
        profitable = 0
        purchase_yen = r["candidate_price_jpy"]
        if purchase_yen is not None and purchase_yen > 0:
            try:
                profit_jpy = _estimate_profit_for_candidate(
                    listing=listing, purchase_yen=int(purchase_yen), settings=settings,
                )
                if profit_jpy is not None:
                    ok, _ = check_supplier_candidate_profitable(
                        profit_with_refund=profit_jpy, purchase_yen=int(purchase_yen),
                    )
                    profitable = int(ok)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"  profit calc failed for {r['candidate_url']}: {e}")
                stats["errors"] += 1

        # INSERT (重複は INSERT OR IGNORE で skip)
        new_id = add_supplier_candidate(
            sku=r["ebay_sku"] or "",
            candidate_url=r["candidate_url"] or "",
            source_platform=r["candidate_platform"] or "",
            candidate_price_jpy=purchase_yen,
            candidate_title=r["candidate_title"],
            match_score=score,
            match_reasoning=r["reasoning"],
            profit_jpy=profit_jpy,
            profitable=profitable,
            ebay_item_id=r["ebay_item_id"],
            discovered_via=DISCOVERED_VIA,
            junk_likely_untested=int(r["junk_likely_untested"] or 0),
            alt_listing_possible=int(is_alt),
            alt_listing_note=r["alt_listing_note"],
            eval_model=SOURCE_MODEL,
        )
        if new_id is None:
            stats["skipped_duplicate"] += 1
        elif is_alt:
            stats["inserted_alt"] += 1
        else:
            stats["inserted_main"] += 1

    logger.info("=== migration 結果 ===")
    for k, v in stats.items():
        logger.info(f"  {k:25s}: {v}")
    print(f"\nResult: main={stats['inserted_main']} alt={stats['inserted_alt']} "
          f"dup={stats['skipped_duplicate']} low={stats['low_score']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
