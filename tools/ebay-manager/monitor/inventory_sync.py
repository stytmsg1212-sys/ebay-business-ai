"""W133 (2026-05-16): 有在庫 listing の在庫数を eBay へ反映する共通モジュール.

3 箇所が呼ぶ単一エントリポイント:
  1. tasks/task_order_alert._decrement_inventory_for_stock_sku 直後 (売れて減算)
  2. tasks/task_purchase_confirm.confirm_purchase / undo_purchase (入荷で加算)
  3. tabs/tab_product_management._save_product_data (user 手動編集)

設計方針:
  - listing 識別は **必ず ebay_item_id** (sku-rules.md / migration v26 単位化準拠).
    SKU は本モジュールでは一切使わない (キー/集約/フィルタとも不使用).
  - Defect 率最優先: EndItem / RelistFixedPriceItem は **絶対呼ばない**.
    在庫0 は ReviseInventoryStatus の数量0 のみ. **かつ** 数量0 revise の前に
    Out-of-Stock Control が ON か機械検証 (get_out_of_stock_control_enabled).
    ON 確認できない / 不明なら数量0 revise を **実行せず** 抑止し痕跡を残す.
  - Q0 silent skip 禁止: 失敗 / 抑止は qty_sync_error 列 + 戻り dict の
    success:False で必ず痕跡化 (偽装成功しない). 自動リトライはしない
    (Q4: 本フェーズ対象外、リトライ層は別 W).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# OOS Control が **ON と確認できた場合のみ** 1 プロセス内でキャッシュ
# (GetUserPreferences の過剰 API call 回避). OFF / 不明 (API 失敗) は
# キャッシュせず毎回再問い合わせ: user が後から ON 化しても scheduler 再起動
# 不要で反映し、一時障害でも永続抑止しないため (HIGH-2 fix, 2026-05-16).
_oos_control_cache: Optional[bool] = None


def _get_credentials() -> Optional[tuple]:
    """eBay API 認証情報 (app_id, dev_id, cert_id, user_token) を取得."""
    try:
        from monitor.credentials import get_ebay_credentials
        return get_ebay_credentials()
    except (ImportError, KeyError, OSError, ValueError) as e:
        logger.error(f"認証取得失敗: {e}")
        return None


def _resolve_oos_control(creds: tuple) -> Optional[bool]:
    """OOS Control の ON/OFF を機械検証 (キャッシュ付き).

    Returns: True (ON) / False (OFF) / None (不明 = API 失敗等).
    """
    global _oos_control_cache
    if _oos_control_cache is not None:
        return _oos_control_cache
    from monitor.ebay_client import get_out_of_stock_control_enabled
    app_id, dev_id, cert_id, user_token = creds
    enabled = get_out_of_stock_control_enabled(app_id, dev_id, cert_id, user_token)
    if enabled is True:
        # ON のみ恒久キャッシュ (確定 ON は安定). OFF/None はキャッシュせず
        # 毎回再問い合わせ = user が後から ON 化した時に再起動不要で反映.
        _oos_control_cache = True
    return enabled


def _record_sync_error(ebay_item_id: str, message: str) -> None:
    """qty_sync_error 列に失敗 / 抑止理由を記録 (痕跡層 1/4)."""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "UPDATE ebay_listings SET qty_sync_error=? WHERE ebay_item_id=?",
            (message, ebay_item_id),
        )


def sync_listing_quantity(ebay_item_id: str) -> dict:
    """ebay_listings.inventory_count を eBay の出品数量へ反映する.

    Args:
        ebay_item_id: listing 識別 (eBay 一意 ID). SKU は使わない.

    Returns dict:
        success           : bool
        ebay_item_id       : str
        target_quantity    : int | None  (反映しようとした数量 = inventory_count)
        skipped_zero_unsafe: bool        (在庫0 だが OOS 未確認で抑止した)
        message            : str
    """
    from monitor.database import get_conn

    base = {
        "success": False,
        "ebay_item_id": ebay_item_id,
        "target_quantity": None,
        "skipped_zero_unsafe": False,
        "message": "",
    }

    with get_conn() as c:
        row = c.execute(
            "SELECT inventory_count, quantity_ebay FROM ebay_listings "
            "WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
    if row is None:
        msg = f"ebay_item_id={ebay_item_id} が ebay_listings に無い"
        logger.warning(f"[inventory_sync] {msg}")
        base["message"] = msg
        return base

    target = row["inventory_count"]
    if target is None:
        msg = "inventory_count=NULL (在庫数未入力) のため sync skip"
        logger.info(f"[inventory_sync] {ebay_item_id}: {msg}")
        base["message"] = msg
        return base

    target = int(target)
    base["target_quantity"] = target

    creds = _get_credentials()
    if not creds:
        msg = "eBay 認証取得失敗のため sync 不可"
        logger.error(f"[inventory_sync] {ebay_item_id}: {msg}")
        _record_sync_error(ebay_item_id, msg)
        base["message"] = msg
        return base

    # ── Defect ゲート: 在庫0 → OOS Control が ON と機械検証できた時のみ実行 ──
    if target == 0:
        oos_on = _resolve_oos_control(creds)
        if oos_on is not True:
            reason = (
                "OOS Control が ON と確認できない (OFF or 不明) ため "
                "数量0 revise を抑止 (listing 自動 End / Defect 防止)"
            )
            logger.warning(f"[inventory_sync] {ebay_item_id}: {reason}")
            _record_sync_error(ebay_item_id, reason)
            base["skipped_zero_unsafe"] = True
            base["message"] = reason
            return base

    # ── ReviseInventoryStatus で数量反映 (target>0 or target0+OOS ON) ──
    from monitor.ebay_client import revise_inventory_quantity
    app_id, dev_id, cert_id, user_token = creds
    api = revise_inventory_quantity(
        ebay_item_id, target, app_id, dev_id, cert_id, user_token
    )

    if not api.get("success"):
        msg = api.get("message") or "ReviseInventoryStatus 失敗 (詳細不明)"
        logger.error(f"[inventory_sync] {ebay_item_id}: {msg}")
        _record_sync_error(ebay_item_id, msg)
        base["message"] = msg
        return base

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        c.execute(
            """UPDATE ebay_listings
               SET quantity_ebay=?, last_synced_quantity=?,
                   last_qty_sync_at=?, qty_sync_error=NULL
               WHERE ebay_item_id=?""",
            (target, target, now, ebay_item_id),
        )
    logger.info(
        f"[inventory_sync] {ebay_item_id}: eBay 数量を {target} に反映成功"
    )
    base["success"] = True
    base["message"] = f"eBay 数量を {target} に反映"
    return base
