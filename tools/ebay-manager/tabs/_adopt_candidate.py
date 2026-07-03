#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仕入先候補 採用ロジック 単一実装 (W314 Phase 3 T1)。

旧: tab_inventory_monitor._adopt_and_apply と
    tab_supplier_candidates._render_candidate_card 内 (通常採用 + alt_only 2段確認
    override 採用の 2 経路) に、それぞれ独立実装されていた
    「accept_supplier_candidate → apply_supplier_candidate → followup フラグ set」
    を本モジュール 1 箇所に統合。

含める範囲 (確認後の「実行部」のみ、K1):
  - accept (is_pending=True のときのみ、status: pending→accepted)
  - apply (eBay ReviseItem で SKU 反映、allow_alt_override 対応)
  - followup フラグ set (写真/description の展開プロンプト、商品仕上げパネルが読む)
  - revive (元 quantity_ebay=0 だった listing のみ) の 0→1 自動復元

含めない範囲 (呼出側タブに残す、K1):
  - alt_only 候補の 2 段確認 UI (warning + 確定/やめる ボタン)
  - eBay 認証情報の事前ガード文言・通知 UI (呼出側の notice/message queue へ変換)
  - ボタン二重押下防止の session_state lock
  - st.rerun() の scope 選択 (呼出側の UI 都合)

