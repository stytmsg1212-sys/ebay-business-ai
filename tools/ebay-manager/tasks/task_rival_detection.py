#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""W153 (2026-05-22 改訂): 商品別ライバル検出.

旧 (グローバル known set + data/known_rival_sellers.json) は廃止.
user が UI で「監視 ON」 した listing についてのみ、商品個別の検索ワードで
eBay Browse API を巡回し listing_rival_discoveries に新規 rival を蓄積.

設計書: .company/engineering/docs/2026-05-22-W153-rival-per-listing-detection-design.md (v2.1)
"""
import json
import logging
import sys
import time
from typing import Optional

# pythonw.exe gotcha guard
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from datetime import datetime, timedelta

from monitor.credentials import get_ebay_credentials
from monitor.database import (
    get_conn,
    record_rival_discovery,
)
from monitor.task_execution_log import claim_alert_dedupe

logger = logging.getLogger(__name__)

# code-reviewer HIGH-1+2 fix: status_code 直接判定 + transient/decode 包括.
# module level に集約 (LOW-2: 毎 listing で再構築する無駄を除去).
try:
    import httpx as _HTTPX  # type: ignore
    _HTTP_RETRY_ERRORS: tuple = (
        _HTTPX.HTTPStatusError,
        _HTTPX.RequestError,
        json.JSONDecodeError,
    )
except ImportError:
    _HTTPX = None  # type: ignore
    _HTTP_RETRY_ERRORS = (json.JSONDecodeError,)


def _get_my_seller_username(config: dict) -> Optional[str]:
    """自分の eBay seller_id (自セラー除外用). 未設定なら None."""
    return (config.get('ebay') or {}).get('seller_id') or None


def _fetch_target_listings() -> list[dict]:
    """rival_watch_enabled=1 かつ active な listing を返す."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ebay_item_id, title, rival_search_keywords, "
            "       initial_registered_at, rival_watch_started_at "
            "FROM ebay_listings "
            "WHERE COALESCE(rival_watch_enabled, 0) = 1 "
            "  AND COALESCE(is_ended, 0) = 0 "
            "ORDER BY ebay_item_id"
        ).fetchall()
    return [dict(r) for r in rows]


def _split_keywords(text: Optional[str]) -> list[str]:
    if not text:
        return []
    return [line.strip() for line in text.split("\n") if line.strip()]


def _backoff_sleep(retry_count: int) -> float:
    """H-H: exponential backoff (1s, 2s, 4s ... cap 30s)."""
    return min(2.0 ** retry_count, 30.0)


