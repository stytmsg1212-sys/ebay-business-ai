"""task_cleanup_old_relisted — daily_relist 由来の古い is_ended=1 row を 90 日経過後に物理 DELETE。

設計理由 (2026-04-30 D 案 + 90 日 user 公認):
- daily_relist で is_ended=1 になった row は relist_history に旧→新の系譜が保存済
  (tasks/task_daily_relist.py:281 で INSERT)。ebay_listings から物理 DELETE しても履歴は失われない。
- eBay の ItemID 参照可能期間は 90 日。返品/通関対応の最大窓と整合。
- DB 肥大化防止 (1 年で ~2,500 件削減、5 年後も ~600 件で頭打ち)。

対象外 (永続保持):
- ended_reason='not_in_active_list' (qty=0 復活機能 W14 で 86 件復活実績、履歴テーブル無し)。

dangling 許容 (履歴消失なしの設計):
- relist_history.old_item_id: cooldown SELECT は relist_history のみ参照のため機能影響なし。
- supplier_candidates (status IN 'rejected'/'applied'): 旧 ItemID が dangling になるが finance/analytics の
  履歴トレース用途で永続化させる (task_daily_relist.py:268-277 で applied は明示的に追従しない設計)。
  JOIN は LEFT JOIN 推奨。

設定:
- 90 日固定 (CLEANUP_DAYS 定数、user 公認 2026-04-30)。
- schedule_config.json では `enabled` / `execution_times` のみ用途、cleanup_days パラメータは持たない (K1 Simplicity)。

定時実行: monitor/task_execution_log.TASK_SCHEDULE で `hours=[2]` 登録、daily_relist 直後。
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn  # noqa: E402

logger = logging.getLogger(__name__)

# 90 日固定 = eBay ItemID 参照可能期間。変更は user 判断後に本定数を直接書き換え (K1 Simplicity)。
CLEANUP_DAYS = 90
TARGET_REASON = "daily_relist_seo"


def run_cleanup_old_relisted(config: dict) -> dict:
    """90 日経過 daily_relist 由来 is_ended=1 row を物理 DELETE。

    Args:
        config: schedule_config.json (interface 統一のため受領、本タスク内は未使用。
                cleanup_days は CLEANUP_DAYS 定数 hardcode で source of truth)

    Returns:
        {'success': bool, 'deleted_count': int, 'message': str}
    """
    # date('now', '-90 day') = 過去 90 日前。誤って '90 day' (未来日付) にしないよう注意
    sql = (
        "DELETE FROM ebay_listings "
        "WHERE is_ended=1 "
        "  AND ended_reason=? "
        "  AND ended_at < date('now', ?)"
    )
    try:
        with get_conn() as conn:
            cursor = conn.execute(sql, (TARGET_REASON, f"-{CLEANUP_DAYS} day"))
            deleted = cursor.rowcount
        logger.info(
            f"[cleanup_old_relisted] daily_relist 由来 {CLEANUP_DAYS} 日経過 row 物理 DELETE: "
            f"{deleted} 件"
        )
        return {
            "success": True,
            "deleted_count": deleted,
            "message": f"daily_relist 由来 {deleted} 件を {CLEANUP_DAYS} 日経過後 DELETE",
        }
    except sqlite3.Error as e:
        logger.error(f"[cleanup_old_relisted] DB エラー: {e}", exc_info=True)
        return {
            "success": False,
            "deleted_count": 0,
            "message": f"DELETE エラー: {e}",
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_cleanup_old_relisted({})
    print(result)
