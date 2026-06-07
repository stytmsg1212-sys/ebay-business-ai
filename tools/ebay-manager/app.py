"""
MONO Deck - eBay 運用コンソール (MonoHonpo 内部ツール)
streamlit run app.py で起動
"""
import logging
import sqlite3
import streamlit as st
import pandas as pd

logger = logging.getLogger(__name__)
from calculator import calculate, CalcInput, load_settings, save_settings, get_all_services
from monitor.database import (
    init_db, get_all_items, get_active_items, get_recent_logs,
    add_item_manual, upsert_item, delete_item, update_item_status, add_check_log,
    get_prev_status, find_site_config_by_sku, get_site_configs,
    save_site_config, delete_site_config, build_source_url,
)
from monitor.scrapers import check_item_by_config, prepare_batch_items, check_items_batch
from monitor.notifier import send_test_notification, send_unavailable_alert
from monitor.ebay_sync import (
    sync_listings_from_ebay, get_sync_report, auto_rank_all_listings_in_db,
    sync_single_listing,
)
from monitor.database import (
    get_ebay_listings, update_ebay_listing_quantity, update_ebay_listing_sku,
    update_ebay_listing_rank, set_ebay_listing_risk_confirmed,
    get_ebay_listings_by_rank, get_rank_stats, get_japan_competitor_alerts,
    get_rank_distribution_details, get_all_listing_metrics,
    get_ebay_listings_supply_risk
)
from monitor.database import update_alert_action
from monitor.database import (
    get_supplier_candidates, update_supplier_candidate_status,
    get_ebay_listing_by_item_id,
)
from tasks.task_supplier_apply import (
    accept_supplier_candidate, apply_supplier_candidate,
)
from monitor.credentials import get_ebay_credentials, ebay_credentials_ok
from monitor.ebay_client import revise_inventory_quantity
from monitor.rank_calculator import check_shipping_cost
from scheduler_integration import get_latest_execution_logs, get_execution_summary
from company_integration import (
    get_today_todos, get_company_status, get_today_routine_result,
    get_active_tasks, get_archived_tasks, complete_task,
)
from ui_themes import apply_custom_styling
from execution_logger import (
    log_execution_result, save_execution_history,
    send_discord_notification, get_execution_statistics
)
from sku_mapping_manager import (
    load_mappings, save_mappings, add_mapping, update_mapping,
    delete_mapping, reset_to_defaults, generate_url, validate_sku,
    url_to_sku,
)
from fuel_surcharge_manager import (
    get_days_since_last_update, UPDATE_WARNING_DAYS,
)
from shipping_rate_manager import (
    parse_pdf as parse_shipping_pdf,
    compute_diff as compute_shipping_diff,
    apply_diff_to_csv as apply_shipping_diff,
    DEFAULT_CARRIER_ZONE_MAPPING,
    get_shipping_rate_days_since_update,
    SHIPPING_RATE_WARNING_DAYS,
)
import html
import json
import time
import sys
from pathlib import Path

# W9 個別新規出品 (Phase 5)
from tabs.tab_individual_listing import render_tab as render_individual_listing_tab
from tabs.tab_description_templates import render_tab as render_description_templates_tab
from tabs.tab_scheduled_execution import render_tab as render_scheduled_execution_tab
# W24 Research 脳 タブ
from tabs.tab_research_brain import render_tab as render_research_brain_tab
from tabs.tab_morning_discovery import render_morning_discovery_tab
from tabs.tab_purchase_confirm import render_purchase_confirm_tab
from tabs.tab_keyword_watch import render_keyword_watch_tab  # W148 (2026-05-21)
from tabs.tab_w228_research import render_w228_research_tab  # W228 (2026-06-07)
from tasks.task_seed_description_template import seed_v4_template_if_needed

# ── W134 Step2: 重い DB ローダの read-cache (体感改善) ──
# 2 層の安全設計 (2026-05-16 user 判断 = 短 TTL 方式):
#  (1) **ttl=3s = 正しさの保証**。bump 配線を 1 箇所漏らしても古い在庫/価格は
#      最悪 3 秒で自動的に最新へ切替わる。app 全体の全書込を網羅 bump する
#      必要はない (網羅は単一巨大 app.py では証明不能で脆弱なため放棄)。
#  (2) bump_db_version() = 既知ホットパスの **即時反映最適化** (0 秒)。
#      在庫確定/保存等で 3 秒すら待たせないための任意配線であり、欠落は
#      correctness 違反でなく「最大 3 秒の遅延」に縮退する。
# db_version は **先頭 _ を付けない** (st.cache_data は _ 始まり引数を hash 対象外に
# するため。token を key に含めるには通常引数である必要)。書込関数は cache せず、
# SQLite 接続も st.cache_resource で共有しない (ui_cache.py の設計約束参照)。
from ui_cache import (  # noqa: E402
    get_db_version, bump_db_version, seed_keyed_value_from_db,
)


