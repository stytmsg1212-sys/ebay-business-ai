# -*- coding: utf-8 -*-
"""定時実行タブ — 本日のスケジュール / リアルタイム状況 / 実行結果を一覧表示.

2026-04-25 hour ドリフト事故 (daily_relist 5 日間サイレントスキップ) を受けて新設.
ユーザーが MonoDeck から「今日 expected されたタスクが実行されたか」を一目で確認できる.

設計方針 (feedback_ui_design.md 遵守):
  - expander 禁止. 全セクションを container(border=True) で常時表示.
  - 絵文字禁止. ステータスは語彙 + 色で表現.
  - 日本語ラベル. JARVIS テーマと整合.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

from monitor.task_execution_log import (
    TASK_SCHEDULE,
    TASK_SCHEDULE_BY_KEY,
    find_missed_tasks,
    get_recent_executions,
    get_task_last_success,
    get_today_executions,
    get_today_expected_tasks,
)

# ステータス -> 表示色 / 表示ラベル
_STATUS_VIEW = {
    "started": ("実行中", "#c89b2a"),
    "completed": ("完了", "#6b7a5c"),
    "failed": ("失敗", "#d84c38"),
    "skip_disabled": ("スキップ(無効)", "#5a5248"),
    "skip_time": ("スキップ(時刻)", "#a85020"),
    "skip_weekday": ("スキップ(曜日)", "#5a5248"),
    "skip_other": ("スキップ", "#5a5248"),
}


def _render_status_badge(status: str) -> str:
    label, color = _STATUS_VIEW.get(status, (status, "#7a6e5f"))
    return (
        f"<span style='display:inline-block;padding:2px 10px;border-radius:3px;"
        f"background:{color};color:#fbf9f3;font-size:11px;font-weight:600;"
        f"font-family:JetBrains Mono,monospace;letter-spacing:0.5px;'>{label}</span>"
    )


def _format_dt(s) -> str:
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(str(s))
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return str(s)


def _format_full_dt(s) -> str:
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(str(s))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(s)


def _load_schedule_config() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def render_tab() -> None:
    """app.py の `with tab_scheduled:` ブロックから呼ばれるエントリポイント."""
    st.subheader("定時実行")
    st.caption(
        "本日 expected されたタスクのリアルタイム状況を表示します. "
        "サイレントスキップ検知のため scheduler.log と task_execution_log の両方を参照しています."
    )

    config = _load_schedule_config()
    now = datetime.now()
    # schedule_config の minute_map から各 hour の分を引く. 02:30 / 11:00 等.
    _minute_map_raw = (config.get("execution_schedule") or {}).get("minutes") or {}
    _minute_by_hour: dict[int, int] = {}
    for k, v in _minute_map_raw.items():
        try:
            _minute_by_hour[int(k)] = int(v)
        except (TypeError, ValueError):
            continue

    # ──────────────────────────────────────────────────────────
    # サマリ
    # ──────────────────────────────────────────────────────────
    today_logs = get_today_executions()
    completed_today = [r for r in today_logs if r["status"] == "completed" and r.get("success")]
    failed_today = [r for r in today_logs if r["status"] == "failed"]
    running_today = [r for r in today_logs if r["status"] == "started"]
    skip_today = [r for r in today_logs if str(r["status"]).startswith("skip_")]
    missed = find_missed_tasks(now, config=config)

    _scols = st.columns(5)
    with _scols[0]:
        st.metric("完了", len(completed_today))
    with _scols[1]:
        st.metric("実行中", len(running_today))
    with _scols[2]:
        st.metric("失敗", len(failed_today), delta_color="inverse")
    with _scols[3]:
        st.metric("スキップ", len(skip_today))
    with _scols[4]:
        st.metric("欠落", len(missed), delta_color="inverse")

    if missed:
        _names = ", ".join(
            f"{m.get('display_name') or m.get('task_key')}({int(m.get('expected_hour', 0)):02d}時)"
            for m in missed
        )
        st.error(
            f"本日 expected されたタスク {len(missed)} 件が未完了です: {_names}. "
            "scheduler.log と Discord 通知を確認してください."
        )

    # ──────────────────────────────────────────────────────────
    # 本日のスケジュール (expected vs actual 表)
    # ──────────────────────────────────────────────────────────
    st.markdown("### 本日のスケジュール")
    _today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    weekday = now.weekday()
    main_slots = config.get("execution_schedule", {}).get("times", [2, 11, 15, 18, 22])

    # 本日 expected の (task_key, hour) 全ペアを構築
    schedule_rows: list[dict] = []
    for t in TASK_SCHEDULE:
        if t.get("weekdays") is not None and weekday not in t["weekdays"]:
            continue
        # Codex Round 2 MEDIUM (2026-05-16): kind=interval task は main_slots 展開しない.
        # find_missed_tasks と整合させ、UI 上も main batch slot に "expected" 表示しない.
        if t.get("kind") == "interval":
            continue
        hours = t.get("hours") if t.get("hours") is not None else main_slots
        for h in hours:
            schedule_rows.append({
                "task_key": t["key"],
                "display": t["display"],
                "hour": int(h),
                "owner": t["owner"],
            })

    # 各 slot ごとの最新実行ログを引き当て
    for r in schedule_rows:
        match = [
            log for log in today_logs
            if log["task_key"] == r["task_key"] and int(log["batch_hour"]) == r["hour"]
        ]
        # 最も新しい successful 完了 > started > 最新 skip > 何もなし
        if match:
            success = next((m for m in match if m["status"] == "completed" and m.get("success")), None)
            running = next((m for m in match if m["status"] == "started"), None)
            failed = next((m for m in match if m["status"] == "failed"), None)
            r["log"] = success or running or failed or match[0]
        else:
            r["log"] = None

    # 上 → 時刻順、同時刻内では owner=main → news → customs の順、その後 task_key 辞書順
    # W154 (2026-05-22): x_news (旧 W13 X/Grok) を news (W154 統合) に rename
    _owner_order = {"main": 0, "news": 1, "customs": 2}
    schedule_rows.sort(key=lambda x: (x["hour"], _owner_order.get(x["owner"], 9), x["task_key"]))

    # ヘッダー
    _hdr = st.container()
    with _hdr:
        cols = st.columns([0.7, 2.4, 1.0, 1.0, 1.0, 2.0])
        for c, lbl in zip(cols, ["時刻", "タスク", "状態", "開始", "終了", "メッセージ"]):
            c.markdown(
                f"<div style='font-family:JetBrains Mono,monospace;font-size:11px;"
                f"color:#8d927f;letter-spacing:1px;text-transform:uppercase;'>{lbl}</div>",
                unsafe_allow_html=True,
            )

    for r in schedule_rows:
        log = r.get("log")
        if log is None:
            # 期待時刻と現在時刻を比較して「未到達」 or 「欠落」
            slot_minute = _minute_by_hour.get(r["hour"], 0)
            slot_dt_today = now.replace(hour=r["hour"], minute=slot_minute, second=0, microsecond=0)
            # batch スタート想定時刻 + grace 60 分以内なら「未到達/実行中の可能性」
            grace_minutes = 60
            if now < slot_dt_today + timedelta(minutes=grace_minutes):
                state_html = (
                    "<span style='display:inline-block;padding:2px 10px;border-radius:3px;"
                    "background:#2a2420;color:#a89d8a;font-size:11px;font-weight:600;"
                    "font-family:JetBrains Mono,monospace;letter-spacing:0.5px;'>未到達</span>"
                )
                started_disp = "—"
                finished_disp = "—"
                msg = ""
            else:
                state_html = (
                    "<span style='display:inline-block;padding:2px 10px;border-radius:3px;"
                    "background:#d84c38;color:#fbf9f3;font-size:11px;font-weight:600;"
                    "font-family:JetBrains Mono,monospace;letter-spacing:0.5px;'>欠落</span>"
                )
                started_disp = "—"
                finished_disp = "—"
                msg = "本日 expected slot に実行ログ無し"
        else:
            state_html = _render_status_badge(log["status"])
            started_disp = _format_dt(log.get("started_at"))
            finished_disp = _format_dt(log.get("finished_at"))
            msg_raw = (log.get("message") or "")
            # JSON message なら code-like 表示用に短縮
            msg = msg_raw[:120] + ("…" if len(msg_raw) > 120 else "")

        with st.container(border=True):
            cols = st.columns([0.7, 2.4, 1.0, 1.0, 1.0, 2.0])
            cols[0].markdown(
                f"<div style='font-family:JetBrains Mono,monospace;font-size:13px;"
                f"color:#2a2e2a;'>{r['hour']:02d}:00</div>",
                unsafe_allow_html=True,
            )
            cols[1].markdown(
                f"<div style='font-family:Inter,sans-serif;font-size:13px;color:#2a2e2a;'>"
                f"{r['display']} <span style='color:#7a6e5f;font-size:11px;font-family:JetBrains Mono;'>"
                f"({r['task_key']})</span></div>",
                unsafe_allow_html=True,
            )
            cols[2].markdown(state_html, unsafe_allow_html=True)
            cols[3].markdown(
                f"<div style='font-family:JetBrains Mono;font-size:12px;color:#8d927f;'>{started_disp}</div>",
                unsafe_allow_html=True,
            )
            cols[4].markdown(
                f"<div style='font-family:JetBrains Mono;font-size:12px;color:#8d927f;'>{finished_disp}</div>",
                unsafe_allow_html=True,
            )
            cols[5].markdown(
                f"<div style='font-family:JetBrains Mono;font-size:11px;color:#7a6e5f;"
                f"overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>{msg}</div>",
                unsafe_allow_html=True,
            )

    # ──────────────────────────────────────────────────────────
    # タスク別 直近実行サマリ (last success / last failure)
    # ──────────────────────────────────────────────────────────
    st.markdown("### タスク別 直近実行")
    st.caption("各タスクの最終成功時刻と「最後に実行されてから何時間経過したか」を一覧します.")

    _hdr2 = st.container()
    with _hdr2:
        cols = st.columns([2.4, 1.5, 1.5, 1.5])
        for c, lbl in zip(cols, ["タスク", "想定スケジュール", "最終成功", "経過"]):
            c.markdown(
                f"<div style='font-family:JetBrains Mono,monospace;font-size:11px;"
                f"color:#8d927f;letter-spacing:1px;text-transform:uppercase;'>{lbl}</div>",
                unsafe_allow_html=True,
            )

    for t in TASK_SCHEDULE:
        last = get_task_last_success(t["key"])
        if last:
            elapsed = now - last
            elapsed_h = elapsed.total_seconds() / 3600.0
            if elapsed_h < 24:
                elapsed_str = f"{elapsed_h:.1f} 時間前"
                elapsed_color = "#6b7a5c"
            elif elapsed_h < 72:
                elapsed_str = f"{elapsed_h/24:.1f} 日前"
                elapsed_color = "#c89b2a"
            else:
                elapsed_str = f"{elapsed_h/24:.0f} 日前"
                elapsed_color = "#d84c38"
            last_str = last.strftime("%Y-%m-%d %H:%M")
        else:
            elapsed_str = "未記録"
            elapsed_color = "#7a6e5f"
            last_str = "—"

        # スケジュール表示
        # Codex Round 2 MEDIUM (2026-05-16): kind=interval は "N分ごと" 専用表示で正しく区別.
        if t.get("kind") == "interval":
            interval = t.get("interval_minutes")
            sched_str = f"{interval}分ごと" if interval else "interval (詳細未指定)"
        elif t.get("hours") is None:
            sched_str = "毎batch"
        else:
            sched_str = ", ".join(f"{h:02d}時" for h in t["hours"])
        if t.get("weekdays") is not None:
            wd_names = ["月", "火", "水", "木", "金", "土", "日"]
            sched_str += " (" + ",".join(wd_names[w] for w in t["weekdays"]) + ")"

        with st.container(border=True):
            cols = st.columns([2.4, 1.5, 1.5, 1.5])
            cols[0].markdown(
                f"<div style='font-family:Inter,sans-serif;font-size:13px;color:#2a2e2a;'>"
                f"{t['display']} <span style='color:#7a6e5f;font-size:11px;font-family:JetBrains Mono;'>"
                f"({t['key']})</span></div>",
                unsafe_allow_html=True,
            )
            cols[1].markdown(
                f"<div style='font-family:JetBrains Mono;font-size:12px;color:#8d927f;'>{sched_str}</div>",
                unsafe_allow_html=True,
            )
            cols[2].markdown(
                f"<div style='font-family:JetBrains Mono;font-size:12px;color:#2a2e2a;'>{last_str}</div>",
                unsafe_allow_html=True,
            )
            cols[3].markdown(
                f"<div style='font-family:JetBrains Mono;font-size:12px;color:{elapsed_color};font-weight:600;'>{elapsed_str}</div>",
                unsafe_allow_html=True,
            )

    # ──────────────────────────────────────────────────────────
    # 直近 7 日 実行ログ (生)
    # ──────────────────────────────────────────────────────────
    st.markdown("### 直近 7 日 実行ログ (新しい順 / 上位 200 件)")
    rows = get_recent_executions(days=7, limit=200)
    if not rows:
        st.caption("ログ無し. scheduler が再起動された直後の場合、まだ蓄積されていません.")
        return

    # シンプルな dataframe 表示 (色付けはせず、ソート/検索はユーザーが streamlit のデフォルトで使う)
    import pandas as _pd

    df = _pd.DataFrame([
        {
            "started_at": _format_full_dt(r["started_at"]),
            "task_key": r["task_key"],
            "display": r.get("display_name") or r["task_key"],
            "batch_hour": r["batch_hour"],
            "status": r["status"],
            "success": r.get("success"),
            "duration_sec": (round(r["duration_sec"], 1) if r.get("duration_sec") else None),
            "message": (r.get("message") or "")[:200],
        }
        for r in rows
    ])
    st.dataframe(df, hide_index=True, width="stretch", height=420)
