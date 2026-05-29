"""READ-ONLY 診断 (H2/H3 設計判断用, 2026-05-29 Opus 4.8 総チェック).

目的:
- H2: ebay_listings.FOREIGN KEY(sku) が意味論的に妥当か (sku → 複数 ebay_item_id があれば FK は誤り)
- H3: supplier_candidates.UNIQUE(sku, candidate_url) を ebay_item_id ベースへ変える時のリスク定量化
  (NULL ebay_item_id 件数 / sku 跨ぎ ebay_item_id / 現行 dedup が ebay_item_id 化で壊れる行)
本スクリプトは SELECT のみ. 一切書き込まない.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn  # noqa: E402


def main() -> None:
    with get_conn() as c:
        c.row_factory = None
        print("=== H2: ebay_listings ===")
        total = c.execute("SELECT COUNT(*) FROM ebay_listings").fetchone()[0]
        print(f"total rows: {total}")
        # sku が複数 ebay_item_id にまたがるか (FK on sku が誤りの証拠).
        # 注: 以下の GROUP BY sku は「FK が意味論的に誤り」を実証する READ-ONLY 診断専用.
        # 本番ロジックでの SKU 集約ではない (sku-rules.md の GROUP BY sku 禁止は本番識別/集約が対象).
        shared = c.execute(
            "SELECT sku, COUNT(DISTINCT ebay_item_id) AS n "
            "FROM ebay_listings GROUP BY sku HAVING n > 1 ORDER BY n DESC LIMIT 10"
        ).fetchall()
        print(f"sku が複数 ebay_item_id を持つ sku 数(top10): {len(shared)}")
        for sku, n in shared:
            print(f"  sku={sku!r} -> {n} 個の ebay_item_id")
        # FK 参照先 monitored_items に存在しない sku を持つ listing (FK が enforced なら違反)
        orphan = c.execute(
            "SELECT COUNT(*) FROM ebay_listings el "
            "WHERE el.sku NOT IN (SELECT sku FROM monitored_items WHERE sku IS NOT NULL)"
        ).fetchone()[0]
        print(f"monitored_items.sku に存在しない sku の listing 数: {orphan} "
              "(FK 未強制なので現状は無害)")

        print("\n=== H3: supplier_candidates ===")
        sc_total = c.execute("SELECT COUNT(*) FROM supplier_candidates").fetchone()[0]
        print(f"total rows: {sc_total}")
        null_eid = c.execute(
            "SELECT COUNT(*) FROM supplier_candidates "
            "WHERE ebay_item_id IS NULL OR ebay_item_id = ''"
        ).fetchone()[0]
        print(f"ebay_item_id が NULL/空 の行: {null_eid} "
              "(UNIQUE を ebay_item_id 化すると NULL は SQLite で全て distinct = dedup 効かない)")
        # 現 dedup キー (sku, candidate_url) の重複実数 (INSERT OR IGNORE が効いている証拠)
        dup_sku = c.execute(
            "SELECT COUNT(*) FROM (SELECT sku, candidate_url, COUNT(*) n "
            "FROM supplier_candidates GROUP BY sku, candidate_url HAVING n > 1)"
        ).fetchone()[0]
        print(f"(sku, candidate_url) が重複しているグループ数: {dup_sku} "
              "(現 UNIQUE があるので 0 が正常)")
        # ebay_item_id 化した時に衝突が変わる行: 同一 (ebay_item_id, candidate_url) で sku が違う
        eid_collide = c.execute(
            "SELECT COUNT(*) FROM (SELECT ebay_item_id, candidate_url, "
            "COUNT(DISTINCT sku) n FROM supplier_candidates "
            "WHERE ebay_item_id IS NOT NULL AND ebay_item_id != '' "
            "GROUP BY ebay_item_id, candidate_url HAVING n > 1)"
        ).fetchone()[0]
        print(f"同一 (ebay_item_id, candidate_url) で sku が複数のグループ: {eid_collide}")
        # 逆: 同一 (sku, candidate_url) で ebay_item_id が複数 (sku が listing 跨ぎの証拠)
        sku_span = c.execute(
            "SELECT COUNT(*) FROM (SELECT sku, candidate_url, "
            "COUNT(DISTINCT ebay_item_id) n FROM supplier_candidates "
            "WHERE ebay_item_id IS NOT NULL AND ebay_item_id != '' "
            "GROUP BY sku, candidate_url HAVING n > 1)"
        ).fetchone()[0]
        print(f"同一 (sku, candidate_url) で ebay_item_id が複数のグループ: {sku_span} "
              "(>0 なら sku が listing 跨ぎ = sku dedup が listing を取り違える証拠)")
        # alt_listing_possible (別SKU機会) で ebay_item_id NULL の件数
        alt_null = c.execute(
            "SELECT COUNT(*) FROM supplier_candidates "
            "WHERE alt_listing_possible = 1 AND (ebay_item_id IS NULL OR ebay_item_id = '')"
        ).fetchone()[0]
        print(f"別SKU機会(alt_listing_possible=1) かつ ebay_item_id NULL: {alt_null}")


if __name__ == "__main__":
    main()
