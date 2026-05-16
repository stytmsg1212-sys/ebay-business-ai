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


def render_purchase_confirm_tab() -> None:
    """有在庫 入荷確認 main tab."""
    from monitor.database import get_recent_emails
    from tasks.task_purchase_confirm import (
        confirm_purchase,
        suggest_listings,
        undo_purchase,
    )

    st.subheader("📥 有在庫 入荷確認")
    st.caption(
        "仕入れた商品の入荷を確認 → 対象 listing と仕入個数を選んで在庫数を加算します。"
        " eBay 出品数量も自動反映されます (自動確定はされません)。"
    )

    emails = get_recent_emails(limit=50, exclude_categories=())
    if not emails:
        st.info("メールがありません (メール取得タスク実行後に表示されます)。")
        return

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

    st.markdown("**入荷した listing を選んでください**")
    qty = st.number_input(
        "仕入個数",
        min_value=1,
        value=1,
        step=1,
        key=f"pc_qty_{gmail_id}",
    )

    for cand in candidates:
        eid = cand["ebay_item_id"]
        title = cand["title"] or "(no title)"
        inv = cand["inventory_count"]
        inv_str = "未設定" if inv is None else str(inv)
        score = cand["score"]
        label = f"{title[:55]} ({str(eid)[-4:]}) | 現在庫 {inv_str} | 類似 {score:.2f}"
        col_btn, col_warn = st.columns([3, 1])
        with col_btn:
            if st.button(
                f"この listing に確定 → {label}",
                key=f"pc_confirm_{gmail_id}_{eid}",
                use_container_width=True,
            ):
                res = confirm_purchase(gmail_id, eid, int(qty))
                # 確定結果を session_state に保持 → rerun を跨いで取消可能に
                # (Streamlit ネスト button は次 run で消えるため: MEDIUM fix).
                st.session_state["pc_last_confirm"] = {
                    "gmail_id": gmail_id,
                    "ebay_item_id": eid,
                    "message": res["message"],
                    "kind": (
                        "success"
                        if (res["success"] and res.get("sync_success"))
                        else "partial" if res["success"]
                        else "already" if res.get("already")
                        else "error"
                    ),
                }
                st.rerun()
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