st.set_page_config(page_title="MONO Deck", page_icon="◯", layout="wide")
apply_custom_styling()

# ── W217-C (2026-06-04 mockup): グローバル密度 CSS ──
# 全体的にボタン・入力枠が大きく余白過多 → 密度を上げて視線移動を減らす。
# K2 surgical: padding / min-height / gap / font-size のみ調整。色・font-family
# は apply_dark_paper_theme に従う。過度に詰めて崩さないため控えめ。
# Streamlit 1.56 の data-testid を基準に上書き。
st.markdown(
    """<style>
    /* ── Buttons (st.button / st.download_button / form_submit_button) ──
       既定 padding 0.5rem 1rem (≈ 8 16px) min-height ~38-42px を詰める。
       ナビボタン ([class*="st-key-_w134_navbtn_"]) は独自 padding 持つので影響なし
       (より具体的なセレクタが優先)。 */
    [data-testid="stButton"] > button,
    [data-testid="stDownloadButton"] > button,
    [data-testid="stFormSubmitButton"] > button {
        min-height: 34px !important;
        padding: 5px 12px !important;
        font-size: 13px !important;
        line-height: 1.3 !important;
    }
    /* ── Text / Number / Selectbox / Textarea inputs ── */
    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input,
    [data-testid="stDateInput"] input,
    [data-testid="stTimeInput"] input,
    [data-testid="stSelectbox"] div[role="combobox"],
    [data-testid="stMultiSelect"] div[role="combobox"] {
        min-height: 34px !important;
        padding-top: 5px !important;
        padding-bottom: 5px !important;
        font-size: 13px !important;
    }
    [data-testid="stTextArea"] textarea {
        font-size: 13px !important;
        line-height: 1.4 !important;
        padding: 6px 9px !important;
    }
    /* widget label 余白詰め */
    [data-testid="stWidgetLabel"] {
        margin-bottom: 2px !important;
    }
    [data-testid="stWidgetLabel"] p {
        font-size: 12px !important;
        line-height: 1.35 !important;
    }
    /* ── Vertical block gap (要素間余白) ──
       既定 1rem を詰めて密度向上。ただし極端に詰めると見出しが食い込むので
       控えめ (0.55rem ≈ 9px)。 */
    [data-testid="stVerticalBlock"] {
        gap: 0.55rem !important;
    }
    /* ── Block container padding ── */
    .main .block-container {
        padding-top: 0.6rem !important;
    }
    /* ── Metric / Caption / Subheader 余白詰め ── */
    [data-testid="stMetric"] {
        padding: 4px 8px !important;
    }
    [data-testid="stMetricLabel"] p {
        font-size: 11px !important;
    }
    [data-testid="stCaptionContainer"],
    .stCaption {
        font-size: 11.5px !important;
        margin-top: 1px !important;
        margin-bottom: 1px !important;
    }
    /* ── Expander header 詰め ── */
    [data-testid="stExpander"] details > summary {
        padding: 6px 10px !important;
        font-size: 13px !important;
    }
    /* ── Checkbox / Radio 余白 ── */
    [data-testid="stCheckbox"] label,
    [data-testid="stRadio"] label {
        font-size: 13px !important;
        line-height: 1.35 !important;
    }
    </style>""",
    unsafe_allow_html=True,
)

init_db()
# W9: description_templates に v4 テンプレを初期投入 (既に何かあれば no-op)
try:
    seed_v4_template_if_needed()
except Exception as _seed_e:  # noqa: BLE001 — UI 起動を絶対に止めない
    import logging as _log
    _log.getLogger(__name__).warning(f"seed_v4_template_if_needed failed: {_seed_e}")

# W29 (2026-04-26): MonoDeck 起動時に必ず eBay OAuth token を最新化する.
# Streamlit プロセスの os.environ に古い token が残ってしまう問題への根本対策.
# 残り有効時間 < 30分なら refresh、十分なら no-op.
# 失敗しても起動を止めない (eBay 機能が一部使えなくなるだけで他機能は動かす).
try:
    from monitor.ebay_oauth_refresh import is_token_near_expiry, refresh_access_token
    if is_token_near_expiry(threshold_sec=1800):  # 30 分閾値
        _refresh_r = refresh_access_token(force=False)
        if _refresh_r.get('success'):
            import logging as _log
            _log.getLogger(__name__).info(
                f"eBay OAuth token refreshed at MonoDeck startup "
                f"(expires_in={_refresh_r.get('expires_in')}s)"
            )
        else:
            import logging as _log
            _log.getLogger(__name__).warning(
                f"eBay OAuth refresh at startup failed: {_refresh_r.get('errors')}"
            )
