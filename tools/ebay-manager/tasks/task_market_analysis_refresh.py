"""W7-A 週次タスク: 全 SKU の Terapeak 市場分析を refresh.

実行タイミング: 毎週日曜 02:00 JST (動画 [60JJUZaMdpo] 推奨周期)

前提:
  - Chrome が --remote-debugging-port=9222 で起動済 (CDP attach)
  - Chrome に eBay Seller アカウントでログイン済
  - 起動していなければ Discord 通知して終了 (Q0 no silent skip)

処理:
  1. 全 active ebay_listings を取得
  2. 各 SKU について:
     a. keyword 抽出 (Haiku)
     b. terapeak_scraper.scrape_one_sku() 実行
     c. market_analysis テーブルに insert
     d. primary_market 変化があれば pending_market_changes に提案
  3. 完了時に Discord 通知 (件数 + 提案数)
"""
from __future__ import annotations

import logging
import os
import random  # W110(3): rate limit jitter (anti-bot 再発防止)
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 2026-05-06: defensive load_dotenv (caller (script / scheduler) が忘れた場合の防御).
# 過去事故: caller が load_dotenv を忘れ ANTHROPIC_API_KEY 未設定 → keyword 抽出が
# fallback (低品質) で動作 → Terapeak hit せず unknown 量産が 64+ 件発生.
# Q0 silent skip 構造的トラップの根絶のため、本 module 単独で env を保証.
try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    pass


