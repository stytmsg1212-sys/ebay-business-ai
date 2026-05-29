"""定時実行ヘルスチェック現状監査 (read-only).

task_execution_log は started_at/finished_at が JST naive (sqlite-timezone.md 例外)
なので DATE(started_at) で JST 日付直接比較 (+9h shift 禁止)。
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "monitor.db"
c = sqlite3.connect(str(DB))
c.row_factory = sqlite3.Row

print("=== 直近3日 task_execution_log status 集計 (JST date) ===")
for r in c.execute("""
    SELECT DATE(started_at) AS d, status, COUNT(*) AS n
    FROM task_execution_log
    WHERE DATE(started_at) >= DATE('now','+9 hours','-3 days')
    GROUP BY d, status ORDER BY d DESC, status
"""):
    print(f"  {r['d']} | {r['status']:<14} | {r['n']}")

print()
print("=== 直近3日 failed / error 系の詳細 ===")
rows = c.execute("""
    SELECT DATE(started_at) AS d, task_key, status, started_at, finished_at,
           substr(COALESCE(message,''),1,200) AS msg
    FROM task_execution_log
    WHERE DATE(started_at) >= DATE('now','+9 hours','-3 days')
      AND (status LIKE '%fail%' OR status LIKE '%error%')
    ORDER BY started_at DESC LIMIT 40
""").fetchall()
if not rows:
    print("  (failed/error なし)")
for r in rows:
    print(f"  {r['started_at']} | {r['task_key']} | {r['status']} | {r['msg']}")

print()
print("=== started のみで finished が無い (中断/ハング疑い) 直近3日 ===")
rows = c.execute("""
    SELECT task_key, status, started_at, finished_at
    FROM task_execution_log
    WHERE DATE(started_at) >= DATE('now','+9 hours','-3 days')
      AND finished_at IS NULL AND status NOT LIKE 'skip%'
    ORDER BY started_at DESC LIMIT 40
""").fetchall()
if not rows:
    print("  (無し)")
for r in rows:
    print(f"  {r['started_at']} | {r['task_key']} | {r['status']} | finished={r['finished_at']}")

print()
print("=== health_alert_log 直近3日 ===")
try:
    rows = c.execute("""
        SELECT created_at, alert_type, substr(COALESCE(message,''),1,140) AS msg
        FROM health_alert_log
        WHERE created_at >= datetime('now','-3 days')
        ORDER BY created_at DESC LIMIT 30
    """).fetchall()
    if not rows:
        print("  (alert なし)")
    for r in rows:
        print(f"  {r['created_at']} | {r['alert_type']} | {r['msg']}")
except Exception as e:
    print(f"  health_alert_log query err: {e}")

c.close()
