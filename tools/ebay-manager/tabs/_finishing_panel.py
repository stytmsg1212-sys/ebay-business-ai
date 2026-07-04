"""W314 Phase 2 S5 (2026-07-03): 統一「商品仕上げパネル」本体.

設計書: .company/engineering/docs/2026-07-03-finishing-panel-design.md §1-§5
モックアップ: 2026-07-03-finishing-panel-mockup.html (同ディレクトリ、user 承認済)

商品管理 / 在庫監視 / 仕入先候補 の 3 タブから同一パネルに合流させる統一 UI。
結線 (3 タブへの呼出し追加) は別 agent S6 が担当、本ファイルは
`render_finishing_panel()` の提供のみが scope (K1: 新規ファイルのみ)。

呼び出し契約 (S6 へ):
    from tabs._finishing_panel import render_finishing_panel
    render_finishing_panel(
        eid,                       # ebay_item_id (str, 必須、sku-rules 準拠)
        config,                    # schedule_config.json 相当 dict (省略可、None 可)
        candidate_id=None,         # 仕入先候補 id (供給リスク採用後 / 仕入先候補タブ経由のみ)
        candidate_url=None,        # 仕入先 URL (省略時 ebay_listings.source_url で自動補完)
        source_tab="product_management",  # 'product_management'|'inventory'|'supplier' 等
    )

Phase 2 スコープ外 (Phase 3 以降、設計書§8):
    - 価格・送料の編集 (本パネルには置かない、商品管理タブへ誘導のみ = T3 隔離)
    - 仕入先 URL の編集 (表示のみ)
    - 採用ロジック単一化 / 商品管理タブの 5 グループ再編

画像フィールドの扱い (Phase 2 の明示的な簡略化):
    `_supplier_photo_pipeline.render_supplier_photo_apply_section` は 3 モード
    それぞれが自前の「eBay に反映」ボタンを持ち、押下と同時に eBay へ即時反映される
    (= コンテンツ一括反映ボタンを待たない)。この独自完結フローに合わせるため、
    本パネルの「変更プレビュー」テーブルおよび「🚀 eBay へ反映」一括ボタンには
    images を含めない (`_finishing_panel_state.DISPATCH_FIELD_ORDER` に 'images' は
    無い)。画像パイプラインの内部 session_state (`sup_*` prefix) を横断的に
    dirty 追跡することは Phase 2 では行わない (3 モードで session_state 形状が
    異なり、外部ファイルを触らずに安全に統合するのは困難なため。Phase 3/5 の
    photo_pipeline 収斂で再検討)。
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

import streamlit as st

from tabs._finishing_panel_state import (
    AS_IS_CD_MAX_LEN,
    DISPATCH_FIELD_ORDER,
    FIELD_LABELS_JA,
    RANK_CHOICES,
    RANK_LABELS_JA,
    RANK_CONDITION_DESCRIPTION_TEMPLATE,
    RANK_LABELS_EN,
    apply_item_specifics_to_ebay,
    consume_pending_desc_retarget,
    schedule_desc_retarget,
    build_change_preview,
    compute_dirty_dispatch_fields,
    compute_header_metrics,
    dispatch_content_changes,
    fetch_condition_and_specifics_from_ebay,
    fetch_description_from_ebay,
    generate_description_via_ai,
    is_field_dirty,
    mark_field_synced,
    pf_key,
    rank_to_condition_id,
    resolve_condition_description_for_rank,
    resolve_effective_condition_id_for_cd_dispatch,
    retarget_rank_block_in_description,
    retarget_rank_headers_in_description,
    resolve_rank_initial,
    resolve_source_url,
    seed_initial,
    seed_session_value,
    validate_as_is_condition_description,
)

logger = logging.getLogger(__name__)

_RANK_BLANK = "（未設定 / eBay未取得）"

_PANEL_CSS = """<style>
.pf-money-note { color:#7a5407; font-size:12px; font-weight:600; }
</style>"""


def _inject_css_once() -> None:
    """パネル用 CSS を注入する.

    W314 Phase 4 (2026-07-03 性能監査): 旧実装は ``st.session_state`` センチネルで
    「このセッションで最初の呼出のみ」に絞っていたが、``render_finishing_panel`` は
    ``@st.fragment`` の**外側** (top-level script の通常フロー) から呼ばれるため、
    2 回目以降の full rerun ではこの ``st.markdown`` 自体が呼ばれず、Streamlit の
    要素ツリーからその delta path が消える → 以前注入した ``<style>`` タグが
    DOM から除去され、``.pf-money-note`` の配色が最初の 1 回しか効かなくなる
    (Streamlit は「今回の run で再送されなかった要素は破棄される」仕様のため、
    静的コンテンツであっても毎 full rerun で再送しないと表示が消える)。
    本 CSS は 3 行のみで再送コストが無視できるため、センチネルは撤去し
    無条件で毎回出力する (正しさ優先、K1)。360 行規模の商品管理 CSS /
    仕入先候補カード CSS (145 行×カード数) のような重い重複は
    tab_product_management.py / tab_supplier_candidates.py / tab_inventory_monitor.py
    側で「1 render 内で 1 回だけ」(session 単位でなく) の安全な方式に対応済み。
    """
    st.markdown(_PANEL_CSS, unsafe_allow_html=True)


def render_finishing_panel(
    eid: str,
    config: Optional[dict] = None,
    *,
    candidate_id: Optional[int] = None,
    candidate_url: Optional[str] = None,
    source_tab: str = "product_management",
    top_slot: Optional[Callable[[], None]] = None,
    bottom_slot: Optional[Callable[[], None]] = None,
) -> None:
    """統一「商品仕上げパネル」を描画するエントリポイント (呼び出し契約は module docstring 参照).

    Args:
        top_slot: ヘッダ (サムネ + 4 指標) の**直下**、「コンテンツ」 expander の**手前**に
            差し込む callable (user 2026-07-03 要望: 詳細編集 (従来) をこの位置に置く)。
            None なら差し込まない (followup 経由 = source_tab != "product_management" の
            既定動作)。callable は streamlit UI を描画する副作用のみ、戻り値は使わない。
        bottom_slot: 「コンテンツ」 expander の**下**に差し込む callable (W314 Phase 3 T2
            / 2026-07-03: 商品管理タブから「⚔ 競合・監視」「🌍 eBaymag」を渡す)。None なら
            差し込まない (商品管理専用スロット。followup 経由では渡されないため出ない)。
            callable は streamlit UI を描画する副作用のみ、戻り値は使わない。
    """
    if not eid:
        st.error("ebay_item_id が指定されていません (商品仕上げパネルを表示できません)")
        return
    _inject_css_once()
    _render_finishing_panel_fragment(
        eid, config, candidate_id=candidate_id,
        candidate_url=candidate_url, source_tab=source_tab,
        top_slot=top_slot, bottom_slot=bottom_slot,
    )


@st.fragment
def _render_finishing_panel_fragment(
    eid: str,
    config: Optional[dict],
    *,
    candidate_id: Optional[int],
    candidate_url: Optional[str],
    source_tab: str,
    top_slot: Optional[Callable[[], None]] = None,
    bottom_slot: Optional[Callable[[], None]] = None,
) -> None:
    """パネル本体 (@st.fragment、設計書§7: 採用時 scope="app" ではなくパネル scope に縮小).

    レイアウト (user 2026-07-03 要望反映):
        1. ヘッダ (サムネ + タイトル + 4 指標)
        2. top_slot() — 商品管理タブから渡す「🔧 詳細編集 (従来)」がここに来る
        3. 「コンテンツ」 expander (統一パネルの主要 UI)
        4. bottom_slot() — 商品管理タブから渡す「⚔ 競合・監視」「🌍 eBaymag」が
           ここに来る (W314 Phase 3 T2 / 2026-07-03)

    「仕入先」「💰 価格・送料」 expander は 2026-07-03 に撤去 (user 判断: 仮置き
    ブロックは詳細編集 (従来) がすぐ上にあるため案内不要)。関数
    `_render_supplier_group` / `_render_money_zone` は Phase 3 で再利用可能性が
    あるためソースは残置 (呼出のみ削除、K1)。
    """
    from monitor.database import get_ebay_listing_by_item_id

    row = get_ebay_listing_by_item_id(eid)
    if not row:
        st.error(f"listing が見つかりません (ebay_item_id={eid})")
        return

    _render_header(eid, row)

    if top_slot is not None:
        top_slot()

    _content_open = source_tab in ("inventory", "supplier")
    with st.expander("コンテンツ", expanded=_content_open):
        _render_content_group(
            eid, row, config,
            candidate_id=candidate_id, candidate_url=candidate_url, source_tab=source_tab,
        )

    if bottom_slot is not None:
        bottom_slot()


# =============================================================================
# ヘッダ
# =============================================================================

def _render_header(eid: str, row: dict) -> None:
    """サムネ / タイトル / 4 指標 (価格・利益・在庫・ステータス) を常時表示."""
    title = row.get("title") or "(タイトル不明)"
    metrics = compute_header_metrics(row)

    head_cols = st.columns([1, 4])
    with head_cols[0]:
        img_url = row.get("ebay_image_url")
        if img_url:
            try:
                st.image(img_url, use_container_width=True)
            except Exception:  # noqa: BLE001 -- 画像表示失敗でパネル全体を壊さない
                st.caption("🖼️")
        else:
            st.caption("🖼️ (画像未取得)")
    with head_cols[1]:
        st.markdown(f"**{title}**")
        st.caption(f"ebay_item_id: {eid} ・ SKU: {row.get('sku') or '-'}")
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            if metrics["profit_jpy"] is not None:
                _delta = (
                    f"{metrics['profit_rate_pct']}%"
                    if metrics["profit_rate_pct"] is not None else None
                )
                st.metric("利益", f"¥{metrics['profit_jpy']:,}", _delta)
            else:
                st.metric("利益", "—")
        with m2:
            st.metric("販売価格", f"${metrics['price_usd']:,.2f}")
        with m3:
            st.metric("在庫数", metrics["quantity"])
        with m4:
            st.metric("ステータス", metrics["status"])


def _get_condition_snapshot(eid: str, config: Optional[dict]) -> dict:
    """CD / Item Specifics の eBay 現在値を 1 回だけ取得しキャッシュする (baseline 用).

    #44 (2026-07-04): 従来 CD baseline は空文字固定だった (dirty 判定・監査ログの
    before が実態と乖離) ため、GetItem 実値を fetch して baseline にする。
    取得失敗時は空文字/空 dict にフォールバックし、呼出側が「現在値取得失敗」
    キャプションを出す (Q0: 失敗を隠さない)。session_state にキャッシュするため
    同一 render 内で CD 欄 / Item Specifics 欄の双方から呼んでも GetItem は 1 回のみ。
    """
    sk = pf_key(eid, "cond_snapshot")
    if sk not in st.session_state:
        st.session_state[sk] = fetch_condition_and_specifics_from_ebay(eid, config)
    return st.session_state[sk]


# =============================================================================
# コンテンツグループ
# =============================================================================

def _render_content_group(
    eid: str,
    row: dict,
    config: Optional[dict],
    *,
    candidate_id: Optional[int],
    candidate_url: Optional[str],
    source_tab: str,
) -> None:
    fields: dict[str, dict] = {}

    # ---- タイトル ----
    title_key = pf_key(eid, "title")
    title_initial = seed_initial(st.session_state, eid, "title", row.get("title") or "")
    seed_session_value(st.session_state, title_key, title_initial)
    _title_dirty_before = is_field_dirty(
        "title", title_initial, st.session_state.get(title_key, title_initial),
    )
    st.text_input(
        "商品タイトル (eBay Title / 80 文字以内)"
        + ("　🟠 変更あり" if _title_dirty_before else ""),
        max_chars=80,
        key=title_key,
    )
    new_title = st.session_state.get(title_key, title_initial)
    st.caption(f"{len(new_title or '')}/80 文字")
    fields["title"] = {"before": title_initial, "after": new_title}

    # ---- description (3 方式: AI で生成 / eBay から取得 / 手動編集) ----
    _render_description_field(
        eid, row, config,
        candidate_id=candidate_id, candidate_url=candidate_url, fields=fields,
    )

    # ---- Item Specifics (#44、Description & Condition 枠の下、編集可プレビュー) ----
    _render_item_specifics_field(eid, row, config, fields)

    # ---- 画像 (3モード常時表示、一括反映の対象外 — module docstring 参照) ----
    _render_image_field(
        eid, row,
        candidate_id=candidate_id, candidate_url=candidate_url,
    )

    # ---- ランク + コンディション理由は Description コンテナ内 (「セット」構成) で
    # 既に処理済 (`_render_condition_subblock` が fields['rank'] /
    # fields['condition_description'] を設定)。ここで独立配置しない (user 要望)。

    # ---- 数量 (独立フィールド) ----
    qty_initial = seed_initial(
        st.session_state, eid, "quantity", int(row.get("quantity_ebay") or 0),
    )
    qty_key = pf_key(eid, "quantity")
    seed_session_value(st.session_state, qty_key, qty_initial)
    st.number_input("数量", min_value=0, step=1, key=qty_key)
    new_qty = int(st.session_state.get(qty_key, qty_initial))
    fields["quantity"] = {"before": qty_initial, "after": new_qty}

    # ---- 変更プレビュー + 一括反映 (2026-07-03 UX: 常時表示) ----
    # user 指摘: 「dirty がある時だけ描画」だと、画像だけ触る典型フロー (画像は
    # 別ボタンで即時反映される) で「🚀 eBay へ反映」ボタンが一度も現れず、
    # user が「反映ボタンが見当たらない」と迷う。dirty ゼロでも案内 + 無効ボタンを
    # 常時描画して視認性を確保する。
    #
    # T1 修正 (2026-07-04 実機 E2E): N ランク (ConditionID 1000) は eBay 仕様上
    # ConditionDescription 非対応のため apply 層で CD を送信しない (HIGH-1)。
    # UI 側の変更プレビュー表 / 「反映 (N件)」件数からも CD dirty を除外して、
    # 「反映 (2件)」と表示したのに実送信が rank だけ = 1 件、という不整合を防ぐ
    # (user 方針「反映漏れ・不整合は許されない」)。rank 変更自体の計上は維持する
    # (rank は revise_item_condition で送られる)。
    _effective_cond_id_for_cd_dispatch = resolve_effective_condition_id_for_cd_dispatch(
        fields.get("rank"), row.get("ebay_condition_id"),
    )
    _cd_dispatch_suppressed_by_n = (_effective_cond_id_for_cd_dispatch == "1000")

    # 変更プレビュー表からも CD を除外する。build_change_preview は fields dict の
    # dirty のみを列挙するため、事前に fields から抜くか事後に filter するかの 2 択。
    # 事後 filter を選ぶ (fields 自体は _apply_content_changes に渡って apply 層側の
    # 二段防御でも CD=None 化されるため、fields を変えずに保つ方が挙動の予測が容易)。
    preview = build_change_preview(fields)
    if _cd_dispatch_suppressed_by_n:
        preview = [p for p in preview if p.get("field") != "condition_description"]

    # ボタン件数表示: `compute_dirty_dispatch_fields` に集約 (state 層の純関数)。
    # 内訳: DISPATCH_FIELD_ORDER + condition_description (N=1000 は除外) +
    # item_specifics (dispatch_disabled は除外)。テストしやすさと UI/apply の
    # 「表示件数と実送信件数の一致」を守るための単一情報源。
    _cd_present = fields.get("condition_description") or None
    _spec_present = fields.get("item_specifics") or None
    _dirty_dispatch_fields = compute_dirty_dispatch_fields(
        fields, _effective_cond_id_for_cd_dispatch,
    )

    # H2 (2026-07-04): Item Specifics の dirty があっても baseline 失敗で dispatch
    # 抑止中のケースを反映ボタン側にも波及させる (dispatch 対象がゼロの時は反映不可)。
    _spec_disabled_dirty = bool(
        _spec_present
        and _spec_present.get("dispatch_disabled")
        and is_field_dirty(
            "item_specifics",
            _spec_present.get("before") or {}, _spec_present.get("after") or {},
        )
    )

    # T1 修正 (2026-07-04): N の時の CD 除外を user に明示するため、CD dirty があった
    # 場合のみ 1 行 caption を出す (dirty が無い時は静かに何もしない)。
    _cd_present_dirty = bool(
        _cd_present and is_field_dirty(
            "condition_description",
            _cd_present.get("before", ""), _cd_present.get("after", ""),
        )
    )
    if _cd_dispatch_suppressed_by_n and _cd_present_dirty:
        st.caption(
            "ℹ️ 新品 (N) はコンディション説明が無いため CD は送信対象外です "
            "(ランク変更は反映されます)。"
        )

    if preview:
        st.markdown("**変更プレビュー** (変更されたフィールドのみ表示)")
        st.table([
            {"フィールド": p["label"], "Before": p["before"], "After": p["after"]}
            for p in preview
        ])
        if _spec_disabled_dirty:
            st.warning(
                "⚠ 現行値を取得できないため Item Specifics の反映は無効化中です "
                "(再読み込みで再試行)。他フィールドのみ反映されます。"
            )
        if _dirty_dispatch_fields:
            _btn_label = f"🚀 eBay へ反映 ({len(_dirty_dispatch_fields)}件の変更)"
            _btn_disabled = False
        else:
            # Item Specifics のみ dirty で dispatch 抑止中 = 反映対象ゼロ。
            _btn_label = "🚀 eBay へ反映 (Item Specifics のみ・反映無効化中)"
            _btn_disabled = True
    else:
        # dirty ゼロ: 2 行案内 + 無効化ボタン (常時表示で視認性確保)。
        st.info(
            "📭 反映待ちの変更はありません "
            "(タイトル / Description / コンディション / 数量をここで編集すると反映ボタンが有効になります)"
        )
        st.caption(
            "🖼 画像は上の画像セクション内の専用ボタンで即時反映されます"
            " (この一括反映ボタンの対象外)。"
        )
        _btn_label = "🚀 eBay へ反映 (変更なし)"
        _btn_disabled = True

    if st.button(
        _btn_label,
        key=pf_key(eid, "apply_btn"), type="primary",
        disabled=_btn_disabled,
    ):
        _apply_content_changes(eid, fields, config, source_tab=source_tab, candidate_id=candidate_id)


# =============================================================================
# description フィールド (3 方式: AI で生成 / eBay から取得 / 手動編集)
# モックアップ tab_t1「コンテンツ」→「Description」ブロックに対応
# =============================================================================

_DESC_METHOD_AI = "🤖 AI で生成"
_DESC_METHOD_EBAY = "⬇️ eBay から取得"
_DESC_METHOD_MANUAL = "✏️ 手動編集"


def _render_description_field(
    eid: str,
    row: dict,
    config: Optional[dict],
    *,
    candidate_id: Optional[int],
    candidate_url: Optional[str],
    fields: dict[str, dict],
) -> None:
    """Description フィールド (3 方式選択 + プレビュー + 編集 textarea).

    モックアップ .field > label='Description' + btnrow (🤖/⬇️/✏️) + desc-preview
    に対応。3 方式は st.radio で明示 (task 指示「モックの見た目に寄せる」)。
    生成/取得結果は共通 session_state (desc_key) に流し、textarea で編集可能。
    反映は下の「🚀 eBay へ反映」一括ボタン経由 (K1: 反映経路を統一)。
    """
    desc_key = pf_key(eid, "description")
    desc_initial = seed_initial(
        st.session_state, eid, "description", row.get("listing_description") or "",
    )
    seed_session_value(st.session_state, desc_key, desc_initial)

    # HIGH crash 修正 (2026-07-04 live QA): description text_area (下記 L468) が
    # 既に instantiate 済みの状態で `_render_condition_subblock` が `desc_key` へ
    # 直接書込むと StreamlitAPIException で panel 全体が落ちる (「widget 生成後の
    # session_state 変更不可」制約)。retarget 経路を pending キー経由に変え、
    # widget 生成前 (ここ) で pending → desc_key を反映する (`consume_pending_desc_retarget`)。
    consume_pending_desc_retarget(st.session_state, eid)

    with st.container(border=True):
        st.markdown("**📝 Description & Condition (商品説明とコンディション)**")
        st.caption(
            "user 要望 2026-07-03: 商品タイトル / 説明文 / ランク / コンディション理由は"
            "「セット」で編集する構成。ランク編集時は理由 (中古 = 動作確認結果 / As-Is = 必須) "
            "も一緒に。"
        )

        # ── (1) Description サブブロック ──
        st.markdown("**Description**")
        # 現行プレビュー (先頭 200 字)
        st.caption("現行プレビュー")
        _preview_src = desc_initial or "(未設定)"
        st.markdown(_preview_src[:200] + ("…" if len(_preview_src) > 200 else ""))

        # 3 方式選択 radio (モック btnrow 相当)
        method_key = pf_key(eid, "desc_method")
        seed_session_value(st.session_state, method_key, _DESC_METHOD_MANUAL)
        st.radio(
            "生成/取得方式",
            options=[_DESC_METHOD_AI, _DESC_METHOD_EBAY, _DESC_METHOD_MANUAL],
            key=method_key,
            horizontal=True,
        )
        method = st.session_state.get(method_key, _DESC_METHOD_MANUAL)

        if method == _DESC_METHOD_AI:
            _render_description_ai_controls(
                eid, row, desc_key,
                candidate_id=candidate_id, candidate_url=candidate_url,
            )
        elif method == _DESC_METHOD_EBAY:
            _render_description_ebay_fetch(eid, config, desc_key)
        else:
            st.caption("✏️ 下の編集欄で直接編集してください。")

        # textarea は常時表示 (全方式で最終編集可能)
        st.text_area(
            "説明文 (HTML) を編集",
            key=desc_key,
            height=180,
        )

        # ── (2) Condition サブブロック (Description とセット、user 2026-07-03 要望) ──
        st.divider()
        _render_condition_subblock(eid, row, fields, config)

    new_desc = st.session_state.get(desc_key, desc_initial)
    fields["description"] = {"before": desc_initial, "after": new_desc}


def _render_description_ai_controls(
    eid: str,
    row: dict,
    desc_key: str,
    *,
    candidate_id: Optional[int],
    candidate_url: Optional[str],
) -> None:
    """AI 生成方式の入力欄 (URL / ランク / 追加指示 / 生成ボタン).

    URL 解決順: candidate_url > row["source_url"] > user 入力欄 (resolve_source_url)。
    生成は `_finishing_panel_state.generate_description_via_ai` 経由で
    `_supplier_description_pipeline.generate_supplier_description` を呼ぶ
    (candidate_id=0 で URL 直接投入経路、既存 tab_product_management
    `_render_url_direct_description_section` と同じ)。

    2026-07-04 user 恒久仕様: URL が無くても「description に入れたい文言・指示」が
    あれば生成可能 (既存 listing 情報 = row の title/condition_rank/description を
    代替コンテキストとして渡す)。両方空の時のみ生成ボタンを disabled にする
    (URL 必須の従来制約は撤廃)。
    """
    from tabs._finishing_panel_state import RANK_CHOICES as _RANK_CHOICES

    url_input_key = pf_key(eid, "desc_ai_url")
    rank_key = pf_key(eid, "desc_ai_rank")
    extra_key = pf_key(eid, "desc_ai_extra")

    # URL 入力欄 (candidate_url / row.source_url があれば prefill、無ければ user 入力)
    _prefilled_url = (candidate_url or row.get("source_url") or "").strip()
    seed_session_value(st.session_state, url_input_key, _prefilled_url)
    st.text_input(
        "引用元 URL (メルカリ/ヤフオク/PayPay=専用解析、その他=AI解析)",
        key=url_input_key,
        placeholder="https://...",
    )
    _typed_url = (st.session_state.get(url_input_key) or "").strip()
    resolved_url = resolve_source_url(candidate_url, row, _typed_url)

    # ランク: 既存 condition_rank を default、"(引用元から AI 自動判定)" も選択可
    _listing_rank = (row.get("condition_rank") or "").strip()
    _rank_opts = ["(引用元から AI 自動判定)"] + list(_RANK_CHOICES)
    _default_idx = _rank_opts.index(_listing_rank) if _listing_rank in _RANK_CHOICES else 0
    seed_session_value(st.session_state, rank_key, _rank_opts[_default_idx])
    st.selectbox(
        "商品ランク (既存の商品状態を尊重。AI 判定に任せる場合は先頭を選択)",
        options=_rank_opts,
        key=rank_key,
    )
    _rank_sel = st.session_state.get(rank_key, _rank_opts[_default_idx])
    _rank_override = _rank_sel if _rank_sel in _RANK_CHOICES else None

    seed_session_value(st.session_state, extra_key, "")
    st.text_area(
        "description に入れたい文言・指示 (任意)",
        key=extra_key,
        placeholder="例: ギフト包装対応可と必ず書いて / バンドル品である点を強調",
        height=70,
    )
    _extra = (st.session_state.get(extra_key) or "").strip() or None

    # 2026-07-04 user 恒久仕様: URL が無くても「description に入れたい文言・指示」が
    # あれば生成を続行できる (URL 必須の従来制約を緩和)。両方空の時のみ案内・disabled。
    if not resolved_url and not _extra:
        st.warning(
            "引用元 URL か「description に入れたい文言・指示」のいずれかを"
            "入力してください。"
        )

    sku = (row.get("sku") or "")
    is_in_stock = sku.startswith("stock")

    if st.button(
        "🤖 生成",
        key=pf_key(eid, "desc_ai_run_btn"),
        type="primary",
        disabled=not (resolved_url or _extra),
    ):
        # existing_listing_context: URL 未指定時の代替コンテキスト (2026-07-04)。
        # URL があっても無害 (URL 空時のみ generate_supplier_description 側で使う)。
        _existing_ctx = {
            "title": row.get("title") or "",
            "condition_rank": row.get("condition_rank") or "",
            "listing_description": row.get("listing_description") or "",
        }
        with st.spinner("scrape/AI 解析 + Claude description 生成中 (~30-60 秒)..."):
            result = generate_description_via_ai(
                resolved_url,
                candidate_id=candidate_id or 0,
                in_stock=is_in_stock,
                rank_override_code=_rank_override,
                extra_instructions=_extra,
                existing_listing_context=_existing_ctx,
            )
        if result.get("success"):
            st.session_state[desc_key] = result.get("description_html") or ""
            # #44 (2026-07-04) user 要望: description AI 生成時、CD 欄 / Item Specifics
            # プレビュー欄にも自動セット (どちらも dirty 化 → 変更プレビューに出る)。
            # baseline (*_initial) は eBay 現在値のまま変えない (dirty 判定を保つ)。
            #
            # バグ2修正 (2026-07-04): CD は AI 自由文をそのまま採用せず、ランクから
            # 決定論的に導出する (`resolve_condition_description_for_rank`)。AI が
            # 商品固有の長文を condition_description に混入させても採用しないことで、
            # 「コンディション欄に商品説明が残る・入る」事故の経路を断つ (As-Is のみ
            # 定型化不能なため AI 生成値をそのまま使う、同関数内で分岐)。
            _effective_rank_for_cd = (
                _rank_override or (result.get("rank_code") or "").strip() or None
            )
            _cd_final = resolve_condition_description_for_rank(
                _effective_rank_for_cd, (result.get("condition_description") or "").strip(),
            )
            if _cd_final:
                st.session_state[pf_key(eid, "condition_description")] = _cd_final
                if _effective_rank_for_cd:
                    st.session_state[pf_key(eid, "cd_auto_last_rank")] = _effective_rank_for_cd
            _specs_ai = result.get("item_specifics") or {}
            if _specs_ai:
                # HIGH (2026-07-04 Codex 外部レビュー): AI 生成 (generate_listing) は
                # reference Keys しか埋めず (URL-less 時は Brand/Model/Type/Color/
                # Connectivity の 5 個程度)、不明項目は key ごと省略する = 出力は eBay
                # 既存 Item Specifics の**真部分集合**になりがち。従来の完全置換
                # `= dict(_specs_ai)` + apply の replace_all=True の組合せで、既存の
                # Brand/MPN/UPC 等が消失する事故 (Q0 silent 変更) が起きる経路だった。
                # 対策: baseline (item_specifics_initial = eBay 現在値) と **merge** し、
                # 既存全項目を保持しつつ AI 生成値は上書き/追加のみ許す。
                # baseline 取得失敗時は H2 ガードで dispatch が抑止されるので、その
                # ケースは merge 元が空でも安全側に振れる (どうせ送信されない)。
                _baseline_full = dict(
                    st.session_state.get(pf_key(eid, "item_specifics_initial"), {}) or {}
                )
                st.session_state[pf_key(eid, "item_specifics_current")] = {
                    **_baseline_full, **_specs_ai,
                }
                # data_editor は key 付き widget の内部状態を優先するため、次の
                # data 引数を反映させるには widget key 自体を削除して再初期化させる
                # 必要がある (tab_individual_listing.py の「生成結果に戻す」と同型)。
                _de_key = pf_key(eid, "dataeditor_item_specifics")
                if _de_key in st.session_state:
                    del st.session_state[_de_key]
            st.success(result.get("message") or "生成完了")
            st.rerun(scope="fragment")
        else:
            st.error(result.get("message") or "生成に失敗しました")


def _render_description_ebay_fetch(
    eid: str,
    config: Optional[dict],
    desc_key: str,
) -> None:
    """eBay GetItem で現行 description を取得して textarea に流す (従来動作)."""
    if st.button("⬇️ eBay から取得", key=pf_key(eid, "desc_fetch_btn")):
        _res = fetch_description_from_ebay(eid, config)
        if _res["success"]:
            st.session_state[desc_key] = _res["description"]
            st.success("取得しました。下の編集欄に反映しました。")
            st.rerun(scope="fragment")
        else:
            st.error(_res["message"])


# =============================================================================
# Condition サブブロック (Description コンテナ内、user 2026-07-03 要望で統合)
# =============================================================================

def _render_condition_subblock(
    eid: str, row: dict, fields: dict[str, dict], config: Optional[dict],
) -> None:
    """ランク + コンディション理由 (ConditionDescription) を Description とセットで編集.

    2026-07-03 user 要望「コンディションは Description とセットで編集する」に基づき、
    従来独立フィールドだったランク selectbox をここへ移動 + eBay ConditionDescription
    を編集する text_input を新設。dirty 判定結果は `fields['rank']` と
    `fields['condition_description']` にそのまま書き込む (呼出側 build_change_preview /
    _apply_content_changes に届く形)。

    As-Is (7000) の理由必須ガード + 65 字制約は
    `_finishing_panel_state.validate_as_is_condition_description` で dispatch 直前に検証。

    #44 (2026-07-04): CD の dirty 判定 baseline は従来「常に空文字」固定だったため、
    eBay に既存の ConditionDescription が設定されている listing で「未変更」が
    dirty 誤検知/未検知になっていた (監査ログの before も不正確)。
    `_get_condition_snapshot` (GetItem 1 回) で実値を取得し baseline にする。
    """
    st.markdown("**🏷️ Condition (商品ランク + 理由)**")
    st.caption(
        "ランクは eBay 実 Condition 由来 (人気度グレードとは別)。"
        "N=新品 / S=新品同様 / A-PO=Used サブランク / As-Is=未確認・部品取り。"
        "🚀 反映 で eBay Condition + ConditionDescription も一緒に更新されます。"
    )

    # ── ランク selectbox ──
    rank_initial = seed_initial(st.session_state, eid, "rank", resolve_rank_initial(row))
    rank_key = pf_key(eid, "rank")
    _rank_opts = [_RANK_BLANK] + list(RANK_CHOICES)
    _rank_seed = rank_initial if rank_initial in RANK_CHOICES else _RANK_BLANK
    seed_session_value(st.session_state, rank_key, _rank_seed)
    st.selectbox(
        "ランク",
        options=_rank_opts,
        format_func=lambda v: RANK_LABELS_JA.get(v, v),
        key=rank_key,
        help="変更を 🚀 反映 すると eBay Condition が更新されます。"
             "S(Open Box=1500) は一部カテゴリで不可 → 送信後に verify 失敗ならメッセージで通知。",
    )
    _rank_sel = st.session_state.get(rank_key, _rank_seed)
    new_rank = None if _rank_sel == _RANK_BLANK else _rank_sel
    fields["rank"] = {"before": (rank_initial or None), "after": new_rank}

    # ── コンディション理由 (eBay ConditionDescription) ──
    # DB には保存しない (eBay 専用、tab_product_management 従来動作を踏襲)。
    # baseline (`condition_description_initial`) は eBay 実値を GetItem で 1 回だけ
    # 取得して使う (#44)。取得失敗時は空文字 fallback + 警告キャプション (Q0)。
    _snap = _get_condition_snapshot(eid, config)
    _cd_baseline = (_snap.get("condition_description") or "") if _snap.get("success") else ""
    cd_initial = seed_initial(st.session_state, eid, "condition_description", _cd_baseline)
    cd_key = pf_key(eid, "condition_description")
    seed_session_value(st.session_state, cd_key, cd_initial)

    # #44 バグ2修正 (2026-07-04): ランク変更時は CD (ConditionDescription) を
    # ランク定型文へ強制同期し dirty 化する (user 報告バグ2「description だけ
    # 反映した場合に旧 CD が dirty 扱いにならず残存する」経路を断つ対策)。
    # As-Is は商品固有の理由が必須で定型化不能なため対象外 (user 入力に委ねる)。
    # 初回 render (rank 未変更) を「変更」と誤検知しないよう baseline rank で
    # 追跡値を先に seed する。
    _cd_auto_rank_key = pf_key(eid, "cd_auto_last_rank")
    seed_session_value(st.session_state, _cd_auto_rank_key, rank_initial or None)

    # HIGH-1 修正 (2026-07-04 Codex): 「As-Is へ切替時に旧ランク定型が cd_key に
    # 残留 → validate は非空・65字以内しか見ず素通り → As-Is 商品に『動作確認済』の
    # 矛盾 Seller Notes」事故経路の根治。
    #   (a) description 見出し retarget を As-Is ガードの外へ (As-Is 切替時も本文
    #       見出しを `Rank As-Is` へ追従)
    #   (b) As-Is へ遷移した時、現行 CD が rank テンプレ由来 (テンプレ値一致 or
    #       `Rank ` 始まり) ならクリアして「As-Is は理由入力が必須」caption を表示
    #   (c) validate_as_is_condition_description に「`Rank ` 始まり / `As-Is`
    #       不在」reject を追加 (state 層、二重ゲート)
    _rank_changed = bool(new_rank and new_rank != st.session_state.get(_cd_auto_rank_key))
    if _rank_changed:
        if new_rank != "As-Is":
            st.session_state[cd_key] = resolve_condition_description_for_rank(new_rank)
            # MED-1 (2026-07-04 T3 レビュー): user が手動編集した CD がランク変更に伴い
            # 定型文で無警告に上書きされるのを明示する (Q0: silent 更新禁止)。
            st.caption(
                "ℹ️ ランク変更に伴いコンディション定型文へ更新しました "
                "(手動編集していた場合は上書きされています)。"
            )
        else:
            # As-Is 遷移時: 旧ランク定型が残留していたらクリア (二重ゲート、
            # validate は state 層でも `Rank ` prefix reject するが、UI 側で
            # 事前クリアして user に理由入力を促す)。
            _cur_cd = (st.session_state.get(cd_key) or "").strip()
            _is_template = (
                _cur_cd.startswith("Rank ")
                or _cur_cd in RANK_CONDITION_DESCRIPTION_TEMPLATE.values()
            )
            if _is_template:
                st.session_state[cd_key] = ""
                st.caption(
                    "⚠️ As-Is へ変更したため旧ランクのコンディション定型文をクリアしました。"
                    "As-Is は理由入力が必須です "
                    "(例: 'As-Is — No AC adapter for testing')。"
                )
            else:
                st.caption(
                    "ℹ️ As-Is は商品固有の理由が必須です "
                    "(例: 'As-Is — <reason>')。"
                )
        st.session_state[_cd_auto_rank_key] = new_rank

        # バグ2修正 (2026-07-04 358754421540): AI 生成済み description 本文の
        # CONDITION RANK 見出し (`<h3>Rank B &mdash; Good</h3>` 等) をランク変更に追従。
        # HIGH-1 修正 (2026-07-04 Codex): As-Is 遷移時も含めて追従させるため
        # As-Is ガードの外に配置。
        # HIGH crash 修正 (2026-07-04 live QA): description text_area は既に
        # instantiate 済み (`_render_description_field` L468) のため desc_key への
        # 直接書込は StreamlitAPIException で panel クラッシュ。**pending キー経由**
        # (`schedule_desc_retarget`) で書いて fragment rerun し、次サイクルの
        # widget 生成前 (`consume_pending_desc_retarget`) で desc_key へ反映する。
        # 【部分追従バグ完遂 2026-07-04 実機再確認】従来 h3 のみ追従で letter/chip が
        # stale していた + As-Is 遷移が「Rank As-Is」(bare) を吐いて片道切符だった。
        # `retarget_rank_block_in_description` で 3 要素同時追従 + bare 回収 + Quick
        # Notes 自由文の警告表示に切替。
        _desc_key = pf_key(eid, "description")
        _desc_html = st.session_state.get(_desc_key) or ""
        _rt = retarget_rank_block_in_description(
            _desc_html, new_rank, RANK_LABELS_EN.get(new_rank),
        )
        if _rt["any_changed"]:
            schedule_desc_retarget(st.session_state, eid, _rt["new_html"])
            _changed_parts: list[str] = []
            if _rt["h3_changed"]:
                _changed_parts.append("見出し")
            if _rt["letter_changed"]:
                _changed_parts.append("バッジ文字")
            if _rt["chip_changed"]:
                _changed_parts.append("状態チップ")
            st.caption(
                "ℹ️ description 本文の Rank 表記 "
                f"({' / '.join(_changed_parts)}) をランク変更に追従しました。"
            )
            if _rt["quick_notes_present"]:
                st.caption(
                    "⚠️ 本文の状態説明文 (Quick Notes) は旧ランク時の内容のままです。"
                    "整合させるには Description を再生成してください。"
                )
            # 次サイクルで pending が widget 生成前に反映されるよう fragment rerun。
            st.rerun(scope="fragment")

    if not _snap.get("success"):
        st.caption(
            f"⚠️ eBay 現在値取得失敗: {_snap.get('message') or '不明'} "
            "(空欄を基準に編集します)"
        )
    _cd_help_lines = [
        "中古ランク (A-PO) では動作確認結果 (例: 'Tested OK. Power on/off: OK / Audio: OK')。",
        f"As-Is は必須 (欠落 = buyer 紛争で Defect 確定リスク、eBay XML {AS_IS_CD_MAX_LEN} 字以内・"
        "'As-Is — <reason>' 形式推奨)。",
        "空欄で反映すると eBay 側の既存 ConditionDescription を維持 (未変更)。",
    ]
    st.text_input(
        "コンディション理由 (eBay ConditionDescription)",
        key=cd_key,
        max_chars=1000,  # eBay 一般上限 (As-Is は 65 字で別途 dispatch 直前検証)
        help="\n".join(_cd_help_lines),
    )
    new_cd = (st.session_state.get(cd_key, "") or "").strip()
    fields["condition_description"] = {"before": (cd_initial or ""), "after": new_cd}

    # As-Is + 空理由の warn は入力時点で早めに表示 (dispatch 直前の error より UX 良い)
    if new_rank == "As-Is" and not new_cd:
        st.warning(
            f"⚠️ As-Is は理由が必須です。{AS_IS_CD_MAX_LEN} 字以内 / "
            "'As-Is — <reason>' 形式で入力してください。"
        )
    elif new_rank == "As-Is" and len(new_cd) > AS_IS_CD_MAX_LEN:
        st.warning(
            f"⚠️ As-Is 理由は {AS_IS_CD_MAX_LEN} 字以内 (現在 {len(new_cd)} 字)。"
            "短縮してください。"
        )


# =============================================================================
# Item Specifics フィールド (#44 2026-07-04、Description & Condition 枠の下)
# =============================================================================

def _render_item_specifics_field(
    eid: str, row: dict, config: Optional[dict], fields: dict[str, dict],
) -> None:
    """Item Specifics プレビュー欄 (name:value 編集可、Description & Condition 枠の下).

    user 要望「description を AI 生成/編集したら Item Specifics も自動生成して
    変更してほしい」に対応する編集可能プレビュー。baseline は eBay 現在値
    (`_get_condition_snapshot` 経由、CD と同じ GetItem 1 回でまとめて取得)。
    AI 生成時は `_render_description_ai_controls` が
    `pf_key(eid, "item_specifics_current")` を直接書き換えて自動セットする
    (tab_individual_listing.py の data_editor 編集パターンを踏襲)。

    一括反映は `_apply_content_changes` が dirty 時のみ動的に
    `apply_item_specifics_to_ebay` (revise_item_specifics 経由) を dispatch する
    (rank/condition_description と同じ「DISPATCH_FIELD_ORDER 外の動的判断」方式)。
    """
    st.markdown("**🏷️ Item Specifics**")
    st.caption("eBay Item Specifics (項目名 / 値)。行を追加・削除して編集できます。")

    _snap = _get_condition_snapshot(eid, config)
    _snap_ok = bool(_snap.get("success"))
    _baseline = dict(_snap.get("item_specifics") or {}) if _snap_ok else {}
    _multi_value_names = list(_snap.get("multi_value_names") or []) if _snap_ok else []
    specifics_initial = seed_initial(st.session_state, eid, "item_specifics", _baseline)

    # H2 (2026-07-04 T3 レビュー): baseline fetch (GetItem) が失敗している状態で
    # revise_item_specifics (replace_all=True) を走らせると、eBay 側の現行 Item
    # Specifics を「取得できていない不明な値」で全置換して既存を消し得る (transient
    # 失敗時の事故)。「全置換は直前の GetItem 成功が前提条件」として、baseline
    # 失敗時は編集 UI を残しつつ dispatch を抑止するフラグを fields に載せる
    # (`_apply_content_changes` 側が dispatch を skip、st.error で理由を表示)。
    if not _snap_ok:
        st.warning(
            "⚠️ eBay 現在値の取得に失敗しました "
            f"(理由: {_snap.get('message') or '不明'})。"
            " Item Specifics は eBay 仕様上「全置換」で反映されるため、"
            "現行値が不明な状態で反映すると **既存の Item Specifics が消える恐れ** があります。"
            " このため Item Specifics の反映は一時的に無効化されています "
            "(編集自体は可能。ページを再読み込みして再取得すると再度有効化されます)。"
        )
    # MED (2026-07-04 Codex): multi-value aspect (同一 Name に複数 Value) を持つ
    # listing は、baseline dict[Name]=先頭値 で保持しているため、そのまま
    # replace_all=True で送ると追加値が消える。データモデル拡張 (dict[Name]=list) は
    # Phase3 に回し、ここでは反映を抑止する (最小対応)。編集は許可する
    # (user が全部手動で再入力すれば理論上反映可能だが、Phase3 まで案内のみ)。
    if _snap_ok and _multi_value_names:
        st.warning(
            "⚠️ 複数値項目 (multi-value aspect) を検出しました: "
            f"{', '.join(_multi_value_names)}。"
            " この listing の Item Specifics は現在の実装では追加値が失われる恐れがあるため、"
            "反映を一時的に無効化しています (編集自体は可能、Phase3 で対応予定)。"
        )

    cur_key = pf_key(eid, "item_specifics_current")
    seed_session_value(st.session_state, cur_key, dict(specifics_initial))
    _current = st.session_state.get(cur_key) or {}

    _rows = [{"項目名": str(k), "値": str(v)} for k, v in _current.items()]
    _edited_rows = st.data_editor(
        _rows,
        column_config={
            "項目名": st.column_config.TextColumn("項目名", width="medium"),
            "値": st.column_config.TextColumn("値", width="large"),
        },
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        key=pf_key(eid, "dataeditor_item_specifics"),
    )
    _new_specifics: dict = {}
    for _r in _edited_rows:
        _name = str(_r.get("項目名") or "").strip()
        if not _name:
            continue
        _new_specifics[_name] = str(_r.get("値") or "").strip()
    st.session_state[cur_key] = _new_specifics
    st.caption(f"{len(_new_specifics)} 項目 (行を追加/削除して編集可)")

    # HIGH (2026-07-04 Codex): 項目数が減っている時は削除意図の確認 warning。
    # 削除は eBay 側で該当 aspect が消えるため、AI 生成 auto-set の merge 化 (下記
    # 追加コメント参照) と併せて「意図しない消失」の見落としを二重に防ぐ。
    if len(_new_specifics) < len(specifics_initial or {}):
        _missing = [
            n for n in (specifics_initial or {}) if n not in _new_specifics
        ]
        st.warning(
            "⚠️ Item Specifics の項目数が減少しています "
            f"(現在値 {len(specifics_initial or {})} → 編集後 {len(_new_specifics)}"
            f"、削除された Name: {', '.join(_missing[:5])}"
            + (" …" if len(_missing) > 5 else "")
            + ")。反映すると eBay 側の該当項目が削除されます。意図した変更か確認してください。"
        )

    _has_multivalue = bool(_snap_ok and _multi_value_names)
    fields["item_specifics"] = {
        "before": specifics_initial,
        "after": _new_specifics,
        # H2 (2026-07-04 T3): baseline 失敗時 / MED (2026-07-04 Codex): multi-value
        # aspect 検出時のどちらかで dispatch を抑止する
        # (`_apply_content_changes` / dirty カウント表示側で参照)。
        "dispatch_disabled": (not _snap_ok) or _has_multivalue,
        "dispatch_disabled_reason": (
            "現行 Item Specifics を取得できないため全置換を保留 "
            "(既存の Item Specifics を消さないよう安全側)"
            if not _snap_ok
            else (
                f"複数値項目 ({', '.join(_multi_value_names)}) の追加値が消える"
                "恐れがあるため保留 (Phase3 で対応予定)"
                if _has_multivalue else None
            )
        ),
    }


# =============================================================================
# 画像フィールド (3 モード常時表示、URL 未解決時は入力欄を提供)
# モックアップ tab_t1「コンテンツ」→「画像」ブロックに対応
# =============================================================================

def _render_image_field(
    eid: str,
    row: dict,
    *,
    candidate_id: Optional[int],
    candidate_url: Optional[str],
) -> None:
    """画像フィールド (常時 3 モード radio、URL 未解決時は入力欄を提供).

    モックアップ .field > label='画像' + imgmode-select (①/②/③) + img-mode-panel に対応。
    URL 解決順は description と同じ (candidate_url > row.source_url > user 入力)。

    実描画は既存 `_supplier_photo_pipeline.render_supplier_photo_apply_section`
    へ委譲 (3 モード radio + mode 別 UI + 反映は同関数内で完結。設計書§4 + task
    指示「画像はセクション内の即時完結」)。URL が空文字でも渡せる (各モードが
    自前で「画像取得できません」の error を出す = fail-loud、Q0)。ただし
    task 指示に沿って URL 未解決時は section 上部に入力欄を出して user が URL を
    与えられるようにする (candidate 由来 URL が無い商品でも運用可能)。
    """
    st.markdown("**画像**")
    st.caption(
        "💡 画像はこのセクション内の「eBay に反映」ボタンで即時完結します"
        "(下の「🚀 eBay へ反映」一括ボタンの対象外)。"
    )

    img_url_key = pf_key(eid, "img_source_url")
    _prefilled_url = (candidate_url or row.get("source_url") or "").strip()

    if not _prefilled_url:
        # モックの「(仕入先 URL 未指定時は入力欄)」— 入力後 rerun で下流に伝播
        st.caption(
            "仕入先 URL 未解決: ①合成 / ②そのまま採用 モードを使うには URL 入力が必要"
            " (③メイン差し替えは URL 無しでも現行 eBay 画像は取得できます)。"
        )
        seed_session_value(st.session_state, img_url_key, "")
        st.text_input(
            "仕入先 URL (任意、モード①②で使用)",
            key=img_url_key,
            placeholder="https://... (candidate_url が未指定の商品向け)",
        )
    _typed_url = (st.session_state.get(img_url_key) or "").strip() if not _prefilled_url else ""
    resolved_url = resolve_source_url(candidate_url, row, _typed_url)

    from tabs._supplier_photo_pipeline import render_supplier_photo_apply_section
    _photo_cid = candidate_id if candidate_id is not None else eid
    # candidate_url= には空文字を渡してもよい (下流の 3 モード renderer が
    # fail-loud で対応。①/② はエラー表示、③は現行 eBay 画像だけ表示される)。
    render_supplier_photo_apply_section(
        candidate_id=_photo_cid,
        candidate_url=resolved_url,
        ebay_item_id=eid,
        candidate_title=row.get("title") or "",
    )


def _sync_description_db(eid: str, description: str) -> None:
    """description の DB 同期 (listing_description 列).

    database.py に専用の `update_ebay_listing_*` 関数が無いため (title/quantity/
    rank には既存関数があるが description は無い)、本ファイル内で直接 UPDATE する。
    scope 制約 (S5 = 新規ファイルのみ) のため database.py への関数追加は見送り、
    Phase 3 の既存ファイル改修時に `update_ebay_listing_description()` として
    昇格させることを推奨する (S6 引き継ぎ事項)。
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET listing_description=? WHERE ebay_item_id=?",
            (description, eid),
        )


