#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W119 Round 2: 2026-05-10〜11 daily_relist 由来の継承漏れを retrofit する one-shot script.

問題: 2026-05-11 朝に発見された 7 件の新規 listing で search_keyword / primary_market /
      lp_min_price 等が NULL だった. daily_relist の旧実装 (継承列: weight/size/source のみ)
      が原因. W119 ふりかえりで本 issue を発見、`inherit_listing_on_relist()` で恒久対策済.

本 script は **既に relist 済の 7 件を遡及補完** する. 同様の漏れが再発した場合の retry も可能.

実行手順 (`.claude/rules/db-migration-rules.md` Q2 準拠):
  1. dry-run で snapshot 確認
  2. apply で実行
  3. 結果 SELECT で verify
  4. 24h 以内に retrospective code-reviewer 投入

使い方:
  python scripts/retrofit_w119_relist_inheritance.py --dry-run
  python scripts/retrofit_w119_relist_inheritance.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn  # noqa: E402


# 継承対象列 (daily_relist と同じセット)
INHERIT_COLUMNS = [
    "search_keyword",
    "search_keyword_source",
    "search_keyword_generated_at",
    "purchase_yen",
    "lp_min_price",
    "lp_breakeven_usd",
    "primary_market",
    "us_buyer_ratio",
    "market_analysis_at",
    "market_sample_size",
]


def find_retrofit_targets() -> list[dict]:
    """relist_history で new_item_id が存在し、何らかの継承列が NULL の listing."""
    cols = ", ".join(f"n.{c} AS new_{c}" for c in INHERIT_COLUMNS)
    old_cols = ", ".join(f"o.{c} AS old_{c}" for c in INHERIT_COLUMNS)
    sql = f"""
        SELECT rh.old_item_id, rh.new_item_id, n.title, {cols}, {old_cols}
        FROM relist_history rh
        JOIN ebay_listings n ON n.ebay_item_id = rh.new_item_id
        JOIN ebay_listings o ON o.ebay_item_id = rh.old_item_id
        WHERE rh.success = 1
          AND (n.is_ended IS NULL OR n.is_ended = 0)
          AND (
                (n.search_keyword IS NULL AND o.search_keyword IS NOT NULL)
             OR (n.purchase_yen IS NULL AND o.purchase_yen IS NOT NULL)
             OR (n.lp_min_price IS NULL AND o.lp_min_price IS NOT NULL)
             OR (n.lp_breakeven_usd IS NULL AND o.lp_breakeven_usd IS NOT NULL)
             OR (n.primary_market IS NULL AND o.primary_market IS NOT NULL)
          )
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def find_orphaned_competitors() -> list[dict]:
    """OLD listing にだけ紐づく active competitor (NEW にすでに同じ ID があれば skip)."""
    sql = """
        SELECT rh.old_item_id, rh.new_item_id,
               cp.competitor_item_id, cp.is_active
        FROM relist_history rh
        JOIN competitor_products cp ON cp.our_item_id = rh.old_item_id
        WHERE rh.success = 1
          AND cp.is_active = 1
          AND NOT EXISTS (
              SELECT 1 FROM competitor_products cp2
              WHERE cp2.our_item_id = rh.new_item_id
                AND cp2.competitor_item_id = cp.competitor_item_id
          )
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def apply_retrofit(targets: list[dict], orphans: list[dict]) -> dict:
    """1 トランザクションで全 retrofit を実行.

    H3 fix (2026-05-11 code-reviewer): TEXT 列の空文字列 '' は NULL と見做して上書きすべき.
    COALESCE(col, ?) は '' を non-NULL 扱いするため、NULLIF(col, '') と組合せて
    「NULL または空文字列なら OLD で上書き」セマンティクスを実現.
    """
    updated_listings = 0
    updated_competitors = 0
    set_clauses = ", ".join(
        f"{c} = COALESCE(NULLIF({c}, ''), ?)" for c in INHERIT_COLUMNS
    )
    sql_update_listing = (
        f"UPDATE ebay_listings SET {set_clauses} WHERE ebay_item_id = ?"
    )

    with get_conn() as conn:
        for t in targets:
            values = tuple(t[f"old_{c}"] for c in INHERIT_COLUMNS) + (t["new_item_id"],)
            cur = conn.execute(sql_update_listing, values)
            updated_listings += cur.rowcount

        # competitor_products: OLD → NEW に移動 (NEW 側に既に同 ID あれば skip = NOT EXISTS 句で対象外)
        for orphan in orphans:
            cur = conn.execute(
                "UPDATE competitor_products SET our_item_id = ? "
                "WHERE our_item_id = ? AND competitor_item_id = ? AND is_active = 1",
                (orphan["new_item_id"], orphan["old_item_id"], orphan["competitor_item_id"]),
            )
            updated_competitors += cur.rowcount

    return {
        "updated_listings": updated_listings,
        "updated_competitors": updated_competitors,
    }


def print_summary(targets: list[dict], orphans: list[dict]) -> None:
    print(f"=== 継承漏れ listing: {len(targets)} 件 ===")
    for t in targets:
        new_id = t["new_item_id"]
        old_id = t["old_item_id"]
        missing = [c for c in INHERIT_COLUMNS
                   if t[f"new_{c}"] is None and t[f"old_{c}"] is not None]
        print(f"  {new_id[-6:]} (← {old_id[-6:]}) | 補完予定: {missing}")
        # title preview
        title = (t.get("title") or "")[:50]
        print(f"    title: {title!r}")

    print(f"\n=== 孤立 competitor_products (OLD → NEW 移動): {len(orphans)} 件 ===")
    for o in orphans:
        print(f"  {o['old_item_id'][-6:]} → {o['new_item_id'][-6:]} | competitor={o['competitor_item_id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="実際に UPDATE を実行 (省略時は dry-run)")
    args = parser.parse_args()

    targets = find_retrofit_targets()
    orphans = find_orphaned_competitors()

    print_summary(targets, orphans)

    if not args.apply:
        print("\n--- dry-run (UPDATE 未実行). --apply で実行 ---")
        return

    if not targets and not orphans:
        print("\n対象なし、変更不要.")
        return

    result = apply_retrofit(targets, orphans)
    print(f"\n=== 実行結果 ===")
    print(f"  ebay_listings UPDATE: {result['updated_listings']} 件")
    print(f"  competitor_products UPDATE: {result['updated_competitors']} 件")

    # verify (再度 query)
    remaining = find_retrofit_targets()
    remaining_orphans = find_orphaned_competitors()
    print(f"\n=== verify (再 query) ===")
    print(f"  残り継承漏れ listing: {len(remaining)} 件")
    print(f"  残り孤立 competitor: {len(remaining_orphans)} 件")


if __name__ == "__main__":
    main()