def run_rival_per_listing_detection_one(
    eid: str,
    config: dict,
    *,
    keywords_override: Optional[list[str]] = None,
    sleep_between: float = 2.0,  # M-internal-7: UI 経路 0.0、cron 2.0
    max_requests_remaining: Optional[int] = None,
) -> dict:
    """単一 listing の検索. UI/cron 双方から呼ぶ.

    Returns: {success, ebay_item_id, new_discoveries, refreshed, errors,
              skipped_bad_item_id, requests_used, message}
    """
    summary = {
        "success": False, "ebay_item_id": eid,
        "new_discoveries": 0, "refreshed": 0, "errors": 0,
        "skipped_bad_item_id": 0, "requests_used": 0,
        "message": "",
    }
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT title, rival_search_keywords "
                "FROM ebay_listings WHERE ebay_item_id = ?", (eid,)
            ).fetchone()
        if not row:
            summary["message"] = f"listing not found: {eid}"
            return summary
        keywords = keywords_override or _split_keywords(row["rival_search_keywords"])
        if not keywords:
            # H-D: 空 keyword で errors++ + success=False
            logger.warning(
                f"[W153] {eid}: no keywords "
                f"(UI で生成 or 保存してください)"
            )
            summary["errors"] += 1
            summary["message"] = "no keywords"
            return summary

        creds = get_ebay_credentials(config)
        from tasks.ebay_browse_api import BrowseAPIClient

        client = BrowseAPIClient(
            creds.get('app_id', ''),
            creds.get('cert_id', ''),
        )
        my_seller = _get_my_seller_username(config)

        for kw in keywords:
            # H-H: max_requests budget check
            if max_requests_remaining is not None and max_requests_remaining <= 0:
                logger.warning(
                    f"[W153] {eid}: max_requests budget exhausted, "
                    f"early break (kw={kw!r})"
                )
                summary["message"] = "max_requests_per_run reached"
                # v2.1 HIGH-4 fix: early break path で末尾 sleep を skip
                summary["_skip_final_sleep"] = True
                break
            items = None
            for retry in range(3):
                # v2.1 HIGH-3 fix: 試行 *前* に counter 消費
                # (failed retry も含めて quota cap 厳守)
                summary["requests_used"] += 1
                if max_requests_remaining is not None:
                    max_requests_remaining -= 1
                try:
                    items = client.search_items(
                        query=kw, limit=50, item_location_country="JP",
                    )
                    break
                except _HTTP_RETRY_ERRORS as e:
                    # code-reviewer HIGH-1+2 fix: str(e) でなく status_code 直接判定.
                    # httpx.HTTPStatusError は `Server error '502 ...' for url '...'`
                    # 形式で str(e) からの先頭 3 文字判定は dead code 化。
                    status = getattr(
                        getattr(e, 'response', None), 'status_code', None
                    )
                    is_429 = (status == 429)
                    is_5xx = (status is not None and 500 <= status < 600)
                    # transport / decode 系も transient 扱い (eBay 一時応答異常)
                    is_transient = (
                        _HTTPX is not None and
                        isinstance(e, _HTTPX.RequestError) and
                        not isinstance(e, _HTTPX.HTTPStatusError)
                    )
                    is_decode = isinstance(e, json.JSONDecodeError)
                    if is_429 or is_5xx or is_transient or is_decode:
                        sleep_s = _backoff_sleep(retry)
                        logger.warning(
                            f"[W153] {eid}: '{kw[:40]}' "
                            f"transient (status={status}, type={type(e).__name__}), "
                            f"retry {retry+1}/3 in {sleep_s}s: {e}"
                        )
                        time.sleep(sleep_s)
                        continue
                    # 非 transient (auth / 4xx 等) = retry 無価値、即 break
                    # errors 加算は outer block で行う (double-count 防止)
                    logger.warning(
                        f"[W153] {eid}: '{kw[:40]}' "
                        f"non-transient API error (status={status}): {e}"
                    )
                    items = None
                    break
            if items is None:
                # retry 全失敗 (3 回 backoff exhausted) or non-transient break
                summary["errors"] += 1
                continue

            for it in items:
                seller = (it.get("seller") or "").strip()
                if not seller or seller == my_seller:
                    continue
                # Browse API itemId "v1|123456789|0" → "123456789" 抽出
                raw_iid = it.get("item_id") or ""
                parts = raw_iid.split("|")
                competitor_iid = parts[1] if len(parts) >= 2 else raw_iid
                if not competitor_iid:
                    # H-G: silent gap 排除、WARNING + counter
                    logger.warning(
                        f"[W153] {eid}: Browse API returned item without "
                        f"competitor_item_id (kw={kw!r}, raw_iid={raw_iid!r})"
                    )
                    summary["skipped_bad_item_id"] += 1
                    continue
                new_id = record_rival_discovery(
                    ebay_item_id=eid,
                    competitor_seller=seller,
                    competitor_item_id=competitor_iid,
                    competitor_title=(it.get("title") or "")[:200],
                    competitor_price_usd=it.get("price_usd"),
                    search_keyword=kw,
                )
                if new_id is not None:
                    summary["new_discoveries"] += 1
                else:
                    summary["refreshed"] += 1

        # H-D: errors>0 で success=False
        summary["success"] = (summary["errors"] == 0)
        summary["message"] = (
            f"new={summary['new_discoveries']} "
            f"refreshed={summary['refreshed']} "
            f"err={summary['errors']} "
            f"bad_iid={summary['skipped_bad_item_id']}"
        )
        # M-internal-7: sleep は呼び側 (cron loop) が arg で制御 (UI 経路は 0)
        # code-reviewer HIGH-4 fix: max_requests early break path では skip
        # (retry 後に追加 2s = cron batch hour drift 防止)
        if sleep_between > 0 and not summary.get("_skip_final_sleep"):
            time.sleep(sleep_between)
    except Exception as e:
        logger.exception(f"[W153] {eid} run_one failed")
        summary["message"] = f"top-level: {type(e).__name__}: {e}"
        summary["success"] = False
    return summary


