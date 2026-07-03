#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""採用後フォローアップ session_state クリーンアップ ロジック (2026-06-11 H-1 抽出)。

streamlit 非依存の純関数として定義し、unit test から直接 import できるようにする。
_supplier_followup_section.py の _close_supplier_followup は本関数を呼ぶ thin wrapper
(2026-06-12 依頼ボード#11: followup render が tab_supplier_candidates.py から
_supplier_followup_section.py へ移設、wrapper も同移設先に在る)。

W314 Phase 1 S3 (2026-07-03): 採用後 followup 欄のタイトル編集ロジックも本モジュールに
追加 (dirty 判定 / 原産国リスク語検出 / eBay 反映)。streamlit runtime 無しで
unit test できるよう、UI 層 (_supplier_followup_section.py) から呼ばれる薄い関数群。
"""
from __future__ import annotations

import logging
from typing import Any, MutableMapping, Optional

logger = logging.getLogger(__name__)


def close_supplier_followup_state(
    session_state: MutableMapping[str, Any],
    cid: int,
) -> None:
    """cid に紐づく採用後フォローアップ欄の session_state キーを全消し。

    photo pipeline prefix: "sup_" (_SS in _supplier_photo_pipeline.py)
    desc pipeline prefix:  "sup_desc_pipeline_" (_SS in _supplier_description_pipeline.py)
    w158 image pipeline:   "sup_desc_pipeline_{cid}_w158_" (cid が中間位置)

    endswith(f"_{cid}") は直前が "_" 固定なので cid 11 vs 111 の誤爆なし。
    w158 キーは cid が中間なので startswith で別途捕捉する。
    """
    exact = [
        f"_sup_photo_prompt_{cid}", f"_sup_photo_open_inline_{cid}",
        f"_sup_desc_prompt_{cid}", f"_sup_desc_open_inline_{cid}",
        f"_sup_photo_meta_{cid}", f"_sup_msgs_{cid}",
    ]
    for k in exact:
        session_state.pop(k, None)

    suffix = f"_{cid}"
    w158_prefix = f"sup_desc_pipeline_{cid}_w158_"
    for k in list(session_state.keys()):
        # w158 キー: cid が中間位置のため endswith では一致しない → startswith で捕捉
        if k.startswith(w158_prefix):
            session_state.pop(k, None)
            continue
        if k.endswith(suffix) and (
            k.startswith("sup_hero_")
            or k.startswith("sup_additional_")
            or k.startswith("sup_apply_")
            or k.startswith("sup_all_image_urls_")
            or k.startswith("sup_btn_")
            or k.startswith("sup_desc_pipeline_")
        ):
            session_state.pop(k, None)


# ─────────────────────────────────────────────────
# W314 Phase 1 S3 (2026-07-03): 採用後 followup 欄のタイトル編集
# ─────────────────────────────────────────────────

def title_is_dirty(new_title: str, initial_title: str) -> bool:
    """タイトルが編集され、eBay へ反映すべき状態かどうか。

    空文字への変更は dirty 扱いしない (誤って全消しして反映ボタンが
    活性化するのを防ぐ。revise_item_title 側も空文字は reject するため
    二重防御になる)。
    """
    new_t = (new_title or "").strip()
    init_t = (initial_title or "").strip()
    return bool(new_t) and new_t != init_t


# eBay 出品文への Country of Origin / Country of Manufacture 記載は CLAUDE.md で
# 禁止 (関税リスク、US Customs が原産国を再計算する根拠を与えない)。"Japan" 単体は
# ブランド名 (例: "Made in Japan Quality" ではなく "Japan Import" 等) や型番の一部で
# 普通に使われるため対象外とし、"made in" のような明示句のみ検出する (K1: 既存の
# title 用ガードが無いため最小実装、警告のみでブロックしない)。
_ORIGIN_RISK_PATTERNS: tuple[str, ...] = (
    "made in", "country of origin", "manufactured in",
    "原産国", "原産地", "製造国",
)


def detect_origin_risk_words(title: str) -> list[str]:
    """タイトルに原産国を示唆する表現が含まれるか検出する (警告専用)。

    ブロックはしない。呼び出し側 (UI) が警告バッジを出すだけに使う。
    """
    if not title:
        return []
    lowered = title.lower()
    return [p for p in _ORIGIN_RISK_PATTERNS if p in lowered]


def apply_followup_title_to_ebay(
    ebay_item_id: str,
    new_title: str,
    before_title: str,
    *,
    source_tab: Optional[str] = None,
    candidate_id: Optional[int] = None,
) -> dict:
    """採用後フォローアップ欄からのタイトル編集を eBay へ反映する。

    revise_item_title (既存 W31、ebay_client.py) → 成功時のみ DB
    update_ebay_listing_title → 監査ログ (listing_content_change_log、
    W314 で並行実装中のため未実装期間は ImportError を no-op fallback)。

    listing 識別は ebay_item_id (sku-rules)。

    Returns:
        {'success': bool, 'message': str}
    """
    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    from monitor.database import update_ebay_listing_title
    from monitor.ebay_client import revise_item_title

    new_t = (new_title or "").strip()
    before_t = (before_title or "").strip()

    try:
        creds = get_ebay_credentials()
    except Exception as e:  # noqa: BLE001 -- credentials 解決の多様な例外を UI に伝える
        return {"success": False, "message": f"credentials 取得エラー: {e}"}
    if not ebay_credentials_ok(creds):
        return {"success": False, "message": "eBay credentials 未設定"}

    result = revise_item_title(
        ebay_item_id, new_t,
        creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
    )
    ok = bool(result.get("success"))
    message = (result.get("message") or "").strip()

    # F11 (2026-07-03 Codex MED): eBay 反映済みで DB 更新のみ失敗した場合、
    # 例外を上に throw すると (a) UI にはエラーだけ、(b) 監査ログ未記録、(c) DB
    # 未更新、の三重不整合になり user が「eBay タイトルが変わっていない」と
    # 誤認する。eBay 実値 = 真実の source なので、DB 更新失敗は logger.error で
    # 痕跡を残しつつ、監査ログには必ず success=True + 「DB 更新失敗」注記付きで
    # 記録し、次回 ebay_sync で DB が自然回復する旨を UI 側 message に含める。
    db_sync_error: Optional[str] = None
    if ok:
        try:
            update_ebay_listing_title(ebay_item_id, new_t)
        except Exception as e:  # noqa: BLE001 -- DB 例外種別を問わず eBay 実値を優先
            db_sync_error = f"{type(e).__name__}: {e}"
            logger.error(
                "update_ebay_listing_title 失敗 (eBay 反映済み・DB 未同期) "
                "eid=%s title=%r error=%s",
                ebay_item_id, new_t, db_sync_error,
            )

    _ebay_ack = message
    if db_sync_error:
        _ebay_ack = f"{message} (DB 更新失敗: {db_sync_error})" if message else (
            f"DB 更新失敗: {db_sync_error}"
        )
    _log_title_change(
        ebay_item_id, before_t, new_t,
        source_tab=source_tab, candidate_id=candidate_id,
        success=ok, ebay_ack=_ebay_ack,
    )

    if ok and db_sync_error:
        return {
            "success": True,
            "message": (
                "eBay タイトルは更新済みですが、DB 反映に失敗しました "
                f"({db_sync_error})。次回 eBay 同期で自然回復します。"
            ),
        }
    return {
        "success": ok,
        "message": message or ("タイトルを更新しました" if ok else "タイトル更新に失敗しました"),
    }


def _log_title_change(
    ebay_item_id: str,
    before_value: str,
    after_value: str,
    *,
    source_tab: Optional[str],
    candidate_id: Optional[int],
    success: bool,
    ebay_ack: Optional[str],
) -> None:
    """listing_content_change_log への記録 (W314 監査ログ)。

    monitor/listing_content_change_log.py は別 agent が同時実装中 (設計書
    2026-07-03-finishing-panel-design.md §6 API 契約準拠)。未実装期間は
    ImportError を no-op + warning ログに落とし、タイトル反映自体は止めない
    (Q0: eBay 反映は既に成功済みなので silent skip ではなく、監査ログのみの
    機能低下として明示記録する)。
    """
    try:
        from monitor.listing_content_change_log import log_content_change
    except ImportError:
        logger.warning(
            "listing_content_change_log 未実装のため監査ログ記録スキップ "
            "(eid=%s, field=title)", ebay_item_id,
        )
        return
    try:
        log_content_change(
            ebay_item_id, "title", before_value, after_value,
            source_tab=source_tab, candidate_id=candidate_id,
            success=success, ebay_ack=ebay_ack,
        )
    except Exception:  # noqa: BLE001 -- 監査ログ失敗で eBay 反映結果を握り潰さない
        logger.exception(
            "log_content_change 呼出失敗 (eid=%s, field=title)", ebay_item_id,
        )
