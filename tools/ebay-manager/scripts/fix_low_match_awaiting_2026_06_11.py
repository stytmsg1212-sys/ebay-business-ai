#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 Phase 3 Q1 実機事故の補正 one-shot (2026-06-11).

経緯: 初回 sourcing バッチ (match_score floor 未実装) で 15 件全件が
match_score < 60 (誤マッチ) の虚偽利益のまま awaiting_approval に積まれた。
本スクリプトで:
  1. 対象行 snapshot (rollback 用)
  2. 利益フィールドクリア (誤マッチ価格由来の虚偽数値)
  3. status を awaiting_approval → not_found に直接 UPDATE
     (状態機械にこのエッジは無い = 誤書込補正のため直接 UPDATE が正当。
      db-migration-rules「ROLLBACK 用 (障害復旧)」相当)
  4. sold_1_2yr > 0 の行のみ update_status (正規エッジ not_found→awaiting_approval)
     で監視候補として再キュー (利益未計算表示)
  5. SELECT で結果検証

Q2 6-step 準拠: snapshot → 1 件試行 → 残り全件 → SELECT 確認。
24h retrospective review 対象。

実行: python scripts/fix_low_match_awaiting_2026_06_11.py [--apply]
      (--apply なしは dry-run)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from monitor.database import get_conn
from monitor.research_candidates_db import (
    update_status,
    STATUS_AWAITING_APPROVAL,
)

FLOOR = 60  # MATCH_SCORE_SUGGESTED_FLOOR (research_poc.py L56)
BACKUP_PATH = Path(__file__).resolve().parent.parent / "data" / (
    f"backup_low_match_awaiting_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
)


def fetch_targets(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM research_candidates
           WHERE status = 'awaiting_approval'
             AND match_score IS NOT NULL AND match_score < ?""",
        (FLOOR,),
    ).fetchall()
    return [dict(r) for r in rows]


def clear_and_demote(conn, rc_id: int) -> None:
    """利益クリア + not_found 直接 UPDATE (1 行、同一 transaction)."""
    cur = conn.execute(
        """UPDATE research_candidates
           SET profit_jpy_true=NULL, profit_usd_true=NULL,
               keisuke_pass=0, keisuke_detail_json='{}',
               estimated_profit_usd=NULL,
               status='not_found',
               updated_at=CURRENT_TIMESTAMP
           WHERE rc_id=? AND status='awaiting_approval'""",
        (rc_id,),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"rc_id={rc_id}: 期待 1 行更新が {cur.rowcount} 行")


def main() -> int:
    apply = "--apply" in sys.argv

    with get_conn() as conn:
        conn.row_factory = __import__("sqlite3").Row
        targets = fetch_targets(conn)

    print(f"対象 (awaiting_approval + match_score<{FLOOR}): {len(targets)} 件")
    requeue_ids: list[int] = []
    for t in targets:
        gate_inputs = {}
        try:
            gate_inputs = json.loads(t.get("gate_inputs_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        sold = gate_inputs.get("sold_1_2yr") or 0
        will_requeue = bool(sold and sold > 0)
        if will_requeue:
            requeue_ids.append(t["rc_id"])
        print(
            f"  rc_id={t['rc_id']:>4} score={t['match_score']:>3} "
            f"profit=¥{t['profit_jpy_true']} sold_1_2yr={sold} "
            f"→ not_found{' → awaiting_approval(監視候補)' if will_requeue else ''} "
            f"| {(t['title_ja'] or '')[:35]}"
        )

    if not targets:
        print("対象なし — 終了")
        return 0

    if not apply:
        print("\n[dry-run] --apply で実行します")
        return 0

    # Step 1: snapshot
    BACKUP_PATH.write_text(
        json.dumps(targets, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nsnapshot 保存: {BACKUP_PATH.name} ({len(targets)} 行)")

    # Step 2: 1 件試行
    first_id = targets[0]["rc_id"]
    with get_conn() as conn:
        clear_and_demote(conn, first_id)
    print(f"1 件試行 OK: rc_id={first_id} → not_found + 利益クリア")

    # Step 3: 残り全件
    for t in targets[1:]:
        with get_conn() as conn:
            clear_and_demote(conn, t["rc_id"])
    print(f"残り {len(targets) - 1} 件完了")

    # Step 4: sold_1_2yr>0 を正規エッジで監視候補に再キュー
    for rc_id in requeue_ids:
        update_status(rc_id, STATUS_AWAITING_APPROVAL)
    print(f"監視候補 再キュー: {len(requeue_ids)} 件 (not_found→awaiting_approval 正規エッジ)")

    # Step 5: SELECT 検証
    with get_conn() as conn:
        conn.row_factory = __import__("sqlite3").Row
        leftover = conn.execute(
            """SELECT COUNT(*) AS n FROM research_candidates
               WHERE status='awaiting_approval'
                 AND (profit_jpy_true IS NOT NULL OR keisuke_pass=1)
                 AND match_score IS NOT NULL AND match_score < ?""",
            (FLOOR,),
        ).fetchone()["n"]
        counts = conn.execute(
            """SELECT status, COUNT(*) AS n FROM research_candidates
               GROUP BY status ORDER BY n DESC"""
        ).fetchall()
    print(f"\n検証: 虚偽利益が残る awaiting_approval = {leftover} 件 (期待 0)")
    print("status 分布:")
    for c in counts:
        print(f"  {c['status']}: {c['n']}")
    if leftover != 0:
        print("!! 補正不完全 — snapshot から確認要")
        return 1
    print("補正完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
