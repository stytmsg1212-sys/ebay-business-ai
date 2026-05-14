"""
Discord Webhook通知（リトライ・レート制限対応）
"""
import logging
import time

import httpx

logger = logging.getLogger(__name__)

STATUS_LABEL = {
    "unavailable": "在庫切れ",
    "not_found": "ページなし（削除？）",
    "available": "在庫あり",
    "error": "チェックエラー",
    "unknown": "不明",
}

STATUS_COLOR = {
    "unavailable": 0xFF4444,
    "not_found": 0xFF8800,
    "available": 0x44FF44,
    "error": 0xFFAA00,
}

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


def _send_webhook(webhook_url: str, payload: dict) -> bool:
    """Discord Webhook送信（リトライ・レート制限対応）"""
    if not webhook_url:
        return False

    for attempt in range(MAX_RETRIES):
        try:
            resp = httpx.post(webhook_url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                return True
            if resp.status_code == 429:
                # Rate limited
                retry_after = float(resp.json().get("retry_after", RETRY_DELAY))
                logger.warning(f"Discord rate limited, waiting {retry_after}s")
                time.sleep(retry_after)
                continue
            logger.warning(f"Discord webhook HTTP {resp.status_code} (attempt {attempt+1})")
        except Exception as e:
            logger.warning(f"Discord webhook error (attempt {attempt+1}): {e}")

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)

    logger.error("Discord webhook failed after all retries")
    return False


def send_unavailable_alert(webhook_url: str, item: dict) -> bool:
    """在庫切れ / ページなし 通知"""
    from datetime import datetime
    status = item.get("last_status", "unavailable")
    label = STATUS_LABEL.get(status, status)
    emoji = "\U0001f534" if status == "unavailable" else "\U0001f7e0"
    title_display = item.get("title") or item.get("sku", "")
    source_url = item.get("source_url", "")

    lines = []
    if source_url:
        lines.append(f"**URL**: {source_url}")
    if item.get("ebay_item_id"):
        lines.append(f"**eBay**: https://www.ebay.com/itm/{item['ebay_item_id']}")
    lines.append(f"**SKU**: `{item.get('sku', '')}`")

    payload = {
        "embeds": [{
            "title": f"{emoji} {label}: {title_display}",
            "description": "\n".join(lines),
            "color": STATUS_COLOR.get(status, 0xFF4444),
            "footer": {"text": f"eBay Monitor | {datetime.now().strftime('%Y-%m-%d %H:%M')}"},
        }]
    }
    return _send_webhook(webhook_url, payload)


def send_restock_alert(webhook_url: str, item: dict) -> bool:
    """在庫復活通知"""
    from datetime import datetime
    title_display = item.get("title") or item.get("sku", "")
    source_url = item.get("source_url", "")
    payload = {
        "embeds": [{
            "title": f"\U0001f7e2 {STATUS_LABEL['available']}: {title_display}",
            "description": f"**URL**: {source_url}\n**SKU**: `{item.get('sku', '')}`",
            "color": STATUS_COLOR["available"],
            "footer": {"text": f"eBay Monitor | {datetime.now().strftime('%Y-%m-%d %H:%M')}"},
        }]
    }
    return _send_webhook(webhook_url, payload)


def send_test_notification(webhook_url: str) -> bool:
    from datetime import datetime
    payload = {
        "embeds": [{
            "title": "\u2705 eBay Monitor - Connection Test",
            "description": "Discord notification is working.",
            "color": 0x0099FF,
            "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M")},
        }]
    }
    return _send_webhook(webhook_url, payload)


def send_new_competitor_alert(webhook_url: str, our_item_id: str, keyword: str,
                              competitor: dict, competitor_count: int = 1) -> bool:
    """新規 Japan セラーライバル出現通知"""
    from datetime import datetime

    payload = {
        "embeds": [{
            "title": f"🚨 New Japan Competitor Detected!",
            "description": (
                f"**Keyword**: {keyword}\n"
                f"**Your Item ID**: {our_item_id}\n\n"
                f"**Competitor Info**:\n"
                f"Item ID: {competitor.get('found_item_id')}\n"
                f"Seller: {competitor.get('found_seller')}\n"
                f"Location: {competitor.get('found_location')} ✅ Japan\n"
                f"Price: ${competitor.get('found_price', 0):.2f}\n\n"
                f"Total new Japan competitors: {competitor_count}"
            ),
            "color": 0xFF1744,
            "footer": {"text": f"eBay Competitor Monitor | {datetime.now().strftime('%Y-%m-%d %H:%M')}"},
        }]
    }
    return _send_webhook(webhook_url, payload)