except Exception as _oauth_e:  # noqa: BLE001 — UI 起動を絶対に止めない
    import logging as _log
    _log.getLogger(__name__).warning(f"OAuth startup refresh skipped: {_oauth_e}")

if "settings" not in st.session_state:
    st.session_state.settings = load_settings()
s = st.session_state.settings

# Sidebar nav (2026-05-26): 21 タブが horizontal radio で視認困難になったため
# st.sidebar 内のグループ別 button 群に再構成。
# W134 contract (downstream 21 箇所 `if _w134_sel == "<ラベル>":` 機械置換) は
# 変数 _w134_sel を維持することで保つ。各タブ本体への影響なし。
_W134_GROUPS = {
    "★ 毎日": [
        "DASHBOARD",
        "商品管理",         # W119 (2026-05-11)
        "在庫監視",
        "仕入先候補",
        "入荷確認",         # W133 (2026-05-16)
    ],
    "⚲ リサーチ": [
        "リサーチ脳",       # W24 (2026-04-26)
        "今日の発掘",       # W122 (2026-05-13)
        "キーワード新着監視",  # W148 (2026-05-21)
        "ライバルセラー監視",  # W#3 (2026-06-07)
        "最安値チェック",   # W98 (2026-05-05)
        "利益計算",
        "商品リサーチ(W228)",  # W228 (2026-06-07)
    ],
    "⏺ 出品・関連": [
        "個別出品",
        "eBay連携",
        "市場戦略",         # W7-A (2026-04-27)
        "通関対応",         # W14 (2026-04-24)
    ],
    "⛭ 設定・ops": [
        "手動実行",
        "定時実行",         # 2026-04-25
        "SKU変換",
        "動画学習",
        "モデル比較",       # W86 (2026-05-01)
        "エージェント監視",
        "設定",
    ],
}
_W134_TABS = [page for pages in _W134_GROUPS.values() for page in pages]

# session_state migration: 旧 horizontal radio key (_w134_nav) があれば値を継承。
if "_w134_sel" not in st.session_state:
    _legacy = st.session_state.get("_w134_nav")
    st.session_state._w134_sel = _legacy if _legacy in _W134_TABS else "DASHBOARD"

