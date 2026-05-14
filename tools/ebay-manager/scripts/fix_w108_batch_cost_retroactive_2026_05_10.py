"""W108 retroactive fix: 既存 batch operation 行の cost_usd を 0.5 倍 + is_batch=1 に修正.

経緯:
  - Anthropic Message Batches API は通常価格の 50% 割引.
  - api_logger.py は W108 deploy (5/9-5/10) まで全ての cost を通常価格で記録 → 約 2x 過大評価.
  - W108 fix で `is_batch` 列追加 + INSERT 時 auto-detect (`operation.endswith("_batch")`).
  - 既存 1823 行 (deploy 前の candidate_evaluate_batch) は is_batch=0 + cost_usd 過大のまま.

修正対象 (DB query 実測値、5/10 0:35 JST 時点):
  - WHERE is_batch=0 AND operation LIKE '%_batch'  → 1823 件
  - 期間: 2026-05-01 23:59 〜 2026-05-09 18:09
  - 現在 SUM(cost_usd): $36.0229 (このうち)
  - 修正後 SUM(cost_usd): $18.0115 (期待 delta -$18.0115)
  - 既に is_batch=1 の 12 件 ($0.0004 分) は対象外 (W108 deploy 後 INSERT、既に正価)

冪等性 (Q2 / db-migration-rules.md):
  - WHERE is_batch=0 AND operation LIKE '%_batch' で物理保証.
  - 2 回目以降は 0 件 match → 何もせず exit 0.
  - flag table / user_version 不要 (新スキーマ自体が冪等性キー).

Q2 6-step:
  1. snapshot JSON で 1823 件の現状を保存 (data/backups/)
  2. SUM/COUNT 事前計測
  3. 1 件試行 → cost_usd 半減 + is_batch=1 遷移確認
  4. 残り 1822 件 (1 トランザクション、partial commit 防止)
  5. 再 SELECT で SUM/COUNT 検証 (delta が期待値と一致、tolerance $0.10)
  6. 24h 以内に retrospective code-reviewer

実行: python scripts/fix_w108_batch_cost_retroactive_2026_05_10.py
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

DELTA_TOLERANCE_USD = 0.10  # 浮動小数点 + 端数累積分の許容範囲


def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # ───────────────────────────────────────────
    # Step 1: snapshot (修正対象の現状を JSON dump)
    # ───────────────────────────────────────────
    targets = conn.execute(
        """SELECT id, provider, model, operation, input_tokens, output_tokens,
                  cache_read_tokens, cache_write_tokens, cost_usd, called_at, is_batch
           FROM api_call_log
           WHERE is_batch = 0 AND operation LIKE '%_batch'
           ORDER BY id"""
    ).fetchall()

    if len(targets) == 0:
        print("対象 0 件、既に修正済 or 対象なし. abort.")
        sys.exit(0)

    print(f"対象 rows: {len(targets)} 件")

    backup = {
        "timestamp": datetime.now().isoformat(),
        "rows": [dict(r) for r in targets],
    }
    backup_path = BACKUP_DIR / f"fix_w108_batch_cost_2026_05_10_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backup_path.write_text(
        json.dumps(backup, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"snapshot 保存: {backup_path}")

    # ───────────────────────────────────────────
    # Step 2: SUM/COUNT 事前計測
    # ───────────────────────────────────────────
    sum_before = conn.execute(
        "SELECT ROUND(SUM(cost_usd), 6) FROM api_call_log WHERE is_batch=0 AND operation LIKE '%_batch'"
    ).fetchone()[0]
    sum_already = conn.execute(
        "SELECT ROUND(COALESCE(SUM(cost_usd), 0), 6) FROM api_call_log WHERE is_batch=1"
    ).fetchone()[0]
    expected_delta = round(sum_before / 2.0, 6)  # half goes away
    expected_after_target_sum = round(sum_before / 2.0, 6)
    print(f"\n[step2] 事前計測:")
    print(f"  対象 SUM(cost_usd) (is_batch=0 batch): ${sum_before}")
    print(f"  既修正分 SUM(cost_usd) (is_batch=1):   ${sum_already}")
    print(f"  期待 delta: -${expected_delta} (= 対象の半分が消える)")
    print(f"  期待 修正後対象 SUM:  ${expected_after_target_sum}")

    # ───────────────────────────────────────────
    # Step 3: 1 件試行
    # ───────────────────────────────────────────
    first = targets[0]
    first_id = first["id"]
    first_cost_before = first["cost_usd"]
    expected_first_cost_after = round(first_cost_before * 0.5, 8)
    print(f"\n[step3] 1 件試行: id={first_id}, cost_usd 現在=${first_cost_before}")
    print(f"  期待 修正後: cost_usd=${expected_first_cost_after}, is_batch=1")

    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute(
            "UPDATE api_call_log SET cost_usd = cost_usd * 0.5, is_batch = 1 WHERE id = ?",
            (first_id,),
        )
        if cur.rowcount != 1:
            conn.rollback()
            print(f"!!! 1 件試行失敗: rowcount={cur.rowcount}, expected 1, abort !!!")
            sys.exit(2)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"!!! 1 件試行 exception: {type(e).__name__}: {e}, abort !!!")
        raise

    after = conn.execute(
        "SELECT cost_usd, is_batch FROM api_call_log WHERE id = ?",
        (first_id,),
    ).fetchone()
    print(f"  実際: cost_usd=${after['cost_usd']}, is_batch={after['is_batch']}")
    if abs(after["cost_usd"] - expected_first_cost_after) > 1e-8 or after["is_batch"] != 1:
        print("!!! 1 件試行: 期待値と異なる、abort (1 行残存、snapshot から手動 rollback) !!!")
        sys.exit(3)
    print("[step3] OK")

    # ───────────────────────────────────────────
    # Step 4: 残り 1822 件 (1 トランザクション)
    # ───────────────────────────────────────────
    remaining_ids = [r["id"] for r in targets[1:]]
    print(f"\n[step4] 残り {len(remaining_ids)} 件実行 (1 トランザクション)")

    cur.execute("BEGIN")
    try:
        # SQLite IN (...) は host param 制限 (~1000) があるため、ID 範囲ではなく filter で UPDATE
        # WHERE is_batch=0 AND operation LIKE '%_batch' に依存して全 row 一括 UPDATE
        cur.execute(
            "UPDATE api_call_log SET cost_usd = cost_usd * 0.5, is_batch = 1 "
            "WHERE is_batch = 0 AND operation LIKE '%_batch'"
        )
        rows_updated = cur.rowcount
        if rows_updated != len(remaining_ids):
            conn.rollback()
            print(f"!!! step4 affected rows mismatch: {rows_updated} vs expected {len(remaining_ids)}, abort !!!")
            sys.exit(4)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"!!! step4 exception: {type(e).__name__}: {e}, partial commit may exist !!!")
        raise

    print(f"[step4] {rows_updated} 件 UPDATE 完了")

    # ───────────────────────────────────────────
    # Step 5: 検証
    # ───────────────────────────────────────────
    print("\n[step5] 検証")

    # 5-a: 残存 is_batch=0 batch 行が 0 件
    leftover = conn.execute(
        "SELECT COUNT(*) FROM api_call_log WHERE is_batch=0 AND operation LIKE '%_batch'"
    ).fetchone()[0]
    print(f"  残存 (is_batch=0 batch): {leftover} 件 (期待 0)")

    # 5-b: SUM 検証 (修正後 batch 全体 = 対象/2 + 既修正分)
    sum_after_batch_all = conn.execute(
        "SELECT ROUND(SUM(cost_usd), 6) FROM api_call_log WHERE operation LIKE '%_batch'"
    ).fetchone()[0]
    expected_after_total = round(sum_before / 2.0 + sum_already, 6)
    delta_observed = round((sum_before + sum_already) - sum_after_batch_all, 6)
    print(f"  修正後 SUM(cost_usd) (batch 全体): ${sum_after_batch_all}")
    print(f"  期待値: ${expected_after_total}")
    print(f"  observed delta: -${delta_observed} (期待 -${expected_delta}, tolerance ${DELTA_TOLERANCE_USD})")

    if leftover != 0:
        print("!!! 検証失敗: is_batch=0 残存 !!!")
        sys.exit(5)

    if abs(delta_observed - expected_delta) > DELTA_TOLERANCE_USD:
        print(f"!!! 検証失敗: delta tolerance {DELTA_TOLERANCE_USD} 超過 !!!")
        sys.exit(6)

    # 5-c: spot-check (1 件試行した row が変わっていないこと)
    spot = conn.execute(
        "SELECT cost_usd, is_batch FROM api_call_log WHERE id = ?",
        (first_id,),
    ).fetchone()
    print(f"  spot-check id={first_id}: cost_usd=${spot['cost_usd']}, is_batch={spot['is_batch']}")
    if spot["is_batch"] != 1 or abs(spot["cost_usd"] - expected_first_cost_after) > 1e-8:
        print("!!! spot-check 失敗 !!!")
        sys.exit(7)

    # 完了報告 (Q5 4 行テンプレ)
    print("\n" + "=" * 60)
    print("- 使用モデル: retroactive UPDATE script (no LLM)")
    print(f"- 検証経路: DB SELECT (leftover=0, SUM delta {abs(delta_observed - expected_delta):.6f} < tolerance {DELTA_TOLERANCE_USD})")
    print(f"- 実機ログ: snapshot {backup_path.name}")
    print("- 残リスク: 24h 以内に retrospective code-reviewer 投入必須 (Q2)")
    print("=" * 60)
    conn.close()


if __name__ == "__main__":
    main()
