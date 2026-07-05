#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W322 夕方 refresh (19:30 JST): 競合再観測 + 分類 + AI店長「今夜の価格対応候補」digest.

設計書: .company/engineering/docs/2026-07-04-daily-workflow-design.md §4/§6

夜の部 (21:00-23:30) 開始時点で AI店長の判断材料 (競合の売れ行き・在庫・分類) が
朝のまま 15-18h stale になる構造的ギャップの解消。eBay API 日次プールは 16:00 JST
リセットのため 19:30 は早朝バッチと競合しない。

責務:
  1. competitor_snapshot の 2 回目実行 (既存 tasks.task_competitor_snapshot.
     run_competitor_snapshot をそのまま再利用、GetItem cap は既存 config を継承)。
  2. rival_classify の実行 (既存 tasks.task_rival_classify.run_rival_classify を
     そのまま再利用)。対象は status='new' の discovery のみなので、当日中に
     新規発見された discovery だけが自然に処理される (既存フィルタで成立、
     朝分の再処理は起きない)。
  3. AI店長 夕方 digest: 上記 2 タスク後の競合スナップショットから
     monitor.evening_digest.get_evening_price_candidates() で「今夜の価格対応候補」
     を抽出し、Discord (category='rival') へ通知。0 件の日も「対応候補なし」を
     明示送信する (Q0: silent skip との誤認防止)。
     同じ抽出クエリを tabs/tab_today_tasks.py が render 時に直接呼び出し、
     タブ上部にも同内容を表示する (通知と画面表示のデータソースを共有)。

kill switch: config['tasks_enabled']['evening_refresh']['enabled'] (default True)。
  無効時は内包する competitor_snapshot / rival_classify 自体は起動しない
  (それぞれの独立 cron 側の enabled 判定とは独立 — 19:30 の 2 回目実行だけを止めたい
  場合に本フラグを使う)。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# pythonw.exe gotcha guard (stdout が capture されず reconfigure 不能な環境向け)
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError, OSError) as _e:
        logger.debug(f"stdout reconfigure skip: {_e}")


def _send_evening_digest(config: dict, candidates: list) -> None:
    """AI店長 夕方 digest を Discord へ (通知失敗で run_evening_refresh を失敗にしない)."""
    try:
        from monitor.evening_digest import format_digest_body
        from notifiers.notification_center import record_and_maybe_send

        body = format_digest_body(candidates)
        title = f"🤖 AI店長 夕方 — 今夜の価格対応候補 {len(candidates)} 件"
        dedupe_key = f"evening_refresh_digest_{date.today().isoformat()}"
        record_and_maybe_send(
            "rival", "info", title, body,
            link_target="rival", dedupe_key=dedupe_key,
        )
    except Exception as e:  # noqa: BLE001 — 通知失敗を run 全体の失敗にしない (Q0)
        logger.warning(f"[W322] 夕方 digest 通知失敗 (痕跡のみ): {e}")


def run_evening_refresh(config: Optional[dict] = None) -> dict:
    """cron 経路. daily_scheduler.py から呼ばれる (毎日 19:30 JST)."""
    cfg = config or {}
    task_cfg = (cfg.get('tasks_enabled') or {}).get('evening_refresh') or {}
    result: dict = {
        "success": False,
        "competitor_snapshot": None,
        "rival_classify": None,
        "digest_candidates": 0,
        "message": "",
    }

    # ── kill switch (Q0: skip も痕跡) ──
    if not task_cfg.get('enabled', True):
        result["success"] = True
        result["message"] = "evening_refresh: enabled=false → skip"
        logger.info(result["message"])
        return result

    try:
        from tasks.task_competitor_snapshot import run_competitor_snapshot
        from tasks.task_rival_classify import run_rival_classify

        snap_result = run_competitor_snapshot(cfg)
        result["competitor_snapshot"] = snap_result

        classify_result = run_rival_classify(cfg)
        result["rival_classify"] = classify_result

        from monitor.evening_digest import get_evening_price_candidates
        candidates = get_evening_price_candidates()
        result["digest_candidates"] = len(candidates)
        _send_evening_digest(cfg, candidates)

        # snapshot/classify いずれかが失敗しても digest 送信は継続 (Q0: 部分失敗を隠さない)。
        result["success"] = bool(snap_result.get("success")) and bool(classify_result.get("success"))
        result["message"] = (
            f"snapshot_success={snap_result.get('success')} "
            f"classify_success={classify_result.get('success')} "
            f"digest_candidates={len(candidates)}"
        )
        logger.info(f"[W322] {result['message']}")
    except Exception as e:
        logger.exception("[W322] run_evening_refresh failed")
        result["message"] = f"top-level: {type(e).__name__}: {e}"
        result["success"] = False
    return result


if __name__ == "__main__":
    # 手動 CLI 実行用 (cron 同等)。
    from daily_scheduler import load_config  # type: ignore
    _cfg = load_config()
    _r = run_evening_refresh(_cfg)
    print(json.dumps(_r, indent=2, ensure_ascii=False, default=str))
