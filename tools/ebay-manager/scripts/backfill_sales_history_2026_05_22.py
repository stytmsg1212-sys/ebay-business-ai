"""W149 (2026-05-22) one-shot backfill: 過去 89 日の eBay 売却注文を GetOrders で
取得 → sales_history populate → fulfillment 一括 matching.

設計書: .company/engineering/docs/2026-05-21-W149-ebay-orders-fetch-fulfillment-link-design.md v2

冪等性: sales_history.ebay_order_id UNIQUE INDEX で再実行で重複 INSERT を物理排除.

**eBay API 制約 (2026-05-22 dry-run で発覚)**: GetOrders CreateTimeFrom は
**現在から 90 日以内** という上限あり (eBay error: "Orders older than 90 days
cannot be retrieved"). 設計書 v2 の「2026/1/1 ~ 現在 = 5 ヶ月」は API では不可能.
本 script は過去 89 日 (1 日 buffer) を 1 chunk で取得. それより古い注文は
別経路 (Gmail 経由の旧 task_sales_tracking、本 W で OFF 化) で歴史的取得不能.
過去 90 日 sold で並び順は実用的 (sold rate 一定なら ~120 件 estimate).

使い方:
  python scripts/backfill_sales_history_2026_05_22.py --dry-run   # API 呼ぶが INSERT しない
  python scripts/backfill_sales_history_2026_05_22.py             # 本実行
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# project root を sys.path に追加 (script 直接実行用)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 89  # eBay API 90 日上限の 1 日 buffer
SLEEP_BETWEEN_CHUNKS_SEC = 2.0


def main(dry_run: bool = False) -> int:
    """Returns: 0 = success, 1 = credentials missing, 2 = some chunks failed."""
    from monitor.credentials import get_ebay_credentials, ebay_credentials_ok
    from monitor.database import add_sale, init_db
    from monitor.ebay_client import get_orders
    from monitor.fulfillment_order_matcher import link_unmatched

    init_db()  # v47 migration (sales_history.ebay_order_id 列 + fulfillment_order_link)

    creds = get_ebay_credentials()
    if not ebay_credentials_ok(creds):
        logger.error("eBay credentials not configured (.env)")
        return 1
    app_id = creds["app_id"]
    dev_id = creds["dev_id"]
    cert_id = creds["cert_id"]
    user_token = creds["user_token"]

    # eBay GetOrders は CreateTimeFrom が現在から 90 日以内 (API 制約)
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=LOOKBACK_DAYS)
    chunks: list[tuple[datetime, datetime]] = [(start, end)]
    print(f"[backfill] {len(chunks)} chunk: "
          f"{[f'{c[0].date()}~{c[1].date()}' for c in chunks]} (LOOKBACK_DAYS={LOOKBACK_DAYS})",
          flush=True)
    print(f"[backfill] dry_run={dry_run}", flush=True)

    total_fetched = 0       # 新規 INSERT 成立
    total_skipped_dup = 0   # UNIQUE 衝突 skip (再実行時に発生)
    total_failed_chunks = 0
    total_orders_seen = 0

    for (frm, to) in chunks:
        print(f"[backfill] chunk {frm.date()} ~ {to.date()} ...", flush=True)
        result = get_orders(
            app_id, dev_id, cert_id, user_token,
            create_time_from=frm, create_time_to=to,
        )
        if not result.get("success"):
            logger.error(f"chunk {frm.date()}~{to.date()} failed: "
                         f"{result.get('message')}")
            total_failed_chunks += 1
            time.sleep(SLEEP_BETWEEN_CHUNKS_SEC)
            continue

        orders = result.get("orders") or []  # transaction flatten list
        total_orders_seen += len(orders)

        # order_id で group → qty 比按分 (W149 §5 設計書 v2 準拠).
        # shipping は order レベル値が全 txn にコピーされている (ebay_client.py L1481-1512).
        orders_by_id: dict[str, list] = {}
        for txn in orders:
            oid = txn.get("order_id")
            if oid:
                orders_by_id.setdefault(oid, []).append(txn)

        for order_id, txns in orders_by_id.items():
            # HIGH-1 (code-reviewer Phase D, 2026-05-22): paid_time 空 (未払い) は skip.
            # sold_at='' で INSERT すると並び順破壊 + matcher 時系列ガード崩壊.
            paid_time = txns[0].get("paid_time") or ""
            if not paid_time:
                continue
            total_qty = sum(int(t.get("qty") or 1) for t in txns) or 1
            order_shipping = float(txns[0].get("shipping_usd") or 0.0)
            for txn in txns:
                qty = int(txn.get("qty") or 1)
                ship_share = (order_shipping * qty / total_qty) if total_qty > 0 else 0.0
                if dry_run:
                    total_fetched += 1
                    continue
                sid = add_sale(
                    ebay_item_id=txn.get("ebay_item_id") or "",
                    sku=txn.get("sku") or "",
                    title=txn.get("title") or "",
                    sold_price_usd=float(txn.get("item_price_usd") or 0.0),
                    sold_at=paid_time,
                    buyer_country=txn.get("buyer_country") or "",
                    shipping_cost_usd=ship_share,
                    ebay_fee_usd=0.0,  # 別 W で取得
                    ebay_order_id=order_id,
                )
                if sid > 0:
                    total_fetched += 1
                elif sid == 0:
                    total_skipped_dup += 1

        time.sleep(SLEEP_BETWEEN_CHUNKS_SEC)

    if dry_run:
        print(
            f"[backfill DRY-RUN] orders_seen={total_orders_seen} "
            f"would_fetch={total_fetched} failed_chunks={total_failed_chunks}",
            flush=True,
        )
        return 0 if total_failed_chunks == 0 else 2

    # 全 fulfillment と sales を FIFO 一括ひも付け
    link_count = link_unmatched()
    print(
        f"[backfill] orders_seen={total_orders_seen} "
        f"recorded={total_fetched} skipped_dup={total_skipped_dup} "
        f"failed_chunks={total_failed_chunks} fulfillment_linked={link_count}",
        flush=True,
    )
    return 0 if total_failed_chunks == 0 else 2


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    dry = "--dry-run" in sys.argv
    sys.exit(main(dry_run=dry))