def run_rival_detection(config: dict) -> dict:
    """cron 経路. daily_scheduler.py L620-625 から呼ばれる.

    署名・戻り型は旧版と互換 (success / sellers / new_sellers_count / total_scanned / message).
    """
    summary: dict = {
        "success": False, "new_sellers_count": 0, "total_scanned": 0,
        "listings_processed": 0, "new_discoveries_total": 0,
        "errors": 0, "skipped_bad_item_id": 0, "requests_used": 0,
        "sellers": [], "message": "",
    }
    per_listing_summaries: list[dict] = []
    new_by_listing: dict = {}
    try:
        listings = _fetch_target_listings()
        if not listings:
            # H-E: 0 listings 永続シナリオで週 1 reminder
            _maybe_remind_user_of_unused_w153(config)
            logger.info("[W153] no listings with rival_watch_enabled=1, skip")
            summary["success"] = True
            summary["message"] = "0 listings monitored (UI で監視 ON にしてください)"
            return summary

        # H-H: per-run cap
        cfg_block = (config.get('tasks_enabled') or {}).get('rival_detection') or {}
        max_listings = int(cfg_block.get('max_listings_per_run', 30))
        max_requests = int(cfg_block.get('max_requests_per_run', 150))
        if len(listings) > max_listings:
            logger.info(
                f"[W153] {len(listings)} listings > "
                f"max_listings_per_run={max_listings}, truncating"
            )
            listings = listings[:max_listings]
        requests_remaining = max_requests

        for lst in listings:
            eid = lst["ebay_item_id"]
            if requests_remaining <= 0:
                logger.warning(
                    f"[W153] max_requests_per_run={max_requests} exhausted, "
                    f"stopping at listings_processed={summary['listings_processed']}"
                )
                summary["message"] = "max_requests_per_run reached"
                break
            res = run_rival_per_listing_detection_one(
                eid, config,
                sleep_between=2.0,
                max_requests_remaining=requests_remaining,
            )
            per_listing_summaries.append(res)
            summary["listings_processed"] += 1
            summary["new_discoveries_total"] += res["new_discoveries"]
            summary["errors"] += res["errors"]
            summary["skipped_bad_item_id"] += res["skipped_bad_item_id"]
            summary["requests_used"] += res["requests_used"]
            requests_remaining -= res["requests_used"]
            if res["new_discoveries"] > 0:
                new_by_listing[eid] = {
                    "new": res["new_discoveries"],
                    "title": (lst["title"] or "")[:40],
                    "tail4": eid[-4:],
                }

        # 旧契約互換 (daily_scheduler L726-738 の reformat ロジック)
        summary["sellers"] = []
        summary["new_sellers_count"] = summary["new_discoveries_total"]
        summary["total_scanned"] = summary["listings_processed"]
        summary["message"] = (
            f"listings={summary['listings_processed']} "
            f"new={summary['new_discoveries_total']} "
            f"err={summary['errors']} "
            f"bad_iid={summary['skipped_bad_item_id']} "
            f"reqs={summary['requests_used']}"
        )

        # 集約 Discord 通知 (new>0)
        if new_by_listing:
            _send_discord_aggregate(config, new_by_listing)

        # H-D: errors>0 で 別 Discord alert + success=False
        if summary["errors"] > 0:
            _send_discord_errors_alert(config, summary, per_listing_summaries)
            summary["success"] = False
        else:
            summary["success"] = True
    except Exception as e:
        logger.exception("[W153] run_rival_detection failed")
        summary["message"] = f"top-level: {type(e).__name__}: {e}"
        summary["success"] = False
    return summary


