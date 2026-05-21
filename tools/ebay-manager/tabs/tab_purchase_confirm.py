#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W133 (2026-05-16): 有在庫 入荷確認タブ.

入荷メール → 対象 listing 候補 (類似度) → user が listing + 仕入個数を確定
→ inventory_count 加算 + eBay 数量反映.

判断ロジックは tasks/task_purchase_confirm.py (純ロジック) に分離。
本ファイルは Streamlit UI のみ (薄いビュー).
"""
from __future__ import annotations

import logging

import streamlit as st

logger = logging.getLogger(__name__)


def _jump_to_next_unconfirmed(current_idx: int, filtered_list: list) -> None:
    """2026-05-20 Codex UX #4: 確定成功後、次の未確認メールへ session_state を
    移す (なければそのまま)。Q0: 自動確定はしない (フォーカス移動のみ)。"""
    next_idx = None
    n = len(filtered_list)
    for j in range(current_idx + 1, n):
        if not filtered_list[j].get('confirmed'):
            next_idx = j
            break
    if next_idx is None:
        for j in range(0, current_idx):
            if not filtered_list[j].get('confirmed'):
                next_idx = j
                break
    if next_idx is not None and next_idx != current_idx:
        st.session_state["pc_email_select"] = next_idx


def render_purchase_confirm_tab() -> None:
    """有在庫 入荷確認 main tab."""
    from monitor.database import get_recent_emails, set_email_confirmed
    from tasks.task_purchase_confirm import (
        TOP1_AUTO_RECOMMEND_THRESHOLD,
        confirm_purchase,
        extract_purchase_qty,
        suggest_listings,
        undo_purchase,
    )
    from ui_cache import bump_db_version  # W134 Step2: 書込後 read-cache 無効化

    st.subheader("📥 有在庫 入荷確認")
    st.caption(
        "仕入れた商品の入荷を確認 → 対象 listing と仕入個数を選んで在庫数を加算します。"
        " eBay 出品数量も自動反映されます (自動確定はされません)。"
    )

    # 2026-05-20 user 緊急要望: 全メール (eBay 系含む) を表示していた旧仕様を
    # 修正。category='supplier_purchase' (メルカリ/ヤフオク/楽天/Amazon 等の
    # 購入確認メール、task_email_pickup._categorize_email で分類) のみに絞る。
    # 旧 Gmail query は eBay 系のみ取込だったため supplier_purchase メールが
    # 0 件の可能性高い。query 拡張は同 commit 内、cron 走行後反映。
    emails = get_recent_emails(
        limit=100, include_categories=('supplier_purchase',),
    )
    if not emails:
        st.warning(
            "📭 仕入購入メール (category='supplier_purchase') がまだありません。\n\n"
            "原因の可能性:\n"
            "1. **Gmail 取込タスクが未実行**: 直近 14 日の仕入先メール (メルカリ/"
            "ヤフオク/楽天/Amazon/PayPay/駿河屋/まんだらけ) を取り込む `task_email_pickup` を"
            "手動実行してください (定時実行タブから or scheduler 次回発火を待つ)。\n"
            "2. **直近 14 日に仕入購入なし**: その期間に仕入購入確認メールが届いていない場合は正常。\n"
            "3. **Gmail 側で別 sender**: 上記以外の仕入先を使っている場合は task_email_"
            "pickup._SUPPLIER_SENDER_HINTS への追加が必要 (実装更新で対応)。"
        )
        return

    # 2026-05-20 Codex LOW 対応: 旧 `.__hash__()` は process-randomized で日付順
    # にならない。Python の stable sort を 2 段適用 (date 降順 → confirmed 昇順)
    # で「未確認 priority + 各グループ内で新しい順」を実現。
    emails.sort(
        key=lambda em: em.get('date') or em.get('fetched_at') or '',
        reverse=True,
    )
    emails.sort(key=lambda em: int(em.get('confirmed', 0)))

    # サマリ件数
    n_unconfirmed = sum(1 for em in emails if not em.get('confirmed'))
    st.caption(
        f"📬 仕入購入メール: 全 {len(emails)} 件 "
        f"(未確認 **{n_unconfirmed}** 件、確認済 {len(emails) - n_unconfirmed} 件)"
    )

    flt = st.text_input(
        "メール絞り込み (件名 / 差出人 / 本文の部分一致)",
        value="",
        key="pc_email_filter",
    ).strip().lower()

    def _match(em: dict) -> bool:
        if not flt:
            return True
        hay = " ".join(
            str(em.get(k) or "")
            for k in ("subject", "sender", "body_text", "body_ja")
        ).lower()
        return flt in hay

    filtered = [em for em in emails if _match(em)]
    if not filtered:
        st.warning("絞り込み条件に一致するメールがありません。")
        return

    def _email_label(em: dict) -> str:
        subj = (em.get("subject") or "(no subject)")[:60]
        snd = (em.get("sender") or "").split("<")[0].strip()[:30]
        when = em.get("fetched_at") or ""
        return f"{subj} | {snd} | {when}"

    idx = st.selectbox(
        "対象メールを選択",
        options=list(range(len(filtered))),
        format_func=lambda i: _email_label(filtered[i]),
        key="pc_email_select",
    )
    email = filtered[idx]
    gmail_id = email.get("gmail_id") or ""

    # 規約: Streamlit 1.56 で expander 禁止 → checkbox + 条件表示で代替
    # (project_ebay_manager.md / Material Icon arrow_right 重なりバグ回避).
    if st.checkbox(
        "メール本文を表示", value=False, key=f"pc_show_body_{gmail_id}"
    ):
        st.text(
            (email.get("body_ja") or email.get("body_text") or "(本文なし)")[:3000]
        )

    email_text = " ".join(
        str(email.get(k) or "") for k in ("subject", "body_ja", "body_text")
    )
    candidates = suggest_listings(email_text, top=5)

    if not candidates:
        st.warning(
            "有在庫 listing 候補が見つかりません"
            " (有在庫 listing が未登録、またはメール本文が空の可能性)。"
        )
        return

    # 2026-05-20 Codex UX #1: メール本文から数量を自動抽出 → number_input
    # の default に。誤抽出は user が上書き可能 (Q0 保持)。
    extracted_qty = extract_purchase_qty(email_text)

    st.markdown("**入荷した listing を選んでください**")
    qty = st.number_input(
        "仕入個数",
        min_value=1,
        value=int(extracted_qty) if extracted_qty else 1,
        step=1,
        key=f"pc_qty_{gmail_id}",
    )
    if extracted_qty:
        st.caption(
            f"📝 メール本文から数量 **{extracted_qty}** を自動抽出済 "
            f"(誤っていれば上で調整)"
        )

    def _do_confirm(
        target_eid: str, source_label: str, kind: str = 'restock',
    ) -> None:
        """共通: confirm_purchase 実行 + session_state 更新 + next email focus.

        2026-05-21 W133-FU: kind 引数追加 ('restock' = 有在庫補充 / 'fulfillment'
        = 無在庫が売れた仕入)。kind は候補の SKU prefix から自動判定済。"""
        res = confirm_purchase(gmail_id, target_eid, int(qty), kind=kind)
        bump_db_version()
        st.session_state["pc_last_confirm"] = {
            "gmail_id": gmail_id,
            "ebay_item_id": target_eid,
            "message": res["message"],
            "source": source_label,
            "kind": (
                "success" if (res["success"] and res.get("sync_success"))
                else "partial" if res["success"]
                else "already" if res.get("already")
                else "error"
            ),
        }
        # 2026-05-20 Codex UX #4: emails.confirmed=1 にして次の未確認へ
        # フォーカスを移す (user 労力削減、自動確定はしない = Q0 保持)。
        if res.get("success") or res.get("already"):
            try:
                set_email_confirmed([gmail_id], confirmed=1)
            except Exception as _e:
                logger.warning(f"set_email_confirmed failed: {_e}")
            _jump_to_next_unconfirmed(idx, filtered)
        st.rerun()

    # 2026-05-20 Codex UX #2: Top-1 が高信頼 + 次点との差が明確なら
    # 「✅ 推奨候補で確定」を primary ボタンとして上部に強調表示
    # (1 クリック + 候補スキャン不要)。下の通常リストはそのまま残し
    # 手動 override 可能 (Q0)。
    top1 = candidates[0]
    top2 = candidates[1] if len(candidates) > 1 else None
    score_gap = top1["score"] - (top2["score"] if top2 else 0)
    auto_recommended = (
        top1["score"] >= TOP1_AUTO_RECOMMEND_THRESHOLD
        and score_gap >= 0.10
        and not top1["low_confidence"]
    )
    if auto_recommended:
        _ttl = top1["title"] or "(no title)"
        _inv = top1["inventory_count"]
        _inv_str = "未設定" if _inv is None else str(_inv)
        _kind = top1.get("kind") or "restock"
        # 2026-05-21 W133-FU: kind 区別表示
        _kind_badge = "🏪 有在庫補充" if _kind == "restock" else "📤 無在庫 fulfillment (在庫加算なし)"
        _btn_label = (
            f"✅ 推奨候補で確定 (qty {int(qty)})"
            if _kind == "restock"
            else f"📤 推奨候補で fulfillment 記録 (在庫加算なし)"
        )
        st.success(
            f"🎯 **推奨候補** [{_kind_badge}] (類似 {top1['score']:.2f}、"
            f"次点との差 {score_gap:+.2f}): "
            f"**{_ttl[:60]}** ({str(top1['ebay_item_id'])[-4:]}) | "
            f"現在庫 {_inv_str}"
        )
        if st.button(
            _btn_label,
            key=f"pc_confirm_top1_{gmail_id}",
            type="primary",
            use_container_width=True,
        ):
            _do_confirm(top1["ebay_item_id"], "top1_auto", kind=_kind)
        st.markdown("---")
        st.caption("または下のリストから手動選択:")

    for cand in candidates:
        eid = cand["ebay_item_id"]
        title = cand["title"] or "(no title)"
        inv = cand["inventory_count"]
        inv_str = "未設定" if inv is None else str(inv)
        score = cand["score"]
        kind = cand.get("kind") or "restock"
        # 2026-05-21 W133-FU: kind badge + ボタン text 区別
        kind_badge = "🏪 補充" if kind == "restock" else "📤 fulfillment"
        label = (
            f"[{kind_badge}] {title[:50]} ({str(eid)[-4:]}) | "
            f"現在庫 {inv_str} | 類似 {score:.2f}"
        )
        btn_text = (
            f"この listing に確定 → {label}"
            if kind == "restock"
            else f"この listing に fulfillment 記録 → {label}"
        )
        col_btn, col_warn = st.columns([3, 1])
        with col_btn:
            if st.button(
                btn_text,
                key=f"pc_confirm_{gmail_id}_{eid}",
                use_container_width=True,
            ):
                _do_confirm(eid, "manual_pick", kind=kind)
        with col_warn:
            if cand["low_confidence"]:
                st.caption("⚠️ 類似度低 (要確認)")

    # 直近の確定結果 + 取消 (per-candidate button の外 = rerun を跨いで到達可能).
    last = st.session_state.get("pc_last_confirm")
    if last:
        kind = last.get("kind")
        if kind == "success":
            st.success(last["message"])
        elif kind in ("partial", "already"):
            # partial = ローカル在庫加算済だが eBay 反映失敗 (緑にしない=偽装防止)
            st.warning(last["message"])
        else:
            st.error(last["message"])
        if kind in ("success", "partial", "already"):
            if st.button("↩ 直前の確定を取消", key="pc_undo_last"):
                u = undo_purchase(last["gmail_id"], last["ebay_item_id"])
                bump_db_version()  # W134 Step2: 在庫戻し後 read-cache 無効化
                # 2026-05-20 Codex HIGH 対応: undo 成功時に emails.confirmed=0 へ
                # 戻す。これがないと「確定→undo」後もメールは確認済 bucket に
                # 残り、未確認ワークフローから silent skip される (Q0 違反)。
                if u.get("success") or u.get("already"):
                    try:
                        set_email_confirmed(
                            [last["gmail_id"]], confirmed=0,
                        )
                    except Exception as _e:
                        logger.warning(f"set_email_confirmed (undo) failed: {_e}")
                if u.get("success") and u.get("sync_success"):
                    st.info(u["message"])
                elif u.get("success") or u.get("already"):
                    st.warning(u["message"])
                else:
                    st.error(u["message"])
                st.session_state.pop("pc_last_confirm", None)
                st.rerun()
        if st.button("結果をクリア", key="pc_clear_last"):
            st.session_state.pop("pc_last_confirm", None)
            st.rerun()
