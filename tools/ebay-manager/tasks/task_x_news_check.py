#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task: W13 X ベース AI ニュース取得 (新統合版)

既存 task_news_check.py を **吸収統合** (code-reviewer H-3):
  - 従来の Anthropic/OpenAI 公式ブログ巡回機能はこの task に移植せず、
    本タスクは X/Reddit/HN を主軸とする.
  - 並行稼働は避け、daily_scheduler から本タスクのみを起動.
  - DB 書込みは save_news_item_v2 経由で news_items テーブルに一本化
    (source_type='x'/'reddit'/'hn' で識別).

code-reviewer H-4 対応:
  - 独立 CronJob (06:00) は daily_scheduler.py で setup_scheduler に直接追加.
  - execution_times ベースの既存フィルタだけでは発火しないため.

使用例 (手動実行):
    python -m tasks.task_x_news_check
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import save_news_item_v2  # noqa: E402
from monitor.x_news_fetchers import fetch_all  # noqa: E402
from monitor.x_news_pipeline import run_pipeline  # noqa: E402
from monitor.x_news_sources import (  # noqa: E402
    XSourcesConfigError, load_sources,
)

logger = logging.getLogger(__name__)


def run_x_news_check(config: dict | None = None) -> dict:
    """W13 統合タスク: X/Reddit/HN から AI ニュースを取得→dedupe→classify→DB 保存.

    Args:
        config: schedule_config.json 全体 dict (task 個別設定の拡張用).

    Returns:
        {'success': bool, 'fetched': int, 'saved': int, 'high': int,
         'message': str}
    """
    task_cfg = (config or {}).get("tasks_enabled", {}).get("x_news_check") or {}
    # feature flag: 統合用 kill switch
    if task_cfg is not False and task_cfg.get("enabled") is False:
        return {
            "success": True, "fetched": 0, "saved": 0, "high": 0,
            "message": "disabled (feature flag off)",
        }

    # sources.json ロード
    try:
        src_cfg = load_sources()
    except (FileNotFoundError, XSourcesConfigError) as e:
        logger.error(f"failed to load x_news_sources.json: {e}")
        return {
            "success": False, "fetched": 0, "saved": 0, "high": 0,
            "message": f"sources config error: {e}",
        }

    # --- Fetch ---
    logger.info("【開始】W13 X ニュース取得タスク")
    raw_items = fetch_all(src_cfg)
    logger.info(f"fetched {len(raw_items)} raw items")

    if not raw_items:
        return {
            "success": True, "fetched": 0, "saved": 0, "high": 0,
            "message": "0 items fetched (all sources empty or skipped)",
        }

    # --- Pipeline (dedupe + classify) ---
    try:
        classified = run_pipeline(
            raw_items, anthropic_cap_usd=src_cfg.anthropic_daily_cap_usd
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"pipeline failed: {e}", exc_info=True)
        return {
            "success": False, "fetched": len(raw_items), "saved": 0, "high": 0,
            "message": f"pipeline error: {e}",
        }

    # --- Save to DB ---
    saved = 0
    high_count = 0
    for it in classified:
        if it.impact_level == "noise":
            continue  # noise は DB にも入れない
        try:
            rid = save_news_item_v2(
                source=f"{it.source_type}:{it.source_handle}" if it.source_handle
                       else it.source_type,
                title=it.title,
                url=it.url,
                source_type=it.source_type,
                source_handle=it.source_handle,
                summary_ja=it.summary_ja,
                impact_ja=it.impact_ja,
                impact_level=it.impact_level,
                categories=it.category,
                published_at=it.published_at,
                engagement_count=it.engagement_count,
                raw_content=it.raw_content,
            )
            if rid is not None:
                saved += 1
                if it.impact_level == "high":
                    high_count += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"save failed for {it.url}: {e}")

    msg = (
        f"fetched={len(raw_items)} dedup_classified={len(classified)} "
        f"saved={saved} high={high_count}"
    )
    logger.info(f"【完了】W13 X ニュース取得: {msg}")
    return {
        "success": True,
        "fetched": len(raw_items),
        "saved": saved,
        "high": high_count,
        "message": msg,
    }


if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    r = run_x_news_check(cfg)
    print(json.dumps(r, indent=2, ensure_ascii=False))