# ── W217-A v2 (2026-06-04): 上部ナビを 2 段式 (segmented_control + pages) に刷新 ──
# 旧 (v1): 4 カテゴリ × 全 21 ページを 4 列の極小ボタンで一度に並べ視認困難。
# 新 (v2): 上段=カテゴリ segmented_control / 下段=選択カテゴリのページのみ
# 横並びボタン (最大 4 列/行)。Codex/GPT-5.5 設計。
#
# state/routing は **完全不変**:
#   - state key: st.session_state._w134_sel (選択中ページ名)
#   - widget key: _w134_navbtn_<page>
#   - 各 if _w134_sel == "..." 分岐 21 箇所は 1 行も変更しない。
# K2 surgical: ナビ表示制御だけを変える。下流 routing 差分ゼロ。
#
# カテゴリ表示同期: _w134_sel が属するカテゴリを「現在表示中カテゴリ」の
# 初期値にして、別カテゴリページに jump した後の rerun でもページが消えない
# ようにする (segmented_control 値は _w217a_cat_view state key で保持)。
st.markdown(
    """<style>
    /* W217-A v2: 上部ナビ container を sticky + コンパクト化 (keyed container) */
    .st-key-_w217a_navbar {
        position: sticky;
        top: 0;
        z-index: 100;
        background: rgba(20, 22, 26, 0.95);
        backdrop-filter: blur(6px);
        margin: -8px -16px 8px -16px;
        padding: 6px 16px 8px 16px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }
    /* Streamlit 新レイアウトは各要素を stLayoutWrapper で個別ラップする。
       その wrapper は nav の高さしかなく sticky の移動余地が無いため、
       display:contents で wrapper をボックスツリーから外し、nav の実効親を
       全高の stVerticalBlock にして sticky を機能させる (2026-06-04)。 */
    [data-testid="stLayoutWrapper"]:has(> .st-key-_w217a_navbar) {
        display: contents;
    }
    /* nav ブランド名 */
    .w217a-topnav-brand {
        font-family: var(--f-mono, monospace);
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 3px;
        color: #e6e9ee;
        margin: 0 12px 4px 0;
        display: inline-block;
        vertical-align: middle;
    }
    /* segmented_control 自身は Streamlit の default style を流用 (上段カテゴリ)。
       W217-B v2 (2026-06-04): mockup .seg button (padding:6px 13px / font 12.5px)
       準拠で Streamlit segmented_control を引き締め。 */
    .st-key-_w134_nav_group button {
        padding: 6px 13px !important;
        font-size: 12.5px !important;
        font-weight: 600 !important;
        min-height: 30px !important;
    }
    /* 下段ページボタン: mockup .pages button (padding:8px 14px / font 13px /
       min-height 38px) 準拠。rounded + hover + 選択中は青 accent 左ライン。 */
    [class*="st-key-_w134_navbtn_"] button {
        padding: 8px 14px !important;
        font-size: 13px !important;
        line-height: 1.25 !important;
        min-height: 38px !important;
        margin: 0 !important;
        border-radius: 8px !important;
        border: 1px solid rgba(255, 255, 255, 0.12) !important;
        background: #1d222b !important;
        color: #c8ced8 !important;
        font-weight: 500 !important;
        transition: background 0.12s, border-color 0.12s, color 0.12s !important;
    }
    [class*="st-key-_w134_navbtn_"] button:hover {
        background: #252b36 !important;
        border-color: #3a4352 !important;
        color: #fff !important;
    }
    /* 選択中 (type="primary") = 青 accent 左ライン強調 */
    [class*="st-key-_w134_navbtn_"] button[kind="primary"] {
        background: rgba(110, 168, 254, 0.14) !important;
        color: #fff !important;
        border-color: rgba(110, 168, 254, 0.65) !important;
        box-shadow: inset 3px 0 0 #6ea8fe !important;
    }
    /* nav 内の columns 間隔をつめる */
    [data-testid="stHorizontalBlock"]:has([class*="st-key-_w134_navbtn_"]) {
        gap: 6px !important;
        flex-wrap: wrap !important;
        justify-content: flex-start !important;
    }
    /* W217-B v2: ページボタン列を「内容幅」にして左詰めパック (mockup .pages の
       flex auto-width 準拠)。等幅列で全幅ボタンになるのを防ぎ、サンプルの
       コンパクトな pill 並びにする。 */
    [data-testid="stColumn"]:has([class*="st-key-_w134_navbtn_"]) {
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: 0 !important;
    }
    /* メインコンテナ余白を広げる (sidebar 撤去で横幅を活かす) */
    .main .block-container {
        padding-top: 1rem;
        max-width: 1600px;
    }
    </style>""",
    unsafe_allow_html=True,
)

# page → 所属カテゴリ の逆引き map (下記 view-sync で使う)
_W134_PAGE_TO_GROUP = {
    page: grp for grp, pages in _W134_GROUPS.items() for page in pages
}

# 現在表示中カテゴリ = _w134_sel の属するカテゴリを default に。
# user が segmented_control で別カテゴリへ切り替えると _w217a_cat_view が
# それを記憶し、_w134_sel と別カテゴリでも下段にそのカテゴリのページが出る。
_W217A_VIEW_KEY = "_w217a_cat_view"
_sel_group = _W134_PAGE_TO_GROUP.get(
    st.session_state._w134_sel, next(iter(_W134_GROUPS))
)
if _W217A_VIEW_KEY not in st.session_state:
    st.session_state[_W217A_VIEW_KEY] = _sel_group
# 選択中ページが現在表示カテゴリに含まれていなければ自動同期
# (例: routing で _w134_sel が外部から変更された場合)。
if st.session_state._w134_sel not in _W134_GROUPS.get(
    st.session_state[_W217A_VIEW_KEY], []
):
    st.session_state[_W217A_VIEW_KEY] = _sel_group

_navbar = st.container(key="_w217a_navbar")
_navbar.markdown(
    '<div class="w217a-topnav-brand">◯ &nbsp; MONO &nbsp; DECK</div>',
    unsafe_allow_html=True,
)

# ── 上段: カテゴリ segmented_control ──
# segmented_control は Streamlit 1.56+ で安定提供。本 project は requirements.txt
# で streamlit>=1.56.0 を pin 済み。version 不在時の fallback は不要 (要件未満)。
_cat_labels = list(_W134_GROUPS.keys())
_cur_view = st.session_state[_W217A_VIEW_KEY]
_picked_cat = _navbar.segmented_control(
    "カテゴリ",
    options=_cat_labels,
    default=_cur_view if _cur_view in _cat_labels else _cat_labels[0],
    key="_w134_nav_group",
    label_visibility="collapsed",
)
# 戻り値 None ガード (Streamlit が segmented_control で None を返す状況に備え)
if _picked_cat and _picked_cat != _cur_view:
    st.session_state[_W217A_VIEW_KEY] = _picked_cat
    _cur_view = _picked_cat

