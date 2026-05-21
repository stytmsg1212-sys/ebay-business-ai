"""W149 fulfillment_order_matcher: 仕入確認 (purchase_confirmation_log) と
売却注文 (sales_history) を 1:1 で時系列 FIFO ひも付け.

時系列ガード: fulfillment.confirmed_at >= sales.sold_at
(売却が先、仕入が後 = 無在庫の正しい順序). 売却前の仕入は別物として扱わない.

UNIQUE(purchase_confirmation_log_id) / UNIQUE(sales_history_id) で 1:1 を物理担保.
N:1 / 1:N (返品再仕入等) は本 W スコープ外.
"""

import logging
import sqlite3
from typing import Optional

from monitor.database import get_conn

logger = logging.getLogger(__name__)


def link_unmatched() -> int:
    """過去 fulfillment 全件を未マッチ sales_history と FIFO ひも付け. backfill 用.

    Returns: ひも付け成立件数.
    """
    count = 0
    with get_conn() as conn:
        unmatched_fulfillments = conn.execute(
            """SELECT id, ebay_item_id, confirmed_at FROM purchase_confirmation_log
               WHERE COALESCE(fulfillment_kind, 'restock') = 'fulfillment'
                 AND id NOT IN (
                     SELECT purchase_confirmation_log_id FROM fulfillment_order_link
                 )
               ORDER BY confirmed_at ASC, id ASC"""
        ).fetchall()
        for f in unmatched_fulfillments:
            sale = conn.execute(
                """SELECT id FROM sales_history
                   WHERE ebay_item_id = ?
                     AND id NOT IN (
                         SELECT sales_history_id FROM fulfillment_order_link
                     )
                     AND sold_at <= ?
                   ORDER BY sold_at ASC, id ASC LIMIT 1""",
                (f["ebay_item_id"], f["confirmed_at"]),
            ).fetchone()
            if sale is None:
                continue
            try:
                conn.execute(
                    """INSERT INTO fulfillment_order_link
                       (purchase_confirmation_log_id, sales_history_id, match_method)
                       VALUES (?, ?, 'batch')""",
                    (f["id"], sale["id"]),
                )
                count += 1
            except sqlite3.IntegrityError:
                logger.warning(
                    "link_unmatched UNIQUE skip: pcl=%s sale=%s",
                    f["id"], sale["id"],
                )
    return count


def link_one_by_sale(sale_id: int) -> Optional[int]:
    """polling 直後の sale_id に対応する最古 unmatched fulfillment をひも付け.

    Returns: fulfillment (purchase_confirmation_log) id if matched else None.
    """
    with get_conn() as conn:
        sale = conn.execute(
            """SELECT id, ebay_item_id, sold_at FROM sales_history
               WHERE id = ?
                 AND id NOT IN (
                     SELECT sales_history_id FROM fulfillment_order_link
                 )""",
            (sale_id,),
        ).fetchone()
        if sale is None:
            return None
        f = conn.execute(
            """SELECT id FROM purchase_confirmation_log
               WHERE ebay_item_id = ?
                 AND COALESCE(fulfillment_kind, 'restock') = 'fulfillment'
                 AND confirmed_at >= ?
                 AND id NOT IN (
                     SELECT purchase_confirmation_log_id FROM fulfillment_order_link
                 )
               ORDER BY confirmed_at ASC, id ASC LIMIT 1""",
            (sale["ebay_item_id"], sale["sold_at"]),
        ).fetchone()
        if f is None:
            return None
        try:
            conn.execute(
                """INSERT INTO fulfillment_order_link
                   (purchase_confirmation_log_id, sales_history_id, match_method)
                   VALUES (?, ?, 'realtime')""",
                (f["id"], sale_id),
            )
            return f["id"]
        except sqlite3.IntegrityError:
            logger.warning(
                "link_one_by_sale UNIQUE skip: pcl=%s sale=%s",
                f["id"], sale_id,
            )
            return None


def link_one(ebay_item_id: str) -> Optional[dict]:
    """confirm_purchase 末尾用. 同 ebay_item_id の最古 unmatched fulfillment + sale をひも付け.

    Returns:
        {'purchase_confirmation_log_id': pcl_id, 'sale_id': sale_id, 'ebay_order_id': str}
        if matched else None.
    """
    with get_conn() as conn:
        f = conn.execute(
            """SELECT id, confirmed_at FROM purchase_confirmation_log
               WHERE ebay_item_id = ?
                 AND COALESCE(fulfillment_kind, 'restock') = 'fulfillment'
                 AND id NOT IN (
                     SELECT purchase_confirmation_log_id FROM fulfillment_order_link
                 )
               ORDER BY confirmed_at ASC, id ASC LIMIT 1""",
            (ebay_item_id,),
        ).fetchone()
        if f is None:
            return None
        sale = conn.execute(
            """SELECT id, ebay_order_id FROM sales_history
               WHERE ebay_item_id = ?
                 AND id NOT IN (
                     SELECT sales_history_id FROM fulfillment_order_link
                 )
                 AND sold_at <= ?
               ORDER BY sold_at ASC, id ASC LIMIT 1""",
            (ebay_item_id, f["confirmed_at"]),
        ).fetchone()
        if sale is None:
            return None
        try:
            conn.execute(
                """INSERT INTO fulfillment_order_link
                   (purchase_confirmation_log_id, sales_history_id, match_method)
                   VALUES (?, ?, 'realtime')""",
                (f["id"], sale["id"]),
            )
            return {
                "purchase_confirmation_log_id": f["id"],
                "sale_id": sale["id"],
                "ebay_order_id": sale["ebay_order_id"],
            }
        except sqlite3.IntegrityError:
            logger.warning(
                "link_one UNIQUE skip: pcl=%s sale=%s",
                f["id"], sale["id"],
            )
            return None
