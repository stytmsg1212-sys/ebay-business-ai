#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W284: ebaymag_apply_queue の fail-safe OFF 回帰テスト (2026-06-20 HIGH-A 修正)

`ebaymag_apply_queue` は eBaymag 各国版の公開状態を mutate する money-direct タスク。
canary 検証前に自走しないよう、以下 2 点を恒久ガードする:

1. schedule_config.json に `tasks_enabled.ebaymag_apply_queue.enabled = false` が明示されている
   (relist と同じ canary 運用。block 削除や true 化を検知)
2. daily_scheduler は config 欠落時 fail-safe OFF (`_emq_cfg.get('enabled', False)`)
   = 過去の暗黙 ON (`get('enabled', True)`) への退行を検知

出典: code-reviewer (Opus 4.8) HIGH-A / Codex 指摘 (relist=明示OFF と apply_queue=暗黙ON の非対称)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def test_apply_queue_enabled_false_in_config():
    cfg = json.loads((_PROJECT_ROOT / "config" / "schedule_config.json").read_text(encoding="utf-8"))
    aq = (cfg.get("tasks_enabled") or {}).get("ebaymag_apply_queue")
    assert aq is not None, "ebaymag_apply_queue ブロックが schedule_config.json に存在しない"
    assert aq.get("enabled") is False, "money-direct タスクは canary 前 enabled=false 必須"


def test_apply_queue_scheduler_default_failsafe_off():
    src = (_PROJECT_ROOT / "daily_scheduler.py").read_text(encoding="utf-8")
    assert "_emq_cfg.get('enabled', False)" in src, "config 欠落時 fail-safe OFF が崩れている"
    assert "_emq_cfg.get('enabled', True)" not in src, "暗黙 ON (get('enabled', True)) への退行"
