"""primary_market IS NULL の active listing を Terapeak 分析して pending 提案化.

背景 (2026-06-09):
  標準の market_analysis_refresh は `WHERE sku != ''` で SKU 空 listing を除外する
  ため、5/20 の filter 解除で流入した SKU 空 101 件 (+ 他 NULL 20 件) が一度も
  分析されず primary_market=NULL のまま放置されていた。本 script はその NULL 群を
  対象に Terapeak 分析を回し、結果を pending_market_changes に提案する
  (反映は MonoDeck 市場戦略タブで user 承認。本 script は ebay_listings を直接
  書き換えない = Q2 直接書込回避)。

前提:
  - CDP Chrome (eBay ログイン済) が port 9222 で起動済
  - Terapeak Research ページ (Last 365 days / Seller=Japan / Sold) を 1 度開いておく
  - ANTHROPIC_API_KEY (keyword 抽出 Haiku 用。無ければ fallback)

使い方:
  python scripts/backfill_null_primary_market_2026_06_09.py --limit 1   # 疎通テスト
  python scripts/backfill_null_primary_market_2026_06_09.py             # 全 NULL
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# repo path 解決 + .env (ANTHROPIC_API_KEY)
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_null_pm")


def _get_null_listings() -> list[dict]:
    """primary_market IS NULL の active listing (SKU 空も含む). 高額順."""
    from monitor.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ebay_item_id, COALESCE(sku,'') AS sku, title,
                      current_price, COALESCE(quantity_ebay,0) AS qty
               FROM ebay_listings
               WHERE primary_market IS NULL
                 AND COALESCE(is_ended, 0) = 0
                 AND title IS NOT NULL AND title != ''
               ORDER BY current_price DESC NULLS LAST"""
        ).fetchall()
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="先頭 N 件のみ (疎通テスト用)")
    ap.add_argument("--day-range", type=int, default=365)
    ap.add_argument("--stop-consec", type=int, default=5, help="連続失敗で eBay 規制と判定し停止")
    args = ap.parse_args()

    from monitor.terapeak_scraper import propose_market_change_for_listing, CDP_ENDPOINT
    from monitor.market_analysis_cache import get_or_scrape
    from monitor.keyword_extractor import extract_keyword
    import urllib.request
    import time as _time

    # CDP 起動確認 (Q0: 落ちていたら即停止, silent skip しない)
    try:
        urllib.request.urlopen(f"{CDP_ENDPOINT}/json/version", timeout=5)
    except Exception as e:
        logger.error(f"CDP Chrome 未起動 ({e}). scripts/start_chrome_cdp.bat → eBay ログイン後に再実行.")
        sys.exit(2)

    listings = _get_null_listings()
    if args.limit:
        listings = listings[: args.limit]
    logger.info(f"NULL primary_market 対象: {len(listings)} 件 (day_range={args.day_range})")

    succeeded = failed = proposed = 0
    consec = 0
    errors: list[str] = []

    for i, lst in enumerate(listings, 1):
        title = lst["title"]
        sku = lst["sku"]
        ebay_id = lst["ebay_item_id"]
        keyword = extract_keyword(title, use_ai=True)
        if not keyword:
            failed += 1
            errors.append(f"{ebay_id}: keyword 抽出失敗 ({title[:30]})")
            logger.warning(f"[{i}/{len(listings)}] keyword 抽出失敗: {title[:50]}")
            continue

        logger.info(f"[{i}/{len(listings)}] ${lst['current_price']} {title[:48]} → kw='{keyword}'")
        result, inserted_id, cache_hit = get_or_scrape(
            sku=sku, keyword=keyword, ebay_item_id=ebay_id,
            day_range=args.day_range, ttl_hours=168,
        )
        if not result.success:
            failed += 1
            consec += 1
            errors.append(f"{ebay_id} ({title[:28]}): {result.error}")
            logger.warning(f"  失敗: {result.error}")
            if args.stop_consec > 0 and consec >= args.stop_consec:
                logger.error(
                    f"連続 {consec} 件失敗 → eBay 規制と判定し停止. 処理済 {i}/{len(listings)}. "
                    f"残 {len(listings)-i} 件は再実行で続行可."
                )
                break
            continue

        consec = 0
        succeeded += 1
        if not cache_hit:
            _time.sleep(2)  # anti-bot: 実 scrape 間に小休止 (cache hit は不要)
        if inserted_id and result.primary_market:
            n = propose_market_change_for_listing(
                ebay_item_id=ebay_id, sku=sku, market_analysis_id=inserted_id,
                proposed_market=result.primary_market,
                reason=result.primary_market_reason or "",
            )
            proposed += n
            tag = " [cache]" if cache_hit else ""
            logger.info(
                f"  → {result.primary_market} (US {result.us_count}/{result.total_sold}) "
                f"propose={n}{tag}"
            )
        else:
            logger.info(f"  → 判定不能 (primary_market={result.primary_market}, sold={result.total_sold})")

    logger.info("=" * 60)
    logger.info(f"完了: 成功 {succeeded} / 失敗 {failed} / pending 提案 {proposed} 件")
    if errors:
        logger.info(f"エラー先頭 10件: {errors[:10]}")


if __name__ == "__main__":
    main()
