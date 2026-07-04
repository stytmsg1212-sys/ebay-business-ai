#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""W153 (2026-05-22 改訂): 商品別ライバル検出.

旧 (グローバル known set + data/known_rival_sellers.json) は廃止.
user が UI で「監視 ON」 した listing についてのみ、商品個別の検索ワードで
eBay Browse API を巡回し listing_rival_discoveries に新規 rival を蓄積.

【v2 2026-05-22 PM】: 当初の「3-5 candidate 改行区切り → 各々別 query で
union」設計は user 視認で「Black 単独 50 件 noise」発覚 → 廃止.
**空白区切り 1 query AND 検索** に統一 (UI / generator / DB すべて).

設計書: .company/engineering/docs/2026-05-22-W153-rival-per-listing-detection-design.md (v2.1)
"""
import json
import logging
import re
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
    get_self_ebay_item_ids,
    record_rival_discovery,
    enrich_rival_discovery_shipping,
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


def _normalize_query(text: Optional[str]) -> str:
    """改行・連続空白を単一空白に collapse + trim.

    v2: 当初の「\\n split → 複数 query 別検索」は廃止.
    過去 DB data (改行混じり) も runtime で空白化して 1 query AND 検索.
    """
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _backoff_sleep(retry_count: int) -> float:
    """H-H: exponential backoff (1s, 2s, 4s ... cap 30s)."""
    return min(2.0 ** retry_count, 30.0)


def run_rival_per_listing_detection_one(
    eid: str,
    config: dict,
    *,
    query_override: Optional[str] = None,
    sleep_between: float = 2.0,  # M-internal-7: UI 経路 0.0、cron 2.0
    max_requests_remaining: Optional[int] = None,
    self_item_ids: Optional[frozenset] = None,
) -> dict:
    """単一 listing の検索. UI/cron 双方から呼ぶ.

    v2 (2026-05-22 PM): 引数 keywords_override: list[str] → query_override: str
    (空白区切り 1 query AND 検索に統一).

    self_item_ids: W308 自己マッチ遮断用 (自社 ebay_listings.ebay_item_id の集合)。
      None = 遮断しない (既存呼出側/テスト互換のデフォルト)。呼出側
      (run_rival_detection / UI) が `monitor.database.get_self_ebay_item_ids()`
      で読んで渡す想定。既存のセラー名一致除外 (`seller == my_seller`) は
      config['ebay']['seller_id'] 未設定 (本番で常に None) のため機能していな
      かった (77 件混入の根本原因)。item_id 一致は seller_id 設定に依存しない
      decisive 判定のため、これを主防御とする。

    Returns: {success, ebay_item_id, new_discoveries, refreshed, errors,
              skipped_bad_item_id, skipped_self_listing, requests_used, message}
    """
    summary = {
        "success": False, "ebay_item_id": eid,
        "new_discoveries": 0, "refreshed": 0, "errors": 0,
        "skipped_bad_item_id": 0,
        "skipped_self_listing": 0,  # W308: 自社出品との自己マッチ
        "skipped_keywords_null": 0,  # W153-UX (Codex 推奨 2026-05-26): keywords 未設定 = failure ではなく skipped
        "requests_used": 0,
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
        query = _normalize_query(query_override or row["rival_search_keywords"])
        if not query:
            # W153-UX (Codex 推奨 2026-05-26): keywords 未設定 = 半有効状態 (rival_watch_enabled=1 だが query 未設定)
            # failure ではなく **skipped** として扱う (毎日 first_err 化を回避)。
            # 旧: errors++ + success=False (H-D 当時の判断、毎朝 first_err Discord 通知 + log 騒音)
            # 新: skipped_keywords_null++ + success=True (UI で生成すれば自然解消、騒音 0)
            logger.info(
                f"[W153] {eid}: keywords NULL = skipped "
                f"(UI で『検索ワード生成』ボタン押下が必要)"
            )
            summary["skipped_keywords_null"] += 1
            summary["success"] = True
            summary["message"] = "keywords NULL (skipped, UI で生成要)"
            return summary
        # v2: 単 query AND 検索 = 1 word は AND が成立せず noise hit
        words = query.split(" ")
        if len(words) < 2:
            logger.warning(
                f"[W153] {eid}: query too short ({len(words)} word), refuse to search: {query!r}"
            )
            summary["errors"] += 1
            summary["message"] = f"query too short ({len(words)} word)"
            return summary

        creds = get_ebay_credentials(config)
        from tasks.ebay_browse_api import BrowseAPIClient

        client = BrowseAPIClient(
            creds.get('app_id', ''),
            creds.get('cert_id', ''),
        )
        my_seller = _get_my_seller_username(config)

        # v2: 1 listing = 1 Browse API call (loop 廃止)
        # H-H: max_requests budget check
        if max_requests_remaining is not None and max_requests_remaining <= 0:
            logger.warning(
                f"[W153] {eid}: max_requests budget exhausted before search "
                f"(query={query!r})"
            )
            summary["message"] = "max_requests_per_run reached"
            return summary

        items = None
        for retry in range(3):
            # v2.1 HIGH-3 fix: 試行 *前* に counter 消費
            # (failed retry も含めて quota cap 厳守)
            summary["requests_used"] += 1
            if max_requests_remaining is not None:
                max_requests_remaining -= 1
            try:
                items = client.search_items(
                    query=query, limit=50, item_location_country="JP",
                )
                break
            except _HTTP_RETRY_ERRORS as e:
                # code-reviewer HIGH-1+2 fix: str(e) でなく status_code 直接判定.
                status = getattr(
                    getattr(e, 'response', None), 'status_code', None
                )
                is_429 = (status == 429)
                is_5xx = (status is not None and 500 <= status < 600)
                is_transient = (
                    _HTTPX is not None and
                    isinstance(e, _HTTPX.RequestError) and
                    not isinstance(e, _HTTPX.HTTPStatusError)
                )
                is_decode = isinstance(e, json.JSONDecodeError)
                if is_429 or is_5xx or is_transient or is_decode:
                    sleep_s = _backoff_sleep(retry)
                    logger.warning(
                        f"[W153] {eid}: '{query[:40]}' "
                        f"transient (status={status}, type={type(e).__name__}), "
                        f"retry {retry+1}/3 in {sleep_s}s: {e}"
                    )
                    time.sleep(sleep_s)
                    continue
                # 非 transient (auth / 4xx 等) = retry 無価値、即 break
                logger.warning(
                    f"[W153] {eid}: '{query[:40]}' "
                    f"non-transient API error (status={status}): {e}"
                )
                items = None
                break
        if items is None:
            # retry 全失敗 or non-transient break
            summary["errors"] += 1
            summary["success"] = False
            summary["message"] = f"search failed: query={query!r}"
            return summary

        for it in items:
            seller = (it.get("seller") or "").strip()
            if not seller or seller == my_seller:
                continue
            # Browse API itemId "v1|123456789|0" → "123456789" 抽出
            raw_iid = it.get("item_id") or ""
            parts = raw_iid.split("|")
            competitor_iid = parts[1] if len(parts) >= 2 else raw_iid
            if not competitor_iid:
                logger.warning(
                    f"[W153] {eid}: Browse API returned item without "
                    f"competitor_item_id (query={query!r}, raw_iid={raw_iid!r})"
                )
                summary["skipped_bad_item_id"] += 1
                continue
            # W308: 自社出品との自己マッチ遮断 (item_id 一致は seller_id 設定に
            # 依存しない decisive 判定、上記 self_item_ids 説明参照)。
            if self_item_ids and competitor_iid in self_item_ids:
                logger.info(
                    f"[W308] {eid}: competitor_item_id={competitor_iid} が "
                    f"自社出品と一致、記録をスキップ"
                )
                summary["skipped_self_listing"] += 1
                continue
            new_id = record_rival_discovery(
                ebay_item_id=eid,
                competitor_seller=seller,
                competitor_item_id=competitor_iid,
                competitor_title=(it.get("title") or "")[:200],
                competitor_price_usd=it.get("price_usd"),
                search_keyword=query,
                # v51: search response の shipping info を保存 (UI 表示 + Economy hide 用)
                competitor_shipping_cost_usd=it.get("shipping_cost_usd"),
                min_delivery_date=it.get("min_delivery_date"),
                max_delivery_date=it.get("max_delivery_date"),
            )
            if new_id is not None:
                summary["new_discoveries"] += 1
                # v52: 新規 INSERT 成功時のみ詳細 API で shipping_service_code 等を
                # enrich. search response に shipping info が含まれない (= NULL) 場合の
                # 補完 + 「発送方法名」取得 (user 業務上「Economy」判定に必須).
                # quota: 新規分のみ = 1 listing 平均 ~5 calls/run (= cron 30 listings
                # × 5 = 150/day、daily cap 5000 の 3%).
                try:
                    summary["requests_used"] += 1
                    if max_requests_remaining is not None:
                        max_requests_remaining -= 1
                    detail = client.get_item_pricing(competitor_iid)
                    if detail:
                        enrich_rival_discovery_shipping(
                            new_id,
                            shipping_service_code=detail.get("shipping_service_code"),
                            shipping_cost_usd=detail.get("shipping_usd"),
                            min_delivery_date=detail.get("min_delivery_date"),
                            max_delivery_date=detail.get("max_delivery_date"),
                        )
                except Exception as e:
                    # enrich 失敗は record 自体は成功なので errors++ しない
                    # (UI 上は send info 欠落で表示するだけ、business impact 小).
                    logger.warning(
                        f"[W153] {eid}: enrich failed for {competitor_iid}: "
                        f"{type(e).__name__}: {e}"
                    )
            else:
                summary["refreshed"] += 1

        summary["success"] = (summary["errors"] == 0)
        summary["message"] = (
            f"new={summary['new_discoveries']} "
            f"refreshed={summary['refreshed']} "
            f"err={summary['errors']} "
            f"bad_iid={summary['skipped_bad_item_id']} "
            f"self={summary['skipped_self_listing']}"
        )
        if sleep_between > 0:
            time.sleep(sleep_between)
    except Exception as e:
        # Codex GPT-5.5 HIGH (2026-05-22 PM): top-level except で errors も +=1.
        # 旧実装は success=False のみ set し errors を更新しないため、
        # run_rival_detection の集約は res["errors"] のみ参照 → silent skip.
        # money-direct silent gap (record_rival_discovery DB write 例外、
        # credentials 構築失敗、unexpected item shape AttributeError 等が
        # Discord error alert に乗らない).
        logger.exception(f"[W153] {eid} run_one failed")
        summary["errors"] += 1
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
        "errors": 0, "skipped_bad_item_id": 0,
        "skipped_self_listing": 0,  # W308: 自社出品との自己マッチ
        "skipped_keywords_null": 0,  # W153-UX (Codex 推奨 2026-05-26): UI 生成待ち listing
        "requests_used": 0,
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
            logger.warning(
                f"[W153] {len(listings)} listings > "
                f"max_listings_per_run={max_listings}, truncating "
                f"(deterministic ORDER BY ebay_item_id = tail listings starve)"
            )
            # Codex MED-1 fix (2026-05-22 PM): silent skip 防止 Q0.
            # 10 件以上 skip なら Discord warn (user が ON 数を絞る判断ができるよう).
            skipped = len(listings) - max_listings
            if skipped >= 10:
                webhook = _resolve_rival_webhook(config)
                if webhook:
                    try:
                        from notifiers.discord_notifier import DiscordNotifier
                        DiscordNotifier(webhook, bypass_env=True).send_message(
                            f"⚠️ **W153 truncation**: 監視 ON listing が "
                            f"{len(listings)} 件あり max_listings_per_run="
                            f"{max_listings} を超えています。今回 {skipped} 件 skip "
                            f"(ORDER BY ebay_item_id 末尾は永久 starve リスク)。"
                            f"商品管理タブで ON 数を絞るか設定で cap を上げてください。"
                        )
                    except Exception as e:
                        logger.warning(f"[W153] truncate notify failed: {e}")
            listings = listings[:max_listings]
        requests_remaining = max_requests
        # W308: 1 run につき 1 回だけ読み、全 listing の巡回で使い回す
        # (自社 ebay_item_id 集合は 1 run の途中で変化しない前提)。
        self_item_ids = get_self_ebay_item_ids()

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
                self_item_ids=self_item_ids,
            )
            per_listing_summaries.append(res)
            summary["listings_processed"] += 1
            summary["new_discoveries_total"] += res["new_discoveries"]
            summary["errors"] += res["errors"]
            summary["skipped_bad_item_id"] += res["skipped_bad_item_id"]
            summary["skipped_self_listing"] += res.get("skipped_self_listing", 0)
            # W153-UX (Codex 推奨 2026-05-26): keywords NULL skipped を集計 (errors と分離)
            summary["skipped_keywords_null"] += res.get("skipped_keywords_null", 0)
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
            f"skip_self={summary['skipped_self_listing']} "
            f"skip_kw_null={summary['skipped_keywords_null']} "
            f"reqs={summary['requests_used']}"
        )
        # W164-pm Codex #4: errors>0 時 DB message に first error 詳細を追記.
        # Discord には既に詳細が乗っているが (_send_discord_errors_alert) Discord
        # は揮発、DB は audit 用. 次回 failure 時 task_execution_log で原因即特定.
        if summary["errors"] > 0:
            err_entries = [r for r in per_listing_summaries if r.get("errors", 0) > 0]
            if err_entries:
                first = err_entries[0]
                eid = first.get("ebay_item_id", "?")
                excerpt = (first.get("message") or "")[:100]
                summary["message"] += f" | first_err: {eid}: {excerpt}"

        # 集約 Discord 通知 (new>0)
        if new_by_listing:
            _send_discord_aggregate(config, new_by_listing)

        # H-D: errors>0 で 別 Discord alert + success=False
        if summary["errors"] > 0:
            _send_discord_errors_alert(config, summary, per_listing_summaries)
            summary["success"] = False
        else:
            summary["success"] = True

        # W153-UX HIGH-4 (code-reviewer 2026-05-26): keywords NULL skipped 率が
        # 高い時の週次 reminder (silent failure 検知の補完).
        _maybe_remind_user_of_keywords_null(config, summary)
    except Exception as e:
        logger.exception("[W153] run_rival_detection failed")
        summary["message"] = f"top-level: {type(e).__name__}: {e}"
        summary["success"] = False
    return summary


def _resolve_rival_webhook(config: dict) -> str:
    """W153 (2026-06-08): 専用ライバルチャンネル webhook を優先, 未設定なら既定へ fallback.

    inject_webhook_into_config が DISCORD_RIVAL_WEBHOOK_URL を
    config['discord']['rival_webhook_url'] に注入済 (entrypoint で実行)。専用未設定
    環境では既定 webhook_url (DISCORD_WEBHOOK_URL) へ fallback し通知先消失を防ぐ (Q0)。
    送信側は DiscordNotifier(..., bypass_env=True) で env DISCORD_WEBHOOK_URL の上書きを
    無効化し、本 URL (専用 or 既定) を確実に使う。
    """
    disc = config.get('discord') or {}
    return (disc.get('rival_webhook_url') or disc.get('webhook_url') or "").strip()


def _send_discord_aggregate(config: dict, new_by_listing: dict) -> None:
    """new>0 集約通知 (1 run 1 message). alert fatigue 抑制."""
    webhook = _resolve_rival_webhook(config)
    if not webhook:
        return
    from notifiers.discord_notifier import DiscordNotifier
    lines = [
        f"- **{v['title']}** ({v['tail4']}): {v['new']} 名"
        for v in new_by_listing.values()
    ]
    content = (
        f"🎯 **新規ライバル検出** ({len(new_by_listing)} listings)\n"
        + "\n".join(lines[:20])
    )
    if len(lines) > 20:
        content += f"\n... 他 {len(lines) - 20} listings"
    content += "\n→ 最安値チェックタブで価格対応を確認してください。"
    try:
        DiscordNotifier(webhook, bypass_env=True).send_message(content)
    except Exception as e:
        logger.warning(f"[W153] discord aggregate notify failed: {e}")


def _send_discord_errors_alert(
    config: dict, summary: dict, per_listing: list[dict],
) -> None:
    """H-D: errors>0 専用 alert. listing 名 + reason を 3-5 件抜粋."""
    webhook = _resolve_rival_webhook(config)
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
        f"⚠️ **ライバル監視エラー** "
        f"({summary['errors']}/{summary['listings_processed']} listings で取得失敗)\n"
        + "\n".join(excerpt)
        + (f"\n... 他 {extra} listings" if extra > 0 else "")
        + "\n→ 多くは一時的な取得エラーです。次回実行 (毎日) で自動的に再取得されるため、"
          "対応不要な場合が多いです。継続する場合のみ scheduler.log の rival_detection を確認してください。"
    )
    try:
        DiscordNotifier(webhook, bypass_env=True).send_message(content)
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


def _maybe_remind_user_of_keywords_null(config: dict, summary: dict) -> None:
    """W153-UX HIGH-4 (code-reviewer 2026-05-26): keywords NULL skipped 率が
    高い時の週次 reminder.

    背景: keywords NULL を errors → skipped に変更したことで毎朝 Discord errors_alert
    騒音は解消したが、user が log を見ない限り NULL 状態に気づけない (Q0 silent skip
    防止 weak化). skipped_keywords_null 率 ≥ 30% を threshold に週 1 reminder を発射
    (rival_keyword_generator silent failure or user 未生成 の両方を可視化).

    H-E の `_maybe_remind_user_of_unused_w153` と同じ週 1 dedupe (alert_date=月曜).
    webhook 未設定なら何もしない (claim 消費を避ける).
    """
    if summary.get("listings_processed", 0) == 0:
        return  # H-E 経路でカバー済 (0 listing reminder)
    skip_rate = (summary.get("skipped_keywords_null", 0)
                 / summary["listings_processed"])
    if skip_rate < 0.3:
        return
    webhook = (config.get('discord') or {}).get('webhook_url') or ""
    if not webhook:
        return
    today = datetime.now().date()
    week_monday = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    if not claim_alert_dedupe(
        task_key='w153_keywords_null_weekly',
        expected_hour=2,
        alert_date=week_monday,
    ):
        return
    from notifiers.discord_notifier import DiscordNotifier
    n_skip = summary["skipped_keywords_null"]
    n_total = summary["listings_processed"]
    content = (
        f"ℹ️ **W153 検索ワード未生成リマインダー**\n"
        f"ライバル監視 ON ({n_total} 件) のうち **{n_skip} 件 ({skip_rate*100:.0f}%)** "
        f"が検索ワード未生成 = rival_detection で skip 中.\n"
        f"商品管理タブで該当 listing の **「検索ワード生成」ボタン** を押下してください。"
        f"\n(もし UI で生成しても永続 skip の場合、rival_keyword_generator の "
        f"silent failure 可能性あり = scheduler.log 要確認)"
    )
    try:
        DiscordNotifier(webhook).send_message(content)
    except Exception as e:
        logger.warning(
            f"[W153] discord keywords-null reminder failed (1 week lost): {e}"
        )


if __name__ == "__main__":
    # 手動 CLI 実行用 (cron 同等)
    from monitor.config_loader import load_config  # type: ignore
    cfg = load_config()
    r = run_rival_detection(cfg)
    print(json.dumps(r, indent=2, ensure_ascii=False))
