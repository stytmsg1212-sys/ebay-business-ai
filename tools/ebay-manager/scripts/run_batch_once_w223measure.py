#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定時実行 (hour=2 batch) を 1 回手動実行 (W223 実測用、2026-06-05)。

別プロセスで execute_daily_tasks(config, scheduled_hour=2) を呼ぶ。
稼働中 scheduler (別 PID) とは _batch_ctx が独立。price_optimization は
execution_times=None で skip = 新 W222 floor による実価格値下げは発火しない。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def main() -> None:
    cfg = json.load(open(_ROOT / "config" / "schedule_config.json", encoding="utf-8"))
    from daily_scheduler import execute_daily_tasks

    t0 = time.time()
    print(f"[batch] hour=2 定時実行を手動起動 ...")
    results = execute_daily_tasks(cfg, scheduled_hour=2)
    dt = time.time() - t0
    print(f"[batch] 完了 ({dt:.0f}s)")
    print("[batch] tasks 実行結果:")
    for k, v in (results or {}).items():
        if isinstance(v, dict):
            print(f"  {k}: success={v.get('success')} "
                  f"reused_eval={v.get('reused_eval')} ranked_out={v.get('ranked_out')} "
                  f"msg={str(v.get('message'))[:120]}")
        else:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
