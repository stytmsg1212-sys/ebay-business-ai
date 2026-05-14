"""W110(4) Phase 2 (D): primary_market IS NULL 専用 dedicated re-scrape.

前提:
  - 5/9 朝 W110(4) Phase 1 (pilot 1+2+B) で active 残 23 件 scrape 完了
  - しかし旧仕様 (90 days, sample<5) で unknown 確定した listing 約 320 件は、
    cache TTL 168h により新仕様 scrape を skip されている
  - 本 launcher で primary_market IS NULL の active listing を強制 re-scrape

実装方針:
  - skip_recent_hours=1 (TTL=1h に縮小) で cache hit を 1h 以内に限定
    → 5/9 朝の pilot で saved 済 listing は cache hit で skip (= 二重 scrape 回避)
    → 旧仕様 unknown は cache miss → 新仕様で fresh scrape
  - listing 抽出 SQL に primary_market IS NULL filter 追加 (custom driver)
  - 50 件 batch、anti-bot abort 時 stop_on_consec_fail=5

実行 (推奨段階):
  - 1 batch ~25-30 分、~6-7 batch で完走見込
  - user 監視: anti-bot 検知 / CDP Chrome OOM / browser CAPTCHA 出現
  - 起動: python scripts/run_w110_d_rescrape_null_2026_05_09.py [--limit 50]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/w110_d_rescrape_null_2026_05_09.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="W110 D: primary_market NULL 専用 re-scrape")
    parser.add_argument("--limit", type=int, default=50, help="batch 件数 (default 50)")
    parser.add_argument(
        "--ttl-hours", type=int, default=1,
        help="cache TTL hours (default 1, 5/9 朝 pilot 結果は cache hit で skip)",
    )
    parser.add_argument(
        "--sleep-seconds", type=float, default=3.0,
        help="ジッタ前の base sleep 秒 (default 3.0、anti-bot 慎重モードは 4.0+ 推奨)",
    )
    parser.add_argument(
        "--include-ended", action="store_true",
        help="ended (is_ended=1) listings も scrape 対象に含める (再出品時 primary_market 参考データ確保)",
    )
    args = parser.parse_args()

    cfg_path = Path('config') / 'schedule_config.json'
    config = {}
    if cfg_path.exists():
        with open(cfg_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

    # CDP Chrome alive check
    from tasks.task_market_analysis_refresh import _check_cdp_available
    if not _check_cdp_available():
        logger.error(
            "CDP Chrome 未起動. scripts/start_chrome_cdp.bat 実行 + eBay ログイン + "
            "Terapeak (Last 365 days, Seller=Japan, Sold) を開いてから再実行."
        )
        sys.exit(1)

    # 対象 listing: primary_market IS NULL かつ active.
    # ORDER BY market_analysis_at ASC で「未 scrape (NULL) → 古い scrape」順に処理.
    # これにより前 batch で scrape 済の listing は market_analysis_at 更新で後ろに送られ、
    # 次 batch では別の 50 件が選択される (cache TTL=1h と組み合わせで full coverage 進行).
    from monitor.database import get_conn
    ended_clause = "" if args.include_ended else "AND COALESCE(is_ended, 0) = 0"
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT ebay_item_id, sku, title, primary_market, market_analysis_at, watch_count
               FROM ebay_listings
               WHERE sku IS NOT NULL AND sku != ''
                 AND primary_market IS NULL
                 {ended_clause}
               ORDER BY market_analysis_at ASC,
                        watch_count DESC,
                        ebay_item_id
               LIMIT ?""",
            (args.limit,),
        ).fetchall()
        listings = [dict(r) for r in rows]

    print("=" * 60)
    print(f"W110(4) D phase: primary_market IS NULL 専用 re-scrape")
    print(f"  対象 {len(listings)} 件 (limit={args.limit})")
    print(f"  cache TTL = {args.ttl_hours}h (5/9 朝 pilot 済 listing は cache hit で skip)")
    print(f"  起動時刻: {datetime.now().isoformat()}")
    print("=" * 60)

    if not listings:
        print("対象 0 件、終了。NULL primary_market が無いため D phase は不要状態。")
        return

    # 進捗表示用 callback
    def on_progress(i, n, title, phase, result):
        if phase == "scraping":
            logger.info(f"進捗 [{i}/{n}] scraping: {title[:60]}")

    # D の listing list を strict に渡すため、内部の _get_active_listings を bypass.
    # task_market_analysis_refresh の処理 loop を再利用しつつ listings を override する
    # 方法は signature に無いため、本 launcher で同等 loop を実装. ただし code 重複は
    # 最小化 (TTL 解釈ロジック + scrape 委譲 + propose 委譲のみコア).
    from monitor.market_analysis_cache import get_or_scrape
    from monitor.keyword_extractor import extract_keyword
    from monitor.terapeak_scraper import propose_market_change_for_listing
    import time
    import random

    started = datetime.now()
    succeeded = 0
    failed = 0
    proposed = 0
    consec_fail = 0
    STOP_ON_CONSEC = 5
    aborted = False

    for i, lst in enumerate(listings, 1):
        sku = lst["sku"] or ""
        title = lst["title"] or ""
        ebay_id = lst["ebay_item_id"]
        if not title:
            failed += 1
            consec_fail += 1
            print(f"  [{i}/{len(listings)}] title 空 → skip")
            continue

        keyword = extract_keyword(title, use_ai=True)
        if not keyword:
            failed += 1
            consec_fail += 1
            print(f"  [{i}/{len(listings)}] keyword 抽出失敗 → skip")
            continue

        print(f"[{i}/{len(listings)}] {title[:55]} → kw='{keyword}'")
        try:
            result, inserted_id, cache_hit = get_or_scrape(
                sku=sku, keyword=keyword, ebay_item_id=ebay_id,
                day_range=365, ttl_hours=args.ttl_hours,
            )
        except Exception as e:
            logger.exception(f"scrape 例外 sku={sku} eid={ebay_id}")
            failed += 1
            consec_fail += 1
            if consec_fail >= STOP_ON_CONSEC:
                logger.error(
                    f"連続 {STOP_ON_CONSEC} 件失敗 → eBay 規制疑い、abort. 処理済 {i-1}/{len(listings)}"
                )
                aborted = True
                break
            continue

        if not result.success:
            failed += 1
            consec_fail += 1
            print(f"  失敗: {result.error or 'unknown error'}")
            if consec_fail >= STOP_ON_CONSEC:
                logger.error(
                    f"連続 {STOP_ON_CONSEC} 件失敗 → abort. 処理済 {i-1}/{len(listings)}"
                )
                aborted = True
                break
        else:
            consec_fail = 0
            cache_tag = " [cache]" if cache_hit else ""
            print(f"  → {result.primary_market} (US {result.us_count}/{result.total_sold}){cache_tag}")
            if inserted_id and result.primary_market:
                n_proposed = propose_market_change_for_listing(
                    ebay_item_id=ebay_id,
                    sku=sku,
                    market_analysis_id=inserted_id,
                    proposed_market=result.primary_market,
                    reason=result.primary_market_reason or "",
                )
                proposed += n_proposed
            succeeded += 1

        # W110(3) ジッタ (固定間隔 anti-bot 回避).
        sleep_sec = args.sleep_seconds * (0.7 + random.random() * 0.8)
        time.sleep(sleep_sec)

    duration = (datetime.now() - started).total_seconds()
    print()
    print("=" * 60)
    print("W110(4) D phase RESULT")
    print(f"  処理: {succeeded + failed}/{len(listings)} 件")
    print(f"  成功: {succeeded} / 失敗: {failed} / 区分変更提案: {proposed}")
    print(f"  所要時間: {duration:.0f} 秒")
    print(f"  abort: {aborted}")
    print("=" * 60)


if __name__ == "__main__":
    main()
