"""eBaymag 送料ポリシー band のライフサイクルフック (W284 Phase3, 2026-06-21).

設計書: `.company/engineering/docs/2026-06-21-ebaymag-shipping-policy-automation-design.md`
§8 (ライフサイクルフック) / §15 HIGH-2 (案b) / MED-2 (band 更新と enqueue を同一 tx)。

役割:
  - weight 変更 / 新規 import discover 時に ebay_listings.ebaymag_shipping_band を
    `band_for_weight_g` で設定し、反映キューへ enqueue する (reason=shipping_policy)。

責務分離 (mapping module は値レイヤ専用 = mutation 禁止):
  - 値生成 (build_canonical_policy / band_for_weight_g) … ebaymag_policy_mapping.py
  - DB mutation (band 設定 + enqueue, applied token 記録) … database.py
  - ライフサイクル接続 (weight → band → enqueue) … 本モジュール

band 更新と enqueue は database.set_ebaymag_shipping_band_and_enqueue が
同一 DB トランザクションで行う (消化との race 防止、MED-2)。
識別キーは ebay_item_id (SKU 禁止、sku-rules.md)。
mutation は eBaymag/CDP には一切しない (DB のみ。CDP 付替は消化タスクの責務)。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def sync_shipping_band_for_listing(ebay_item_id: str, weight_g: float | None) -> str | None:
    """1 listing の weight から band を導出し、変化があれば band 設定 + enqueue する。

    weight 変更 / 新規 discover の各ライフサイクルイベントから呼ぶ統一エントリポイント。
    現 band と新 band が同じなら DB を触らない (K1 / 無駄な enqueue 回避)。

    Args:
        ebay_item_id: 対象 listing。
        weight_g: 重量 (g)。None / 非正の場合は band を決定できないため
                  何もせず None を返す (Q0: 無効重量を黙って band に落とさない。
                  ただし weight 未設定の listing は多数あるため例外ではなく warning ログ)。

    Returns:
        設定した新 band 文字列。スキップ時 (weight 無効 / listing 不在 / 変化なし) は None。
    """
    if not ebay_item_id:
        raise ValueError("ebay_item_id は必須です")

    if weight_g is None:
        logger.debug(
            "sync_shipping_band_for_listing: weight 未設定で band スキップ eid=%s",
            ebay_item_id,
        )
        return None

    from monitor.ebaymag_policy_mapping import band_for_weight_g
    try:
        new_band = band_for_weight_g(weight_g)
    except ValueError as e:
        # 非正/非数の weight は band 不能。silent skip せず warning で痕跡を残す (Q0)。
        logger.warning(
            "sync_shipping_band_for_listing: band 導出不能 eid=%s weight_g=%r: %s",
            ebay_item_id, weight_g, e,
        )
        return None

    from monitor.database import get_ebaymag_policy_state, set_ebaymag_shipping_band_and_enqueue

    state = get_ebaymag_policy_state(ebay_item_id)
    if state is None:
        logger.warning(
            "sync_shipping_band_for_listing: listing 不在 eid=%s (band 設定せず)",
            ebay_item_id,
        )
        return None

    current_band = state.get("band")
    if current_band == new_band:
        # 帯が変わっていない = 付替不要 (K1 / enqueue しない)。
        logger.debug(
            "sync_shipping_band_for_listing: band 変化なし eid=%s band=%s",
            ebay_item_id, new_band,
        )
        return None

    ok = set_ebaymag_shipping_band_and_enqueue(ebay_item_id, new_band)
    if ok:
        logger.info(
            "sync_shipping_band_for_listing: band 更新+enqueue eid=%s %s→%s",
            ebay_item_id, current_band, new_band,
        )
        return new_band
    return None
