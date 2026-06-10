"""W229 Phase 2 Q1 実機検証 (one-shot、縮小設定).

本番 config を in-memory で override (enabled=true / max_items=3 / max_pages=1) して
run_research_harvest を 1 回実行し、DB 着地と Discord 通知を実機確認する。
schedule_config.json は変更しない (enabled=false のまま)。

実行: python scripts/q1_research_harvest_2026_06_10.py
"""
import sys
import os
import json
import copy

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tasks.task_research_harvest import run_research_harvest

# migration v70 (harvest_pattern) を本番 DB に適用 (冪等、scheduler 起動時と同じ経路)
from monitor.database import init_db
init_db()

CONFIG_PATH = os.path.join(_ROOT, "config", "schedule_config.json")
with open(CONFIG_PATH, encoding="utf-8") as f:
    config = json.load(f)

cfg = copy.deepcopy(config)
hc = cfg["tasks_enabled"]["research_harvest"]
hc["enabled"] = True          # in-memory のみ (json は不変)
hc["max_items_per_run"] = 3   # 縮小: 3 商品
hc["max_pages"] = 1           # 縮小: 1 ページ

print("=" * 70)
print("  Q1 実機検証: run_research_harvest (enabled=true / 3 items / 1 page)")
print("=" * 70)

result = run_research_harvest(cfg)

print()
print("--- 実行結果 ---")
for k, v in result.items():
    print(f"  {k}: {v}")

# --- DB 着地確認 ---
from monitor.database import get_conn

print()
print("--- DB 着地確認 (research_candidates / source='terapeak_harvest' 直近1h) ---")
with get_conn() as conn:
    rows = conn.execute(
        """SELECT rc_id, status, harvest_pattern, gate_decision, gate_reason,
                  substr(title_ja, 1, 50) AS title_head
           FROM research_candidates
           WHERE source = 'terapeak_harvest'
             AND created_at >= datetime('now', '-1 hours')
           ORDER BY rc_id DESC"""
    ).fetchall()
for r in rows:
    print(f"  rc_id={r['rc_id']} | {r['status']} | {r['harvest_pattern']} | "
          f"{r['gate_decision']} | {r['gate_reason']} | {r['title_head']}")
print(f"  着地行数: {len(rows)}")

print()
print("--- api_call_log クォータ計上確認 (本日 JST / terapeak) ---")
with get_conn() as conn:
    cnt = conn.execute(
        """SELECT COUNT(*) FROM api_call_log
           WHERE provider = 'terapeak'
             AND DATE(called_at, '+9 hours') = DATE('now', '+9 hours')"""
    ).fetchone()[0]
print(f"  本日 terapeak navigate 計上: {cnt}")

print()
verdict = "PASS" if (result.get("success") and len(rows) > 0) else "FAIL"
print(f"=== Q1 判定: {verdict} ===")
