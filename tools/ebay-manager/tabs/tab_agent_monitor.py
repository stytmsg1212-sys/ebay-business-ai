#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""エージェント監視タブ (W221 Tier2 抽出、2026-06-04)。

app.py L6781-6963 から body をそのまま移植。挙動不変 (K2 surgical)。
Claude/Gemini API 稼働状況・モデル別コスト・エラー・.company 部署更新を一望。
"""
from __future__ import annotations

import html
from pathlib import Path

import streamlit as st


def render_agent_monitor_tab() -> None:
    """エージェント (API/部署) 監視タブ."""
    from monitor.database import get_conn as _ag_conn
    import pandas as _pd

    st.title("エージェント監視")
    st.caption("Claude/Gemini API 稼働状況、モデル使用、コスト、エラー、最近の更新を一望。")

    # === Section 1: 今日 / 過去7日 / 過去30日 の API 使用状況 ===
    st.subheader("API 使用状況")

    _periods = [("今日", "-1 day"), ("過去7日", "-7 days"), ("過去30日", "-30 days")]
    _cols = st.columns(len(_periods))
    for (_label, _range), _col in zip(_periods, _cols):
        with _col:
            with _ag_conn() as _c:
                _r = _c.execute(
                    """SELECT COUNT(*) as calls,
                              SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as ok,
                              SUM(input_tokens) as in_tok,
                              SUM(output_tokens) as out_tok,
                              SUM(cost_usd) as cost
                       FROM api_call_log
                       WHERE called_at >= datetime('now', ?)""",
                    (_range,),
                ).fetchone()
                _n = _r["calls"] or 0
                _ok = _r["ok"] or 0
                _rate = (_ok / _n * 100) if _n else 100.0
                _cost = _r["cost"] or 0.0
                st.metric(
                    _label,
                    f"{_n} calls",
                    delta=f"成功 {_rate:.1f}% | ${_cost:.2f}",
                    delta_color="off",
                )

    # === Section 2: モデル別内訳（過去7日） ===
    st.markdown("#### モデル別内訳 (過去7日)")
    with _ag_conn() as _c:
        _model_rows = [dict(r) for r in _c.execute(
            """SELECT provider, model,
                      COUNT(*) as calls,
                      SUM(input_tokens) as in_tok,
                      SUM(output_tokens) as out_tok,
                      SUM(cost_usd) as cost,
                      SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as errors,
                      AVG(duration_ms) as avg_ms
               FROM api_call_log
               WHERE called_at >= datetime('now', '-7 days')
               GROUP BY provider, model
               ORDER BY cost DESC"""
        ).fetchall()]

    if _model_rows:
        _df = _pd.DataFrame([{
            "Provider": r["provider"],
            "Model": r["model"],
            "Calls": r["calls"],
            "In tokens": f"{(r['in_tok'] or 0):,}",
            "Out tokens": f"{(r['out_tok'] or 0):,}",
            "Errors": r["errors"] or 0,
            "Avg ms": f"{int(r['avg_ms'] or 0)}",
            "Cost (USD)": f"${(r['cost'] or 0):.4f}",
        } for r in _model_rows])
        st.dataframe(_df, hide_index=True, width="stretch")
    else:
        st.info("まだ API コールの記録がありません。定時実行後にここに集計されます。")

    # === Section 3: Operation 別内訳 ===
    st.markdown("#### 用途別内訳 (過去7日)")
    with _ag_conn() as _c:
        _op_rows = [dict(r) for r in _c.execute(
            """SELECT operation, COUNT(*) as calls, SUM(cost_usd) as cost,
                      SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as errors
               FROM api_call_log
               WHERE called_at >= datetime('now', '-7 days')
               GROUP BY operation
               ORDER BY calls DESC"""
        ).fetchall()]
    if _op_rows:
        _df_op = _pd.DataFrame([{
            "Operation": r["operation"] or "(不明)",
            "Calls": r["calls"],
            "Errors": r["errors"] or 0,
            "Cost": f"${(r['cost'] or 0):.4f}",
        } for r in _op_rows])
        st.dataframe(_df_op, hide_index=True, width="stretch")
    else:
        st.caption("—")

    # === Section 4: 最近のエラー ===
    st.subheader("最近のエラー (30日)")
    with _ag_conn() as _c:
        _err_rows = [dict(r) for r in _c.execute(
            """SELECT called_at, provider, model, operation, error_message
               FROM api_call_log
               WHERE success=0 AND called_at >= datetime('now', '-30 days')
               ORDER BY called_at DESC LIMIT 20"""
        ).fetchall()]
    if _err_rows:
        for _e in _err_rows:
            st.markdown(
                f'<div style="border-left:2px solid rgba(240,64,80,0.6);padding:4px 10px;'
                f'margin:3px 0;background:rgba(240,64,80,0.04);font-size:12px;">'
                f'<span style="color:rgba(180,200,220,0.6);">{html.escape(_e.get("called_at") or "")}</span> '
                f'<span style="color:rgba(240,200,48,0.85);">{html.escape(_e.get("model") or "")}</span> '
                f'<span style="color:rgba(180,220,255,0.7);">{html.escape(_e.get("operation") or "")}</span>'
                f'<br><span style="color:rgba(255,180,180,0.9);">{html.escape((_e.get("error_message") or "")[:200])}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
    else:
        st.success("直近30日でAPIエラーはありません。")

    # === Section 5: .company 各部署 更新状況 ===
    st.subheader(".company 各部署の更新")
    st.caption("エージェント（仮想組織）のファイル最終更新。長期未更新は連携ギャップの兆候。")

    _company_root = Path(__file__).resolve().parent.parent.parent / ".company"
    _depts = [
        ("secretary", "秘書室", ["inbox", "notes", "todos", "routine_results"]),
        ("research", "リサーチ", ["notes", "learning", "topics"]),
        ("finance", "経理", ["expenses", "invoices"]),
        ("ebay-knowledge", "eBay知識", ["topics"]),
        ("engineering", "エンジニア", ["docs", "debug-log"]),
        ("daily-operations", "日々業務", ["logs", "listings", "orders", "customer-support"]),
        ("ebay-listing", "eBay出品", ["drafts"]),
    ]
    _dept_data = []
    for _code, _jp, _subdirs in _depts:
        _dept_dir = _company_root / _code
        if not _dept_dir.exists():
            _dept_data.append({"部署": _jp, "最終更新": "(無し)", "経過": "-", "ファイル数": 0})
            continue
        # 最新のファイル更新時刻を探す
        _latest = None
        _file_count = 0
        for _sd in _subdirs:
            _sd_path = _dept_dir / _sd
            if not _sd_path.exists():
                continue
            for _f in _sd_path.rglob("*"):
                if _f.is_file() and not _f.name.startswith("."):
                    _file_count += 1
                    _mt = _f.stat().st_mtime
                    if _latest is None or _mt > _latest:
                        _latest = _mt
        if _latest:
            from datetime import datetime as _dt
            _ago = (_dt.now() - _dt.fromtimestamp(_latest))
            _d = _ago.days
            _h = int(_ago.total_seconds() // 3600) % 24
            _ago_str = f"{_d}日{_h}時間前" if _d > 0 else f"{_h}時間前"
            _latest_str = _dt.fromtimestamp(_latest).strftime("%m-%d %H:%M")
        else:
            _ago_str = "(未更新)"
            _latest_str = "-"
        _dept_data.append({
            "部署": _jp,
            "最終更新": _latest_str,
            "経過": _ago_str,
            "ファイル数": _file_count,
        })
    st.dataframe(_pd.DataFrame(_dept_data), hide_index=True, width="stretch")

    # === Section 6: 日別 API コスト推移 ===
    st.subheader("日別コスト推移 (過去14日)")
    with _ag_conn() as _c:
        _daily = [dict(r) for r in _c.execute(
            """SELECT DATE(called_at) as day,
                      SUM(cost_usd) as cost,
                      COUNT(*) as calls
               FROM api_call_log
               WHERE called_at >= datetime('now', '-14 days')
               GROUP BY DATE(called_at)
               ORDER BY day ASC"""
        ).fetchall()]
    if _daily:
        _df_d = _pd.DataFrame(_daily).set_index("day")
        st.bar_chart(_df_d["cost"])
        st.caption(f"合計: ${sum((r['cost'] or 0) for r in _daily):.4f} / 14日 ({sum(r['calls'] for r in _daily)} calls)")
    else:
        st.caption("データ蓄積待ち")