def _apply_content_changes(
    eid: str,
    fields: dict[str, dict],
    config: Optional[dict],
    *,
    source_tab: str,
    candidate_id: Optional[int],
) -> None:
    """「🚀 eBay へ反映」ボタン押下時の一括反映処理."""
    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    from monitor.database import (
        update_ebay_listing_condition,
        update_ebay_listing_quantity,
        update_ebay_listing_title,
    )
    from monitor.ebay_client import (
        revise_inventory_quantity,
        revise_item_condition,
        revise_item_description,
        revise_item_title,
    )

    try:
        creds = get_ebay_credentials(config)
    except Exception as e:  # noqa: BLE001 -- credentials 解決の多様な例外を UI に伝える
        st.error(f"credentials 取得エラー: {e}")
        return
    if not ebay_credentials_ok(creds):
        st.error("eBay credentials 未設定です (設定タブ参照)")
        return
    app_id, dev_id = creds["app_id"], creds["dev_id"]
    cert_id, token = creds["cert_id"], creds["user_token"]

    changes: list[dict] = []

    if is_field_dirty("title", fields["title"]["before"], fields["title"]["after"]):
        # M2 fix (2026-07-03 code review MED): revise_item_title / update_ebay_listing_title
        # は内部で strip するが本 apply が渡す raw 値は未 strip のため、監査ログ
        # (dispatch_content_changes の after 引数) と eBay/DB 実値が末尾空白で
        # 乖離し得る。ここで先に strip して 3 者 (eBay/DB/監査ログ) を一致させる。
        _after = (fields["title"]["after"] or "").strip()

        def _apply_title(_after=_after):
            r = revise_item_title(eid, _after, app_id, dev_id, cert_id, token)
            if r.get("success"):
                update_ebay_listing_title(eid, _after)
            return r

        changes.append({
            "field": "title", "before": fields["title"]["before"], "after": _after,
            "apply": _apply_title,
        })

    if is_field_dirty("description", fields["description"]["before"], fields["description"]["after"]):
        _after = fields["description"]["after"]

        def _apply_description(_after=_after):
            r = revise_item_description(eid, _after, app_id, dev_id, cert_id, token)
            if r.get("success"):
                _sync_description_db(eid, _after)
            return r

        changes.append({
            "field": "description", "before": fields["description"]["before"], "after": _after,
            "apply": _apply_description,
        })

    # ── Condition (rank + condition_description) — bundle 送信 ──
    # rank と conddesc は同じ revise_item_condition で送るため、bundle して 1 API 呼出。
    # As-Is 必須ガードは dispatch 直前に validate_as_is_condition_description で。
    # `condition_description` は _render_condition_subblock が set するが、
    # unit test 等が渡さないケースに備え defensive default (`.get()` + 空 dict fallback)。
    _cd_field = fields.get("condition_description") or {"before": "", "after": ""}
    _rank_dirty = is_field_dirty("rank", fields["rank"]["before"], fields["rank"]["after"])
    _cd_dirty = is_field_dirty(
        "condition_description", _cd_field["before"], _cd_field["after"],
    )
    _effective_rank = (
        fields["rank"]["after"] if _rank_dirty else fields["rank"]["before"]
    )
    _effective_cd_after = _cd_field["after"]

    if _rank_dirty or _cd_dirty:
        # As-Is 必須ガード (state 層) — 失敗時は dispatch 全体を中止 (K1: 部分反映しない)
        _guard = validate_as_is_condition_description(_effective_rank, _effective_cd_after)
        if _guard is not None:
            st.error(_guard)
            return

    if _rank_dirty:
        # rank dirty ⇒ conddesc も同時送信 (bundled、二重 API 回避)。
        # conddesc 単独 dirty のケースは次の elif で処理。
        _r_after = fields["rank"]["after"]
        _cond_id = rank_to_condition_id(_r_after)
        # cd を「送るか」の判定: dirty なら新値を送る、そうでなければ None で eBay 側維持
        _cd_to_send = _effective_cd_after if _cd_dirty else None
        _cd_before = _cd_field["before"]

        def _apply_rank(_r_after=_r_after, _cond_id=_cond_id,
                        _cd_to_send=_cd_to_send, _cd_dirty=_cd_dirty,
                        _cd_before=_cd_before):
            if not _cond_id:
                return {"success": False,
                        "message": f"rank={_r_after!r} に対応する ConditionID が不明です"}
            from monitor.ebay_listing_snapshot import fetch_listing_snapshot
            from monitor.listing_content_change_log import log_content_change
            snap_pre = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, token)
            _cd_arg = _cd_to_send if (_cd_to_send or "") else None
            # HIGH-1 (2026-07-04 T3 レビュー): eBay は ConditionID=1000 (新品 N) で
            # ConditionDescription をサポートしない (カテゴリによっては Ack=Failure で
            # ランク変更操作自体が通らない)。N への変更時は CD を絶対に送らない。
            # `RANK_CONDITION_DESCRIPTION_TEMPLATE` からも "N" を除外済みだが、user が
            # 手動入力した CD が dirty で bundle されるケースを apply 層でも遮断する
            # (二段防御、Q0: silent 送信でなく明示的に None 化)。
            if _cond_id == "1000":
                _cd_arg = None
            # 既に同 ConditionID (rank 一致) の時、conddesc 変更が無ければ DB 同期のみ。
            # conddesc dirty なら revise を必ず走らせて eBay に送る (rank 一致でも cd は更新)。
            if (snap_pre.ok
                    and (snap_pre.condition_id or "") == _cond_id
                    and not _cd_dirty):
                update_ebay_listing_condition(
                    eid, ebay_condition_id=_cond_id, condition_rank=_r_after)
                return {"success": True, "message": f"既に {_r_after}({_cond_id}) — DB 同期のみ"}
            r = revise_item_condition(
                eid, _cond_id, app_id, dev_id, cert_id, token,
                condition_description=_cd_arg,
            )
            snap_post = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, token)
            actual = snap_post.condition_id if snap_post.ok else None

            # 監査ログの共通処理 (bundled 送信で conddesc も pushed 済 → 追記)。
            # dispatch_content_changes は本 apply を "rank" として 1 回のみ log するため、
            # conddesc dirty 時は追加で log_content_change(field="condition_description")。
            def _log_bundled_cd_if_dirty(ebay_ack_msg: Optional[str]) -> None:
                if not _cd_dirty:
                    return
                try:
                    log_content_change(
                        eid, "condition_description", _cd_before, _cd_to_send or "",
                        source_tab=source_tab, candidate_id=candidate_id,
                        success=True, ebay_ack=ebay_ack_msg,
                    )
                except Exception:  # noqa: BLE001 -- 監査ログ失敗で reviseの結果を握り潰さない
                    logger.exception("condition_description 追加監査ログ失敗 eid=%s", eid)

            if actual == _cond_id:
                update_ebay_listing_condition(
                    eid, ebay_condition_id=_cond_id, condition_rank=_r_after)
                _log_bundled_cd_if_dirty(r.get("message"))
                return {"success": True,
                        "message": f"Condition を {_r_after}({_cond_id}) に反映"
                                    + (" + 理由も更新" if _cd_dirty else "")}

            # W220 regression fix (2026-07-03 code review MED): S(1500) verify 失敗時の
            # 3000(Used) 自動降格 fallback を復元。旧 tab_product_management.py:4310-4327 の
            # 挙動 (CLAUDE.md「Cond ID 1500 はカテゴリ依存」規約に基づく降格) がランク編集の
            # パネル移設で失われていた。K2: 旧実装を忠実に移植 (改良しない、silent 降格禁止)。
            if _cond_id == "1500":
                _rf = revise_item_condition(
                    eid, "3000", app_id, dev_id, cert_id, token,
                    condition_description=_cd_arg,
                )
                snap3 = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, token)
                if snap3.ok and snap3.condition_id == "3000":
                    # eBay 実値は 3000(Used)。user 意図"S"は実態と乖離するので
                    # condition_rank に "S" を残さない: ebay_condition_id のみ実値
                    # 同期 (user が後で Used サブランクを別途指定)。
                    update_ebay_listing_condition(eid, ebay_condition_id="3000")
                    # 監査ログの after は実際に適用された 3000 で記録 (silent 降格禁止 = Q0)。
                    # dispatch_content_changes は "rank" を _r_after で log するが、それは
                    # user 意図値。追加で「実適用=3000」を残すため、bundled cd log と同じ
                    # ルートで rank 実適用を明示 log する。
                    try:
                        log_content_change(
                            eid, "rank", fields["rank"]["before"], "Used(3000, S降格)",
                            source_tab=source_tab, candidate_id=candidate_id,
                            success=True,
                            ebay_ack=(
                                f"S(1500) 不可カテゴリのため 3000 へ降格: "
                                f"{_rf.get('message', '不明')}"
                            ),
                        )
                    except Exception:  # noqa: BLE001 -- log 失敗で反映結果を握り潰さない
                        logger.exception("S→3000 降格の監査ログ追記失敗 eid=%s", eid)
                    _log_bundled_cd_if_dirty(_rf.get("message"))
                    return {
                        "success": True,
                        "message": (
                            "Condition: S(新品同様)はこのカテゴリで不可のため "
                            "Used(3000) で反映しました "
                            "(Used サブランクは別途指定してください)"
                            + ("　※ 理由も更新済" if _cd_dirty else "")
                        ),
                    }
                return {
                    "success": False,
                    "message": (
                        f"Condition 反映失敗 (S=1500 不可・3000 fallback も失敗): "
                        f"{r.get('message', '不明')}"
                    ),
                }
            return {
                "success": False,
                "message": f"Condition 反映 verify 失敗 (実値={actual}): {r.get('message', '不明')}",
            }

        changes.append({
            "field": "rank", "before": fields["rank"]["before"], "after": _r_after,
            "apply": _apply_rank,
        })
    elif _cd_dirty:
        # rank 未変更・conddesc のみ dirty ⇒ 現行 ConditionID を保って cd のみ更新。
        _cd_after = _effective_cd_after
        _cd_before = _cd_field["before"]

        def _apply_cd_only(_cd_after=_cd_after):
            from monitor.ebay_listing_snapshot import fetch_listing_snapshot
            snap = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, token)
            if not snap.ok or not (snap.condition_id or "").strip():
                return {
                    "success": False,
                    "message": f"現行 ConditionID を取得できません: {snap.error or '不明'}",
                }
            _cur_cid = str(snap.condition_id).strip()
            # HIGH-1 (2026-07-04 T3 レビュー): ConditionID=1000 (新品 N) は eBay 仕様上
            # ConditionDescription 非対応。カテゴリによっては Ack=Failure で通らず、
            # 通ってもゴミデータ残存リスクがあるため、cd 単独 dirty でも N の listing
            # には送らず「送信対象なし」として success 扱いにする (user 意図は「cd を
            # 消したい/直したい」だが N では eBay 側に反映できないため、DB 側の
            # session_state だけ後段 mark_field_synced で追従させる)。
            if _cur_cid == "1000":
                return {
                    "success": True,
                    "message": (
                        "新品 (ConditionID 1000) は eBay 仕様上 ConditionDescription を "
                        "受け付けないため送信をスキップしました (現在値を維持)"
                    ),
                }
            r = revise_item_condition(
                eid, _cur_cid, app_id, dev_id, cert_id, token,
                condition_description=(_cd_after or None),
            )
            if r.get("success"):
                return {"success": True,
                        "message": f"コンディション理由を更新 (ConditionID {_cur_cid} 維持)"}
            return {"success": False,
                    "message": f"理由更新失敗: {r.get('message', '不明')}"}

        changes.append({
            "field": "condition_description",
            "before": _cd_before, "after": _cd_after,
            "apply": _apply_cd_only,
        })

    # ── Item Specifics (#44、動的判断: DISPATCH_FIELD_ORDER には含めず condition_
    #    description と同様 dirty 時のみ register)。before/after は監査ログ
    #    (listing_content_change_log) が str のみを想定するため JSON 文字列化し、
    #    baseline 同期用の生 dict は raw_after_by_field に別途保持する。
    raw_after_by_field: dict = {}
    _spec_field = fields.get("item_specifics") or {"before": {}, "after": {}}
    _spec_before = _spec_field.get("before") or {}
    _spec_after = _spec_field.get("after") or {}
    _spec_dirty = is_field_dirty("item_specifics", _spec_before, _spec_after)
    if _spec_dirty and _spec_field.get("dispatch_disabled"):
        # H2 最終ガード (2026-07-04 T3 レビュー残課題): baseline (GetItem) 取得失敗時は
        # apply_item_specifics_to_ebay (replace_all=True) を絶対に呼ばない。UI 側
        # (反映ボタンの活性化 / 件数カウント) は既に対応済みだが、ボタンだけに頼らず
        # apply 関数自体にも防御を入れる (Q0: 呼出経路が増えても事故らない多層防御)。
        st.warning(
            "⚠ Item Specifics は現行値取得に失敗しているため反映をスキップしました "
            f"({_spec_field.get('dispatch_disabled_reason') or '理由不明'})。"
            " 他フィールドは通常どおり反映します。"
        )
    elif _spec_dirty:
        raw_after_by_field["item_specifics"] = _spec_after

        def _apply_item_specifics(_spec_after=_spec_after):
            r = apply_item_specifics_to_ebay(
                eid, _spec_after,
                app_id=app_id, dev_id=dev_id, cert_id=cert_id, user_token=token,
            )
            _removed = r.get("removed_names") or []
            if _removed:
                r = dict(r)
                r["message"] = (
                    (r.get("message") or "") + f" (自動除去: {', '.join(_removed)})"
                ).strip()
            return r

        changes.append({
            "field": "item_specifics",
            "before": json.dumps(_spec_before, ensure_ascii=False),
            "after": json.dumps(_spec_after, ensure_ascii=False),
            "apply": _apply_item_specifics,
        })

    if is_field_dirty("quantity", fields["quantity"]["before"], fields["quantity"]["after"]):
        _after = fields["quantity"]["after"]

        def _apply_quantity(_after=_after):
            r = revise_inventory_quantity(eid, int(_after), app_id, dev_id, cert_id, token)
            if r.get("success"):
                update_ebay_listing_quantity(eid, int(_after))
            return r

        changes.append({
            "field": "quantity", "before": fields["quantity"]["before"], "after": _after,
            "apply": _apply_quantity,
        })

    if not changes:
        st.info("反映対象の変更がありません。")
        return

    after_by_field = {c["field"]: c["after"] for c in changes}
    with st.spinner(f"{len(changes)} 件を eBay へ反映中..."):
        results = dispatch_content_changes(
            eid, changes, source_tab=source_tab, candidate_id=candidate_id,
        )

    any_success = False
    for field, res in results.items():
        label = FIELD_LABELS_JA.get(field, field)
        if res["success"]:
            st.success(f"{label}: {res['message'] or '反映しました'}")
            # item_specifics は監査ログ用に JSON 文字列化した after を dispatch したが、
            # baseline (dirty 判定用) は生 dict でないと次回描画の比較が壊れるため
            # raw_after_by_field を優先する (無ければ従来通り after_by_field)。
            _sync_value = raw_after_by_field.get(field, after_by_field[field])
            mark_field_synced(st.session_state, eid, field, _sync_value)
            any_success = True
        else:
            st.error(f"{label}: {res['message'] or '反映に失敗しました'}")

    # M1 fix (2026-07-03 code review MED): DB を更新した場合は ui_cache の
    # db_version を bump し、外側 (tab_product_management 等) の
    # `_cd_fetch_all_products` cache を無効化する。bump しないと反映後も
    # 商品一覧が旧値を表示し続ける (既存編集ゾーンの流儀、
    # tab_product_management.py L3943 付近と同じパターン)。全失敗時は DB は
    # 変わっていないため bump しない。
    if any_success:
        from ui_cache import bump_db_version
        bump_db_version()

    st.rerun(scope="fragment")


