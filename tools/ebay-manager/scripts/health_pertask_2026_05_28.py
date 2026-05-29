"""失敗4タスクの直近5日 per-task 状況 + 最新失敗 message + 全タスクの本日カバレッジ."""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "monitor.db"
c = sqlite3.connect(str(DB))
c.row_factory = sqlite3.Row

WATCH = ["rival_detection", "fuel_surcharge_check", "research_morning_brief", "daily_codex_lint"]

print("=== 失敗4タスクの直近5日 status (JST date) ===")
for tk in WATCH:
    print(f"\n[{tk}]")
    for r in c.execute("""
        SELECT DATE(started_at) AS d, status, COUNT(*) AS n,
               MAX(started_at) AS last_run
        FROM task_execution_log
        WHERE task_key=? AND DATE(started_at) >= DATE('now','+9 hours','-5 days')
        GROUP BY d, status ORDER BY d DESC, status
    """, (tk,)):
        print(f"   {r['d']} | {r['status']:<12} | n={r['n']} | last={r['last_run']}")

print("\n\n=== 失敗4タスクの最新 failed message 全文 (直近5日) ===")
for tk in WATCH:
    row = c.execute("""
        SELECT started_at, status, message FROM task_execution_log
        WHERE task_key=? AND status LIKE '%fail%'
          AND DATE(started_at) >= DATE('now','+9 hours','-5 days')
        ORDER BY started_at DESC LIMIT 1
    """, (tk,)).fetchone()
    if row:
        print(f"\n[{tk}] {row['started_at']} ({row['status']})")
        print(f"   {row['message']}")

print("\n\n=== 最新の各タスク終了状態 (TASK_SCHEDULE 全体、最後の実行) ===")
rows = c.execute("""
    SELECT t.task_key, t.status, t.started_at
    FROM task_execution_log t
    JOIN (SELECT task_key, MAX(started_at) mx FROM task_execution_log
          GROUP BY task_key) m
      ON t.task_key=m.task_key AND t.started_at=m.mx
    ORDER BY (t.status LIKE '%fail%') DESC, t.task_key
""").fetchall()
for r in rows:
    flag = " <<< FAILED" if "fail" in (r["status"] or "") else ""
    print(f"   {r['task_key']:<32} | {r['status']:<12} | {r['started_at']}{flag}")
c.close()
