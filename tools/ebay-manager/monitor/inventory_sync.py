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
    """eBay API 認証情報を (app_id, dev_id, cert_id, user_token) タプルで取得.

    get_ebay_credentials() は **dict** を返す. dict のまま呼び出し側で
    `a, b, c, d = creds` するとキー文字列 ("app_id" 等) が入り eBay 認証が
    静かに全滅する (2026-05-16 W133 item2 実機検証準備のコード精査で発見;
    pytest は tuple の fake creds を mock していたため不可視だった).
    ここで明示的にタプル化し、4 値が 1 つでも空なら None を返して
    呼び出し側の `if not creds` ガードを機能させる.
    """
    try:
        from monitor.credentials import (
            ebay_credentials_ok,
            get_ebay_credentials,
        )
        creds = get_ebay_credentials()
        if not ebay_credentials_ok(creds):
            logger.error(
                "eBay 認証情報が未設定 "
                "(app_id/dev_id/cert_id/user_token のいずれか空)"
            )
            return None
        return (
            creds["app_id"], creds["dev_id"],
            creds["cert_id"], creds["user_token"],
        )
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


def sync_listing_quantity(
    ebay_item_id: str, explicit_quantity: Optional[int] = None
) -> dict:
    """ebay_listings の出品数量を eBay へ反映する.

    Args:
        ebay_item_id: listing 識別 (eBay 一意 ID). SKU は使わない.
        explicit_quantity: 反映する数量を明示指定する場合に渡す.
            None の時は **有在庫の inventory_count を読んで反映** (従来動作).
            非 None の時は inventory_count を読まず、その値をそのまま反映する
            (W205 2026-05-31: 無在庫 listing は inventory_count=NULL のため、
             user が UI で入力した quantity_ebay を直接 eBay へ送る経路に使う).
            無在庫は Amazon/楽天/Yahoo から無限調達可能なので、売れて0になり
            販売機会を逃すのを防ぐ目的で手動で数量を持ち上げる.

    Returns dict:
        success           : bool
        ebay_item_id       : str
        target_quantity    : int | None  (反映しようとした数量)
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

    if explicit_quantity is not None:
        # W205: 明示数量モード (無在庫 listing 等、inventory_count を読まない).
        target = explicit_quantity
        if target < 0:
            msg = f"explicit_quantity={target} が負 (0 以上が必須)"
            logger.warning(f"[inventory_sync] {ebay_item_id}: {msg}")
            base["message"] = msg
            return base
    else:
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
    # eBay セマンティクス注意 (2026-05-16 本番実機検証で確定):
    #   ReviseInventoryStatus の <Quantity> は **available 数量**として解釈され、
    #   eBay は内部で 総Quantity = 投入値 + QuantitySold を保存する.
    #   (実測: 投入29, QuantitySold20 → GetItem Quantity=49). 一方 GetItem の
    #   Item/Quantity は **総数量(available+sold)** なので両者を混同しないこと.
    #   ここで target(=inventory_count = 手元の available 在庫) をそのまま渡すのは
    #   「eBay available = 手元在庫数」をセットする意図で **設計上正しい**.
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
