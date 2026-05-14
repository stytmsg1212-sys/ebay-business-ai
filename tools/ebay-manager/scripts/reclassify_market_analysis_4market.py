"""W7-A 候補 C: 既存 market_analysis 行を 4 区分判定で再分類 (one-shot).

生 data (us_count, total_sold) は変更せず、primary_market 列のみ更新.

冪等: 同じ判定結果になる場合は UPDATE skip.

実行:
  cd tools/ebay-manager
  python scripts/reclassify_market_analysis_4market.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn
from monitor.terapeak_scraper import _judge_primary_market


def main() -> int:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, sku, ebay_item_id, total_sold, us_count, non_us_count, primary_market
            FROM market_analysis
        """).fetchall()

        n_total = len(rows)
        n_changed = 0
        n_unchanged = 0

        for r in rows:
            row_id = r[0]
            us = r[4] if r[4] is not None else 0
            non_us = r[5] if r[5] is not None else 0
            old_market = r[6]

            new_market, new_reason = _judge_primary_market(us, non_us)

            if old_market == new_market:
                n_unchanged += 1
                continue

            conn.execute(
                """UPDATE market_analysis
                   SET primary_market = ?, primary_market_reason = ?
                   WHERE id = ?""",
                (new_market, new_reason, row_id),
            )
            n_changed += 1
            print(
                f"  id={row_id} sku={r[1]} eid={r[2]} "
                f"total={r[3]} us={us} | {old_market} -> {new_market}"
            )

        print()
        print(f"=== 完了 ===")
        print(f"  total: {n_total}")
        print(f"  changed: {n_changed}")
        print(f"  unchanged: {n_unchanged}")
        print()

        # 4 区分集計
        agg = conn.execute("""
            SELECT primary_market, COUNT(*)
            FROM market_analysis
            GROUP BY primary_market
            ORDER BY COUNT(*) DESC
        """).fetchall()
        print("4 区分集計 (UPDATE 後):")
        for market, count in agg:
            print(f"  {market}: {count}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
