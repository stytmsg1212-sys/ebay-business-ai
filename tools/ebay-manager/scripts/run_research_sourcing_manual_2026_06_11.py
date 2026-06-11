#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 Phase 3 Q1 実機検証: run_research_sourcing を手動 1 回実行.

scheduler と同じ config (schedule_config.json) を読み、結果 dict を JSON 出力。
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from tasks.task_research_sourcing import run_research_sourcing  # noqa: E402

CONFIG_FILE = BASE / "config" / "schedule_config.json"


def main() -> None:
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    result = run_research_sourcing(config)
    print("=== RESULT ===")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
