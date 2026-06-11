#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rc_id 9, 12 (Q1 検証テストデータ) の one-shot 掃除.

Q2 6-step: snapshot → 1 件試行 → 残り → SELECT 確認。
実行: python scripts/cleanup_rc_testdata_2026_06_11.py [--apply]
"""
import json
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "monitor.db"
BACKUP = BASE / "data" / "backup_research_candidates_rc9_rc12_20260611.json"
TARGET_IDS = (9, 12)


def main() -> None:
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM research_candidates WHERE rc_id IN ({','.join('?' * len(TARGET_IDS))})",
        TARGET_IDS,
    ).fetchall()
    data = [dict(r) for r in rows]
    for r in data:
        print(f"rc_id={r['rc_id']} | status={r['status']} | title={str(r.get('title_ja') or '')[:60]}")

    if not apply:
        print(f"[dry-run] {len(data)} 件対象。--apply で削除実行")
        conn.close()
        return

    BACKUP.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"snapshot: {BACKUP.name} ({len(data)} 件)")

    # step 2: 1 件試行
    cur = conn.execute("DELETE FROM research_candidates WHERE rc_id = ?", (TARGET_IDS[0],))
    print(f"step2: rc_id={TARGET_IDS[0]} deleted rowcount={cur.rowcount}")
    # step 3: 残り
    cur = conn.execute("DELETE FROM research_candidates WHERE rc_id = ?", (TARGET_IDS[1],))
    print(f"step3: rc_id={TARGET_IDS[1]} deleted rowcount={cur.rowcount}")
    conn.commit()

    # step 4: SELECT 確認
    left = conn.execute(
        f"SELECT COUNT(*) FROM research_candidates WHERE rc_id IN ({','.join('?' * len(TARGET_IDS))})",
        TARGET_IDS,
    ).fetchone()[0]
    gate_passed = conn.execute(
        "SELECT COUNT(*) FROM research_candidates WHERE status = 'gate_passed'"
    ).fetchone()[0]
    print(f"step4: 残存 {left} 件 (期待 0) / gate_passed 残 {gate_passed} 件")
    conn.close()


if __name__ == "__main__":
    main()
