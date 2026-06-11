# -*- coding: utf-8 -*-
"""
description テンプレート設定 サブタブ UI (W9 Phase 5)

監視・出品対象の description テンプレートを CRUD する。個別出品タブの
サブタブとして呼ばれる想定だが、単独でも使える。

設計方針:
  - expander 禁止 (feedback_ui_design.md) のため、編集フォームは
    selectbox + st.container(border=True) で常時表示する。
  - JARVIS スタイル: 絵文字禁止、日本語ラベル、st.dataframe ではなく
    各行を container で描画してアクション列を確保。
  - 削除確認は「削除チェック → 削除実行ボタン」の2段階で誤操作防止。
  - デフォルトは 1件のみ保証。DB 層 (save_description_template) が
    is_default=True 指定時に他行の is_default を 0 にする。
"""
from __future__ import annotations

import html
import logging

import streamlit as st

from monitor.database import (
    delete_description_template,
    get_description_templates,
    save_description_template,
)

logger = logging.getLogger(__name__)

# session_state キープレフィクス (UI 間衝突回避)
_SS = "dt_"

# placeholder 一覧 (v4 テンプレと一致)。UI のヒント表示用。
_PLACEHOLDERS: tuple[str, ...] = (
    "product_name", "product_sub",
    "rank", "rank_label", "rank_jp", "quick_notes",
    "includes_rows", "specs_rows", "spec_strip_rows",
    "shipping_origin", "shipping_carrier", "shipping_handling",
    "shipping_delivery_us", "shipping_packaging", "shipping_notes",
    "mode_class",
)


# =========================================================================
# helpers
# =========================================================================

