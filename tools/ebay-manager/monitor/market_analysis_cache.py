"""W7-A SKU 単位 Terapeak 結果キャッシュ.

事故再発防止 (2026-04-29 SKU 主キー設計崩壊):
  - 同 SKU の 40 listing で Terapeak を 40 回叩く無駄を防ぐ
  - 1 SKU あたり 1 回 scrape → 結果を `market_analysis` に保存 →
    pending 提案時は listing 単位で展開して N 行 insert
  - cache TTL = 168h (1 週間, 週次 refresh と同期, user 指示 α)

責務分担:
  - terapeak_scraper.scrape_via_search_box: 実 Terapeak 呼出
  - terapeak_scraper.save_to_db: market_analysis insert + ebay_listings UPDATE
  - 本 module: cache lookup + miss 時の scrape 委譲
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from monitor.database import get_conn

logger = logging.getLogger(__name__)


def get_or_scrape(
    *,
    sku: str,
    keyword: str,
    ebay_item_id: str,
    day_range: int = 90,
    ttl_hours: int = 168,
):
    """SKU の最新 market_analysis が ttl 以内ならそれを返す. なければ scrape.

    Args:
        sku: 対象 SKU
        keyword: Terapeak 検索キーワード (cache miss 時のみ使用)
        ebay_item_id: 当該 listing の ebay_item_id (ebay_listings UPDATE 用)
        day_range: Terapeak 集計期間 (default 90)
        ttl_hours: cache TTL (default 168 = 1 週間)

    Returns:
        (result, market_analysis_id, cache_hit)
        result: MarketAnalysisResult (cache hit 時は DB row から再構築)
        market_analysis_id: market_analysis テーブルの id (cache hit でも有効)
        cache_hit: True なら scrape 呼出していない
    """
    from monitor.terapeak_scraper import (
        MarketAnalysisResult, scrape_via_search_box, save_to_db,
    )

    # cache lookup: 同 keyword + 同 day_range の最新 row
    # 2026-04-29 修正: SKU 共有問題 (stock:01 が 40 異商品で共有) のため
    # cache key を sku → keyword に変更. 同一 keyword なら同一商品とみなす.
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, sku, total_sold, us_count, non_us_count,
                      countries_breakdown, primary_market, primary_market_reason,
                      avg_sold_price_usd, avg_shipping_usd, sell_through_pct,
                      total_sellers, scraped_at, day_range, keyword
               FROM market_analysis
               WHERE keyword = ? AND day_range = ?
               ORDER BY scraped_at DESC LIMIT 1""",
            (keyword, day_range),
        ).fetchone()

    if row:
        try:
            scraped = datetime.fromisoformat(row["scraped_at"])
        except (ValueError, TypeError) as e:
            # Q0 silent skip 防止: 不正フォーマットを warning でログ
            logger.warning(
                f"market_analysis.id={row['id']} scraped_at parse 失敗: "
                f"{row['scraped_at']!r} ({e}). cache miss として scrape 続行."
            )
            scraped = None

        if scraped and (datetime.now() - scraped) < timedelta(hours=ttl_hours):
            # cache hit: DB row から MarketAnalysisResult を再構築
            res = MarketAnalysisResult(sku=sku, keyword=row["keyword"] or keyword)
            res.success = True
            res.total_sold = row["total_sold"]
            res.us_count = row["us_count"]
            res.non_us_count = row["non_us_count"]
            res.primary_market = row["primary_market"]
            res.primary_market_reason = row["primary_market_reason"]
            res.avg_sold_price_usd = row["avg_sold_price_usd"]
            res.avg_shipping_usd = row["avg_shipping_usd"]
            res.sell_through_pct = row["sell_through_pct"]
            res.total_sellers = row["total_sellers"]
            res.day_range = row["day_range"]
            res.scraped_at = row["scraped_at"]
            us = res.us_count or 0
            non_us = res.non_us_count or 0
            res.us_ratio = us / max(1, us + non_us)

            # 当該 listing の market_analysis_at だけ更新
            # (listing 単位の最終確認時刻として記録, cascade UPDATE はしない)
            with get_conn() as conn:
                conn.execute(
                    """UPDATE ebay_listings SET
                        market_analysis_at = ?,
                        market_sample_size = ?,
                        us_buyer_ratio = ?
                       WHERE ebay_item_id = ?""",
                    (datetime.now().isoformat(), res.total_sold,
                     res.us_ratio, ebay_item_id),
                )
            logger.info(
                f"[cache hit] sku={sku} (scraped {scraped.isoformat()}, "
                f"age={(datetime.now() - scraped).total_seconds()/3600:.1f}h)"
            )
            return res, row["id"], True

    # cache miss → scrape
    logger.info(f"[cache miss] sku={sku} day_range={day_range} → scrape")
    res = scrape_via_search_box(sku, keyword, day_range=day_range)
    inserted_id: Optional[int] = None
    if res.success:
        inserted_id = save_to_db(res, ebay_item_id=ebay_item_id)
    return res, inserted_id, False
