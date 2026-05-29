# -*- coding: utf-8 -*-
"""W24: MonoDeck リサーチ脳タブ.

Streamlit チャット UI で Research 脳 (Opus 4.8) に質問できる.
過去 Q&A 履歴を sidebar 表示 + 1-5 星 rating (W26 の基礎).

設計方針 (feedback_ui_design.md 遵守):
  - expander 禁止. 全セクションを container(border=True) で常時表示
  - 絵文字は最小限 (Karpathy K1)
  - 日本語ラベル統一
  - JARVIS テーマと整合
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import streamlit as st

logger = logging.getLogger(__name__)

# session_state プレフィクス (UI 衝突回避)
_SS = "rb_"


def _format_dt(s) -> str:
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(str(s)).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(s)[:16]


def _model_short(model: str) -> str:
    if "opus" in (model or "").lower():
        return "Opus"
    if "sonnet" in (model or "").lower():
        return "Sonnet"
    if "haiku" in (model or "").lower():
        return "Haiku"
    return model or "?"


def _model_color(model: str) -> str:
    s = _model_short(model)
    return {
        "Opus": "rgba(196,128,255,0.95)",
        "Sonnet": "rgba(120,200,255,0.95)",
        "Haiku": "rgba(180,220,200,0.85)",
    }.get(s, "rgba(180,180,180,0.7)")


def _render_qa_card(qa: dict) -> None:
    """1 件の Q&A を container 表示."""
    with st.container(border=True):
        # ヘッダ: 時刻 / model / cost / duration
        m_short = _model_short(qa.get("model", ""))
        m_color = _model_color(qa.get("model", ""))
        dur = qa.get("duration_ms", 0) // 1000
        cost = qa.get("cost_usd") or 0.0
        rating = qa.get("user_rating")
        rating_str = "★" * rating + "☆" * (5 - rating) if rating else "未評価"

        st.markdown(
            f'<div style="display:flex;gap:10px;align-items:center;font-family:JetBrains Mono;font-size:11px;">'
            f'<span style="color:#a89d8a;">#{qa["id"]}</span>'
            f'<span style="color:#a89d8a;">{_format_dt(qa.get("asked_at"))}</span>'
            f'<span style="color:{m_color};font-weight:600;">{m_short}</span>'
            f'<span style="color:#7a6e5f;">{dur}s</span>'
            f'<span style="color:#7a6e5f;">${cost:.4f}</span>'
            f'<span style="color:#c89b2a;margin-left:auto;">{rating_str}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        # query (1 行省略表示)
        q_short = (qa.get("query") or "")[:120]
        st.markdown(
            f'<div style="font-size:13px;color:#e8ddc9;margin-top:4px;">{q_short}</div>',
            unsafe_allow_html=True,
        )

        # 詳細表示ボタン (expanded toggle in session_state)
        toggle_key = f"{_SS}detail_{qa['id']}"
        is_open = st.session_state.get(toggle_key, False)
        if st.button(
            "閉じる" if is_open else "詳細",
            key=f"{_SS}toggle_{qa['id']}",
            help="質問と回答の全文を表示/非表示",
        ):
            st.session_state[toggle_key] = not is_open
            st.rerun()

        if is_open:
            st.markdown("**質問全文**")
            st.markdown(f"```\n{qa.get('query','')}\n```")
            st.markdown("**回答**")
            st.markdown(qa.get("answer_md") or "(回答無し)")

            # rating UI (1-5 星)
            st.markdown("**評価 (W26 評価ループ)**")
            rate_cols = st.columns(6)
            for i in range(1, 6):
                with rate_cols[i - 1]:
                    if st.button(
                        "★" * i,
                        key=f"{_SS}rate_{qa['id']}_{i}",
                        help=f"{i} 星評価",
                    ):
                        try:
                            from monitor.research_brain import rate_qa
                            rate_qa(qa["id"], i)
                            st.toast(f"#{qa['id']} を {i} 星で評価しました")
                            st.rerun()
                        except Exception as e:  # noqa: BLE001
                            st.error(f"評価失敗: {e}")
            with rate_cols[5]:
                if rating:
                    if st.button(
                        "アクション済",
                        key=f"{_SS}act_{qa['id']}",
                        help="回答を見て実際にアクションを取った",
                    ):
                        try:
                            from monitor.research_brain import rate_qa
                            rate_qa(qa["id"], rating, action_taken=True)
                            st.toast(f"#{qa['id']} アクション記録")
                            st.rerun()
                        except Exception as e:  # noqa: BLE001
                            st.error(f"記録失敗: {e}")


def render_tab() -> None:
    """メインタブエントリポイント. app.py の `with tab_research_brain:` から呼ばれる."""
    st.subheader("リサーチ脳 (Opus 4.8)")
    st.caption(
        "MonoHonpo の eBay 業務 / 新システム開発 への問いに、動画学習 KB 30 件 + "
        "memory feedback + 既存 listing 統計を踏まえて Opus 4.8 が深く回答します。"
        "1 回 30-90 秒、Method A (Max plan 内、API 課金 $0)。"
    )

    # 質問入力
    query = st.chat_input(
        "質問を入力 (例: Section 232 で家電値付け戦略は? / W41 MCP 化の妥当性?)",
    )

    # session_state に質問キューを蓄積
    if query:
        st.session_state[f"{_SS}_pending_query"] = query

    pending = st.session_state.pop(f"{_SS}_pending_query", None)
    if pending:
        with st.status(
            f"Research 脳 (Opus 4.8) が思考中... (30-90 秒、動画 KB 30 件 + memory 参照)",
            expanded=True,
        ) as status:
            try:
                from monitor.research_brain import ask
                st.write(f"質問: {pending[:200]}")
                ans = ask(pending, source="ui_chat", save_history=True)
                if ans.error:
                    status.update(label=f"失敗: {ans.error}", state="error")
                    st.error(ans.answer_md)
                else:
                    status.update(
                        label=(
                            f"完了: {_model_short(ans.model_used)} "
                            f"{ans.duration_ms//1000}s "
                            f"${ans.cost_usd:.4f} "
                            f"citations {len(ans.citations)} 件"
                        ),
                        state="complete",
                    )
                    st.success("回答が届きました (下記参照)")
            except Exception as e:  # noqa: BLE001
                status.update(label=f"例外: {e}", state="error")
                logger.exception("research_brain.ask failed")
                st.error(f"Research 脳エラー: {e}")
        st.rerun()

    # 過去 Q&A 履歴 (新しい順)
    st.markdown("### 履歴 (直近 20 件)")
    try:
        from monitor.research_brain import get_recent_qa
        qa_list = get_recent_qa(limit=20)
    except Exception as e:  # noqa: BLE001
        st.error(f"履歴取得失敗: {e}")
        qa_list = []

    if not qa_list:
        st.info("まだ質問履歴がありません。上のチャット入力から質問してください。")
        return

    # source 別フィルタ
    sources = sorted(set(q.get("source", "?") for q in qa_list))
    selected_source = st.selectbox(
        "source フィルタ",
        ["all"] + sources,
        index=0,
        key=f"{_SS}filter_source",
    )

    filtered = (
        qa_list if selected_source == "all"
        else [q for q in qa_list if q.get("source") == selected_source]
    )

    for qa in filtered:
        _render_qa_card(qa)

    # 日次予算サマリ (sidebar 的位置)
    st.markdown("### 日次予算")
    try:
        import sqlite3
        from pathlib import Path as _P
        with sqlite3.connect(str(_P("data/monitor.db"))) as c:
            c.row_factory = sqlite3.Row
            today = datetime.now().strftime("%Y-%m-%d")
            row = c.execute(
                "SELECT * FROM research_brain_quota WHERE date=?", (today,)
            ).fetchone()
            if row:
                cols = st.columns(4)
                with cols[0]:
                    st.metric("Opus calls", row["opus_calls"], help="日次上限 30")
                with cols[1]:
                    st.metric("Opus cost", f"${row['opus_cost_usd']:.3f}")
                with cols[2]:
                    st.metric("Haiku calls", row["haiku_calls"], help="日次上限 200")
                with cols[3]:
                    st.metric("Haiku cost", f"${row['haiku_cost_usd']:.3f}")
            else:
                st.caption(f"本日 ({today}) はまだ呼出無し")
    except Exception as e:  # noqa: BLE001
        st.caption(f"予算情報取得失敗: {e}")

    # ────────────────────────────────────────
    # W26 評価ループ analytics (rating 集計)
    # ────────────────────────────────────────
    st.markdown("### 評価ループ (W26)")
    try:
        from monitor.research_brain_analytics import (
            get_overall_stats, find_low_rated, find_high_rated,
        )
        stats_7d = get_overall_stats(7)
        cols = st.columns(4)
        with cols[0]:
            st.metric("直近7日 Q&A", stats_7d["total_qa"])
        with cols[1]:
            st.metric("評価済", f"{stats_7d['rated_count']}/{stats_7d['total_qa']}",
                      delta=f"{stats_7d['rated_pct']:.0f}%")
        with cols[2]:
            avg_r = stats_7d["avg_rating"]
            st.metric("平均評価", f"{avg_r:.2f}" if avg_r else "—",
                      help="1-5 星、null は未評価")
        with cols[3]:
            st.metric("アクション率", f"{stats_7d['action_pct']:.0f}%",
                      help="高評価のうち実際にアクション取った割合")

        # 低評価サマリ
        low = find_low_rated(threshold=2, limit=5)
        if low:
            with st.container(border=True):
                st.markdown("**低評価 (1-2 星) の直近 Q&A** — プロンプト改善の手がかり (W26b 後続)")
                for qa in low:
                    st.caption(
                        f"#{qa['id']} {(qa.get('query') or '')[:70]} "
                        f"({qa.get('user_rating')} 星, {qa.get('source')})"
                    )

        # 高評価サマリ
        high = find_high_rated(threshold=5, limit=3)
        if high:
            with st.container(border=True):
                st.markdown("**高評価 (5 星) のお手本** — このパターンを学習")
                for qa in high:
                    st.caption(
                        f"#{qa['id']} {(qa.get('query') or '')[:70]} "
                        f"({qa.get('source')}, {qa.get('duration_ms', 0)//1000}s)"
                    )

        if stats_7d["rated_count"] == 0:
            st.info("まだ評価データがありません。Q&A の詳細ボタンから 1-5 星評価してください。")
    except Exception as e:  # noqa: BLE001
        st.caption(f"analytics 取得失敗: {e}")
