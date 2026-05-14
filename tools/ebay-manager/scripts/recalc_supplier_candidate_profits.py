"""既存 supplier_candidates を再計算して、利益の出ないものを削除する.

2026-04-23 Q1-Q6 決定事項に基づく:
  Q1: 採用判定 = check_supplier_candidate_profitable (600円 + スライド率)
  Q2: DB 物理削除
  Q3: K1 (price None) 削除 / K3 (alt_listing) 残す
  Q4: 一括再計算 (Claude 再スクレイプ不要、DB 内のデータのみで再計算)

実行方法:
    python scripts/recalc_supplier_candidate_profits.py [--dry-run]

dry-run 時は削除予定の件数だけ報告、実際の DB 変更はしない。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calculator import (  # noqa: E402
    check_supplier_candidate_profitable, load_settings,
)
from monitor.database import get_conn  # noqa: E402
from tasks.task_supplier_candidate_search import (  # noqa: E402
    _estimate_profit_for_candidate,
)


def _fetch_listing_by_item_id(conn, ebay_item_id: str) -> dict:
    """ebay_listings から ebay_item_id 紐付きの listing を dict で取得.

    LIMIT 1 不要根拠: ebay_listings.ebay_item_id は UNIQUE 制約 (monitor/database.py:407).
    sku-rules.md 準拠 (W68 Iteration 2 で SKU lookup から移行).
    """
    cols = ["sku", "current_price", "weight_g", "length_cm", "width_cm", "height_cm"]
    cur = conn.execute(
        f"""SELECT {', '.join(cols)}
           FROM ebay_listings WHERE ebay_item_id = ?""",
        (ebay_item_id,),
    )
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else {}


def recalc_all(dry_run: bool = False) -> dict:
    """全候補を再計算して不採用を削除. 結果サマリ返却."""
    settings = load_settings()
    stats = {
        "total": 0,
        "recalculated": 0,
        "still_profitable": 0,
        "now_unprofitable_deleted": 0,
        "price_none_deleted": 0,
        "alt_listing_kept": 0,
        "already_rejected_kept": 0,
        "error_kept": 0,
    }

    with get_conn() as conn:
        conn.row_factory = None
        rows = conn.execute(
            """SELECT id, sku, candidate_price_jpy, alt_listing_possible, status, ebay_item_id
               FROM supplier_candidates"""
        ).fetchall()
        stats["total"] = len(rows)

        to_delete: list[int] = []
        to_update: list[tuple[int, float, int]] = []  # (id, profit_jpy, profitable)

        for row in rows:
            cid = row[0]
            sku = row[1]
            price_jpy = row[2]
            alt_listing = int(row[3] or 0)
            status = row[4] or "pending"
            eid = row[5]  # W68 Iteration 2: ebay_item_id 直接取得 (sku-rules.md 準拠)

            # alt_listing_possible=1 は Q3 B で残す (計算対象外)
            if alt_listing:
                stats["alt_listing_kept"] += 1
                continue

            # 既に rejected/applied のものは触らない (ユーザー判断履歴保全)
            if status in ("rejected", "applied"):
                stats["already_rejected_kept"] += 1
                continue

            # price が None なら計算不能 → K1 削除対象
            if price_jpy is None or price_jpy <= 0:
                to_delete.append(cid)
                stats["price_none_deleted"] += 1
                continue

            # listing を取得して再計算
            listing = _fetch_listing_by_item_id(conn, eid)
            if not listing:
                # listing 不在 = K1 類似 (対応する eBay 出品なし) → 削除
                to_delete.append(cid)
                stats["price_none_deleted"] += 1
                continue

            try:
                profit_jpy = _estimate_profit_for_candidate(
                    listing=listing,
                    purchase_yen=int(price_jpy),
                    settings=settings,
                )
            except Exception as e:  # noqa: BLE001
                print(f"  [WARN] calc failed id={cid} sku={sku}: {e}")
                stats["error_kept"] += 1
                continue

            stats["recalculated"] += 1

            if profit_jpy is None:
                to_delete.append(cid)
                stats["price_none_deleted"] += 1
                continue

            ok, _breakdown = check_supplier_candidate_profitable(
                profit_with_refund=profit_jpy,
                purchase_yen=int(price_jpy),
            )

            if ok:
                to_update.append((cid, float(profit_jpy), 1))
                stats["still_profitable"] += 1
            else:
                to_delete.append(cid)
                stats["now_unprofitable_deleted"] += 1

        if dry_run:
            print("=== DRY RUN ===")
            print(f"Total: {stats['total']}")
            print(f"Would DELETE: {len(to_delete)}")
            print(f"  - price None / no listing: {stats['price_none_deleted']}")
            print(f"  - now unprofitable: {stats['now_unprofitable_deleted']}")
            print(f"Would UPDATE profit: {len(to_update)}")
            print(f"Kept (alt_listing): {stats['alt_listing_kept']}")
            print(f"Kept (rejected/applied): {stats['already_rejected_kept']}")
            print(f"Kept (error): {stats['error_kept']}")
            return stats

        # 実行
        if to_delete:
            conn.executemany(
                "DELETE FROM supplier_candidates WHERE id = ?",
                [(cid,) for cid in to_delete],
            )
        if to_update:
            conn.executemany(
                "UPDATE supplier_candidates SET profit_jpy = ?, profitable = ? WHERE id = ?",
                [(p, pf, cid) for cid, p, pf in to_update],
            )
        conn.commit()

    print("=== DONE ===")
    print(f"Total: {stats['total']}")
    print(f"Deleted: {len(to_delete)} (price_none={stats['price_none_deleted']}, "
          f"unprofitable={stats['now_unprofitable_deleted']})")
    print(f"Updated profit: {len(to_update)}")
    print(f"Kept alt_listing: {stats['alt_listing_kept']}")
    print(f"Kept rejected/applied: {stats['already_rejected_kept']}")
    print(f"Kept error: {stats['error_kept']}")
    return stats


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    recalc_all(dry_run=dry)
