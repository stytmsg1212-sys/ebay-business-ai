"""W109(1) 一括承認スクリプト (one-shot, 2026-05-09).

W7-A 市場分析で確定分類 (US_only / global_only) かつ ebay_listings.primary_market が NULL
の listing を auto-approve する.

設計議論経緯:
  - Q1 (b) user 採用: US_only + global_only 自動承認、mixed_global は手動承認 (DDP 関税
    判定誤りで赤字直撃リスク高、user 目視必須).
  - H-1 fix (code-reviewer): 抽出条件に primary_market IS NULL 追加で二重承認防止.
  - H-2 fix (code-reviewer): 既存 _bulk_decision 流用でコード重複ゼロ + cascade 排除.
  - H-5 fix (code-reviewer): Q5 完了報告 4 行テンプレを stdout に明示.

Q2 6-step (db-migration-rules.md 準拠):
  1. snapshot JSON で rollback 用に対象 listing を保存
  2. 1 件試行 (即実 UPDATE/INSERT/DELETE)
  3. 残り実行
  4. 再 SELECT で primary_market 遷移確認
  5. 24h 以内に retrospective code-reviewer 投入 (本 script の本番実行後別途)
  6. kill switch (scheduler の market_analysis_refresh) は別途 tasks_enabled で停止可能

実行: python scripts/bulk_approve_market_changes_2026_05_09.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# H-2: 既存 _bulk_decision (UI 経由 SKU cascade 排除済) を流用. reviewer 引数化済.
from tabs.tab_market_strategy import _bulk_decision  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "monitor.db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
TARGET_MARKETS = ("US_only", "global_only")
REVIEWER = "auto_w109_2026-05-09"


def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # 1) 対象抽出 (H-1: primary_market IS NULL で二重承認防止)
    placeholders = ",".join("?" * len(TARGET_MARKETS))
    rows = conn.execute(
        f"""
        SELECT pmc.ebay_item_id, pmc.sku, pmc.proposed_market, pmc.current_market,
               pmc.reason, el.title, el.primary_market AS current_pm
        FROM pending_market_changes pmc
        JOIN ebay_listings el ON pmc.ebay_item_id = el.ebay_item_id
        WHERE pmc.proposed_market IN ({placeholders})
          AND el.primary_market IS NULL
          AND COALESCE(el.is_ended, 0) = 0
        ORDER BY pmc.proposed_market, pmc.ebay_item_id
        """,
        list(TARGET_MARKETS),
    ).fetchall()

    print("=" * 60)
    print("W109(1) 一括承認 — 2026-05-09")
    print("対象: pending US_only / global_only かつ ebay_listings.primary_market IS NULL")
    print("=" * 60)
    print(f"対象 {len(rows)} 件")

    by_market: dict[str, int] = {}
    for r in rows:
        by_market[r["proposed_market"]] = by_market.get(r["proposed_market"], 0) + 1
    for m, n in sorted(by_market.items()):
        print(f"  {m}: {n} 件")

    if len(rows) == 0:
        print("\n対象 0 件、終了。")
        conn.close()
        return

    # Q2 6-step #1: snapshot JSON (rollback 用)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = BACKUP_DIR / f"w109_bulk_approve_snapshot_{ts}.json"
    snapshot = {
        "created_at": datetime.now().isoformat(),
        "purpose": "W109(1) 一括承認 rollback snapshot (Q2 6-step #1)",
        "reviewer": REVIEWER,
        "target_markets": list(TARGET_MARKETS),
        "rows": [dict(r) for r in rows],
    }
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nsnapshot: {snapshot_path}")

    print("\n--- サンプル (先頭 5 件) ---")
    for r in rows[:5]:
        title_short = (r["title"] or "?")[:50]
        sku_short = (r["sku"] or "?")[:25]
        print(f"  {r['ebay_item_id']} {r['proposed_market']:13s} sku={sku_short:25s} {title_short}")
    if len(rows) > 5:
        print(f"  ... ほか {len(rows) - 5} 件")

    # _bulk_decision は内部で別 connection を開くので一旦閉じる
    conn.close()

    # Q2 6-step #2: 1 件試行
    print("\n--- Q2 6-step #2: 1 件試行 ---")
    first_row = rows[0]
    first_eid = first_row["ebay_item_id"]
    first_market = first_row["proposed_market"]
    print(f"対象: {first_eid} → {first_market}")

    _bulk_decision({first_eid}, "approved", reviewer=REVIEWER)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    pm_after = conn.execute(
        "SELECT primary_market FROM ebay_listings WHERE ebay_item_id=?", (first_eid,)
    ).fetchone()
    pmc_after = conn.execute(
        "SELECT COUNT(*) AS cnt FROM pending_market_changes WHERE ebay_item_id=?", (first_eid,)
    ).fetchone()
    msd_after = conn.execute(
        "SELECT COUNT(*) AS cnt FROM market_strategy_decisions "
        "WHERE ebay_item_id=? AND reviewer=?",
        (first_eid, REVIEWER),
    ).fetchone()

    print(f"  ebay_listings.primary_market = {pm_after['primary_market']!r} (期待: {first_market!r})")
    print(f"  pending_market_changes 残数 = {pmc_after['cnt']} (期待: 0)")
    print(f"  market_strategy_decisions ({REVIEWER}) = {msd_after['cnt']} (期待: 1)")

    if pm_after["primary_market"] != first_market:
        print(f"\n!!! 1 件試行失敗: primary_market が期待値と異なる、abort !!!")
        sys.exit(2)
    if pmc_after["cnt"] != 0:
        print(f"\n!!! 1 件試行失敗: pending 残存、abort !!!")
        sys.exit(2)
    if msd_after["cnt"] != 1:
        print(f"\n!!! 1 件試行失敗: decisions 記録なし、abort !!!")
        sys.exit(2)

    print("1 件試行 OK")
    conn.close()

    # Q2 6-step #3: 残り実行
    remaining_eids = {r["ebay_item_id"] for r in rows[1:]}
    print(f"\n--- Q2 6-step #3: 残り {len(remaining_eids)} 件実行 ---")
    if len(remaining_eids) > 0:
        _bulk_decision(remaining_eids, "approved", reviewer=REVIEWER)

    # Q2 6-step #4: 再 SELECT (結果検証)
    print(f"\n--- Q2 6-step #4: 結果検証 ---")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    target_eids = [r["ebay_item_id"] for r in rows]
    target_placeholders = ",".join("?" * len(target_eids))

    final_pm_count = conn.execute(
        f"""SELECT primary_market, COUNT(*) AS cnt FROM ebay_listings
            WHERE ebay_item_id IN ({target_placeholders})
            GROUP BY primary_market""",
        target_eids,
    ).fetchall()
    print("対象 listing の primary_market 分布:")
    for r in final_pm_count:
        print(f"  {r['primary_market']!r}: {r['cnt']} 件")

    final_pmc_remaining = conn.execute(
        f"""SELECT COUNT(*) AS cnt FROM pending_market_changes
            WHERE ebay_item_id IN ({target_placeholders})""",
        target_eids,
    ).fetchone()
    print(f"対象 listing の pending_market_changes 残数: {final_pmc_remaining['cnt']} (期待: 0)")

    final_msd_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM market_strategy_decisions WHERE reviewer=?",
        (REVIEWER,),
    ).fetchone()
    print(f"market_strategy_decisions ({REVIEWER!r}) 件数: {final_msd_count['cnt']} (期待: {len(rows)})")
    conn.close()

    # Q5 完了報告 4 行
    print("\n" + "=" * 60)
    print("Q5 完了報告:")
    print("- 使用モデル: なし (純 SQL operation)")
    print("- 検証経路: Q2 6-step (snapshot + 1 件試行 + 残り実行 + 再 SELECT)")
    print(f"- 実機ログ: 本 stdout / snapshot {snapshot_path}")
    print("- 残リスク: 24h 以内に retrospective code-reviewer 投入必要 (Q2 6-step #5)")
    print("=" * 60)


if __name__ == "__main__":
    main()
