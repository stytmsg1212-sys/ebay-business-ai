# -*- coding: utf-8 -*-
"""W122: MonoDeck 「今日の発掘候補」タブ.

朝 07:00 cron で生成された新商品候補 3 件を表示し、
クリック型評価 (buy/skip/hold/listed) + 自由記述コメントで学習データを蓄積.

設計方針 (feedback_ui_design.md 遵守):
  - expander 禁止. container(border=True) で常時表示
  - 絵文字は最小限
  - JARVIS テーマ整合
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import streamlit as st

logger = logging.getLogger(__name__)

_SS = "md_"  # session_state prefix


def _format_dt(s) -> str:
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(str(s)).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(s)[:16]


def _star_str(rating) -> str:
    if rating is None:
        return "—"
    try:
        n = int(rating)
    except (ValueError, TypeError):
        return "—"
    n = max(0, min(5, n))
    return "★" * n + "☆" * (5 - n)


def _layer_label(origin: str) -> str:
    mapping = {
        "horizontal_pattern": "階層 1 横展開",
        "meta_pattern": "階層 2 メタ拡張",
        "competitor_sold": "階層 3 競合 sold",
        "error": "エラー",
        "parse_error": "JSON 解析失敗",
        "unknown": "未分類",
    }
    return mapping.get(origin or "unknown", origin or "未分類")


def _vero_color(level: str) -> str:
    return {
        "none": "#7ac17a",
        "low": "#a4c87a",
        "medium": "#c89b2a",
        "high": "#d05858",
    }.get((level or "").lower(), "#a89d8a")


def _decision_label(decision: str) -> str:
    return {
        "buy": "買う",
        "skip": "見送る",
        "hold": "保留",
        "listed": "出品済",
    }.get(decision or "", "—")


def _render_discovery_economics_html(
    sup_jpy, ebay_usd, profit_usd, sold_30d, comp_jp
) -> str:
    """採算ブロックを 2 軸 HTML で返す (self-contained、表示のみ).

    軸:
      左 (input):  仕入 ¥sup_jpy  →  eBay 想定 $ebay_usd
      右 (output): 想定粗利 $profit_usd  /  類似 sold(30d) sold_30d 件  /  JP 競合 comp_jp 人

    色 (粗利):
      profit > 0  → 緑系 (#7ac17a)
      profit == 0 → セピア中立 (#bfb59a) + "見積不能" 表現 (W129 保持、$0 を赤字と誤読させない)
      profit < 0  → 赤系 (#d05858)
      None/非数値 → セピア中立 (#bfb59a) + "—"

    money-direct 表示の安全弁: profit_usd が 0 のときは金額 "$0" を出さず、見積不能シグナル
    (W129、prompt 制約由来) を保持する。本ヘルパーは表示専用、判定ロジック不変。
    """
    # 仕入 ¥
    sup_str = f"¥{sup_jpy:,}" if isinstance(sup_jpy, (int, float)) else "—"
    # eBay 想定 $
    ebay_str = f"${ebay_usd:.0f}" if isinstance(ebay_usd, (int, float)) else "—"
    # 想定粗利 $ (W129 sentinel 保持)
    if isinstance(profit_usd, (int, float)):
        if profit_usd == 0:
            profit_str = "見積不能 (理由は根拠欄)"
            profit_color = "#5f6557"
        elif profit_usd > 0:
            profit_str = f"${profit_usd:.0f}"
            profit_color = "#2e7d5b"
        else:
            profit_str = f"-${abs(profit_usd):.0f}"
            profit_color = "#a8341b"
    else:
        profit_str = "—"
        profit_color = "#5f6557"
    # 類似 sold / 競合
    sold_str = f"{sold_30d} 件" if isinstance(sold_30d, (int, float)) else "—"
    comp_str = f"{comp_jp} 人" if isinstance(comp_jp, (int, float)) else "—"

    # 2 軸レイアウト (self-contained inline CSS、pm-* 共有 class 非依存)
    label_style = "color:#8d927f;font-size:11px;letter-spacing:0.04em;"
    axis_style = (
        "flex:1;padding:8px 10px;border:1px solid rgba(166,150,121,0.25);"
        "border-radius:4px;background:#f2ecdf;"
    )
    return (
        f'<div style="display:flex;gap:10px;margin:6px 0 4px 0;">'
        # 軸 1: input (仕入 → eBay 想定)
        f'<div style="{axis_style}">'
        f'<div style="{label_style}">仕入 → eBay 想定</div>'
        f'<div style="color:#2a2e2a;font-size:14px;font-weight:600;'
        f'margin-top:2px;">{sup_str} → {ebay_str}</div>'
        f'</div>'
        # 軸 2: output (想定粗利 / sold / 競合)
        f'<div style="{axis_style}">'
        f'<div style="{label_style}">想定粗利 / 類似 sold(30d) / JP 競合</div>'
        f'<div style="margin-top:2px;font-size:14px;">'
        f'<span style="color:{profit_color};font-weight:700;">{profit_str}</span>'
        f'<span style="color:#8d927f;"> / </span>'
        f'<span style="color:#2a2e2a;">{sold_str}</span>'
        f'<span style="color:#8d927f;"> / </span>'
        f'<span style="color:#2a2e2a;">{comp_str}</span>'
        f'</div>'
        f'</div>'
        f'</div>'
    )


def _render_candidate(cand: dict) -> None:
    """1 候補の表示 + フィードバック UI."""
    cid = cand["id"]
    name = cand.get("product_name") or "(無題)"
    star = cand.get("star_rating")
    rationale = cand.get("rationale") or ""
    next_action = cand.get("next_action") or ""
    rank = cand.get("candidate_rank") or 0
    origin = cand.get("layer_origin") or "unknown"
    decision = cand.get("user_decision")
    comment = cand.get("user_comment") or ""

    # 価格情報
    sup_jpy = cand.get("supplier_price_jpy")
    ebay_usd = cand.get("ebay_estimated_price_usd")
    profit_usd = cand.get("estimated_profit_usd")
    sold_30d = cand.get("similar_sold_count_30d")
    comp_jp = cand.get("competitor_jp_count")
    vero = cand.get("vero_risk_level") or "unknown"

    with st.container(border=True):
        # ヘッダ (セピアトーン維持、letter-spacing で視認性向上)
        st.markdown(
            f'<div style="display:flex;gap:10px;align-items:center;'
            f'font-family:JetBrains Mono;font-size:12px;'
            f'letter-spacing:0.03em;">'
            f'<span style="color:#8d927f;">#{rank}</span>'
            f'<span style="color:#7a6e5f;">{_layer_label(origin)}</span>'
            f'<span style="color:{_vero_color(vero)};font-weight:700;'
            f'padding:1px 6px;border:1px solid {_vero_color(vero)};'
            f'border-radius:3px;">VeRO: {vero}</span>'
            f'<span style="color:#c89b2a;margin-left:auto;">'
            f'{_star_str(star)}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        # 商品名
        st.markdown(
            f'<div style="font-size:18px;font-weight:700;color:#2a2e2a;'
            f'margin:6px 0 4px 0;">{name}</div>',
            unsafe_allow_html=True,
        )
        # 売れる根拠
        if rationale:
            st.markdown(
                f'<div style="color:#5f6557;font-size:13px;margin-bottom:8px;">'
                f'{rationale}</div>',
                unsafe_allow_html=True,
            )

        # 採算 2 軸 (仕入¥→eBay想定$ / 想定粗利$・類似sold・JP競合)
        # W129 sentinel (profit==0 = 見積不能) はヘルパー内で保持、挙動不変
        st.markdown(
            _render_discovery_economics_html(
                sup_jpy, ebay_usd, profit_usd, sold_30d, comp_jp
            ),
            unsafe_allow_html=True,
        )

        # 次アクション
        if next_action:
            st.markdown(
                f'<div style="color:#2e7d5b;font-size:13px;margin-top:6px;">'
                f'<b>次アクション:</b> {next_action}</div>',
                unsafe_allow_html=True,
            )

        # 情報源 URL
        try:
            urls = json.loads(cand.get("source_urls") or "[]")
        except (json.JSONDecodeError, TypeError):
            urls = []
        if urls:
            url_md = " / ".join(f"[出典]({u})" for u in urls[:5])
            st.markdown(url_md)

        # フィードバック UI (セピアトーン維持、letter-spacing で視認性向上)
        st.markdown(
            '<div style="margin-top:8px;color:#8d927f;font-size:11px;'
            'letter-spacing:0.06em;font-weight:600;">'
            'フィードバック</div>',
            unsafe_allow_html=True,
        )
        if decision:
            st.caption(
                f"判定済: {_decision_label(decision)} "
                f"({_format_dt(cand.get('user_decided_at'))})"
            )

        col_b1, col_b2, col_b3, col_b4 = st.columns(4)
        for col, dec_key, dec_label in [
            (col_b1, "buy", "買う"),
            (col_b2, "skip", "見送る"),
            (col_b3, "hold", "保留"),
            (col_b4, "listed", "出品済"),
        ]:
            with col:
                btn_key = f"{_SS}btn_{cid}_{dec_key}"
                if st.button(
                    dec_label,
                    key=btn_key,
                    use_container_width=True,
                    type="primary" if decision == dec_key else "secondary",
                ):
                    st.session_state[f"{_SS}pending_{cid}"] = dec_key

        # コメント textarea + 保存
        pending = st.session_state.get(f"{_SS}pending_{cid}")
        if pending:
            st.info(f"判定: {_decision_label(pending)} — コメント入力後に保存")

        comment_key = f"{_SS}comment_{cid}"
        new_comment = st.text_area(
            "コメント (任意)",
            value=comment,
            key=comment_key,
            height=68,
            placeholder="判断理由、補足情報、次回への注意点など",
        )

        if st.button(
            "フィードバック保存",
            key=f"{_SS}save_{cid}",
            use_container_width=True,
        ):
            target_decision = pending or decision
            if not target_decision:
                st.warning("先に [買う/見送る/保留/出品済] のいずれかを選んでください.")
            else:
                from tasks.task_morning_discovery import update_candidate_feedback
                ok = update_candidate_feedback(
                    cid, target_decision, new_comment or ""
                )
                if ok:
                    st.success(
                        f"保存しました: {_decision_label(target_decision)}"
                    )
                    st.session_state.pop(f"{_SS}pending_{cid}", None)
                    st.rerun()
                else:
                    st.error("保存失敗: decision 値不正")


def _render_recent_feedback(days: int = 7) -> None:
    """過去 N 日のフィードバック履歴サマリ (container 内、expander 禁止)."""
    from tasks.task_morning_discovery import get_recent_feedback
    rows = get_recent_feedback(days=days)
    with st.container(border=True):
        st.markdown(
            f'<div style="color:#8d927f;font-size:13px;font-weight:600;">'
            f'過去 {days} 日のフィードバック ({len(rows)} 件)</div>',
            unsafe_allow_html=True,
        )
        if not rows:
            st.caption("フィードバック履歴なし.")
            return
        counts = {"buy": 0, "skip": 0, "hold": 0, "listed": 0}
        for r in rows:
            d = r.get("user_decision")
            if d in counts:
                counts[d] += 1
        st.markdown(
            f"**買う** {counts['buy']} / **出品済** {counts['listed']} "
            f"/ **保留** {counts['hold']} / **見送る** {counts['skip']}"
        )
        # 最近 10 件サマリ
        for r in rows[:10]:
            name = (r.get("product_name") or "(無題)")[:50]
            dec = _decision_label(r.get("user_decision"))
            comment = (r.get("user_comment") or "")[:80]
            dt = _format_dt(r.get("user_decided_at"))
            st.markdown(
                f"- `{dt}` **{dec}** {name}"
                + (f" — {comment}" if comment else "")
            )


def render_morning_discovery_tab() -> None:
    """W122 タブのメインエントリ."""
    from tasks.task_morning_discovery import (
        get_today_candidates,
        run_morning_discovery,
    )

    st.markdown(
        '<h2 style="color:#2a2e2a;margin-bottom:0;">今日の発掘候補</h2>',
        unsafe_allow_html=True,
    )
    today = datetime.now().strftime("%Y-%m-%d")
    st.caption(f"{today} / 朝 07:00 cron 自動発掘 (W122)")

    candidates = get_today_candidates()

    # 手動再生成 (Phase 3: 朝の cron が動かなかった日の救済策)
    col_a, col_b = st.columns([3, 1])
    with col_b:
        if st.button("今すぐ生成 (手動)", use_container_width=True):
            # H-1 fix: webhook_url 取得のために settings を渡す
            settings = st.session_state.get("settings") or {}
            with st.spinner("Opus 4.8 が分析中... (60-120 秒)"):
                result = run_morning_discovery(config=settings)
            if result.get("success"):
                msg = result.get("message", "完了")
                if result.get("fallback_warning"):
                    st.warning(result["fallback_warning"])
                st.success(msg)
                st.rerun()
            else:
                st.error(result.get("message", "失敗"))

    if not candidates:
        with st.container(border=True):
            st.info(
                "本日の発掘候補は未生成です. "
                "07:00 の cron 自動実行を待つか、上の『今すぐ生成 (手動)』を押してください."
            )
    else:
        for cand in candidates:
            _render_candidate(cand)

    st.markdown("---")
    _render_recent_feedback(days=7)
