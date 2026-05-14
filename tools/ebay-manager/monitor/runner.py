"""
在庫監視メインループ（バッチ処理・設定リロード対応）
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .database import (
    init_db, get_active_items, update_item_status,
    add_check_log, get_prev_status, get_site_configs, prune_old_logs,
)
from .scrapers import prepare_batch_items, check_items_batch
from .notifier import send_unavailable_alert, send_restock_alert
from .ebay_sync import sync_listings_from_ebay, get_sync_report

logger = logging.getLogger(__name__)
SETTINGS_FILE = Path(__file__).parent.parent / "settings.json"


def _load_settings() -> dict:
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        return json.load(f)


def run_check_cycle(settings: dict) -> dict:
    """全アクティブアイテムをバッチチェック。Returns: {checked, notified, errors}"""
    init_db()
    items = get_active_items()
    webhook_url = settings.get("discord_webhook_url", "")
    notify_restock = settings.get("notify_on_restock", True)

    configs = get_site_configs()
    configs_by_prefix = {c["convert_url"]: c for c in configs}

    stats = {"checked": 0, "notified": 0, "errors": 0}

    # バッチチェック用データ準備
    batch = prepare_batch_items(items, configs_by_prefix)
    if not batch:
        return stats

    # 全アイテムを一括チェック（ブラウザ再利用）
    logger.info(f"Checking {len(batch)} items...")
    results = check_items_batch(batch)

    # 結果をDB反映・通知
    items_by_id = {item["id"]: item for item in items}
    for item_id, status in results.items():
        item = items_by_id.get(item_id)
        if not item:
            continue

        prev_status = get_prev_status(item_id)
        update_item_status(item_id, status)
        discord_sent = False

        # 在庫切れ・ページなし通知（error からの遷移も含む）
        if status in ("unavailable", "not_found") and prev_status not in ("unavailable", "not_found"):
            item["last_status"] = status
            sent = send_unavailable_alert(webhook_url, item)
            if sent:
                discord_sent = True
                stats["notified"] += 1
            logger.warning(f"Stock alert: {item.get('source_url', '')} -> {status}")

        # 在庫復活通知
        elif status == "available" and prev_status in ("unavailable", "not_found") and notify_restock:
            item["last_status"] = status
            sent = send_restock_alert(webhook_url, item)
            if sent:
                discord_sent = True
                stats["notified"] += 1
            logger.info(f"Restock alert: {item.get('source_url', '')} -> available")

        if status == "error":
            stats["errors"] += 1

        add_check_log(item_id, status, discord_sent)
        stats["checked"] += 1

    return stats


def run_ebay_sync(settings: dict) -> dict:
    """eBay出品と仕入元在庫を同期。Returns: {synced, matched, errors, messages}"""
    init_db()
    app_id = settings.get("ebay_app_id", "")
    dev_id = settings.get("ebay_dev_id", "")
    cert_id = settings.get("ebay_cert_id", "")
    user_token = settings.get("ebay_user_token", "")

    logger.info("Starting eBay sync...")
    sync_result = sync_listings_from_ebay(app_id, dev_id, cert_id, user_token)

    if sync_result.get("synced", 0) > 0 or sync_result.get("matched", 0) > 0:
        report = get_sync_report()
        logger.info(f"eBay sync complete: {sync_result}")
        logger.info(f"Sync report: {report}")

    return sync_result


def start_monitor_loop():
    """連続監視ループ（毎サイクル設定リロード・ログ自動削除）"""
    logger.info("Monitor started")

    while True:
        # 毎サイクル設定リロード（Streamlitでの変更を反映）
        try:
            settings = _load_settings()
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            time.sleep(60)
            continue

        interval = int(settings.get("monitor_interval_minutes", 30)) * 60

        start = datetime.now()
        logger.info(f"=== Cycle start: {start.strftime('%H:%M:%S')} ===")
        try:
            stats = run_check_cycle(settings)
            logger.info(f"Cycle done: {stats}")
        except Exception as e:
            logger.error(f"Cycle error: {e}", exc_info=True)

        # 古いログを自動削除（毎サイクル実行、軽量）
        try:
            prune_old_logs(30)
        except Exception:
            pass

        elapsed = (datetime.now() - start).total_seconds()
        sleep_sec = max(0, interval - elapsed)
        logger.info(f"Next check in {sleep_sec / 60:.1f}m")
        time.sleep(sleep_sec)
