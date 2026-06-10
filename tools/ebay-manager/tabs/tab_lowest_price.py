#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最安値チェック (ライバル登録 + 自動値下げ) タブ (W221 Tier2 抽出、2026-06-04)。

app.py の `if _w134_sel == "最安値チェック":` 分岐 body をそのまま移植。挙動不変 (K2 surgical)。
同梱ヘルパー (app.py top-level から移動、単一タブ専用): _cd_listings_by_rank, _cd_market_displays, _cd_competitors_grouped
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd
import streamlit as st


@st.cache_data(ttl=3, show_spinner=False)
def _cd_listings_by_rank(db_version: int, order_by_rank: bool):
    from monitor.database import get_ebay_listings_by_rank
    return get_ebay_listings_by_rank(order_by_rank=order_by_rank)


@st.cache_data(ttl=3, show_spinner=False)
def _cd_market_displays(db_version: int, ids: tuple):
    from monitor.lowest_price import get_listing_market_displays
    return get_listing_market_displays(list(ids))


@st.cache_data(ttl=3, show_spinner=False)
def _cd_competitors_grouped(db_version: int, ids: tuple):
    from monitor.lowest_price import get_competitors_grouped
    return get_competitors_grouped(list(ids))


def render_lowest_price_tab(s: dict) -> None:
    # W221 Tier2 fix (2026-06-05): app.py top-level import をグローバル参照していた
    # 名前を関数内 lazy import で補完 (抽出漏れ修正、render 実行時 NameError 防止)。
    import json
    from monitor.database import get_japan_competitor_alerts, update_alert_action
    from ui_cache import bump_db_version, get_db_version, seed_keyed_value_from_db
    st.title("最安値チェック")
    st.caption(
        "商品ごとに最大 10 ライバルを登録し、6 時間ごと (00:45 / 06:45 / 12:45 / 18:45 JST) に "
        "ライバル最安より 1 セント安く自動値下げします。1 listing につき 1 日 4 回まで、"
        "min_price (未設定時は損益分岐) を下回る場合は skip します。"
    )

    from monitor.lowest_price import (
        upsert_listing_competitors,
        set_listing_lowest_price_fields,
        update_listing_breakeven,
        get_competitors_grouped,
        get_competitors_with_pricing,
        fetch_alert_shipping_usd,
        refresh_competitor_pricing,
        fetch_supplier_purchase_yen,
        get_listing_market_displays,
        get_price_change_log,
        count_today_price_changes_jst,
    )

    _LP_MAX_COMPETITORS = 10  # 1 商品あたりライバル上限

    # config 読み込み (Browse API credentials 用)
    _lp_cfg: dict = {}
    _lp_cfg_path = Path(__file__).resolve().parent.parent / 'config' / 'schedule_config.json'
    if _lp_cfg_path.exists():
        try:
            with open(_lp_cfg_path, 'r', encoding='utf-8') as _cf:
                _lp_cfg = json.load(_cf)
        except Exception:
            _lp_cfg = {}

    # ── W119: 商品データ FIX (per-listing 編集) ──
    try:
        from tabs.tab_data_fix import render_data_fix
        render_data_fix(_lp_cfg)
    except Exception as _e:
        st.warning(f"商品データ FIX 描画エラー: {_e}")

    # ── W119: 商品リサーチ自動化 wizard (最安値チェックの前段) ──
    try:
        from tabs.tab_research_wizard import render_research_wizard
        render_research_wizard(_lp_cfg)
    except Exception as _e:
        st.warning(f"商品リサーチ wizard 描画エラー: {_e}")

    # ── 出品中の商品取得 ──
    _lp_my_items = _cd_listings_by_rank(get_db_version(), True)
    _lp_active = [it for it in _lp_my_items if not it.get('is_ended', 0)]
    _lp_items_map = {it['ebay_item_id']: it for it in _lp_my_items}

    if not _lp_active:
        st.info("出品中の商品がありません。")
    else:
        _lp_our_ids = [it['ebay_item_id'] for it in _lp_active]
        # 区分 (final/proposed/analysis 優先) と ライバル件数を一括取得
        _lp_market_map = _cd_market_displays(get_db_version(), tuple(_lp_our_ids))
        _lp_grouped = _cd_competitors_grouped(get_db_version(), tuple(_lp_our_ids))

        # ───────────────────────────────────
        # サマリ
        # ───────────────────────────────────
        _lp_metrics_cols = st.columns(5)
        with _lp_metrics_cols[0]:
            st.metric("商品", len(_lp_active))
        with _lp_metrics_cols[1]:
            _lp_total_competitors = sum(len(v) for v in _lp_grouped.values())
            st.metric("登録ライバル", _lp_total_competitors)
        with _lp_metrics_cols[2]:
            _lp_with_purchase = sum(1 for it in _lp_active if it.get('purchase_yen'))
            st.metric("仕入価格設定済", f"{_lp_with_purchase} / {len(_lp_active)}")
        with _lp_metrics_cols[3]:
            _lp_with_market = sum(1 for v in _lp_market_map.values() if v != '-')
            st.metric("区分判定済", f"{_lp_with_market} / {len(_lp_active)}")
        with _lp_metrics_cols[4]:
            # W184 (L6): 新規発見アラート未対応件数 (Discord ではなく本タブで処理)
            try:
                _lp_pending_alerts = get_japan_competitor_alerts(action="pending")
                _lp_pending_count = len(_lp_pending_alerts)
            except Exception:
                _lp_pending_count = 0
            st.metric("新規アラート", _lp_pending_count, help="本タブ下段の「新規発見ライバル」で処理")

        # ───────────────────────────────────
        # 1. 商品一覧 (read-only 一覧表)
        # ───────────────────────────────────
        st.subheader("商品一覧")
        st.caption("各行は商品 1 件。詳細編集は下の「商品の詳細・編集」セクションへ。")

        _lp_summary_rows = []
        for _it in _lp_active:
            _ebid = _it['ebay_item_id']
            _comps_count = len(_lp_grouped.get(_ebid, []))
            _lp_summary_rows.append({
                'ebay_item_id': _ebid,
                'market': _lp_market_map.get(_ebid, '-'),
                'title': (_it.get('title') or '')[:50],
                'current_price': float(_it.get('current_price') or 0),
                'shipping_cost': float(_it.get('shipping_cost') or 0),
                'purchase_yen': _it.get('purchase_yen'),
                'lp_breakeven_usd': _it.get('lp_breakeven_usd'),
                'lp_min_price': _it.get('lp_min_price'),
                'competitors': f"{_comps_count} / {_LP_MAX_COMPETITORS}",
            })
        _lp_summary_df = pd.DataFrame(_lp_summary_rows)

        st.dataframe(
            _lp_summary_df,
            column_config={
                'ebay_item_id': st.column_config.TextColumn('item id', width='small'),
                'market': st.column_config.TextColumn('区分', width='small'),
                'title': st.column_config.TextColumn('商品名', width='medium'),
                'current_price': st.column_config.NumberColumn('現在価格', format='$%.2f', width='small'),
                'shipping_cost': st.column_config.NumberColumn('送料', format='$%.2f', width='small'),
                'purchase_yen': st.column_config.NumberColumn('仕入価格', format='¥%.0f', width='small'),
                'lp_breakeven_usd': st.column_config.NumberColumn('最低利益価格', format='$%.2f', width='small'),
                'lp_min_price': st.column_config.NumberColumn('最低価格(下限)', format='$%.2f', width='small'),
                'competitors': st.column_config.TextColumn('ライバル', width='small'),
            },
            hide_index=True,
            use_container_width=True,
            height=400,
        )

        # ───────────────────────────────────
        # 2. 商品の詳細・編集 (1 商品ずつ)
        # ───────────────────────────────────
        st.divider()
        st.subheader("商品の詳細・編集")

        # 商品選択 selectbox
        _lp_select_options = [it['ebay_item_id'] for it in _lp_active]

        def _lp_format_select(eid: str) -> str:
            it = _lp_items_map.get(eid, {})
            t = (it.get('title') or '')[:50] or '(no title)'
            return f"{t} ({eid})"

        _lp_selected_id = st.selectbox(
            "商品を選択",
            options=_lp_select_options,
            format_func=_lp_format_select,
            key='lp_selected_listing',
        )

        if _lp_selected_id:
            _lp_sel = _lp_items_map[_lp_selected_id]
            _lp_sel_sku = _lp_sel.get('sku', '')
            _lp_is_no_stock = _lp_sel_sku.startswith('ebay')

            # 基本情報 (read-only)
            _lp_info_cols = st.columns(5)
            with _lp_info_cols[0]:
                st.markdown(f"**item id**  \n`{_lp_selected_id}`")
            with _lp_info_cols[1]:
                st.markdown(f"**区分**  \n{_lp_market_map.get(_lp_selected_id, '-')}")
            with _lp_info_cols[2]:
                st.markdown(f"**現在価格**  \n${float(_lp_sel.get('current_price') or 0):.2f}")
            with _lp_info_cols[3]:
                st.markdown(f"**送料**  \n${float(_lp_sel.get('shipping_cost') or 0):.2f}")
            with _lp_info_cols[4]:
                st.markdown(f"**SKU**  \n`{_lp_sel_sku}`")

            st.markdown(f"**商品名**: {_lp_sel.get('title', '')}")

            # 仕入価格 + 最低利益価格 (read-only 表示) + 仕入価格自動取得 button
            # H6 fix: None と 0 を区別 (number_input value=None で未入力状態)
            # 2026-05-10: 最低利益価格を最低価格 (下限) 入力欄の **真上** に配置し、
            # user が下限値決定時に必ず breakeven を見てから入力できる動線に改修.
            _lp_pyen_cols = st.columns([2, 2, 1])
            with _lp_pyen_cols[0]:
                _lp_pyen_default = (
                    int(_lp_sel['purchase_yen'])
                    if _lp_sel.get('purchase_yen') is not None
                    else None
                )
                # ③同型 scalar 修正 (Codex 監査 HIGH): keyed number_input の
                # value= は session_state 既出後無視される。別経路 (仕入価格
                # 自動取得ボタン/supplier sweep) で DB が変わった後、stale な
                # 旧値のまま保存すると W183 赤字防止 floor が古値へ巻戻る。
                # DB 値 signature で session_state を再シード (value= 撤去)。
                seed_keyed_value_from_db(
                    st.session_state, f"lp_pyen_{_lp_selected_id}",
                    f"_lp_pyen_sig_{_lp_selected_id}", _lp_pyen_default,
                )
                _lp_pyen_input = st.number_input(
                    "仕入価格 (JPY)",
                    min_value=0,
                    step=100,
                    key=f"lp_pyen_{_lp_selected_id}",
                    help="無在庫商品は仕入先 URL から scrape 自動取得可能 (右ボタン、既存値を上書き)。"
                )
            with _lp_pyen_cols[1]:
                # 最低利益価格 (read-only、最低価格入力直前に表示)
                _lp_be = _lp_sel.get('lp_breakeven_usd')
                if _lp_be:
                    _lp_be_label = f"💡 最低利益価格 (赤字境界): **${_lp_be:.2f}**"
                    _lp_be_caption = (
                        f"仕入¥{_lp_pyen_default or 0:,} / 重量"
                        f"{int(_lp_sel.get('weight_g') or 0)}g / DDP US 想定で算出。"
                        f"これ以下で売れたら粗利マイナス。"
                    )
                else:
                    _lp_be_label = "💡 最低利益価格: 未計算"
                    _lp_be_caption = "仕入価格 + 重量を入れて保存すると自動計算"
                st.markdown(_lp_be_label)
                st.caption(_lp_be_caption)

                _lp_minp_default = (
                    float(_lp_sel['lp_min_price'])
                    if _lp_sel.get('lp_min_price') is not None
                    else None
                )
                # ③同型 scalar 修正 (Codex 監査 HIGH): 下限価格も同様に
                # stale 保存で W183 floor が巻戻るため DB signature 再シード。
                seed_keyed_value_from_db(
                    st.session_state, f"lp_minp_{_lp_selected_id}",
                    f"_lp_minp_sig_{_lp_selected_id}", _lp_minp_default,
                )
                _lp_minp_input = st.number_input(
                    "最低価格 (USD、下限)",
                    min_value=0.0,
                    step=1.0,
                    format="%.2f",
                    key=f"lp_minp_{_lp_selected_id}",
                    help=(
                        "自動値下げの絶対下限。最低利益価格 (上記💡) "
                        "以上を推奨。未入力なら最低利益価格が自動 floor として使われる。"
                    )
                )
                # breakeven 未満を入力した場合の警告
                if (_lp_minp_input is not None and _lp_minp_input > 0
                        and _lp_be and _lp_minp_input < _lp_be):
                    st.warning(
                        f"⚠️ 入力値 ${_lp_minp_input:.2f} は最低利益価格 ${_lp_be:.2f} "
                        f"未満です。値下げ後赤字になる可能性。意図的なら OK。"
                    )
            with _lp_pyen_cols[2]:
                st.write("")  # 縦位置調整
                if _lp_is_no_stock:
                    if st.button("仕入価格を自動取得", key=f"lp_fetch_pyen_{_lp_selected_id}"):
                        # H4 fix: spinner で 15 秒の進捗を可視化
                        with st.spinner("仕入先サイトから価格取得中... (最大 15 秒)"):
                            try:
                                _fetched = fetch_supplier_purchase_yen(_lp_selected_id)
                                if _fetched is None:
                                    st.error("取得失敗 (URL / scrape エラー)")
                                else:
                                    st.success(f"仕入価格 ¥{_fetched:,} を保存")
                                    _lp_calc_settings = dict(s)
                                    update_listing_breakeven(_lp_selected_id, _lp_calc_settings)
                                    bump_db_version()  # W134 Step2: 書込後 read-cache 無効化
                                    st.rerun()
                            except Exception as e:
                                st.error(f"取得エラー: {e}")
                else:
                    st.caption("(在庫品)")

            # ライバル × 10 入力
            st.markdown("**ライバル (item id)**")
            st.caption(
                "eBay item id (12 桁前後の数字) を入力。空にすると登録解除。"
                "保存後、下の「ライバル価格を再取得」で価格・送料を取得します"
                "(ウィザード経由の登録は登録時に自動取得)。"
            )
            _lp_existing = _lp_grouped.get(_lp_selected_id, [])
            # ③ データ損失ホットフィックス (2026-05-18): Streamlit は key 付き
            # text_input の value= を「その key が session_state に既出の後」は
            # 無視する. その結果、一括検索等で DB 登録された競合が #1-#10 欄に
            # 出ず、空欄のまま「保存」→ upsert_listing_competitors の全置換で
            # **登録済み競合が黙って全消滅** していた (W183 追従対象の損失).
            # 対策: DB の競合 id 集合を signature 化し、変化時のみ session_state
            # を DB 値で再シード (= 表示が常に DB と一致 → 全置換が安全化).
            # plain rerun では signature 不変 = 再シードせず user 入力途中を温存.
            # 既知トレードオフ (Q0 透明性 / 2段review MEDIUM): #1-#10 編集途中に
            # 別経路 bulk 登録が走り DB 集合が変わると signature 変化で再シード =
            # user 未保存編集が DB 値で上書きされる. これは「登録済み競合の silent
            # 全消滅 (W183 追従対象の恒久損失=金銭直結)」回避を優先した許容判断.
            # ★往復バグ修正 (2026-05-18 Q1 Playwright で検出): Streamlit は
            # 描画されなかった keyed widget の session_state を破棄するが、本
            # signature は plain key なので破棄されず残存する. listing を切替えて
            # 戻ると widget state はクリア済なのに signature だけ残り「一致」判定
            # で再シードされず #1-#10 が空欄化 → その保存で全消滅が再発する.
            # → widget key 自体の不在 (= 前 run で未描画 = 切替で破棄された) も
            # 再シード条件に含める (初回描画も key 不在なので自然に seed される).
            _lp_comp_sig_key = f"_lp_comp_loaded_sig_{_lp_selected_id}"
            _lp_db_sig = tuple(_lp_existing)
            _lp_widget_state_present = (
                f"lp_comp_{_lp_selected_id}_0" in st.session_state
            )
            if (not _lp_widget_state_present
                    or st.session_state.get(_lp_comp_sig_key) != _lp_db_sig):
                for _i in range(_LP_MAX_COMPETITORS):
                    st.session_state[f"lp_comp_{_lp_selected_id}_{_i}"] = (
                        _lp_existing[_i] if _i < len(_lp_existing) else ''
                    )
                st.session_state[_lp_comp_sig_key] = _lp_db_sig
            _lp_comp_cols_a = st.columns(5)
            _lp_comp_cols_b = st.columns(5)
            _lp_comp_inputs: list[str] = []
            for _i in range(_LP_MAX_COMPETITORS):
                _col = _lp_comp_cols_a[_i] if _i < 5 else _lp_comp_cols_b[_i - 5]
                with _col:
                    # value= は渡さない: session_state[key] を唯一の真実源とする
                    # (value= と session_state 併用は Streamlit が警告を出す).
                    _lp_comp_inputs.append(
                        st.text_input(
                            f"#{_i + 1}",
                            key=f"lp_comp_{_lp_selected_id}_{_i}",
                            placeholder="285123456789",
                            label_visibility='visible',
                        )
                    )

            # ライバル価格・送料 一覧
            st.markdown("**ライバル価格・送料**")
            _lp_pricing_rows = get_competitors_with_pricing(_lp_selected_id)
            if not _lp_pricing_rows:
                st.caption("登録ライバルなし")
            else:
                # H2 fix: LinkColumn を正しく URL 列で機能させる
                _lp_pricing_df = pd.DataFrame([
                    {
                        'item id': r['competitor_item_id'],
                        'リンク': f"https://www.ebay.com/itm/{r['competitor_item_id']}",
                        '商品価格': r['price_usd'] if r['price_usd'] is not None else None,
                        '送料': r['shipping_usd'] if r['shipping_usd'] is not None else None,
                        '合計': r['total_usd'] if r['total_usd'] is not None else None,
                        '最終取得': r['last_priced_at'] or '-',
                    }
                    for r in _lp_pricing_rows
                ])
                st.dataframe(
                    _lp_pricing_df,
                    column_config={
                        'item id': st.column_config.TextColumn('item id', width='small'),
                        'リンク': st.column_config.LinkColumn(
                            'リンク', display_text='開く', width='small',
                            help="クリックで eBay 商品ページ",
                        ),
                        '商品価格': st.column_config.NumberColumn('商品価格', format='$%.2f', width='small'),
                        '送料': st.column_config.NumberColumn('送料', format='$%.2f', width='small'),
                        '合計': st.column_config.NumberColumn('合計', format='$%.2f', width='small'),
                        '最終取得': st.column_config.TextColumn('最終取得', width='medium'),
                    },
                    hide_index=True,
                    use_container_width=True,
                )

            # 操作ボタン群
            _lp_action_cols = st.columns([1, 1, 2])
            with _lp_action_cols[0]:
                if st.button("保存", type='primary', key=f"lp_save_{_lp_selected_id}"):
                    try:
                        _lp_calc_settings = dict(s)
                        # H6 fix: None と 0 を区別 (0 も有効値として保存)
                        # H-A3 fix: purchase_yen は INTEGER 列、int で揃える
                        _new_pyen = (
                            int(_lp_pyen_input) if _lp_pyen_input is not None else None
                        )
                        _new_minp = (
                            float(_lp_minp_input) if _lp_minp_input is not None else None
                        )
                        set_listing_lowest_price_fields(
                            _lp_selected_id, _new_pyen, _new_minp
                        )
                        # 仕入価格が変わったら breakeven 再計算 (型揃えて比較)
                        _orig_pyen = (
                            int(_lp_sel['purchase_yen'])
                            if _lp_sel.get('purchase_yen') is not None
                            else None
                        )
                        if _new_pyen != _orig_pyen:
                            update_listing_breakeven(_lp_selected_id, _lp_calc_settings)
                        # ライバル更新
                        upsert_listing_competitors(_lp_selected_id, _lp_comp_inputs)
                        bump_db_version()  # W134 Step2: 書込後 read-cache 無効化
                        st.success("保存しました")
                        st.rerun()
                    except Exception as e:
                        st.error(f"保存エラー: {e}")
                        import logging as _lp_lg
                        _lp_lg.getLogger(__name__).exception("最安値チェック 保存処理失敗")

            with _lp_action_cols[1]:
                if st.button(
                    "ライバル価格を再取得",
                    key=f"lp_refresh_pricing_{_lp_selected_id}"
                ):
                    # H4 fix: spinner で進捗を可視化 (10 件 × Browse API ~5-10 秒)
                    with st.spinner("ライバル価格を Browse API から取得中..."):
                        try:
                            result = refresh_competitor_pricing(_lp_selected_id, _lp_cfg)
                            if result['fetched'] == 0 and result['failed'] == 0:
                                st.info("登録ライバルなし")
                            else:
                                st.success(
                                    f"取得成功 {result['fetched']} 件 / 失敗 {result['failed']} 件"
                                )
                                st.rerun()
                        except Exception as e:
                            st.error(f"取得エラー: {e}")

            # ───────────────────────────────────
            # W183: 今すぐ値下げ + 値下げ履歴
            # ───────────────────────────────────
            st.divider()
            _w183_today_count = count_today_price_changes_jst(_lp_selected_id)
            _w183_cap = 4  # L2: 1 日 4 回
            _w183_remaining = max(0, _w183_cap - _w183_today_count)
            st.markdown(
                f"**値下げ実行 (W183)**  \n"
                f"本日 (JST) {_w183_today_count} / {_w183_cap} 回実行済 "
                f"(残り {_w183_remaining} 回)"
            )
            _w183_btn_cols = st.columns([2, 5])
            with _w183_btn_cols[0]:
                _w183_disabled = (_w183_remaining <= 0)
                if st.button(
                    "今すぐ値下げ",
                    key=f"w183_revise_{_lp_selected_id}",
                    type='primary',
                    disabled=_w183_disabled,
                    help=(
                        "ライバル最安より $0.01 安く値下げ。"
                        "min_price 下限・本日 4 回上限を尊重。"
                    ),
                ):
                    with st.spinner("ReviseFixedPriceItem 実行中..."):
                        try:
                            from tasks.task_rival_pricing import _evaluate_and_apply_one
                            _w183_result = _evaluate_and_apply_one(
                                _lp_selected_id, _lp_cfg, 'manual_button'
                            )
                            _w183_action = _w183_result.get('action', 'unknown')
                            if _w183_action == 'reduced':
                                st.success(
                                    f"値下げ成功: ${_w183_result['old_price']:.2f}"
                                    f" → ${_w183_result['new_price']:.2f}"
                                )
                                st.rerun()
                            elif _w183_action == 'failed_api':
                                st.error(
                                    f"API 失敗: {_w183_result.get('message', '')}"
                                )
                            else:
                                st.info(
                                    f"skip: {_w183_action} — "
                                    f"{_w183_result.get('message', '')}"
                                )
                        except Exception as e:
                            st.error(f"値下げ実行エラー: {e}")
            with _w183_btn_cols[1]:
                if _w183_disabled:
                    st.caption("本日 4 回上限に到達 — 翌 JST 0 時にリセット")

            # 値下げ履歴 (直近 20 件)
            _w183_log = get_price_change_log(_lp_selected_id, limit=20)
            if _w183_log:
                _w183_log_df = pd.DataFrame([
                    {
                        '日時(UTC)': r['changed_at'],
                        '旧価格': r['old_price_usd'],
                        '新価格': r['new_price_usd'],
                        'ライバル合計': r['competitor_total_usd'],
                        'rule': r['rule_applied'],
                        '実行元': r['triggered_by'],
                        '結果': '✓' if r['success'] else '✗',
                        'エラー': r['error_message'] or '',
                    }
                    for r in _w183_log
                ])
                st.dataframe(
                    _w183_log_df,
                    column_config={
                        '日時(UTC)': st.column_config.TextColumn('日時 (UTC)', width='medium'),
                        '旧価格': st.column_config.NumberColumn('旧価格', format='$%.2f', width='small'),
                        '新価格': st.column_config.NumberColumn('新価格', format='$%.2f', width='small'),
                        'ライバル合計': st.column_config.NumberColumn('ライバル合計', format='$%.2f', width='small'),
                        'rule': st.column_config.TextColumn('rule', width='small'),
                        '実行元': st.column_config.TextColumn('実行元', width='small'),
                        '結果': st.column_config.TextColumn('結果', width='small'),
                        'エラー': st.column_config.TextColumn('エラー', width='medium'),
                    },
                    hide_index=True,
                    use_container_width=True,
                    height=250,
                )
            else:
                st.caption("この商品の値下げ履歴はまだありません。")

    # ─────────────────────────────────────────────
    # 3. 新規発見ライバル (W99 連携、コンパクト 1 行 / 件)
    # ─────────────────────────────────────────────
    st.divider()
    st.subheader("新規発見ライバル")

    try:
        _lp_alerts = get_japan_competitor_alerts(action="pending")
        _lp_registered_ids: set[str] = set()
        for _l in _lp_grouped.values() if _lp_active else []:
            _lp_registered_ids.update(_l)

        def _lp_is_real_item_id(iid: str) -> bool:
            if not iid or iid.startswith('synthetic_'):
                return False
            return iid.isdigit() and 11 <= len(iid) <= 14

        _lp_real_alerts = [a for a in _lp_alerts if _lp_is_real_item_id(a.get('found_item_id', ''))]
        _lp_synthetic_count = len(_lp_alerts) - len(_lp_real_alerts)

        if not _lp_alerts:
            st.info("新規発見ライバルはありません。")
        elif not _lp_real_alerts:
            st.warning(
                f"未対応 {len(_lp_alerts)} 件すべて旧形式 (合成 ID) で表示不可。"
                f"次回 W99 タスク実行時に新形式で登録されます。"
            )
        else:
            _lp_show_alerts = [
                a for a in _lp_real_alerts
                if a.get('found_item_id') not in _lp_registered_ids
            ][:30]
            _lp_msg = f"未対応: {len(_lp_real_alerts)} 件"
            if _lp_synthetic_count > 0:
                _lp_msg += f" / 旧形式 (除外): {_lp_synthetic_count} 件"
            st.caption(_lp_msg)

            # コンパクト 1 行 / 件 (header 風 + データ行)
            _lp_target_options = [it['ebay_item_id'] for it in _lp_active] if _lp_active else []

            # ヘッダ
            _lp_h = st.columns([3, 2, 2, 4, 3, 2, 1])
            _lp_h[0].markdown("**Item / セラー**")
            _lp_h[1].markdown("**価格**")
            _lp_h[2].markdown("**送料**")
            _lp_h[3].markdown("**自分の商品**")
            _lp_h[4].markdown("**操作**")
            _lp_h[5].markdown("")
            _lp_h[6].markdown("")
            st.divider()

            for _lp_alert in _lp_show_alerts:
                _aid = _lp_alert['id']
                _iid = _lp_alert['found_item_id']
                _url = f"https://www.ebay.com/itm/{_iid}"
                _ship = _lp_alert.get('found_shipping')
                _pr = _lp_alert.get('found_price') or 0

                _r = st.columns([3, 2, 2, 4, 3, 2, 1])
                with _r[0]:
                    st.markdown(f"[`{_iid}`]({_url})  \n_{_lp_alert.get('found_seller', '-')}_")
                with _r[1]:
                    st.markdown(f"${_pr:.2f}")
                with _r[2]:
                    if _ship is None:
                        if st.button("取得", key=f"lp_alert_fetch_{_aid}"):
                            try:
                                _f = fetch_alert_shipping_usd(_aid, _lp_cfg)
                                if _f is None:
                                    st.error("失敗")
                                else:
                                    bump_db_version()  # W134 Step2: 書込後 read-cache 無効化
                                    st.rerun()
                            except Exception as e:
                                st.error(f"err: {e}")
                    else:
                        st.markdown(f"${float(_ship):.2f}")
                with _r[3]:
                    if _lp_target_options:
                        _tgt = st.selectbox(
                            "自分商品",
                            options=_lp_target_options,
                            format_func=lambda x: (
                                (_lp_items_map.get(x, {}).get('title') or '')[:25] +
                                f" ({x[-4:]})"
                            ),
                            key=f"lp_alert_target_{_aid}",
                            label_visibility='collapsed',
                        )
                    else:
                        st.caption("商品なし")
                        _tgt = None
                with _r[4]:
                    if st.button("追加", key=f"lp_alert_add_{_aid}", type='primary'):
                        if not _tgt:
                            st.error("商品選択 必要")
                        else:
                            try:
                                _existing = _lp_grouped.get(_tgt, [])
                                if len(_existing) >= _LP_MAX_COMPETITORS:
                                    st.error(f"既に {_LP_MAX_COMPETITORS} 件登録済")
                                else:
                                    upsert_listing_competitors(
                                        _tgt, _existing + [_iid]
                                    )
                                    update_alert_action(_aid, "registered")
                                    # ② (2026-05-18) 登録直後に価格・送料を
                                    #    Browse API で自動取得 (単件 1 listing)。
                                    #    bump_db_version は fetch 後 = 競合集合
                                    #    確定後に cache 無効化 (③signature 整合)。
                                    with st.spinner("ライバル価格を取得中..."):
                                        _pr = refresh_competitor_pricing(
                                            _tgt, _lp_cfg
                                        )
                                    bump_db_version()  # W134: 書込後 read-cache 無効化
                                    st.success(
                                        f"追加 (価格取得 成功{_pr['fetched']}"
                                        f"/失敗{_pr['failed']})"
                                    )
                                    st.rerun()
                            except Exception as e:
                                st.error(f"err: {e}")
                with _r[5]:
                    if st.button("無視", key=f"lp_alert_skip_{_aid}"):
                        try:
                            update_alert_action(_aid, "ignored")
                            bump_db_version()  # W134 Step2: 書込後 read-cache 無効化
                            st.rerun()
                        except Exception as e:
                            st.error(f"err: {e}")
                with _r[6]:
                    pass
    except Exception as e:
        st.error(f"新規発見ライバル読込エラー: {e}")