def _check_cdp_available() -> bool:
    """CDP endpoint (port 9222) が応答するか確認."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("127.0.0.1", 9222))
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def _get_active_listings(skip_recent_hours: Optional[int] = None) -> list[dict]:
    """販売中 listing を **listing 単位** (1 行 = 1 ebay_item_id) で取得.

    W7-A Phase 3 修正版 (2026-04-29):
      - 旧版は GROUP BY sku で 244 → 157 に集約していたが、stock:01 が 40 異商品で
        共有されているため「1 SKU 1 scrape → 40 listing に同 market 押付」事故発生.
      - 本版は GROUP BY せず listing 単位で iterate. cache 層が同一 keyword での
        重複 scrape を skip する.

    Args:
        skip_recent_hours: 指定時間以内に scrape 成功した listing を skip.
            None なら全件. 例: 24 で「直近 24h 以内に成功した分は skip」.

    Returns:
        list of {ebay_item_id, sku, title, primary_market, market_analysis_at}
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        # 2026-05-06: quantity_ebay >= 1 フィルタ削除.
        # W7-A は eBay buyer 分布の判定であり、在庫数 (qty=0/>=1) は無関係.
        # 旧 qty フィルタで無在庫商品 (sku ebay**, eBay 上 qty=0 が正常) 234 件が
        # silent skip されていた問題を解消. is_ended=0 のみで「販売中」を判定.
        if skip_recent_hours and skip_recent_hours > 0:
            rows = conn.execute(
                """SELECT ebay_item_id, sku, title,
                          primary_market, market_analysis_at, watch_count
                   FROM ebay_listings
                   WHERE sku IS NOT NULL AND sku != ''
                     AND COALESCE(is_ended, 0) = 0
                     AND (market_analysis_at IS NULL
                          OR datetime(market_analysis_at) < datetime('now', ?, 'localtime'))
                   ORDER BY watch_count DESC NULLS LAST""",
                (f"-{skip_recent_hours} hours",),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ebay_item_id, sku, title,
                          primary_market, market_analysis_at, watch_count
                   FROM ebay_listings
                   WHERE sku IS NOT NULL AND sku != ''
                     AND COALESCE(is_ended, 0) = 0
                   ORDER BY watch_count DESC NULLS LAST"""
            ).fetchall()
    return [dict(r) for r in rows]


def _send_discord(config: dict, message: str, severity: str = "info") -> bool:
    """Discord 通知ヘルパ."""
    webhook = (config or {}).get("discord", {}).get("webhook_url") or ""
    if not webhook:
        return False
    color = {"info": 0x3399ff, "warn": 0xc89b2a, "error": 0xd84c38}.get(severity, 0x3399ff)
    try:
        import httpx
        embed = {
            "title": "市場戦略 (W7-A) refresh",
            "description": message,
            "color": color,
            "timestamp": datetime.now().isoformat(),
        }
        r = httpx.post(webhook, json={"embeds": [embed]}, timeout=10.0)
        return r.status_code in (200, 204)
    except Exception as e:
        logger.warning(f"Discord 送信失敗: {e}")
        return False


def run_market_analysis_refresh(config: Optional[dict] = None,
                                 limit: Optional[int] = None,
                                 use_ai_keyword: bool = True,
                                 on_progress=None,
                                 day_range: int = 365,  # W110(2): 90→365 (2026-05-09)
                                 skip_recent_hours: Optional[int] = None,
                                 stop_on_consecutive_failures: int = 5,
                                 sleep_seconds: float = 3.0) -> dict:
    """市場分析バッチ実行 (検索ボックス自動入力方式).

    user は事前に CDP Chrome で Terapeak ページを 1 回開いておく (Last X days, Seller=Japan, Sold).
    あとはこの関数が検索ボックスに各商品の keyword を順次入力 → Research → 抽出.

    Args:
        config: schedule_config.json
        limit: テスト用. None なら全 SKU
        use_ai_keyword: True なら Haiku で keyword 抽出, False なら fallback のみ
        on_progress: callback(i, n, title, phase, result=None). UI 進捗表示用
        day_range: Terapeak 集計期間 (default 365 days, W110(2) 2026-05-09 で 90→365)

    Returns:
        {success, processed, succeeded, failed, proposed_changes, message}
    """
    cfg = config or {}
    started_at = datetime.now()

    if not _check_cdp_available():
        msg = (
            "CDP Chrome が起動していません.\n"
            "scripts/start_chrome_cdp.bat を実行 → eBay にログイン → 再度実行してください."
        )
        logger.error(msg)
        _send_discord(cfg, msg, severity="error")
        return {
            "success": False,
            "error": "cdp_not_available",
            "message": msg,
        }

    listings = _get_active_listings(skip_recent_hours=skip_recent_hours)
    if limit:
        listings = listings[:limit]
    logger.info(
        f"市場分析開始: {len(listings)} listings "
        f"(skip_recent_hours={skip_recent_hours}, stop_on_consec_fail={stop_on_consecutive_failures})"
    )

    from monitor.terapeak_scraper import propose_market_change_for_listing
    from monitor.market_analysis_cache import get_or_scrape
    from monitor.keyword_extractor import extract_keyword

    succeeded = 0
    failed = 0
    proposed = 0
    errors = []
    consecutive_failures = 0  # eBay 規制検知用
    aborted_by_block = False

    for i, lst in enumerate(listings, 1):
        sku = lst["sku"] or ""
        title = lst["title"] or ""
        ebay_id = lst["ebay_item_id"]
        current_market = lst.get("primary_market")

        if on_progress:
            on_progress(i, len(listings), title, "scraping", None)

        if not title:
            failed += 1
            errors.append(f"{sku}: title 空")
            if on_progress:
                on_progress(i, len(listings), title, "failed",
                            {"success": False, "error": "title 空"})
            continue

        keyword = extract_keyword(title, use_ai=use_ai_keyword)
        if not keyword:
            failed += 1
            errors.append(f"{sku}: keyword 抽出失敗")
            if on_progress:
                on_progress(i, len(listings), title, "failed",
                            {"success": False, "error": "keyword 抽出失敗"})
            continue

        logger.info(f"[{i}/{len(listings)}] {title[:60]} → keyword='{keyword}'")

        # W7-A Phase 3: SKU 単位 cache + listing 単位 pending 提案
        # ttl: skip_recent_hours 指定時はそれを TTL とする. 未指定時は default 168h (1 週間)
        ttl = skip_recent_hours if skip_recent_hours and skip_recent_hours > 0 else 168
        result, inserted_id, cache_hit = get_or_scrape(
            sku=sku, keyword=keyword, ebay_item_id=ebay_id,
            day_range=day_range, ttl_hours=ttl,
        )

        # H-3: cache miss (= 実 navigate 発生) 時に api_call_log に記録する。
        # harvest と同一 provider/operation で合算可能にする。
        # scrape_via_search_box は最低 1 navigate (search) を行う。
        if not cache_hit:
            try:
                from monitor.api_logger import log_api_call
                log_api_call(
                    provider="terapeak",
                    model="cdp",
                    operation="terapeak_read",
                    input_tokens=0,
                    output_tokens=0,
                    success=result.success,
                    error_message=result.error if not result.success else None,
                )
            except Exception as _log_err:  # noqa: BLE001
                logger.warning(f"market_analysis: terapeak_read log 失敗: {_log_err}")

        if not result.success:
            failed += 1
            consecutive_failures += 1
            errors.append(f"{sku} ({title[:30]}): {result.error}")
            logger.warning(f"  失敗: {result.error}")
            if on_progress:
                on_progress(i, len(listings), title, "failed",
                            {"success": False, "error": result.error})

            # eBay 規制検知: N 件連続失敗で自動停止 (Q0 silent skip 防止).
            # 連続失敗 = block されている兆候. 続行しても全失敗で時間とログを浪費する.
            if (stop_on_consecutive_failures > 0
                    and consecutive_failures >= stop_on_consecutive_failures):
                aborted_by_block = True
                logger.error(
                    f"連続 {consecutive_failures} 件失敗 → eBay 規制と判定して停止. "
                    f"処理済 {i}/{len(listings)} 件. 残 {len(listings)-i} 件は次回再実行."
                )
                break
        else:
            if inserted_id and result.primary_market:
                # 1 listing 1 propose: 当該 ebay_item_id の current ≠ proposed なら upsert.
                # SKU 内の他 listing は触らない (異商品共有 SKU での cascade 排除).
                n_proposed = propose_market_change_for_listing(
                    ebay_item_id=ebay_id,
                    sku=sku,
                    market_analysis_id=inserted_id,
                    proposed_market=result.primary_market,
                    reason=result.primary_market_reason or "",
                )
                proposed += n_proposed
            succeeded += 1
            consecutive_failures = 0  # 成功でリセット
            cache_tag = " [cache]" if cache_hit else ""
            logger.info(
                f"  → {result.primary_market} (US {result.us_count}/{result.total_sold})"
                f"{cache_tag}"
            )
            if on_progress:
                on_progress(i, len(listings), title, "done", {
                    "success": True,
                    "primary_market": result.primary_market,
                    "us_count": result.us_count,
                    "total_sold": result.total_sold,
                    "cache_hit": cache_hit,
                })

        # W110(3) (2026-05-09): rate limit にジッタを加えて anti-bot 検知パターン
        # (固定間隔リクエスト) を回避. 5/8 04:08 abort (Imperva anti-bot 検知) の
        # 再発防止. 範囲: sleep_seconds * [0.7, 1.5] (0.85x avg、最大 50% 上振れ).
        _jitter_factor = 0.7 + random.random() * 0.8  # 0.7〜1.5
        _actual_sleep = sleep_seconds * _jitter_factor
        time.sleep(_actual_sleep)

    duration_sec = (datetime.now() - started_at).total_seconds()
    processed = succeeded + failed  # break で抜けた場合は len(listings) と一致しない

    msg_lines = [
        f"処理: {processed}/{len(listings)} 件 / 成功: {succeeded} / 失敗: {failed}",
        f"区分変更提案: {proposed} 件 (要承認)",
        f"所要時間: {duration_sec:.0f} 秒",
    ]
    if aborted_by_block:
        msg_lines.append(
            f"⚠️ eBay 規制疑い ({stop_on_consecutive_failures} 件連続失敗) で停止. "
            f"残 {len(listings) - processed} 件は次回再実行 (1-3 時間後 推奨)."
        )
    if errors[:3]:
        msg_lines.append("失敗例:")
        for err in errors[:3]:
            msg_lines.append(f"  - {err}")
    message = "\n".join(msg_lines)
    logger.info(message)

    severity = "warn" if failed > 0 or aborted_by_block else "info"
    _send_discord(cfg, message, severity=severity)

    # W242 (2026-06-09): 市場分析後に eBaymag 区分を再計算。新規取込/relist された
    # listing の ebaymag_segment NULL 放置を防ぐ (daily_relist の relist プール枯渇 /
    # 誤 relist 回避)。失敗しても市場分析本体の結果は妨げない。
    try:
        from monitor.ebaymag_segment import recompute_ebaymag_segments
        seg = recompute_ebaymag_segments()
        logger.info(f"ebaymag_segment 再計算: {seg}")
    except Exception as e:  # noqa: BLE001 — 区分再計算失敗は本体を妨げない
        logger.warning(f"ebaymag_segment 再計算失敗: {e}")

    return {
        "success": True,
        "processed": processed,
        "total_target": len(listings),
        "succeeded": succeeded,
        "failed": failed,
        "proposed_changes": proposed,
        "duration_sec": duration_sec,
        "aborted_by_block": aborted_by_block,
        "message": message,
    }


if __name__ == "__main__":
    import json
    import sys
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg_path = __import__("pathlib").Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    cfg = {}
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    # Default test: 1 件のみ
    result = run_market_analysis_refresh(cfg, limit=1, use_ai_keyword=False)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