# ── 下段: 選択カテゴリのページのみ横並びボタン (最大 4 列/行) ──
_pages_in_view = _W134_GROUPS.get(_cur_view, [])
_PAGE_COLS_PER_ROW = 4
for _row_start in range(0, len(_pages_in_view), _PAGE_COLS_PER_ROW):
    _row_pages = _pages_in_view[_row_start:_row_start + _PAGE_COLS_PER_ROW]
    # 端数行も同じ列幅で揃える (右端が伸びるのを防ぐ)
    _cols = _navbar.columns(_PAGE_COLS_PER_ROW, gap="small")
    for _i_btn, _page in enumerate(_row_pages):
        with _cols[_i_btn]:
            _is_active = (st.session_state._w134_sel == _page)
            if st.button(
                _page,
                key=f"_w134_navbtn_{_page}",
                use_container_width=False,
                type=("primary" if _is_active else "secondary"),
            ):
                st.session_state._w134_sel = _page
                # ページ jump 後は新 _w134_sel の所属カテゴリへ view も同期
                st.session_state[_W217A_VIEW_KEY] = _W134_PAGE_TO_GROUP.get(
                    _page, _cur_view
                )
                st.rerun()

_w134_sel = st.session_state._w134_sel
# 2026-04-22: MAIL タブを削除 (ダッシュボードに統合)。
# DASHBOARD には緊急メール (urgent/buyer_message/sale/offer/return) を常時表示し、
# その下の expander 代替セクションで「非緊急・参考メール」を表示する。
# 重複出力バグは reset_confirmed_emails() を age-based prune に置き換えて解消済み。

# ========== ダッシュボードタブ ==========
if _w134_sel == "DASHBOARD":
    from tabs.tab_dashboard import render_dashboard_tab
    render_dashboard_tab(s)

# ========== 利益計算タブ ==========
if _w134_sel == "利益計算":
    from tabs.tab_profit_calc import render_profit_calc_tab
    render_profit_calc_tab(s)


# ========== 在庫監視タブ ==========
if _w134_sel == "在庫監視":
    from tabs.tab_inventory_monitor import render_inventory_monitor_tab
    render_inventory_monitor_tab(s)


# ========== eBay連携タブ ==========
if _w134_sel == "eBay連携":
    from tabs.tab_ebay_sync import render_ebay_sync_tab
    render_ebay_sync_tab(s)


# ========== 商品管理 タブ (W119 / 2026-05-11) ==========
# 1 商品の全情報を 1 画面に統合: 物理属性 / 仕入先 / 在庫 / 利益計算 / 競合
# クリックで展開、編集して保存 → breakeven 自動再計算.
if _w134_sel == "商品管理":
    try:
        # 最安値チェックタブと同じ config 読込
        import json as _pm_json
        _pm_cfg: dict = {}
        _pm_cfg_path = Path(__file__).parent / 'config' / 'schedule_config.json'
        if _pm_cfg_path.exists():
            try:
                with open(_pm_cfg_path, 'r', encoding='utf-8') as _pm_cf:
                    _pm_cfg = _pm_json.load(_pm_cf)
            except Exception:
                _pm_cfg = {}
        from tabs.tab_product_management import render_product_management
        render_product_management(_pm_cfg)
    except Exception as _e:
        st.error(f"商品管理タブ 描画エラー: {_e}")


# ========== 最安値チェック タブ (W98 登録 UI + W183 自動値下げ) ==========
# 自分の商品ごとに監視したいライバルを登録し、6 時間ごとにライバル最安より
# 0.01 USD 安く ReviseFixedPriceItem で自動値下げする (W183, task_rival_pricing.py)。
if _w134_sel == "最安値チェック":
    from tabs.tab_lowest_price import render_lowest_price_tab
    render_lowest_price_tab(s)


# ========== 手動実行タブ ==========
if _w134_sel == "手動実行":
    from tabs.tab_manual_run import render_manual_run_tab
    render_manual_run_tab(s)


# ========== 仕入先候補タブ ==========
if _w134_sel == "仕入先候補":
    from tabs.tab_supplier_candidates import render_supplier_candidates_tab
    render_supplier_candidates_tab(s)


