#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI店長 要確認 (review 滞留 triage) タブ (W323 2026-07-05).

listing_rival_discoveries.status='new' の滞留 (task_rival_classify.py が
AI confidence 中間 / AI cap 超過 / AI 例外 で人間確認へ回した分、2026-07-05
時点 783 件) を自社商品ごとにグループ化して一覧表示し、real (監視へ) /
noise (除外) を確定する。

状態遷移は task_rival_classify.run_rival_classify の real/noise 分岐と同じ
DB 関数を再利用する (monitor.database.resolve_review_discovery が内部で
add_or_reactivate_competitor / update_rival_discovery_status を呼ぶ、
意味論を揃える・二重実装しない)。listing 識別は ebay_item_id のみ
(sku-rules.md 準拠、SKU はグループキーに使わない)。

一括操作 (グループ全体 / 同一セラー一掃) は監視対象の増減 = pricing_eligible
へ間接影響するため、2 段確認 (押す → 警告表示 → 確定ボタン) を必須にする
(単発の 1 件ずつの real/noise ボタンは即時実行、確認不要)。
"""
from __future__ import annotations

import streamlit as st

_GROUPS_PER_PAGE = 50

_DENSITY_CSS = """
<style>
.st-key-rrv_root { max-width: 1000px; }
div[class*="st-key-rrv_"] button {
  font-size:12px !important;
  padding:2px 10px !important;
  min-height:26px !important;
  line-height:22px !important;
}
div[class*="st-key-rrv_"] label {
  font-size:12px !important;
  margin-bottom:2px !important;
}
</style>
"""

_ACTION_LABEL = {"real": "監視へ追加 (real)", "noise": "除外 (noise)"}


def _fmt_price(v) -> str:
    return f"${v:.2f}" if v is not None else "—"


def _fmt_conf(v) -> str:
    return f"{v:.2f}" if v is not None else "—"


def render_rival_review_tab() -> None:
    """app.py `if _w134_sel == "AI店長 要確認":` から呼ばれるエントリポイント."""
    from monitor.database import (
        count_new_rival_discoveries,
        dismiss_discoveries_by_seller,
        get_review_discoveries_grouped,
        resolve_review_discovery,
    )

    st.markdown(_DENSITY_CSS, unsafe_allow_html=True)
    root = st.container(key="rrv_root")
    with root:
        st.subheader("🤖 AI店長 要確認")
        st.caption(
            "AI が real/noise を自動確定できず保留した rival を、自社商品ごとに確認します。"
            "Shadow 運用中 — ここでの操作は監視リスト (競合追跡) への追加/除外のみで、"
            "自動値付けへの直接反映はまだありません。"
        )

        backlog_total = count_new_rival_discoveries()
        st.markdown(f"**残件数: {backlog_total} 件 (未対応)**")

        groups = get_review_discoveries_grouped()
        if not groups:
            st.success("要確認の rival はありません。")
            return

        total_pages = max(
            1, (len(groups) + _GROUPS_PER_PAGE - 1) // _GROUPS_PER_PAGE
        )
        page = st.number_input(
            f"ページ (自社商品 {len(groups)} 件 / {_GROUPS_PER_PAGE} 件ずつ)",
            min_value=1, max_value=total_pages, value=1, step=1,
            key="rrv_page",
        )
        start = (int(page) - 1) * _GROUPS_PER_PAGE
        page_groups = groups[start:start + _GROUPS_PER_PAGE]

        for grp in page_groups:
            eid = grp["ebay_item_id"]
            n = len(grp["discoveries"])
            tail = eid[-4:] if eid else "----"
            with st.expander(
                f"{grp['our_title'][:70]} | {_fmt_price(grp['our_price'])} "
                f"(…{tail}) — {n} 件",
                expanded=False,
            ):
                _render_group_bulk_actions(
                    grp, resolve_review_discovery,
                )
                for d in grp["discoveries"]:
                    _render_discovery_row(
                        grp, d, resolve_review_discovery,
                        dismiss_discoveries_by_seller,
                    )


def _render_group_bulk_actions(grp: dict, resolve_review_discovery) -> None:
    """グループ単位の一括「監視へ」「除外」(2 段確認必須)."""
    eid = grp["ebay_item_id"]
    n = len(grp["discoveries"])
    confirm_key = f"rrv_confirm_group_{eid}"
    pending_action = st.session_state.get(confirm_key)

    if pending_action:
        st.warning(
            f"⚠️ この商品の未処理 rival {n} 件を一括"
            f"「{_ACTION_LABEL.get(pending_action, pending_action)}」します。"
            f"よろしいですか？"
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button(
                "✅ 実行する", key=f"rrv_group_yes_{eid}", type="primary",
            ):
                ok, ng = 0, 0
                for d in grp["discoveries"]:
                    r = resolve_review_discovery(
                        d["id"], pending_action,
                        our_item_id=eid, our_sku=grp["our_sku"],
                    )
                    is_ok = (
                        r == "dismissed" if pending_action == "noise"
                        else r in ("added", "reactivated")
                    )
                    ok += 1 if is_ok else 0
                    ng += 0 if is_ok else 1
                st.session_state[confirm_key] = None
                st.toast(f"完了: 成功 {ok} 件 / 対象外・失敗 {ng} 件")
                st.rerun()
        with c2:
            if st.button("キャンセル", key=f"rrv_group_no_{eid}"):
                st.session_state[confirm_key] = None
                st.rerun()
        return

    c1, c2 = st.columns(2)
    with c1:
        if st.button("▶ グループ全部を監視へ", key=f"rrv_group_real_{eid}"):
            st.session_state[confirm_key] = "real"
            st.rerun()
    with c2:
        if st.button("▶ グループ全部を除外", key=f"rrv_group_noise_{eid}"):
            st.session_state[confirm_key] = "noise"
            st.rerun()


def _render_discovery_row(
    grp: dict, d: dict, resolve_review_discovery, dismiss_discoveries_by_seller,
) -> None:
    """1 行 = 競合 1 件。real/noise は即時実行、セラー一掃のみ 2 段確認."""
    eid = grp["ebay_item_id"]
    did = d["id"]
    seller = d["competitor_seller"] or "(seller 不明)"

    reason_bits = []
    if d.get("ai_route"):
        reason_bits.append(str(d["ai_route"]))
    if d.get("ai_confidence") is not None:
        reason_bits.append(f"conf={_fmt_conf(d['ai_confidence'])}")
    if d.get("ai_reason"):
        reason_bits.append(str(d["ai_reason"])[:80])
    reason_str = " | ".join(reason_bits) if reason_bits else "（保留理由未記録）"

    seller_confirm_key = f"rrv_confirm_seller_{seller}"
    if st.session_state.get(seller_confirm_key):
        st.warning(
            f"⚠️ セラー『{seller}』の未処理 rival を全商品から一括除外します。"
            f"よろしいですか？"
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button(
                "✅ 実行する", key=f"rrv_seller_yes_{seller}_{did}",
                type="primary",
            ):
                n = dismiss_discoveries_by_seller(seller)
                st.session_state[seller_confirm_key] = False
                st.toast(f"セラー『{seller}』の {n} 件を除外しました")
                st.rerun()
        with c2:
            if st.button("キャンセル", key=f"rrv_seller_no_{seller}_{did}"):
                st.session_state[seller_confirm_key] = False
                st.rerun()
        return

    cols = st.columns([5, 1, 1, 1.3])
    with cols[0]:
        st.markdown(
            f"**{seller}** | "
            f"[{(d['competitor_title'] or '')[:60]}]"
            f"(https://www.ebay.com/itm/{d['competitor_item_id']})"
        )
        st.caption(
            f"💰 {_fmt_price(d['competitor_price_usd'])} | 🤖 {reason_str} | "
            f"{d['first_seen_at']} UTC"
        )
    with cols[1]:
        if st.button("✅ 監視へ", key=f"rrv_real_{did}"):
            r = resolve_review_discovery(
                did, "real", our_item_id=eid, our_sku=grp["our_sku"],
            )
            if r in ("added", "reactivated"):
                st.toast("監視に追加しました")
            elif r == "conflict":
                st.toast("⚠️ 他 listing で既に監視中 (conflict) — 対応不要")
            elif r == "error":
                st.error("追加に失敗しました (ログ確認要、状態は new のまま)")
            else:
                st.toast(f"既に処理済みでした ({r})")
            st.rerun()
    with cols[2]:
        if st.button("🗑️ 除外", key=f"rrv_noise_{did}"):
            r = resolve_review_discovery(did, "noise", our_item_id=eid)
            if r == "dismissed":
                st.toast("除外しました")
            else:
                st.toast(f"既に処理済みでした ({r})")
            st.rerun()
    with cols[3]:
        if st.button("🚫 セラー一掃", key=f"rrv_seller_btn_{did}"):
            st.session_state[seller_confirm_key] = True
            st.rerun()
