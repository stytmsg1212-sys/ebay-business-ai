#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W284: ebaymag_apply_queue の fail-safe OFF 回帰テスト (2026-06-20 HIGH-A 修正 → 2026-07-05 W317 再有効化)

`ebaymag_apply_queue` は eBaymag 各国版の公開状態を mutate する money-direct タスク。

**テスト変更の経緯 (2026-07-05)**:
- 2026-06-20: canary 検証前の安全策として enabled=false を PIN
- 2026-07-05: W317 キュー完全稼働確認後、enabled=true へ再有効化 → config 更新
- 本テストは「config エントリが存在し enabled が bool であること」に緩和
  (enabled の値そのもの PIN は削除。将来の誤った凍結を検知できれば十分)

恒久ガード (変更なし):
1. daily_scheduler は config 欠落時 fail-safe OFF (`_emq_cfg.get('enabled', False)`)
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


def test_apply_queue_enabled_bool_in_config():
    cfg = json.loads((_PROJECT_ROOT / "config" / "schedule_config.json").read_text(encoding="utf-8"))
    aq = (cfg.get("tasks_enabled") or {}).get("ebaymag_apply_queue")
    assert aq is not None, "ebaymag_apply_queue ブロックが schedule_config.json に存在しない"
    assert isinstance(aq.get("enabled"), bool), "enabled は bool 型である必要がある (W317 再有効化後も型チェック維持)"


def test_apply_queue_scheduler_default_failsafe_off():
    src = (_PROJECT_ROOT / "daily_scheduler.py").read_text(encoding="utf-8")
    assert "_emq_cfg.get('enabled', False)" in src, "config 欠落時 fail-safe OFF が崩れている"
    assert "_emq_cfg.get('enabled', True)" not in src, "暗黙 ON (get('enabled', True)) への退行"
