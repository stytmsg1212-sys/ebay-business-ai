"""morning_brief 欠落 false-alarm 調査."""
import sqlite3
from pathlib import Path

c = sqlite3.connect(str(Path(__file__).resolve().parents[1] / "data" / "monitor.db"))
c.row_factory = sqlite3.Row

print("=== research_morning_brief 直近6件 (status/success/expected_today/batch_hour) ===")
for r in c.execute("""
    SELECT started_at, finished_at, status, success, batch_hour, expected_today,
           substr(COALESCE(message,''),1,120) m
    FROM task_execution_log WHERE task_key='research_morning_brief'
    ORDER BY started_at DESC LIMIT 6
"""):
    print(f"  {r['started_at']} | st={r['status']} | success={r['success']} | "
          f"bh={r['batch_hour']} | exp={r['expected_today']}")
    if r['m']:
        print(f"      {r['m']}")

print()
print("=== health_alert_log スキーマ ===")
cols = [x[1] for x in c.execute("PRAGMA table_info(health_alert_log)").fetchall()]
print("  ", cols)

print()
print("=== health_alert_log 直近10件 ===")
tcol = "created_at" if "created_at" in cols else ("alerted_at" if "alerted_at" in cols else cols[1])
try:
    for r in c.execute(f"SELECT * FROM health_alert_log ORDER BY {tcol} DESC LIMIT 10"):
        d = dict(r)
        print("  ", {k: (str(v)[:80] if v is not None else None) for k, v in d.items()})
except Exception as e:
    print("  err:", e)

print()
print("=== 本日(JST) と 昨日 の morning_brief 関連 (DATE 比較確認) ===")
for r in c.execute("""
    SELECT DATE(started_at) d, COUNT(*) n,
           SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) comp
    FROM task_execution_log WHERE task_key='research_morning_brief'
      AND DATE(started_at) >= DATE('now','+9 hours','-2 days')
    GROUP BY d ORDER BY d DESC
"""):
    print(f"  {r['d']} | total={r['n']} | completed={r['comp']}")
print("  now(JST date) =", c.execute("SELECT DATE('now','+9 hours')").fetchone()[0])
print("  now(UTC) =", c.execute("SELECT datetime('now')").fetchone()[0])
c.close()
