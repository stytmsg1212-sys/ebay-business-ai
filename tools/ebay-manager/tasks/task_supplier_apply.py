#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仕入先候補 反映タスク（採用→ReviseItem→applied）

UI「反映」ボタンから呼ばれる。以下を一連で実行する:
  1. 候補を取得（status='accepted'のみ有効）
  2. 候補URLから新SKUを生成（sku_mapping_manager.url_to_sku）
  3. eBay Trading API `ReviseItem` で SKU を更新
  4. 成功したら ebay_listings.sku 追従、supplier_candidates.status='applied'

W100 (2026-05-06): 旧仕様「採用後 24h 猶予 (ヤフオク)」を完全削除.
  - 旧コメント「Yahoo 出品は落札リスクがあるため」は誤り
  - 24h 猶予の本来意図は「ヤフオク終了 → 1日後の再出品を待つ」リサーチ前待機
  - 新仕様 (W100 Phase 3) で inventory_check 側に grace を移管
  - 反映 (apply) 経路は猶予なし、即時 ReviseItem
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import (  # noqa: E402
    build_source_url,
    get_conn,
    get_supplier_candidate_by_id,
    get_ebay_listing_by_item_id,
    update_ebay_listing_sku,
    update_supplier_candidate_status,
    upsert_item,
)
from monitor.ebay_client import revise_item_sku  # noqa: E402
from monitor.credentials import get_ebay_credentials, ebay_credentials_ok  # noqa: E402
from sku_mapping_manager import url_to_sku  # noqa: E402
# W(stock-gate): module-level import で test monkeypatch 互換 (W182 流儀)
from monitor.scrapers import check_candidate_availability  # noqa: E402

logger = logging.getLogger(__name__)


def accept_supplier_candidate(candidate_id: int) -> dict:
    """採用ボタン処理. status='accepted' に遷移するだけ.

    W100 (2026-05-06): 旧仕様で source_platform='yahoo_auctions' の場合に
    yahoo_grace_until = now + 24h をセットしていたロジックを削除.
    猶予の本来意図はリサーチ前待機 (inventory_check 側、Phase 3) であり、
    採用後の反映遅延は不要.
    """
    c = get_supplier_candidate_by_id(candidate_id)
    if not c:
        return {"success": False, "message": f"候補 id={candidate_id} が見つかりません"}

    update_supplier_candidate_status(candidate_id, "accepted")
    return {"success": True, "message": "採用しました。反映ボタンで eBay に SKU 変更を適用できます"}


