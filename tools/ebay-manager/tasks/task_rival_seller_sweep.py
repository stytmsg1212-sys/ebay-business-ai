#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W#3 ライバルセラー新規出品モニター — scheduled task entry。

呼出経路:
  (A) daily_scheduler (毎朝 02:30 batch / execution_times=[2]): run_rival_seller_sweep_task(config, scheduled_hour)
      ※ W244 (2026-06-10) で結線。それ以前は本 docstring の記述に反して dispatch が
        存在せず UI 経路 (B) のみだった (D1 指摘 = 文書と実装の乖離)。
  (B) UI「今すぐチェック」: run_rival_seller_sweep_task(config) 直接呼出

task_key: "rival_seller_sweep"
"""
from __future__ import annotations

import logging
import sys

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from monitor.task_execution_log import log_task_start, log_task_finish, make_batch_id

logger = logging.getLogger(__name__)

TASK_KEY = "rival_seller_sweep"
TASK_DISPLAY = "ライバルセラー新規出品モニター (W#3)"


def run_rival_seller_sweep_task(config: dict, scheduled_hour: int = 2) -> dict:
    """全 active セラーを巡回して新規出品を差分検知→AI評価→Discord通知。

    silent skip 防止 (Q0): 処理結果は必ず log_task_finish で記録。
    """
    batch_id = make_batch_id()

    log_id = log_task_start(
        task_key=TASK_KEY,
        display_name=TASK_DISPLAY,
        batch_id=batch_id,
        batch_hour=scheduled_hour,
    )

    try:
        from monitor.rival_seller_monitor import run_rival_seller_sweep
        result = run_rival_seller_sweep(config)

        summary = (
            f"sellers={result['sellers_checked']} "
            f"new={result['total_new']} "
            f"notified={result['total_notified']} "
            f"skipped={result['total_skipped']}"
        )
        if result["errors"]:
            summary += f" errors={len(result['errors'])}: {result['errors'][:2]}"

        log_task_finish(
            log_id=log_id,
            success=True,
            message=summary,
        )
        logger.info(f"[{TASK_KEY}] 完了: {summary}")
        return {"success": True, "message": summary, **result}

    except Exception as e:
        msg = f"予期しないエラー: {e}"
        logger.error(f"[{TASK_KEY}] {msg}", exc_info=True)
        log_task_finish(
            log_id=log_id,
            success=False,
            message=msg,
        )
        return {"success": False, "message": msg}
