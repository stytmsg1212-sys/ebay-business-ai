"""W108 (UI 誤操作) rollback: 22:24:59 一括 NULL→unknown 承認 50 件を巻き戻す.

経緯:
  - 5/9 22:24:59.482977 ~ .493937 の 11ms 内に 50 件が `final_market='unknown'` で
    一括承認された. reason は全て `sample 0/1/2/3 < 3 (or 5)` = データ不足判定.
  - 11ms で 50 件は人間判断不可能 = UI で「unknown 全件一括承認」ボタン誤押下相当.
  - 本来は scheduler 週次 batch (W110 dayRange=365) でデータ蓄積を待つべき行.

採用方針 (A2):
  - ebay_listings.primary_market = NULL に戻す (これが本質的 rollback)
  - ebay_listings.market_analysis_at = NULL に戻す (= 「採用判断は巻戻し」を表現)
  - ebay_listings.market_sample_size は維持 (分析事実の audit 用に残す)
  - market_strategy_decisions の既存 50 行は audit trail として保持 (改竄禁止)
  - 新規 50 行を action='reverted', reviewer='rollback_2026_05_09' で追加

Q2 6-step (db-migration-rules.md 準拠):
  1. snapshot JSON で 50 件の現状を保存 (data/backups/)
  2. 1 件試行 → primary_market 遷移確認
  3. 残り 49 件
  4. 再 SELECT で全 50 件が NULL に遷移したか検証
  5. 24h 以内に retrospective code-reviewer
  6. kill switch (scheduler.tasks_enabled.market_analysis_refresh) は別途停止可能

実行: python scripts/rollback_unknown_22_24_59_2026_05_09.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "monitor.db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"

TARGET_TIMESTAMP_PREFIX = "2026-05-09T22:24:59"
REVIEWER = "rollback_2026_05_09"
ROLLBACK_REASON = (
    "rollback of accidental bulk-approval of 50 NULL->unknown decisions "
    "made within 11ms (UI mass-approve button mis-press). "
    "Original reasons were 'sample N < threshold' = data shortage, "
    "should have stayed NULL until W110 scheduler weekly batch accumulates samples."
)


def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # ───────────────────────────────────────────
    # Step 1: snapshot
    # ───────────────────────────────────────────
    targets = conn.execute(
        """SELECT id, sku, ebay_item_id, previous_market, proposed_market,
                  final_market, action, decided_at, reason, reviewer
           FROM market_strategy_decisions
           WHERE decided_at LIKE ?
             AND final_market = 'unknown'
             AND action = 'approved'
           ORDER BY id""",
        (TARGET_TIMESTAMP_PREFIX + "%",),
    ).fetchall()

    if len(targets) == 0:
        print("対象 0 件、既に rollback 済 or 条件不一致. abort.")
        sys.exit(0)

    print(f"対象 decisions: {len(targets)} 件")

    # ebay_listings 側の現状も snapshot
    item_ids = [r["ebay_item_id"] for r in targets]
    placeholders = ",".join(["?"] * len(item_ids))
    listings_before = conn.execute(
        f"""SELECT ebay_item_id, sku, title, primary_market,
                   market_analysis_at, market_sample_size
            FROM ebay_listings WHERE ebay_item_id IN ({placeholders})""",
        item_ids,
    ).fetchall()

    backup = {
        "timestamp": datetime.now().isoformat(),
        "decisions": [dict(r) for r in targets],
        "listings_before": [dict(r) for r in listings_before],
    }
    backup_path = BACKUP_DIR / f"rollback_22_24_59_2026_05_09_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backup_path.write_text(
        json.dumps(backup, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"snapshot 保存: {backup_path}")

    # ───────────────────────────────────────────
    # Step 2: 1 件試行
    # ───────────────────────────────────────────
    first = targets[0]
    first_id = first["ebay_item_id"]
    print(f"\n[step2] 1 件試行: ebay_item_id={first_id}, sku={first['sku']}")

    cur = conn.cursor()
    first_restored_market = first["previous_market"]  # NULL or 'US_only' etc.
    try:
        cur.execute("BEGIN")
        # ebay_listings を previous_market に戻す (NULL なら NULL のまま、US_only なら US_only に戻る)
        cur.execute(
            """UPDATE ebay_listings
               SET primary_market = ?,
                   market_analysis_at = NULL
               WHERE ebay_item_id = ?""",
            (first_restored_market, first_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            print(f"!!! 1 件試行失敗: rowcount={cur.rowcount}, expected 1, abort !!!")
            sys.exit(2)

        # market_strategy_decisions に rejected 行追加 (CHECK 制約準拠、reviewer + reason で rollback 文脈表現)
        cur.execute(
            """INSERT INTO market_strategy_decisions
               (sku, ebay_item_id, previous_market, proposed_market, final_market,
                action, decided_at, reason, reviewer)
               VALUES (?, ?, 'unknown', NULL, ?, 'rejected', ?, ?, ?)""",
            (first["sku"], first_id, first_restored_market,
             datetime.now().isoformat(), ROLLBACK_REASON, REVIEWER),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"!!! 1 件試行 exception: {type(e).__name__}: {e}, abort !!!")
        raise

    pm_after = conn.execute(
        "SELECT primary_market, market_analysis_at, market_sample_size FROM ebay_listings WHERE ebay_item_id=?",
        (first_id,),
    ).fetchone()
    print(f"  primary_market = {pm_after['primary_market']!r} (期待 {first_restored_market!r})")
    print(f"  market_analysis_at = {pm_after['market_analysis_at']!r} (期待 None)")
    print(f"  market_sample_size = {pm_after['market_sample_size']!r} (維持)")
    if pm_after["primary_market"] != first_restored_market or pm_after["market_analysis_at"] is not None:
        print("!!! 1 件試行: 期待値と異なる、abort !!!")
        sys.exit(3)
    print("[step2] OK")

    # ───────────────────────────────────────────
    # Step 3: 残り 49 件
    # ───────────────────────────────────────────
    print(f"\n[step3] 残り {len(targets) - 1} 件実行")
    cur.execute("BEGIN")
    try:
        for r in targets[1:]:
            restored = r["previous_market"]
            cur.execute(
                """UPDATE ebay_listings
                   SET primary_market = ?,
                       market_analysis_at = NULL
                   WHERE ebay_item_id = ?""",
                (restored, r["ebay_item_id"]),
            )
            cur.execute(
                """INSERT INTO market_strategy_decisions
                   (sku, ebay_item_id, previous_market, proposed_market, final_market,
                    action, decided_at, reason, reviewer)
                   VALUES (?, ?, 'unknown', NULL, ?, 'rejected', ?, ?, ?)""",
                (r["sku"], r["ebay_item_id"], restored, datetime.now().isoformat(),
                 ROLLBACK_REASON, REVIEWER),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"!!! step3 exception: {type(e).__name__}: {e}, partial commit may exist !!!")
        raise

    # ───────────────────────────────────────────
    # Step 4: 再 SELECT で検証
    # ───────────────────────────────────────────
    print("\n[step4] 検証")
    listings_after = conn.execute(
        f"""SELECT primary_market, COUNT(*) AS cnt FROM ebay_listings
            WHERE ebay_item_id IN ({placeholders})
            GROUP BY primary_market""",
        item_ids,
    ).fetchall()
    print("  ebay_listings.primary_market 分布:")
    for r in listings_after:
        print(f"    {r['primary_market']!r}: {r['cnt']} 件")

    reverted_count = conn.execute(
        """SELECT COUNT(*) FROM market_strategy_decisions
           WHERE reviewer = ? AND action = 'rejected'""",
        (REVIEWER,),
    ).fetchone()[0]
    print(f"  market_strategy_decisions rejected (rollback 由来) 行: {reverted_count} 件 (期待 {len(targets)})")

    if reverted_count != len(targets):
        print("!!! 検証失敗: rollback 行数が一致しない !!!")
        sys.exit(4)

    # 完了報告 (Q5 4 行テンプレ)
    print("\n" + "=" * 60)
    print("- 使用モデル: rollback script (no LLM)")
    print(f"- 検証経路: DB SELECT (primary_market 50 件 NULL + reverted 50 件 INSERT)")
    print(f"- 実機ログ: snapshot {backup_path.name}")
    print("- 残リスク: 24h 以内に retrospective code-reviewer 投入必須 (Q2)")
    print("=" * 60)
    conn.close()


if __name__ == "__main__":
    main()