def apply_supplier_candidate(
    candidate_id: int,
    config: dict,
    allow_alt_override: bool = False,
) -> dict:
    """反映ボタン処理。eBay Trading API ReviseItem を実行し、ローカルDBを追従させる。

    Args:
        candidate_id: supplier_candidates.id
        config: schedule_config.json ロード済 dict（ebay 資格情報を含む）
        allow_alt_override: True の時のみ、alt_only 候補 (score<60 + alt=1) の
            SKU書換ブロックをスキップする。user が「別商品の可能性はあるが、この
            仕入先URLで現 listing を更新したい」と明示判断した場合専用。
            既定 False = 既存呼び出し側の挙動は不変（後方互換）。
            #35 (2026-06-28) 別SKU候補の手動 override 採用機能。

    Returns:
        {'success': bool, 'message': str, 'new_sku': Optional[str]}
    """
    c = get_supplier_candidate_by_id(candidate_id)
    if not c:
        return {"success": False, "message": f"候補 id={candidate_id} が見つかりません"}

    if c.get("status") != "accepted":
        return {
            "success": False,
            "message": f"反映には status='accepted' が必要です (current={c.get('status')!r})",
        }

    # alt_listing のみの候補は「別SKU新規出品機会」であり SKU書き換え反映は不適切。
    # ただし allow_alt_override=True の場合は user が明示確認した上での手動 override。
    score = c.get("match_score") or 0
    alt_only = (score < 60) and bool(c.get("alt_listing_possible"))
    if alt_only and not allow_alt_override:
        return {
            "success": False,
            "message": (
                f"別SKU出品機会の候補は反映(SKU書換)対象外です "
                f"(score={score}, alt_listing_possible=1)。新規出品フローをご利用ください。"
            ),
        }

    # 退役済listingへの反映は不可（ebay_sync が退役マーキングした場合ブロック）
    listing = get_ebay_listing_by_item_id(c["ebay_item_id"])
    if listing and listing.get("is_ended"):
        return {
            "success": False,
            "message": (
                f"このSKUの eBay listing は退役済です "
                f"(ended_at={listing.get('ended_at')}, reason={listing.get('ended_reason')}). "
                f"ReviseItem は実行できません。"
            ),
        }

    new_sku = url_to_sku(c.get("candidate_url") or "")
    if not new_sku:
        return {
            "success": False,
            "message": f"候補URLから新SKUを生成できませんでした: {c.get('candidate_url')!r}",
        }

    ebay_cfg = get_ebay_credentials(config)
    if not ebay_credentials_ok(ebay_cfg):
        return {
            "success": False,
            "message": "eBay 認証情報不足 (.env または schedule_config.json の ebay セクション)",
        }

    ebay_item_id = c.get("ebay_item_id")
    if not ebay_item_id:
        return {"success": False, "message": "候補に ebay_item_id がありません"}

    # 在庫ゲート: ReviseItem 直前 (全安価チェック通過後、最重い処理の直前)
    # unavailable / not_found → SKU 書換をブロック (候補 status は accepted のまま据置)
    # unknown → 判定保留で通過 (W182 既存ゲートと同一方針)
    try:
        _avail = check_candidate_availability(c.get("candidate_url") or "")
    except Exception as _e:
        logger.warning("在庫チェック例外→unknown続行: cid=%s %s", candidate_id, _e)
        _avail = {"status": "unknown", "signal": f"exception: {type(_e).__name__}"}
    _avail_status = _avail.get("status")
    if _avail_status in ("unavailable", "not_found"):
        logger.info(
            "在庫チェックでブロック: cid=%s ebay_item_id=%s status=%s signal=%s",
            candidate_id, ebay_item_id, _avail_status, _avail.get("signal"),
        )
        return {
            "success": False,
            "message": (
                f"この仕入先は既に売り切れです（{_avail.get('signal', '')}）。"
                f"SKU 書換は行いませんでした"
            ),
        }
    if _avail_status == "unknown":
        logger.info(
            "在庫確認できず(unknown)続行: cid=%s ebay_item_id=%s signal=%s",
            candidate_id, ebay_item_id, _avail.get("signal"),
        )

    if alt_only and allow_alt_override:
        logger.warning(
            f"別SKU手動override採用: ItemID={ebay_item_id} score={score} "
            f"alt_listing_possible=1 → ReviseItem 実行 (user 明示確認済, cid={candidate_id})"
        )
    logger.info(f"ReviseItem 実行: ItemID={ebay_item_id} new_sku={new_sku}")
    result = revise_item_sku(
        item_id=ebay_item_id,
        new_sku=new_sku,
        app_id=ebay_cfg["app_id"],
        dev_id=ebay_cfg["dev_id"],
        cert_id=ebay_cfg["cert_id"],
        user_token=ebay_cfg["user_token"],
    )

    if not result.get("success"):
        return {
            "success": False,
            "message": f"ReviseItem 失敗: {result.get('message')}",
        }

    # DB 追従
    old_sku = c.get("sku")
    update_ebay_listing_sku(ebay_item_id, new_sku)

    # monitored_items 追従（新SKU行作成 or 既存の ebay_item_id 行を新SKUに更新）
    # 旧仕入元の暫定行 (ebay_item_id 未紐付け) は is_active=0 で孤立化（履歴は残す）
    # W72 (2026-05-01): UNIQUE(sku) 撤廃後は同 sku 多 listing が共存するため、
    # 旧 `WHERE sku=? AND ebay_item_id != ?` は無関係 listing 巻き添えリスク。
    # 対象 listing の旧仕入元 URL ベース identify に変更 (= 1 暫定行のみ deactivate)。
    try:
        listing_title = c.get("candidate_title") or ""
        upsert_item(sku=new_sku, ebay_item_id=ebay_item_id, title=listing_title)
        if old_sku and old_sku != new_sku:
            old_source_url = build_source_url(old_sku)
            if old_source_url:
                with get_conn() as _mi_conn:
                    _mi_conn.execute(
                        "UPDATE monitored_items SET is_active=0 "
                        "WHERE source_url=? "
                        "AND (ebay_item_id IS NULL OR ebay_item_id='')",
                        (old_source_url,),
                    )
        # 同 listing (ebay_item_id) に紐づいた pending supplier_candidates は superseded.
        # 2026-04-20 (HIGH-3): auto_rejected=1 を立てて Phase 1 学習履歴から除外.
        # 2026-05-01 (W81 CRITICAL fix): 旧 `WHERE sku=?` は stock:01 等の同 SKU 多 listing
        # で他 listing の pending 候補が巻き添え rejected 化する重大バグだった.
        # ebay_item_id 主導で 1 listing 限定の supersede に修正.
        # 2026-05-09 (W113 fix): alt_listing_possible=1 候補は「別 SKU で新規出品する機会」
        # であり置換 (apply) とは独立 lifecycle. 巻き添え auto_rejected を防ぐため
        # WHERE 句に COALESCE(alt_listing_possible, 0) = 0 を追加。
        # 事故事例: 5/8 W112 verify 中に ItemID 358274830101 で id=508 (TR6143 alt 候補) が
        # apply 副作用で auto_rejected された。
        with get_conn() as _sup_conn:
            _sup_conn.execute(
                "UPDATE supplier_candidates "
                "SET status='rejected', auto_rejected=1, user_action_at=CURRENT_TIMESTAMP "
                "WHERE ebay_item_id=? AND status='pending' AND id != ? "
                "  AND COALESCE(alt_listing_possible, 0) = 0",
                (ebay_item_id, candidate_id),
            )
    except Exception as _e:
        logger.warning(f"monitored_items/supplier_candidates 追従エラー: {_e}")

    update_supplier_candidate_status(candidate_id, "applied")

    _override_note = " [別SKU手動override採用]" if (alt_only and allow_alt_override) else ""
    logger.info(f"反映成功: sku={c['sku']} → {new_sku}{_override_note}")
    return {
        "success": True,
        "new_sku": new_sku,
        "message": (
            f"eBay に反映しました（SKU: {c['sku']} → {new_sku}）{_override_note}"
        ),
    }


if __name__ == "__main__":
    # 手動テスト: python -m tasks.task_supplier_apply <candidate_id>
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python -m tasks.task_supplier_apply <candidate_id>")
        sys.exit(1)

    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}

    cid = int(sys.argv[1])
    print(json.dumps(apply_supplier_candidate(cid, cfg), indent=2, ensure_ascii=False))
