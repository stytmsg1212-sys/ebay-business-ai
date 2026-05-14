"""
eBay競合商品監視システム
- Japan セラーのみをライバル対象
- 新規出品の自動検知
- Discord通知
"""
import logging
from datetime import datetime
from typing import Optional

from .ebay_client import get_active_listings, filter_items_with_sku
from .database import (
    init_db, add_new_competitor_alert, get_japan_competitor_alerts,
    get_all_competitor_alerts, update_alert_action, mark_alert_as_notified,
    get_competitors_for_item, add_competitor_product,
)

logger = logging.getLogger(__name__)


def is_japan_seller(location_str: str) -> bool:
    """
    セラーロケーションが Japan かどうかを判定

    Examples:
        is_japan_seller("Japan") -> True
        is_japan_seller("Tokyo, Japan") -> True
        is_japan_seller("USA") -> False
    """
    if not location_str:
        return False

    japan_keywords = ["Japan", "JP", "JPN", "Tokyo", "Osaka", "Kyoto"]
    location_lower = location_str.lower()

    return any(kw.lower() in location_lower for kw in japan_keywords)


def search_competitors_by_keyword(keyword: str, our_item_id: str,
                                  app_id: str, dev_id: str, cert_id: str,
                                  user_token: str) -> dict:
    """
    キーワード検索で競合商品を検索
    Japan セラーのみをライバルとして検知

    Returns: {japan_competitors, other_sellers}
    """
    init_db()

    japan_competitors = []
    other_sellers = []

    try:
        # eBay API で検索（概念的。実装時は actual search API を使用）
        logger.info(f"Searching competitors for keyword: {keyword}")

        # Note: 実装時は eBay Search API (shopping.finding service) を使用
        # ここではシミュレーション構造を示す

        # listings = search_ebay_api(keyword)
        # for listing in listings:
        #     item = {
        #         'item_id': listing['itemId'][0],
        #         'seller': listing['sellerInfo'][0]['sellerUserName'][0],
        #         'location': listing['sellerInfo'][0]['sellerItemRevation'][0],
        #         'price': float(listing['sellingStatus'][0]['currentPrice'][0]['__value__']),
        #         'post_time': listing['listingInfo'][0]['startTime'][0]
        #     }

        # if is_japan_seller(item['location']):
        #     japan_competitors.append(item)
        # else:
        #     other_sellers.append(item)

    except Exception as e:
        logger.error(f"Error searching competitors: {e}")

    return {
        'japan_competitors': japan_competitors,
        'other_sellers': other_sellers
    }


def detect_new_competitors(our_item_id: str, keyword: str,
                          app_id: str, dev_id: str, cert_id: str,
                          user_token: str) -> dict:
    """
    新規ライバルセラーを検知
    Japan セラーのみ通知対象

    Returns: {japan_alerts, other_alerts, notified_count}
    """
    init_db()

    result = {
        'japan_alerts': [],
        'other_alerts': [],
        'notified_count': 0,
        'errors': 0
    }

    try:
        # 検索実行
        search_result = search_competitors_by_keyword(
            keyword, our_item_id, app_id, dev_id, cert_id, user_token
        )

        # Japan セラーのアラートを記録
        for competitor in search_result['japan_competitors']:
            try:
                alert_id = add_new_competitor_alert(
                    our_item_id=our_item_id,
                    keyword=keyword,
                    found_item_id=competitor['item_id'],
                    found_seller=competitor['seller'],
                    found_location=competitor['location'],
                    found_price=competitor['price'],
                    is_japan_seller=1
                )

                result['japan_alerts'].append({
                    'alert_id': alert_id,
                    'item_id': competitor['item_id'],
                    'seller': competitor['seller'],
                    'location': competitor['location'],
                    'price': competitor['price']
                })

                logger.info(f"New Japan competitor detected: {competitor['seller']} (Item {competitor['item_id']})")
                result['notified_count'] += 1

            except Exception as e:
                logger.warning(f"Failed to record alert: {e}")
                result['errors'] += 1

        # Japan以外のセラーもログに記録（参考情報）
        for seller in search_result['other_sellers']:
            try:
                alert_id = add_new_competitor_alert(
                    our_item_id=our_item_id,
                    keyword=keyword,
                    found_item_id=seller['item_id'],
                    found_seller=seller['seller'],
                    found_location=seller['location'],
                    found_price=seller['price'],
                    is_japan_seller=0
                )

                result['other_alerts'].append({
                    'alert_id': alert_id,
                    'item_id': seller['item_id'],
                    'seller': seller['seller'],
                    'location': seller['location'],
                    'price': seller['price']
                })

                logger.debug(f"Other seller found (not Japan): {seller['seller']} ({seller['location']})")

            except Exception as e:
                logger.warning(f"Failed to record non-Japan alert: {e}")
                result['errors'] += 1

    except Exception as e:
        logger.error(f"Error detecting competitors: {e}")
        result['errors'] += 1

    return result


def get_pending_japan_alerts() -> list[dict]:
    """
    未処理の Japan セラーアラートを取得
    これらは Discord で通知すべきもの
    """
    alerts = get_japan_competitor_alerts(action="pending")
    return alerts


def register_competitor_from_alert(alert_id: int, our_item_id: str,
                                   price_rule: str = "competitor - 0.01",
                                   min_price: float = 0.0,
                                   max_discount: float = 10.0) -> bool:
    """
    アラートから競合商品を登録
    """
    init_db()

    try:
        alert = get_japan_competitor_alerts(action="pending")
        if not alert:
            return False

        alert = alert[0]

        comp_id = add_competitor_product(
            our_item_id=our_item_id,
            competitor_item_id=alert['found_item_id'],
            competitor_seller=alert['found_seller'],
            seller_location=alert['found_location'],
            price_rule=price_rule,
            min_price=min_price,
            max_discount=max_discount,
            our_sku=our_item_id
        )

        update_alert_action(alert_id, "registered")
        logger.info(f"Competitor registered: {alert['found_seller']} (Item {alert['found_item_id']})")
        return True

    except Exception as e:
        logger.error(f"Error registering competitor: {e}")
        return False


def ignore_competitor_alert(alert_id: int) -> bool:
    """
    アラートを「無視」として処理
    """
    try:
        update_alert_action(alert_id, "ignored")
        logger.info(f"Alert {alert_id} marked as ignored")
        return True
    except Exception as e:
        logger.error(f"Error ignoring alert: {e}")
        return False


def get_competition_report() -> dict:
    """
    現在の競合状況をレポート
    """
    japan_alerts = get_japan_competitor_alerts(action="pending")
    other_alerts = get_all_competitor_alerts()

    japan_count = len([a for a in other_alerts if a['is_japan_seller'] == 1])
    other_count = len([a for a in other_alerts if a['is_japan_seller'] == 0])

    return {
        'pending_japan_alerts': len(japan_alerts),
        'total_japan_alerts': japan_count,
        'total_other_alerts': other_count,
        'recent_alerts': get_all_competitor_alerts(limit=5)
    }