# =============================================================================
# 仕入先グループ / 価格・送料 zone (2026-07-03 現在 panel から呼ばれていない、
# Phase 3 の再利用余地を残すため関数体は温存)
# =============================================================================

def _render_supplier_group(row: dict, candidate_url: Optional[str]) -> None:
    url = candidate_url or row.get("source_url") or ""
    if url:
        st.markdown(f"仕入先 URL: [{url}]({url})")
    else:
        st.caption("仕入先 URL は未設定です。")
    st.caption("💡 仕入先 URL の編集機能は Phase 3 で対応予定です (現時点は表示のみ)。")


# =============================================================================
# 価格・送料 (T3 隔離)
# =============================================================================

def _render_money_zone(eid: str) -> None:
    st.markdown(
        '<span class="pf-money-note">⚠️ 誤操作防止のため、価格・送料の変更は'
        'このパネルには含まれません。</span>',
        unsafe_allow_html=True,
    )
    st.caption("商品管理タブの従来フォーム (2段確認) から変更してください。")
    if st.button("📝 商品管理タブで価格・送料を編集", key=pf_key(eid, "goto_pm")):
        # W292 jump 流儀 (tab_today_tasks.py L578-581) を踏襲。
        st.session_state["pm_focus_eid"] = eid
        st.session_state["_w134_sel"] = "商品管理"
        st.session_state["_w217a_cat_view"] = "★ 毎日"
        # fragment 内での st.rerun() は明示 scope 指定が確実 (Streamlit バージョン依存の
        # default 挙動に頼らない、W174-pm test_accept_button_uses_app_scope_rerun と同方針)。
        # 商品管理タブへ完全ナビゲートするため app scope (fragment scope だとタブ切替が
        # 反映されない)。
        st.rerun(scope="app")
