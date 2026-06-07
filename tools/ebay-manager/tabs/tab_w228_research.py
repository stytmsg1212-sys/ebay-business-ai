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
K1 (シンプル): 実eBay出品は作らない。承認 + キーワード監視登録は W228 後続スコープとして追加。
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
    STATUS_IDENTITY_APPROVED,
    STATUS_IDENTITY_REJECTED,
    STATUS_NEEDS_REVIEW,
    STATUS_NEW,
    STATUS_NOT_FOUND,
    STATUS_SOURCED,
    STATUS_SOURCING,
    STATUS_WATCH_REGISTERED,
    get_research_candidate,
    list_research_candidates,
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

    # ── キーワード新着監視登録ボタン (identity_approved のみ表示) ──
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
    status_labels: dict[Optional[str], str] = {
        None: "全て",
        STATUS_NEW: "new",
        STATUS_SOURCING: "sourcing",
        STATUS_SOURCED: "sourced",
        STATUS_NOT_FOUND: "not_found",
        STATUS_NEEDS_REVIEW: "needs_review",
        STATUS_IDENTITY_APPROVED: "identity_approved",
        STATUS_IDENTITY_REJECTED: "identity_rejected",
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

    st.divider()
    _render_section_a()

    st.divider()
    _render_section_b(config)

    st.divider()
    _render_section_c()
