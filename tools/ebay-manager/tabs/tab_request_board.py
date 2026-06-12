"""W266 — 依頼ボード (問題記述＆実装依頼表)。2026-06-12 user 依頼で新設。

user 依頼単位のタスク管理ボード:
  1. user が不具合 / 実装依頼をフォームで追記
  2. assistant がセッション開始時に吸い上げて対応 (SessionStart hook で未完了件数を注入)
  3. assistant が status / 進捗ログ / 確認手順を随時更新
  4. 対応完了 = 「確認待ち」+ user 向け確認手順を必ず記載 (DB 層で強制)
  5. user が確認手順どおり確認 → 「確認完了」ボタンで done / 問題あれば差し戻し

ROADMAP (system_improvements / W 番号) = assistant 発の実装管理とは別軸。
related_w 列で紐付けのみ行う。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import streamlit as st

from monitor.database import (
    add_user_request,
    answer_user_request,
    append_user_request_log,
    get_user_requests,
    set_user_request_status,
)

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))

_KINDS = ["不具合", "実装依頼", "質問・相談"]
_PRIORITIES = ["高", "通常", "低"]

# status → カードの badge 色 (Streamlit markdown badge 構文)
_STATUS_BADGE = {
    "open": ":gray-badge[受付]",
    "in_progress": ":blue-badge[対応中]",
    "waiting_user": ":orange-badge[回答待ち]",
    "awaiting_check": ":violet-badge[確認待ち]",
    "done": ":green-badge[完了]",
    "on_hold": ":gray-badge[保留]",
}


def _fmt_jst(utc_str: str | None) -> str:
    """DB の UTC TIMESTAMP 文字列 → 'M/D HH:MM' (JST) 表示。"""
    if not utc_str:
        return "-"
    try:
        dt = datetime.strptime(utc_str[:19], "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc).astimezone(_JST)
        # %-m / %#m は OS 依存のため手組み (Windows 安全)
        return f"{dt.month}/{dt.day} {dt:%H:%M}"
    except ValueError:
        return utc_str


def _card_header(req: dict) -> str:
    badge = _STATUS_BADGE.get(req["status"], req["status"])
    prio = f" :red-badge[優先度 {req['priority']}]" if req["priority"] == "高" else ""
    return f"#{req['id']} {badge}{prio} **[{req['kind']}]** {req['title']}"


def _render_progress_log(req: dict) -> None:
    log = (req.get("progress_log") or "").strip()
    if log:
        st.markdown("**経過ログ**")
        st.code(log, language=None)


def _render_meta(req: dict) -> None:
    meta = (
        f"登録 {_fmt_jst(req['created_at'])} / 最終更新 {_fmt_jst(req['updated_at'])}"
    )
    if req.get("related_w"):
        meta += f" / 関連: {req['related_w']}"
    st.caption(meta)


def _render_user_note_form(req: dict) -> None:
    """user 追記欄 (どの依頼にも気付き・補足をどんどん追記できる)。"""
    with st.form(key=f"urq_note_form_{req['id']}", clear_on_submit=True):
        note = st.text_area(
            "追記 (補足・気付き・再現条件など)",
            key=f"urq_note_{req['id']}",
            height=68,
        )
        if st.form_submit_button("追記する"):
            if note.strip():
                append_user_request_log(req["id"], note, author="user")
                st.rerun()
            else:
                st.warning("追記内容が空です")


def _render_awaiting_check_card(req: dict) -> None:
    """確認待ちカード: 確認手順 + 確認完了 / 差し戻し。

    2026-06-12 user 要望: 開いた時は全カード閉じた状態 (expanded=False)。
    """
    with st.expander(_card_header(req), expanded=False):
        if req.get("description"):
            st.write(req["description"])
        _render_meta(req)

        st.info(
            "**あなたの確認手順** (問題なければ下の「確認完了」を押してください)\n\n"
            + (req.get("verify_steps") or "(確認手順未記載 — assistant に確認してください)")
        )
        _render_progress_log(req)

        col_ok, col_back = st.columns([1, 2])
        with col_ok:
            if st.button(
                "✅ 確認完了 (タスク終了)",
                key=f"urq_confirm_{req['id']}",
                type="primary",
            ):
                set_user_request_status(req["id"], "done", author="user")
                st.rerun()
        with col_back:
            with st.form(key=f"urq_back_form_{req['id']}", clear_on_submit=True):
                reason = st.text_input(
                    "問題があれば内容を書いて差し戻し", key=f"urq_back_{req['id']}"
                )
                if st.form_submit_button("↩ 差し戻し (再対応依頼)"):
                    if reason.strip():
                        set_user_request_status(
                            req["id"],
                            "in_progress",
                            note=f"差し戻し: {reason}",
                            author="user",
                        )
                        st.rerun()
                    else:
                        st.warning("差し戻し理由を書いてください")


def _render_answer_form(req: dict) -> None:
    """W267 (依頼ボード#13): 回答待ちカードに質問本文 + 専用回答欄を表示。

    回答送信 → answer_user_request() が in_progress 復帰 + 検知イベント発行
    (assistant が Monitor で即検知して作業再開)。
    """
    question = (req.get("pending_question") or "").strip()
    st.warning(
        "**assistant からの質問**\n\n"
        + (question or "(質問本文未記録 — 経過ログ末尾をご確認ください)")
    )
    with st.form(key=f"urq_answer_form_{req['id']}", clear_on_submit=True):
        ans = st.text_area("回答", key=f"urq_answer_{req['id']}", height=90)
        if st.form_submit_button(
            "回答を送信 (assistant が検知して作業再開)", type="primary"
        ):
            if ans.strip():
                try:
                    # H1 (code-reviewer 2026-06-12): False (行不在等) を成功表示しない
                    if answer_user_request(req["id"], ans):
                        st.session_state["urq_answer_success"] = req["id"]
                        st.rerun()
                    else:
                        st.error(
                            "回答の保存に失敗しました (依頼が見つかりません)。"
                            "ページを再読込してください。"
                        )
                except ValueError as ve:
                    st.warning(str(ve))
            else:
                st.warning("回答が空です")


def _render_active_card(req: dict) -> None:
    """受付/対応中/回答待ち/保留 カード: 閲覧 + user 追記。常に閉じた状態で表示。"""
    with st.expander(_card_header(req), expanded=False):
        if req.get("description"):
            st.write(req["description"])
        _render_meta(req)
        if req["status"] == "waiting_user":
            _render_answer_form(req)
        _render_progress_log(req)
        _render_user_note_form(req)


def _render_done_card(req: dict) -> None:
    with st.expander(_card_header(req), expanded=False):
        if req.get("description"):
            st.write(req["description"])
        st.caption(
            f"登録 {_fmt_jst(req['created_at'])} / 確認完了 {_fmt_jst(req['confirmed_at'])}"
            + (f" / 関連: {req['related_w']}" if req.get("related_w") else "")
        )
        _render_progress_log(req)


def render_request_board_tab() -> None:
    st.subheader("📋 依頼ボード")
    st.caption(
        "あなた→assistant の依頼 (不具合報告・実装依頼) をタスク単位で管理します。"
        "フロー: 受付 → 対応中 → **確認待ち** (確認手順つき) → あなたの「確認完了」で終了。"
        "ROADMAP (W 番号) は assistant 側の実装管理で、ここは依頼単位の進捗表です。"
    )

    # M2: 直前 rerun で登録した依頼の成功メッセージ (rerun 跨ぎで表示)
    _added = st.session_state.pop("urq_add_success", None)
    if _added is not None:
        st.success(f"依頼 #{_added} を登録しました。assistant が次回吸い上げます。")

    # W267: 回答送信の成功メッセージ (rerun 跨ぎで表示)
    _answered = st.session_state.pop("urq_answer_success", None)
    if _answered is not None:
        st.success(
            f"#{_answered} への回答を送信しました。assistant が検知して作業を再開します。"
        )

    try:
        requests = get_user_requests()
    except Exception as e:
        # 旧 DB (migration v72 未適用) 等。silent 化せず明示 (Q0)
        st.error(f"依頼ボード読込失敗: {e}")
        return

    # ── 進捗サマリ ──
    counts: dict[str, int] = {}
    for r in requests:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("受付 (未着手)", counts.get("open", 0) + counts.get("on_hold", 0))
    m2.metric("対応中", counts.get("in_progress", 0))
    m3.metric("回答待ち", counts.get("waiting_user", 0))
    m4.metric("あなたの確認待ち", counts.get("awaiting_check", 0))
    m5.metric("完了 (累計)", counts.get("done", 0))

    # ── 新規依頼フォーム ──
    # 2026-06-12 user 要望: 開いた時は全 expander 閉 (新規フォーム含む)
    with st.expander("➕ 新しい依頼を追加", expanded=False):
        with st.form(key="urq_add_form", clear_on_submit=True):
            title = st.text_input("タイトル (短く)")
            description = st.text_area(
                "内容 (不具合の再現条件 / 実装してほしいことの詳細)", height=110
            )
            c1, c2 = st.columns(2)
            kind = c1.selectbox("種別", _KINDS)
            priority = c2.selectbox("優先度", _PRIORITIES, index=1)
            if st.form_submit_button("依頼を登録", type="primary"):
                try:
                    rid = add_user_request(
                        title, description, kind=kind, priority=priority
                    )
                    # M2: st.rerun() で消えないよう session_state 経由で表示
                    st.session_state["urq_add_success"] = rid
                    st.rerun()
                except ValueError as ve:
                    st.warning(str(ve))

    # ── 確認待ち (user アクション必要 = 最上段) ──
    awaiting = [r for r in requests if r["status"] == "awaiting_check"]
    if awaiting:
        st.markdown("### 🟣 あなたの確認待ち")
        for r in awaiting:
            _render_awaiting_check_card(r)

    # ── 対応中 / 回答待ち ──
    active = [r for r in requests if r["status"] in ("in_progress", "waiting_user")]
    if active:
        st.markdown("### 🔵 対応中・回答待ち")
        for r in active:
            _render_active_card(r)

    # ── 受付 (未着手) / 保留 ──
    backlog = [r for r in requests if r["status"] in ("open", "on_hold")]
    if backlog:
        st.markdown("### ⚪ 受付 (未着手)・保留")
        for r in backlog:
            _render_active_card(r)

    # ── 完了 ──
    done = [r for r in requests if r["status"] == "done"]
    if done:
        st.markdown(f"### ✅ 完了 ({len(done)} 件)")
        for r in done[:20]:
            _render_done_card(r)
        if len(done) > 20:
            st.caption(f"ほか {len(done) - 20} 件 (直近 20 件のみ表示)")

    if not requests:
        st.info("まだ依頼がありません。上のフォームから最初の依頼を追加してください。")
