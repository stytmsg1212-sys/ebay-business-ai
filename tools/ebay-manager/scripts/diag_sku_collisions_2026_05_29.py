"""READ-ONLY 診断 (W185 H3 設計, 2026-05-29): supplier_candidates の
同一 (ebay_item_id, candidate_url) で sku が異なる衝突 24 グループの中身を dump.

目的: UNIQUE(sku, candidate_url) → UNIQUE(ebay_item_id, candidate_url) 化時の
dedup 統合ルール決定のため、各衝突グループの status / match_score / sku prefix を確認.
- status に accepted/applied (user 判断/反映済) が混在していないか
- sku が stock*/ebay* prefix を跨いでいないか (有/無在庫種別の取り違えリスク)
本スクリプトは SELECT のみ. 一切書き込まない. blueprint §3 の事前調査に相当.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn  # noqa: E402


def main() -> None:
    with get_conn() as c:
        c.row_factory = None
        rows = c.execute(
            "SELECT ebay_item_id, candidate_url, "
            "GROUP_CONCAT(id) AS ids, "
            "GROUP_CONCAT(sku) AS skus, "
            "GROUP_CONCAT(status) AS statuses, "
            "GROUP_CONCAT(COALESCE(match_score, -1)) AS scores, "
            "GROUP_CONCAT(created_at) AS created_ats "
            "FROM supplier_candidates "
            "WHERE ebay_item_id IS NOT NULL AND ebay_item_id != '' "
            "GROUP BY ebay_item_id, candidate_url "
            "HAVING COUNT(DISTINCT sku) > 1 "
            "ORDER BY ebay_item_id"
        ).fetchall()

        print(f"=== 衝突グループ数: {len(rows)} ===")
        non_pending_set = {"accepted", "applied", "rejected"}
        risky_groups = 0
        prefix_mixed = 0
        for i, (eid, url, ids, skus, statuses, scores, created) in enumerate(rows, 1):
            st_list = (statuses or "").split(",")
            non_pending = [s for s in st_list if s in non_pending_set]
            distinct_non_pending = set(non_pending)
            risky = len(distinct_non_pending) >= 2 or (
                len(non_pending) >= 2 and len(distinct_non_pending) >= 1
            )
            # prefix 跨ぎ判定 (stock* と ebay* が同グループに混在)
            sku_list = (skus or "").split(",")
            has_stock = any(s.startswith("stock") for s in sku_list)
            has_ebay = any(s.startswith("ebay") for s in sku_list)
            mixed = has_stock and has_ebay
            if risky:
                risky_groups += 1
            if mixed:
                prefix_mixed += 1
            flag = ""
            if risky:
                flag += " [RISKY: non-pending 複数]"
            if mixed:
                flag += " [PREFIX-MIXED: stock/ebay 跨ぎ]"
            print(f"\n--- group {i} eid={eid}{flag}")
            print(f"  url={url}")
            print(f"  ids={ids}")
            print(f"  skus={skus}")
            print(f"  statuses={statuses}")
            print(f"  scores={scores}")
            print(f"  created_ats={created}")

        print(f"\n=== サマリ ===")
        print(f"衝突グループ総数: {len(rows)}")
        print(f"RISKY (non-pending 複数 = 自動 abort 対象): {risky_groups}")
        print(f"PREFIX-MIXED (stock/ebay 跨ぎ = sku 種別取り違えリスク): {prefix_mixed}")
        # status 分布 (全衝突行)
        dist = c.execute(
            "SELECT status, COUNT(*) FROM supplier_candidates "
            "WHERE (ebay_item_id, candidate_url) IN ("
            "  SELECT ebay_item_id, candidate_url FROM supplier_candidates "
            "  WHERE ebay_item_id IS NOT NULL AND ebay_item_id != '' "
            "  GROUP BY ebay_item_id, candidate_url HAVING COUNT(DISTINCT sku) > 1"
            ") GROUP BY status ORDER BY COUNT(*) DESC"
        ).fetchall()
        print("衝突行の status 分布:")
        for status, n in dist:
            print(f"  {status!r}: {n}")


if __name__ == "__main__":
    main()
