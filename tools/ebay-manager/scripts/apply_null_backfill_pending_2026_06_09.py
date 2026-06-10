"""今回の NULL backfill 提案 (pending_market_changes で current=NULL) のみを承認適用.

Q2 6-step 準拠 (db-migration-rules):
  1. snapshot (rollback 用 JSON)
  2. 1 件試行 → 検証
  3. 残りを _bulk_decision で適用
  4. 検証 (NULL 残数 / pending 残 / decisions 記録)
  5. retrospective review は別途 code-reviewer
  6. 異常時は snapshot から rollback

scope: COALESCE(current_market,'')='' のみ (current 有りの 24 件 = 既存分類の変更提案は
       money-direct のため user レビューに残す。本 script は触らない)。
"""
from __future__ import annotations
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "monitor.db"
SNAP = ROOT / "data" / f"snapshot_null_backfill_pre_apply_2026_06_09.json"
REVIEWER = "claude_null_backfill_2026_06_09"

from tabs.tab_market_strategy import _bulk_decision  # noqa: E402


def _conn():
    c = sqlite3.connect(str(DB)); c.row_factory = sqlite3.Row
    return c


def main():
    c = _conn()
    # 対象: current=NULL の pending (今回 backfill 分)
    targets = c.execute(
        """SELECT p.ebay_item_id, p.proposed_market, e.primary_market, e.lp_breakeven_usd
           FROM pending_market_changes p JOIN ebay_listings e ON e.ebay_item_id=p.ebay_item_id
           WHERE COALESCE(p.current_market,'')='' """
    ).fetchall()
    eids = [r["ebay_item_id"] for r in targets]
    print(f"対象 (current=NULL backfill): {len(eids)} 件")
    if not eids:
        print("対象なし、終了"); return

    # Q2 #1: snapshot
    snap = [{"ebay_item_id": r["ebay_item_id"],
             "primary_market_before": r["primary_market"],
             "lp_breakeven_usd_before": r["lp_breakeven_usd"],
             "proposed": r["proposed_market"]} for r in targets]
    SNAP.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"snapshot 保存: {SNAP.name} ({len(snap)} 件, rollback 用)")

    # 全 before が NULL であることを確認 (backfill = 上書きでないことの保証)
    non_null = [s for s in snap if s["primary_market_before"] not in (None, "")]
    if non_null:
        print(f"!!! 中止: before が NULL でない行 {len(non_null)} 件 (上書きリスク)。例: {non_null[:3]}")
        sys.exit(2)
    print("確認: 全 121 件 before=NULL (上書きなし) ✅")
    c.close()

    # Q2 #2: 1 件試行
    first = eids[0]
    print(f"\n--- 1 件試行: {first} ---")
    _bulk_decision({first}, "approved", reviewer=REVIEWER)
    c = _conn()
    pm = c.execute("SELECT primary_market FROM ebay_listings WHERE ebay_item_id=?", (first,)).fetchone()["primary_market"]
    pend = c.execute("SELECT COUNT(*) n FROM pending_market_changes WHERE ebay_item_id=?", (first,)).fetchone()["n"]
    dec = c.execute("SELECT COUNT(*) n FROM market_strategy_decisions WHERE ebay_item_id=? AND reviewer=?", (first, REVIEWER)).fetchone()["n"]
    print(f"  primary_market={pm!r} / pending残={pend} / decisions={dec}")
    if pm in (None, "") or pend != 0 or dec != 1:
        print("!!! 1 件試行 検証失敗 → abort (残りは適用しない)"); sys.exit(2)
    print("  ✅ 1 件試行 OK")
    c.close()

    # Q2 #3: 残り一括適用
    rest = set(eids[1:])
    print(f"\n--- 残り {len(rest)} 件 一括適用 ---")
    _bulk_decision(rest, "approved", reviewer=REVIEWER)

    # Q2 #4: 検証
    c = _conn()
    still_null = c.execute("SELECT COUNT(*) n FROM ebay_listings WHERE primary_market IS NULL AND COALESCE(is_ended,0)=0").fetchone()["n"]
    pend_left = c.execute("SELECT COUNT(*) n FROM pending_market_changes WHERE COALESCE(current_market,'')=''").fetchone()["n"]
    dec_total = c.execute("SELECT COUNT(*) n FROM market_strategy_decisions WHERE reviewer=?", (REVIEWER,)).fetchone()["n"]
    print(f"\n=== 検証 ===")
    print(f"  active primary_market IS NULL 残: {still_null} 件 (backfill前121→期待: 大幅減)")
    print(f"  current=NULL pending 残: {pend_left} 件 (期待: 0)")
    print(f"  decisions ({REVIEWER}): {dec_total} 件 (期待: 121)")
    print("\n  適用後 内訳:")
    for r in c.execute("""SELECT primary_market pm, COUNT(*) n FROM ebay_listings
                          WHERE COALESCE(is_ended,0)=0 GROUP BY pm ORDER BY n DESC""").fetchall():
        print(f"    {str(r['pm']):14s}: {r['n']}")
    c.close()


if __name__ == "__main__":
    main()
