"""eBaymag 国別出品 区分選択 共通コンポーネント (W284, 2026-06-20).

商品管理タブと個別出品タブで再利用する「区分選択 (4択)」UI。
M2 (code-architect) に従い、**ebay_item_id 不要** の区分選択部分のみを共通化する
(個別出品は出品前 = item_id 未確定でも区分を選べるようにするため)。
driver 反映 (item_id 必須) は各タブ側の既存ロジックに委ねる。

4択: 全国 / 優先国 / カスタム / 出さない (設計書 §0)。
出品国の解決は monitor.ebaymag_segment.resolve_* (single source = market_analysis)。
"""
from __future__ import annotations

import streamlit as st

from monitor.ebaymag_driver import SITE_MAP
from monitor.ebaymag_segment import resolve_priority_sites

SEGMENTS = ["全国", "優先国", "カスタム", "出さない"]
SITE_CODES = list(SITE_MAP)  # ["UK","DE","FR","IT","ES","CA","AU"]


def infer_segment_from_sites(on_sites: list[str]) -> dict:
    """現在 ON の国セットから区分を推定する (希望未保存の既設定商品のデフォルト用)。

    user 要望 (2026-06-20): 既に eBaymag に設定済みの商品は、4択/チェックを現在の
    eBaymag 実態に合わせて初期表示する (誤って『出さない』で取り下げる事故を防ぐ)。

    Returns: {"segment": str, "desired_sites": list[str]}
    """
    on = sorted(s for s in (on_sites or []) if s in SITE_CODES)
    if not on:
        return {"segment": "出さない", "desired_sites": []}
    if set(on) == set(SITE_CODES):
        return {"segment": "全国", "desired_sites": list(SITE_CODES)}
    return {"segment": "カスタム", "desired_sites": on}


def render_segment_selector(
    key_prefix: str,
    *,
    ebay_item_id: str | None = None,
    current_segment: str | None = None,
    current_desired: list[str] | None = None,
) -> dict:
    """区分4択 + 出品国の表示/編集を描画し、選択結果を返す。

    ebay_item_id を渡さなくても使える (個別出品の出品前)。優先国の実績解決は
    ebay_item_id がある時のみ DB から計算、無い時は current_desired を初期値にする。

    Returns: {"segment": str, "desired_sites": list[str]}
    """
    seg_default = current_segment if current_segment in SEGMENTS else "出さない"
    segment = st.radio(
        "eBaymag 出品国",
        SEGMENTS,
        index=SEGMENTS.index(seg_default),
        key=f"{key_prefix}_seg",
        horizontal=True,
        help="全国=7カ国 / 優先国=販売実績のある国 / カスタム=手動選択 / 出さない=国際公開なし",
    )

    if segment == "全国":
        desired = list(SITE_CODES)
        st.caption(f"出品国: {', '.join(SITE_CODES)} (全 7 カ国)")
    elif segment == "優先国":
        if ebay_item_id:
            desired = resolve_priority_sites(ebay_item_id)
        else:
            desired = list(current_desired or [])
        if desired:
            st.caption(f"優先国 (販売実績): {', '.join(desired)}")
        else:
            st.warning(
                "この商品はまだ販売実績が無いため優先国が空です。"
                "「全国」または「カスタム」を選んでください。"
            )
    elif segment == "カスタム":
        st.caption("出品する国を選択:")
        cols = st.columns(len(SITE_CODES))
        pre = set(current_desired or [])
        desired = []
        for col, code in zip(cols, SITE_CODES):
            with col:
                if st.checkbox(code, value=(code in pre), key=f"{key_prefix}_cs_{code}"):
                    desired.append(code)
    else:  # 出さない
        desired = []
        st.caption("国際公開しません (US 本体のみ)")

    return {"segment": segment, "desired_sites": desired}