def _send_discord_aggregate(config: dict, new_by_listing: dict) -> None:
    """new>0 集約通知 (1 run 1 message). alert fatigue 抑制."""
    webhook = (config.get('discord') or {}).get('webhook_url') or ""
    if not webhook:
        return
    from notifiers.discord_notifier import DiscordNotifier
    lines = [
        f"- **{v['title']}** ({v['tail4']}): {v['new']} 名"
        for v in new_by_listing.values()
    ]
    content = (
        f"🎯 **W153 新規ライバル検出** ({len(new_by_listing)} listings)\n"
        + "\n".join(lines[:20])
    )
    if len(lines) > 20:
        content += f"\n... 他 {len(lines) - 20} listings"
    try:
        DiscordNotifier(webhook).send_message(content)
    except Exception as e:
        logger.warning(f"[W153] discord aggregate notify failed: {e}")


def _send_discord_errors_alert(
    config: dict, summary: dict, per_listing: list[dict],
) -> None:
    """H-D: errors>0 専用 alert. listing 名 + reason を 3-5 件抜粋."""
    webhook = (config.get('discord') or {}).get('webhook_url') or ""
    if not webhook:
        return
    from notifiers.discord_notifier import DiscordNotifier
    err_entries = [r for r in per_listing if r.get("errors", 0) > 0]
    excerpt = [
        f"- {r.get('ebay_item_id', '?')}: {r.get('message', '')[:100]}"
        for r in err_entries[:5]
    ]
    extra = len(err_entries) - 5
    content = (
        f"⚠️ **W153 errors 検出** "
        f"(listings={summary['listings_processed']}, "
        f"errors={summary['errors']})\n"
        + "\n".join(excerpt)
        + (f"\n... 他 {extra} listings" if extra > 0 else "")
    )
    try:
        DiscordNotifier(webhook).send_message(content)
    except Exception as e:
        logger.warning(f"[W153] discord errors-alert notify failed: {e}")


def _maybe_remind_user_of_unused_w153(config: dict) -> None:
    """H-E: 0 listings 週 1 reminder.

    claim_alert_dedupe は日次 dedupe (date_str + task_key + expected_hour 組で UNIQUE).
    週 1 cap 実装: alert_date = 当週月曜日 (`today - today.weekday()`) を渡すことで、
    同じ週内の INSERT OR IGNORE は 2 回目以降 rowcount=0 (= False 返却) で skip 確定.

    v2.1 MED-4 fix: webhook 存在確認を claim *前* に実施.
    webhook 未設定で claim 消費 = reminder 永久失効を防ぐ.
    """
    webhook = (config.get('discord') or {}).get('webhook_url') or ""
    if not webhook:
        # webhook 未設定 = 何もできない、claim も消費しない
        return
    today = datetime.now().date()
    week_monday = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    if not claim_alert_dedupe(
        task_key='w153_unused_weekly',
        expected_hour=2,
        alert_date=week_monday,
    ):
        return  # 既に当週内に通知済
    from notifiers.discord_notifier import DiscordNotifier
    content = (
        "ℹ️ **W153 リマインダー**: 「ライバル監視 ON」の商品がありません。\n"
        "商品管理タブの hero 「🎯 ライバル監視 (W153)」section で ON にすると、"
        "W183 自動値下げの監視対象に新規 rival を流入させられます。"
    )
    try:
        DiscordNotifier(webhook).send_message(content)
    except Exception as e:
        # 送信失敗 = claim は消費済なので来週まで再試行できない (1 週ロス admit)
        logger.warning(
            f"[W153] discord reminder notify failed (1 week lost): {e}"
        )


if __name__ == "__main__":
    # 手動 CLI 実行用 (cron 同等)
    from monitor.config_loader import load_config  # type: ignore
    cfg = load_config()
    r = run_rival_detection(cfg)
    print(json.dumps(r, indent=2, ensure_ascii=False))
