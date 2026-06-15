#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 商品リサーチ Wizard タブ — フェーズ B (Phase 1 FIX-1〜4 実装済).

仕様書: .company/engineering/docs/2026-06-07-product-research-automation-spec.md
設計書: .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md
ロック済み決定 (§7 / §8):
  - ゲートは当面手入力 (§8 P1-2)。
  - 同一商品 + 同状態の最終一致判定は人間 (§2-B)。AI は候補提示のみ。
  - 完全自動購入 / 最終 eBay 公開ボタン自動押下はしない。

Phase 1 FIX (2026-06-10):
  FIX-1: ゲート判定結果を session_state だけでなく research_candidates DB に永続化。
          再起動後も判定履歴が引ける。候補行 (source='manual') を FIX-1 で先行作成。
  FIX-3: needs_review (技術失敗) 候補に「再探索」ボタンを追加。

3 セクション構成:
  A: 売れ行きゲート (手入力 → 5 分岐判定、FIX-1 で DB 永続化)
  B: フリマ探索 + AI 同一性 + 利益判定 (ゲート target_* 時のみ表示)
  C: research_candidates 一覧 (status フィルタ付き、FIX-3 再探索ボタン)

K2 (外科的変更): 既存タブ (tab_product_management / tab_research_wizard 等) には一切触れない。
Q0 (サイレントスキップ禁止): フリマ探索エラーは偽成功を返さず st.error で表示。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from monitor.research_gate import (
    DECISION_REJECT_DEADSTOCK,
    DECISION_REJECT_GLOBAL_GLUT,
    DECISION_REJECT_NO_DEMAND,
    DECISION_SKIP_TOO_NEW,
    DECISION_TARGET_INSTOCK,
    DECISION_TARGET_OOS_WATCH,
    evaluate_sourcing_gate,
)
from monitor.research_candidates_db import (
    STATUS_APPROVED,
    STATUS_AWAITING_APPROVAL,
    STATUS_DRAFT_GENERATED,
    STATUS_GATE_PASSED,
    STATUS_GATE_REJECTED,
    STATUS_HARVESTED,
    STATUS_IDENTITY_APPROVED,
    STATUS_IDENTITY_REJECTED,
    STATUS_NEEDS_REVIEW,
    STATUS_NEW,
    STATUS_NOT_FOUND,
    STATUS_SOURCED,
    STATUS_SOURCING,
    STATUS_WATCH_REGISTERED,
    get_research_candidate,
    insert_research_candidate,
    list_research_candidates,
    record_listing_draft,
    record_watch_ids,
    save_gate_decision,
    update_status,
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
_GATE_RC_ID_KEY = "_w228_gate_rc_id"  # FIX-1: ゲート判定時に作成した rc_id

# セクション C のデフォルト件数上限
_CANDIDATE_LIST_LIMIT = 100

# セクション D: 承認ハンドラの連打防止ロック (module-level、プロセス全体共有)
_APPROVE_PROCESS_LOCK = threading.Lock()

# セクション D: rerun を跨いで結果メッセージを表示する flash key
_SEC_D_FLASH_KEY = "_w228_sec_d_flash"


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

    # FIX-1: ゲート判定を DB に永続化 (session_state だけでは再起動で消える)。
    # 候補行 (source='manual') を先行 INSERT → gate_decision を保存。
    # 同一商品名で既判定の rc_id がある場合も常に新行を作る
    # (同じ商品名を再調査するケースは別候補として管理するのが正しい)。
    try:
        inputs_snap = {
            "sold_90d": int(sold_90d),
            "has_active_listing": bool(has_active_listing),
            "listing_start_date": listing_start_date.strip() if listing_start_date else None,
            "sold_1_2yr": int(sold_1_2yr),
        }
        rc_id_for_gate = insert_research_candidate(title_ja=title_ja.strip())
        save_gate_decision(
            rc_id=rc_id_for_gate,
            decision=decision,
            reason=reason,
            inputs_dict=inputs_snap,
            move_status=True,
        )
        # session_state に rc_id も保存しておく (セクション B で引き継ぎ用)
        st.session_state[_GATE_RC_ID_KEY] = rc_id_for_gate
        logger.info(
            "[w228_sec_a] FIX-1 gate persisted: rc_id=%s decision=%s",
            rc_id_for_gate, decision,
        )
    except Exception as _e:
        # HIGH-1 (4巡目): DB 永続化失敗時に旧商品の rc_id が残ると、次回の探索が
        # 別商品の DB 行に着地する。即 pop して stale rc_id を破棄する。
        st.session_state.pop(_GATE_RC_ID_KEY, None)
        # DB 永続化失敗はエラー表示するが判定結果は表示継続 (UI 失敗防止)
        st.warning(f"ゲート判定の DB 保存に失敗しました (UI は継続): {_e}")
        logger.exception("[w228_sec_a] FIX-1 gate persistence failed")

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
    elif decision == DECISION_REJECT_GLOBAL_GLUT:
        # 依頼ボード#23 (2026-06-15): 全世界グラット除外
        st.error(f"判定: **除外 (全世界で出品過多・売れていない)**\n\n{reason}")
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

    # ── FIX-A: ゲート経由の rc_id を引き継ぐ ────────────────────────────────
    # _GATE_RC_ID_KEY が存在し、かつ入力中の title_ja がゲート時と一致する場合のみ
    # rc_id を渡す (商品名を書き換えた場合は別商品 → 新規行)。
    # pop しない (2026-06-10 Q1 実機 rc_id=6 で発覚): 1 回目が入力不備等で
    # needs_review に落ちた後のリトライでも同じ gate 行を更新する。pop-once だと
    # リトライが gate 連携なしの新行に分裂し、Phase 4 承認キューの前提
    # (gate 判定と利益データの同一行同居) が崩れる。誤着地は title 一致ガード
    # (本 if) + evaluate_product 側の DB title 照合 (HIGH-1) の 2 層で防御済み。
    gate_rc_id: Optional[int] = None
    gate_title = st.session_state.get(_GATE_TITLE_KEY, "")
    if (
        _GATE_RC_ID_KEY in st.session_state
        and gate_title
        and title_ja.strip() == gate_title.strip()
    ):
        gate_rc_id = st.session_state[_GATE_RC_ID_KEY]
        logger.info(
            "[w228_sec_b] FIX-A: gate rc_id=%s を引き継ぎ (title=%r)",
            gate_rc_id, title_ja.strip(),
        )

    # ── 同一セッション内 連打防止 (UI 即時 disable) ──────────────────────
    st.session_state[_SECTION_B_INFLIGHT_KEY] = True
    try:
        with st.spinner("フリマ探索 + AI 同一性判定 + 利益計算を実行中..."):
            from monitor.research_poc import evaluate_product
            result = evaluate_product(
                title_ja.strip(),
                rc_id=gate_rc_id,  # FIX-A: gate 経由なら既存行を再利用
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
    # FIX-D: 真値利益 + けいすけ基準を取得
    profit_jpy_true: Optional[int] = result.get("profit_jpy_true")
    keisuke_detail: Optional[dict] = result.get("keisuke_detail")
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
                # FIX-D: 利益真値 (円) を優先表示
                if profit_jpy_true is not None:
                    profit_color = "green" if profit_jpy_true >= 0 else "red"
                    st.markdown(
                        f"利益真値: "
                        f"<span style='color:{profit_color};font-weight:bold'>"
                        f"¥{profit_jpy_true:,}</span>",
                        unsafe_allow_html=True,
                    )
                elif profit_usd is not None:
                    profit_color = "green" if profit_usd >= 0 else "red"
                    st.markdown(
                        f"利益見込み: "
                        f"<span style='color:{profit_color};font-weight:bold'>"
                        f"${profit_usd:+.2f}</span>",
                        unsafe_allow_html=True,
                    )
                elif needs_review_reason:
                    st.caption(f"利益計算不能: {needs_review_reason}")

                # FIX-D: けいすけ基準の表示 (自動ブロックなし、表示のみ)
                if keisuke_detail is not None:
                    keisuke_pass = keisuke_detail.get("pass")
                    profit_rate = keisuke_detail.get("profit_rate")
                    revenue_jpy = keisuke_detail.get("revenue_jpy")
                    if keisuke_pass is True:
                        keisuke_label = "合格"
                        keisuke_color = "green"
                    elif keisuke_pass is False:
                        keisuke_label = "不合格"
                        keisuke_color = "red"
                    else:
                        keisuke_label = "未判定"
                        keisuke_color = "gray"
                    rate_str = (
                        f"{profit_rate * 100:.1f}%" if profit_rate is not None else "—"
                    )
                    revenue_str = (
                        f"¥{revenue_jpy:,.0f}" if revenue_jpy is not None else "—"
                    )
                    st.markdown(
                        f"けいすけ基準: "
                        f"<span style='color:{keisuke_color};font-weight:bold'>"
                        f"{keisuke_label}</span>"
                        f" (利益率: {rate_str} / 売上: {revenue_str})",
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption("けいすけ基準: 未判定 (利益計算不能)")

            if needs_review_reason and status != STATUS_NEEDS_REVIEW:
                st.caption(f"要確認: {needs_review_reason}")

    # PoC 宣言: 出品 / 購入 / 監視ボタンは意図的に置かない (§8 P2)
    st.caption(
        "注: PoC のため出品・購入・監視登録ボタンは実装していません。"
        "最終一致判定 + 出品は別ステップ (W228 完全版) で対応します。"
    )


# ============================================================================
# セクション C: research_candidates 一覧 + 承認・watch 登録 UI
# ============================================================================

# メルカリ / ヤフオク 検索 URL ビルダー
def _mercari_search_url(keyword: str) -> str:
    from urllib.parse import quote_plus
    return "https://jp.mercari.com/search?keyword=" + quote_plus(keyword)


def _yahoo_auctions_search_url(keyword: str) -> str:
    from urllib.parse import quote_plus
    return "https://auctions.yahoo.co.jp/search/search?p=" + quote_plus(keyword)


def _calc_price_max_jpy(
    found_price_jpy: Optional[int],
    estimated_profit_usd: Optional[float],
) -> Optional[int]:
    """利益が出る上限仕入価格 (JPY) を算出する.

    estimated_profit_usd は「(terapeak 平均 - breakeven) USD」なので、
    現在の found_price_jpy で breakeven から profit_usd 分の余裕がある。
    上限 = found_price_jpy (今の価格が限界として最も保守的)。

    estimated_profit_usd が正ならその分だけ仕入価格に余裕がある。
    USD → JPY は設定レートを使わず UI で編集可能にするため、ここでは
    found_price_jpy を返すだけ (0 以下なら None)。
    """
    if not found_price_jpy or found_price_jpy <= 0:
        return None
    # Codex 2段指摘#3: estimated_profit_usd が None/≤0 (= 現在価格で損 or 利益未検証、
    # needs_review からも承認可能) の候補に found_price をそのまま上限にすると、
    # 損失価格での通知 → 過大仕入を招く。安全な自動上限を出せないので None を返し、
    # UI 側で「利益が出る上限価格の手動入力」を必須にする。
    if estimated_profit_usd is None or estimated_profit_usd <= 0:
        return None
    return found_price_jpy


def _render_candidate_actions(row: dict) -> None:
    """1 候補の承認 / 却下 / watch 登録ボタン群 (form 外、W225 作法)."""
    rc_id = row["rc_id"]
    status = row.get("status", "")
    title_ja = row.get("title_ja", f"rc_id={rc_id}")
    found_price_jpy: Optional[int] = row.get("found_price_jpy")
    estimated_profit_usd: Optional[float] = row.get("estimated_profit_usd")

    # ── 承認 / 却下ボタン (sourced / needs_review → identity_approved/rejected) ──
    can_approve = status in {STATUS_SOURCED, STATUS_NEEDS_REVIEW}
    can_reject = status in {STATUS_SOURCED, STATUS_NEEDS_REVIEW}

    col_approve, col_reject, col_watch = st.columns([1, 1, 2])

    with col_approve:
        if can_approve:
            if st.button(
                "同一性OK",
                key=f"w228_approve_{rc_id}",
                help="人間が同一商品と確認した場合に押す。",
            ):
                try:
                    update_status(rc_id, STATUS_IDENTITY_APPROVED)
                    st.success(f"rc_id={rc_id} を identity_approved に更新しました。")
                    st.rerun()
                except Exception as e:
                    st.error(f"承認失敗 (rc_id={rc_id}): {e}")
                    logger.exception("[w228_sec_c] approve 例外 rc_id=%s", rc_id)

    with col_reject:
        if can_reject:
            if st.button(
                "却下",
                key=f"w228_reject_{rc_id}",
                help="同一商品でないと判断した場合に押す。",
            ):
                try:
                    update_status(rc_id, STATUS_IDENTITY_REJECTED)
                    st.info(f"rc_id={rc_id} を identity_rejected に更新しました。")
                    st.rerun()
                except Exception as e:
                    st.error(f"却下失敗 (rc_id={rc_id}): {e}")
                    logger.exception("[w228_sec_c] reject 例外 rc_id=%s", rc_id)

    # ── FIX-3: 再探索ボタン (needs_review のみ表示) ──────────────────────────
    # needs_review = 技術失敗 (仕入先 fetch エラー等)。
    # 連打防止は _EVAL_PROCESS_LOCK (プロセス全体共有の threading.Lock)。
    with col_watch:
        if status == STATUS_NEEDS_REVIEW:
            if st.button(
                "再探索",
                key=f"w228_retry_{rc_id}",
                help=(
                    "技術的エラーで探索できなかった候補を needs_review → sourcing に戻し、"
                    "次回の自動探索サイクルで再試行します。"
                ),
                disabled=_EVAL_PROCESS_LOCK.locked(),
            ):
                from monitor.research_poc import retry_sourcing  # lazy import (circular 回避)
                try:
                    ok = retry_sourcing(rc_id)
                    if ok:
                        st.success(
                            f"rc_id={rc_id} を sourcing に戻しました。"
                            "次回の自動探索で再試行されます。"
                        )
                        st.rerun()
                    else:
                        st.warning(
                            f"rc_id={rc_id} の再探索リセットに失敗しました "
                            "(既に状態が変わったか、該当 ID が存在しません)。"
                        )
                except Exception as _re:
                    st.error(f"再探索失敗 (rc_id={rc_id}): {_re}")
                    logger.exception("[w228_sec_c] retry_sourcing 例外 rc_id=%s", rc_id)

    # ── キーワード新着監視登録ボタン (identity_approved のみ表示) ──────────
    with col_watch:
        if status == STATUS_IDENTITY_APPROVED:
            # 上限仕入価格の初期値 (UI で編集可)
            default_price_max = _calc_price_max_jpy(found_price_jpy, estimated_profit_usd)
            # Codex#3: 利益 None/≤0 (損 or 未検証) は安全な自動上限が出せない → 警告 + 手動必須
            _unprofitable = (estimated_profit_usd is None or estimated_profit_usd <= 0)
            if _unprofitable:
                st.warning(
                    "この候補は現在価格で利益が出ない / 利益未検証です。"
                    "利益が出る上限仕入価格を手動入力してから登録してください "
                    "(0 のままの無制限監視は不可)。"
                )
            price_max_input = st.number_input(
                "上限仕入価格 (JPY)",
                min_value=0,
                value=default_price_max if default_price_max else 0,
                step=500,
                key=f"w228_price_max_{rc_id}",
                help=(
                    "この価格以下で出品されたとき Discord 通知します。"
                    "0 の場合は全件通知 (price_max=None)。"
                ),
            )
            if st.button(
                "キーワード新着監視に登録",
                key=f"w228_watch_{rc_id}",
                help="メルカリ・ヤフオクにキーワード監視を登録します。",
            ):
                _pm = (
                    int(price_max_input)
                    if price_max_input and price_max_input > 0 else None
                )
                # Codex#3: 損/未検証候補を上限なし(全件通知)で監視登録させない
                if _unprofitable and _pm is None:
                    st.error(
                        "利益が出る上限仕入価格 (>0) を入力してください "
                        "(損失/未検証候補の無制限監視は不可)。"
                    )
                else:
                    _register_keyword_watch(
                        rc_id=rc_id, title_ja=title_ja, price_max_jpy=_pm,
                    )

        elif status == STATUS_WATCH_REGISTERED:
            st.caption("監視登録済")


def _register_keyword_watch(
    rc_id: int,
    title_ja: str,
    price_max_jpy: Optional[int],
) -> None:
    """メルカリ・ヤフオク の 2 サイトにキーワード監視を登録し status を watch_registered に遷移."""
    from monitor.keyword_watch_db import add_watch
    from monitor.research_candidates_db import update_status as _update_status

    keyword = title_ja.strip()
    sites = [
        ("mercari", _mercari_search_url(keyword)),
        ("yahoo_auctions", _yahoo_auctions_search_url(keyword)),
    ]

    registered_any = False
    for site, search_url in sites:
        try:
            watch_id, is_new = add_watch(
                site=site,
                search_url=search_url,
                keyword=keyword,
                price_max_jpy=price_max_jpy,
                memo=f"W228 research rc_id={rc_id}",
                source="w228_research",
            )
            if is_new:
                logger.info(
                    "[w228_sec_c] watch 登録: rc_id=%s site=%s watch_id=%s",
                    rc_id, site, watch_id,
                )
                registered_any = True
            else:
                # Codex 2段指摘#2: 既存 watch (UNIQUE(site,search_url) 衝突) は add_watch が
                # 既存行を返すだけで price_max を更新しない。古い/None の上限が残ると
                # 過大仕入通知になるため、今回の意図値で既存 watch の price_max を更新する。
                try:
                    from monitor.keyword_watch_db import update_watch
                    update_watch(watch_id, price_max_jpy=price_max_jpy)
                except Exception as _ue:
                    logger.warning(
                        "[w228_sec_c] 既存watch price_max更新失敗 wid=%s: %s",
                        watch_id, _ue,
                    )
                logger.info(
                    "[w228_sec_c] watch 既存(price_max更新): rc_id=%s site=%s watch_id=%s",
                    rc_id, site, watch_id,
                )
        except Exception as e:
            st.error(f"watch 登録失敗 ({site}): {e}")
            logger.exception("[w228_sec_c] add_watch 例外 rc_id=%s site=%s", rc_id, site)
            return  # 登録失敗したら status 遷移しない (Q0 偽装成功禁止)

    # 全サイト登録 (or 既存) 成功 → status を watch_registered に遷移
    try:
        _update_status(rc_id, STATUS_WATCH_REGISTERED)
        if registered_any:
            st.success(
                f"rc_id={rc_id} 「{keyword}」をメルカリ・ヤフオクに監視登録しました。"
                f" 上限価格: {'¥{:,}'.format(price_max_jpy) if price_max_jpy else '制限なし'}"
            )
        else:
            st.info(
                f"rc_id={rc_id} は既存 watch と同一URL のため新規登録なし (重複防止)。"
                "status を watch_registered に更新しました。"
            )
        st.rerun()
    except Exception as e:
        st.error(f"status 更新失敗 (rc_id={rc_id}): {e}")
        logger.exception("[w228_sec_c] update_status watch_registered 例外 rc_id=%s", rc_id)


def _render_section_c() -> None:
    """research_candidates を表示 + 行ごとに承認 / 却下 / watch 登録ボタン."""
    st.markdown("### C. リサーチ候補一覧")

    # status フィルタ (承認系 status も選択可)
    # FIX-D: 新 status 5 つを追加 (W229 ハーベスト + 承認キュー経路)
    status_labels: dict[Optional[str], str] = {
        None: "全て",
        STATUS_NEW: "new",
        STATUS_HARVESTED: "発掘済 (判定待ち)",         # FIX-D
        STATUS_GATE_PASSED: "ゲート通過 (探索待ち)",   # FIX-D
        STATUS_GATE_REJECTED: "ゲート除外",             # FIX-D
        STATUS_SOURCING: "sourcing",
        STATUS_SOURCED: "sourced",
        STATUS_NOT_FOUND: "not_found",
        STATUS_NEEDS_REVIEW: "needs_review",
        STATUS_IDENTITY_APPROVED: "identity_approved",
        STATUS_IDENTITY_REJECTED: "identity_rejected",
        STATUS_AWAITING_APPROVAL: "承認待ち",           # FIX-D
        STATUS_APPROVED: "承認済",                       # FIX-D
        STATUS_WATCH_REGISTERED: "watch_registered",
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

    # サマリー DataFrame (全件)
    display_cols = [
        "rc_id", "title_ja", "status", "match_score",
        "estimated_profit_usd", "found_url", "found_price_jpy",
        "needs_review_reason", "created_at",
    ]
    df = pd.DataFrame(rows)
    for col in display_cols:
        if col not in df.columns:
            df[col] = None
    df = df[display_cols]

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

    # ── 行ごとの承認 / 却下 / watch 登録 UI ──────────────────────────────
    st.markdown("#### 候補アクション")
    st.caption(
        "sourced / needs_review 状態の候補に「同一性OK / 却下」ボタンが表示されます。"
        "identity_approved になった候補はキーワード新着監視に登録できます。"
    )

    actionable_statuses = {
        STATUS_SOURCED, STATUS_NEEDS_REVIEW,
        STATUS_IDENTITY_APPROVED, STATUS_WATCH_REGISTERED,
    }
    for row in rows:
        if row.get("status") not in actionable_statuses:
            continue
        with st.container(border=True):
            rc_id = row["rc_id"]
            title = row.get("title_ja", f"rc_id={rc_id}")
            status = row.get("status", "")
            profit = row.get("estimated_profit_usd")
            price_jpy = row.get("found_price_jpy")
            found_url = row.get("found_url")

            # ヘッダ行
            header_parts = [f"**rc_id={rc_id}** — {title}", f"status: `{status}`"]
            if profit is not None:
                header_parts.append(f"利益見込み: ${profit:+.2f}")
            if price_jpy:
                header_parts.append(f"仕入価格: ¥{price_jpy:,}")
            if found_url:
                header_parts.append(f"[仕入先リンク]({found_url})")
            st.markdown("  |  ".join(header_parts))

            _render_candidate_actions(row)


# ============================================================================
# セクション D: 承認キュー (Phase 4)
# ============================================================================

def _count_oos_active_listings() -> int:
    """W228 リサーチ経由で出品された listing のうち active なものの数を返す.

    在庫0上限ガード (P0-3) の比較元として使用。
    設計書 §7-5 の定義: research_candidates.status='listed' かつ
    result_ebay_item_id が紐づく ebay_listings が active なもの。
    listing 識別は ebay_item_id (SKU 規約準拠)。
    現状 listed 行は 0 件なのでガードは通過する (正常)。
    """
    from monitor.database import get_conn

    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) "
            "FROM research_candidates rc "
            "JOIN ebay_listings el ON el.ebay_item_id = rc.result_ebay_item_id "
            "WHERE rc.status = 'listed' "
            "  AND rc.result_ebay_item_id IS NOT NULL "
            "  AND (el.is_ended IS NULL OR el.is_ended = 0)",
        ).fetchone()
    return int(row[0]) if row else 0


def _load_max_oos_limit() -> int:
    """schedule_config.json から max_oos_active_listings を読む.

    読み取り失敗 / キー不在時は 20 をフォールバックとして返す。
    silent 化しないため失敗時は logger.warning を出す。
    """
    cfg_path = Path(__file__).resolve().parent.parent / 'config' / 'schedule_config.json'
    try:
        with cfg_path.open(encoding='utf-8') as f:
            cfg = json.load(f)
        return int(
            cfg.get('tasks_enabled', {})
            .get('research_harvest', {})
            .get('max_oos_active_listings', 20)
        )
    except Exception as e:
        logger.warning(
            '[w228_sec_d] schedule_config.json から max_oos_active_listings 読み取り失敗 '
            '(fallback=20): %s', e,
        )
        return 20


def _compute_target_buy_jpy(rc: Optional[dict]) -> tuple[Optional[int], Optional[str]]:
    """W262: rc 行から損益分岐仕入価格 (けいすけ基準 PASS 上限、円) を逆算.

    例外は (None, reason) に握って caller の fallback (found_price_jpy) に倒す
    (watch 登録自体を逆算失敗で止めない)。
    """
    if not rc:
        return None, 'rc 未提供'
    try:
        from monitor.research_poc import compute_max_purchase_jpy
        return compute_max_purchase_jpy(
            terapeak_avg_price_usd=rc.get('terapeak_avg_price_usd'),
            manual_weight_g=rc.get('manual_weight_g'),
            length_cm=rc.get('length_cm'),
            width_cm=rc.get('width_cm'),
            height_cm=rc.get('height_cm'),
        )
    except Exception as e:
        logger.warning(
            '[w228_sec_d] 損益分岐仕入価格 逆算失敗 rc_id=%s: %s', rc.get('rc_id'), e
        )
        return None, f'逆算例外: {type(e).__name__}: {e}'


def _get_max_purchase_for_rc(rc: dict) -> tuple[Optional[int], Optional[str]]:
    """W262: Section D 表示用の損益分岐仕入価格 (session_state cache 付き).

    逆算は calculator.calculate を ~25 回呼ぶため、rerun 毎の再計算を避ける。
    cache key に入力値 (terapeak/weight) を含め、値の更新時は自動で再計算する。
    """
    cache_key = (
        f'_w228_max_buy_{rc.get("rc_id")}'
        f'_{rc.get("terapeak_avg_price_usd")}_{rc.get("manual_weight_g")}'
        f'_{rc.get("length_cm")}_{rc.get("width_cm")}_{rc.get("height_cm")}'
    )
    cached = st.session_state.get(cache_key)
    if cached is not None:
        return cached
    res = _compute_target_buy_jpy(rc)
    st.session_state[cache_key] = res
    return res


def _run_watch_only_approval(
    rc_id: int,
    title_ja: str,
    found_price_jpy: Optional[int],
    max_oos: int,
    result: dict,
    rc: Optional[dict] = None,
) -> dict:
    """found_url 無し (監視候補) の承認: keyword watch 登録のみ実施.

    not_found → awaiting_approval 再キュー行 (在庫0 + 過去取引あり) は仕入先 URL を
    持たない (誤マッチ URL は clear_found_fields で NULL 化 / retrospective H1)。
    description / draft 生成は不可能かつ不要のため跳ばし、watch 登録成功で
    watch_registered 終端に遷移する。watch 登録が唯一の目的のため、失敗・上限超過は
    needs_review に戻して可視化する (silent 終端禁止 / Q0)。

    W262: price_max_jpy は誤マッチ品の found_price_jpy ではなく、けいすけ基準 PASS が
    成立する損益分岐仕入価格 (rc から逆算) を優先する。逆算不能時のみ found_price_jpy
    に fallback。
    """
    # 在庫0上限ガード (P0-3、通常経路と同一基準)
    try:
        current_oos_count = _count_oos_active_listings()
    except Exception as e:
        logger.error('[w228_sec_d] OOS count 失敗 rc_id=%s: %s', rc_id, e)
        current_oos_count = 0  # 取得失敗は保守的に 0 として続行 (watch 登録の機会は守る)

    if current_oos_count >= max_oos:
        result['watch_skipped_oos_limit'] = True
        reason = (
            f'在庫0上限 {max_oos} 件超過で watch 未登録 '
            f'(現在 {current_oos_count} 件) — 手動登録要'
        )
        try:
            update_status(
                rc_id, STATUS_NEEDS_REVIEW,
                needs_review_reason=f'[watch-only 承認] {reason}',
            )
        except ValueError as e:
            logger.error('[w228_sec_d] needs_review 遷移失敗 rc_id=%s: %s', rc_id, e)
        result['needs_review_fallen'] = True
        result['message'] = f'監視候補ですが{reason}。needs_review に戻しました。'
        return result

    # ── W262: 損益分岐仕入価格を watch 上限価格に採用 ────────────────────
    target_buy_jpy, _target_reason = _compute_target_buy_jpy(rc)
    # H-1: Section232 該当は逆算に実関税が未反映 (calculator legacy washing) のため
    # 上限が過大になり得る → memo/message に注記伝播 (自動 BLOCK はしない = 規約準拠)
    _s232 = bool(rc.get('section232_flag')) if rc else False
    price_max = target_buy_jpy if target_buy_jpy is not None else found_price_jpy
    if target_buy_jpy is not None:
        memo_price = f' 上限¥{target_buy_jpy:,}=損益分岐仕入価格'
        if _s232:
            memo_price += ' (Section232関税未反映・実際の上限はこれより低い)'
    elif found_price_jpy is not None:
        # M-2: 由来の正のラベル (損益分岐とは書かない = 虚偽ラベル禁止)
        memo_price = f' 上限¥{found_price_jpy:,}=候補品価格(参考)'
        if _s232:
            memo_price += ' (Section232該当候補・関税注意)'
    else:
        memo_price = ''
    result['price_max_jpy'] = price_max

    from monitor.keyword_watch_db import add_watch
    keyword = title_ja.strip()
    sites = [
        ('mercari', _mercari_search_url(keyword)),
        ('yahoo_auctions', _yahoo_auctions_search_url(keyword)),
    ]
    watch_ids_registered: list[int] = []
    for site, search_url in sites:
        try:
            watch_id, _is_new = add_watch(
                site=site,
                search_url=search_url,
                keyword=keyword,
                price_max_jpy=price_max,
                memo=f'W228 research rc_id={rc_id} (監視候補承認){memo_price}',
                source='w228_research',
            )
            watch_ids_registered.append(watch_id)
            logger.info(
                '[w228_sec_d] watch-only 登録 rc_id=%s site=%s watch_id=%s',
                rc_id, site, watch_id,
            )
        except Exception as e:
            logger.exception(
                '[w228_sec_d] add_watch 失敗 (watch-only) rc_id=%s site=%s', rc_id, site
            )
            # M-1: 部分成功分 (登録済 watch) の対応関係を先に記録 (orphan watch 痕跡保全)
            if watch_ids_registered:
                try:
                    record_watch_ids(rc_id, watch_ids_registered)
                except Exception as e3:
                    logger.error('[w228_sec_d] record_watch_ids 失敗 rc_id=%s: %s', rc_id, e3)
            try:
                update_status(
                    rc_id, STATUS_NEEDS_REVIEW,
                    needs_review_reason=f'[watch-only 承認] {site} watch 登録失敗: {e}',
                )
            except ValueError as e2:
                logger.error('[w228_sec_d] needs_review 遷移も失敗 rc_id=%s: %s', rc_id, e2)
            result['needs_review_fallen'] = True
            result['watch_registered'] = bool(watch_ids_registered)
            result['watch_ids'] = watch_ids_registered
            result['message'] = (
                f'{site} の watch 登録に失敗したため needs_review に戻しました: {e}'
            )
            return result

    if watch_ids_registered:
        try:
            record_watch_ids(rc_id, watch_ids_registered)
        except Exception as e:
            logger.error('[w228_sec_d] record_watch_ids 失敗 rc_id=%s: %s', rc_id, e)

    try:
        update_status(rc_id, STATUS_WATCH_REGISTERED)
    except ValueError as e:
        # watch 自体は登録済 (実害なし)。遷移失敗だけ log + message で痕跡
        logger.error('[w228_sec_d] watch_registered 遷移失敗 rc_id=%s: %s', rc_id, e)

    result['success'] = True
    result['watch_registered'] = True
    result['watch_ids'] = watch_ids_registered
    if target_buy_jpy is not None:
        price_note = (
            f'上限価格 ¥{target_buy_jpy:,} (損益分岐仕入価格 = この価格以下で出品されれば'
            'けいすけ基準 PASS) で'
        )
        if _s232:
            price_note += (
                '【注意: Section232 関税は逆算に未反映 — 実際の上限はこれより低い】'
            )
    elif price_max is not None:
        price_note = f'上限価格 ¥{price_max:,} (候補品価格・参考) で'
        if _s232:
            price_note += '【Section232該当候補・関税注意】'
    else:
        price_note = '上限価格なしで'
    result['message'] = (
        f'監視候補として keyword watch を {len(watch_ids_registered)} サイトに'
        f'{price_note}登録しました '
        '(仕入先未発見のため下書きは生成していません。新着検知をお待ちください)。'
    )
    return result


def _run_approval_logic(
    rc_id: int,
    rc: dict,
    config: dict,
    max_oos_limit: Optional[int] = None,
) -> dict:
    """承認ボタン押下のロジック層 (UI 非依存、テスト可能).

    設計書 §7-4 の 6 step を実装:
      1. update_status → approved
      2. W226 description 生成 (同期)
      3. save_listing_draft → draft_id
      4. 個別出品 prefill (il_* session_state へ積む)
      5. update_status → draft_generated + listing_draft_id 記録
      6. 在庫0上限ガード → keyword watch 登録 (P0-3)

    Args:
        rc_id: research_candidates の行 ID。
        rc: research_candidates の行データ辞書。
        config: app.py から渡される計算設定辞書 (max_oos 読み取りには不使用)。
        max_oos_limit: テスト用上限値の直接 override。None 時は schedule_config.json を参照。

    Returns:
        {
            'success': bool,
            'draft_id': int or None,
            'watch_registered': bool,
            'watch_skipped_oos_limit': bool,   # True = 上限超過でスキップ
            'watch_ids': list[int],
            'needs_review_fallen': bool,        # True = step 2/5 失敗で needs_review に落ちた
            'message': str,
        }
    UI 層は本関数の戻り値を見て st.success / st.error / st.warning を出す。
    """
    result: dict = {
        'success': False,
        'draft_id': None,
        'watch_registered': False,
        'watch_skipped_oos_limit': False,
        'watch_ids': [],
        'needs_review_fallen': False,
        'message': '',
    }

    title_ja = rc.get('title_ja') or f'rc_id={rc_id}'
    found_url = rc.get('found_url') or ''
    manual_weight_g = rc.get('manual_weight_g')
    found_price_jpy = rc.get('found_price_jpy')
    max_oos = max_oos_limit if max_oos_limit is not None else _load_max_oos_limit()

    # ── Step 1: approved に遷移 ───────────────────────────────────────────
    try:
        ok = update_status(rc_id, STATUS_APPROVED)
        if not ok:
            result['message'] = f'rc_id={rc_id} の状態遷移に失敗しました (既に変更済みの可能性)。'
            return result
    except ValueError as e:
        result['message'] = f'状態遷移エラー: {e}'
        return result

    # ── Step 1.5: found_url 無し (監視候補) は watch 登録のみ ─────────────
    # not_found 再キュー行は仕入先 URL を持たない (retrospective H1 で誤マッチ
    # URL は clear_found_fields により NULL 化)。description 生成に進むと必ず
    # 失敗 → needs_review 降格し watch 登録 (本経路の唯一の目的) に到達できない
    # ため、ここで分岐する (2026-06-12 code-review H-1)。
    if not found_url:
        return _run_watch_only_approval(
            rc_id, title_ja, found_price_jpy, max_oos, result, rc=rc
        )

    # ── Step 2: W226 description 生成 ────────────────────────────────────
    # rank_override_code は None = auto-classify に任せる。
    # found_condition_ja はフリマ状態表記 (「美品」等) であり rank code ではない。
    gen_result: dict = {'success': False, 'description_html': '', 'rank_code': '', 'title_en': ''}
    try:
        from tabs._supplier_description_pipeline import generate_supplier_description
        gen_result = generate_supplier_description(
            candidate_id=rc_id,
            candidate_url=found_url,
            in_stock=False,  # 承認候補は全件無在庫前提
            rank_override_code=None,
        )
    except Exception as e:
        logger.exception('[w228_sec_d] generate_supplier_description failed rc_id=%s', rc_id)
        gen_result = {
            'success': False,
            'message': f'description 生成例外: {type(e).__name__}: {e}',
            'description_html': '', 'rank_code': '', 'title_en': '',
        }

    if not gen_result.get('success'):
        # 生成失敗 → needs_review に降格、理由を記録
        reason = gen_result.get('message') or 'description 生成失敗 (詳細不明)'
        try:
            update_status(
                rc_id, STATUS_NEEDS_REVIEW,
                needs_review_reason=f'[Phase4 description 生成失敗] {reason}',
            )
        except ValueError as e2:
            logger.error('[w228_sec_d] needs_review 遷移も失敗 rc_id=%s: %s', rc_id, e2)
        result['needs_review_fallen'] = True
        result['message'] = f'description 生成に失敗したため needs_review に戻しました。理由: {reason}'
        return result

    used_rank = gen_result.get('rank_code') or ''
    title_en = gen_result.get('title_en') or ''
    description_html = gen_result.get('description_html') or ''

    # ── Step 3: listing_drafts に下書き保存 ──────────────────────────────
    draft_data: dict = {
        'supplier_url': found_url,
        'supplier_title_ja': title_ja,
        'supplier_price_jpy': found_price_jpy,
        'rank_code': used_rank,
        'ebay_title': title_en,
        'ebay_description': description_html,
        'weight_g': int(manual_weight_g) if manual_weight_g is not None else None,
        'in_stock': 0,  # 無在庫
        'status': 'draft',
    }
    try:
        from monitor.database import save_listing_draft
        draft_id = save_listing_draft(draft_data)
    except Exception as e:
        logger.exception('[w228_sec_d] save_listing_draft failed rc_id=%s', rc_id)
        reason = f'listing_drafts 保存失敗: {type(e).__name__}: {e}'
        try:
            update_status(
                rc_id, STATUS_NEEDS_REVIEW,
                needs_review_reason=f'[Phase4 draft 保存失敗] {reason}',
            )
        except ValueError as e2:
            logger.error('[w228_sec_d] needs_review 遷移も失敗 rc_id=%s: %s', rc_id, e2)
        result['needs_review_fallen'] = True
        result['message'] = f'下書き保存に失敗したため needs_review に戻しました。理由: {reason}'
        return result

    result['draft_id'] = draft_id

    # ── Step 4: 個別出品タブへ prefill (W176 正規経路) ────────────────────
    # il_* state key 直書きは widget key (il_input_supplier_url) に反映されず、
    # さらに URL 差分検知 (_clear_from_step) で prefill が全消去される。
    # 保存済みドラフト読込と同じ _load_draft_into_form を呼び、pending seed /
    # scraped_product / generated_listing / current_draft_id を一括復元する
    # (tab_individual_listing.py の「保存済みドラフト」読込ボタンと同型)。
    try:
        from monitor.database import get_listing_draft
        from tabs.tab_individual_listing import _load_draft_into_form
        _full_draft = get_listing_draft(int(draft_id))
        if _full_draft:
            _load_draft_into_form(_full_draft)
    except Exception:
        # prefill 失敗でも draft は保存済みのためフローは止めない (痕跡は log)
        logger.exception(
            '[w228_sec_d] 個別出品 prefill 失敗 rc_id=%s draft_id=%s', rc_id, draft_id
        )

    # ── Step 5: draft_generated に遷移 + listing_draft_id 記録 ───────────
    try:
        update_status(rc_id, STATUS_DRAFT_GENERATED)
        record_listing_draft(rc_id, draft_id)
    except Exception as e:
        logger.error('[w228_sec_d] draft_generated 遷移失敗 rc_id=%s: %s', rc_id, e)
        reason = f'draft_generated 遷移失敗: {e}'
        try:
            update_status(
                rc_id, STATUS_NEEDS_REVIEW,
                needs_review_reason=f'[Phase4 status 更新失敗] {reason}',
            )
        except ValueError as e2:
            logger.error('[w228_sec_d] needs_review 遷移も失敗 rc_id=%s: %s', rc_id, e2)
        result['needs_review_fallen'] = True
        result['message'] = (
            f'下書き生成後の status 更新に失敗し needs_review に戻しました。'
            f'下書き #{draft_id} は listing_drafts に保存済みです。理由: {reason}'
        )
        return result

    # ── Step 6: 在庫0上限ガード + keyword watch 登録 (P0-3) ──────────────
    try:
        current_oos_count = _count_oos_active_listings()
    except Exception as e:
        logger.error('[w228_sec_d] OOS count 失敗 rc_id=%s: %s', rc_id, e)
        current_oos_count = 0  # 取得失敗は保守的に 0 として続行 (watch 登録の機会は守る)

    if current_oos_count >= max_oos:
        result['watch_skipped_oos_limit'] = True
        logger.warning(
            '[w228_sec_d] P0-3 上限超過でwatch未登録 rc_id=%s '
            'oos_active=%d max=%d',
            rc_id, current_oos_count, max_oos,
        )
        # M-1: 上限超過を Discord 通知 (Q0 痕跡必須、送信失敗はフローを止めない)
        try:
            from notifiers.discord_notifier import DiscordNotifier
            notifier = DiscordNotifier('')
            if notifier.webhook_url:
                notifier.send_message(
                    f'W228 承認: 在庫0上限超過のため watch 未登録 '
                    f'(rc_id={rc_id} / 現在 {current_oos_count} 件 / 上限 {max_oos} 件) '
                    f'— 手動登録要'
                )
        except Exception as _disc_e:
            logger.error('[w228_sec_d] Discord 通知失敗 rc_id=%s: %s', rc_id, _disc_e)
        result['success'] = True
        result['message'] = (
            f'下書き #{draft_id} を生成しました。'
            f'在庫0上限 {max_oos} 件超過のため watch 未登録 — 手動登録要。'
            f'(現在 {current_oos_count} 件アクティブ)'
        )
        return result

    # watch 登録
    from monitor.keyword_watch_db import add_watch
    keyword = title_ja.strip()
    sites = [
        ('mercari', _mercari_search_url(keyword)),
        ('yahoo_auctions', _yahoo_auctions_search_url(keyword)),
    ]
    watch_ids_registered: list[int] = []
    for site, search_url in sites:
        try:
            watch_id, _is_new = add_watch(
                site=site,
                search_url=search_url,
                keyword=keyword,
                price_max_jpy=found_price_jpy,
                memo=f'W228 research rc_id={rc_id} (Phase4 承認)',
                source='w228_research',
            )
            watch_ids_registered.append(watch_id)
            logger.info(
                '[w228_sec_d] watch 登録 rc_id=%s site=%s watch_id=%s',
                rc_id, site, watch_id,
            )
        except Exception as e:
            logger.exception('[w228_sec_d] add_watch 失敗 rc_id=%s site=%s', rc_id, site)
            # watch 登録失敗は draft_generated 遷移を取り消さない (下書きは有効)
            # ただし Q0 で痕跡を残す (呼出元が UI に警告表示)
            result['message'] = (
                f'下書き #{draft_id} を生成しましたが、{site} の watch 登録に失敗しました: {e}'
            )
            result['success'] = True
            result['watch_registered'] = len(watch_ids_registered) > 0
            result['watch_ids'] = watch_ids_registered
            return result

    if watch_ids_registered:
        try:
            record_watch_ids(rc_id, watch_ids_registered)
        except Exception as e:
            logger.error('[w228_sec_d] record_watch_ids 失敗 rc_id=%s: %s', rc_id, e)

    result['watch_registered'] = True
    result['watch_ids'] = watch_ids_registered
    result['success'] = True
    result['message'] = (
        f'下書き #{draft_id} を生成し、keyword watch を {len(watch_ids_registered)} サイトに登録しました。'
        f'「個別出品」タブで続きを行ってください。'
    )
    return result


def _render_section_d(config: dict) -> None:
    """承認キュー UI (セクション D、Phase 4).

    awaiting_approval 状態の候補を一覧表示し、
    承認 → 出品下書き自動生成 / 見送り を提供する。
    既存セクション A/B/C は一切触らない (K2 Surgical)。
    """
    st.markdown('### D. 承認キュー')
    st.caption(
        '探索と利益計算が完了した候補を確認して「承認 → 下書き生成」または「見送り」を選んでください。'
        '承認すると description が自動生成され「個別出品」タブに引き継がれます。'
    )

    # 直前 rerun で stash した結果メッセージ (st.rerun() は直書き表示を即消すため flash 化)
    _flash = st.session_state.pop(_SEC_D_FLASH_KEY, None)
    if _flash:
        _kind, _msg = _flash
        if _kind == 'success':
            st.success(_msg)
        elif _kind == 'warning':
            st.warning(_msg)
        elif _kind == 'error':
            st.error(_msg)
        else:
            st.info(_msg)

    # 候補取得
    try:
        candidates = list_research_candidates(status=STATUS_AWAITING_APPROVAL, limit=50)
    except Exception as e:
        st.error(f'承認キューの取得に失敗しました: {e}')
        logger.exception('[w228_sec_d] list_research_candidates 失敗')
        return

    if not candidates:
        st.info('承認待ちの候補はありません。夜間バッチ (04:30) が実行されると自動的に追加されます。')
        return

    st.caption(f'{len(candidates)} 件の承認待ち候補')

    for rc in candidates:
        rc_id: int = rc['rc_id']
        title_ja: str = rc.get('title_ja') or f'rc_id={rc_id}'

        with st.container(border=True):
            # ヘッダ
            harvest_tag = ''
            hp = rc.get('harvest_pattern')
            if hp == 'fresh_24h':
                harvest_tag = ' `[直近]`'
            elif hp == 'two_year_echo':
                harvest_tag = ' `[2年前型]`'
            st.markdown(f'**rc_id={rc_id}** — {title_ja}{harvest_tag}')

            # ── Terapeak 売れ行き ───────────────────────────────────────
            col_t1, col_t2 = st.columns(2)
            with col_t1:
                st.caption('【Terapeak 売れ行き】')
                avg_price = rc.get('ebay_avg_sold_price_usd')
                total_sold = rc.get('ebay_total_sold')
                gate_inputs_raw = rc.get('gate_inputs_json') or '{}'
                try:
                    gate_inputs = json.loads(gate_inputs_raw)
                except Exception:
                    gate_inputs = {}
                sold_1_2yr = gate_inputs.get('sold_1_2yr') or rc.get('ebay_total_sold')
                st.write(
                    f'90日 sold: **{total_sold if total_sold is not None else "N/A"}** 件 '
                    f'/ 1〜2年: **{sold_1_2yr if sold_1_2yr is not None else "N/A"}** 件 '
                    f'/ Avg: **{"${:.2f}".format(avg_price) if avg_price is not None else "N/A"}**'
                )
                gate_decision = rc.get('gate_decision') or ''
                if gate_decision:
                    st.caption(f'ゲート判定: `{gate_decision}`')

            with col_t2:
                # ── 仕入先候補 ────────────────────────────────────────
                st.caption('【仕入先候補 (フリマ)】')
                found_url = rc.get('found_url') or ''
                found_price_jpy = rc.get('found_price_jpy')
                match_score = rc.get('match_score')
                match_reason = rc.get('match_reason') or ''
                found_condition = rc.get('found_condition_ja') or ''

                if found_url:
                    price_str = f'¥{found_price_jpy:,}' if found_price_jpy is not None else 'N/A'
                    score_str = f'{match_score}' if match_score is not None else 'N/A'
                    # 「状態: 不明」だと仕入先 URL 不明と誤読される (2026-06-12 user 指摘)
                    st.write(
                        f'[仕入先リンク]({found_url})  '
                        f'**{price_str}**  コンディション: `{found_condition or "未取得"}`'
                    )
                    st.write(f'AI 一致度: **{score_str}**')
                    if match_reason:
                        st.caption(f'根拠: {match_reason[:120]}')
                else:
                    st.write('仕入先 URL なし (監視候補)')

            # ── 利益額 ──────────────────────────────────────────────────
            profit_jpy = rc.get('profit_jpy_true')
            profit_usd = rc.get('profit_usd_true')
            keisuke_pass = rc.get('keisuke_pass')
            keisuke_detail_raw = rc.get('keisuke_detail_json') or '{}'
            try:
                keisuke_detail = json.loads(keisuke_detail_raw)
            except Exception:
                keisuke_detail = {}

            col_p1, col_p2 = st.columns(2)
            with col_p1:
                st.caption('【利益額 (真値)】')
                if profit_jpy is None:
                    st.warning('利益未計算 (監視候補)')
                    # ── W262: 損益分岐仕入価格 (目標仕入価格) を併記 ──────
                    _target_buy, _target_reason = _get_max_purchase_for_rc(rc)
                    if _target_buy is not None:
                        st.markdown(
                            f'目標仕入価格: **¥{_target_buy:,} 以下**'
                        )
                        st.caption(
                            'この価格以下で出品されれば けいすけ基準 PASS '
                            '(Terapeak 平均売値 + 推定重量から逆算)。'
                            '承認すると watch 上限価格にこの値を使います。'
                        )
                        if rc.get('section232_flag'):
                            st.caption(
                                ':red[Section232 関税は逆算に未反映 — '
                                '実際の上限はこれより低い]'
                            )
                    else:
                        st.caption(
                            f'目標仕入価格: 算出不可 ({_target_reason or "理由不明"})'
                        )
                else:
                    profit_jpy_int = int(profit_jpy)
                    profit_usd_f = float(profit_usd) if profit_usd is not None else 0.0
                    color = 'green' if profit_jpy_int >= 0 else 'red'
                    st.markdown(
                        f'利益: <span style="color:{color};font-weight:bold;">'
                        f'¥{profit_jpy_int:,} / ${profit_usd_f:+.2f}</span>',
                        unsafe_allow_html=True,
                    )
                # けいすけ基準バッジ (Q11: detail_json の有無で判別)
                if not keisuke_detail:
                    st.caption('けいすけ基準: 未判定')
                elif keisuke_pass:
                    st.success('けいすけ基準: PASS')
                else:
                    st.error('けいすけ基準: 不合格')

                # ── W265: 状態整合 (中古は売値減額) の明示 ─────────────
                _cond_note = rc.get('condition_match_note')
                _cond_used = rc.get('condition_is_used')
                if _cond_note:
                    if _cond_used == 1:
                        # 中古減額は利益に直結するので警告色で明示
                        st.markdown(f':orange[🏷️ {_cond_note}]')
                    else:
                        st.caption(f'🏷️ 状態整合: {_cond_note}')

            with col_p2:
                # Section 232 赤バッジ
                if rc.get('section232_flag'):
                    s232_reason = rc.get('section232_reason') or '詳細不明'
                    st.error(f'Section232 該当の可能性: {s232_reason}')
                    st.caption('DDP関税で赤字化リスク。承認は任意 (自動BLOCKしない)。')

                # 重量表示
                weight_g = rc.get('manual_weight_g')
                weight_src = rc.get('weight_source') or ''
                weight_conf = rc.get('weight_confidence') or ''
                if weight_g is not None:
                    src_label = (
                        'AI推定' if weight_src == 'ai_estimate'
                        else ('手動' if weight_src == 'manual' else weight_src)
                    )
                    conf_label = f' (確信度: {weight_conf})' if weight_conf else ''
                    st.caption(f'推定重量: {int(weight_g)} g ({src_label}{conf_label})')
                else:
                    st.caption('重量: 未設定')

            # ── ボタン (form 外で即時反応、W225 教訓) ────────────────────
            # 連打防止 key
            _inflight_key = f'_w228_sec_d_inflight_{rc_id}'
            _processing = st.session_state.get(_inflight_key, False)

            # watch-only 判定 (found_url 無し = 監視候補、下書きは生成しない)
            _is_watch_only = not (rc.get('found_url') or '')
            _btn_label = '承認 → 監視登録' if _is_watch_only else '承認 → 下書き生成'
            _btn_help = (
                '仕入先未発見のため keyword watch (メルカリ+ヤフオク) のみ登録します。'
                if _is_watch_only
                else 'description を自動生成し「個別出品」タブに引き継ぎます。'
            )
            btn_col1, btn_col2 = st.columns([2, 1])
            with btn_col1:
                if _processing:
                    st.caption('処理中... (しばらくお待ちください)')
                elif st.button(
                    _btn_label,
                    key=f'w228_approve_{rc_id}',
                    type='primary',
                    help=_btn_help,
                ):
                    st.session_state[_inflight_key] = True
                    # _APPROVE_PROCESS_LOCK で同時実行防止
                    acquired = _APPROVE_PROCESS_LOCK.acquire(blocking=False)
                    if not acquired:
                        st.warning('別の承認処理が進行中です。少し待ってから再試行してください。')
                        st.session_state[_inflight_key] = False
                    else:
                        try:
                            with st.spinner(f'rc_id={rc_id} を処理中...'):
                                approval_result = _run_approval_logic(
                                    rc_id=rc_id,
                                    rc=rc,
                                    config=config,
                                )
                        finally:
                            _APPROVE_PROCESS_LOCK.release()
                            st.session_state[_inflight_key] = False

                        # st.rerun() は直書き表示を即消すため flash に stash して次 render で表示
                        if approval_result.get('success'):
                            msg = approval_result.get('message', '承認完了')
                            if approval_result.get('watch_skipped_oos_limit'):
                                _kind = 'warning'
                                logger.warning(
                                    '[w228_sec_d] OOS上限超過でwatch未登録 rc_id=%s', rc_id
                                )
                            else:
                                _kind = 'success'
                            st.session_state[_SEC_D_FLASH_KEY] = (_kind, f'「{title_ja}」: {msg}')
                        elif approval_result.get('needs_review_fallen'):
                            st.session_state[_SEC_D_FLASH_KEY] = (
                                'error',
                                f'「{title_ja}」: '
                                + approval_result.get('message', '下書き生成失敗'),
                            )
                        else:
                            st.session_state[_SEC_D_FLASH_KEY] = (
                                'error',
                                f'「{title_ja}」: '
                                + approval_result.get('message', '承認に失敗しました'),
                            )
                        st.rerun()

            with btn_col2:
                if not _processing and st.button(
                    '見送り',
                    key=f'w228_reject_{rc_id}',
                    type='secondary',
                    help='この候補を見送り (gate_rejected) にします。DB には残ります。',
                ):
                    try:
                        update_status(
                            rc_id, STATUS_GATE_REJECTED,
                        )
                        logger.info('[w228_sec_d] 見送り rc_id=%s', rc_id)
                        st.session_state[_SEC_D_FLASH_KEY] = (
                            'info', f'「{title_ja}」を見送りました。'
                        )
                        st.rerun()
                    except ValueError as e:
                        st.error(f'見送り遷移エラー: {e}')
                        logger.exception('[w228_sec_d] 見送り遷移失敗 rc_id=%s', rc_id)


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
        "フェーズ B MVP — Terapeak で発掘した商品をフリマ探索 → AI 同一性判定 → 利益判定 → 人間承認。"
        "セクション C で承認後、キーワード新着監視に登録できます。"
        "実eBay出品は別ステップ (最高リスクのため未実装)。"
    )

    # 日常運用は承認キュー (D) だけで完結するため D を先頭に表示し、
    # 手動調査ツール (A/B) と全候補一覧 (C) は折りたたみに格納する (2026-06-12 user 指示)。
    # A/B = 自動化前の手動単品調査 (リカバリ/スポット調査用に温存)、C = 監査用全履歴。
    st.divider()
    _render_section_d(config)

    st.divider()
    with st.expander('手動調査ツール / 全候補一覧 (A・B・C) — 日常運用では通常使いません', expanded=False):
        _render_section_a()
        st.divider()
        _render_section_b(config)
        st.divider()
        _render_section_c()