qty 復元の判定について:
  - revive / replace / 在庫監視 (在庫監視タブの `_adopt_and_apply` 経路) の
    3 経路は旧実装と等価挙動 (0→1 復元 / 復元しない、いずれも旧観測と一致)。
  - alt override (`allow_alt_override=True`、別SKU候補の手動採用) は旧実装
    (`tab_supplier_candidates.py` sup_accept_alt_confirm 経路) 同様
    **qty 復元しない**。理由: 別商品の可能性があるため自動販売再開しない
    (数量はパネルから手動反映可能)。W314 Phase 3 T1 code-reviewer HIGH-1
    (+ Codex 独立検出) で確認済。
  - qty 判定は apply 直前の `get_ebay_listing_by_item_id` fresh read で行う
    (render 時分類ではなく実測値)。これは意図的なトレードオフで、
    render→click 間の DB 変化に強く、呼出側の分類 (context / バケツ)
    への結合も不要になる。旧実装は render 時分類 (revive バケツ、要対応
    quantity_ebay>=1 抽出) だったが、上記等価性の範囲で fresh read に統一。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def adopt_candidate(
    cid: int,
    config: dict,
    *,
    source_tab: str,
    is_pending: bool = True,
    allow_alt_override: bool = False,
) -> dict:
    """仕入先候補を採用 → eBay へ SKU 反映まで一気通貫実行する。

    Args:
        cid: supplier_candidates.id
        config: schedule_config.json ロード済 dict (eBay 資格情報を含む)
        source_tab: 呼出元タブ名 (ログ痕跡用、"inventory" | "supplier")
        is_pending: True なら accept (status: pending→accepted) から実行。
            False なら既に accepted 済 (在庫監視タブ「反映」ボタン再試行) として
            accept をスキップし apply のみ実行。
        allow_alt_override: True の時のみ alt_only 候補 (score<60 + alt=1) の
            SKU 書換ブロックをスキップする (仕入先候補タブの 2 段確認 override 専用)。

    Returns:
        {
          "success": bool,
          "message": str,          # 主メッセージ (呼出側の通知テキストにそのまま使える)
          "stage": Optional[str],  # 失敗時のみ "accept" | "apply" | "lookup"
          "eid": str,               # ebay_item_id (取得できなければ "")
          "qty_restored": bool,     # revive (元 quantity_ebay=0) の 0→1 復元が成功したか
          "qty_restore_message": Optional[str],  # 復元を試みた場合のみ (成功/失敗いずれも設定)
          "qty_restore_ok": Optional[bool],       # 上記に対応する成否 (通知レベル切替用)
        }
    """
    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    from monitor.database import (
        get_ebay_listing_by_item_id,
        get_supplier_candidate_by_id,
        update_ebay_listing_quantity,
    )
    from monitor.ebay_client import revise_inventory_quantity
    from tasks.task_supplier_apply import accept_supplier_candidate, apply_supplier_candidate
    from ui_cache import bump_db_version

    cand = get_supplier_candidate_by_id(cid)
    eid = (cand.get("ebay_item_id") if cand else "") or ""

    # revive 判定用に apply 直前の quantity を fresh read で記録。
    # 意図的トレードオフ (Codex MED): render 時分類 (revive/replace バケツ) では
    # なく apply 直前の実測値で判定することで、render→click 間の DB 変化
    # (別セッション/バッチによる qty 更新) に強くなり、呼出側の分類ロジックへの
    # 結合も不要になる。
    _listing = get_ebay_listing_by_item_id(eid) if eid else None
    _qty_before: Optional[int] = (
        int(_listing.get("quantity_ebay") or 0) if _listing else None
    )

    if is_pending:
        res_a = accept_supplier_candidate(cid)
        if not res_a.get("success"):
            logger.error(
                "adopt_candidate: accept failed cid=%s eid=%s source_tab=%s msg=%s",
                cid, eid, source_tab, res_a.get("message"),
            )
            return {
                "success": False,
                "message": res_a.get("message") or "採用に失敗しました",
                "stage": "accept",
                "eid": eid,
                "qty_restored": False,
                "qty_restore_message": None,
                "qty_restore_ok": None,
            }

    res_b = apply_supplier_candidate(cid, config, allow_alt_override=allow_alt_override)
    if not res_b.get("success"):
        logger.error(
            "adopt_candidate: apply failed cid=%s eid=%s source_tab=%s msg=%s",
            cid, eid, source_tab, res_b.get("message"),
        )
        return {
            "success": False,
            "message": res_b.get("message") or "eBay 反映に失敗しました",
            "stage": "apply",
            "eid": eid,
            "qty_restored": False,
            "qty_restore_message": None,
            "qty_restore_ok": None,
        }

    # followup フラグ (写真/description 展開プロンプト、商品仕上げパネルが読む) —
    # 両旧実装が成功時に必ず set していた session_state (単一化の中核)。
    import streamlit as st

    cand_title = (cand.get("candidate_title") if cand else "") or ""
    cand_url = (cand.get("candidate_url") if cand else "") or ""
    st.session_state[f"_sup_photo_prompt_{cid}"] = True
    st.session_state[f"_sup_desc_prompt_{cid}"] = True
    st.session_state[f"_sup_photo_meta_{cid}"] = {
        "url": cand_url,
        "eid": eid,
        "title": cand_title,
    }

    # revive: apply 前の quantity_ebay が 0 だった listing のみ 0→1 自動復元。
    # alt override (別SKU候補の手動採用) は旧実装同様 qty 復元しない
    # (別商品の可能性があるため自動販売再開しない — 数量はパネルから手動反映可能)。
    # 旧: tab_supplier_candidates.py の sup_accept_alt_confirm 経路は
    # apply 成功後に followup フラグ set のみで revise_inventory_quantity を
    # 呼ばなかった (HEAD 逐語確認済、W314 Phase 3 T1 code-reviewer HIGH-1)。
    qty_restored = False
    qty_restore_message: Optional[str] = None
    qty_restore_ok: Optional[bool] = None
    if eid and _qty_before == 0 and not allow_alt_override:
        ebay_creds = get_ebay_credentials(config)
        if not ebay_credentials_ok(ebay_creds):
            logger.error(
                "adopt_candidate: qty restore 認証不足 cid=%s eid=%s source_tab=%s",
                cid, eid, source_tab,
            )
            qty_restore_ok = False
            qty_restore_message = (
                f"{eid}: eBay 認証不足のため qty 復元失敗 "
                f"(SKU は書換済、手動で在庫を 1 に戻してください、cid={cid})"
            )
        else:
            try:
                _qres = revise_inventory_quantity(eid, 1, **ebay_creds)
            except (RuntimeError, ConnectionError, TimeoutError, OSError) as _qe:
                logger.exception(
                    "adopt_candidate: qty restore exception cid=%s eid=%s source_tab=%s",
                    cid, eid, source_tab,
                )
                qty_restore_ok = False
                qty_restore_message = (
                    f"{eid}: SKU 書換成功だが qty 復元中に例外 ({_qe}). "
                    f"手動で在庫を 1 に戻してください (cid={cid})."
                )
            else:
                if _qres.get("success"):
                    update_ebay_listing_quantity(eid, 1)
                    qty_restored = True
                    qty_restore_ok = True
                    qty_restore_message = f"{eid}: 在庫 0 → 1 自動復元 (復活完了)"
                else:
                    logger.error(
                        "adopt_candidate: qty restore api_failed cid=%s eid=%s "
                        "source_tab=%s msg=%s",
                        cid, eid, source_tab, _qres.get("message"),
                    )
                    qty_restore_ok = False
                    qty_restore_message = (
                        f"{eid}: SKU 書換成功だが qty 復元失敗 "
                        f"({_qres.get('message', '')}). "
                        f"手動で在庫を 1 に戻してください (cid={cid})."
                    )

    bump_db_version()  # SKU / qty 変更後 read-cache 無効化

    return {
        "success": True,
        "message": res_b.get("message") or "採用 → eBay 反映 成功",
        "stage": None,
        "eid": eid,
        "qty_restored": qty_restored,
        "qty_restore_message": qty_restore_message,
        "qty_restore_ok": qty_restore_ok,
    }
