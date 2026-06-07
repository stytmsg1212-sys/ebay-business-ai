#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 商品リサーチ Wizard タブ — フェーズ B MVP PoC.

仕様書: .company/engineering/docs/2026-06-07-product-research-automation-spec.md
ロック済み決定 (§7 / §8):
  - PoC = 「出品しない」。eBay 出品 / 購入 / 監視登録ボタンは置かない。
  - ゲートは当面手入力 (§8 P1-2)。
  - 同一商品 + 同状態の最終一致判定は人間 (§2-B)。AI は候補提示のみ。

3 セクション構成:
  A: 売れ行きゲート (手入力 → 5 分岐判定)
  B: フリマ探索 + AI 同一性 + 利益判定 (ゲート target_* 時のみ表示)
  C: research_candidates 一覧 (status フィルタ付き)

K2 (外科的変更): 既存タブ (tab_product_management / tab_research_wizard 等) には一切触れない。
K1 (シンプル): 出品 / 監視 / 承認フローは作らない (W228 PoC スコープ外)。
Q0 (サイレントスキップ禁止): フリマ探索エラーは偽成功を返さず st.error で表示。
"""
from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Optional

import pandas as pd
import streamlit as st

from monitor.research_gate import (
    DECISION_REJECT_DEADSTOCK,
    DECISION_REJECT_NO_DEMAND,
    DECISION_SKIP_TOO_NEW,
    DECISION_TARGET_INSTOCK,
    DECISION_TARGET_OOS_WATCH,
    evaluate_sourcing_gate,
)
from monitor.research_candidates_db import (
    STATUS_NEEDS_REVIEW,
    STATUS_NEW,
    STATUS_NOT_FOUND,
    STATUS_SOURCED,
    STATUS_SOURCING,
    list_research_candidates,
)

logger = logging.getLogger(__name__)

# ---- 定数 ------------------------------------------------------------------

# セクション B in-flight ロック key (同一セッション内の連打防止 = UI 即時 disable 用)
_SECTION_B_INFLIGHT_KEY = "_w228_section_b_inflight"

# Codex 2段指摘 (HIGH): session_state ロックは 1 セッション内しか効かず、別タブ /
# 別セッションから evaluate_product が並行起動 → Claude/Playwright 二重課金 +
# research_candidates 重複行。Streamlit プロセス共有の threading.Lock で
# プロセス全体を直列化する (module 単一インスタンス = 全セッション共有)。
_EVAL_PROCESS_LOCK = threading.Lock()

# セクション A の session_state key (前回の判定結果を引き継ぎ)
_GATE_DECISION_KEY = "_w228_gate_decision"
_GATE_TITLE_KEY = "_w228_gate_title"

# セクション C のデフォルト件数上限
_CANDIDATE_LIST_LIMIT = 100


# ============================================================================
# セクション A: 売れ行きゲート
# ============================================================================

def _render_section_a() -> None:
    """売れ行きゲート UI (手入力 → 5 分岐判定)."""
    st.markdown("### A. 売れ行きゲート")
    st.caption(
        "Terapeak Product Research で調べた数値を手入力してください。"
        "5 分岐の自動判定で仕入候補かどうかを判定します (仕様書 §2-A)。"
    )

    with st.form("w228_gate_form"):
        title_ja = st.text_input(
            "商品名 (日本語・型番等)",
            placeholder="例: Sony WH-1000XM5 ヘッドフォン",
            key="w228_gate_title_input",
            help="セクション B のフリマ探索にそのまま引き継ぎます。",
        )

        col1, col2 = st.columns(2)
        with col1:
            sold_90d = st.number_input(
                "直近 90 日 sold 数",
                min_value=0,
                step=1,
                value=0,
                key="w228_gate_sold90",
                help="Terapeak の 90-day sold 件数を入力してください。",
            )
            sold_1_2yr = st.number_input(
                "1〜2 年 sold 数",
                min_value=0,
                step=1,
                value=0,
                key="w228_gate_sold12yr",
                help="Terapeak の 1〜2 年 sold 件数を入力してください。",
            )
        with col2:
            has_active_listing = st.checkbox(
                "競合出品あり (has_active_listing)",
                value=False,
                key="w228_gate_has_active",
                help="現在 eBay で同一商品の active 出品が存在するか。",
            )
            listing_start_date = st.text_input(
                "最古の競合出品 開始年月 (YYYY-MM または YYYY-MM-DD)",
                value="",
                placeholder="例: 2025-03 または 2025-03-15",
                key="w228_gate_start_date",
                help=(
                    "出品ありの場合のみ入力してください。"
                    "空欄かつ出品ありの場合は保守的に skip_too_new 扱いになります (Q0)。"
                ),
            )

        submitted = st.form_submit_button("判定", type="primary")

    if not submitted:
        # 前回の判定結果が session_state にあれば再表示
        prev = st.session_state.get(_GATE_DECISION_KEY)
        if prev:
            _show_gate_result(prev["decision"], prev["reason"])
        return

    # ── 入力バリデーション ──────────────────────────────────────────────
    if not title_ja or not title_ja.strip():
        st.warning("商品名を入力してください。セクション B のフリマ探索で使用します。")
        # バリデーション失敗でも前回結果は保持 (session_state 変更なし)
        prev = st.session_state.get(_GATE_DECISION_KEY)
        if prev:
            _show_gate_result(prev["decision"], prev["reason"])
        return

    # ── ゲート判定 ──────────────────────────────────────────────────────
    decision, reason = evaluate_sourcing_gate(
        sold_90d=int(sold_90d),
        has_active_listing=bool(has_active_listing),
        listing_start_date=listing_start_date.strip() if listing_start_date else None,
        sold_1_2yr=int(sold_1_2yr),
        today=date.today(),
    )

    # 判定結果を session_state に保存 (セクション B 引き継ぎ用)
    st.session_state[_GATE_DECISION_KEY] = {"decision": decision, "reason": reason}
    st.session_state[_GATE_TITLE_KEY] = title_ja.strip()

    _show_gate_result(decision, reason)


def _show_gate_result(decision: str, reason: str) -> None:
    """判定結果を色分けして表示."""
    if decision == DECISION_TARGET_INSTOCK:
        st.success(f"判定: **対象 (在庫あり寄り)**\n\n{reason}")
    elif decision == DECISION_TARGET_OOS_WATCH:
        st.info(f"判定: **対象 (在庫0+監視)**\n\n{reason}")
    elif decision == DECISION_REJECT_DEADSTOCK:
        st.error(f"判定: **除外 (死に筋)**\n\n{reason}")
    elif decision == DECISION_SKIP_TOO_NEW:
        st.warning(f"判定: **スキップ (出品新しすぎ)**\n\n{reason}")
    elif decision == DECISION_REJECT_NO_DEMAND:
        st.error(f"判定: **除外 (需要なし)**\n\n{reason}")
    else:
        st.error(f"判定: **不明 ({decision})**\n\n{reason}")


# ============================================================================
# セクション B: フリマ探索 + AI 同一性 + 利益判定
# ============================================================================

def _render_section_b(config: dict) -> None:
    """フリマ探索 + AI 同一性 + 利益判定 UI.

    ゲート結果が target_* の時のみ表示。
    PoC: 出品 / 監視 / 購入ボタンは置かない。
    """
    st.markdown("### B. フリマ探索 + AI 同一性 + 利益判定")

    gate_state = st.session_state.get(_GATE_DECISION_KEY)
    if not gate_state:
        st.info("セクション A でゲート判定を実行してください。")
        return

    decision = gate_state["decision"]
    if decision not in {DECISION_TARGET_INSTOCK, DECISION_TARGET_OOS_WATCH}:
        st.info(
            f"ゲート判定 = **{decision}** のためフリマ探索は対象外です。"
            "セクション A で別の商品を判定してください。"
        )
        return

    prev_title = st.session_state.get(_GATE_TITLE_KEY, "")
    st.caption(
        f"ゲート判定: **{decision}** — フリマ探索で仕入候補を探します。"
        "AI は同一性スコアと理由を提示します。最終一致判定は人間が確認してください (§2-B)。"
    )

    title_ja = st.text_input(
        "商品名 (フリマ検索ワード)",
        value=prev_title,
        key="w228_sec_b_title",
        help="ゲートから引き継ぎ。必要に応じて修正可。",
    )
    col1, col2 = st.columns(2)
    with col1:
        manual_weight_g = st.number_input(
            "概算重量 (g)",
            min_value=0,
            step=50,
            value=0,
            key="w228_sec_b_weight",
            help="送料計算に使用。0 のまま実行すると利益計算が needs_review になります (P1-1)。",
        )
    with col2:
        terapeak_avg_usd = st.number_input(
            "Terapeak 平均売値 (USD)",
            min_value=0.0,
            step=1.0,
            value=0.0,
            format="%.2f",
            key="w228_sec_b_terapeak",
            help="Terapeak の Average sold price を入力してください。",
        )

    in_flight = st.session_state.get(_SECTION_B_INFLIGHT_KEY, False)

    # W225 事故教訓: form 内の st.button は禁止。form 外のボタンを使う。
    run_btn = st.button(
        "フリマ探索 + AI 同一性 + 利益判定",
        key="w228_sec_b_run",
        type="primary",
        disabled=in_flight,
        help="連打防止ロック付き。実行中は再クリック不可。",
    )

    if not run_btn:
        return

    if not title_ja or not title_ja.strip():
        st.warning("商品名を入力してください。")
        return

    if in_flight:
        st.warning("実行中です。完了まで待ってください。")
        return

    # ── プロセス全体ロック (Codex 2段 HIGH 反映): 別タブ/セッション間でも直列化 ──
    # non-blocking acquire。既に他セッションが探索中なら起動せず案内のみ
    # (Claude/Playwright 二重課金・重複行を防ぐ)。
    if not _EVAL_PROCESS_LOCK.acquire(blocking=False):
        st.warning(
            "別のセッション / タブで探索が実行中です。完了後に再実行してください。"
        )
        return

    # ── 同一セッション内 連打防止 (UI 即時 disable) ──────────────────────
    st.session_state[_SECTION_B_INFLIGHT_KEY] = True
    try:
        with st.spinner("フリマ探索 + AI 同一性判定 + 利益計算を実行中..."):
            from monitor.research_poc import evaluate_product
            result = evaluate_product(
                title_ja.strip(),
                manual_weight_g=float(manual_weight_g) if manual_weight_g and manual_weight_g > 0 else None,
                terapeak_avg_price_usd=float(terapeak_avg_usd) if terapeak_avg_usd and terapeak_avg_usd > 0 else None,
                settings=None,  # calculator.load_settings() に委ねる
            )
        _render_section_b_result(result)
    except ValueError as e:
        st.error(f"入力エラー: {e}")
        logger.warning(f"[w228_sec_b] ValueError: {e}")
    except Exception as e:
        st.error(f"探索エラー: {e}")
        logger.exception("[w228_sec_b] evaluate_product 例外")
    finally:
        st.session_state[_SECTION_B_INFLIGHT_KEY] = False
        _EVAL_PROCESS_LOCK.release()


def _render_section_b_result(result: dict) -> None:
    """evaluate_product の結果をカード形式で表示."""
    status = result.get("status", "unknown")
    rc_id = result.get("rc_id")
    match_score = result.get("match_score")
    match_reason = result.get("match_reason")
    profit_usd = result.get("estimated_profit_usd")
    needs_review_reason = result.get("needs_review_reason")
    found_url = result.get("found_url")
    found_price_jpy = result.get("found_price_jpy")
    search_errors = result.get("search_errors") or []
    hits_count = result.get("hits_count_total", 0)
    source_platform = result.get("source_platform")

    # ── ステータス別ヘッダー ────────────────────────────────────────────
    if status == STATUS_SOURCED:
        st.success(f"探索完了 (rc_id: {rc_id})")
    elif status == STATUS_NOT_FOUND:
        st.warning(f"フリマで見つかりませんでした (rc_id: {rc_id}、ヒット: {hits_count} 件)")
    elif status == STATUS_NEEDS_REVIEW:
        st.error(f"要確認 (rc_id: {rc_id})\n\n**理由**: {needs_review_reason}")
    else:
        st.info(f"status: {status} (rc_id: {rc_id})")

    if search_errors:
        st.warning("フリマ探索エラー:\n" + "\n".join(f"- {e}" for e in search_errors))

    # ── 詳細カード ──────────────────────────────────────────────────────
    if found_url or match_score is not None or profit_usd is not None:
        with st.container(border=True):
            st.markdown("**探索結果**")
            col1, col2 = st.columns(2)
            with col1:
                if found_url:
                    platform_label = source_platform or "フリマ"
                    price_str = f"¥{found_price_jpy:,}" if found_price_jpy else "価格不明"
                    st.markdown(f"仕入先候補: [{platform_label} — {price_str}]({found_url})")
                if match_score is not None:
                    if match_score >= 80:
                        score_label = f"一致度: **{match_score}** (高)"
                    elif match_score >= 60:
                        score_label = f"一致度: **{match_score}** (中)"
                    else:
                        score_label = f"一致度: **{match_score}** (低)"
                    st.markdown(score_label)
                if match_reason:
                    st.caption(f"AI 根拠: {match_reason}")
            with col2:
                if profit_usd is not None:
                    profit_color = "green" if profit_usd >= 0 else "red"
                    st.markdown(
                        f"利益見込み: "
                        f"<span style='color:{profit_color};font-weight:bold'>"
                        f"${profit_usd:+.2f}</span>",
                        unsafe_allow_html=True,
                    )
                elif needs_review_reason:
                    st.caption(f"利益計算不能: {needs_review_reason}")

            if needs_review_reason and status != STATUS_NEEDS_REVIEW:
                st.caption(f"要確認: {needs_review_reason}")

    # PoC 宣言: 出品 / 購入 / 監視ボタンは意図的に置かない (§8 P2)
    st.caption(
        "注: PoC のため出品・購入・監視登録ボタンは実装していません。"
        "最終一致判定 + 出品は別ステップ (W228 完全版) で対応します。"
    )


# ============================================================================
# セクション C: research_candidates 一覧
# ============================================================================

def _render_section_c() -> None:
    """research_candidates を st.dataframe で新しい順表示."""
    st.markdown("### C. リサーチ候補一覧")

    # status フィルタ
    all_statuses = [None, STATUS_NEW, STATUS_SOURCING, STATUS_SOURCED,
                    STATUS_NOT_FOUND, STATUS_NEEDS_REVIEW]
    status_labels = {
        None: "全て",
        STATUS_NEW: "new",
        STATUS_SOURCING: "sourcing",
        STATUS_SOURCED: "sourced",
        STATUS_NOT_FOUND: "not_found",
        STATUS_NEEDS_REVIEW: "needs_review",
    }
    selected_label = st.selectbox(
        "status フィルタ",
        options=list(status_labels.values()),
        index=0,
        key="w228_sec_c_status_filter",
    )
    # label → status 値に逆変換
    selected_status: Optional[str] = None
    for k, v in status_labels.items():
        if v == selected_label:
            selected_status = k
            break

    try:
        rows = list_research_candidates(status=selected_status, limit=_CANDIDATE_LIST_LIMIT)
    except Exception as e:
        st.error(f"一覧取得エラー: {e}")
        logger.exception("[w228_sec_c] list_research_candidates 例外")
        return

    if not rows:
        st.info("候補がありません。セクション B でフリマ探索を実行すると候補が追加されます。")
        return

    # DataFrame 化 (表示列を絞る)
    display_cols = [
        "rc_id", "title_ja", "status", "match_score",
        "estimated_profit_usd", "found_url", "found_price_jpy",
        "needs_review_reason", "created_at",
    ]
    df = pd.DataFrame(rows)
    # 存在しない列は空文字で補完 (スキーマ変更でも安全)
    for col in display_cols:
        if col not in df.columns:
            df[col] = None
    df = df[display_cols]

    # 列名を日本語ラベルに変換して表示
    col_rename = {
        "rc_id": "ID",
        "title_ja": "商品名",
        "status": "status",
        "match_score": "一致度",
        "estimated_profit_usd": "利益見込み(USD)",
        "found_url": "仕入先URL",
        "found_price_jpy": "仕入価格(JPY)",
        "needs_review_reason": "要確認理由",
        "created_at": "作成日時",
    }
    df = df.rename(columns=col_rename)

    st.caption(f"{len(rows)} 件表示 (上限 {_CANDIDATE_LIST_LIMIT} 件、新しい順)")
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "仕入先URL": st.column_config.LinkColumn("仕入先URL", display_text="リンク"),
            "利益見込み(USD)": st.column_config.NumberColumn(
                "利益見込み(USD)", format="$%.2f"
            ),
            "一致度": st.column_config.NumberColumn("一致度", format="%d"),
            "仕入価格(JPY)": st.column_config.NumberColumn("仕入価格(JPY)", format="¥%d"),
        },
    )


# ============================================================================
# Public API
# ============================================================================

def render_w228_research_tab(config: dict) -> None:
    """W228 商品リサーチ Wizard タブのエントリポイント.

    Args:
        config: schedule_config.json 内容 (credentials / settings)。
                セクション B の evaluate_product に間接的に使用する。
    """
    st.header("商品リサーチ (W228)")
    st.caption(
        "フェーズ B MVP PoC — Terapeak で発掘した商品をフリマ探索 → AI 同一性判定 → 利益判定。"
        "出品 / 購入 / 監視登録は PoC スコープ外 (別ステップ)。"
    )

    st.divider()
    _render_section_a()

    st.divider()
    _render_section_b(config)

    st.divider()
    _render_section_c()