def _init_session_state() -> None:
    """本タブ用 session_state キーの初期化。"""
    defaults = {
        f"{_SS}mode": "list",                    # 'list' | 'edit' | 'create'
        f"{_SS}editing_id": None,                # 編集対象の template_id
        f"{_SS}form_name": "",
        f"{_SS}form_body": "",
        f"{_SS}form_is_default": False,
        f"{_SS}confirm_delete_id": None,         # 削除確認対象 id
        f"{_SS}last_saved_id": None,
        f"{_SS}last_error": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _enter_create_mode() -> None:
    st.session_state[f"{_SS}mode"] = "create"
    st.session_state[f"{_SS}editing_id"] = None
    st.session_state[f"{_SS}form_name"] = ""
    st.session_state[f"{_SS}form_body"] = ""
    st.session_state[f"{_SS}form_is_default"] = False
    st.session_state[f"{_SS}last_error"] = None


def _enter_edit_mode(template: dict) -> None:
    st.session_state[f"{_SS}mode"] = "edit"
    st.session_state[f"{_SS}editing_id"] = template.get("id")
    st.session_state[f"{_SS}form_name"] = template.get("name") or ""
    st.session_state[f"{_SS}form_body"] = template.get("body") or ""
    st.session_state[f"{_SS}form_is_default"] = bool(template.get("is_default"))
    st.session_state[f"{_SS}last_error"] = None


def _exit_to_list() -> None:
    st.session_state[f"{_SS}mode"] = "list"
    st.session_state[f"{_SS}editing_id"] = None
    st.session_state[f"{_SS}last_error"] = None


# =========================================================================
# render parts
# =========================================================================

def _render_placeholder_hint() -> None:
    """利用可能な placeholder 一覧をインラインで表示 (expander 不使用)。"""
    chips_html = "".join(
        f'<span style="display:inline-block;margin:2px 4px;padding:3px 8px;'
        f'background:rgba(14,79,75,0.08);border:1px solid rgba(14,79,75,0.25);'
        f'border-radius:3px;font-family:monospace;font-size:11px;'
        f'color:#2a2e2a;">{{{{{html.escape(p)}}}}}</span>'
        for p in _PLACEHOLDERS
    )
    st.markdown(
        f'<div style="margin-bottom:8px;">'
        f'<div style="font-size:11px;color:#8d927f;margin-bottom:4px;">'
        f'利用可能なプレースホルダ（本文中に <code>{{{{name}}}}</code> 形式で記載）</div>'
        f'<div>{chips_html}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_list(templates: list[dict]) -> None:
    """テンプレ一覧と新規追加ボタンを描画。"""
    st.markdown(
        '<div style="font-size:13px;color:#5f6557;margin-bottom:8px;">'
        f'登録済みテンプレート: {len(templates)} 件 '
        '（個別出品タブでデフォルトが自動選択される）</div>',
        unsafe_allow_html=True,
    )

    _c1, _c2 = st.columns([1, 6])
    with _c1:
        if st.button("新規追加", key=f"{_SS}btn_create"):
            _enter_create_mode()
            st.rerun()
    with _c2:
        if st.session_state.get(f"{_SS}last_saved_id") is not None:
            st.success("テンプレートを保存しました。", icon=None)
            st.session_state[f"{_SS}last_saved_id"] = None  # 1回だけ表示

    if not templates:
        st.info(
            "テンプレートが1件も登録されていません。起動時の自動投入に失敗した可能性があります。"
            "「新規追加」から手動で作成してください。"
        )
        return

    for t in templates:
        tid = t.get("id")
        name = t.get("name") or "(名前なし)"
        is_default = bool(t.get("is_default"))
        body_len = len(t.get("body") or "")

        with st.container(border=True):
            _h1, _h2, _h3, _h4, _h5 = st.columns([4.0, 1.2, 1.0, 1.2, 1.2])
            with _h1:
                badge = (
                    '<span style="display:inline-block;margin-left:8px;padding:2px 8px;'
                    'background:rgba(184,134,11,0.12);border:1px solid rgba(184,134,11,0.40);'
                    'border-radius:3px;font-size:10px;color:#b8860b;'
                    'letter-spacing:1px;">DEFAULT</span>'
                ) if is_default else ""
                st.markdown(
                    f'<div style="padding-top:6px;font-size:14px;color:#2a2e2a;">'
                    f'{html.escape(name)}{badge}</div>',
                    unsafe_allow_html=True,
                )
            with _h2:
                st.markdown(
                    f'<div style="padding-top:6px;font-size:11px;color:#8d927f;">'
                    f'{body_len} 文字</div>',
                    unsafe_allow_html=True,
                )
            with _h3:
                if st.button("編集", key=f"{_SS}btn_edit_{tid}"):
                    _enter_edit_mode(t)
                    st.rerun()
            with _h4:
                # 削除要求 (確認ステップに遷移)
                if st.button("削除", key=f"{_SS}btn_delete_{tid}"):
                    st.session_state[f"{_SS}confirm_delete_id"] = tid
                    st.rerun()
            with _h5:
                if not is_default and st.button("デフォルト化", key=f"{_SS}btn_default_{tid}"):
                    try:
                        save_description_template(
                            name=name,
                            body=t.get("body") or "",
                            is_default=True,
                            template_id=tid,
                        )
                        st.session_state[f"{_SS}last_saved_id"] = tid
                    except Exception as e:  # noqa: BLE001
                        logger.exception("default toggle failed")
                        st.session_state[f"{_SS}last_error"] = str(e)
                    st.rerun()

            # 削除確認 2段階目
            if st.session_state.get(f"{_SS}confirm_delete_id") == tid:
                st.warning(
                    f"『{name}』を削除します。この操作は取り消せません。"
                    "関連する listing_drafts の template_id は NULL にはなりませんが、"
                    "UI 側から template_id を解決できなくなります。"
                )
                _cc1, _cc2, _cc3 = st.columns([1.2, 1.2, 4])
                with _cc1:
                    if st.button("削除実行", key=f"{_SS}btn_confirm_del_{tid}", type="primary"):
                        try:
                            delete_description_template(tid)
                            st.session_state[f"{_SS}confirm_delete_id"] = None
                            st.success("削除しました。")
                        except Exception as e:  # noqa: BLE001
                            logger.exception("delete failed")
                            st.session_state[f"{_SS}last_error"] = str(e)
                        st.rerun()
                with _cc2:
                    if st.button("キャンセル", key=f"{_SS}btn_cancel_del_{tid}"):
                        st.session_state[f"{_SS}confirm_delete_id"] = None
                        st.rerun()


def _render_form() -> None:
    """編集/新規追加フォーム。"""
    mode = st.session_state[f"{_SS}mode"]
    editing_id = st.session_state[f"{_SS}editing_id"]

    title = "テンプレート編集" if mode == "edit" else "テンプレート新規作成"
    st.markdown(
        f'<div style="font-size:14px;color:#2a2e2a;margin:12px 0 8px;">'
        f'{title}</div>',
        unsafe_allow_html=True,
    )

    _render_placeholder_hint()

    with st.form(key=f"{_SS}form", clear_on_submit=False):
        name = st.text_input(
            "名前 (一意、例: MonoHonpo v4 (default))",
            value=st.session_state[f"{_SS}form_name"],
            max_chars=100,
        )
        body = st.text_area(
            "本文 HTML",
            value=st.session_state[f"{_SS}form_body"],
            height=300,
            help="v4 テンプレは CSS を <style>...</style> ブロックで含む。"
                 "{{placeholder}} は listing_generator が置換する。",
        )
        is_default = st.checkbox(
            "デフォルトテンプレートに設定",
            value=st.session_state[f"{_SS}form_is_default"],
            help="チェックすると他のテンプレの default フラグは自動的に外れる。",
        )

        _c1, _c2, _c3 = st.columns([1, 1, 6])
        with _c1:
            submit = st.form_submit_button("保存", type="primary")
        with _c2:
            cancel = st.form_submit_button("キャンセル")

    if cancel:
        _exit_to_list()
        st.rerun()

    if submit:
        # バリデーション
        name_stripped = (name or "").strip()
        body_stripped = (body or "").strip()
        if not name_stripped:
            st.error("名前は必須です。")
            return
        if not body_stripped:
            st.error("本文は必須です。")
            return
        if len(body_stripped) < 20:
            st.error("本文が短すぎます（20文字以上必要）。")
            return

        try:
            new_id = save_description_template(
                name=name_stripped,
                body=body_stripped,
                is_default=bool(is_default),
                template_id=editing_id,
            )
            st.session_state[f"{_SS}last_saved_id"] = new_id
            _exit_to_list()
            st.rerun()
        except Exception as e:  # noqa: BLE001
            # UNIQUE 制約違反などを含む
            logger.exception("save_description_template failed")
            msg = str(e)
            if "UNIQUE constraint" in msg or "UNIQUE" in msg.upper():
                st.error(f"同名のテンプレートが既に存在します: {name_stripped!r}")
            else:
                st.error(f"保存に失敗しました: {msg}")


# =========================================================================
# 公開 API
# =========================================================================

def render_tab(settings: dict | None = None) -> None:
    """description テンプレート設定タブを描画する。

    Args:
        settings: 互換のため受け取るが本タブでは未使用。
    """
    _init_session_state()

    st.markdown(
        '<div style="font-size:12px;color:#8d927f;letter-spacing:2px;'
        'margin-bottom:6px;">D E S C R I P T I O N &nbsp; T E M P L A T E S</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div style="font-size:13px;color:#2a2e2a;margin-bottom:12px;">'
        'eBay 出品の description HTML テンプレートを管理する。'
        '個別出品タブで selectbox から選択される。</div>',
        unsafe_allow_html=True,
    )

    # エラー表示 (モード遷移で消える)
    err = st.session_state.get(f"{_SS}last_error")
    if err:
        st.error(f"直前の操作でエラーが発生しました: {err}")
        st.session_state[f"{_SS}last_error"] = None

    try:
        templates = get_description_templates()
    except Exception as e:  # noqa: BLE001
        logger.exception("get_description_templates failed")
        st.error(f"テンプレート一覧の取得に失敗しました: {e}")
        templates = []

    mode = st.session_state[f"{_SS}mode"]
    if mode in ("create", "edit"):
        _render_form()
        st.markdown("---")
        st.caption("一覧に戻るには上のキャンセルボタンをクリックしてください。")
    else:
        _render_list(templates)
