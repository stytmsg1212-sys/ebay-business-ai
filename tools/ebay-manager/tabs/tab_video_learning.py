#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""動画学習タブ (W221 Tier2 抽出、2026-06-04)。

app.py L6775-6992 から body をそのまま移植。挙動不変 (K2 surgical)。
YouTube URL 登録 → Gemini 2.5 Flash で eBay 物販視点の構造化知識抽出。
"""
from __future__ import annotations

import html

import streamlit as st


def render_video_learning_tab() -> None:
    """動画学習 (YouTube → Gemini 構造化知識) タブ."""
    import json as _json_vid
    import threading
    from monitor.database import get_conn as _vl_conn

    st.title("動画学習")
    st.caption(
        "YouTube動画を登録すると Gemini 2.5 Flash がeBay物販視点で構造化知識を抽出します。"
        "リサーチエージェントと仕入先候補探索に自動で知識が反映されます。"
    )

    # ── 登録フォーム ──
    st.subheader("新規登録")
    _vl_url = st.text_input(
        "YouTube URL",
        key="video_learning_url",
        placeholder="https://www.youtube.com/watch?v=...",
    )
    _vl_col_a, _vl_col_b = st.columns([1, 3])
    with _vl_col_a:
        _vl_btn_now = st.button("即時処理", type="primary", key="vl_now")
    with _vl_col_b:
        _vl_btn_queue = st.button("キューに追加のみ", key="vl_queue")

    if _vl_btn_now and _vl_url:
        from tasks.task_video_learning import enqueue_video
        _enq = enqueue_video(_vl_url)
        if not _enq.get("success"):
            st.error(_enq.get("message"))
        elif _enq.get("status") == "exists":
            st.warning(f"既に登録されています (status={_enq.get('existing_status')})")
        else:
            # バックグラウンド処理開始（Streamlit 再描画を止めない）
            def _bg_process(u):
                from tasks.task_video_learning import process_single_video
                process_single_video(u)
            threading.Thread(target=_bg_process, args=(_vl_url,), daemon=True).start()
            st.success("処理を開始しました。数分後にこのページを更新すると結果が表示されます。")

    if _vl_btn_queue and _vl_url:
        from tasks.task_video_learning import enqueue_video
        _enq = enqueue_video(_vl_url)
        if _enq.get("success"):
            if _enq.get("status") == "exists":
                st.warning(f"既に登録されています (status={_enq.get('existing_status')})")
            else:
                st.success("キューに追加しました（次回 daily_scheduler 02:30 で処理されます）")
        else:
            st.error(_enq.get("message"))

    st.divider()

    # ── ライブラリ ──
    st.subheader("動画ライブラリ")
    with _vl_conn() as _vc:
        _vl_rows = [dict(r) for r in _vc.execute(
            "SELECT * FROM videos_learned ORDER BY added_at DESC"
        ).fetchall()]

    if not _vl_rows:
        st.info("まだ動画が登録されていません。上のフォームからYouTube URLを追加してください。")
    else:
        # ステータス分布 (わかりやすい日本語ラベル)
        _st_counts = {}
        for r in _vl_rows:
            _st_counts[r['status']] = _st_counts.get(r['status'], 0) + 1
        _status_ja = {
            "done": "✅ 学習完了",
            "processing": "処理中",
            "pending": "⏳ 処理待ち",
            "failed": "❌ 失敗",
        }
        total = len(_vl_rows)
        summary_parts = [
            f"{_status_ja.get(k, k)}: {v}件"
            for k, v in sorted(_st_counts.items())
        ]
        st.caption(f"**登録 {total}件** — " + " / ".join(summary_parts))

        # 最終学習実行時刻を表示 (done のうち最新の processed_at)
        _last_done = None
        for r in _vl_rows:
            if r.get("status") == "done":
                _pa = r.get("processed_at") or r.get("added_at")
                if _pa and (_last_done is None or _pa > _last_done):
                    _last_done = _pa
        if _last_done:
            st.caption(f"**最終学習完了**: {_last_done}")

        # pending 件数が多い場合の案内
        _pending_n = _st_counts.get("pending", 0)
        if _pending_n >= 5:
            st.warning(
                f"⏳ 処理待ちが {_pending_n} 件あります。"
                " 日々の定時実行 (06:00 / 11:00 / 18:00) で順次学習処理されます。"
                " 今すぐ処理したい場合は「手動実行」タブから「動画学習」を実行してください。"
            )

        _vl_filter = st.selectbox("ステータス", ["すべて", "done", "pending", "processing", "failed"], key="vl_filter")

        for _r in _vl_rows:
            if _vl_filter != "すべて" and _r.get("status") != _vl_filter:
                continue

            _status = _r.get("status", "?")
            _st_color = {
                "done": "#2e7d5b",
                "processing": "#b8860b",
                "pending": "#5f6557",
                "failed": "rgba(255,80,80,0.9)",
            }.get(_status, "rgba(200,200,200,0.5)")

            _title = _r.get("title") or _r.get("video_id") or ""
            _dur = _r.get("duration_sec") or 0
            _dur_str = f"{_dur//60}分{_dur%60}秒" if _dur else ""
            _added = _r.get("added_at") or ""

            _dur_html = (
                f'<span style="color:#8d927f;font-size:11px;">{_dur_str}</span>'
                if _dur_str else ''
            )

            # 関税時代バッジ
            _era = _r.get("tariff_era") or ""
            _era_label = {
                "pre_tariff": "旧時代(DDU)",
                "transition": "移行期",
                "post_tariff": "新時代(DDP)",
                "evergreen": "時代不問",
            }.get(_era, "")
            _era_color = {
                "pre_tariff": "rgba(200,150,150,0.75)",
                "transition": "rgba(240,200,80,0.85)",
                "post_tariff": "rgba(118,255,180,0.85)",
                "evergreen": "#5f6557",
            }.get(_era, "rgba(180,180,180,0.5)")
            _era_html = (
                f'<span style="color:{_era_color};font-size:11px;font-weight:700;">[{_era_label}]</span>'
                if _era_label else ''
            )

            # 公開日
            _pub = _r.get("published_date") or ""
            _pub_html = (
                f'<span style="color:#8d927f;font-size:11px;">{html.escape(_pub)}</span>'
                if _pub else ''
            )
            _summary_html = (
                f'<div style="margin-top:6px;font-size:13px;color:#2a2e2a;line-height:1.5;">'
                f'{html.escape((_r.get("summary_ja") or "")[:300])}</div>'
                if _r.get("summary_ja") else ''
            )
            _topics_html = (
                f'<div style="margin-top:4px;font-size:11px;color:#8d927f;">'
                f'Topics: {html.escape(_r.get("topics") or "")}</div>'
                if _r.get("topics") else ''
            )

            st.markdown(
                f'<div style="border:1px solid rgba(166,150,121,0.30);'
                f'border-radius:6px;padding:10px 14px;margin:6px 0;'
                f'background:rgba(166,150,121,0.24);">'
                f'<div style="display:flex;gap:12px;align-items:center;font-family:Share Tech Mono,monospace;">'
                f'<span style="color:{_st_color};font-size:11px;font-weight:700;">[{_status.upper()}]</span>'
                f'{_era_html}'
                f'{_pub_html}'
                f'<span style="color:#2a2e2a;font-size:12px;">{html.escape(_title[:80])}</span>'
                f'{_dur_html}'
                f'<span style="color:#8d927f;font-size:10px;margin-left:auto;">{html.escape(_added[:16])}</span>'
                f'</div>'
                f'{_summary_html}{_topics_html}'
                f'</div>',
                unsafe_allow_html=True
            )

            if _status == "done":
                with st.expander(f"▸ 詳細 - {_title[:50]}"):
                    _insights = _json_vid.loads(_r.get("key_insights") or "[]")
                    if _insights:
                        st.markdown("**Key Insights**")
                        for i, x in enumerate(_insights, 1):
                            st.markdown(f"{i}. {x}")

                    _steps = _json_vid.loads(_r.get("actionable_steps") or "[]")
                    if _steps:
                        st.markdown("**Actionable Steps**")
                        for i, x in enumerate(_steps, 1):
                            st.markdown(f"{i}. {x}")

                    _products = _json_vid.loads(_r.get("products_mentioned") or "[]")
                    if _products:
                        st.markdown("**言及された商品**")
                        for p in _products:
                            if isinstance(p, dict):
                                st.markdown(f"- **{p.get('name','?')}** ({p.get('category','?')}) — {p.get('price_range','?')}")

                    _hints = _json_vid.loads(_r.get("pricing_hints") or "[]")
                    if _hints:
                        st.markdown("**価格ヒント**")
                        for h in _hints:
                            if isinstance(h, dict):
                                st.markdown(f"- **{h.get('product','?')}**: {h.get('range','?')}  \n  {h.get('reasoning','')}")

                    _plats = _json_vid.loads(_r.get("platforms_mentioned") or "[]")
                    if _plats:
                        st.markdown(f"**Platforms**: {', '.join(_plats)}")

                    _kws_rows = []
                    with _vl_conn() as _cc:
                        _kws_rows = [k['keyword'] for k in _cc.execute(
                            "SELECT keyword FROM knowledge_index WHERE video_id=? ORDER BY keyword",
                            (_r['video_id'],)
                        ).fetchall()]
                    if _kws_rows:
                        st.markdown(f"**Indexed Keywords ({len(_kws_rows)}件)**: {', '.join(_kws_rows)}")

            if _status == "failed":
                st.caption(f"失敗理由: {_r.get('error_detail') or _r.get('status_message') or '不明'}")
