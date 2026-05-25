"""orphan started 残骸 3 件 cleanup one-shot script (W164-pm Problem #5).

対象: 5/02 22:07 / 5/21 11:08 / 5/22 18:09 の inventory_check
  (status='started' のまま finished_at NULL = scheduler crash/kill 残骸)

Q2 6 step 完備 (db-migration-rules.md):
    1. SELECT で 3 件 dump → JSON snapshot 保存 (rollback artifact)
    2. UPDATE 1 件 試行 → 結果 SELECT 確認
    3. 残り 2 件 UPDATE
    4. SELECT で全 3 件再確認
    5. retrospective code-reviewer (本 session 内なら同時実施)
    6. rollback 関数提供 (誤更新時 snapshot から元値復元)

実行方法:
    python scripts/cleanup_orphan_started_2026_05_25.py            # 実行
    python scripts/cleanup_orphan_started_2026_05_25.py --dryrun   # 内容確認のみ
    python scripts/cleanup_orphan_started_2026_05_25.py --rollback # snapshot から復元
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "monitor.db"
SNAPSHOT_PATH = BASE_DIR / "data" / "tmp" / "orphan_snapshot_2026_05_25.json"

TARGETS = [
    # (date_prefix, batch_id) で WHERE 句を絞る (id 直指定でなく構造化、誤更新リスク低減)
    ("2026-05-02", "20260502_22sched_order_alert_"),
    ("2026-05-21", "20260521_11sched"),
    ("2026-05-22", "20260522_18sched"),
]

CLEANUP_MESSAGE = (
    "auto-cleanup W164-pm 2026-05-25: scheduler crash/kill artifact "
    "(started のまま finished_at NULL を Phase C 検知後に cleanup)"
)


def _select_targets(conn: sqlite3.Connection) -> list[dict]:
    """対象 3 件を取得 (rollback 用 snapshot 兼確認)."""
    rows = []
    for date_prefix, batch_id in TARGETS:
        r = conn.execute(
            "SELECT id, task_key, batch_id, batch_hour, status, started_at, "
            "       finished_at, duration_sec, success, message, expected_today "
            "FROM task_execution_log "
            "WHERE task_key='inventory_check' AND batch_id=? AND status='started' "
            "  AND started_at LIKE ? AND finished_at IS NULL",
            (batch_id, f"{date_prefix}%"),
        ).fetchall()
        for row in r:
            rows.append({
                "id": row[0], "task_key": row[1], "batch_id": row[2],
                "batch_hour": row[3], "status": row[4], "started_at": row[5],
                "finished_at": row[6], "duration_sec": row[7], "success": row[8],
                "message": row[9], "expected_today": row[10],
            })
    return rows


def _save_snapshot(rows: list[dict]) -> None:
    """Step 1: rollback 用 snapshot 保存."""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps({"saved_at": datetime.now().isoformat(), "rows": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[Step 1] snapshot saved: {SNAPSHOT_PATH}")


def _compute_finished_at(started_at: str) -> str:
    """instant-fail として finished_at=started_at で記録 (W164-pm HIGH-4 対応).

    旧版は started_at + 12h を入れていたが、これは架空 duration=43200s を作り
    audit query (「duration > 1h は重い task」等) を誤判定させる. 実 crash 時刻は
    復元不能のため、`finished_at = started_at` で「instantaneous failed record」
    と明示し、message で auto-cleanup である旨を補足する.
    """
    return started_at[:19]  # remove microseconds


def _update_one(conn: sqlite3.Connection, row: dict) -> int:
    """Step 2/3: 1 件 UPDATE."""
    finished_at = _compute_finished_at(row["started_at"])
    cur = conn.execute(
        "UPDATE task_execution_log "
        "SET status='failed', finished_at=?, success=0, message=? "
        "WHERE id=? AND status='started' AND finished_at IS NULL",
        (finished_at, CLEANUP_MESSAGE, row["id"]),
    )
    return cur.rowcount


def _verify_target(conn: sqlite3.Connection, target_id: int) -> dict | None:
    """UPDATE 後の確認."""
    r = conn.execute(
        "SELECT id, status, success, message, finished_at "
        "FROM task_execution_log WHERE id=?", (target_id,),
    ).fetchone()
    if not r:
        return None
    return {"id": r[0], "status": r[1], "success": r[2],
            "message": r[3], "finished_at": r[4]}


def run(dryrun: bool = False) -> dict:
    """Q2 6 step 完備の orphan cleanup."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        # Step 1: SELECT + snapshot
        targets = _select_targets(conn)
        print(f"[Step 1] 対象 {len(targets)} 件:")
        for t in targets:
            print(f"  id={t['id']} batch={t['batch_id']} started={t['started_at']}")
        if not targets:
            print("対象 0 件 = 既に cleanup 済または対象不一致")
            return {"updated": 0, "skipped_reason": "no targets"}
        if len(targets) != 3:
            print(f"WARN: 対象 {len(targets)} 件 (期待 3) → 安全のため中断")
            return {"updated": 0, "skipped_reason": f"unexpected count {len(targets)}"}
        _save_snapshot(targets)

        if dryrun:
            print("\n[--dryrun] 以下を実行する予定 (実行しない):")
            for t in targets:
                fin = _compute_finished_at(t["started_at"])
                print(f"  UPDATE id={t['id']}: status='started'→'failed', "
                      f"finished_at=NULL→{fin}, message='auto-cleanup ...'")
            return {"updated": 0, "dryrun": True}

        # Step 2: 1 件試行 + verify
        first = targets[0]
        print(f"\n[Step 2] 1 件試行: id={first['id']}")
        n = _update_one(conn, first)
        conn.commit()
        if n != 1:
            print(f"  ABORT: rowcount={n} (期待 1)")
            return {"updated": 0, "error": "row count mismatch"}
        v = _verify_target(conn, first["id"])
        print(f"  verify: {v}")
        if v["status"] != "failed" or v["success"] != 0:
            print(f"  ABORT: 結果不一致 status={v['status']} success={v['success']}")
            return {"updated": 1, "error": "verify failed"}

        # Step 3: 残り 2 件
        print("\n[Step 3] 残り 2 件:")
        updated = 1
        for t in targets[1:]:
            n = _update_one(conn, t)
            conn.commit()
            v = _verify_target(conn, t["id"])
            print(f"  id={t['id']} → rowcount={n}, verify={v}")
            if n == 1 and v and v["status"] == "failed":
                updated += 1

        # Step 4: 全 3 件再確認
        print("\n[Step 4] 全 3 件再 SELECT:")
        for t in targets:
            v = _verify_target(conn, t["id"])
            print(f"  id={t['id']}: status={v['status']} "
                  f"finished_at={v['finished_at']} message={(v['message'] or '')[:60]}")

        # Step 5: retrospective review hint (本 session 内なら同時)
        print("\n[Step 5] retrospective code-reviewer は本 session 内で実施 (W164-pm 一括)")

        # Step 6: rollback artifact 確認
        print(f"\n[Step 6] rollback artifact: {SNAPSHOT_PATH}")
        print("  誤更新発覚時: python scripts/cleanup_orphan_started_2026_05_25.py --rollback")

        return {"updated": updated, "snapshot": str(SNAPSHOT_PATH)}
    finally:
        conn.close()


def rollback() -> dict:
    """Step 6: snapshot から元値を復元 (誤更新発覚時用)."""
    if not SNAPSHOT_PATH.exists():
        print(f"snapshot 不在: {SNAPSHOT_PATH}")
        return {"restored": 0, "error": "no snapshot"}
    data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    rows = data["rows"]
    print(f"rollback: snapshot saved_at={data['saved_at']}, rows={len(rows)}")
    conn = sqlite3.connect(str(DB_PATH))
    restored = 0
    try:
        for row in rows:
            cur = conn.execute(
                "UPDATE task_execution_log SET status=?, finished_at=?, "
                "  success=?, message=? WHERE id=?",
                (row["status"], row["finished_at"], row["success"],
                 row["message"], row["id"]),
            )
            conn.commit()
            if cur.rowcount == 1:
                restored += 1
            print(f"  id={row['id']} restored: status={row['status']} "
                  f"finished_at={row['finished_at']}")
    finally:
        conn.close()
    return {"restored": restored}


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        print(rollback())
    elif "--dryrun" in sys.argv:
        print(run(dryrun=True))
    else:
        print(run())