# ========== モデル比較タブ (W86 / 2026-05-01) ==========
# Opus 4.7 vs Sonnet 4.6 supplier evaluation A/B test 結果の並列比較.
# 元データ: supplier_ab_test_runs テーブル.
if _w134_sel == "モデル比較":
    from tabs.tab_model_comparison import render_model_comparison_tab
    render_model_comparison_tab()


# ========== 個別出品タブ (W9) ==========
if _w134_sel == "個別出品":
    render_individual_listing_tab(s)


# ========== 通関対応タブ (W14 2026-04-24) ==========
if _w134_sel == "通関対応":
    from tabs.tab_customs import render_customs_tab
    render_customs_tab()


# ========== 市場戦略タブ (W7-A 2026-04-27) ==========
if _w134_sel == "市場戦略":
    from tabs.tab_market_strategy import render_tab as render_market_strategy_tab
    render_market_strategy_tab()


# ========== 動画学習タブ ==========
if _w134_sel == "動画学習":
    from tabs.tab_video_learning import render_video_learning_tab
    render_video_learning_tab()


# ========== エージェント監視タブ ==========
if _w134_sel == "エージェント監視":
    from tabs.tab_agent_monitor import render_agent_monitor_tab
    render_agent_monitor_tab()


# ========== SKU変換ルール設定タブ ==========
if _w134_sel == "SKU変換":
    from tabs.tab_sku_conversion import render_sku_conversion_tab
    render_sku_conversion_tab()


# ========== メールタブ (2026-04-22 削除) ==========
# 旧 MAIL タブは DASHBOARD の INBOX + REFERENCE セクションに統合された。
# 重複バグは monitor.database.prune_old_confirmed_emails(age-based) への切替で解消済み。

# ========== リサーチ脳タブ (W24 2026-04-26) ==========
if _w134_sel == "リサーチ脳":
    render_research_brain_tab()

if _w134_sel == "今日の発掘":
    render_morning_discovery_tab()

# ========== 入荷確認タブ (W133 2026-05-16) ==========
if _w134_sel == "入荷確認":
    render_purchase_confirm_tab()

# ========== キーワード新着監視タブ (W148 2026-05-21) ==========
if _w134_sel == "キーワード新着監視":
    render_keyword_watch_tab()

# ========== ライバルセラー監視タブ (W#3 2026-06-07) ==========
if _w134_sel == "ライバルセラー監視":
    try:
        from tabs.tab_rival_sellers import render_rival_sellers_tab
        render_rival_sellers_tab(s)
    except Exception as _e:
        st.error(f"ライバルセラー監視タブ 描画エラー: {_e}")

# ========== 商品リサーチ Wizard タブ (W228 2026-06-07) ==========
if _w134_sel == "商品リサーチ(W228)":
    try:
        render_w228_research_tab(s)
    except Exception as _e:
        st.error(f"商品リサーチ(W228)タブ 描画エラー: {_e}")

# ========== 定時実行タブ (2026-04-25 hour ドリフト事故対応) ==========
if _w134_sel == "定時実行":
    render_scheduled_execution_tab()

# ========== 設定タブ ==========
if _w134_sel == "設定":
    st.subheader("基本設定")
    c1, c2 = st.columns(2)
    with c1:
        new_fx = st.number_input("為替レート", value=float(s["exchange_rate"]), key="s_fx")
        new_duty = st.number_input("デフォルト関税率（%）", value=float(s["duty_rate"]), key="s_duty")
        new_pl = st.number_input("PL広告費率（%）", value=float(s["promoted_listing_rate"]), key="s_pl")
        new_tax = st.number_input("消費税率（%）", value=float(s["consumption_tax_rate"]), key="s_tax")
        new_point = st.number_input("ポイント付与率（%）", value=float(s["point_reward_rate"]), key="s_point")
    with c2:
        new_payoneer = st.number_input("Payoneer手数料率（%）", value=float(s["payoneer_fee_rate"]), key="s_pay")
        # 燃料サーチャージ: session_state経由でPDF抽出値を反映可能
        _ff_default = float(st.session_state.get("pending_fuel_fedex", s["fuel_surcharge_fedex"]))
        _fd_default = float(st.session_state.get("pending_fuel_dhl", s["fuel_surcharge_dhl"]))
        new_fuel_fedex = st.number_input("燃料サーチャージ FedEx（%）", value=_ff_default, key="s_ff")
        new_fuel_dhl = st.number_input("燃料サーチャージ DHL（%）", value=_fd_default, key="s_fd")
        new_cpass_proc = st.number_input("CPaSS 米国処理費率（%）", value=float(s.get("cpass_us_processing_rate", 2.10)), key="s_cp")
        new_cpass_mpf = st.number_input("CPaSS MPF簡易（USD）", value=float(s.get("cpass_fedex_mpf_simple_usd", 2.69)), key="s_cm")

    # 運送料PDF自動反映セクション
    st.divider()
    st.markdown("### 運送料 PDF自動反映")
    st.caption("eBay SpeedPAK 料金ガイド（FedEx / DHL）のPDFをアップロードすると、ShippingRates.csv の該当ゾーンを一括更新します。")

    _ship_days = get_shipping_rate_days_since_update(s)
    _ship_last = s.get("shipping_rate_last_updated", "")
    if _ship_days is None:
        st.warning("最終更新日が未記録です。最新の運送料PDFをアップロードしてください。")
    elif _ship_days >= SHIPPING_RATE_WARNING_DAYS:
        st.warning(f"最終更新から **{_ship_days}日経過** しています（{_ship_last[:10]}）。最新の運送料PDFをアップロードしてください。")
    else:
        st.caption(f"最終更新: {_ship_last[:10]} ({_ship_days}日前) — {SHIPPING_RATE_WARNING_DAYS}日以上経過で警告表示")

    _ship_pdf = st.file_uploader(
        "運送料PDFをアップロード（eBay SpeedPAK FedEx / DHL）",
        type=["pdf"],
        key="shipping_pdf_uploader",
    )
    if _ship_pdf is not None:
        if st.button("PDFから抽出", key="btn_extract_shipping"):
            with st.status("PDFを解析中...", expanded=False) as _status:
                _sr = parse_shipping_pdf(_ship_pdf)
                if _sr.error:
                    _status.update(label=f"失敗: {_sr.error}", state="error")
                    st.error(_sr.error)
                else:
                    _zmap = DEFAULT_CARRIER_ZONE_MAPPING.get(_sr.carrier)
                    if _zmap is None:
                        _status.update(label="失敗: キャリア判定不能", state="error")
                        st.error(f"キャリア不明: {_sr.carrier}")
                    else:
                        _reports = []
                        for _tbl in _sr.tables:
                            _rep = compute_shipping_diff(
                                _tbl,
                                zone_letter=_zmap["pdf_zone"],
                                zone_name_in_csv=_zmap["csv_zone"],
                            )
                            if _rep:
                                _reports.append((_tbl, _rep))
                        st.session_state.shipping_extract = {
                            "carrier": _sr.carrier,
                            "effective_date": _sr.effective_date,
                            "reports": _reports,
                        }
                        _status.update(
                            label=f"抽出完了（{_sr.carrier}: {len(_reports)}サービス）",
                            state="complete",
                        )

    _ship_ext = st.session_state.get("shipping_extract")
    if _ship_ext and _ship_ext.get("reports"):
        st.markdown(
            f"**キャリア**: {_ship_ext['carrier']}　"
            f"**発効日**: {_ship_ext['effective_date'] or '不明'}"
        )

        for (_tbl, _rep) in _ship_ext["reports"]:
            _pcts = [d.delta_pct for d in _rep.diffs if d.delta_pct is not None]
            _avg = sum(_pcts) / len(_pcts) if _pcts else 0.0
            _max = max(_pcts) if _pcts else 0.0
            _min = min(_pcts) if _pcts else 0.0
            _big = sum(1 for p in _pcts if abs(p) >= 15.0)

            st.markdown(
                f"**{_tbl.service_name}** — SID {_rep.service_id} / ゾーン {_rep.zone_name}　"
                f"更新 {_rep.updated}件・新規 {_rep.added}件・変更なし {_rep.unchanged}件"
            )
            _summary = f"値上げ率: 平均 {_avg:+.1f}% / 最小 {_min:+.1f}% / 最大 {_max:+.1f}%"
            if _big > 0:
                st.warning(f"{_summary}　— 15%以上の変動が {_big}行あります。詳細差分で内容を確認してください。")
            else:
                st.caption(_summary)

            if st.checkbox(
                f"詳細差分を表示（{_tbl.service_name}）",
                key=f"chk_diff_{_rep.service_id}",
            ):
                _diff_rows = []
                for _d in _rep.diffs:
                    if _d.old_rate == _d.new_rate:
                        continue
                    _diff_rows.append({
                        "重量(g)": _d.weight_grams,
                        "旧料金(円)": "—" if _d.old_rate is None else _d.old_rate,
                        "新料金(円)": _d.new_rate,
                        "変化率": "NEW" if _d.delta_pct is None else f"{_d.delta_pct:+.1f}%",
                    })
                if _diff_rows:
                    st.dataframe(pd.DataFrame(_diff_rows), width="stretch", hide_index=True, height=320)

        if st.button("全サービスの差分をCSVに反映", type="primary", key="btn_apply_shipping"):
            with st.status("CSV更新中...", expanded=False) as _status2:
                from datetime import datetime as _dt_now
                _total_upd = 0
                _total_add = 0
                for (_, _rep) in _ship_ext["reports"]:
                    _u, _a = apply_shipping_diff(_rep)
                    _total_upd += _u
                    _total_add += _a
                s["shipping_rate_last_updated"] = _dt_now.now().isoformat(timespec='seconds')
                save_settings(s)
                st.session_state.settings = s
                st.session_state.pop("shipping_extract", None)
                _status2.update(
                    label=f"反映完了: {_total_upd}行更新 / {_total_add}行追加",
                    state="complete",
                )
            st.success(f"反映完了: {_total_upd}行更新 / {_total_add}行追加")
            st.rerun()

    st.divider()

    st.subheader("使用するサービス")
    all_services = get_all_services()
    all_service_names = [r["ServiceName"] for r in all_services]
    current_selected = s.get("selected_services", ["CPaSS - FedEx - FICP"])
    new_selected = st.multiselect(
        "計算対象の配送サービス（複数選択可）",
        options=all_service_names,
        default=[n for n in current_selected if n in all_service_names],
    )

    st.subheader("在庫監視・通知設定")
    d1, d2 = st.columns(2)
    with d1:
        new_webhook = st.text_input("Discord Webhook URL", value=s.get("discord_webhook_url", ""), type="password", key="s_webhook")
        new_interval = st.number_input("監視間隔（分）", min_value=5, max_value=1440, value=int(s.get("monitor_interval_minutes", 30)), step=5, key="s_interval")
        new_notify_restock = st.checkbox("在庫復活時も通知する", value=bool(s.get("notify_on_restock", True)), key="s_restock")
        if st.button("Discord テスト通知"):
            if new_webhook:
                ok = send_test_notification(new_webhook)
                st.success("送信成功！") if ok else st.error("送信失敗。Webhook URLを確認してください。")
            else:
                st.error("Webhook URLを入力してください。")
    with d2:
        st.subheader("eBay API認証情報")
        new_app_id = st.text_input("App ID (Client ID)", value=s.get("ebay_app_id", ""), key="s_app")
        new_dev_id = st.text_input("Dev ID", value=s.get("ebay_dev_id", ""), key="s_dev")
        new_cert_id = st.text_input("Cert ID (Client Secret)", value=s.get("ebay_cert_id", ""), type="password", key="s_cert")
        new_user_token = st.text_area("User Token", value=s.get("ebay_user_token", ""), height=100, key="s_token")

    if st.button("設定を保存", type="primary"):
        with st.status("設定を保存中...", expanded=True) as status:
            st.write("▸ 設定値を更新中...")
            # 燃料サーチャージは「手動更新の鮮度」を fuel_surcharge_last_updated で追跡する
            # (週次リマインダー task_fuel_surcharge_check と app.py 鮮度警告が参照)。
            # ここで値が変わった時だけ最終更新日時を打ち直す (無関係な設定保存で時計を
            # リセットしないため、変更検知してから更新)。
            _fuel_changed = (
                float(s.get("fuel_surcharge_fedex", 0)) != float(new_fuel_fedex)
                or float(s.get("fuel_surcharge_dhl", 0)) != float(new_fuel_dhl)
            )
            s.update({
                "exchange_rate": new_fx, "duty_rate": new_duty,
                "promoted_listing_rate": new_pl, "consumption_tax_rate": new_tax,
                "point_reward_rate": new_point, "payoneer_fee_rate": new_payoneer,
                "fuel_surcharge_fedex": new_fuel_fedex, "fuel_surcharge_dhl": new_fuel_dhl,
                "cpass_us_processing_rate": new_cpass_proc, "cpass_fedex_mpf_simple_usd": new_cpass_mpf,
                "selected_services": new_selected,
                "discord_webhook_url": new_webhook, "monitor_interval_minutes": new_interval,
                "notify_on_restock": new_notify_restock,
                "ebay_app_id": new_app_id, "ebay_dev_id": new_dev_id,
                "ebay_cert_id": new_cert_id, "ebay_user_token": new_user_token,
            })
            if _fuel_changed:
                from datetime import datetime as _dt_fuel
                s["fuel_surcharge_last_updated"] = _dt_fuel.now().isoformat(timespec="seconds")
            save_settings(s)
            st.session_state.settings = s
            st.write("▸ 保存完了")
            status.update(label="設定を保存しました", state="complete")
