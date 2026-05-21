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
from monitor.ebay_sync import sync_listings_from_ebay, get_sync_report, auto_rank_all_listings_in_db
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

# 仕入先候補 status 表示用の日本語マップ (W115 v2、2026-05-10 user 要望).
# DB / API は英語識別子のまま、UI 表示のみ日本語化.
_STATUS_JA = {
    "pending": "未判定",
    "accepted": "採用済",
    "rejected": "不採用",
    "applied": "反映済",
}

# W9 個別新規出品 (Phase 5)
from tabs.tab_individual_listing import render_tab as render_individual_listing_tab
from tabs.tab_description_templates import render_tab as render_description_templates_tab
from tabs.tab_scheduled_execution import render_tab as render_scheduled_execution_tab
# W24 Research 脳 タブ
from tabs.tab_research_brain import render_tab as render_research_brain_tab
from tabs.tab_morning_discovery import render_morning_discovery_tab
from tabs.tab_purchase_confirm import render_purchase_confirm_tab
from tabs.tab_keyword_watch import render_keyword_watch_tab  # W148 (2026-05-21)
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


@st.cache_data(ttl=3, show_spinner=False)
def _cd_execution_summary(db_version: int):
    from scheduler_integration import get_execution_summary
    return get_execution_summary()


@st.cache_data(ttl=3, show_spinner=False)
def _cd_dash_emails(db_version: int, limit: int):
    from monitor.database import get_recent_emails
    # 2026-05-21 user 要望: DASHBOARD ノイズ削減。
    # - listing_notification: 既存除外 (user 自身の出品通知)
    # - supplier_purchase: 入荷確認タブ専用 (W133/今回 fix)
    # - sale: 売却通知 = 自動処理 (task_order_alert) で対応、UI 露出不要
    # - promo: eBay キャンペーン等 = REFERENCE で本当に重要なものだけ別 filter
    #   (本 SQL では category_ai は見れないため UI 側で再 filter)
    return get_recent_emails(
        limit,
        exclude_categories=(
            'listing_notification', 'supplier_purchase', 'sale',
        ),
    )


@st.cache_data(ttl=3, show_spinner=False)
def _cd_customs_pending_count(db_version: int) -> int:
    """2026-05-21 user 要望: DASHBOARD に通関対応待ち件数 metric を出すため、
    customs_requests の未送信 (status IN detected/drafted/drafted_no_photo)
    をカウントする。送信済 (sent) は除外。"""
    from monitor.database import get_conn
    try:
        with get_conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM customs_requests "
                "WHERE status IN ('detected','drafted','drafted_no_photo')"
            ).fetchone()[0]
        return int(n or 0)
    except Exception:
        return 0


@st.cache_data(ttl=3, show_spinner=False)
def _cd_active_tasks(db_version: int):
    from company_integration import get_active_tasks
    return get_active_tasks()


@st.cache_data(ttl=3, show_spinner=False)
def _cd_supply_risk(db_version: int):
    from monitor.database import get_ebay_listings_supply_risk
    return get_ebay_listings_supply_risk()


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


st.set_page_config(page_title="MONO Deck", page_icon="◯", layout="wide")
apply_custom_styling()

# Helper functions
def rank_to_stars(rank: str) -> str:
    """Convert rank (S-E) to star representation"""
    rank_stars = {
        'S': "SSSSS TOP PRIORITY",
        'A': "AAAA",
        'B': "BBB",
        'C': "CC",
        'D': "D",
        'E': "-",
    }
    return rank_stars.get(rank, "???")

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

# Add new tabs for Dashboard and Manual Execution
# W134 Phase 1 Step 1: st.tabs は全タブ本体を毎回実行する (単一巨大 app.py で
# 操作毎に全タブの DB/API 処理が走り激重)。最上位ナビを st.radio 化し、
# 選択タブの本体だけを実行する (各 with tab_X: → if _w134_sel == "<ラベル>": 機械置換)。
_W134_TABS = [
    "DASHBOARD",
    "商品管理",      # W119 (2026-05-11) 1 商品の全情報統合 (物理属性/仕入先/在庫/利益/競合)
    "リサーチ脳",     # W24 (2026-04-26) Research 脳 Opus 4.7 チャット UI
    "今日の発掘",     # W122 (2026-05-13) 朝 07:00 Opus 4.7 で 3 件発掘
    "入荷確認",      # W133 (2026-05-16) 有在庫の入荷 → 在庫加算 + eBay 反映
    "利益計算",
    "在庫監視",
    "eBay連携",
    "最安値チェック",   # W98 (2026-05-05) ライバル価格追随で SEO ブースト
    "仕入先候補",
    "キーワード新着監視",  # W148 (2026-05-21) AlertCrawler 移植 メルカリ/ヤフオク 攻めの市場ディスカバリ
    "モデル比較",    # W86 (2026-05-01) Opus 4.7 vs Sonnet 4.6 supplier A/B test
    "個別出品",
    "通関対応",      # W14 (2026-04-24)
    "市場戦略",      # W7-A (2026-04-27) Buyer Location 別運用
    "動画学習",
    "エージェント監視",
    "SKU変換",
    "手動実行",
    "定時実行",      # 2026-04-25 hour ドリフト事故対応
    "設定"
]
_w134_sel = st.radio(
    "ページ", _W134_TABS, key="_w134_nav",
    horizontal=True, label_visibility="collapsed",
)
# 2026-04-22: MAIL タブを削除 (ダッシュボードに統合)。
# DASHBOARD には緊急メール (urgent/buyer_message/sale/offer/return) を常時表示し、
# その下の expander 代替セクションで「非緊急・参考メール」を表示する。
# 重複出力バグは reset_confirmed_emails() を age-based prune に置き換えて解消済み。

# ========== ダッシュボードタブ ==========
if _w134_sel == "DASHBOARD":
    import re as _re_dash
    from monitor.database import get_recent_emails as _dash_get_emails, set_email_confirmed
    import streamlit.components.v1 as _components

    # ── W24: Research 脳 morning brief セクション (本日分があれば表示) ──
    try:
        from tasks.task_research_morning_brief import get_today_brief as _get_today_brief
        _today_brief = _get_today_brief()
        if _today_brief:
            with st.container(border=True):
                st.markdown(
                    '<div style="font-size:11px;color:#a89d8a;letter-spacing:2px;'
                    'margin-bottom:6px;">M O R N I N G &nbsp; B R I E F &nbsp; — &nbsp; '
                    'Research 脳 (Opus 4.7)</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(_today_brief.get("answer_md") or "(空)")
                _dur = (_today_brief.get("duration_ms") or 0) // 1000
                _cost = _today_brief.get("cost_usd") or 0.0
                _cites_n = len(json.loads(_today_brief.get("citations") or "[]"))
                st.caption(
                    f"qa_id #{_today_brief['id']} / {_dur}s / ${_cost:.4f} / "
                    f"citations {_cites_n} 件 / 「リサーチ脳」タブで履歴・評価可能"
                )
    except (sqlite3.Error, KeyError, json.JSONDecodeError) as _brief_e:
        # H8 (Wave C): broad except → specific exceptions.
        # 該当 path で起き得るのは DB 不整合 / row 構造変化 / brief JSON 破損.
        import logging as _bl
        _bl.getLogger(__name__).debug(f"morning brief 表示 skip: {_brief_e}")

    # ── W119 (2026-05-12): 在庫通知 (有在庫 SKU で在庫低下・在庫切れ・在庫未入力) ──
    # H4 (Wave B): stock prefix + inventory_count=NULL = 在庫数未入力 listing も明示表示.
    # 旧実装は NULL を sweep して silent skip → 売れた時に減算されず oversell リスク.
    try:
        from monitor.database import get_conn as _inv_get_conn
        with _inv_get_conn() as _inv_c:
            _inv_zero = _inv_c.execute(
                """SELECT ebay_item_id, sku, title, inventory_count
                   FROM ebay_listings
                   WHERE (is_ended IS NULL OR is_ended=0)
                     AND sku LIKE 'stock%'
                     AND inventory_count IS NOT NULL
                     AND inventory_count = 0
                   ORDER BY ebay_item_id"""
            ).fetchall()
            _inv_low = _inv_c.execute(
                """SELECT ebay_item_id, sku, title, inventory_count
                   FROM ebay_listings
                   WHERE (is_ended IS NULL OR is_ended=0)
                     AND sku LIKE 'stock%'
                     AND inventory_count IS NOT NULL
                     AND inventory_count > 0 AND inventory_count <= 2
                   ORDER BY inventory_count ASC, ebay_item_id"""
            ).fetchall()
            # H4: stock prefix だが inventory_count NULL (= user 在庫数未入力)
            _inv_unset = _inv_c.execute(
                """SELECT ebay_item_id, sku, title
                   FROM ebay_listings
                   WHERE (is_ended IS NULL OR is_ended=0)
                     AND sku LIKE 'stock%'
                     AND inventory_count IS NULL
                   ORDER BY ebay_item_id"""
            ).fetchall()
            # 直近 7 日の自動減算履歴 (migration v37 で inventory_decrement_log は確定存在)
            _inv_dec_recent = _inv_c.execute(
                """SELECT order_id, ebay_item_id, sku, quantity_decremented,
                          new_inventory_count, decremented_at
                   FROM inventory_decrement_log
                   WHERE decremented_at >= datetime('now', '-7 days')
                   ORDER BY decremented_at DESC LIMIT 15"""
            ).fetchall()

        if _inv_zero or _inv_low or _inv_unset:
            with st.container(border=True):
                st.markdown("### 📦 在庫通知")
                metric_cols = st.columns(4)
                with metric_cols[0]:
                    st.metric("🔴 在庫切れ (0 個)", len(_inv_zero),
                              help="商品管理タブで在庫補充 + eBay 反映を")
                with metric_cols[1]:
                    st.metric("🟡 在庫低下 (1-2 個)", len(_inv_low))
                with metric_cols[2]:
                    st.metric("⚪ 在庫数未入力", len(_inv_unset),
                              help="stock prefix SKU だが inventory_count=NULL. "
                                   "売れても自動減算されないため oversell リスク. "
                                   "商品管理タブで在庫数を入力してください.")
                with metric_cols[3]:
                    st.metric("📉 直近 7 日減算", len(_inv_dec_recent))

                if _inv_zero:
                    st.markdown("**🔴 在庫切れ (即対応推奨)**")
                    for r in _inv_zero[:10]:
                        st.markdown(
                            f"- `{r['sku']}` | "
                            f"[{(r['title'] or '')[:60]}]"
                            f"(https://www.ebay.com/itm/{r['ebay_item_id']}) "
                            f"(ID: ...{r['ebay_item_id'][-6:]})"
                        )
                    if len(_inv_zero) > 10:
                        st.caption(f"... 他 {len(_inv_zero) - 10} 件")

                if _inv_low:
                    st.markdown("**🟡 在庫低下 (残り 1-2 個)**")
                    for r in _inv_low[:5]:
                        st.markdown(
                            f"- `{r['sku']}` | 残 **{r['inventory_count']}** | "
                            f"[{(r['title'] or '')[:60]}]"
                            f"(https://www.ebay.com/itm/{r['ebay_item_id']})"
                        )
                    if len(_inv_low) > 5:
                        st.caption(f"... 他 {len(_inv_low) - 5} 件")

                if _inv_unset:
                    st.warning(
                        f"⚠️ stock prefix SKU で在庫数未入力が **{len(_inv_unset)} 件**. "
                        f"このまま売れると自動減算されず oversell リスク."
                    )
                    with st.expander(
                        f"⚪ 在庫数未入力一覧 ({len(_inv_unset)} 件) — 商品管理タブで入力推奨",
                        expanded=False,
                    ):
                        for r in _inv_unset[:30]:
                            st.markdown(
                                f"- `{r['sku']}` | "
                                f"[{(r['title'] or '')[:60]}]"
                                f"(https://www.ebay.com/itm/{r['ebay_item_id']}) "
                                f"(ID: ...{r['ebay_item_id'][-6:]})"
                            )
                        if len(_inv_unset) > 30:
                            st.caption(f"... 他 {len(_inv_unset) - 30} 件")

                if _inv_dec_recent:
                    with st.expander(f"直近 7 日 自動減算履歴 ({len(_inv_dec_recent)} 件)",
                                     expanded=False):
                        for r in _inv_dec_recent:
                            st.markdown(
                                f"- {r['decremented_at']} | "
                                f"`{r['sku']}` | order {r['order_id']} | "
                                f"-{r['quantity_decremented']} → 残 {r['new_inventory_count']}"
                            )
    except (sqlite3.Error, sqlite3.OperationalError) as _inv_e:
        import logging as _il
        _il.getLogger(__name__).warning(f"在庫通知 描画失敗: {_inv_e}")

    # ── 2026-05-21 user 要望: 通関対応 待ち件数 metric ──
    # W14 自動検知済の通関要求 (FedEx/UPS/DHL) のうち未送信を可視化。
    # DASHBOARD 直結で見落とし防止 (旧来は「通関対応」タブを開かないと未認識)。
    try:
        _customs_pending = _cd_customs_pending_count(get_db_version())
        if _customs_pending > 0:
            with st.container(border=True):
                st.markdown("### ⚖️ 通関対応 (未送信)")
                _ccol1, _ccol2 = st.columns([1, 4])
                with _ccol1:
                    st.metric(
                        "📦 待ち件数", _customs_pending,
                        help="FedEx/UPS/DHL からの通関情報要求 (W14 自動検知)。"
                             "「通関対応」タブで承認・送信してください。",
                    )
                with _ccol2:
                    st.caption(
                        "→ 左ナビ「通関対応」タブで内容確認・ドラフト送信。"
                        "放置するとリードタイムが伸び返品/赤字リスク (CLAUDE.md 「DDP / Section 232」参照)。"
                    )
    except Exception as _ce:
        import logging as _cel
        _cel.getLogger(__name__).warning(f"通関対応 metric 描画失敗: {_ce}")

    # ── W120+W121 (2026-05-12): 仕入先 価格変動 alert ──
    # ±3% 急騰/急落 + 在庫切れ→復活 を別 metric で表示. Discord 通知なし (DASHBOARD only).
    # H5 fix: monitored_items.ebay_item_id は非 UNIQUE のため GROUP BY mi.id で row 増殖防御.
    # H6 fix: LIMIT 件数 + 別 COUNT クエリで「他 N 件」表示 (旧実装は全件 fetch + Python slice).
    try:
        from monitor.database import get_conn as _pr_get_conn
        _DISPLAY_LIMIT = 10
        with _pr_get_conn() as _pc:
            def _fetch_price_alert_rows(state: str, order_dir: str) -> list:
                sql = f"""SELECT mi.id, mi.sku, mi.source_url,
                                 mi.baseline_price_jpy, mi.current_price_jpy,
                                 MIN(el.title) AS title, MIN(mi.last_check) AS last_check
                          FROM monitored_items mi
                          LEFT JOIN ebay_listings el ON mi.ebay_item_id = el.ebay_item_id
                          WHERE mi.is_active=1 AND mi.price_alert_state=?
                          GROUP BY mi.id
                          ORDER BY ((mi.current_price_jpy * 1.0)
                                    / NULLIF(mi.baseline_price_jpy, 0)) {order_dir}
                          LIMIT {_DISPLAY_LIMIT + 1}"""
                return _pc.execute(sql, (state,)).fetchall()

            def _count_alert_rows(state: str) -> int:
                row = _pc.execute(
                    "SELECT COUNT(*) FROM monitored_items "
                    "WHERE is_active=1 AND price_alert_state=?", (state,)
                ).fetchone()
                return row[0] if row else 0

            _surge_rows = _fetch_price_alert_rows("surge", "DESC")
            _drop_rows = _fetch_price_alert_rows("drop", "ASC")
            _restock_rows = _pc.execute(
                """SELECT mi.id, mi.sku, mi.source_url,
                          mi.baseline_price_jpy, mi.current_price_jpy,
                          MIN(el.title) AS title, MIN(mi.last_check) AS last_check
                   FROM monitored_items mi
                   LEFT JOIN ebay_listings el ON mi.ebay_item_id = el.ebay_item_id
                   WHERE mi.is_active=1 AND mi.price_alert_state='restock'
                   GROUP BY mi.id
                   ORDER BY mi.last_check DESC
                   LIMIT """ + str(_DISPLAY_LIMIT + 1)
            ).fetchall()
            _surge_total = _count_alert_rows("surge")
            _drop_total = _count_alert_rows("drop")
            _restock_total = _count_alert_rows("restock")

        if _surge_total or _drop_total or _restock_total:
            with st.container(border=True):
                st.markdown("### 💰 仕入先 価格変動")
                _pcols = st.columns(3)
                with _pcols[0]:
                    st.metric("📈 急騰 (+3%以上)", _surge_total,
                              help="販売停止 / 価格改定リスク。商品価格見直し推奨.")
                with _pcols[1]:
                    st.metric("📉 急落 (-3%以下)", _drop_total,
                              help="仕入チャンス。即発注検討.")
                with _pcols[2]:
                    st.metric("🔄 在庫復活", _restock_total,
                              help="在庫切れ → 在庫有 遷移 (24h 経過で自動 normal 降格).")

                def _fmt_price_row(row, sign: str):
                    base = row["baseline_price_jpy"] or 0
                    cur = row["current_price_jpy"] or 0
                    if base > 0:
                        pct = (cur - base) / base * 100
                        pct_str = f"**{sign}{abs(pct):.1f}%**"
                    else:
                        pct_str = "(baseline 0)"
                    title = (row["title"] or row["sku"] or "?")[:60]
                    url = row["source_url"] or "#"
                    return (
                        f"- [{title}]({url}) "
                        f"¥{base:,} → ¥{cur:,} {pct_str}"
                    )

                if _surge_total > 0:
                    st.markdown(f"**📈 急騰 (販売停止/値上げリスク、上位 {_DISPLAY_LIMIT} 件)**")
                    for r in _surge_rows[:_DISPLAY_LIMIT]:
                        st.markdown(_fmt_price_row(r, "+"))
                    if _surge_total > _DISPLAY_LIMIT:
                        st.caption(f"... 他 {_surge_total - _DISPLAY_LIMIT} 件")

                if _drop_total > 0:
                    st.markdown(f"**📉 急落 (仕入チャンス、上位 {_DISPLAY_LIMIT} 件)**")
                    for r in _drop_rows[:_DISPLAY_LIMIT]:
                        st.markdown(_fmt_price_row(r, "-"))
                    if _drop_total > _DISPLAY_LIMIT:
                        st.caption(f"... 他 {_drop_total - _DISPLAY_LIMIT} 件")

                if _restock_total > 0:
                    st.markdown(f"**🔄 在庫復活 (上位 {_DISPLAY_LIMIT} 件)**")
                    for r in _restock_rows[:_DISPLAY_LIMIT]:
                        title = (r["title"] or r["sku"] or "?")[:60]
                        url = r["source_url"] or "#"
                        st.markdown(
                            f"- [{title}]({url}) "
                            f"(last_check: {str(r['last_check'])[:16]})"
                        )
                    if _restock_total > _DISPLAY_LIMIT:
                        st.caption(f"... 他 {_restock_total - _DISPLAY_LIMIT} 件")
    except (sqlite3.Error, sqlite3.OperationalError) as _pe:
        import logging as _pl
        _pl.getLogger(__name__).warning(f"価格変動 描画失敗: {_pe}")


    # ── MONO Deck — Interstellar Cockpit Header ──
    # Cooper's cockpit (Endurance) + TARS terminal + Gargantua amber accent
    exec_summary = _cd_execution_summary(get_db_version())
    _sr = (exec_summary['success'] / max(exec_summary['total'], 1) * 100) if exec_summary['total'] > 0 else 0
    _dash_emails_all = _cd_dash_emails(get_db_version(), 50)
    _dash_unconf = [em for em in _dash_emails_all if em.get('confirmed', 0) == 0]
    active = _cd_active_tasks(get_db_version())
    _high_tasks = [t for t in active if t['priority'] in ('高', '中')]

    from datetime import datetime as _dt
    _now_str = _dt.now().strftime("%H:%M:%S")
    _date_str = _dt.now().strftime("%Y · %m · %d")

    # Mission clock: MONO Deck が最初に起動した日からの経過時間 (なければ起点を now 寸前に設定)
    _mission_epoch_file = Path("data/.mission_epoch")
    import time as _t_mod
    if _mission_epoch_file.exists():
        try:
            _mission_start = float(_mission_epoch_file.read_text().strip())
        except Exception:
            _mission_start = _t_mod.time()
    else:
        _mission_start = _t_mod.time()
        try:
            _mission_epoch_file.parent.mkdir(exist_ok=True, parents=True)
            _mission_epoch_file.write_text(str(_mission_start))
        except Exception:
            pass
    _elapsed = int(_t_mod.time() - _mission_start)
    _mission_days = _elapsed // 86400
    _mission_h = (_elapsed % 86400) // 3600
    _mission_m = (_elapsed % 3600) // 60
    _mission_s = _elapsed % 60
    _mission_clock = f"T+{_mission_days:03d}:{_mission_h:02d}:{_mission_m:02d}:{_mission_s:02d}"

    # ステータス色 (Interstellar: amber=caution / red=alert / sage=nominal)
    def _c(val: int, warn: int, crit: int) -> str:
        if val >= crit: return "alert"
        if val >= warn: return "caution"
        return "nominal"
    _inbox_cls = _c(len(_dash_unconf), 1, 4)
    _tasks_cls = "caution" if _high_tasks else "nominal"
    _sr_cls = "alert" if (_sr < 80 and exec_summary['total'] > 0) else "nominal"

    _components.html(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@200;300;400;500;600&family=JetBrains+Mono:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap');
    *{{margin:0;padding:0;box-sizing:border-box;}}
    body{{background:transparent;overflow:hidden;}}

    :root {{
        /* Interstellar poster palette: pure void black + Gargantua accretion disk */
        --void: #030204;
        --space: #06050a;
        --hull: #0c0a0e;
        --panel: #12100d;
        --panel-2: #1a1612;
        --trim: #2a2420;
        --trim-hi: #3a332c;
        --steel: #5a5248;
        --steel-hi: #7a6e5f;
        --placard: #a89d8a;
        --instrument: #e8ddc9;
        --readout: #fbf9f3;
        /* Gargantua accretion disk (actual movie colors) */
        --disk-core: #fff4d6;
        --disk-hot: #ffa84a;
        --disk-mid: #e08a2c;
        --disk-cool: #a85020;
        --disk-dim: #5a2810;
        --void-hole: #050403;
        /* Alerts */
        --alert: #d84c38;
        --nominal: #6b7a5c;
        --caution: #c89b2a;
        /* Fonts */
        --f-term: 'JetBrains Mono', 'Consolas', monospace;
        --f-slab: 'Space Mono', 'JetBrains Mono', monospace;
        --f-movie: 'Inter', sans-serif;
    }}

    .cockpit {{
        background: var(--void);
        position: relative;
        overflow: hidden;
        border: 0;
    }}

    /* ── Deep space star-field (CSS box-shadow stars) ── */
    .stars, .stars-2, .stars-3 {{
        position: absolute; inset: 0;
        pointer-events: none;
    }}
    .stars {{
        background-image:
            radial-gradient(1px 1px at 20px 30px, rgba(255,255,255,0.6), transparent),
            radial-gradient(1px 1px at 60px 70px, rgba(255,255,255,0.4), transparent),
            radial-gradient(1px 1px at 110px 20px, rgba(255,255,255,0.7), transparent),
            radial-gradient(1px 1px at 150px 110px, rgba(255,255,255,0.5), transparent),
            radial-gradient(1px 1px at 200px 50px, rgba(255,255,255,0.8), transparent),
            radial-gradient(1.5px 1.5px at 260px 90px, rgba(255,255,255,0.9), transparent),
            radial-gradient(1px 1px at 320px 30px, rgba(255,240,220,0.6), transparent),
            radial-gradient(1px 1px at 380px 130px, rgba(255,255,255,0.4), transparent),
            radial-gradient(1px 1px at 450px 70px, rgba(255,255,255,0.7), transparent),
            radial-gradient(1px 1px at 530px 20px, rgba(255,255,255,0.5), transparent),
            radial-gradient(1px 1px at 600px 110px, rgba(255,255,255,0.6), transparent),
            radial-gradient(1.2px 1.2px at 680px 60px, rgba(255,255,255,0.8), transparent),
            radial-gradient(1px 1px at 760px 130px, rgba(255,240,220,0.5), transparent),
            radial-gradient(1px 1px at 850px 40px, rgba(255,255,255,0.6), transparent),
            radial-gradient(1px 1px at 920px 90px, rgba(255,255,255,0.7), transparent),
            radial-gradient(1px 1px at 1000px 30px, rgba(255,255,255,0.5), transparent),
            radial-gradient(1.3px 1.3px at 1080px 120px, rgba(255,255,255,0.9), transparent),
            radial-gradient(1px 1px at 1150px 70px, rgba(255,240,220,0.6), transparent);
        background-size: 1200px 160px;
        background-repeat: repeat;
        opacity: 0.9;
    }}
    .stars-2 {{
        background-image:
            radial-gradient(0.8px 0.8px at 40px 50px, rgba(255,255,255,0.3), transparent),
            radial-gradient(0.8px 0.8px at 130px 15px, rgba(255,255,255,0.4), transparent),
            radial-gradient(0.8px 0.8px at 230px 80px, rgba(255,255,255,0.3), transparent),
            radial-gradient(0.8px 0.8px at 310px 120px, rgba(255,255,255,0.5), transparent),
            radial-gradient(0.8px 0.8px at 420px 40px, rgba(255,255,255,0.3), transparent),
            radial-gradient(0.8px 0.8px at 510px 100px, rgba(255,255,255,0.4), transparent),
            radial-gradient(0.8px 0.8px at 620px 30px, rgba(255,255,255,0.3), transparent),
            radial-gradient(0.8px 0.8px at 710px 85px, rgba(255,255,255,0.5), transparent),
            radial-gradient(0.8px 0.8px at 800px 25px, rgba(255,255,255,0.3), transparent),
            radial-gradient(0.8px 0.8px at 880px 115px, rgba(255,255,255,0.4), transparent);
        background-size: 1000px 140px;
        background-repeat: repeat;
        animation: twinkle 6s ease-in-out infinite;
    }}
    .stars-3 {{
        background-image:
            radial-gradient(2px 2px at 180px 60px, rgba(255,240,210,0.9), transparent),
            radial-gradient(2.2px 2.2px at 480px 25px, rgba(210,225,255,0.85), transparent),
            radial-gradient(1.8px 1.8px at 820px 95px, rgba(255,245,220,0.9), transparent),
            radial-gradient(2px 2px at 1050px 50px, rgba(255,230,200,0.8), transparent);
        background-size: 1200px 160px;
        background-repeat: repeat;
        filter: blur(0.3px);
    }}
    @keyframes twinkle {{
        0%, 100% {{ opacity: 0.4; }}
        50% {{ opacity: 0.85; }}
    }}

    /* ── HERO: Gargantua + title (movie poster vibe) ── */
    .hero {{
        position: relative;
        padding: 20px 24px 14px;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 44px;
        min-height: 180px;
        background: radial-gradient(ellipse 900px 260px at 50% 50%, rgba(224,138,44,0.06), transparent);
    }}

    /* Gargantua — SVG black hole + accretion disk */
    .gargantua {{
        width: 280px; height: 156px;
        flex-shrink: 0;
        filter: drop-shadow(0 0 40px rgba(224,138,44,0.25));
        animation: drift 40s ease-in-out infinite;
    }}
    @keyframes drift {{
        0%, 100% {{ transform: translateY(0) scale(1); }}
        50% {{ transform: translateY(-2px) scale(1.01); }}
    }}

    /* Title stack next to Gargantua */
    .title-stack {{
        display: flex; flex-direction: column; gap: 8px;
        position: relative; z-index: 2;
    }}
    .title {{
        font-family: var(--f-movie);
        font-size: 34px; font-weight: 200;
        letter-spacing: 14px;
        color: var(--instrument);
        text-shadow: 0 0 24px rgba(232,221,201,0.18), 0 2px 0 rgba(0,0,0,0.6);
        line-height: 1;
    }}
    .subtitle {{
        font-family: var(--f-term);
        font-size: 10px; font-weight: 400;
        letter-spacing: 5px;
        color: var(--placard);
        text-transform: uppercase;
        padding-left: 2px;
    }}
    .tagline {{
        font-family: var(--f-movie);
        font-size: 11px; font-weight: 300;
        font-style: italic;
        letter-spacing: 2px;
        color: var(--steel-hi);
        margin-top: 6px;
    }}

    /* ── STATUS BAR (mission clock + local time) ── */
    .statusbar {{
        position: relative;
        display: grid;
        grid-template-columns: auto 1fr auto;
        gap: 20px;
        align-items: center;
        padding: 10px 24px;
        background: linear-gradient(180deg, transparent, rgba(3,2,4,0.7));
        border-top: 1px solid rgba(58,51,44,0.5);
        border-bottom: 1px solid rgba(58,51,44,0.5);
    }}
    .status-left {{
        display: flex; align-items: center; gap: 10px;
        font-family: var(--f-term);
        font-size: 10px; letter-spacing: 2.5px;
        color: var(--placard);
        text-transform: uppercase;
    }}
    .status-left .live {{
        color: var(--disk-hot);
        font-size: 9px;
    }}
    .status-left .live::before {{
        content: '●';
        margin-right: 5px;
        animation: blink 2s ease-in-out infinite;
    }}
    @keyframes blink {{
        0%, 100% {{ opacity: 1; }}
        50% {{ opacity: 0.4; }}
    }}
    .mission-clock {{
        text-align: center;
        font-family: var(--f-slab);
        font-size: 13px; font-weight: 500;
        letter-spacing: 4px;
        color: var(--disk-hot);
        font-variant-numeric: tabular-nums;
    }}
    .mission-clock .lbl {{
        display: block;
        font-family: var(--f-term);
        font-size: 8px; font-weight: 400;
        letter-spacing: 3px;
        color: var(--steel);
        text-transform: uppercase;
        margin-bottom: 2px;
    }}
    .local-clock {{
        text-align: right;
        font-family: var(--f-slab);
        font-size: 13px; font-weight: 400;
        letter-spacing: 2px;
        color: var(--instrument);
        font-variant-numeric: tabular-nums;
    }}
    .local-clock .lbl {{
        display: block;
        font-family: var(--f-term);
        font-size: 8px;
        letter-spacing: 3px;
        color: var(--steel);
        text-transform: uppercase;
        margin-bottom: 2px;
    }}

    /* ── TELEMETRY STRIP (TARS-style gauges + Cooper cockpit readouts) ── */
    .tele {{
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 0;
        background: var(--void);
        position: relative;
    }}
    .gauge {{
        position: relative;
        padding: 16px 20px 14px;
        background: rgba(18,16,13,0.85);
        border-right: 1px solid var(--trim-hi);
        border-top: 1px solid var(--trim-hi);
    }}
    .gauge:last-child {{ border-right: 0; }}
    /* vertical accent strip (TARS slab indicator) */
    .gauge::before {{
        content: '';
        position: absolute;
        top: 14px; bottom: 14px; left: 0;
        width: 3px;
        background: var(--nominal);
        box-shadow: 0 0 6px currentColor;
        color: var(--nominal);
    }}
    .gauge.alert::before {{ background: var(--alert); color: var(--alert); }}
    .gauge.caution::before {{ background: var(--caution); color: var(--caution); }}
    .gauge.nominal::before {{ background: var(--nominal); color: var(--nominal); }}

    .gauge-head {{
        display: flex; justify-content: space-between; align-items: baseline;
        margin-bottom: 10px;
    }}
    .gauge-label {{
        font-family: var(--f-term);
        font-size: 8px; font-weight: 500;
        letter-spacing: 2.5px;
        color: var(--placard);
        text-transform: uppercase;
    }}
    .gauge-unit {{
        font-family: var(--f-term);
        font-size: 8px; font-weight: 400;
        letter-spacing: 1.5px;
        color: var(--steel);
        text-transform: uppercase;
    }}

    .readout {{
        display: flex; align-items: baseline; gap: 6px;
        margin-bottom: 8px;
    }}
    .readout .big {{
        font-family: var(--f-slab);
        font-size: 32px; font-weight: 700;
        color: var(--readout);
        line-height: 1;
        font-variant-numeric: tabular-nums;
        letter-spacing: -1px;
    }}
    .gauge.alert .readout .big {{ color: var(--alert); }}
    .gauge.caution .readout .big {{ color: var(--caution); }}
    .gauge.nominal .readout .big {{ color: var(--instrument); }}
    .readout .pct {{
        font-family: var(--f-slab);
        font-size: 14px; font-weight: 500;
        color: var(--steel);
        font-variant-numeric: tabular-nums;
    }}

    /* TARS-style horizontal slider bar */
    .bar-wrap {{
        display: flex; align-items: center; gap: 10px;
        margin-top: 6px;
    }}
    .tars-bar {{
        flex: 1; height: 6px;
        background: var(--trim);
        position: relative; overflow: hidden;
    }}
    .tars-bar .fill {{
        position: absolute;
        top: 0; left: 0; bottom: 0;
        background: var(--nominal);
        box-shadow: 0 0 6px currentColor;
        color: var(--nominal);
    }}
    .gauge.alert .tars-bar .fill {{ background: var(--alert); color: var(--alert); }}
    .gauge.caution .tars-bar .fill {{ background: var(--caution); color: var(--caution); }}
    .tars-bar .tick {{
        position: absolute;
        top: 0; bottom: 0;
        width: 1px;
        background: var(--steel);
        opacity: 0.3;
    }}
    .bar-note {{
        font-family: var(--f-term);
        font-size: 8px; font-weight: 400;
        letter-spacing: 1.5px;
        color: var(--steel-hi);
        text-transform: uppercase;
        white-space: nowrap;
    }}
    </style>

    <div class="cockpit">
        <!-- Deep space star-field layers -->
        <div class="stars"></div>
        <div class="stars-2"></div>
        <div class="stars-3"></div>

        <!-- HERO: Gargantua black hole + movie-poster title -->
        <div class="hero">
            <svg class="gargantua" viewBox="0 0 280 156" xmlns="http://www.w3.org/2000/svg">
              <defs>
                <!-- Radial disk glow -->
                <radialGradient id="garg-glow" cx="50%" cy="50%" r="50%">
                  <stop offset="0%" stop-color="#fff4d6" stop-opacity="0"/>
                  <stop offset="55%" stop-color="#ffa84a" stop-opacity="0"/>
                  <stop offset="72%" stop-color="#e08a2c" stop-opacity="0.45"/>
                  <stop offset="85%" stop-color="#a85020" stop-opacity="0.25"/>
                  <stop offset="100%" stop-color="#5a2810" stop-opacity="0"/>
                </radialGradient>
                <!-- Horizontal disk band -->
                <linearGradient id="garg-disk" x1="0%" x2="100%" y1="0%" y2="0%">
                  <stop offset="0%" stop-color="#5a2810" stop-opacity="0"/>
                  <stop offset="12%" stop-color="#a85020" stop-opacity="0.6"/>
                  <stop offset="30%" stop-color="#e08a2c" stop-opacity="0.95"/>
                  <stop offset="45%" stop-color="#ffa84a"/>
                  <stop offset="50%" stop-color="#fff4d6"/>
                  <stop offset="55%" stop-color="#ffa84a"/>
                  <stop offset="70%" stop-color="#e08a2c" stop-opacity="0.95"/>
                  <stop offset="88%" stop-color="#a85020" stop-opacity="0.6"/>
                  <stop offset="100%" stop-color="#5a2810" stop-opacity="0"/>
                </linearGradient>
                <!-- Lensed arc gradient -->
                <linearGradient id="garg-arc" x1="0%" x2="100%" y1="0%" y2="0%">
                  <stop offset="0%" stop-color="#5a2810" stop-opacity="0"/>
                  <stop offset="20%" stop-color="#a85020" stop-opacity="0.7"/>
                  <stop offset="50%" stop-color="#ffa84a" stop-opacity="0.95"/>
                  <stop offset="80%" stop-color="#a85020" stop-opacity="0.7"/>
                  <stop offset="100%" stop-color="#5a2810" stop-opacity="0"/>
                </linearGradient>
                <!-- Hole black -->
                <radialGradient id="garg-hole" cx="50%" cy="50%" r="55%">
                  <stop offset="0%" stop-color="#030203"/>
                  <stop offset="85%" stop-color="#050403"/>
                  <stop offset="100%" stop-color="#1a0f08"/>
                </radialGradient>
                <filter id="garg-blur">
                  <feGaussianBlur stdDeviation="0.8"/>
                </filter>
              </defs>

              <!-- outer glow -->
              <ellipse cx="140" cy="78" rx="130" ry="60" fill="url(#garg-glow)" />

              <!-- Top lensed arc (gravitational lensing rear of disk over hole) -->
              <path d="M 20 78 Q 140 -6 260 78" fill="none" stroke="url(#garg-arc)" stroke-width="14" stroke-linecap="round" opacity="0.75"/>
              <path d="M 40 78 Q 140 18 240 78" fill="none" stroke="#ffe0a8" stroke-width="3.5" stroke-linecap="round" opacity="0.9"/>
              <path d="M 52 78 Q 140 26 228 78" fill="none" stroke="#fff4d6" stroke-width="1.5" stroke-linecap="round" opacity="0.9" filter="url(#garg-blur)"/>

              <!-- Bottom lensed arc (lensing under) -->
              <path d="M 20 78 Q 140 162 260 78" fill="none" stroke="url(#garg-arc)" stroke-width="10" stroke-linecap="round" opacity="0.6"/>

              <!-- Horizontal accretion disk (foreground flat disk) -->
              <ellipse cx="140" cy="78" rx="128" ry="8" fill="url(#garg-disk)" opacity="0.95"/>
              <ellipse cx="140" cy="78" rx="128" ry="3.5" fill="#fff4d6" opacity="0.9"/>
              <ellipse cx="140" cy="78" rx="100" ry="1.5" fill="#ffffff" opacity="0.7"/>

              <!-- Central black hole shadow (slightly larger than horizon, dark) -->
              <ellipse cx="140" cy="78" rx="32" ry="30" fill="url(#garg-hole)"/>

              <!-- Inner photon ring bright edge -->
              <ellipse cx="140" cy="78" rx="34" ry="31" fill="none" stroke="#ffa84a" stroke-width="0.6" opacity="0.45"/>
              <ellipse cx="140" cy="78" rx="36" ry="32" fill="none" stroke="#e08a2c" stroke-width="0.3" opacity="0.3"/>
            </svg>

            <div class="title-stack">
                <div class="title">M O N O &nbsp; D E C K</div>
                <div class="subtitle">MONOHONPO · Internal Ops Console</div>
                <div class="tagline">"Love is the one thing we're capable of perceiving that transcends dimensions of time and space."</div>
            </div>
        </div>

        <!-- STATUS BAR (mission clock + local) -->
        <div class="statusbar">
            <div class="status-left">
                <span class="live">LIVE</span>
                <span>· eBay LINK ACTIVE · All systems nominal</span>
            </div>
            <div class="mission-clock">
                <span class="lbl">Mission Elapsed</span>
                {_mission_clock}
            </div>
            <div class="local-clock">
                <span class="lbl">Local · JST</span>
                {_now_str} &nbsp;·&nbsp; {_date_str}
            </div>
        </div>

        <!-- Telemetry strip (TARS personality setting inspired gauges) -->
        <div class="tele">
            <div class="gauge {_inbox_cls}">
                <div class="gauge-head">
                    <span class="gauge-label">Inbox</span>
                    <span class="gauge-unit">Msg · Unread</span>
                </div>
                <div class="readout">
                    <span class="big">{len(_dash_unconf):02d}</span>
                    <span class="pct">/{len(_dash_emails_all)}</span>
                </div>
                <div class="bar-wrap">
                    <div class="tars-bar">
                        <span class="fill" style="width:{min(100, len(_dash_unconf)*20)}%"></span>
                        <span class="tick" style="left:25%"></span>
                        <span class="tick" style="left:50%"></span>
                        <span class="tick" style="left:75%"></span>
                    </div>
                    <span class="bar-note">{'ATTN' if _dash_unconf else 'CLEAR'}</span>
                </div>
            </div>

            <div class="gauge {_tasks_cls}">
                <div class="gauge-head">
                    <span class="gauge-label">Tasks</span>
                    <span class="gauge-unit">Active · Pri</span>
                </div>
                <div class="readout">
                    <span class="big">{len(active):02d}</span>
                    <span class="pct">/ {len(_high_tasks)} HI</span>
                </div>
                <div class="bar-wrap">
                    <div class="tars-bar">
                        <span class="fill" style="width:{min(100, len(active)*10)}%"></span>
                        <span class="tick" style="left:25%"></span>
                        <span class="tick" style="left:50%"></span>
                        <span class="tick" style="left:75%"></span>
                    </div>
                    <span class="bar-note">{'CAUTION' if _high_tasks else 'NOMINAL'}</span>
                </div>
            </div>

            <div class="gauge nominal">
                <div class="gauge-head">
                    <span class="gauge-label">Exec Log</span>
                    <span class="gauge-unit">Run · 24h</span>
                </div>
                <div class="readout">
                    <span class="big">{exec_summary['total']:03d}</span>
                    <span class="pct">PASS {exec_summary['success']}</span>
                </div>
                <div class="bar-wrap">
                    <div class="tars-bar">
                        <span class="fill" style="width:{min(100, exec_summary['total']*2)}%"></span>
                        <span class="tick" style="left:25%"></span>
                        <span class="tick" style="left:50%"></span>
                        <span class="tick" style="left:75%"></span>
                    </div>
                    <span class="bar-note">FAIL {exec_summary['failed']:02d}</span>
                </div>
            </div>

            <div class="gauge {_sr_cls}">
                <div class="gauge-head">
                    <span class="gauge-label">Success Rate</span>
                    <span class="gauge-unit">Rolling · %</span>
                </div>
                <div class="readout">
                    <span class="big">{_sr:.0f}</span>
                    <span class="pct">%</span>
                </div>
                <div class="bar-wrap">
                    <div class="tars-bar">
                        <span class="fill" style="width:{_sr:.0f}%"></span>
                        <span class="tick" style="left:25%"></span>
                        <span class="tick" style="left:50%"></span>
                        <span class="tick" style="left:75%"></span>
                    </div>
                    <span class="bar-note">{'ALERT' if _sr<80 and exec_summary['total']>0 else 'NOMINAL'}</span>
                </div>
            </div>
        </div>
    </div>
    """, height=420)

    # セクションヘッダーCSS
    _section_css = """
    <style>
    .sec-header {
        font-family: 'Orbitron', sans-serif;
        font-size: 11px; font-weight: 500;
        color: rgba(77,217,240,0.85);
        letter-spacing: 3px;
        text-transform: uppercase;
        padding: 10px 16px;
        margin: 16px 0 10px 0;
        border: 1px solid rgba(77,217,240,0.35);
        border-left: 3px solid rgba(77,217,240,0.6);
        border-radius: 4px;
        background: linear-gradient(135deg, rgba(77,217,240,0.08), rgba(77,217,240,0.02));
        box-shadow: 0 0 15px rgba(77,217,240,0.05), inset 0 0 20px rgba(77,217,240,0.02);
        position: relative;
    }
    .sec-header::before {
        content: ''; position: absolute; top: 3px; bottom: 3px; left: -1px; width: 2px;
        background: rgba(77,217,240,0.8); box-shadow: 0 0 8px rgba(77,217,240,0.4);
    }
    .sec-header::after {
        content: ''; position: absolute; top: 0; right: 10px; left: 60%; height: 1px;
        background: linear-gradient(90deg, transparent, rgba(77,217,240,0.3));
    }
    .task-section {
        font-family: 'Exo 2', sans-serif;
        font-size: 11px; font-weight: 400;
        color: rgba(255,145,0,0.8);
        letter-spacing: 1.5px;
        text-transform: uppercase;
        padding: 4px 0;
        border-bottom: 1px solid rgba(255,145,0,0.15);
        margin: 8px 0 4px 0;
    }
    .pri-hi { color: #ff4444; font-family: 'Share Tech Mono', monospace; font-size: 11px; }
    .pri-md { color: #ff9100; font-family: 'Share Tech Mono', monospace; font-size: 11px; }
    .mail-row {
        padding: 8px 14px;
        border-left: 2px solid rgba(77,217,240,0.4);
        margin-bottom: 6px;
        background: rgba(77,217,240,0.04);
        border-radius: 0 4px 4px 0;
        border: 1px solid rgba(77,217,240,0.1);
        border-left: 3px solid rgba(77,217,240,0.4);
    }
    .mail-row.sale { border-left-color: rgba(112,240,128,0.6); background: rgba(112,240,128,0.04); }
    .mail-row.return { border-left-color: rgba(240,64,80,0.6); background: rgba(240,64,80,0.04); }
    .mail-row.offer { border-left-color: rgba(240,160,48,0.6); background: rgba(240,160,48,0.04); }
    .clear-status {
        font-family: 'Share Tech Mono', monospace;
        color: rgba(118,255,3,0.7); font-size: 12px; letter-spacing: 1px;
    }
    </style>
    """
    st.markdown(_section_css, unsafe_allow_html=True)

    # メイン 2カラム
    # W13 (2026-04-24): ニュース表示枠拡張 (2→3) user 要求「見やすく」反映
    # 旧 [3, 2] = action 60% / intel 40% → 新 [2, 3] = action 40% / intel 60%
    col_action, col_intel = st.columns([2, 3], gap="large")

    with col_action:
        # ── INBOX ──
        st.markdown('<div class="sec-header">INBOX</div>', unsafe_allow_html=True)

        def _extract_buyer_message(body: str) -> str:
            """メール本文からバイヤーの実際のメッセージを抽出"""
            if not body:
                return ""
            # "New message: ..." パターン
            m = _re_dash.search(r'New message:\s*(.+?)(?:\n|$)', body)
            if m:
                msg = m.group(1).strip()
                if msg and len(msg) > 2:
                    return msg[:80]
            # 最初の意味のある行
            for line in body.split('\n'):
                line = line.strip()
                if line and len(line) > 5 and not line.startswith('New message from') and 'Reply' not in line:
                    return line[:80]
            return ""

        _inbox_confirm_ids = []
        _has_urgent = False

        # 優先度ベース判定 (Claude judgments prioritized, keyword fallback)
        _urgent_priorities = {'urgent', 'high'}
        # 2026-05-21 Phase A: customs_request (FedEx/UPS/DHL 通関情報要求) も urgent。
        # 期限内 (deadline) に提出しないとリードタイム延伸 / 返品リスク = money-direct。
        _urgent_categories = {
            'buyer_message', 'sale', 'offer', 'return', 'customs_request',
        }

        def _format_email_date(date_str: str) -> tuple[str, str]:
            """Gmail の date ヘッダ (RFC2822) を「N日前」と「MM/DD HH:MM」の2形式で返す。"""
            if not date_str:
                return "", ""
            try:
                from email.utils import parsedate_to_datetime
                from datetime import datetime, timezone as _tz
                dt = parsedate_to_datetime(date_str)
                if dt is None:
                    return "", date_str[:20]
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                now = datetime.now(_tz.utc)
                delta = now - dt
                total_sec = delta.total_seconds()
                if total_sec < 0:
                    rel = "未来"
                elif total_sec < 3600:
                    rel = f"{int(total_sec//60)}分前"
                elif total_sec < 86400:
                    rel = f"{int(total_sec//3600)}時間前"
                elif total_sec < 604800:
                    rel = f"{int(total_sec//86400)}日前"
                else:
                    rel = f"{int(total_sec//86400)}日前"
                # ローカル時刻で絶対表示（JSTに変換）
                try:
                    from datetime import timedelta as _td
                    dt_local = dt.astimezone(_tz(_td(hours=9)))
                    abs_str = dt_local.strftime("%m/%d %H:%M")
                except Exception:
                    abs_str = dt.strftime("%m/%d %H:%M")
                return rel, abs_str
            except Exception:
                return "", (date_str[:20] if date_str else "")

        # 2026-05-21 Codex HIGH 対応: category_ai='sale' (Claude判定 売上通知) で
        # priority_ai=high/urgent の漏れを INBOX 側でも guard。rule category と
        # category_ai のどちらかに excluded カテゴリが入っていれば skip (sale /
        # supplier_purchase / listing_notification は dashboard 表示不要)。
        _inbox_excluded_categories = {
            'supplier_purchase', 'sale', 'listing_notification',
        }
        for _em in _dash_unconf:
            # Claude 判定（あれば優先）、なければ従来の keyword カテゴリ
            _pri_ai = _em.get('priority_ai') or ''
            _cat_ai = _em.get('category_ai') or ''
            _cat_rule = _em.get('category', 'other')
            cat = _cat_ai or _cat_rule
            # excluded カテゴリは rule/AI どちらか hit で skip (Codex HIGH 漏れ穴塞ぎ)
            if _cat_rule in _inbox_excluded_categories \
                    or _cat_ai in _inbox_excluded_categories:
                continue
            if (_pri_ai and _pri_ai in _urgent_priorities) or (not _pri_ai and cat in _urgent_categories):
                _has_urgent = True
            else:
                continue  # 対応不要扱い

            subj = _em.get('subject', '')
            sender = _em.get('sender', '').split('<')[0].strip().strip('"').replace('eBay - ', '')
            gmail_id = _em.get('gmail_id', '')
            gmail_link = f"https://mail.google.com/mail/u/0/#inbox/{gmail_id}" if gmail_id else ""
            body = _em.get('body_text', '')

            # Claude 要約（新方式）優先、無ければ旧フォールバック
            summary_ja = (_em.get('summary_ja') or '').strip()
            action_ja = (_em.get('action_ja') or '').strip()
            buyer_msg_ja = (_em.get('buyer_message_ja') or '').strip()

            _pm = _re_dash.search(r'about (.+?)(?:\s*#\d|$)', subj)
            product = _pm.group(1).strip()[:40] if _pm else ''

            # カテゴリ別の色/ラベル
            if cat == 'buyer_message':
                action_color = 'rgba(240,160,48,0.85)'
                row_cls = 'mail-row'
                is_reply = subj.startswith('Re:')
                type_label = f'{sender} — {"返信" if is_reply else "問い合わせ"}'
            elif cat == 'sale':
                action_color = 'rgba(112,240,128,0.85)'
                row_cls = 'mail-row sale'
                type_label = '売上通知'
            elif cat == 'offer':
                action_color = 'rgba(240,160,48,0.85)'
                row_cls = 'mail-row offer'
                type_label = f'{sender} — オファー'
            elif cat == 'return':
                action_color = 'rgba(240,64,80,0.85)'
                row_cls = 'mail-row return'
                type_label = f'{sender} — 返品リクエスト'
            else:
                action_color = 'rgba(180,200,220,0.7)'
                row_cls = 'mail-row'
                type_label = f'{sender} — {cat}'

            # 優先度バッジ（Claude 判定）
            _pri_badge = ''
            if _pri_ai == 'urgent':
                _pri_badge = '<span style="color:rgba(240,64,80,0.9);font-size:11px;font-weight:700;margin-right:6px;">[最優先]</span>'
            elif _pri_ai == 'high':
                _pri_badge = '<span style="color:rgba(240,160,48,0.9);font-size:11px;font-weight:700;margin-right:6px;">[高]</span>'

            # 受信日時（相対＋絶対）
            _rel, _abs = _format_email_date(_em.get('date', ''))
            _date_html = ''
            if _rel or _abs:
                # 1時間以内は緑、1日以内は通常、1日以上は薄灰
                _age_color = 'rgba(112,240,128,0.85)' if '分前' in _rel or '時間前' in _rel else \
                             'rgba(180,200,220,0.7)'
                _date_html = (
                    f'<span style="color:{_age_color};font-size:11px;margin-right:6px;">'
                    f'{html.escape(_rel)}'
                    + (f' <span style="color:rgba(150,170,190,0.6);">({html.escape(_abs)})</span>'
                       if _abs else '')
                    + '</span>'
                )

            # チェックボックス + 情報カード
            _chk = st.checkbox(
                f"{type_label} — {product[:25]}" if product else type_label,
                key=f"inbox_{gmail_id}",
            )
            if _chk:
                _inbox_confirm_ids.append(gmail_id)

            # 詳細情報をHTMLカードで（XSS対策）
            _link_safe = html.escape(gmail_link or "", quote=True)
            link_btn = f'<a href="{_link_safe}" target="_blank" style="font-size:11px;color:rgba(77,217,240,0.7);float:right;">▸ Gmailで開く</a>' if gmail_link else ''

            # バイヤー実メッセージ（Claude の buyer_message_ja、なければ body_text抽出）
            if not buyer_msg_ja and body:
                buyer_msg_ja = _extract_buyer_message(body)
            quote_html = ''
            if buyer_msg_ja:
                quote_html = (
                    f'<div style="color:#d0e4f0;font-size:12px;margin:3px 0;padding:4px 8px;'
                    f'background:rgba(77,217,240,0.06);border-radius:3px;border-left:2px solid rgba(77,217,240,0.4);">'
                    f'「{html.escape(buyer_msg_ja[:150])}」</div>'
                )

            # 要約 (Claude)
            summary_html = ''
            if summary_ja:
                summary_html = (
                    f'<div style="color:#e0ecfa;font-size:12px;margin:4px 0 2px 0;">'
                    f'{html.escape(summary_ja[:200])}</div>'
                )

            # アクション (Claude)
            action_text = action_ja or '対応を検討してください'
            action_html = (
                f'<span style="color:{action_color};font-size:11px;">▸ {html.escape(action_text)}</span>'
            )

            st.markdown(
                f'<div class="{row_cls}" style="margin-top:-8px;margin-bottom:8px;">'
                f'{link_btn}'
                f'{_pri_badge}'
                f'{_date_html}'
                f'<span style="color:#a8c4d8;font-size:12px;">{html.escape(product or "")}</span>'
                f'{summary_html}'
                f'{quote_html}'
                f'{action_html}'
                f'</div>', unsafe_allow_html=True)

        if _inbox_confirm_ids:
            if st.button(f"{len(_inbox_confirm_ids)}件を確認済みにする", type="primary", key="inbox_confirm"):
                set_email_confirmed(_inbox_confirm_ids)
                bump_db_version()  # W134 Step2: 書込後 read-cache 無効化
                st.rerun()

        # 2026-04-22: MAIL タブ廃止に伴い、非緊急メールもダッシュボードに表示する。
        # urgent 判定に漏れたメール (eBay promotion / feedback / payment 通知等) を
        # 参考セクションとして下に並べる。feedback memory: expander 禁止ルールに従い、
        # セクション区切り線とリスト表示で toggle 無しで表示する。
        # 2026-05-21 user 要望: REFERENCE にも本当に重要なものだけ。
        # promo (eBay キャンペーン等) は high/urgent priority のみ通す。
        # supplier_purchase / sale は fetch 時に除外済だが念のため safety guard。
        _ref_excluded_categories = {
            'supplier_purchase', 'sale', 'listing_notification',
        }
        _non_urgent = []
        for _em in _dash_unconf:
            _pri_ai = _em.get('priority_ai') or ''
            _cat_ai = _em.get('category_ai') or ''
            _cat_rule = _em.get('category', 'other')
            cat = _cat_ai or _cat_rule
            is_urgent = (_pri_ai and _pri_ai in _urgent_priorities) or \
                        (not _pri_ai and cat in _urgent_categories)
            if is_urgent:
                continue
            # safety: 除外カテゴリ (rule または AI 判定どちらかに含まれていれば skip)
            if _cat_rule in _ref_excluded_categories \
                    or _cat_ai in _ref_excluded_categories:
                continue
            # promo は high/urgent priority のみ REFERENCE に出す
            if _cat_ai == 'promo' and _pri_ai not in ('high', 'urgent'):
                continue
            _non_urgent.append(_em)

        if _non_urgent:
            st.markdown(
                '<div class="sec-header" style="margin-top:24px;">'
                'REFERENCE &middot; NON-URGENT INBOX '
                f'({len(_non_urgent)})</div>',
                unsafe_allow_html=True,
            )
            _ref_confirm_ids = []
            for _em in _non_urgent[:30]:  # 上位 30 件のみ (HUD 過密防止)
                gmail_id = _em.get('gmail_id', '')
                gmail_link = f"https://mail.google.com/mail/u/0/#inbox/{gmail_id}" if gmail_id else ""
                subj = _em.get('subject', '')
                sender = _em.get('sender', '').split('<')[0].strip().strip('"').replace('eBay - ', '')
                summary_ja = (_em.get('summary_ja') or '').strip()
                _rel, _abs = _format_email_date(_em.get('date', ''))
                _date_str = f"{_rel}" if _rel else (_abs or '')

                _chk = st.checkbox(
                    f"{sender[:24]} — {subj[:60]}",
                    key=f"inbox_ref_{gmail_id}",
                )
                if _chk:
                    _ref_confirm_ids.append(gmail_id)

                _link_safe = html.escape(gmail_link or "", quote=True)
                _link_btn = (
                    f'<a href="{_link_safe}" target="_blank" '
                    f'style="font-size:11px;color:rgba(77,217,240,0.5);float:right;">▸ Gmail</a>'
                    if gmail_link else ''
                )
                _summary_line = (
                    f'<div style="color:#c0d4e8;font-size:11px;margin-top:2px;">'
                    f'{html.escape(summary_ja[:180])}</div>'
                ) if summary_ja else ''
                st.markdown(
                    f'<div class="mail-row" style="margin-top:-6px;margin-bottom:6px;'
                    f'border-left-color:rgba(168,196,216,0.3);background:rgba(168,196,216,0.03);">'
                    f'{_link_btn}'
                    f'<span style="color:rgba(168,196,216,0.55);font-size:10px;">{html.escape(_date_str)}</span>'
                    f'{_summary_line}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            if _ref_confirm_ids:
                if st.button(
                    f"{len(_ref_confirm_ids)}件を確認済みにする",
                    type="secondary", key="inbox_ref_confirm",
                ):
                    set_email_confirmed(_ref_confirm_ids)
                    bump_db_version()  # W134 Step2: 書込後 read-cache 無効化
                    st.rerun()
        elif not _dash_unconf:
            st.markdown('<span class="clear-status">問題なし</span>', unsafe_allow_html=True)

        # ── TASKS ──
        st.markdown('<div class="sec-header">TASKS</div>', unsafe_allow_html=True)
        show_archive = st.toggle("完了タスクを表示", value=False, key="show_archive")

        if show_archive:
            archived = get_archived_tasks()
            if archived:
                for task in archived:
                    st.markdown(f"~~{task['name']}~~ <span style='color:#5a7a96;font-size:11px;'>({task['completed_date']})</span>", unsafe_allow_html=True)
            else:
                st.caption("—")
        else:
            if active:
                current_section = ""
                done_tasks = []
                for i, task in enumerate(active):
                    if task['section'] != current_section:
                        current_section = task['section']
                        st.markdown(f'<div class="task-section">{current_section}</div>', unsafe_allow_html=True)

                    pri_html = ""
                    if task['priority'] == '高':
                        pri_html = '<span class="pri-hi">[HIGH]</span> '
                    elif task['priority'] == '中':
                        pri_html = '<span class="pri-md">[MED]</span> '

                    dl = f" `{task['deadline']}`" if task['deadline'] and task['deadline'] != '未定' else ""
                    link = f" [MAIL]({task['link']})" if task.get('link') else ""

                    # 並び順変更でチェック状態がずれないよう、タスク名ベースのkeyを使う
                    _task_key = f"task_done_{task.get('section','')}_{task.get('name','')[:40]}_{i}"
                    done = st.checkbox(f"{task['name']}{dl}{link}", key=_task_key)
                    if pri_html:
                        st.markdown(f"<div style='margin-top:-18px;margin-bottom:4px;padding-left:28px;'>{pri_html}</div>", unsafe_allow_html=True)
                    if done:
                        done_tasks.append(task['name'])

                if done_tasks:
                    if st.button(f"{len(done_tasks)}件を完了にする", type="primary"):
                        for name in done_tasks:
                            complete_task(name)
                        st.rerun()
            else:
                st.markdown('<span class="clear-status">全タスク完了</span>', unsafe_allow_html=True)

    with col_intel:
        # INTELLIGENCE (ライバル検出レポート) は「競合監視」タブと内容が重複するため
        # 2026-04-23 にダッシュボードから削除。get_latest_research() 自体は
        # .company/research/ を読むので今後 AI ニュース等の他ジャンルを掲載する
        # 用途で復活させる余地あり。

        # ── NEWS ──
        st.markdown('<div class="sec-header">NEWS</div>', unsafe_allow_html=True)
        from datetime import date as _date_cls

        # 優先: news_items テーブル（Claude要約付き）、フォールバック: 旧JSONファイル
        # W13 (2026-04-24): source_type / source_handle / engagement_count を取得
        _news_db_rows = []
        try:
            from monitor.database import get_conn as _get_conn_news
            with _get_conn_news() as _c:
                _news_db_rows = [dict(r) for r in _c.execute(
                    """SELECT source, title, url, summary_ja, impact_ja, impact_level, categories, checked_at,
                              COALESCE(source_type, 'web') AS source_type,
                              COALESCE(source_handle, '') AS source_handle,
                              COALESCE(engagement_count, 0) AS engagement_count
                       FROM news_items
                       WHERE checked_at >= datetime('now','-3 days')
                       ORDER BY CASE impact_level
                                 WHEN 'high' THEN 0
                                 WHEN 'medium' THEN 1
                                 WHEN 'low' THEN 2
                                 ELSE 3 END, checked_at DESC
                       LIMIT 20"""
                ).fetchall()]
        except Exception as _e:
            _news_db_rows = []

        if _news_db_rows:
            _high_db = [n for n in _news_db_rows if n.get('impact_level') == 'high']
            _med_db = [n for n in _news_db_rows if n.get('impact_level') == 'medium']
            # Claude 要約ベース表示 (W13: ソースタグ + engagement 追加、表示数 8 件に拡張)
            _src_label = {
                'x': 'X', 'reddit': 'Reddit', 'hn': 'HN', 'web': 'Web',
            }
            _src_color = {
                'x': 'rgba(120,200,255,0.9)',
                'reddit': 'rgba(255,140,80,0.9)',
                'hn': 'rgba(255,180,60,0.9)',
                'web': 'rgba(160,180,200,0.85)',
            }
            for _n in (_high_db + _med_db)[:8]:
                _lvl = _n.get('impact_level') or 'low'
                _accent = {'high': 'rgba(240,64,80,0.55)', 'medium': 'rgba(240,200,48,0.55)',
                           'low': 'rgba(120,180,255,0.45)'}.get(_lvl, 'rgba(160,180,200,0.4)')
                _badge = {'high': '[高影響]', 'medium': '[中影響]', 'low': '[低影響]'}.get(_lvl, '')
                _st = (_n.get('source_type') or 'web').lower()
                _src_tag = _src_label.get(_st, 'Web')
                _src_tag_color = _src_color.get(_st, 'rgba(160,180,200,0.85)')
                _handle = html.escape((_n.get('source_handle') or '')[:24])
                _handle_part = f' {_handle}' if _handle else ''
                _src_html = (
                    f'<span style="background:rgba(0,0,0,0.3);color:{_src_tag_color};'
                    f'padding:1px 6px;border-radius:3px;font-size:10px;letter-spacing:1px;">'
                    f'{_src_tag}{_handle_part}</span>'
                )
                _eng = int(_n.get('engagement_count') or 0)
                _eng_html = (
                    f'<span style="color:rgba(180,220,255,0.45);font-size:10px;'
                    f'margin-left:6px;">♥ {_eng:,}</span>'
                ) if _eng > 0 else ''
                _src = html.escape(_n.get('source') or '')
                _sum = html.escape((_n.get('summary_ja') or _n.get('title') or '')[:200])
                _imp = html.escape((_n.get('impact_ja') or '')[:150])
                _url = html.escape(_n.get('url') or '', quote=True)
                _title_or_link = (
                    f'<a href="{_url}" target="_blank" style="color:rgba(120,200,255,0.9);text-decoration:none;">'
                    f'{html.escape((_n.get("title") or "")[:80])}</a>'
                ) if _url else html.escape((_n.get('title') or '')[:80])
                st.markdown(
                    f'<div style="border-left:2px solid {_accent};padding:6px 12px;margin-bottom:8px;'
                    f'background:rgba(80,120,180,0.03);border-radius:0 4px 4px 0;">'
                    f'<div style="display:flex;gap:8px;align-items:center;margin-bottom:4px;flex-wrap:wrap;">'
                    f'{_src_html}'
                    f'<span style="color:rgba(180,220,255,0.55);font-size:10px;letter-spacing:1px;">{_badge}</span>'
                    f'{_eng_html}'
                    f'</div>'
                    f'<span style="font-size:13px;color:#e0ecfa;line-height:1.5;">{_sum}</span>'
                    + (f'<br><span style="color:rgba(160,220,255,0.7);font-size:11px;">▸ 影響: {_imp}</span>' if _imp else '')
                    + f'<br><span style="font-size:10px;color:#5a7a96;">{_title_or_link}</span>'
                    f'</div>', unsafe_allow_html=True)
            # 早期 return（旧 file-based 表示はスキップ）
            _news_file = None
        else:
            _news_file = Path(__file__).parent / "data" / "news" / f"{_date_cls.today().isoformat()}-news.json"
        if _news_file and _news_file.exists():
            import json as _nj
            _news_items = _nj.loads(_news_file.read_text(encoding="utf-8"))
            _high_news = [n for n in _news_items if n.get("impact") == "high"]
            _med_news = [n for n in _news_items if n.get("impact") == "medium"]

            # 技術制約との照合キーワード
            _constraint_keywords = {
                "playwright": "動的コンテンツ取得",
                "selenium": "動的コンテンツ取得",
                "browser": "動的コンテンツ取得",
                "mercari": "マルチプラットフォームAPI",
                "yahoo auction": "マルチプラットフォームAPI",
                "agent sdk": "スクリプト↔AI自動連携",
                "mcp": "MCP連携・マルチプラットフォーム",
                "vision": "画像認識による判定",
                "image recognition": "画像認識による判定",
                "ebay api": "eBay Research自動化",
                "ebay research": "eBay Research自動化",
                "tool use": "スクリプト↔AI自動連携",
            }

            _constraint_hits = []
            for _n in _news_items:
                _title_lower = (_n.get("title") or "").lower()
                _kw = (_n.get("matched_keyword") or "").lower()
                for _ck, _cv in _constraint_keywords.items():
                    if _ck in _title_lower or _ck in _kw:
                        _constraint_hits.append({"news": _n, "constraint": _cv})
                        break

            # 技術制約に関連するニュースを優先表示
            if _constraint_hits:
                st.markdown('<div style="border:1px solid rgba(118,255,3,0.3);border-radius:6px;padding:8px 12px;margin-bottom:8px;background:rgba(118,255,3,0.04);">'
                    '<span style="color:rgba(118,255,3,0.8);font-size:11px;letter-spacing:1px;">CONSTRAINT CHECK — 技術制約に関連</span></div>', unsafe_allow_html=True)
                for _ch in _constraint_hits[:3]:
                    _n = _ch["news"]
                    _title = html.escape((_n.get("title") or "")[:55])
                    _source = html.escape(_n.get("source") or "")
                    _constraint = html.escape(_ch.get("constraint") or "")
                    st.markdown(f'<div style="border-left:2px solid rgba(118,255,3,0.5);padding:4px 10px;margin-bottom:4px;background:rgba(118,255,3,0.03);border-radius:0 4px 4px 0;">'
                        f'<span style="font-size:13px;">{_title}</span><br>'
                        f'<span style="color:rgba(118,255,3,0.6);font-size:11px;">▸ {_constraint}</span> '
                        f'<span style="color:#5a7a96;font-size:11px;">({_source})</span></div>', unsafe_allow_html=True)

            if _high_news:
                for _n in _high_news:
                    _title = html.escape((_n.get("title") or "")[:60])
                    _source = html.escape(_n.get("source") or "")
                    _kw = html.escape(_n.get("matched_keyword") or "")
                    st.markdown(f'<div style="border-left:2px solid rgba(240,64,80,0.5);padding:4px 10px;margin-bottom:4px;background:rgba(240,64,80,0.04);border-radius:0 4px 4px 0;">'
                        f'<strong>{_title}</strong><br>'
                        f'<span style="color:#5a7a96;font-size:11px;">{_source} — [{_kw}]</span></div>', unsafe_allow_html=True)

            if _med_news:
                _remaining_med = [n for n in _med_news if not any(c["news"].get("title") == n.get("title") for c in _constraint_hits)]
                for _n in _remaining_med[:3]:
                    _title = html.escape((_n.get("title") or "")[:55])
                    _source = html.escape(_n.get("source") or "")
                    _kw = html.escape(_n.get("matched_keyword") or "")
                    st.markdown(f'<div style="border-left:2px solid rgba(240,160,48,0.4);padding:4px 10px;margin-bottom:4px;background:rgba(240,160,48,0.03);border-radius:0 4px 4px 0;">'
                        f'<span style="font-size:13px;">{_title}</span><br>'
                        f'<span style="color:#5a7a96;font-size:11px;">{_source} — [{_kw}]</span></div>', unsafe_allow_html=True)
                if len(_remaining_med) > 3:
                    st.caption(f"他 {len(_remaining_med)-3}件")

            if not _high_news and not _med_news and not _constraint_hits:
                st.caption("重要なニュースはありません")
        else:
            st.caption("本日のニュースはまだ取得されていません")

        # ── SYSTEMS ──
        st.markdown('<div class="sec-header">SYSTEMS</div>', unsafe_allow_html=True)
        company_status = get_company_status()
        if company_status['exists']:
            for name, ok in [("SECRETARY", company_status['has_secretary']), ("RESEARCH", company_status['has_research']), ("FINANCE", company_status['has_finance'])]:
                color = "rgba(118,255,3,0.7)" if ok else "rgba(255,23,68,0.7)"
                dot = "●" if ok else "○"
                label = "ONLINE" if ok else "OFFLINE"
                st.markdown(f'<div style="font-family:Share Tech Mono,monospace;font-size:11px;color:{color};padding:2px 0;">{dot} {name} — {label}</div>', unsafe_allow_html=True)

        routine_result = get_today_routine_result()
        if routine_result.get('exists'):
            _todo = routine_result.get('todo', {})
            _rsch = routine_result.get('research', {})
            st.caption(f"秘書ルーティン: 繰越 {_todo.get('carried_over', 0)} / リサーチ {len(_rsch.get('topics', []))}")

        # 燃料サーチャージ更新警告
        _fuel_days = get_days_since_last_update(s)
        if _fuel_days is None or _fuel_days >= UPDATE_WARNING_DAYS:
            _msg = "未記録" if _fuel_days is None else f"{_fuel_days}日経過"
            st.markdown(
                f'<div style="font-family:Share Tech Mono,monospace;font-size:11px;color:rgba(240,160,48,0.85);padding:3px 0;">'
                f'▲ 燃料サーチャージ更新 — {_msg}（設定タブで値を確認）</div>',
                unsafe_allow_html=True,
            )

        # 運送料PDF更新警告
        _ship_days = get_shipping_rate_days_since_update(s)
        if _ship_days is None or _ship_days >= SHIPPING_RATE_WARNING_DAYS:
            _msg = "未記録" if _ship_days is None else f"{_ship_days}日経過"
            st.markdown(
                f'<div style="font-family:Share Tech Mono,monospace;font-size:11px;color:rgba(240,160,48,0.85);padding:3px 0;">'
                f'▲ 運送料PDF更新 — {_msg}（設定タブで最新運送料PDFをアップロード）</div>',
                unsafe_allow_html=True,
            )

        # ── ROADMAP (システム改善タスク一覧) ──
        # data/system_improvements.json を唯一のソースとするデータ駆動UI。
        # ユーザーはチェック (完了化) / 削除 / 未着手戻し を画面から実行可能。
        from datetime import date as _date_today_cls
        st.markdown('<div class="sec-header" style="margin-top:18px;">ROADMAP</div>', unsafe_allow_html=True)
        _imp_path = Path(__file__).parent / "data" / "system_improvements.json"

        # JSON 破損時の全データ損失を防ぐため、読込失敗時は編集操作を無効化する。
        _roadmap_all = []
        _roadmap_load_ok = True
        if _imp_path.exists():
            try:
                _roadmap_all = json.loads(_imp_path.read_text(encoding="utf-8"))
                if not isinstance(_roadmap_all, list):
                    raise ValueError("system_improvements.json は JSON 配列である必要があります")
            except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as _e:
                _roadmap_load_ok = False
                _roadmap_all = []
                st.warning(f"ROADMAP 読込失敗のため編集を一時無効化しました: {_e}")

        _status_meta = {
            "完了": ("完了", "●", "rgba(112,240,128,0.75)", "rgba(112,240,128,0.10)"),
            "進行中": ("進行中", "◐", "rgba(77,217,240,0.85)", "rgba(77,217,240,0.10)"),
            "未着手": ("予定", "○", "rgba(240,200,48,0.75)", "rgba(240,200,48,0.08)"),
        }
        _priority_order = {"高": 0, "中": 1, "通常": 1, "低": 2}
        _status_order = {"進行中": 0, "未着手": 1, "完了": 2}

        def _roadmap_sort_key(t):
            return (
                _status_order.get(t.get("status"), 1),
                _priority_order.get(t.get("priority"), 1),
                t.get("id", 0),
            )

        _pending_cnt = sum(1 for t in _roadmap_all if t.get("status") == "未着手")
        _wip_cnt = sum(1 for t in _roadmap_all if t.get("status") == "進行中")
        _done_cnt = sum(1 for t in _roadmap_all if t.get("status") == "完了")

        st.caption(
            f"残 {_pending_cnt + _wip_cnt} 件 "
            f"(未着手 {_pending_cnt} / 進行中 {_wip_cnt} / 完了 {_done_cnt})"
        )

        _show_done_tasks = st.checkbox("完了済みを表示", key="dash_roadmap_show_done", value=False)

        _display_tasks = sorted(_roadmap_all, key=_roadmap_sort_key)
        if not _show_done_tasks:
            _display_tasks = [t for t in _display_tasks if t.get("status") != "完了"]

        def _save_roadmap(items):
            _imp_path.write_text(
                json.dumps(items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        def _update_roadmap_item(target_id, mutator):
            """id が一致する最初のアイテムを mutator で変更して保存。"""
            for _item in _roadmap_all:
                if _item.get("id") == target_id:
                    mutator(_item)
                    break
            _save_roadmap(_roadmap_all)

        # id 重複時の Streamlit DuplicateWidgetID 回避のため、表示順 index をキーに併用。
        for _display_idx, _t in enumerate(_display_tasks):
            _status = _t.get("status", "未着手")
            _label, _icon, _fg, _bg = _status_meta.get(_status, _status_meta["未着手"])
            _tag_text = _t.get("tag") or ""
            _title = _t.get("title", "")
            _priority = _t.get("priority", "通常")
            _tid = _t.get("id", 0)
            _key_suffix = f"{_tid}_{_display_idx}"

            _c_main, _c_act1, _c_act2 = st.columns([7, 1, 1])
            with _c_main:
                _tag_html = (
                    f'<span style="color:{_fg};font-weight:700;min-width:36px;display:inline-block;">'
                    f'{html.escape(_tag_text)}</span>'
                    if _tag_text else ""
                )
                st.markdown(
                    f'<div style="padding:4px 10px;margin:2px 0;'
                    f'border-left:3px solid {_fg};background:{_bg};'
                    f'font-family:Share Tech Mono,monospace;font-size:12px;'
                    f'color:rgba(210,225,240,0.9);display:flex;align-items:center;gap:8px;">'
                    f'<span style="color:{_fg};">{_icon}</span>'
                    f'{_tag_html}'
                    f'<span style="flex:1;">{html.escape(_title)}</span>'
                    f'<span style="color:#5a7a96;font-size:10px;margin-right:6px;">[{html.escape(_priority)}]</span>'
                    f'<span style="color:{_fg};font-size:10px;letter-spacing:1px;">{_label}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with _c_act1:
                if _status == "完了":
                    if st.button(
                        "戻", key=f"roadmap_reopen_{_key_suffix}",
                        help="未着手に戻す", disabled=not _roadmap_load_ok,
                    ):
                        def _reopen(item):
                            item["status"] = "未着手"
                            item["completed"] = None
                        _update_roadmap_item(_tid, _reopen)
                        st.rerun()
                else:
                    if st.button(
                        "済", key=f"roadmap_done_{_key_suffix}",
                        help="完了にする", disabled=not _roadmap_load_ok,
                    ):
                        _today_iso = _date_today_cls.today().isoformat()
                        def _done(item):
                            item["status"] = "完了"
                            item["completed"] = _today_iso
                        _update_roadmap_item(_tid, _done)
                        st.rerun()
            with _c_act2:
                if st.button(
                    "削", key=f"roadmap_del_{_key_suffix}",
                    help="削除", disabled=not _roadmap_load_ok,
                ):
                    _roadmap_all = [_i for _i in _roadmap_all if _i.get("id") != _tid]
                    _save_roadmap(_roadmap_all)
                    st.rerun()

    # ── LOG ──
    # システム改善は右カラムの ROADMAP セクションに統合済み（2026-04-23）。
    st.divider()
    _show_log = st.checkbox("実行ログを表示", key="dash_show_log")
    if _show_log:
        logs = get_latest_execution_logs(limit=10)
        if logs:
            log_data = [{"時刻": l['timestamp_str'].split('.')[0] if l['timestamp'] else "", "内容": l['message'][:80]} for l in logs]
            st.dataframe(pd.DataFrame(log_data), width="stretch", hide_index=True, height=250)

# ========== 利益計算タブ ==========
if _w134_sel == "利益計算":
    col1, col2, col3 = st.columns([1.2, 1, 1.2])

    with col1:
        st.subheader("商品・コスト情報")
        purchase = st.number_input("仕入れ値（円）", min_value=0, value=52400, step=100)
        item_price = st.number_input("販売価格（USD）", min_value=0.0, value=500.0, step=1.0, format="%.2f")
        category_id = st.number_input("カテゴリーID", min_value=0, value=58248, step=1)
        is_ddu = st.checkbox("DDUモード（関税バイヤー負担）", value=False)
        if not is_ddu:
            duty_rate = s.get("duty_rate", 20.0)
            shipping_usd_preview = item_price * duty_rate / 100
            st.info(f"送料代（関税）: ${shipping_usd_preview:.2f}　（商品価格 × {duty_rate:.0f}%）")
        else:
            st.info("送料代: $0.00（DDUモード）")

    with col2:
        st.subheader("発送情報")
        weight_g = st.number_input("重量（g）", min_value=0, value=3000, step=100)
        st.markdown("**サイズ（cm）**")
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            length = st.number_input("L", min_value=0.0, value=0.0, step=1.0)
        with sc2:
            width = st.number_input("W", min_value=0.0, value=0.0, step=1.0)
        with sc3:
            height = st.number_input("H", min_value=0.0, value=0.0, step=1.0)
        if length > 0 and width > 0 and height > 0:
            from calculator import get_chargeable_weight_kg
            charged_kg = get_chargeable_weight_kg(weight_g, length, width, height, 5000)
            actual_kg = weight_g / 1000
            vol_kg = (length * width * height) / 5000
            st.caption(f"実重量: {actual_kg:.2f}kg　容積重量: {vol_kg:.2f}kg　**課金重量: {charged_kg:.1f}kg**")
        else:
            st.caption(f"実重量のみ: {weight_g/1000:.2f}kg（サイズ未入力）")

    with col3:
        st.subheader("レート・設定")
        fx = st.number_input("為替レート（JPY/USD）", min_value=1.0, value=float(s["exchange_rate"]), step=1.0)
        duty_rate_input = st.number_input("関税率（%）", min_value=0.0, value=float(s["duty_rate"]), step=1.0)
        pl_rate = st.number_input("PL広告費（%）", min_value=0.0, value=float(s["promoted_listing_rate"]), step=0.5)
        tax_rate = st.number_input("消費税（%）", min_value=0.0, value=float(s["consumption_tax_rate"]), step=1.0)
        point_rate = st.number_input("ポイント付与（%）", min_value=0.0, value=float(s["point_reward_rate"]), step=0.5)

    st.divider()
    if st.button("▶ 計算実行", type="primary", width="stretch"):
        calc_settings = dict(s)
        calc_settings.update({
            "exchange_rate": fx, "duty_rate": duty_rate_input,
            "promoted_listing_rate": pl_rate, "consumption_tax_rate": tax_rate,
            "point_reward_rate": point_rate,
        })
        inp = CalcInput(
            purchase_yen=purchase, item_price_usd=item_price,
            weight_g=weight_g, length_cm=length, width_cm=width, height_cm=height,
            category_id=int(category_id), is_ddu=is_ddu, country_code="US",
        )
        result = calculate(inp, calc_settings)

        st.subheader("費用内訳")
        left, right = st.columns(2)
        with left:
            fvf_pct = result.fvf_rate * 100
            data = {
                "項目": [
                    "売上（円）", "ポイント還元",
                    f"落札手数料 ({fvf_pct:.2f}%)",
                    f"海外決済手数料 ({calc_settings['intl_payment_rate']:.2f}%)",
                    "取引手数料", f"広告費 ({pl_rate:.2f}%)",
                    f"Payoneer手数料 ({calc_settings['payoneer_fee_rate']:.2f}%)",
                ],
                "金額（円）": [
                    f"¥{result.revenue:,.0f}",
                    f"¥{result.point_return:,.0f} (0.00%)" if result.point_return == 0 else f"¥{result.point_return:,.0f}",
                    f"¥{result.fvf:,.0f}", f"¥{result.intl_payment:,.0f}",
                    f"¥{result.transaction_fee:.2f}", f"¥{result.ad_fee:,.0f}",
                    f"¥{result.payoneer:,.0f}",
                ],
            }
            st.dataframe(pd.DataFrame(data), hide_index=True, width="stretch")
            st.metric("合計コスト（仕入れ除く）", f"¥{result.ebay_cost_subtotal:,.0f}")
        with right:
            if result.service_results:
                st.subheader("送料別利益")
                for sr in result.service_results:
                    color = "●" if sr.is_listable else "○"
                    st.markdown(f"**{color} {sr.service_name}　利益: ¥{sr.profit:,}　({sr.profit_rate*100:.1f}%)**")
                    sc1, sc2 = st.columns(2)
                    with sc1:
                        st.write(f"**課金重量**: {sr.charged_weight_kg:.1f}kg")
                        st.write(f"**ベース送料**: ¥{sr.base_rate:,.0f}")
                        st.write(f"**燃料サーチャージ**: ¥{sr.fuel_surcharge_amount:,.0f}")
                        st.write(f"**送料**: ¥{sr.shipping_display:,.0f}")
                        for name, amt in sr.additional_fees.items():
                            st.write(f"**{name}**: ¥{amt:,.0f}")
                        st.write(f"**合計送料**: ¥{sr.total_shipping:,.0f}")
                    with sc2:
                        st.metric("利益", f"¥{sr.profit:,}", delta=f"{sr.profit_rate*100:.1f}%")
                        st.metric("還付込利益", f"¥{sr.profit_with_refund:,}", delta=f"{sr.profit_with_refund_rate*100:.1f}%")
                        st.metric("消費税還付", f"¥{sr.tax_refund:,}")
                        if sr.is_listable:
                            st.success("推奨")
                        else:
                            st.error("利益不足")
            else:
                st.warning("選択されたサービスのデータが見つかりませんでした。設定タブでサービスを確認してください。")


# ========== 在庫監視タブ ==========
if _w134_sel == "在庫監視":
    monitor_tab_risk, monitor_tab1, monitor_tab2 = st.tabs(["要対応", "監視リスト", "サイト設定"])

    # ---------- 要対応（仕入先在庫リスク） ----------
    with monitor_tab_risk:
        risk_data = _cd_supply_risk(get_db_version())
        oos_items = risk_data["out_of_stock"]
        pnf_items = risk_data["page_not_found"]
        total_risk = len(oos_items) + len(pnf_items)

        st.subheader(f"要対応商品: {total_risk}件")
        st.caption("eBay在庫が1以上あるのに、仕入先で購入できない商品です。出品停止または仕入先変更を検討してください。")

        # ── 最終実行日時 (source_last_checked の最大値) ──
        try:
            from datetime import datetime as _dt_inv
            with get_conn() as _inv_conn:
                _inv_conn.row_factory = None
                _last_inv = _inv_conn.execute(
                    "SELECT MAX(source_last_checked) FROM ebay_listings WHERE source_last_checked IS NOT NULL"
                ).fetchone()[0]
            if _last_inv:
                # 2つの format を許容: "2026-04-23T15:39:00.671816" (ISO) or "2026-04-23 15:39:00"
                _parse_ok = False
                for _fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    try:
                        _dt_inv_obj = _dt_inv.strptime(_last_inv, _fmt)
                        _parse_ok = True
                        break
                    except Exception:
                        continue
                if _parse_ok:
                    _delta = _dt_inv.now() - _dt_inv_obj
                    if _delta.total_seconds() < 3600:
                        _ago = f"{int(_delta.total_seconds()/60)} 分前"
                    elif _delta.total_seconds() < 86400:
                        _ago = f"{int(_delta.total_seconds()/3600)} 時間前"
                    else:
                        _ago = f"{int(_delta.total_seconds()/86400)} 日前"
                    st.caption(f"**最終チェック**: {_dt_inv_obj.strftime('%Y-%m-%d %H:%M:%S')} ({_ago})")
                else:
                    st.caption(f"**最終チェック**: {_last_inv}")
            else:
                st.caption("**最終チェック**: データなし")
        except Exception as _e:
            pass  # logger 未定義なので silent fail (UI 表示は破綻させない)

        # W100 (2026-05-06): ヤフオク再出品待ち listing 表示
        # ヤフオクで落札者なし終了 → 1日後の再出品慣行を待ってからリサーチする listing
        try:
            from datetime import datetime as _dt_w100, timezone as _tz_w100, timedelta as _td_w100
            import sqlite3 as _sqlite3_w100
            from monitor.database import get_conn as _w100_get_conn
            with _w100_get_conn() as _w100_conn:
                _w100_conn.row_factory = _sqlite3_w100.Row
                _w100_rows = _w100_conn.execute(
                    "SELECT ebay_item_id, sku, title, yahoo_grace_until "
                    "FROM ebay_listings "
                    "WHERE yahoo_grace_until IS NOT NULL "
                    "  AND yahoo_grace_until > datetime('now') "
                    "  AND COALESCE(is_ended,0)=0 "
                    "ORDER BY yahoo_grace_until ASC"
                ).fetchall()
            if _w100_rows:
                st.markdown(f"#### 再出品待ち ({len(_w100_rows)}件)")
                st.caption(
                    "ヤフオクで **落札者なし終了** → 1日後の再出品慣行を待ってからリサーチします。"
                    "ヤフオク終了 + 24時間後に supplier_sweep が自動的にリサーチを実行します。"
                )
                for _r_w100 in _w100_rows[:20]:
                    _until_raw = _r_w100["yahoo_grace_until"]
                    try:
                        _until_dt = _dt_w100.fromisoformat(_until_raw)
                        if _until_dt.tzinfo is None:
                            _until_dt = _until_dt.replace(tzinfo=_tz_w100.utc)
                        _now_w100 = _dt_w100.now(_tz_w100.utc)
                        _remain_w100 = _until_dt - _now_w100
                        _h_w100 = int(_remain_w100.total_seconds() // 3600)
                        _m_w100 = int((_remain_w100.total_seconds() % 3600) // 60)
                        _jst_w100 = _until_dt.astimezone(_tz_w100(_td_w100(hours=9))).strftime("%m/%d %H:%M JST")
                    except (ValueError, TypeError):
                        _jst_w100 = _until_raw
                        _h_w100, _m_w100 = "?", "?"
                    _title_w100 = (_r_w100["title"] or "")[:50]
                    st.caption(
                        f"- {_title_w100} (`{_r_w100['ebay_item_id']}`) "
                        f"残り {_h_w100}時間{_m_w100}分 / リサーチ予定: {_jst_w100}"
                    )
                if len(_w100_rows) > 20:
                    st.caption(f"...他 {len(_w100_rows) - 20} 件")
                st.divider()
        except Exception:
            pass  # 表示失敗は UI 破綻させない (Q0: scheduler.log の [grace] 行で観測可能)

        def _render_risk_table_with_actions(items: list[dict], section_key: str) -> None:
            """要対応商品を編集可能テーブルで表示（二段階チェック方式・DB永続化）+ 候補URL最大3件併記"""
            import pandas as pd
            from monitor.database import get_conn as _risk_conn

            # 元データを保持（変更検出用）
            orig_data = {item["ebay_item_id"]: {"sku": item.get("sku") or "", "qty": item["quantity_ebay"]} for item in items}

            # risk_confirmed はDBから取得済み
            confirmed_ids = {item["ebay_item_id"] for item in items if item.get("risk_confirmed", 0) == 1}

            # SKU 毎に supplier_candidates 上位3件（pending/accepted）を取得
            # 2026-04-24 Q2=iii: 在庫監視は「仕入先在庫0→置換 or 在庫0化」の動線なので
            # alt_listing_possible=1 (別SKU出品機会) は非表示にする (本来の目的と逸れるため)
            _sku_list = [it.get("sku") or "" for it in items if it.get("sku")]
            _cand_by_sku: dict[str, list[dict]] = {}
            # 2026-05-05 追加: 探索済だが alt-only (= 全 candidate が alt_listing_possible=1) の SKU
            # を識別して、UI で「候補未探索」と誤表示する bug を防ぐ. Baccarat case 対策.
            _alt_only_count_by_sku: dict[str, int] = {}
            if _sku_list:
                with _risk_conn() as _rc:
                    _ph = ",".join("?" * len(_sku_list))
                    for _r in _rc.execute(
                        f"""SELECT sku, candidate_url, match_score, status, alt_listing_possible
                            FROM supplier_candidates
                            WHERE sku IN ({_ph})
                              AND status IN ('pending','accepted')
                              AND COALESCE(alt_listing_possible, 0) = 0
                            ORDER BY sku, match_score DESC""",
                        _sku_list,
                    ).fetchall():
                        _cand_by_sku.setdefault(_r["sku"], []).append(dict(_r))
                    # alt-only candidates の数も別途取得 (UI caption 分岐用)
                    for _r in _rc.execute(
                        f"""SELECT sku, COUNT(*) as alt_n
                            FROM supplier_candidates
                            WHERE sku IN ({_ph})
                              AND status IN ('pending','accepted')
                              AND COALESCE(alt_listing_possible, 0) = 1
                            GROUP BY sku""",
                        _sku_list,
                    ).fetchall():
                        _alt_only_count_by_sku[_r["sku"]] = _r["alt_n"]

            def _cand_url(sku: str, idx: int) -> str:
                """sku の候補 idx 番目の URL を返す。無ければ空文字。"""
                cands = _cand_by_sku.get(sku, [])
                if idx >= len(cands):
                    return ""
                return cands[idx].get("candidate_url") or ""

            def _cand_count_label(sku: str) -> str:
                n = len(_cand_by_sku.get(sku, []))
                if n == 0:
                    # 2026-05-05 修正: alt-only 候補があれば「探索済」と区別表示
                    # (PNF / OOS の両 section で同一ロジック、Baccarat case 対策).
                    alt_n = _alt_only_count_by_sku.get(sku, 0)
                    if alt_n > 0:
                        return f"探索済(別出品機会{alt_n})"
                    return "未探索"
                return f"{min(n,3)}/{n}件"

            df = pd.DataFrame([
                {
                    "状態": "● 確認済" if item["ebay_item_id"] in confirmed_ids else "○ 未確認",
                    "確認": item["ebay_item_id"] in confirmed_ids,
                    "Item ID": item["ebay_item_id"],
                    "SKU": item.get("sku") or "",
                    "価格": f"${item['current_price']:.2f}" if item["current_price"] else "-",
                    "在庫": int(item["quantity_ebay"]),
                    "ランク": item["rank"] or "-",
                    "仕入先": item["source"] or "-",
                    "仕入先URL": item.get("source_url") or "",
                    "候補": _cand_count_label(item.get("sku") or ""),
                    "候補URL1": _cand_url(item.get("sku") or "", 0),
                    "候補URL2": _cand_url(item.get("sku") or "", 1),
                    "候補URL3": _cand_url(item.get("sku") or "", 2),
                    "タイトル": (item["title"] or "")[:55],
                }
                for item in items
            ])

            # st.form で囲み、ボタン押下まで中間rerunを防ぐ
            with st.form(key=f"risk_form_{section_key}"):
                edited_df = st.data_editor(
                    df,
                    column_config={
                        "状態": st.column_config.TextColumn("状態", width="small"),
                        "確認": st.column_config.CheckboxColumn("確認", width="small"),
                        "Item ID": st.column_config.TextColumn("Item ID", width="medium"),
                        "SKU": st.column_config.TextColumn("SKU", width="medium"),
                        "在庫": st.column_config.NumberColumn("在庫", min_value=0, step=1, width="small"),
                        "仕入先URL": st.column_config.LinkColumn("仕入先URL", display_text="リンク", width="small"),
                        "候補": st.column_config.TextColumn("候補", width="small",
                                                          help="Pattern 1 async / Pattern 2 batch が探索した候補数"),
                        "候補URL1": st.column_config.LinkColumn("候補1", display_text="▸1", width="small"),
                        "候補URL2": st.column_config.LinkColumn("候補2", display_text="▸2", width="small"),
                        "候補URL3": st.column_config.LinkColumn("候補3", display_text="▸3", width="small"),
                    },
                    disabled=["状態", "Item ID", "価格", "ランク", "仕入先", "仕入先URL",
                              "候補", "候補URL1", "候補URL2", "候補URL3", "タイトル"],
                    hide_index=True,
                    width="stretch",
                    key=f"risk_table_{section_key}",
                )
                submitted = st.form_submit_button("確認済みをeBayに反映", type="primary")

            # --- 集計表示 ---
            total = len(items)
            confirmed_count = len(confirmed_ids)
            remaining = total - confirmed_count
            st.caption(f"全{total}件 | ● 確認済: {confirmed_count}件 | 残り: {remaining}件")

            # --- 二段階目：フォーム送信時にeBayに反映 ---
            if submitted:
                checked_rows = edited_df[edited_df["確認"]]
                if checked_rows.empty:
                    st.warning("チェックが入っている商品がありません")
                else:
                    sync_targets = []
                    no_change_ids = []
                    for _, row in checked_rows.iterrows():
                        eid = row["Item ID"]
                        if eid in confirmed_ids:
                            continue  # 既に確認済みはスキップ
                        orig = orig_data.get(eid, {})
                        new_sku = row["SKU"] if row["SKU"] != orig.get("sku", "") else None
                        new_qty = int(row["在庫"]) if int(row["在庫"]) != int(orig.get("qty", 0)) else None
                        if new_sku is not None or new_qty is not None:
                            sync_targets.append({"ebay_item_id": eid, "new_sku": new_sku, "new_qty": new_qty})
                        else:
                            no_change_ids.append(eid)

                    new_checked = len(sync_targets) + len(no_change_ids)
                    if new_checked == 0:
                        st.success("チェック済みの全商品は確認済みです")
                    else:
                        change_msg = f"変更あり: {len(sync_targets)}件" if sync_targets else ""
                        keep_msg = f"現状維持: {len(no_change_ids)}件" if no_change_ids else ""
                        st.info(f"{new_checked}件を処理中（{', '.join(filter(None, [change_msg, keep_msg]))}）")

                        # 現状維持 → DBに確認済みフラグを立てる
                        for eid in no_change_ids:
                            set_ebay_listing_risk_confirmed(eid, 1)

                        # 変更あり → eBay APIに送信してからDBに確認済みフラグ
                        if sync_targets:
                            ebay_creds = {
                                'app_id': s.get("ebay_app_id", ""),
                                'dev_id': s.get("ebay_dev_id", ""),
                                'cert_id': s.get("ebay_cert_id", ""),
                                'user_token': s.get("ebay_user_token", ""),
                            }
                            if not all(ebay_creds.values()):
                                st.error("eBay API認証情報が未設定です（設定タブ参照）")
                            else:
                                from monitor.ebay_client import revise_inventory_quantity, revise_item_sku
                                progress = st.progress(0)
                                for i, ch in enumerate(sync_targets):
                                    eid = ch["ebay_item_id"]
                                    ok = True
                                    if ch["new_qty"] is not None:
                                        r = revise_inventory_quantity(eid, ch["new_qty"], **ebay_creds)
                                        if r["success"]:
                                            update_ebay_listing_quantity(eid, ch["new_qty"])
                                            st.success(f"{eid}: 在庫 → {ch['new_qty']}")
                                        else:
                                            st.error(f"{eid}: 在庫 → {r['message']}")
                                            ok = False
                                    if ch["new_sku"] is not None:
                                        r = revise_item_sku(eid, ch["new_sku"], **ebay_creds)
                                        if r["success"]:
                                            update_ebay_listing_sku(eid, ch["new_sku"])
                                            st.success(f"{eid}: SKU → {ch['new_sku']}")
                                        else:
                                            st.error(f"{eid}: SKU → {r['message']}")
                                            ok = False
                                    if ok:
                                        set_ebay_listing_risk_confirmed(eid, 1)
                                    progress.progress((i + 1) / len(sync_targets))

                        bump_db_version()  # W134 Step2: 在庫/SKU/risk 一括変更後 read-cache 無効化
                        st.rerun()  # Reload to update status column

        # --- 在庫切れ（インライン候補表示版） ---
        def _on_adopt_check_change(cid: int, eid: str, url: str, section_key: str):
            """採用チェック ON 時に仕入先 URL を SKU 変換して SKU 欄 session_state に反映.

            2026-04-24 要件 Q3=A: ユーザーが採用チェックを入れた瞬間に UI の SKU 欄が
            新仕入先ベースの SKU に自動更新される。確認後に一括実行で eBay 反映。
            OFF にしても SKU 欄は自動復元しない (ユーザーが手動編集した可能性を尊重)。
            """
            adopt_key = f"{section_key}_adopt_{cid}"
            sku_key = f"{section_key}_sku_{eid}"
            if st.session_state.get(adopt_key):
                try:
                    new_sku = url_to_sku(url)
                    if new_sku:
                        st.session_state[sku_key] = new_sku
                except Exception:
                    pass  # url_to_sku 失敗時は何もしない

        @st.fragment
        def _render_oos_block(
            item: dict,
            cands: list[dict],
            section_key: str,
            alt_only_count_by_sku: dict[str, int],
        ) -> None:
            """
            1 SKU分のブロックを描画（OOS情報 + 仕入先候補 up to 3件をインライン）。

            2026-04-24 st.fragment 化 (Q3=A 要件):
            - 採用 checkbox の on_change コールバックで SKU 欄を自動更新
            - fragment ごとに独立 rerun, 他商品には影響しない (爆速維持)
            - st.form は解体、一括実行は単純ボタンで全 session_state を読んで処理

            引数:
              item: oos_items の 1 要素
              cands: 対象SKUの仕入先候補 list[dict]（status IN ('pending','accepted','applied')、
                     score降順、最大3件）
              section_key: widget key プレフィクス（例 "oos"）
              alt_only_count_by_sku: SKU 毎の alt_listing_possible=1 候補数 dict.
                cands=0 件 + alt_only>0 のとき「探索済 (置換候補なし)」caption を出す.
                2026-05-05 NameError バグ fix で引数化 (旧: 別関数のローカル参照でクラッシュ).
            """
            eid = item["ebay_item_id"]
            sku_orig = item.get("sku") or ""
            qty_orig = int(item.get("quantity_ebay") or 0)
            price = item.get("current_price")
            price_str = f"${price:.2f}" if price else "-"
            rank = item.get("rank") or "-"
            source = item.get("source") or "-"
            source_url = item.get("source_url") or ""
            title = (item.get("title") or "")[:80]
            confirmed_now = bool(item.get("risk_confirmed"))

            with st.container(border=True):
                # --- ヘッダ行: 確認 / Item ID / タイトル / 価格 / ランク ---
                _h1, _h2, _h3, _h4, _h5 = st.columns([0.9, 1.6, 5.0, 0.9, 0.8])
                with _h1:
                    st.checkbox(
                        "確認", key=f"{section_key}_confirm_{eid}",
                        value=confirmed_now,
                    )
                with _h2:
                    st.markdown(
                        f'<div style="font-family:var(--font-mono,monospace);'
                        f'font-size:12px;color:rgba(180,220,255,0.85);padding-top:6px;">{html.escape(eid)}</div>',
                        unsafe_allow_html=True,
                    )
                with _h3:
                    st.markdown(
                        f'<div style="font-size:12px;color:rgba(220,235,250,0.85);padding-top:6px;">'
                        f'{html.escape(title)}</div>',
                        unsafe_allow_html=True,
                    )
                with _h4:
                    st.markdown(
                        f'<div style="font-size:12px;color:rgba(180,220,255,0.85);padding-top:6px;">{price_str}</div>',
                        unsafe_allow_html=True,
                    )
                with _h5:
                    st.markdown(
                        f'<div style="font-size:12px;color:rgba(180,220,255,0.85);padding-top:6px;">ランク{rank}</div>',
                        unsafe_allow_html=True,
                    )

                # --- 編集行: SKU / 在庫 / 仕入先URL ---
                _e1, _e2, _e3, _e4 = st.columns([3.0, 1.2, 1.6, 2.0])
                with _e1:
                    st.text_input(
                        "SKU", value=sku_orig,
                        key=f"{section_key}_sku_{eid}",
                        label_visibility="collapsed",
                    )
                with _e2:
                    st.number_input(
                        "在庫", min_value=0, step=1, value=qty_orig,
                        key=f"{section_key}_qty_{eid}",
                        label_visibility="collapsed",
                    )
                with _e3:
                    st.markdown(
                        f'<div style="font-size:11px;color:rgba(180,220,255,0.6);padding-top:8px;">'
                        f'仕入先: {html.escape(source)}</div>',
                        unsafe_allow_html=True,
                    )
                with _e4:
                    if source_url:
                        st.markdown(
                            f'<div style="padding-top:6px;">'
                            f'<a href="{html.escape(source_url, quote=True)}" target="_blank" '
                            f'style="color:rgba(120,200,255,0.9);font-size:12px;">仕入先URLを開く</a></div>',
                            unsafe_allow_html=True,
                        )

                st.markdown("---")

                # --- 候補部（form-safe: 全て checkbox、submit で batch 処理） ---
                if cands:
                    _total_for_sku = len(cands)
                    st.markdown(
                        f'<div style="font-size:11px;color:rgba(180,220,255,0.6);margin-bottom:4px;">'
                        f'仕入先候補 {_total_for_sku}件（score降順／上位最大3件）／'
                        f'下部「一括実行」で採用チェック済みURLをSKUに反映</div>',
                        unsafe_allow_html=True,
                    )

                    for _c in cands:
                        _cid = _c["id"]
                        _score = _c.get("match_score") or 0
                        _is_alt = bool(_c.get("alt_listing_possible")) and _score < 60
                        _score_color = (
                            "rgba(118,255,3,0.85)" if _score >= 80
                            else "rgba(240,200,48,0.85)" if _score >= 60
                            else "rgba(200,150,220,0.85)"
                        )
                        _plat = _c.get("source_platform") or "?"
                        _ttl = (_c.get("candidate_title") or "")[:50]
                        _price_jpy = _c.get("candidate_price_jpy")
                        _price_str = f"仕入 ¥{_price_jpy:,}" if _price_jpy else "仕入 ¥?"
                        _url = _c.get("candidate_url") or ""
                        _status = _c.get("status") or "pending"
                        _type_label = "別出品機会" if _is_alt else "置換候補"

                        # 利益 inline (Interstellar amber)
                        _profit_jpy_v = _c.get("profit_jpy")
                        _profit_str = ""
                        if _profit_jpy_v is not None and _price_jpy and _price_jpy > 0:
                            _rate_v = (_profit_jpy_v / _price_jpy) * 100
                            _profit_str = (
                                f' <span style="color:#ffa84a;font-weight:600;">'
                                f'利益 +¥{int(_profit_jpy_v):,} ({_rate_v:.0f}%)</span>'
                            )

                        # W100 (2026-05-06): 旧「採用後 24h 猶予」UI 削除.
                        # 新仕様 (Phase 3) は inventory_check 側で grace を管理し、
                        # 採用→反映フローは即時実行 (猶予なし).

                        _info_col, _link_col, _btn_col = st.columns([5.6, 1.2, 2.2])
                        with _info_col:
                            _status_badge = ""
                            if _status == "accepted":
                                _status_badge = (
                                    '<span style="color:rgba(118,255,3,0.9);'
                                    'font-size:10px;margin-left:6px;">[採用済]</span>'
                                )
                            elif _status == "applied":
                                _status_badge = (
                                    '<span style="color:rgba(120,200,255,0.9);'
                                    'font-size:10px;margin-left:6px;">[反映済]</span>'
                                )
                            # W100: grace UI 廃止
                            _grace_html = ""
                            st.markdown(
                                f'<div style="border-left:2px solid {_score_color};padding:4px 10px;'
                                f'background:rgba(80,120,180,0.03);font-size:12px;">'
                                f'<span style="color:{_score_color};font-weight:700;">score={_score}</span>'
                                f' <span style="color:rgba(180,220,255,0.5);font-size:10px;">[{_type_label}]</span>'
                                f' <span style="color:rgba(180,220,255,0.6);">{html.escape(_plat)}</span>'
                                f' <span style="color:rgba(255,255,255,0.85);">{html.escape(_ttl)}</span>'
                                f' <span style="color:#d8cdb5;">{_price_str}</span>'
                                f'{_profit_str}'
                                f'{_status_badge}{_grace_html}'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                        with _link_col:
                            if _url:
                                st.markdown(
                                    f'<a href="{html.escape(_url, quote=True)}" target="_blank" '
                                    f'style="color:rgba(120,200,255,0.9);font-size:12px;">[商品開く]</a>',
                                    unsafe_allow_html=True,
                                )
                        with _btn_col:
                            _alt_only = _is_alt

                            if _status == "pending":
                                _b1, _b2 = st.columns(2)
                                with _b1:
                                    st.checkbox(
                                        "採用", key=f"{section_key}_adopt_{_cid}",
                                        disabled=_alt_only,
                                        on_change=_on_adopt_check_change,
                                        args=(_cid, eid, _url, section_key),
                                        help=(
                                            "別SKU出品機会のためSKU置換は不適（新規出品フロー向け）"
                                            if _alt_only
                                            else "採用: SKU欄が自動で仕入先URLベースに更新されます。"
                                            "確認後に下部「一括実行」でeBayに反映。"
                                        ),
                                    )
                                with _b2:
                                    st.checkbox(
                                        "不採用", key=f"{section_key}_reject_{_cid}",
                                        help=(
                                            "不採用にする（別出品機会も却下、学習データに記録）"
                                            if _alt_only
                                            else "不採用（ユーザー判断として学習データに記録）"
                                        ),
                                    )
                            elif _status == "accepted":
                                # W100 (2026-05-06): 旧「ヤフオク 24h 猶予」廃止により
                                # _disabled 条件削除. 採用済はいつでも反映可能.
                                st.checkbox(
                                    "反映する",
                                    key=f"{section_key}_applyck_{_cid}",
                                    help="採用済。submit 時に ReviseItem でSKU反映",
                                )
                                # W115: 写真反映 button (案 A 別 button、SKU と独立).
                                _photo_key = f"{section_key}_photo_open_{_cid}"
                                if st.button(
                                    "📷 写真反映",
                                    key=f"{section_key}_btn_photo_{_cid}",
                                    help=(
                                        "仕入先画像から Photoroom + Gemini で hero 合成 → "
                                        "EPS upload → ReviseItem PictureDetails で eBay 反映"
                                    ),
                                ):
                                    st.session_state[_photo_key] = (
                                        not st.session_state.get(_photo_key, False)
                                    )
                            elif _status == "applied":
                                st.caption("完了")

                        # W115: status='accepted' で「写真反映」展開時は section render.
                        # _btn_col 外に置くことで横幅を活かして 3 候補表示.
                        if _status == "accepted" and st.session_state.get(
                            f"{section_key}_photo_open_{_cid}", False
                        ):
                            from tabs._supplier_photo_pipeline import (
                                render_supplier_photo_apply_section,
                            )
                            render_supplier_photo_apply_section(
                                candidate_id=_cid,
                                candidate_url=_url,
                                ebay_item_id=eid,
                                candidate_title=_ttl,
                            )
                else:
                    # 候補0件 (置換用) → ただし alt-only 候補があれば「探索済」と区別表示.
                    # 2026-05-05 修正: Baccarat case (探索済 7 件すべて alt のみ) で
                    # 「候補未探索」と誤表示してた事象の修正. user 認識ミスを防ぐ.
                    _alt_n = alt_only_count_by_sku.get(sku_orig, 0)
                    if _alt_n > 0:
                        st.caption(
                            f"仕入先候補は探索済みです（{_alt_n} 件見つかりましたが、"
                            f"すべて『別商品の出品候補』として分類されており、この出品の"
                            f"置き換えには使えません）。別商品として出品する検討は"
                            f"『別SKU出品機会』タブで行ってください。"
                        )
                    else:
                        st.caption(
                            "候補未探索（次回02:30 Pattern 2バッチで自動探索、"
                            "または form 下部の「未探索SKUの即時探索」で個別起動）"
                        )

        st.markdown(f"### 仕入先在庫切れ ({len(oos_items)}件)")
        if oos_items:
            # [1] SKU ごとの候補を1回だけ一括取得（N+1 SQL 回避）
            from monitor.database import get_conn as _oos_conn
            _sku_list = [_it.get("sku") for _it in oos_items if _it.get("sku")]
            _cand_by_sku: dict[str, list[dict]] = {}
            # 2026-05-05 追加 (CRITICAL fix): alt-only candidate 数を OOS section でも別取得.
            # _render_oos_block の caption 分岐で参照される. 過去の bug fix で別関数の
            # ローカル変数を参照してて NameError クラッシュしていた事故予防.
            _oos_alt_only_count: dict[str, int] = {}
            if _sku_list:
                with _oos_conn() as _cc:
                    _ph = ",".join("?" * len(_sku_list))
                    for _r in _cc.execute(
                        f"""SELECT id, sku, ebay_item_id, candidate_url, candidate_title,
                                   candidate_price_jpy, match_score, source_platform,
                                   status, alt_listing_possible,
                                   profit_jpy, profitable
                            FROM supplier_candidates
                            WHERE sku IN ({_ph})
                              AND status IN ('pending','accepted','applied')
                              AND COALESCE(alt_listing_possible, 0) = 0
                            ORDER BY sku,
                                     (profit_jpy IS NULL), profit_jpy DESC,
                                     match_score DESC""",
                        _sku_list,
                    ).fetchall():
                        _cand_by_sku.setdefault(_r["sku"], []).append(dict(_r))
                    # alt-only candidate 数も別途取得 (Baccarat case 対策)
                    for _r in _cc.execute(
                        f"""SELECT sku, COUNT(*) as alt_n
                            FROM supplier_candidates
                            WHERE sku IN ({_ph})
                              AND status IN ('pending','accepted')
                              AND COALESCE(alt_listing_possible, 0) = 1
                            GROUP BY sku""",
                        _sku_list,
                    ).fetchall():
                        _oos_alt_only_count[_r["sku"]] = _r["alt_n"]

            # [2] 認証情報と config を事前準備
            _ebay_creds = {
                'app_id': s.get("ebay_app_id", ""),
                'dev_id': s.get("ebay_dev_id", ""),
                'cert_id': s.get("ebay_cert_id", ""),
                'user_token': s.get("ebay_user_token", ""),
            }
            _cfg_path = Path(__file__).parent / "config" / "schedule_config.json"
            _cfg = {}
            if _cfg_path.exists():
                try:
                    _cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass

            st.caption(
                "各商品のすぐ下に仕入先候補(上位3件)を表示。候補に「採用」「不採用」をチェック → "
                "下部の「一括実行」ボタンで eBay API への反映 (ReviseItem) と DB 更新が同時に走ります。"
                " 採用は SKU 書換 / 不採用は学習データに記録。SKU・在庫数の直接編集は「確認」チェック必須。"
            )

            # [3] st.fragment 方式 (2026-04-24 Q3=A 対応):
            # 各商品ブロックは @st.fragment 化され、採用 checkbox の on_change で
            # SKU 欄が即時更新される (他商品には影響せず爆速維持)。
            # 一括実行ボタンは通常ボタン (form_submit_button ではない)、
            # 押下時に全 session_state を読み取って batch 処理する。
            for _item in oos_items:
                _sku_val = _item.get("sku") or ""
                _cands_for_sku = _cand_by_sku.get(_sku_val, [])[:3]
                _render_oos_block(_item, _cands_for_sku, "oos", _oos_alt_only_count)

            st.divider()
            st.caption(
                "採用チェック → SKU 欄が仕入先URLベースに自動更新されます。"
                " 確認後、下のボタンで eBay 反映・DB 更新を一括処理。"
                " 採用 mutex: 同一SKU内で複数 ON の場合は score 最上位を採用、他は不採用扱い。"
            )

            _submitted = st.button(
                "選択した採用/不採用/編集を一括実行（eBay・DB）",
                key="oos_batch_submit",
                type="primary", width="stretch",
            )

            # --- form 外: submit 時のバッチ処理 ---
            if _submitted:
                # 処理の順序:
                #   1) 不採用 (DB のみ、高速)
                #   2) 採用 (pending → accept + apply、accepted→apply のみ、eBay ReviseItem で URL→SKU 設定)
                #   3) 手動SKU/qty編集 (確認✓ かつ widget state vs 元データ diff)
                #   4) 確認済みフラグ更新
                adoptions_pending: list[int] = []     # pending 候補で 採用 checked
                adoptions_accepted: list[int] = []    # accepted 候補で 反映する checked
                rejections: list[int] = []
                _cid_to_eid: dict[int, str] = {}
                _cid_to_score: dict[int, int] = {}
                _eid_to_item: dict[str, dict] = {
                    _it["ebay_item_id"]: _it for _it in oos_items
                }
                # SKU ごとの 採用 候補リスト（mutex 用: 複数 ON なら上位score を採用）
                _sku_adopt_candidates: dict[str, list[int]] = {}

                for _it in oos_items:
                    _sku_for_cand = _it.get("sku") or ""
                    for _c in _cand_by_sku.get(_sku_for_cand, [])[:3]:
                        _cid_scan = _c["id"]
                        _cid_to_eid[_cid_scan] = _it["ebay_item_id"]
                        _cid_to_score[_cid_scan] = _c.get("match_score") or 0
                        if _c.get("status") == "pending":
                            if st.session_state.get(f"oos_adopt_{_cid_scan}", False):
                                _sku_adopt_candidates.setdefault(_sku_for_cand, []).append(_cid_scan)
                            elif st.session_state.get(f"oos_reject_{_cid_scan}", False):
                                rejections.append(_cid_scan)
                        elif _c.get("status") == "accepted":
                            if st.session_state.get(f"oos_applyck_{_cid_scan}", False):
                                adoptions_accepted.append(_cid_scan)

                # Mutex 適用: 同一SKU内で複数 採用 ON → score 最上位のみ採用、他は不採用扱い
                for _sku_m, _cids_m in _sku_adopt_candidates.items():
                    if len(_cids_m) == 1:
                        adoptions_pending.append(_cids_m[0])
                    else:
                        _cids_sorted = sorted(_cids_m, key=lambda x: _cid_to_score.get(x, 0), reverse=True)
                        _winner = _cids_sorted[0]
                        _losers = _cids_sorted[1:]
                        adoptions_pending.append(_winner)
                        rejections.extend(_losers)
                        st.warning(
                            f"SKU {_sku_m}: 採用チェックが {len(_cids_m)} 件ONでしたが、"
                            f"SKU置換は1つのみ可。score最上位(cid={_winner})を採用、他は不採用として処理します。"
                        )

                # 手動 SKU/qty 編集の収集
                sync_targets: list[dict] = []
                no_change_ids: list[str] = []
                for _it in oos_items:
                    eid = _it["ebay_item_id"]
                    is_checked = bool(st.session_state.get(f"oos_confirm_{eid}", False))
                    if not is_checked:
                        continue
                    cur_sku = st.session_state.get(f"oos_sku_{eid}", _it.get("sku") or "")
                    cur_qty = int(st.session_state.get(f"oos_qty_{eid}", _it["quantity_ebay"]))
                    orig_sku = _it.get("sku") or ""
                    orig_qty = int(_it["quantity_ebay"])
                    new_sku = cur_sku if cur_sku != orig_sku else None
                    new_qty = cur_qty if cur_qty != orig_qty else None
                    if new_sku is not None or new_qty is not None:
                        sync_targets.append(
                            {"ebay_item_id": eid, "new_sku": new_sku, "new_qty": new_qty}
                        )
                    else:
                        no_change_ids.append(eid)

                total_adoptions = len(adoptions_pending) + len(adoptions_accepted)
                total_actions = total_adoptions + len(rejections) + len(sync_targets) + len(no_change_ids)
                if total_actions == 0:
                    st.warning("チェックが入っている未処理商品がありません")
                else:
                    st.info(
                        f"処理中: 採用 {total_adoptions}件 "
                        f"(pending→apply {len(adoptions_pending)} + accepted→apply {len(adoptions_accepted)})"
                        f" / 不採用 {len(rejections)}件 / 変更あり {len(sync_targets)}件 "
                        f"/ 現状維持 {len(no_change_ids)}件"
                    )

                    # 1) 不採用 (DB のみ)
                    for _cid in rejections:
                        try:
                            update_supplier_candidate_status(_cid, "rejected")
                        except Exception as _e:
                            st.error(f"不採用 cid={_cid}: {_e}")
                    if rejections:
                        st.success(f"不採用 {len(rejections)}件 を記録しました")

                    # 2) 採用処理（pending→accept+apply と accepted→apply-only）
                    adopt_succeeded_eids: set[str] = set()
                    if total_adoptions > 0:
                        if not all(_ebay_creds.values()):
                            st.error("eBay API認証情報が未設定です（設定タブ参照）")
                        else:
                            from monitor.ebay_client import revise_inventory_quantity as _rev_qty

                            def _process_apply(_cid_p: int, _is_pending: bool) -> None:
                                # W114 (2026-05-09): Surface A の挙動を Surface B (W112 retrospective fix 後)
                                # に統一. 旧 Surface A は apply 失敗を `st.info("採用済（反映保留）")` で
                                # 表示 = Q0 偽装成功境界 + qty 復元 logger 痕跡なし = silent skip 境界
                                # → 全部 specific exception + logger + st.error に修正.
                                _eid_applied = _cid_to_eid.get(_cid_p, "?")
                                if _is_pending:
                                    _res_a = accept_supplier_candidate(_cid_p)
                                    if not _res_a.get("success"):
                                        logger.error(
                                            "supplier accept failed (Surface A) cid=%s eid=%s msg=%s",
                                            _cid_p, _eid_applied, _res_a.get("message"),
                                        )
                                        st.error(
                                            f"採用 cid={_cid_p}: "
                                            f"{_res_a.get('message') or 'accept失敗'}"
                                        )
                                        return
                                _res_b = apply_supplier_candidate(_cid_p, _cfg)
                                if not _res_b.get("success"):
                                    # W114: 旧 `st.info("採用済（反映保留）")` の Q0 偽装成功境界を fix.
                                    # apply 失敗は明確に error として表示 + logger 痕跡保存.
                                    logger.error(
                                        "supplier apply failed (Surface A) cid=%s eid=%s msg=%s",
                                        _cid_p, _eid_applied, _res_b.get("message"),
                                    )
                                    st.error(
                                        f"{_eid_applied}: eBay 反映失敗: "
                                        f"{_res_b.get('message') or 'apply エラー'} (cid={_cid_p})"
                                    )
                                    return
                                st.success(
                                    f"{_eid_applied}: 採用→SKU設定 成功 "
                                    f"({_res_b.get('message') or 'applied'})"
                                )
                                adopt_succeeded_eids.add(_eid_applied)
                                _it_adopted = _eid_to_item.get(_eid_applied)
                                if not _it_adopted:
                                    return
                                try:
                                    _cur_qty_a = int(_it_adopted.get("quantity_ebay") or 0)
                                except (TypeError, ValueError):
                                    _cur_qty_a = 0
                                if _cur_qty_a != 0:
                                    return  # qty 既に >=1 = 復元不要
                                # W114: qty 復元の specific exception + logger.exception
                                try:
                                    _qres = _rev_qty(_eid_applied, 1, **_ebay_creds)
                                except (RuntimeError, ConnectionError, TimeoutError, OSError) as _qe:
                                    logger.exception(
                                        "qty restore exception (Surface A) cid=%s eid=%s",
                                        _cid_p, _eid_applied,
                                    )
                                    st.error(
                                        f"{_eid_applied}: SKU 書換成功だが qty 復元中に例外 "
                                        f"({_qe}). 手動で在庫を 1 に戻してください (cid={_cid_p})."
                                    )
                                    return
                                if _qres.get("success"):
                                    update_ebay_listing_quantity(_eid_applied, 1)
                                    st.success(
                                        f"{_eid_applied}: 在庫 0 → 1 自動復元"
                                    )
                                else:
                                    logger.error(
                                        "qty restore api_failed (Surface A) cid=%s eid=%s msg=%s",
                                        _cid_p, _eid_applied, _qres.get("message"),
                                    )
                                    st.error(
                                        f"{_eid_applied}: SKU 書換成功だが qty 復元失敗 "
                                        f"({_qres.get('message') or 'error'}). "
                                        f"手動で在庫を 1 に戻してください (cid={_cid_p})."
                                    )

                            for _cid in adoptions_pending:
                                _process_apply(_cid, _is_pending=True)
                            for _cid in adoptions_accepted:
                                _process_apply(_cid, _is_pending=False)

                    # 3) 現状維持 → DBに確認済みフラグ
                    for eid in no_change_ids:
                        set_ebay_listing_risk_confirmed(eid, 1)

                    # 4) 変更あり → eBay APIに送信 (採用済 eid は SKU 上書きをスキップ)
                    if sync_targets:
                        if not all(_ebay_creds.values()):
                            st.error("eBay API認証情報が未設定です（設定タブ参照）")
                        else:
                            from monitor.ebay_client import (
                                revise_inventory_quantity, revise_item_sku,
                            )
                            progress = st.progress(0)
                            for i, ch in enumerate(sync_targets):
                                eid = ch["ebay_item_id"]
                                ok = True
                                if ch["new_qty"] is not None:
                                    r = revise_inventory_quantity(eid, ch["new_qty"], **_ebay_creds)
                                    if r["success"]:
                                        update_ebay_listing_quantity(eid, ch["new_qty"])
                                        st.success(f"{eid}: 在庫 → {ch['new_qty']}")
                                    else:
                                        st.error(f"{eid}: 在庫 → {r['message']}")
                                        ok = False
                                if ch["new_sku"] is not None:
                                    if eid in adopt_succeeded_eids:
                                        st.info(
                                            f"{eid}: SKU手動編集は採用反映と競合のためスキップ"
                                        )
                                    else:
                                        r = revise_item_sku(eid, ch["new_sku"], **_ebay_creds)
                                        if r["success"]:
                                            update_ebay_listing_sku(eid, ch["new_sku"])
                                            st.success(f"{eid}: SKU → {ch['new_sku']}")
                                        else:
                                            st.error(f"{eid}: SKU → {r['message']}")
                                            ok = False
                                if ok:
                                    set_ebay_listing_risk_confirmed(eid, 1)
                                progress.progress((i + 1) / len(sync_targets))
                    bump_db_version()  # W134 Step2: 在庫/SKU/risk 一括変更後 read-cache 無効化
                    st.rerun()

            # --- form 外: 候補未探索 SKU の即時探索（form内では button 使えないため分離） ---
            _missing_skus = [
                _it for _it in oos_items
                if not _cand_by_sku.get(_it.get("sku") or "", [])
            ]
            if _missing_skus:
                st.divider()
                st.markdown(
                    f'<div style="font-size:12px;color:rgba(180,220,255,0.7);margin-bottom:6px;">'
                    f'候補未探索 {len(_missing_skus)}件（次回02:30 Pattern 2バッチで max 50件自動探索／'
                    f'下記ボタンで個別または一括即時探索）'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # --- 一括探索ボタン ---
                _bulk_search_flag = "_oos_bulk_search_fired"
                _bulk_search_result = "_oos_bulk_search_result"
                _bulk_in_progress = st.session_state.get(_bulk_search_flag, False)
                _bulk_result = st.session_state.get(_bulk_search_result)

                _bc1, _bc2 = st.columns([3, 1])
                with _bc1:
                    _bulk_limit = st.number_input(
                        "一括探索する件数 (古い順・qty=0優先)",
                        min_value=1, max_value=len(_missing_skus),
                        value=min(50, len(_missing_skus)),
                        step=10,
                        key="oos_bulk_search_limit",
                        help=f"Claude API 1件あたり ~5秒 + スクレイプ ~30秒。{len(_missing_skus)}件全てで約 {len(_missing_skus) * 35 // 60} 分",
                    )
                with _bc2:
                    if _bulk_in_progress:
                        st.caption("一括探索 実行中…")
                    elif st.button(
                        "一括探索 開始", key="oos_bulk_search_btn",
                        type="primary", width="stretch",
                        help="未探索SKUを上限件数まで即時バックグラウンド探索。完了まで数分〜数十分",
                    ):
                        from tasks.task_supplier_candidate_search import (
                            run_supplier_candidate_search as _run_cs_bulk,
                        )
                        import threading as _th_bulk
                        # 古い順・qty=0優先で N 件選出
                        _sorted_missing = sorted(
                            _missing_skus,
                            key=lambda x: (
                                int(x.get("quantity_ebay") or 0),
                                x.get("source_out_of_stock_since") or "9999",
                            ),
                        )
                        _targets = [
                            (it["ebay_item_id"], it.get("sku") or "")
                            for it in _sorted_missing[:int(_bulk_limit)]
                            if it.get("sku")
                        ]

                        def _bg_bulk_search(targets=_targets, cfg=_cfg,
                                            flag_key=_bulk_search_flag,
                                            result_key=_bulk_search_result):
                            ok_count = 0
                            ng_count = 0
                            errors = []
                            try:
                                for eid, sku in targets:
                                    try:
                                        r = _run_cs_bulk(
                                            ebay_item_id=eid, sku=sku, config=cfg,
                                            discovered_via="ui_bulk_search",
                                        )
                                        if r.get("success"):
                                            ok_count += 1
                                        else:
                                            ng_count += 1
                                            errors.append(f"{sku}: {r.get('message', 'fail')[:40]}")
                                    except Exception as e:
                                        ng_count += 1
                                        errors.append(f"{sku}: 例外 {str(e)[:40]}")
                            finally:
                                st.session_state[result_key] = {
                                    "ok": ok_count, "ng": ng_count,
                                    "total": len(targets),
                                    "errors": errors[:10],  # 最初の10件のみ保持
                                }
                                st.session_state[flag_key] = False

                        _th_bulk.Thread(target=_bg_bulk_search, daemon=True).start()
                        st.session_state[_bulk_search_flag] = True
                        st.session_state.pop(_bulk_search_result, None)  # 前回結果をクリア
                        st.success(
                            f"{len(_targets)}件 の一括探索を開始しました。"
                            f"数分後にページを更新すると候補が表示されます。"
                        )

                # 一括探索の結果表示
                if _bulk_result is not None and not _bulk_in_progress:
                    if _bulk_result["ng"] == 0:
                        st.success(
                            f"一括探索完了: {_bulk_result['ok']}/{_bulk_result['total']} 件"
                            f" 成功"
                        )
                    else:
                        st.warning(
                            f"一括探索完了: 成功 {_bulk_result['ok']}件 / "
                            f"失敗 {_bulk_result['ng']}件 (詳細は下記)"
                        )
                        for _err in _bulk_result.get("errors", []):
                            st.caption(f"  × {_err}")

                _cols_per_row = 3
                for _i_start in range(0, len(_missing_skus), _cols_per_row):
                    _cols_m = st.columns(_cols_per_row)
                    for _j, _it_m in enumerate(_missing_skus[_i_start:_i_start + _cols_per_row]):
                        with _cols_m[_j]:
                            _eid_m = _it_m["ebay_item_id"]
                            _sku_m2 = _it_m.get("sku") or ""
                            _title_m = (_it_m.get("title") or "")[:30]
                            _flag_k = f"_oos_search_fired_{_eid_m}"
                            _result_k_display = f"_oos_search_result_{_eid_m}"
                            _last_result = st.session_state.get(_result_k_display)
                            if st.session_state.get(_flag_k, False):
                                st.caption(f"{_sku_m2}: 探索実行中…")
                            elif _last_result is not None and not _last_result.get("ok"):
                                # 直近の探索失敗を表示、再試行ボタン併設
                                st.caption(
                                    f"{_sku_m2}: 前回失敗 — {_last_result.get('msg', '')[:60]}"
                                )
                                if _sku_m2 and st.button(
                                    f"再試行 {_sku_m2}",
                                    key=f"oos_search_retry_{_eid_m}",
                                    help=f"{_title_m} の候補探索を再試行",
                                    width="stretch",
                                ):
                                    # 失敗結果をクリアして再実行フローへフォールスルー
                                    st.session_state.pop(_result_k_display, None)
                                    st.rerun()
                            elif _sku_m2 and st.button(
                                f"探索 {_sku_m2}",
                                key=f"oos_search_outside_{_eid_m}",
                                help=f"{_title_m} の候補を即時探索（1〜2分・バックグラウンド）",
                                width="stretch",
                            ):
                                from tasks.task_supplier_candidate_search import (
                                    run_supplier_candidate_search as _run_cs,
                                )
                                import threading as _th
                                _sku_tgt = _sku_m2
                                _eid_tgt = _eid_m
                                # 2026-04-20 修正 (HIGH-1): silent failure 対策
                                # 旧: except Exception: pass + flag_k 残置 → 失敗 SKU が再試行不能
                                # 新: 例外も結果も session_state に保存、flag_k は finally で必ず下ろす
                                _result_k = f"_oos_search_result_{_eid_m}"
                                def _bg_cs(eid=_eid_tgt, sku=_sku_tgt, cfg=_cfg,
                                           result_key=_result_k, flag_key=_flag_k):
                                    try:
                                        r = _run_cs(
                                            ebay_item_id=eid, sku=sku, config=cfg,
                                            discovered_via="ui_on_demand",
                                        )
                                        st.session_state[result_key] = {
                                            "ok": bool(r.get("success")),
                                            "msg": r.get("message") or "",
                                        }
                                    except Exception as e:
                                        st.session_state[result_key] = {
                                            "ok": False, "msg": f"例外: {e}",
                                        }
                                    finally:
                                        st.session_state[flag_key] = False
                                _th.Thread(target=_bg_cs, daemon=True).start()
                                st.session_state[_flag_k] = True
                                st.success(f"{_sku_m2}: 探索開始。数分後にページ更新してください。")

            # --- form 外: 一括在庫0 ---
            st.divider()
            _bulk_confirm = st.checkbox(
                f"上記 {len(oos_items)}件 の在庫を一括で0にする",
                key="bulk_confirm_oos",
            )
            if _bulk_confirm:
                st.warning(f"{len(oos_items)}件 のeBay在庫を全て0に変更します。")
                if st.button("一括在庫0実行", key="bulk_qty0_oos", type="primary"):
                    if not all(_ebay_creds.values()):
                        st.error("eBay API認証情報が未設定です（設定タブ参照）")
                    else:
                        from monitor.ebay_client import revise_inventory_quantity
                        with st.status(
                            f"{len(oos_items)}件の在庫を0に変更中...", expanded=True,
                        ) as status:
                            success_count = 0
                            fail_count = 0
                            for i, item in enumerate(oos_items):
                                st.write(f"▸ [{i+1}/{len(oos_items)}] {item['ebay_item_id']}")
                                result = revise_inventory_quantity(
                                    item["ebay_item_id"], 0, **_ebay_creds
                                )
                                if result['success']:
                                    update_ebay_listing_quantity(item["ebay_item_id"], 0)
                                    bump_db_version()  # W134 Step2: 在庫0化後 read-cache 無効化
                                    # 業務ロジック: 一括在庫0 実行 = RISK 解消 (販売停止)。
                                    # risk_confirmed は自動セットしない。必要な listing は
                                    # user が個別に「確認」チェックで手動追跡。
                                    success_count += 1
                                else:
                                    fail_count += 1
                            if fail_count == 0:
                                status.update(
                                    label=f"完了 — {success_count}件の在庫を0に変更",
                                    state="complete",
                                )
                            else:
                                status.update(
                                    label=f"完了 — 成功 {success_count}件 / 失敗 {fail_count}件",
                                    state="complete",
                                )
                        st.rerun()
        else:
            st.success("仕入先在庫切れの商品はありません。")

        st.divider()

        # --- 確認不可 ---
        st.markdown(f"### 確認不可 ({len(pnf_items)}件)")
        st.caption("仕入先ページが削除済み。出品停止または別の仕入先を探す必要があります。")
        if pnf_items:
            _render_risk_table_with_actions(pnf_items, "pnf")
        else:
            st.success("確認不可の商品はありません。")

    # ---------- 監視リスト ----------
    with monitor_tab1:
        h1, h2, h3 = st.columns([3, 1, 1])
        with h1:
            st.subheader("監視中アイテム")
        with h2:
            if st.button("eBay同期"):
                app_id = s.get("ebay_app_id", "")
                dev_id = s.get("ebay_dev_id", "")
                cert_id = s.get("ebay_cert_id", "")
                user_token = s.get("ebay_user_token", "")
                if not all([app_id, dev_id, cert_id, user_token]):
                    st.error("eBay API認証情報が未設定です（設定タブ参照）")
                else:
                    with st.spinner("eBay APIから取得中..."):
                        try:
                            from monitor.ebay_client import get_active_listings, filter_items_with_sku
                            listings = get_active_listings(app_id, dev_id, cert_id, user_token)
                            sku_items = filter_items_with_sku(listings)
                            synced, skipped = 0, 0
                            for item in sku_items:
                                cfg = find_site_config_by_sku(item["sku"])
                                if cfg:
                                    upsert_item(sku=item["sku"], ebay_item_id=item["item_id"], title=item["title"])
                                    synced += 1
                                else:
                                    skipped += 1
                            st.success(f"同期完了: {synced}件 / スキップ: {skipped}件（変換URL未設定）")
                            st.rerun()
                        except Exception as e:
                            st.error(f"同期エラー: {e}")
        with h3:
            if st.button("▶ 全件チェック", type="primary"):
                items = get_active_items()
                if not items:
                    st.warning("監視中のアイテムがありません。")
                else:
                    configs = get_site_configs()
                    configs_by_prefix = {c["convert_url"]: c for c in configs}
                    batch = prepare_batch_items(items, configs_by_prefix)
                    if not batch:
                        st.warning("チェック可能なアイテムがありません。")
                    else:
                        with st.spinner(f"{len(batch)}件をバッチチェック中...（ブラウザ再利用で高速化）"):
                            try:
                                results = check_items_batch(batch)
                            except Exception as e:
                                st.error(f"チェックエラー: {e}")
                                results = {}
                        webhook = s.get("discord_webhook_url", "")
                        items_by_id = {item["id"]: item for item in items}
                        notified = 0
                        for item_id, status in results.items():
                            item = items_by_id.get(item_id)
                            if not item:
                                continue
                            prev = get_prev_status(item_id)
                            update_item_status(item_id, status)
                            discord_sent = False
                            if status in ("unavailable", "not_found") and prev not in ("unavailable", "not_found"):
                                item["last_status"] = status
                                discord_sent = send_unavailable_alert(webhook, item)
                                if discord_sent:
                                    notified += 1
                            add_check_log(item_id, status, discord_sent)
                        st.success(f"全件チェック完了: {len(results)}件チェック / {notified}件通知")
                        st.rerun()

        # 手動追加フォーム
        _show_manual_add = st.checkbox("手動登録（eBay SKU）", key="chk_manual_add")
        if _show_manual_add:
            mc1, mc2, mc3 = st.columns([2, 2, 1])
            with mc1:
                new_sku = st.text_input(
                    "eBay SKU（カスタムラベル）",
                    placeholder="例: ebayme_m12345678",
                    key="new_sku",
                )
            with mc2:
                new_title = st.text_input("メモ（任意）", key="new_title")
            with mc3:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("追加", key="add_item"):
                    if new_sku:
                        cfg = find_site_config_by_sku(new_sku)
                        url = build_source_url(new_sku)
                        if not cfg:
                            st.error("対応する変換URLが見つかりません。サイト設定タブで確認してください。")
                        else:
                            add_item_manual(sku=new_sku, title=new_title)
                            st.success(f"追加: {new_sku} → {url}")
                            st.rerun()
                    else:
                        st.error("SKUを入力してください。")

        st.divider()

        # アイテム一覧テーブル
        STATUS_EMOJI = {
            "available": "●", "unavailable": "○",
            "not_found": "◆", "error": "[!]", "unknown": "[?]",
        }
        STATUS_LABEL = {
            "available": "在庫あり", "unavailable": "在庫切れ",
            "not_found": "ページなし", "error": "エラー", "unknown": "未チェック",
        }

        all_items = get_all_items()
        if not all_items:
            st.info("監視アイテムがありません。eBay同期か手動追加で登録してください。\n\nSKUフォーマット例: `ebayme_m12345678` / `ebayrm_abc123` / `ebayh_xyz789`")
        else:
            # テーブル表示
            header_cols = st.columns([0.5, 1.2, 2.2, 1.8, 1.2, 0.6, 0.7, 0.6])
            header_cols[0].markdown("**状態**")
            header_cols[1].markdown("**Item ID**")
            header_cols[2].markdown("**仕入元URL**")
            header_cols[3].markdown("**SKU / メモ**")
            header_cols[4].markdown("**最終確認**")
            header_cols[5].markdown("**チェック**")
            header_cols[6].markdown("**在庫0**")
            header_cols[7].markdown("**削除**")
            st.divider()

            for item in all_items:
                status = item.get("last_status", "unknown")
                emoji = STATUS_EMOJI.get(status, "[?]")
                label = STATUS_LABEL.get(status, status)
                source_url = item.get("source_url", "")
                last_check = item.get("last_check", "-")
                sku = item.get("sku", "")
                title = item.get("title", "")
                ebay_item_id = item.get("ebay_item_id", "")

                row = st.columns([0.5, 1.2, 2.2, 1.8, 1.2, 0.6, 0.7, 0.6])
                with row[0]:
                    st.markdown(f"{emoji}")
                    st.caption(label)
                with row[1]:
                    if ebay_item_id:
                        st.markdown(
                            f"<a href='https://www.ebay.com/itm/{ebay_item_id}' target='_blank' "
                            f"style='font-size:12px;'>{ebay_item_id}</a>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.caption("-")
                with row[2]:
                    if source_url:
                        # URLを短縮表示
                        display_url = source_url if len(source_url) <= 45 else source_url[:42] + "..."
                        st.markdown(f"[{display_url}]({source_url})")
                    else:
                        st.caption("URL未解決")
                with row[3]:
                    st.code(sku, language=None)
                    if title:
                        st.caption(title)
                with row[4]:
                    st.caption(str(last_check)[:16] if last_check else "-")
                with row[5]:
                    if st.button("▶", key=f"chk_{item['id']}", help="今すぐチェック"):
                        cfg = find_site_config_by_sku(sku)
                        if cfg and source_url:
                            with st.spinner("チェック中..."):
                                new_status = check_item_by_config(item, cfg)
                            update_item_status(item["id"], new_status)
                            add_check_log(item["id"], new_status)
                            st.rerun()
                        else:
                            st.error("サイト設定が見つかりません")
                with row[6]:
                    if st.button("X", key=f"qty0_{item['id']}", help="eBay在庫を0に変更"):
                        if not ebay_item_id:
                            st.error("Item IDなし")
                        else:
                            ebay_creds = {
                                'app_id': s.get("ebay_app_id", ""),
                                'dev_id': s.get("ebay_dev_id", ""),
                                'cert_id': s.get("ebay_cert_id", ""),
                                'user_token': s.get("ebay_user_token", ""),
                            }
                            if not all(ebay_creds.values()):
                                st.error("eBay API未設定")
                            else:
                                from monitor.ebay_client import revise_inventory_quantity
                                result = revise_inventory_quantity(
                                    ebay_item_id, 0, **ebay_creds
                                )
                                if result['success']:
                                    update_ebay_listing_quantity(ebay_item_id, 0)
                                    bump_db_version()  # W134 Step2: 在庫0化後 read-cache 無効化
                                    st.success(f"{ebay_item_id} qty set to 0")
                                    st.rerun()
                                else:
                                    st.error(result['message'])
                with row[7]:
                    if st.button("DEL", key=f"del_{item['id']}", help="削除"):
                        delete_item(item["id"])
                        st.rerun()

                st.divider()

        # チェック履歴
        _show_check_logs = st.checkbox("チェックログ（直近20件）", key="chk_check_logs")
        if _show_check_logs:
            logs = get_recent_logs(20)
            if logs:
                log_data = [{
                    "時刻": str(log["checked_at"])[:16],
                    "SKU": log.get("sku", ""),
                    "状態": STATUS_EMOJI.get(log["status"], "[?]") + " " + STATUS_LABEL.get(log["status"], log["status"]),
                    "Discord": "[OK]" if log["discord_sent"] else "-",
                } for log in logs]
                st.dataframe(pd.DataFrame(log_data), hide_index=True, width="stretch")
            else:
                st.info("履歴がありません。")

    # ---------- サイト設定 ----------
    with monitor_tab2:
        st.subheader("サイト別設定")
        st.caption("変換URLはeBay SKUのプレフィックス。例: `ebayme_` → メルカリ `https://jp.mercari.com/item/m` + 商品ID")

        configs = get_site_configs()

        # 設定テーブル（編集用）
        col_headers = st.columns([1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 2, 1.2, 0.5])
        labels = ["サイト名", "販売中1", "販売中2", "売切テキスト", "ページなしテキスト", "変換URL", "共通URL（仕入元ベースURL）", "有効", "削除"]
        for col, label in zip(col_headers, labels):
            col.markdown(f"**{label}**")
        st.divider()

        for cfg in configs:
            row = st.columns([1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 2, 1.2, 0.5])
            cfg_id = cfg["id"]

            updated = dict(cfg)
            updated["site_name"] = row[0].text_input("サイト名", value=cfg["site_name"], key=f"sn_{cfg_id}", label_visibility="collapsed")
            updated["in_stock_text1"] = row[1].text_input("在庫テキスト1", value=cfg.get("in_stock_text1", ""), key=f"is1_{cfg_id}", label_visibility="collapsed")
            updated["in_stock_text2"] = row[2].text_input("在庫テキスト2", value=cfg.get("in_stock_text2", ""), key=f"is2_{cfg_id}", label_visibility="collapsed")
            updated["sold_out_text"] = row[3].text_input("売切テキスト", value=cfg.get("sold_out_text", ""), key=f"so_{cfg_id}", label_visibility="collapsed")
            updated["no_page_text"] = row[4].text_input("ページなしテキスト", value=cfg.get("no_page_text", ""), key=f"np_{cfg_id}", label_visibility="collapsed")
            updated["convert_url"] = row[5].text_input("変換URL", value=cfg.get("convert_url", ""), key=f"cu_{cfg_id}", label_visibility="collapsed")
            updated["common_url"] = row[6].text_input("共通URL", value=cfg.get("common_url", ""), key=f"comu_{cfg_id}", label_visibility="collapsed")
            updated["is_active"] = int(row[7].checkbox("有効", value=bool(cfg.get("is_active", 1)), key=f"act_{cfg_id}", label_visibility="collapsed"))

            if row[8].button("DEL", key=f"del_cfg_{cfg_id}"):
                delete_site_config(cfg_id)
                st.rerun()

            # 変更があれば自動保存（保存ボタンで一括保存）
            st.session_state[f"cfg_edit_{cfg_id}"] = updated

        st.divider()
        save_col, add_col = st.columns([1, 3])
        with save_col:
            if st.button("設定を保存", type="primary", key="save_site_cfg"):
                for cfg in configs:
                    cfg_id = cfg["id"]
                    edited = st.session_state.get(f"cfg_edit_{cfg_id}")
                    if edited:
                        save_site_config(edited)
                st.success("サイト設定を保存しました。")
                st.rerun()

        with add_col:
            _show_site_add = st.checkbox("サイト新規追加", key="chk_site_add")
            if _show_site_add:
                nc1, nc2, nc3, nc4 = st.columns(4)
                new_cfg_name = nc1.text_input("サイト名", key="new_cfg_name")
                new_cfg_convert = nc2.text_input("変換URL（例: ebayXX_）", key="new_cfg_convert")
                new_cfg_common = nc3.text_input("共通URL", key="new_cfg_common")
                new_cfg_instock = nc4.text_input("販売中テキスト", key="new_cfg_instock")
                if st.button("追加", key="add_cfg"):
                    if new_cfg_name and new_cfg_convert:
                        save_site_config({
                            "site_name": new_cfg_name,
                            "convert_url": new_cfg_convert,
                            "common_url": new_cfg_common,
                            "in_stock_text1": new_cfg_instock,
                            "in_stock_text2": "",
                            "sold_out_text": "",
                            "no_page_text": "",
                        })
                        st.success(f"追加しました: {new_cfg_name}")
                        st.rerun()
                    else:
                        st.error("サイト名と変換URLは必須です。")


# ========== eBay連携タブ ==========
if _w134_sel == "eBay連携":
    st.subheader("eBay出品との同期")
    ebay_col1, ebay_col2 = st.columns([2, 1])

    with ebay_col1:
        if st.button("eBay出品取得・同期", type="primary", width="stretch"):
            app_id = s.get("ebay_app_id", "")
            dev_id = s.get("ebay_dev_id", "")
            cert_id = s.get("ebay_cert_id", "")
            user_token = s.get("ebay_user_token", "")

            if not all([app_id, dev_id, cert_id, user_token]):
                st.error("eBay API認証情報が未設定です（設定タブ参照）")
            else:
                with st.status("eBay同期を実行中...", expanded=True) as status:
                    try:
                        st.write("▸ eBay APIから出品データを取得中...")
                        result = sync_listings_from_ebay(app_id, dev_id, cert_id, user_token)

                        st.write(f"▸ 同期: {result['synced']}件 / マッチ: {result['matched']}件")

                        if result["errors"] > 0:
                            st.write(f"▸ エラー: {result['errors']}件")
                            status.update(label=f"同期完了（エラー {result['errors']}件）", state="complete")
                        else:
                            status.update(label=f"同期完了 — {result['synced']}件取得", state="complete")

                        st.rerun()
                    except Exception as e:
                        status.update(label="同期失敗", state="error")
                        st.error(f"エラー: {e}")

    with ebay_col2:
        if st.button("自動ランク更新", type="secondary", width="stretch"):
            with st.status("ランク再計算中...", expanded=True) as status:
                try:
                    st.write("▸ Watch数・販売数をベースにスコア計算中...")
                    result = auto_rank_all_listings_in_db()
                    st.write(f"▸ {result['rank_assigned']}件のランクを更新")
                    if result["errors"] > 0:
                        st.write(f"▸ エラー: {result['errors']}件")
                        status.update(label=f"ランク更新完了（エラー {result['errors']}件）", state="complete")
                    else:
                        status.update(label=f"ランク更新完了 — {result['rank_assigned']}件", state="complete")
                    st.rerun()
                except Exception as e:
                    status.update(label="ランク更新失敗", state="error")
                    st.error(f"エラー: {e}")
        if st.button("レポート表示"):
            try:
                report = get_sync_report()
                st.json(report)
            except Exception as e:
                st.error(f"レポート生成エラー: {e}")

    st.divider()
    st.subheader("eBay出品一覧")

    # ランク統計
    try:
        rank_stats = get_rank_stats()
        col1, col2, col3, col4, col5, col6, col7 = st.columns(7)
        with col1:
            st.metric("全体", sum(rank_stats.values()))
        with col2:
            st.metric("S (最優先)", rank_stats.get('S', 0))
        with col3:
            st.metric("A (高)", rank_stats.get('A', 0))
        with col4:
            st.metric("B (中高)", rank_stats.get('B', 0))
        with col5:
            st.metric("C (中)", rank_stats.get('C', 0))
        with col6:
            st.metric("D (低)", rank_stats.get('D', 0))
        with col7:
            st.metric("E (最低)", rank_stats.get('E', 0))
    except Exception as e:
        st.warning(f"ランク統計取得エラー: {e}")

    st.divider()

    try:
        ebay_items = get_ebay_listings_by_rank(order_by_rank=True)
        if not ebay_items:
            st.info("eBay出品が登録されていません。上記で同期してください。")
        else:
            # 出品テーブル表示
            df_data = []
            shipping_warnings = []

            for item in ebay_items:
                price = item.get('current_price', 0)
                shipping = item.get('shipping_cost', 0)

                # 送料検証
                shipping_check = check_shipping_cost(price, shipping)
                warning_indicator = "[!]" if shipping_check['status'] == "WARNING" else ""

                # 警告がある場合は記録
                if shipping_check['status'] == "WARNING":
                    shipping_warnings.append({
                        'item_id': item['ebay_item_id'],
                        'sku': item['sku'],
                        'title': item.get('title', ''),
                        'price': price,
                        'shipping': shipping,
                        'check': shipping_check
                    })

                df_data.append({
                    "WARN": warning_indicator,
                    "Rank": rank_to_stars(item.get("rank", "C")),
                    "Item ID": item["ebay_item_id"],
                    "SKU": item["sku"],
                    "Title": item["title"][:50] + "..." if len(item["title"] or "") > 50 else item["title"],
                    "Price": f"${price:.2f}",
                    "Shipping": f"${shipping:.2f}",
                    "eBay Qty": item.get("quantity_ebay", 0),
                    "Source Status": item.get("source_status", "unknown"),
                    "Last Sync": item.get("last_synced_at", "未同期")[:10] if item.get("last_synced_at") else "未同期",
                })

            df = pd.DataFrame(df_data)
            st.dataframe(df, width="stretch", hide_index=True)

            st.caption(f"合計: {len(ebay_items)}件 | ソース紐付: {len([x for x in ebay_items if x.get('source_status')])}件 | 送料警告: {len(shipping_warnings)}件")

            # 送料警告詳細（クリック可能）
            if shipping_warnings:
                _show_ship_warn = st.checkbox(f"[!] Shipping cost warnings ({len(shipping_warnings)})", key="chk_ship_warn")
                if _show_ship_warn:
                    st.subheader("送料が適正範囲外の商品")
                    st.caption("商品価格の20%を基準に、±15%の範囲内に収まっていない商品を表示します")
                    st.divider()

                    for warning in shipping_warnings:
                        with st.container(border=True):
                            col1, col2 = st.columns([3, 2])

                            with col1:
                                st.markdown(f"**{warning['sku']}** - {warning['title'][:60]}")
                                st.caption(f"Item ID: {warning['item_id']}")

                            with col2:
                                check = warning['check']
                                st.metric("誤差率", f"{check['error_pct']:+.1f}%")

                            # 詳細情報を3列で表示
                            detail_col1, detail_col2, detail_col3 = st.columns(3)

                            with detail_col1:
                                st.caption("商品価格")
                                st.write(f"**${warning['price']:.2f}**")

                            with detail_col2:
                                st.caption("期待される送料（20%）")
                                st.write(f"**${check['expected']:.2f}**")
                                st.caption(f"許容範囲: ${check['expected'] * 0.85:.2f} - ${check['expected'] * 1.15:.2f}")

                            with detail_col3:
                                st.caption("実際の送料")
                                st.write(f"**${check['actual']:.2f}**")

                            # 警告メッセージを表示
                            if check['message']:
                                st.error(f"[!] {check['message']}")

                            st.caption(f"状態: {check['status']}")

            # ランク分布詳細（クリック可能）
            _show_rank_dist = st.checkbox("ランク分布（View/Watch/伸び率）", key="chk_rank_dist")
            if _show_rank_dist:
                try:
                    dist_details = get_rank_distribution_details()
                    for rank in ['S', 'A', 'B', 'C', 'D', 'E']:
                        if rank in dist_details:
                            info = dist_details[rank]
                            col_rank, col_count = st.columns([2, 1])
                            with col_rank:
                                st.markdown(f"**{rank} {rank_to_stars(rank)[0:10]}**")
                            with col_count:
                                st.metric("件数", info.get('count', 0))

                            if info.get('count', 0) > 0:
                                detail_col1, detail_col2, detail_col3, detail_col4, detail_col5 = st.columns(5)
                                with detail_col1:
                                    st.caption(f"平均Watch: {info.get('avg_watch', 0):.1f}")
                                with detail_col2:
                                    st.caption(f"平均View: {info.get('avg_view', 0):.1f}")
                                with detail_col3:
                                    st.caption(f"平均販売数(30d): {info.get('avg_sales', 0):.1f}")
                                with detail_col4:
                                    st.caption(f"平均Watch伸び: {info.get('avg_watch_growth', 0):.1f}%")
                                with detail_col5:
                                    st.caption(f"平均View伸び: {info.get('avg_view_growth', 0):.1f}%")
                except Exception as e:
                    st.warning(f"ランク分布詳細取得エラー: {e}")

            # ランク編集セクション
            _show_rank_edit = st.checkbox("ランク手動変更", key="chk_rank_edit")
            if _show_rank_edit:
                st.subheader("商品別ランク設定")
                st.caption("S（最優先）→ A（高）→ B（中高）→ C（中）→ D（低）→ E（最低）")

                edit_cols = st.columns([2, 1, 1])
                with edit_cols[0]:
                    selected_sku = st.selectbox(
                        "商品を選択",
                        options=[f"{item['sku']} - {item['title'][:40]}" for item in ebay_items],
                        key="rank_edit_sku"
                    )

                if selected_sku:
                    # 2026-05-20: item['sku']='' (eBay 側で SKU 空) でも誤マッチしない
                    # よう空文字ガード追加 (空文字は全 string に startswith True で
                    # 最初の item を誤選択する footgun、Codex 指摘)。
                    selected_item = next(
                        (item for item in ebay_items
                         if item['sku'] and selected_sku.startswith(item['sku'])),
                        None,
                    )
                    if selected_item:
                        current_rank = selected_item.get('rank', 'C')

                        with edit_cols[1]:
                            new_rank = st.selectbox(
                                "新しいランク",
                                options=['S', 'A', 'B', 'C', 'D', 'E'],
                                index=['S', 'A', 'B', 'C', 'D', 'E'].index(current_rank) if current_rank in ['S', 'A', 'B', 'C', 'D', 'E'] else 2,
                                key="rank_edit_value"
                            )

                        with edit_cols[2]:
                            if st.button("更新", key="rank_update_btn"):
                                try:
                                    update_ebay_listing_rank(selected_item['ebay_item_id'], new_rank)
                                    bump_db_version()  # W134 Step2: ランク変更後 read-cache 無効化
                                    st.success(f"{new_rank}に更新しました")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"更新エラー: {e}")
    except Exception as e:
        st.error(f"出品一覧取得エラー: {e}")


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


# ========== 最安値チェック タブ (W98) ==========
# 自分の商品ごとに監視したいライバルを登録し、ライバルより 0.01 USD 安く出して
# eBay 検索順位ブースト (SEO) を狙う。実際の価格巡回・値下げ実行は別タスク (G2-G4) で実装。
if _w134_sel == "最安値チェック":
    st.title("最安値チェック")
    st.caption(
        "商品ごとに最大 10 ライバルを登録し、ライバルより 1 セント安く出して "
        "eBay 検索の最安値表示を維持します。値下げ実行は次フェーズ (G2-G4) で実装予定。"
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
    _lp_cfg_path = Path(__file__).parent / 'config' / 'schedule_config.json'
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


# ========== 手動実行タブ ==========
if _w134_sel == "手動実行":
    st.subheader("タスク手動実行")
    st.caption("ここからタスクを即時実行できます。通常は定時実行（5:00 / 11:00 / 17:00 / 22:00）で自動実行されます。")

    # ────────────────────────────────
    # クイック実行セクション
    # ────────────────────────────────
    st.markdown("### クイック実行")
    st.caption("ボタン1つで即時実行。結果は組織(.company)に自動配信されます。")

    # 即時実行タスク定義
    # W21 (2026-04-26): 'research' を削除済 (死蔵化、出力 .company/research/notes/*.md
    # が DASHBOARD から削除 4/23 で誰も参照しないため). 将来 W23 Research 脳が代替.
    _QUICK_TASKS = {
        'email':     ('メール取得',        'tasks.task_email_pickup',       'run_email_pickup'),
        'news':      ('ニュースチェック',   'tasks.task_news_check',         'run_news_check'),
        'rival':     ('ライバル検出',      'tasks.task_rival_detection',    'run_rival_detection'),
        'alert':     ('在庫アラート',      'tasks.task_inventory_alert',    'run_inventory_alert'),
        'supplier':  ('仕入先候補',        'tasks.task_supplier_select',    'run_supplier_select'),
        'data_sync': ('データ統合',        'tasks.task_sync_data_stores',   'run_sync_data_stores'),
        'price':     ('価格最適化',        'tasks.task_price_optimization', 'run_price_optimization'),
        'sales':     ('売上トラッキング',   'tasks.task_sales_tracking',     'run_sales_tracking'),
        'video_learning': ('動画学習',      'tasks.task_video_learning',     'run_video_learning_queue'),
    }

    # 3列でボタン配置
    _quick_cols = st.columns(3)
    _quick_keys = list(_QUICK_TASKS.keys())
    for _qi, _qkey in enumerate(_quick_keys):
        _qdisplay, _qmod, _qfunc = _QUICK_TASKS[_qkey]
        with _quick_cols[_qi % 3]:
            if st.button(_qdisplay, key=f"quick_{_qkey}", width="stretch"):
                st.session_state[f"_run_quick_{_qkey}"] = True

    # 実行処理（ボタン押下後に表示）
    for _qkey in _quick_keys:
        if st.session_state.get(f"_run_quick_{_qkey}"):
            _qdisplay, _qmod, _qfunc = _QUICK_TASKS[_qkey]
            st.session_state[f"_run_quick_{_qkey}"] = False

            with st.status(f"{_qdisplay} 実行中...", expanded=True) as _qstatus:
                _qstart = time.time()
                try:
                    import importlib as _il
                    _qm = _il.import_module(_qmod)
                    _qf = getattr(_qm, _qfunc)

                    # config を読み込み
                    _qconfig_path = Path(__file__).parent / 'config' / 'schedule_config.json'
                    _qconfig = {}
                    if _qconfig_path.exists():
                        with open(_qconfig_path, 'r', encoding='utf-8') as _cf:
                            _qconfig = json.load(_cf)

                    st.write(f"▸ {_qdisplay} を実行中...")
                    _qresult = _qf(_qconfig)
                    _qelapsed = time.time() - _qstart

                    if isinstance(_qresult, dict) and _qresult.get('success') is False:
                        _qstatus.update(label=f"{_qdisplay} — 失敗", state="error")
                        st.error(f"エラー: {_qresult.get('error', '不明')}")
                    else:
                        _qstatus.update(label=f"{_qdisplay} — 完了 ({_qelapsed:.1f}秒)", state="complete")
                        _qmsg = _qresult.get('message', '') if isinstance(_qresult, dict) else ''
                        if _qmsg:
                            st.success(_qmsg)

                        # 主要な数値を表示
                        if isinstance(_qresult, dict):
                            _qmetrics = {k: v for k, v in _qresult.items()
                                         if k not in ('success', 'message', 'error', 'opportunities',
                                                       'report', 'alerts', 'sellers', 'news', 'emails',
                                                       'status', 'results', 'by_source', 'changes',
                                                       'sku_sync', 'inventory_sync', 'enrichment_sync')
                                         and isinstance(v, (int, float))}
                            if _qmetrics:
                                _qmcols = st.columns(min(len(_qmetrics), 4))
                                for _mi, (_mk, _mv) in enumerate(_qmetrics.items()):
                                    with _qmcols[_mi % len(_qmcols)]:
                                        st.metric(_mk, _mv)

                        # ── タスク別 結果詳細表示 ──
                        if isinstance(_qresult, dict):

                            # Email fetch: mail list
                            if _qkey == 'email' and _qresult.get('emails'):
                                st.markdown("**取得したメール:**")
                                for _em in _qresult['emails']:
                                    _subj = _em.get('subject', 'N/A')
                                    _from = _em.get('from', '')
                                    _date = _em.get('date', '')
                                    st.markdown(f"- **{_subj}**  \n  `{_from}` | {_date}")

                            # AI News: news list
                            if _qkey == 'news' and _qresult.get('news'):
                                st.markdown("**取得したニュース:**")
                                for _nw in _qresult['news'][:10]:
                                    _title = _nw.get('title', 'N/A')
                                    _src = _nw.get('source', '')
                                    _imp = _nw.get('impact', '')
                                    _icon = '[HIGH]' if _imp == 'high' else '[MED]' if _imp == 'medium' else '[LOW]'
                                    st.markdown(f"- {_icon} **{_title}** ({_src})")

                            # Research: result summary
                            if _qkey == 'research':
                                _trends = _qresult.get('trends', [])
                                _analysis = _qresult.get('analysis', {})
                                if _trends:
                                    st.markdown("**市場トレンド:**")
                                    for _tr in _trends[:10]:
                                        _tname = _tr.get('keyword', _tr.get('title', 'N/A'))
                                        _tcount = _tr.get('count', _tr.get('total', ''))
                                        st.markdown(f"- **{_tname}** {f'({_tcount}件)' if _tcount else ''}")
                                if _analysis:
                                    _show_analysis = st.checkbox("分析詳細", key=f"chk_analysis_{_qkey}")
                                    if _show_analysis:
                                        st.json(_analysis)

                            # Rival detection
                            if _qkey == 'rival' and _qresult.get('sellers'):
                                st.markdown("**検出されたライバルセラー:**")
                                for _sl in _qresult['sellers'][:10]:
                                    _sname = _sl.get('seller', 'N/A')
                                    _sfb = _sl.get('feedback_score', 0)
                                    _scomp = _sl.get('competing_count', 0)
                                    st.markdown(f"- **{_sname}** | FB: {_sfb} | 競合商品: {_scomp}件")

                            # Inventory alert
                            if _qkey == 'alert' and _qresult.get('alerts'):
                                st.markdown("**在庫変動アラート:**")
                                for _al in _qresult['alerts'][:15]:
                                    _asku = _al.get('sku', '')
                                    _asrc = _al.get('source', '')
                                    _aprev = _al.get('prev_status', '')
                                    _acur = _al.get('current_status', '')
                                    st.markdown(f"- `{_asku}` ({_asrc}): {_aprev} → **{_acur}**")

                            # Price optimization
                            if _qkey == 'price' and _qresult.get('opportunities'):
                                _opps = _qresult['opportunities']
                                _undercut = _opps.get('competitor_undercut', [])
                                _increase = _opps.get('price_increase_candidates', [])
                                if _undercut:
                                    st.markdown("**競合に負けている商品:**")
                                    for _uc in _undercut[:5]:
                                        st.markdown(f"- {_uc.get('title','')} | ${_uc.get('current_price',0):.2f} → ${_uc.get('suggested_price',0):.2f}")
                                if _increase:
                                    st.markdown("**値上げ余地のある商品:**")
                                    for _ic in _increase[:5]:
                                        st.markdown(f"- {_ic.get('title','')} | ${_ic.get('current_price',0):.2f} → ${_ic.get('suggested_price',0):.2f}")

                            # Sales tracking
                            if _qkey == 'sales' and _qresult.get('report'):
                                _rpt = _qresult['report']
                                _s7 = _rpt.get('summary_7d', {})
                                _s30 = _rpt.get('summary_30d', {})
                                if _s7 or _s30:
                                    st.markdown("**売上サマリー:**")
                                    st.markdown(f"- 7日間: {_s7.get('count',0)}件 / ${_s7.get('revenue_usd',0):,.2f}")
                                    st.markdown(f"- 30日間: {_s30.get('count',0)}件 / ${_s30.get('revenue_usd',0):,.2f}")

                            # Data sync
                            if _qkey == 'data_sync':
                                _sk = _qresult.get('sku_sync', {})
                                _iv = _qresult.get('inventory_sync', {})
                                _en = _qresult.get('enrichment_sync', {})
                                st.markdown(f"- SKU統合: {_sk.get('updated',0)}件更新")
                                st.markdown(f"- 在庫統合: {_iv.get('updated',0)}件更新")
                                st.markdown(f"- 物理データ: {_en.get('updated',0)}件更新")

                            # 全結果を折りたたみで表示
                            _show_raw_json = st.checkbox("生データ (JSON)", key=f"chk_raw_json_{_qkey}")
                            if _show_raw_json:
                                _display = {k: v for k, v in _qresult.items()
                                           if k not in ('emails',) or len(_qresult.get('emails', [])) <= 20}
                                st.json(_display)

                    # 組織ルーティング
                    try:
                        from company_router import route_all_results as _qroute
                        _rkey_map = {
                            'email': 'email', 'news': 'news',
                            # 'research' — W21 (2026-04-26) 削除済
                            'rival': 'rival_detection', 'alert': 'inventory_alert',
                            'supplier': 'supplier_select', 'data_sync': 'data_sync',
                            'price': 'price_optimization', 'sales': 'sales_tracking',
                        }
                        _qroute({_rkey_map.get(_qkey, _qkey): _qresult})
                    except Exception:
                        pass

                    # 実行ログ記録
                    try:
                        _qdetails = _qresult if isinstance(_qresult, dict) else {}
                        result_data = log_execution_result(
                            _qkey, "success", f"{_qdisplay} 完了",
                            details=_qdetails, execution_time_sec=_qelapsed)
                        save_execution_history(_qkey, result_data)
                    except Exception:
                        pass

                except Exception as _qe:
                    _qelapsed = time.time() - _qstart
                    _qstatus.update(label=f"{_qdisplay} — エラー", state="error")
                    st.error(f"エラー: {_qe}")
                    try:
                        log_execution_result(_qkey, "failed", str(_qe), execution_time_sec=_qelapsed)
                    except Exception:
                        pass

    st.divider()

    # ────────────────────────────────
    # 詳細実行セクション（既存の3タスク）
    # ────────────────────────────────
    st.markdown("### 詳細実行")

    # タスク選択
    task_choice = st.radio(
        "実行するタスク:",
        ["在庫チェック", "商品検索", "eBay同期"],
        horizontal=True
    )

    st.divider()

    # ============ 在庫チェック ============
    if task_choice == "在庫チェック":
        st.subheader("在庫チェック")
        st.info("監視中の全アイテムを一括チェック (httpx + Playwright batch、約 5-10 分)")

        # 実行前の確認
        items = get_active_items()
        col_info1, col_info2 = st.columns(2)
        with col_info1:
            st.metric("監視中のアイテム数", len(items))
        with col_info2:
            st.metric("前回チェック", "未実行" if not get_recent_logs(limit=1) else "チェック済み")

        if st.button("チェック実行", type="primary", width="stretch", key="btn_check"):
            start_time = time.time()
            checked_count = 0
            elapsed = 0.0

            if not items:
                msg = "監視中のアイテムがありません"
                log_execution_result("inventory_check", "failed", msg)
                st.warning(f"{msg}")
            else:
                # 進捗表示
                progress_container = st.container()
                status_container = st.container()

                with progress_container:
                    with st.status("チェック実行中...", expanded=True) as status:
                        st.write(f"▸ {len(items)}件のアイテムをチェック中...")

                        try:
                            # W50 統合 (2026-04-30): scheduler 経路と同じ
                            # tasks.task_inventory_check.run_inventory_check を呼ぶ.
                            # 入口は異なるが在庫監視本体は 1 つに集約.
                            import json as _json
                            from pathlib import Path as _Path
                            from tasks.task_inventory_check import run_inventory_check

                            _cfg_path = _Path(__file__).parent / "config" / "schedule_config.json"
                            try:
                                with open(_cfg_path, encoding="utf-8") as _f:
                                    _cfg = _json.load(_f)
                            except Exception as _ce:
                                st.warning(f"schedule_config.json 読込失敗 ({_ce}), 空 config で続行")
                                _cfg = {}

                            st.write("▸ 在庫チェック実行中...")
                            res = run_inventory_check(_cfg)
                            elapsed = time.time() - start_time

                            if not res.get("success"):
                                raise RuntimeError(res.get("error", "unknown error"))

                            checked_count = res.get("checked_count", 0)
                            stats = res.get("results", {})

                            # ログに記録
                            details = {
                                "total_items": checked_count,
                                "stats": stats,
                                "execution_time_sec": elapsed,
                            }
                            result_data = log_execution_result(
                                "inventory_check",
                                "success",
                                f"{checked_count}件のアイテムをチェック完了",
                                details=details,
                                execution_time_sec=elapsed
                            )
                            save_execution_history("inventory_check", result_data)

                            # Discord 通知
                            webhook_url = s.get("discord_webhook_url", "")
                            if webhook_url:
                                send_discord_notification(
                                    webhook_url,
                                    "inventory_check",
                                    "success",
                                    details
                                )

                            status.update(
                                label="チェック完了",
                                state="complete"
                            )

                        except Exception as e:
                            elapsed = time.time() - start_time
                            error_msg = str(e)

                            log_execution_result(
                                "inventory_check",
                                "failed",
                                f"エラー: {error_msg}",
                                execution_time_sec=elapsed
                            )

                            status.update(
                                label="チェック失敗",
                                state="error"
                            )

                            st.error(f"チェック失敗: {error_msg}")

                # 結果サマリー
                with status_container:
                    st.divider()
                    st.subheader("チェック結果")
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        st.metric("チェック完了数", checked_count)
                    with col2:
                        st.metric("実行時間", f"{elapsed:.1f}秒" if elapsed else "N/A")
                    with col3:
                        stats_recent = get_execution_statistics("inventory_check", days=7)
                        st.metric("成功率（7日）", f"{stats_recent.get('success_rate', 0):.1f}%")

    # ============ 商品検索 ============
    elif task_choice == "商品検索":
        st.subheader("商品検索")
        st.info("在庫切れが検知された商品の同等商品を検索タスクを準備します")

        if st.button("検索準備", type="primary", width="stretch", key="btn_search"):
            start_time = time.time()

            with st.status("検索準備中...", expanded=True) as status:
                try:
                    # task_product_search をインポート
                    sys.path.insert(0, str(Path(__file__).parent / "tasks"))
                    from task_product_search import run_product_search

                    st.write("▸ Searching out-of-stock items...")
                    result = run_product_search()

                    elapsed = time.time() - start_time

                    if result.get("success"):
                        prepared_count = result.get('prepared_count', 0)

                        # ログに記録
                        details = {
                            "prepared_count": prepared_count,
                            "message": result.get('message', ''),
                        }
                        result_data = log_execution_result(
                            "product_search",
                            "success",
                            f"{prepared_count}件の検索タスクを準備",
                            details=details,
                            execution_time_sec=elapsed
                        )
                        save_execution_history("product_search", result_data)

                        # Discord 通知
                        webhook_url = s.get("discord_webhook_url", "")
                        if webhook_url:
                            send_discord_notification(
                                webhook_url,
                                "product_search",
                                "success",
                                details
                            )

                        status.update(label="準備完了", state="complete")

                        st.success(f"検索準備完了: {prepared_count}件")
                        st.json(result)

                    else:
                        msg = result.get('message', 'Unknown error')
                        log_execution_result("product_search", "failed", msg, execution_time_sec=elapsed)
                        status.update(label="PREPARED (with warnings)", state="complete")
                        st.warning(f"{msg}")

                except Exception as e:
                    elapsed = time.time() - start_time
                    error_msg = str(e)

                    log_execution_result(
                        "product_search",
                        "failed",
                        f"エラー: {error_msg}",
                        execution_time_sec=elapsed
                    )

                    status.update(label="準備失敗", state="error")
                    st.error(f"エラー: {error_msg}")

            # 結果サマリー
            st.divider()
            st.subheader("検索統計")
            stats = get_execution_statistics("product_search", days=7)
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("今週の実行回数", stats.get('total_executions', 0))
            with col2:
                st.metric("成功数", stats.get('successful', 0))
            with col3:
                st.metric("成功率", f"{stats.get('success_rate', 0):.1f}%")

    # ============ eBay同期 ============
    elif task_choice == "eBay同期":
        st.subheader("eBay在庫同期")
        st.info("eBay API から現在の出品状況を同期します")

        # API認証情報確認
        app_id = s.get("ebay_app_id", "")
        dev_id = s.get("ebay_dev_id", "")
        cert_id = s.get("ebay_cert_id", "")
        user_token = s.get("ebay_user_token", "")

        if not all([app_id, dev_id, cert_id, user_token]):
            st.error("eBay API認証情報が未設定です")
            st.info("設定タブで以下を入力してください: App ID, Dev ID, Cert ID, User Token")
        else:
            if st.button("同期実行", type="primary", width="stretch", key="btn_ebay"):
                start_time = time.time()

                with st.status("同期実行中...", expanded=True) as status:
                    try:
                        st.write("▸ Connecting to eBay API...")
                        report = sync_listings_from_ebay(app_id, dev_id, cert_id, user_token)

                        st.write("▸ Auto-updating ranks...")
                        auto_rank_all_listings_in_db()

                        elapsed = time.time() - start_time

                        # ログに記録
                        details = {
                            "active_count": report.get("active_count", 0),
                            "ended_count": report.get("ended_count", 0),
                            "ranked_count": report.get("ranked_count", 0),
                        }
                        result_data = log_execution_result(
                            "ebay_sync",
                            "success",
                            "eBay 同期完了",
                            details=details,
                            execution_time_sec=elapsed
                        )
                        save_execution_history("ebay_sync", result_data)

                        # Discord 通知
                        webhook_url = s.get("discord_webhook_url", "")
                        if webhook_url:
                            send_discord_notification(
                                webhook_url,
                                "ebay_sync",
                                "success",
                                details
                            )

                        status.update(label="同期完了", state="complete")

                        st.success("同期完了")

                        # 結果表示
                        st.divider()
                        st.subheader("同期結果")
                        col1, col2, col3 = st.columns(3)
                        with col1:
                            st.metric("アクティブ出品", report.get("active_count", 0))
                        with col2:
                            st.metric("終了出品", report.get("ended_count", 0))
                        with col3:
                            st.metric("ランク更新", report.get("ranked_count", 0))

                        # 詳細
                        _show_sync_detail = st.checkbox("詳細情報", key="chk_sync_detail")
                        if _show_sync_detail:
                            st.json(report)

                    except Exception as e:
                        elapsed = time.time() - start_time
                        error_msg = str(e)

                        log_execution_result(
                            "ebay_sync",
                            "failed",
                            f"エラー: {error_msg}",
                            execution_time_sec=elapsed
                        )

                        status.update(label="同期失敗", state="error")
                        st.error(f"同期失敗: {error_msg}")

            # 同期統計
            st.divider()
            st.subheader("同期統計")
            stats = get_execution_statistics("ebay_sync", days=7)
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("今週の実行回数", stats.get('total_executions', 0))
            with col2:
                st.metric("成功数", stats.get('successful', 0))
            with col3:
                st.metric("成功率", f"{stats.get('success_rate', 0):.1f}%")


# ========== 仕入先候補タブ ==========
if _w134_sel == "仕入先候補":
    st.title("仕入先候補レビュー")
    st.caption(
        "Claude API が評価した仕入先候補を一覧。"
        "置き換え可能候補と別SKU出品機会を分けて表示します。"
    )

    # ── 最終実行日時 ──
    try:
        from datetime import datetime as _dt, timedelta as _td
        with get_conn() as _sup_conn:
            _sup_conn.row_factory = None
            _last_sup = _sup_conn.execute(
                "SELECT MAX(created_at) FROM supplier_candidates"
            ).fetchone()[0]
        if _last_sup:
            # "2026-04-23 02:44:24" 形式を parse
            try:
                _dt_obj = _dt.strptime(_last_sup, "%Y-%m-%d %H:%M:%S")
                _delta = _dt.now() - _dt_obj
                if _delta.total_seconds() < 3600:
                    _ago = f"{int(_delta.total_seconds()/60)} 分前"
                elif _delta.total_seconds() < 86400:
                    _ago = f"{int(_delta.total_seconds()/3600)} 時間前"
                else:
                    _ago = f"{int(_delta.total_seconds()/86400)} 日前"
                st.caption(f"**最終実行**: {_last_sup} ({_ago})")
            except Exception:
                st.caption(f"**最終実行**: {_last_sup}")
        else:
            st.caption("**最終実行**: データなし")
    except Exception as _e:
        pass  # logger 未定義なので silent fail

    # ── 閾値調整 (T6: Q5=C 手動ボタン、Q6=B タブ上部配置) ──
    import json as _th_json
    _th_settings_path = Path(__file__).parent / "settings.json"
    try:
        with open(_th_settings_path, encoding="utf-8") as _f:
            _th_settings = _th_json.load(_f)
    except Exception:
        _th_settings = {}
    _th_alt0 = int(_th_settings.get("supplier_alt0_score_threshold", 60))
    _th_alt1 = int(_th_settings.get("supplier_alt1_score_threshold", 20))

    with st.container(border=True):
        st.markdown(
            '<div style="font-family:var(--f-mono,monospace);font-size:11px;'
            'letter-spacing:2px;color:#d8cdb5;text-transform:uppercase;">'
            'T H R E S H O L D &nbsp; C O N T R O L</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "新規探索時のスコア下限。緩和 (低い) → 候補多く拾う / "
            "厳格 (高い) → 精度重視。変更後「再計算実行」で既存候補にも適用。"
        )
        _th_c1, _th_c2, _th_c3 = st.columns([1.2, 1.2, 1.6])
        with _th_c1:
            _new_alt0 = st.select_slider(
                "置換候補 (alt=0) の下限",
                options=[20, 30, 40, 50, 60, 70, 80, 90],
                value=_th_alt0,
                key="th_alt0_slider",
                help="同一商品判定の確からしさ (Claude 評価)。60 が標準。",
            )
        with _th_c2:
            _new_alt1 = st.select_slider(
                "別SKU出品機会 (alt=1) の下限",
                options=[0, 10, 20, 30, 40, 50, 60],
                value=_th_alt1,
                key="th_alt1_slider",
                help="別SKU機会のスコア下限。20 で score<20 のゴミを除外。",
            )
        with _th_c3:
            st.write("")
            _th_b1, _th_b2 = st.columns(2)
            with _th_b1:
                if st.button("設定保存", key="th_save", width="stretch"):
                    _th_settings["supplier_alt0_score_threshold"] = int(_new_alt0)
                    _th_settings["supplier_alt1_score_threshold"] = int(_new_alt1)
                    try:
                        with open(_th_settings_path, "w", encoding="utf-8") as _f:
                            _th_json.dump(_th_settings, _f, indent=2, ensure_ascii=False)
                        st.success(f"保存: alt0≥{_new_alt0} / alt1≥{_new_alt1}")
                        st.rerun()
                    except Exception as _e:
                        st.error(f"保存失敗: {_e}")
            with _th_b2:
                if st.button("再計算実行", key="th_recalc", type="primary", width="stretch",
                             help="既存 DB を新閾値で再判定。下限以下の候補を物理削除、"
                                  "残る候補も profit_jpy を最新計算で更新 (rejected/applied は保護)"):
                    try:
                        from scripts.recalc_supplier_candidate_profits import recalc_all
                        # 現在保存中の閾値を使って再計算 (ボタン押下前に保存推奨)
                        with st.status("既存候補を再計算中...", expanded=False) as _st_rc:
                            _res_rc = recalc_all(dry_run=False)
                            _st_rc.update(
                                label=f"完了: 削除 {_res_rc.get('price_none_deleted', 0) + _res_rc.get('now_unprofitable_deleted', 0)}件 / "
                                      f"利益更新 {_res_rc.get('still_profitable', 0)}件",
                                state="complete",
                            )
                        # 低 score の候補も削除 (alt0 閾値 + alt1 閾値に基づく)
                        with get_conn() as _c_rc:
                            _low_score_deleted = _c_rc.execute(
                                """DELETE FROM supplier_candidates
                                   WHERE status NOT IN ('rejected','applied')
                                     AND (
                                       (COALESCE(alt_listing_possible,0) = 0
                                        AND COALESCE(match_score, 0) < ?)
                                       OR
                                       (COALESCE(alt_listing_possible,0) = 1
                                        AND COALESCE(match_score, 0) < ?)
                                     )""",
                                (int(_new_alt0), int(_new_alt1)),
                            ).rowcount
                            _c_rc.commit()
                        st.success(
                            f"再計算完了。低スコア削除 {_low_score_deleted}件 + "
                            f"利益ベース削除 {_res_rc.get('price_none_deleted', 0) + _res_rc.get('now_unprofitable_deleted', 0)}件"
                        )
                    except Exception as _e:
                        st.error(f"再計算エラー: {_e}")

    # ── フィルタ ──
    _sup_f1, _sup_f2, _sup_f3 = st.columns([2, 2, 1])
    with _sup_f1:
        _sup_filter_status = st.selectbox(
            "ステータス",
            options=["pending", "accepted", "rejected", "applied", "すべて"],
            format_func=lambda x: _STATUS_JA.get(x, x),
            index=0,
            key="sup_filter_status",
        )
    with _sup_f2:
        _sup_filter_sku = st.text_input(
            "SKUで絞り込み（空欄で全件）",
            value="",
            key="sup_filter_sku",
        )
    with _sup_f3:
        st.write("")  # spacer
        if st.button("再読込", key="sup_reload"):
            st.rerun()

    _sup_all = get_supplier_candidates(
        sku=_sup_filter_sku or None,
        status=None if _sup_filter_status == "すべて" else _sup_filter_status,
    )

    # W115 v2 root fix (2026-05-10): 履歴 tab は status filter から独立 fetch.
    # 旧挙動: status filter default='pending' で _sup_all=pending only → _sup_history=[] (常に 0 件、UX 不能)
    # 新挙動: 履歴は sku filter のみ尊重して rejected+applied を独立 fetch.
    # status filter は actionable 3 tab (revive/replace/altlist) のみに適用.
    _sup_history_raw = (
        get_supplier_candidates(sku=_sup_filter_sku or None, status="rejected")
        + get_supplier_candidates(sku=_sup_filter_sku or None, status="applied")
    )

    # 親 listing の source_status + quantity_ebay + is_ended をまとめて取得 (N+1 回避)
    # 2026-04-24: qty_ebay も取得して「復活候補」(qty=0) と「置換候補」(qty≥1) を分離
    # W68 Iteration 2: SKU 集約 → ebay_item_id 集約に変更 (sku-rules.md 準拠).
    # 同 SKU 多 listing (有在庫プール 8 SKU で 107 listings) で dict[sku] が衝突する事故防止.
    # W115 v2 (2026-05-10): _sup_history_raw の eids も含めて parent metadata を完全化
    # (履歴行の「仕入先復活警告」表示を担保).
    _sup_parent_status: dict[str, str] = {}
    _sup_parent_qty: dict[str, int] = {}
    _sup_parent_ended: dict[str, int] = {}
    _sup_eids = list({
        r.get("ebay_item_id")
        for r in (_sup_all + _sup_history_raw)
        if r.get("ebay_item_id")
    })
    if _sup_eids:
        from monitor.database import get_conn as _sup_conn
        with _sup_conn() as _sup_cc:
            _ph = ",".join("?" * len(_sup_eids))
            for _srow in _sup_cc.execute(
                f"""SELECT ebay_item_id, source_status, is_ended, quantity_ebay
                    FROM ebay_listings WHERE ebay_item_id IN ({_ph})""",
                _sup_eids,
            ).fetchall():
                _ended = int(_srow["is_ended"] or 0)
                _sup_parent_status[_srow["ebay_item_id"]] = (
                    (_srow["source_status"] or "") if not _ended else "ended"
                )
                _sup_parent_qty[_srow["ebay_item_id"]] = int(_srow["quantity_ebay"] or 0)
                _sup_parent_ended[_srow["ebay_item_id"]] = _ended

    # 4 区分に分離 (2026-04-24 rev2):
    #   revive: eBay qty=0 + alt=0 + status IN (pending,accepted) → 復活候補 (actionable 最優先)
    #   replace: eBay qty≥1 + alt=0 + status IN (pending,accepted) → 置換候補 (actionable)
    #   altlist: alt=1 + status IN (pending,accepted) → 別SKU出品機会 (actionable)
    #   history: status IN (rejected,applied) → 履歴 (過去の判断、参照用)
    #
    # actionable = pending or accepted (まだ action が残っている)
    # history    = rejected (user 不採用済) or applied (既に反映済)
    #   ↳ これらを actionable 3 tab に混ぜると「URL 失効した古い候補」で誤解を招く
    _sup_revive: list[dict] = []
    _sup_replace: list[dict] = []
    _sup_altlist: list[dict] = []
    _sup_history: list[dict] = []
    # W115 v2 (2026-05-10): _sup_all の rejected/applied は skip (履歴は独立 fetch).
    # status filter='rejected'/'applied' 時に _sup_all と _sup_history_raw が重複しないよう
    # actionable loop は明示 status guard で history を除外.
    for _r in _sup_all:
        _qty_r = _sup_parent_qty.get(_r.get("ebay_item_id") or "", -1)
        _is_alt_r = bool(_r.get("alt_listing_possible"))
        _st_r = (_r.get("status") or "pending").lower()
        if _st_r in ("rejected", "applied"):
            continue  # 履歴は _sup_history_raw から独立 populate
        if _is_alt_r:
            _sup_altlist.append(_r)
        elif _qty_r == 0:
            _sup_revive.append(_r)
        else:
            _sup_replace.append(_r)

    # 履歴 populate (status filter 非依存、auto_rejected=1 はノイズ抑止 / FINDING 8 2026-05-05)
    for _r in _sup_history_raw:
        _st_r = (_r.get("status") or "").lower()
        if _st_r == "rejected" and int(_r.get("auto_rejected") or 0) == 1:
            continue
        _sup_history.append(_r)

    _actionable_total = len(_sup_revive) + len(_sup_replace) + len(_sup_altlist)
    st.markdown(
        f"**actionable**: {_actionable_total}件 "
        f"(復活 {len(_sup_revive)} / 置換 {len(_sup_replace)} / 別SKU {len(_sup_altlist)})　"
        f"**履歴 (rejected/applied)**: {len(_sup_history)}件 "
        f"<span style='color:rgba(180,220,255,0.5);font-size:11px;'>"
        f"※ 履歴は status filter 非依存</span>",
        unsafe_allow_html=True,
    )

    def _render_candidate_card(row: dict, context: str):
        """1候補の表示＋操作ボタン。context='replace' or 'altlist' で色分け。"""
        cid = row["id"]
        # W112 H-3 (2026-05-08 retrospective): 前回 click のメッセージを表示
        # (rerun 後に消えないよう session_state 経由で持ち越し).
        for _lvl, _msg in st.session_state.pop(f"_sup_msgs_{cid}", []):
            getattr(st, _lvl, st.info)(_msg)
        score = row.get("match_score") or 0
        platform = row.get("source_platform") or "?"
        price = row.get("candidate_price_jpy")
        title = row.get("candidate_title") or "(タイトル未取得)"
        url = row.get("candidate_url", "")
        reasoning = row.get("match_reasoning") or ""
        alt_note = row.get("alt_listing_note") or ""
        junk_flag = row.get("junk_likely_untested") or 0
        profitable = row.get("profitable") or 0
        status = row.get("status", "pending")
        # 2026-04-26: eBay 出品額 (USD + JPY 換算) 表示。user 要望対応.
        # ebay_item_id から ebay_listings.current_price を引いて利益判断と並列表示.
        _ebay_price_usd: Optional[float] = None
        _ebay_listing = get_ebay_listing_by_item_id(row.get("ebay_item_id"))
        if _ebay_listing:
            _ebay_price_usd = _ebay_listing.get("current_price")
        # 為替レートで JPY 換算 (settings の exchange_rate 使用、無ければ 150 fallback)
        try:
            _fx = float(s.get("exchange_rate") or 150)
        except (TypeError, ValueError):
            _fx = 150.0
        _ebay_price_jpy = (
            int(_ebay_price_usd * _fx) if _ebay_price_usd else None
        )
        _ebay_price_html = (
            f'<span style="color:#76ff03;font-weight:600;margin-right:8px;">'
            f'eBay出品 ${_ebay_price_usd:.2f}'
            + (f' (¥{_ebay_price_jpy:,})' if _ebay_price_jpy else '')
            + '</span>'
        ) if _ebay_price_usd else (
            '<span style="color:rgba(200,200,200,0.5);font-size:11px;margin-right:8px;">eBay出品: 未取得</span>'
        )
        # 2026-04-25: 評価モデル badge. Opus/Sonnet/Haiku を短縮表示.
        # 動画 KB を踏まえた深い判断は Opus, 廉価 bulk は Haiku の使い分けを可視化.
        _eval_model = (row.get("eval_model") or "")
        if "opus" in _eval_model.lower():
            _model_label = "Opus 4.7"
            _model_color = "rgba(196,128,255,0.95)"  # purple = 深い思考
            _model_bg = "rgba(140,80,200,0.18)"
        elif "sonnet" in _eval_model.lower():
            _model_label = "Sonnet 4.6"
            _model_color = "rgba(120,200,255,0.95)"
            _model_bg = "rgba(80,140,200,0.15)"
        elif "haiku" in _eval_model.lower():
            _model_label = "Haiku 4.5"
            _model_color = "rgba(180,220,200,0.85)"
            _model_bg = "rgba(100,140,120,0.15)"
        else:
            _model_label = ""
            _model_color = ""
            _model_bg = ""
        _model_html = (
            f'<span style="color:{_model_color};font-size:11px;font-weight:600;'
            f'background:{_model_bg};padding:1px 8px;border-radius:3px;'
            f'letter-spacing:0.3px;">{_model_label}</span>'
        ) if _model_label else ""

        # 利益額 + 利益率 (Interstellar amber 色) の inline 文字列を構築
        _profit_jpy = row.get("profit_jpy")
        _profit_html = ""
        if _profit_jpy is not None and price and price > 0:
            _rate = (_profit_jpy / price) * 100
            _profit_html = (
                f'<span style="color:#ffa84a;font-weight:600;margin-left:8px;">'
                f'利益 +¥{int(_profit_jpy):,} ({_rate:.0f}%)</span>'
            )
        elif row.get("alt_listing_possible"):
            _profit_html = (
                '<span style="color:#a89d8a;font-size:11px;margin-left:8px;">'
                '利益: (別SKU出品機会・計算対象外)</span>'
            )

        badge_color = "rgba(118,255,3,0.8)" if profitable else "rgba(255,160,64,0.8)"
        score_color = (
            "rgba(118,255,3,0.9)" if score >= 80
            else "rgba(240,200,48,0.9)" if score >= 60
            else "rgba(255,128,128,0.9)"
        )

        # カード内に判定理由・別出品提案・ジャンク警告を全て内包（視覚的統一）
        reasoning_html = (
            f'<div style="margin-top:8px;padding-top:6px;border-top:1px dashed rgba(120,180,255,0.2);'
            f'font-size:11px;color:rgba(200,220,255,0.7);">判定: {reasoning}</div>'
        ) if reasoning else ""
        alt_html = (
            f'<div style="margin-top:4px;font-size:11px;color:rgba(180,255,200,0.75);">'
            f'別出品提案: {alt_note}</div>'
        ) if alt_note else ""
        junk_html = (
            f'<div style="margin-top:4px;font-size:11px;color:rgba(255,200,120,0.85);">'
            f'注意: 「動作未確認ジャンク」の可能性あり（仕入先は動作確認していないだけの可能性）</div>'
        ) if junk_flag else ""

        # 仕入先復活警告: 親 listing の source_status='在庫有' なら警告表示
        # (accepted 候補の場合は特に注意喚起。ユーザー手動判断を促す)
        _parent_ss = _sup_parent_status.get(row.get("ebay_item_id") or "", "")
        _recovered_html = ""
        if _parent_ss == "在庫有":
            _rec_msg = (
                "仕入先が在庫有に復活しています — この候補は不要の可能性。"
                + ("採用済ですが反映前に「不採用」に戻すことを推奨。" if status == "accepted" else "")
            )
            _recovered_html = (
                f'<div style="margin-top:6px;padding:6px 10px;'
                f'background:rgba(240,180,48,0.08);border-left:3px solid rgba(240,180,48,0.85);'
                f'font-size:11px;color:rgba(255,220,120,0.95);">'
                f'仕入先復活: {_rec_msg}</div>'
            )

        _ebay_iid = row.get("ebay_item_id") or ""
        # クリック1回で全選択→Ctrl+C でコピーできるよう user-select:all を指定
        _ebay_id_html = (
            f'<span style="color:rgba(180,220,255,0.8);font-size:11px;">ItemID: '
            f'<code style="background:rgba(120,200,255,0.12);padding:1px 6px;'
            f'border-radius:3px;color:rgba(200,230,255,0.95);'
            f'user-select:all;cursor:text;">{_ebay_iid}</code></span>'
        ) if _ebay_iid else (
            '<span style="color:rgba(200,200,200,0.5);font-size:11px;">ItemID: -</span>'
        )

        st.markdown(
            f'<div style="border:1px solid rgba(120,180,255,0.3);'
            f'border-radius:6px;padding:10px 14px;margin:6px 0;'
            f'background:rgba(20,30,50,0.4);">'
            f'<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;'
            f'font-family:Share Tech Mono,monospace;">'
            f'<span style="color:{score_color};font-size:20px;font-weight:700;">{score}</span>'
            f'<span style="color:rgba(180,220,255,0.8);font-size:12px;">SKU: {row.get("sku","?")}</span>'
            f'{_ebay_id_html}'
            f'<span style="color:rgba(180,220,255,0.6);font-size:11px;">{platform}</span>'
            f'<span style="color:{badge_color};font-size:11px;">'
            f'{"採算OK" if profitable else "採算注意"}</span>'
            f'<span style="color:rgba(200,200,200,0.6);font-size:11px;">'
            f'状態: {_STATUS_JA.get(status, status)}</span>'
            f'{_model_html}'
            f'</div>'
            f'<div style="margin-top:6px;font-size:13px;color:rgba(255,255,255,0.9);">{title}</div>'
            f'<div style="margin-top:4px;font-size:12px;color:#d8cdb5;">'
            f'{_ebay_price_html}'
            f'<span>仕入 ¥{f"{price:,}" if price else "?"}</span>'
            f'{_profit_html}'
            f'　<a href="{url}" target="_blank" style="color:rgba(120,200,255,0.9);">商品ページを開く</a>'
            f'</div>'
            f'{reasoning_html}{alt_html}{junk_html}{_recovered_html}'
            f'</div>',
            unsafe_allow_html=True,
        )

        # 履歴タブは参照専用 (操作ボタン非表示).
        # ただし W115 (2026-05-10): status='applied' は「📷 写真反映」 button のみ例外的に表示.
        # 経緯: W112 (5/8) 1-click 化で status は pending→applied 直行、accepted は遷移しない.
        # 案 A 別 button 設計を維持しつつ、applied 後のリトロアクティブ操作 path を提供.
        if context == "history":
            if status == "applied":
                _photo_key = f"history_photo_open_{cid}"
                if st.button(
                    "📷 写真反映",
                    key=f"history_btn_photo_{cid}",
                    help=(
                        "仕入先画像から Photoroom + Gemini で hero 合成 → "
                        "EPS upload → ReviseItem PictureDetails で eBay 反映"
                    ),
                ):
                    st.session_state[_photo_key] = (
                        not st.session_state.get(_photo_key, False)
                    )
                if st.session_state.get(_photo_key, False):
                    # 2026-05-20 Codex HIGH: 採用直後 prompt 経由の inline section
                    # (画面上部) で同 cid が既に開いている場合、ここで再 render すると
                    # `render_supplier_photo_apply_section` 内の widget key
                    # (sup_*_{cid}) が重複し Streamlit duplicate-key エラーで
                    # 画面破綻。inline が開いている時は history 側を skip + 注意表示。
                    if st.session_state.get(f"_sup_photo_open_inline_{cid}"):
                        st.caption(
                            "⚠️ この候補は採用直後の写真反映 section (画面上部) で"
                            "既に開いています。そちらで操作してください。"
                        )
                    else:
                        from tabs._supplier_photo_pipeline import (
                            render_supplier_photo_apply_section,
                        )
                        render_supplier_photo_apply_section(
                            candidate_id=cid,
                            candidate_url=url,
                            ebay_item_id=row.get("ebay_item_id") or "",
                            candidate_title=title,
                        )
            return

        # 2026-05-08 W112 (UX 1-click 化) + 2026-05-09 W112 retrospective fix (H-1〜H-5):
        # 採用ボタン = accept + apply (eBay ReviseItem) + qty 復元 (revive のみ) 一気通貫.
        #   H-1: bare except → 限定例外 + logger.exception で痕跡保存 (Q0 silent skip 防止)
        #   H-2: st.rerun() 後に明示 return (Streamlit 仕様変更時の防御)
        #   H-3: メッセージは session_state 経由で rerun 越しに表示 (rerun で消失する UX 退化防止)
        #   H-5: session_state lock で重複 click 防止
        # alt_listing のみ (score<60 + alt=1) は反映不可なので採用ボタンも disabled.
        alt_only = (score < 60) and bool(row.get("alt_listing_possible"))
        if alt_only:
            st.caption("別SKU出品機会のため現 listing には反映不可（別途新規出品フロー）")

        _lock_key = f"_sup_lock_{cid}"
        _processing = st.session_state.get(_lock_key, False)

        _btn_cols = st.columns(2)
        with _btn_cols[0]:
            if _processing:
                st.caption("⏳ 処理中... (二度押し防止)")
            elif st.button(
                "採用", key=f"sup_accept_{context}_{cid}",
                type="primary", disabled=alt_only,
            ):
                st.session_state[_lock_key] = True
                _msgs: list[tuple[str, str]] = []
                _eid = row.get("ebay_item_id") or ""
                try:
                    # 1) accept (status='accepted')
                    res_a = accept_supplier_candidate(cid)
                    if not res_a.get("success"):
                        logger.error(
                            "supplier accept failed cid=%s eid=%s msg=%s",
                            cid, _eid, res_a.get("message"),
                        )
                        _msgs.append(("error", res_a.get("message") or "採用に失敗しました"))
                    else:
                        # 2) apply (eBay ReviseItem + DB sku 追従 + status='applied')
                        _cfg_path = Path(__file__).parent / "config" / "schedule_config.json"
                        _cfg = {}
                        _cfg_load_ok = True
                        if _cfg_path.exists():
                            try:
                                _cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
                            except (OSError, json.JSONDecodeError) as _e:
                                logger.exception(
                                    "schedule_config.json 読込失敗 cid=%s", cid,
                                )
                                _msgs.append(("error", f"schedule_config.json 読込失敗: {_e}"))
                                _cfg_load_ok = False
                        if _cfg_load_ok:
                            res_b = apply_supplier_candidate(cid, _cfg)
                            if not res_b.get("success"):
                                logger.error(
                                    "supplier apply failed cid=%s eid=%s msg=%s",
                                    cid, _eid, res_b.get("message"),
                                )
                                _msgs.append((
                                    "error",
                                    f"eBay 反映失敗: {res_b.get('message') or 'apply エラー'}",
                                ))
                            else:
                                _msgs.append((
                                    "success",
                                    res_b.get("message") or "採用→eBay 反映 成功",
                                ))
                                # W115 整合性 fix (2026-05-20 user 緊急要望):
                                # SKU 反映成功後、写真も反映する？」確認 → yes なら
                                # 個別出品と同じプレート選択フロー (3 候補から user 選択)。
                                # 採用後 candidate は status='applied' になり履歴タブに
                                # 移動するため、セクション最上部 (タブ非依存) に prompt
                                # を出す必要あり。url/eid/title を session_state に保持
                                # して、次 rerun でその情報を使って prompt + photo
                                # apply section を描画する。
                                st.session_state[f"_sup_photo_prompt_{cid}"] = True
                                # W148-X (2026-05-20 user 緊急要望): description
                                # 反映ボタンを追加 (個別出品同等 Claude 生成 pipeline)。
                                # 写真 prompt と同 cid で並行設置、user が独立に決定可。
                                st.session_state[f"_sup_desc_prompt_{cid}"] = True
                                st.session_state[f"_sup_photo_meta_{cid}"] = {
                                    "url": url,
                                    "eid": _eid,
                                    "title": title,
                                }
                                # 3) 復活候補のみ qty 0→1 自動復元
                                if context == "revive":
                                    if not _eid:
                                        logger.error(
                                            "qty restore: ebay_item_id missing cid=%s", cid,
                                        )
                                        _msgs.append((
                                            "error",
                                            "ebay_item_id 不明のため qty 復元不可。"
                                            "手動で在庫を 1 に戻してください。",
                                        ))
                                    else:
                                        _ebay_creds = get_ebay_credentials(_cfg)
                                        if not ebay_credentials_ok(_ebay_creds):
                                            logger.error(
                                                "qty restore: eBay credentials missing cid=%s eid=%s",
                                                cid, _eid,
                                            )
                                            _msgs.append((
                                                "error",
                                                f"{_eid}: eBay 認証不足のため qty 復元失敗 "
                                                f"(SKU は書換済、手動で在庫を 1 に戻してください、cid={cid})",
                                            ))
                                        else:
                                            try:
                                                _qres_r = revise_inventory_quantity(
                                                    _eid, 1, **_ebay_creds,
                                                )
                                            except (RuntimeError, ConnectionError, TimeoutError, OSError) as _qe:
                                                logger.exception(
                                                    "qty restore exception cid=%s eid=%s",
                                                    cid, _eid,
                                                )
                                                _msgs.append((
                                                    "error",
                                                    f"{_eid}: SKU 書換成功だが qty 復元中に例外 "
                                                    f"({_qe}). 手動で在庫を 1 に戻してください (cid={cid}).",
                                                ))
                                            else:
                                                if _qres_r.get("success"):
                                                    update_ebay_listing_quantity(_eid, 1)
                                                    bump_db_version()  # W134 Step2: 在庫復元後 read-cache 無効化
                                                    _msgs.append((
                                                        "success",
                                                        f"{_eid}: 在庫 0 → 1 自動復元 (復活完了)",
                                                    ))
                                                else:
                                                    logger.error(
                                                        "qty restore api_failed cid=%s eid=%s msg=%s",
                                                        cid, _eid, _qres_r.get("message"),
                                                    )
                                                    _msgs.append((
                                                        "error",
                                                        f"{_eid}: SKU 書換成功だが qty 復元失敗 "
                                                        f"({_qres_r.get('message', '')}). "
                                                        f"手動で在庫を 1 に戻してください (cid={cid}).",
                                                    ))
                except Exception:  # noqa: BLE001 — H-A: 想定外例外も logger + UI msg で必ず痕跡残す
                    logger.exception(
                        "supplier accept/apply 想定外例外 cid=%s eid=%s", cid, _eid,
                    )
                    _msgs.append((
                        "error",
                        f"想定外エラーが発生しました (cid={cid}). "
                        "詳細はログ確認、手動で在庫/SKU を確認してください。",
                    ))
                finally:
                    st.session_state[_lock_key] = False
                    st.session_state[f"_sup_msgs_{cid}"] = _msgs
                st.rerun()
                return  # H-2: defensive early return
        with _btn_cols[1]:
            if st.button("不採用", key=f"sup_reject_{context}_{cid}"):
                # 不採用の DB 更新も例外吸収せず痕跡残す (Surface B 対称性 / Q0 silent skip 防止)
                try:
                    update_supplier_candidate_status(cid, "rejected")
                except Exception:  # noqa: BLE001
                    logger.exception("supplier reject failed cid=%s", cid)
                    st.session_state[f"_sup_msgs_{cid}"] = [
                        ("error", f"不採用記録に失敗しました (cid={cid}). 詳細はログ確認."),
                    ]
                st.rerun()
                return  # H-2

    # ── 2026-05-20 user 緊急要望: 採用後の写真反映 prompt (タブ非依存) ──
    # 採用 button (W112 1-click) が status='applied' に遷移させると候補は
    # 履歴タブに移動するため、user は元のタブを見ていて写真反映ボタンに
    # 気付かない (= 「採用して終わり」になる)。セクション最上部に prompt
    # を出してから、はい押下で個別出品同様のプレート選択フローを inline
    # 展開する (履歴タブへ移動不要)。
    _photo_prompts = [
        (int(k.replace("_sup_photo_prompt_", "")),
         st.session_state.get(f"_sup_photo_meta_{k.replace('_sup_photo_prompt_', '')}") or {})
        for k in list(st.session_state.keys())
        if k.startswith("_sup_photo_prompt_") and st.session_state.get(k)
    ]
    _photo_opens = [
        (int(k.replace("_sup_photo_open_inline_", "")),
         st.session_state.get(f"_sup_photo_meta_{k.replace('_sup_photo_open_inline_', '')}") or {})
        for k in list(st.session_state.keys())
        if k.startswith("_sup_photo_open_inline_") and st.session_state.get(k)
    ]
    if _photo_prompts or _photo_opens:
        with st.container(border=True):
            st.markdown(
                '<div style="font-size:11px;color:rgba(255,180,80,0.85);'
                'letter-spacing:2px;margin:0 0 8px;">'
                '写 真 反 映 　 — 　 採 用 直 後 確 認</div>',
                unsafe_allow_html=True,
            )
            # Step 1: prompt (採用押下直後、まだ「はい/いいえ」未選択)
            for _pcid, _pmeta in _photo_prompts:
                _ttl = (_pmeta.get("title") or "")[:60]
                _eid_p = _pmeta.get("eid") or ""
                st.warning(
                    f"📷 採用しました ({_ttl} / item {_eid_p})。"
                    f"仕入先の画像で写真も反映しますか？ "
                    f"(個別出品と同じ Photoroom + Gemini 3 候補プレートから選択)"
                )
                _pc = st.columns([1, 1, 5])
                with _pc[0]:
                    if st.button(
                        "📷 はい、写真も選ぶ",
                        key=f"_sup_photo_yes_{_pcid}", type="primary",
                    ):
                        st.session_state[f"_sup_photo_open_inline_{_pcid}"] = True
                        st.session_state[f"_sup_photo_prompt_{_pcid}"] = False
                        st.rerun()
                with _pc[1]:
                    if st.button(
                        "いいえ、後でやる",
                        key=f"_sup_photo_no_{_pcid}",
                    ):
                        st.session_state[f"_sup_photo_prompt_{_pcid}"] = False
                        # meta は履歴タブ「📷 写真反映」ボタン再操作用に残す
                        st.rerun()
            # Step 2: opened (はい押下後 → photo apply section を inline 展開)
            for _ocid, _ometa in _photo_opens:
                _ttl_o = _ometa.get("title") or ""
                _eid_o = _ometa.get("eid") or ""
                _url_o = _ometa.get("url") or ""
                if not _url_o:
                    st.error(
                        f"cid={_ocid}: URL 情報不足 → 履歴タブの「📷 写真反映」"
                        f"から操作してください"
                    )
                    continue
                st.markdown(
                    f"**▼ 写真反映: {_ttl_o[:60]} (item {_eid_o})**"
                )
                from tabs._supplier_photo_pipeline import (
                    render_supplier_photo_apply_section,
                )
                render_supplier_photo_apply_section(
                    candidate_id=_ocid,
                    candidate_url=_url_o,
                    ebay_item_id=_eid_o,
                    candidate_title=_ttl_o,
                )
                # 完了/中断時の「✖ 閉じる」ボタン
                if st.button(
                    "✖ この写真反映を閉じる",
                    key=f"_sup_photo_close_{_ocid}",
                ):
                    st.session_state[f"_sup_photo_open_inline_{_ocid}"] = False
                    st.rerun()
        st.markdown("---")

    # ── W148-X (2026-05-20 user 緊急要望): description 反映 prompt + section ──
    # 個別出品同等の Claude description 生成 pipeline を採用直後の inline で
    # 走らせる。写真 prompt と独立 (user は両方/片方/どちらでもなしを選べる)。
    _desc_prompts = [
        (int(k.replace("_sup_desc_prompt_", "")),
         st.session_state.get(f"_sup_photo_meta_{k.replace('_sup_desc_prompt_', '')}") or {})
        for k in list(st.session_state.keys())
        if k.startswith("_sup_desc_prompt_") and st.session_state.get(k)
    ]
    _desc_opens = [
        (int(k.replace("_sup_desc_open_inline_", "")),
         st.session_state.get(f"_sup_photo_meta_{k.replace('_sup_desc_open_inline_', '')}") or {})
        for k in list(st.session_state.keys())
        if k.startswith("_sup_desc_open_inline_") and st.session_state.get(k)
    ]
    if _desc_prompts or _desc_opens:
        with st.container(border=True):
            st.markdown(
                '<div style="font-size:11px;color:rgba(180,255,200,0.85);'
                'letter-spacing:2px;margin:0 0 8px;">'
                'description 反 映 　 — 　 採 用 直 後 確 認</div>',
                unsafe_allow_html=True,
            )
            for _dpcid, _dpmeta in _desc_prompts:
                _ttl_d = (_dpmeta.get("title") or "")[:60]
                _eid_d = _dpmeta.get("eid") or ""
                st.warning(
                    f"📝 採用しました ({_ttl_d} / item {_eid_d})。"
                    f"仕入先 URL から description (HTML 本文) も生成して反映しますか？ "
                    f"(個別出品と同じ Claude パイプライン、~30-60 秒)"
                )
                _dpc = st.columns([1.6, 1.4, 5])
                with _dpc[0]:
                    if st.button(
                        "📝 はい、description も生成",
                        key=f"_sup_desc_yes_{_dpcid}", type="primary",
                    ):
                        st.session_state[f"_sup_desc_open_inline_{_dpcid}"] = True
                        st.session_state[f"_sup_desc_prompt_{_dpcid}"] = False
                        st.rerun()
                with _dpc[1]:
                    if st.button(
                        "いいえ、後でやる",
                        key=f"_sup_desc_no_{_dpcid}",
                    ):
                        st.session_state[f"_sup_desc_prompt_{_dpcid}"] = False
                        st.rerun()
            for _docid, _dometa in _desc_opens:
                _ttl_do = _dometa.get("title") or ""
                _eid_do = _dometa.get("eid") or ""
                _url_do = _dometa.get("url") or ""
                if not _url_do:
                    st.error(
                        f"cid={_docid}: URL 情報不足 → 採用やり直しで再 prompt 発生"
                    )
                    continue
                st.markdown(
                    f"**▼ description 反映: {_ttl_do[:60]} (item {_eid_do})**"
                )
                from tabs._supplier_description_pipeline import (
                    render_supplier_description_section,
                )
                render_supplier_description_section(
                    candidate_id=_docid,
                    candidate_url=_url_do,
                    ebay_item_id=_eid_do,
                    candidate_title=_ttl_do,
                )
                if st.button(
                    "✖ この description 反映を閉じる",
                    key=f"_sup_desc_close_{_docid}",
                ):
                    st.session_state[f"_sup_desc_open_inline_{_docid}"] = False
                    st.rerun()
        st.markdown("---")

    # ── サブタブ 4 分割 (2026-04-24 rev2) ──
    # 復活候補 = 最優先: eBay 在庫0 商品に仕入先が見つかった → 採用で qty 自動復元
    # 置換候補 = eBay 在庫≥1 商品の SKU 書換
    # 別SKU出品機会 = alt=1 (新規出品検討)
    # 履歴 = rejected / applied (過去判断の参照用)
    _tab_revive, _tab_replace, _tab_altlist, _tab_history = st.tabs([
        f"復活候補 ({len(_sup_revive)})",
        f"置換候補 ({len(_sup_replace)})",
        f"別SKU出品機会 ({len(_sup_altlist)})",
        f"履歴 ({len(_sup_history)})",
    ])

    with _tab_revive:
        st.markdown(
            '<div style="font-family:var(--f-mono,monospace);font-size:11px;'
            'letter-spacing:2px;color:#ffa84a;text-transform:uppercase;">'
            '復 活 候 補 キ ュ ー</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "**eBay 在庫 0 商品**に対して仕入先が見つかった候補。"
            "採用すると SKU が新仕入先 URL ベースに書き換わり、**eBay 在庫も自動で 1 に復元** されます。"
            " (Q3=A, 採用→revise_item_sku+revise_inventory_quantity(1) 連続実行)"
        )
        if not _sup_revive:
            st.info(
                "復活候補はありません。eBay 在庫 0 の商品に新仕入先が見つかればここに表示されます。"
            )
        else:
            for _row in _sup_revive[:30]:
                _render_candidate_card(_row, context="revive")
            if len(_sup_revive) > 30:
                st.caption(f"... ほか {len(_sup_revive) - 30}件")

    with _tab_replace:
        st.markdown(
            '<div style="font-family:var(--f-mono,monospace);font-size:11px;'
            'letter-spacing:2px;color:#d8cdb5;text-transform:uppercase;">'
            '置 換 候 補 キ ュ ー</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "eBay 在庫≥1 の商品で、Claude が同一商品 (または置換可能) と判定した仕入先候補。"
            " 採用で SKU が新仕入先 URL ベースに書き換わります (在庫は既にあるので qty 復元なし)。"
        )
        if not _sup_replace:
            if not _sup_all:
                st.info(
                    "候補がまだ生成されていません。"
                    "在庫切れSKUが検出されると Pattern 1（即時探索）"
                    "または朝バッチ Pattern 2（一括探索）で自動生成されます。"
                    "「手動実行」タブから task_inventory_check → task_supplier_candidate_search の順で実行可能です。"
                )
            else:
                st.info(
                    "actionable な置換候補はありません (rejected/applied は履歴タブへ移動しました)。"
                )
        else:
            for _row in _sup_replace[:30]:
                _render_candidate_card(_row, context="replace")
            if len(_sup_replace) > 30:
                st.caption(f"... ほか {len(_sup_replace) - 30}件（SKUで絞り込んでください）")

    with _tab_altlist:
        st.markdown(
            '<div style="font-family:var(--f-mono,monospace);font-size:11px;'
            'letter-spacing:2px;color:#8b7355;text-transform:uppercase;">'
            '別 S K U 出 品 機 会</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "既存 SKU とは別物だが「別 SKU で新規出品する価値あり」と Claude が判定した商材。"
            " 現 listing の置換には使えない。新規出品フロー (個別出品タブ) で活用してください。"
        )
        if not _sup_altlist:
            st.info("別SKU出品機会は現在の条件では見つかりません。")
        else:
            for _row in _sup_altlist[:30]:
                _render_candidate_card(_row, context="altlist")
            if len(_sup_altlist) > 30:
                st.caption(f"... ほか {len(_sup_altlist) - 30}件")

    with _tab_history:
        st.markdown(
            '<div style="font-family:var(--f-mono,monospace);font-size:11px;'
            'letter-spacing:2px;color:#6b6b6b;text-transform:uppercase;">'
            '履 歴 （不 採 用 / 反 映 済）</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "過去の判断ログ (rejected = 不採用にしたもの / applied = 既に反映済)。"
            " URL は時間経過で失効している場合があります。"
            " 参照専用、アクションは他サブタブで実施してください。"
        )
        if not _sup_history:
            st.info("履歴はまだありません。")
        else:
            # rejected と applied を分けて表示
            _rej = [r for r in _sup_history if (r.get("status") or "").lower() == "rejected"]
            _app = [r for r in _sup_history if (r.get("status") or "").lower() == "applied"]
            if _rej:
                st.markdown(f"#### 不採用 ({len(_rej)}件)")
                for _row in _rej[:20]:
                    _render_candidate_card(_row, context="history")
                if len(_rej) > 20:
                    st.caption(f"... ほか {len(_rej) - 20}件")
            if _app:
                st.markdown(f"#### 反映済 ({len(_app)}件)")
                for _row in _app[:20]:
                    _render_candidate_card(_row, context="history")
                if len(_app) > 20:
                    st.caption(f"... ほか {len(_app) - 20}件")


# ========== モデル比較タブ (W86 / 2026-05-01) ==========
# Opus 4.7 vs Sonnet 4.6 supplier evaluation A/B test 結果の並列比較.
# 元データ: supplier_ab_test_runs テーブル.
if _w134_sel == "モデル比較":
    st.markdown("# モデル比較 (Supplier A/B Test)")
    st.caption(
        "Opus 4.7 vs Sonnet 4.6 で同じ scrape 結果を独立評価し、判定と精度を比較。"
        "past_judgments / knowledge は両モデルとも bypass (test_mode=True)。"
    )

    from monitor.database import get_conn as _get_conn

    with _get_conn() as _conn:
        _runs = _conn.execute(
            "SELECT run_id, MIN(created_at) AS started, COUNT(*) AS rows "
            "FROM supplier_ab_test_runs GROUP BY run_id ORDER BY started DESC"
        ).fetchall()

    if not _runs:
        st.info(
            "A/B test 結果なし。"
            "`python scripts/run_supplier_ab_test_2026_05_01.py` を実行してください。"
        )
    else:
        _run_options = [r["run_id"] for r in _runs]
        _selected_run = st.selectbox(
            "Run ID を選択",
            _run_options,
            format_func=lambda x: f"{x}  ({next((r['started'] for r in _runs if r['run_id']==x), '?')}, "
                                  f"{next((r['rows'] for r in _runs if r['run_id']==x), 0)} rows)",
        )

        # === Summary metrics ===
        with _get_conn() as _conn:
            _summary = _conn.execute(
                "SELECT model, COUNT(*) AS calls, "
                "  SUM(CASE WHEN cache_read_tokens > 0 THEN 1 ELSE 0 END) AS cache_hits, "
                "  SUM(cost_usd) AS total_cost, AVG(cost_usd) AS avg_cost, "
                "  AVG(duration_ms) AS avg_dur, "
                "  AVG(match_score) AS avg_score "
                "FROM supplier_ab_test_runs WHERE run_id=? GROUP BY model",
                (_selected_run,),
            ).fetchall()
            _listing_count = _conn.execute(
                "SELECT COUNT(DISTINCT ebay_item_id) FROM supplier_ab_test_runs WHERE run_id=?",
                (_selected_run,),
            ).fetchone()[0]
            _cand_count = _conn.execute(
                "SELECT COUNT(DISTINCT ebay_item_id || '_' || candidate_index) "
                "FROM supplier_ab_test_runs WHERE run_id=?",
                (_selected_run,),
            ).fetchone()[0]

        st.markdown("## サマリ")
        st.caption(
            f"対象 listings: {_listing_count} 件 / 候補総数: {_cand_count} 件 / "
            f"評価合計: {sum(s['calls'] for s in _summary)} 件 (= 候補数 × 2 model)"
        )
        _summary_rows = []
        for _s in _summary:
            _model_label = (
                "Opus 4.7" if "opus" in _s["model"].lower()
                else "Sonnet 4.6" if "sonnet" in _s["model"].lower()
                else _s["model"]
            )
            _hit_rate = (_s["cache_hits"] / _s["calls"] * 100) if _s["calls"] else 0
            _summary_rows.append({
                "Model": _model_label,
                "calls": _s["calls"],
                "cache hit": f"{_s['cache_hits']}/{_s['calls']} ({_hit_rate:.0f}%)",
                "total cost": f"${_s['total_cost']:.4f}",
                "avg / call": f"${_s['avg_cost']:.5f}",
                "avg duration": f"{_s['avg_dur']/1000:.1f}s" if _s['avg_dur'] else "—",
                "avg score": f"{_s['avg_score']:.1f}" if _s['avg_score'] is not None else "—",
            })
        st.table(_summary_rows)

        # === 判定一致率 ===
        with _get_conn() as _conn:
            _agreement = _conn.execute(
                """
                SELECT
                  COUNT(*) AS total_pairs,
                  SUM(CASE WHEN ABS(opus_score - sonnet_score) <= 10 THEN 1 ELSE 0 END) AS within_10,
                  SUM(CASE WHEN ABS(opus_score - sonnet_score) <= 20 THEN 1 ELSE 0 END) AS within_20,
                  AVG(ABS(opus_score - sonnet_score)) AS avg_diff,
                  SUM(CASE
                    WHEN (opus_score >= 60) = (sonnet_score >= 60) THEN 1 ELSE 0
                  END) AS verdict_agree
                FROM (
                  SELECT
                    ebay_item_id, candidate_index,
                    MAX(CASE WHEN model='claude-opus-4-7' THEN match_score END) AS opus_score,
                    MAX(CASE WHEN model='claude-sonnet-4-6' THEN match_score END) AS sonnet_score
                  FROM supplier_ab_test_runs
                  WHERE run_id=?
                  GROUP BY ebay_item_id, candidate_index
                  HAVING opus_score IS NOT NULL AND sonnet_score IS NOT NULL
                )
                """,
                (_selected_run,),
            ).fetchone()

        st.markdown("## 判定一致統計")
        if _agreement and _agreement["total_pairs"]:
            _t = _agreement["total_pairs"]
            _v = _agreement["verdict_agree"] or 0
            _w10 = _agreement["within_10"] or 0
            _w20 = _agreement["within_20"] or 0
            _avg_d = _agreement["avg_diff"] or 0
            st.markdown(
                f"- 候補ペア総数: **{_t}**\n"
                f"- score 差 ±10 以内: **{_w10}/{_t} ({_w10/_t*100:.0f}%)**\n"
                f"- score 差 ±20 以内: **{_w20}/{_t} ({_w20/_t*100:.0f}%)**\n"
                f"- 平均 score 差: **{_avg_d:.1f}** 点\n"
                f"- 判定一致 (採用60+/不採用60-): **{_v}/{_t} ({_v/_t*100:.0f}%)**"
            )
        else:
            st.caption("候補ペアなし。")

        # === Per-listing 並列カード ===
        st.markdown("## 商品別 比較カード")
        with _get_conn() as _conn:
            _listings = _conn.execute(
                "SELECT DISTINCT ebay_item_id, ebay_title, ebay_sku "
                "FROM supplier_ab_test_runs WHERE run_id=? "
                "ORDER BY ebay_sku",
                (_selected_run,),
            ).fetchall()

        for _li in _listings:
            st.markdown("---")
            st.markdown(f"### {_li['ebay_title']}")
            st.caption(f"ItemID: `{_li['ebay_item_id']}` / SKU: `{_li['ebay_sku']}`")

            with _get_conn() as _conn:
                _cands = _conn.execute(
                    "SELECT candidate_index, candidate_title, candidate_url, "
                    "  candidate_price_jpy, candidate_platform, "
                    "  model, match_score, reasoning, alt_listing_possible, "
                    "  junk_likely_untested, alt_listing_note, "
                    "  cost_usd, cache_read_tokens, duration_ms, error "
                    "FROM supplier_ab_test_runs "
                    "WHERE run_id=? AND ebay_item_id=? "
                    "ORDER BY candidate_index, model",
                    (_selected_run, _li["ebay_item_id"]),
                ).fetchall()

            _by_cand: dict[int, dict[str, dict]] = {}
            for _c in _cands:
                _by_cand.setdefault(_c["candidate_index"], {})[_c["model"]] = dict(_c)

            if not _by_cand:
                st.caption("候補なし (scrape 0 件 or 全エラー)")
                continue

            for _idx in sorted(_by_cand.keys()):
                _opus = _by_cand[_idx].get("claude-opus-4-7")
                _sonnet = _by_cand[_idx].get("claude-sonnet-4-6")
                _cand_title = (_opus or _sonnet)["candidate_title"]
                _cand_url = (_opus or _sonnet)["candidate_url"]
                _cand_price = (_opus or _sonnet)["candidate_price_jpy"]
                _cand_plat = (_opus or _sonnet)["candidate_platform"]

                # diff 強調: score 差 > 20 = 不一致 marker
                _diff = abs((_opus["match_score"] if _opus else 0) - (_sonnet["match_score"] if _sonnet else 0))
                _verdict_diff = (
                    _opus and _sonnet and
                    ((_opus["match_score"] >= 60) != (_sonnet["match_score"] >= 60))
                )
                _diff_badge = ""
                if _verdict_diff:
                    _diff_badge = ' <span style="color:#ff6464;font-weight:600;font-size:11px;background:rgba(200,80,80,0.2);padding:2px 8px;border-radius:3px;">採用判定が不一致</span>'
                elif _diff > 20:
                    _diff_badge = f' <span style="color:#f0c830;font-weight:600;font-size:11px;background:rgba(180,150,40,0.18);padding:2px 8px;border-radius:3px;">score 差 {_diff} 点</span>'

                # _cand_price が None (= scraper で価格取得失敗) でも format crash しないよう
                # 明示 fallback. 2026-05-05 修正.
                _price_disp = f"¥{_cand_price:,}" if _cand_price is not None else "¥-"
                st.markdown(
                    f"**候補 #{_idx}** [{_cand_plat}]　"
                    f"<a href='{_cand_url}' target='_blank' style='color:rgba(120,200,255,0.9);'>{_cand_title}</a>　"
                    f"<span style='color:rgba(180,180,180,0.7);'>{_price_disp}</span>"
                    f"{_diff_badge}",
                    unsafe_allow_html=True,
                )

                _col1, _col2 = st.columns(2)
                for _col, _row, _label, _color in [
                    (_col1, _opus,   "Opus 4.7",   "rgba(196,128,255,0.95)"),
                    (_col2, _sonnet, "Sonnet 4.6", "rgba(120,200,255,0.95)"),
                ]:
                    with _col:
                        if not _row:
                            st.caption(f"{_label}: 評価データなし")
                            continue
                        if _row.get("error"):
                            st.markdown(
                                f"<span style='color:{_color};font-weight:600;'>{_label}</span>: "
                                f"<span style='color:#ff8a80;'>ERROR — {_row['error']}</span>",
                                unsafe_allow_html=True,
                            )
                            continue
                        _score = _row["match_score"]
                        _score_color = (
                            "rgba(118,255,3,0.9)" if _score >= 80
                            else "rgba(240,200,48,0.9)" if _score >= 60
                            else "rgba(255,128,128,0.9)"
                        )
                        _flags = []
                        if _row.get("alt_listing_possible"):
                            _flags.append("alt_listing")
                        if _row.get("junk_likely_untested"):
                            _flags.append("junk_untested")
                        _flag_html = (
                            f' <span style="color:#a89d8a;font-size:10px;">[{",".join(_flags)}]</span>'
                            if _flags else ""
                        )
                        _cache_marker = "✓" if (_row.get("cache_read_tokens") or 0) > 0 else "—"
                        st.markdown(
                            f"<span style='color:{_color};font-weight:600;font-size:13px;'>{_label}</span>　"
                            f"<span style='color:{_score_color};font-size:18px;font-weight:700;'>{_score}</span>"
                            f"{_flag_html}　"
                            f"<span style='color:rgba(160,160,160,0.7);font-size:11px;'>"
                            f"${_row['cost_usd']:.5f} / {_row['duration_ms']/1000:.1f}s / cache {_cache_marker}"
                            f"</span>",
                            unsafe_allow_html=True,
                        )
                        st.caption(f"判定理由: {_row['reasoning']}")
                        if _row.get("alt_listing_note"):
                            st.caption(f"alt_listing_note: {_row['alt_listing_note']}")


# ========== 個別出品タブ (W9) ==========
if _w134_sel == "個別出品":
    render_individual_listing_tab(s)


# ========== 通関対応タブ (W14 2026-04-24) ==========
if _w134_sel == "通関対応":
    import json as _json_cust
    from monitor.database import get_conn as _cust_conn

    st.title("通関対応")
    st.caption(
        "FedEx / DHL / UPS からの通関情報提出要求を自動検知し、英文ドラフトを生成。"
        " 内容を確認の上、送信ボタンで Gmail API 経由で送信します。"
    )

    # ── 最新情報取得ボタン ──
    # daily_scheduler の 06:10 朝バッチを待たずに、user 操作で即時 Gmail から
    # 通関メールを再取得する. 過去 7 日範囲を再 scan, 新規のみ DB に追加 (gmail_id UNIQUE).
    _refresh_col_a, _refresh_col_b = st.columns([1, 4])
    with _refresh_col_a:
        _refresh_btn = st.button(
            "🔄 最新情報取得",
            type="primary",
            key="customs_refresh_btn",
            help="Gmail から最新の通関メールを取得 (過去 7 日)。約 1-3 分。",
        )
    with _refresh_col_b:
        st.caption(
            "前回自動取得: 毎朝 06:10 / 手動実行可能"
        )
    if _refresh_btn:
        with st.spinner("Gmail から通関メール取得中... (1-3 分)"):
            try:
                from tasks.task_customs_check import run_customs_check
                import json as _json_refresh
                import io as _io_refresh
                with _io_refresh.open(
                    "config/schedule_config.json", encoding="utf-8"
                ) as _scf:
                    _refresh_cfg = _json_refresh.load(_scf)
                _r = run_customs_check(_refresh_cfg, days=7)
                if _r.get("success"):
                    st.success(
                        f"取得完了: 検知 {_r.get('detected', 0)} 件、"
                        f"ドラフト生成 {_r.get('drafted', 0)} 件、"
                        f"手動対応要 {_r.get('manual', 0)} 件"
                    )
                else:
                    st.error(f"取得失敗: {_r.get('message','unknown')}")
            except Exception as _re:
                st.error(f"エラー: {type(_re).__name__}: {_re}")
            st.rerun()

    # ── Subtabs ──
    _cust_tab_pending, _cust_tab_sent, _cust_tab_manual, _cust_tab_kb = st.tabs([
        "要対応", "送信済み", "手動対応要", "KB承認",
    ])

    # ── 要対応 (drafted) ──
    with _cust_tab_pending:
        with _cust_conn() as _cc:
            _pending = [dict(r) for r in _cc.execute(
                """SELECT * FROM customs_requests
                   WHERE status IN ('drafted', 'drafted_no_photo',
                                     'drafted_in_gmail')
                   ORDER BY deadline ASC NULLS LAST, detected_at DESC"""
            ).fetchall()]
        st.markdown(f"**要対応: {len(_pending)} 件**")
        if not _pending:
            st.info(
                "現在、要対応の通関案件はありません。"
                "毎朝 06:10 に FedEx/DHL/UPS から新規案件を自動検知します。"
            )
        for _req in _pending:
            _deadline = _req.get("deadline") or ""
            _days_left = ""
            if _deadline:
                try:
                    from datetime import datetime as _dt_cust
                    _d = _dt_cust.strptime(_deadline, "%Y-%m-%d")
                    _diff = (_d - _dt_cust.now()).days
                    _days_left = f" (残 {_diff} 日)" if _diff >= 0 else f" (期限 {abs(_diff)} 日超過)"
                except ValueError:
                    pass
            _deadline_color = "#ff6060" if "超過" in _days_left else (
                "#ffa84a" if "残 0" in _days_left or "残 1" in _days_left
                else "#cccccc"
            )
            _header = (
                f"**[{_req['carrier'].upper()}]** "
                f"TRK# {_req.get('tracking_number') or '(不明)'}  "
                f":green[{_req.get('product_title') or '(商品未特定)'}]"
            )
            st.markdown(
                f'<div style="border-left:3px solid {_deadline_color};padding:6px 12px;'
                f'margin:6px 0;background:rgba(80,120,180,0.03);">'
                f'{_header}<br>'
                f'<span style="color:{_deadline_color};font-size:12px;">'
                f'期限: {_deadline or "不明"}{_days_left}</span>  '
                f'<span style="color:rgba(180,220,255,0.55);font-size:11px;">'
                f'| 検知: {_req.get("detected_at","")[:16]}</span>'
                f'</div>', unsafe_allow_html=True)
            with st.expander(f"ドラフト内容を確認 (#{_req['id']})", expanded=False):
                # 送信先
                try:
                    _recips = _json_cust.loads(_req.get("draft_recipients") or "{}")
                except Exception:
                    _recips = {}
                _to_list = _recips.get("to") or []
                _cc_list = _recips.get("cc") or []
                st.markdown(
                    f"**TO**: :red[**{', '.join(_to_list) if _to_list else '(未解決)'}**]  "
                    f"**CC**: {', '.join(_cc_list) if _cc_list else '(なし)'}"
                )
                st.markdown(f"**Subject**: `{_req.get('draft_subject','')}`")
                st.markdown("**Body** (英文):")
                st.code(_req.get("draft_body") or "", language="text")
                # 添付
                try:
                    _photos = _json_cust.loads(_req.get("attached_photos") or "[]")
                except Exception:
                    _photos = []
                if _photos:
                    st.markdown(f"**添付写真**: {len(_photos)} 枚")
                    _pc = st.columns(min(len(_photos), 5))
                    for _i, _p in enumerate(_photos[:5]):
                        try:
                            _pc[_i].image(_p, width=120)
                        except Exception:
                            _pc[_i].caption(Path(_p).name)
                else:
                    st.warning("添付写真なし (status: drafted_no_photo) — 送信前に user が手動で追加する必要があります")
                # 補助情報
                _warnings = _req.get("error_msg")
                if _warnings:
                    st.warning(f"注意: {_warnings}")
                try:
                    _kb = _json_cust.loads(_req.get("kb_hits") or "{}")
                    st.caption(
                        f"メーカー KB: {_kb.get('manufacturer') or '(未解決)'} "
                        f"/ HTS: {_kb.get('hts') or '(未解決)'} "
                        f"/ テンプレ: {_req.get('template_used') or '(なし)'}"
                    )
                except Exception:
                    pass

                # ── W14ext (2026-04-25): 「送信準備」= Gmail 下書き作成 ──
                # フロー:
                #   status='drafted' or 'drafted_no_photo' (Gmail 未保存):
                #     → 「送信準備」ボタンで Gmail draft 作成
                #     → status='drafted_in_gmail'
                #   status='drafted_in_gmail' (Gmail に下書き保存済):
                #     → 「Gmail で確認」リンク + 「送信」ボタン (赤、2 段階確認)
                #     → 送信完了で status='sent'
                _existing_status = _req.get("status") or ""
                _draft_gmail_id = (_req.get("draft_gmail_id") or "").strip()

                # ── 「✋ 対応済み (手動)」ボタン (全 status 共通) ──
                # MONO Deck 経由で送らず Gmail 直接 or 別手段で対応した場合に
                # 要対応リストから外す.
                # status='sent' + gmail_sent_id=NULL + error_msg にマーカーで記録.
                # 送信済みサブタブには「(手動対応)」ラベル付きで表示される.
                _manual_done_key = f"customs_manual_done_confirm_{_req['id']}"
                if st.session_state.get(_manual_done_key):
                    st.warning("**確認**: この案件を「対応済み」として要対応から外します。")
                    _md_c1, _md_c2 = st.columns(2)
                    with _md_c1:
                        if st.button(
                            "確定",
                            key=f"customs_manual_done_go_{_req['id']}",
                            type="primary",
                        ):
                            from monitor.database import get_conn as _gc_md
                            from datetime import datetime as _dt_md
                            with _gc_md() as _c_md:
                                _c_md.execute(
                                    "UPDATE customs_requests SET status='sent', "
                                    "sent_at=CURRENT_TIMESTAMP, gmail_sent_id=NULL, "
                                    "draft_gmail_id=NULL, draft_lock_at=NULL, "
                                    "error_msg=COALESCE(error_msg,'') || ? "
                                    "WHERE id=?",
                                    (" [manually-handled outside MONO Deck "
                                     f"({_dt_md.now().strftime('%Y-%m-%d %H:%M')})]",
                                     _req["id"]),
                                )
                            st.session_state[_manual_done_key] = False
                            st.success("対応済みにしました")
                            st.rerun()
                    with _md_c2:
                        if st.button(
                            "キャンセル",
                            key=f"customs_manual_done_cancel_{_req['id']}",
                        ):
                            st.session_state[_manual_done_key] = False
                            st.rerun()
                else:
                    if st.button(
                        "✋ 対応済み (手動・要対応から外す)",
                        key=f"customs_manual_done_btn_{_req['id']}",
                        help="Gmail 直接や電話など MONO Deck 外で対応済みの場合"
                             "、このボタンで要対応から外せます。",
                    ):
                        st.session_state[_manual_done_key] = True
                        st.rerun()

                if _existing_status in ("drafted", "drafted_no_photo"):
                    # 段階 1: Gmail 下書きをまだ作っていない → 「送信準備」表示
                    if st.button(
                        "送信準備 (Gmail 下書きとして保存)",
                        key=f"customs_send_prep_{_req['id']}",
                        type="primary",
                    ):
                        try:
                            from monitor.customs_gmail_sender import (
                                CustomsSendBlocked, CustomsSendFailed,
                                create_customs_draft,
                            )
                            import json as _json_prep
                            import io as _io_prep
                            with _io_prep.open(
                                "config/schedule_config.json", encoding="utf-8"
                            ) as _scf:
                                _prep_cfg = _json_prep.load(_scf)
                            _dr = create_customs_draft(
                                _req["id"], config=_prep_cfg,
                            )
                            if _dr.success:
                                st.success(
                                    f"Gmail 下書きを{_dr.action}しました "
                                    f"(draft id: {_dr.draft_gmail_id[:16]}...)"
                                )
                                st.rerun()
                            else:
                                st.error(f"下書き作成失敗: {_dr.error}")
                        except CustomsSendBlocked as _e:
                            st.warning(f"ブロック: {_e}")
                        except CustomsSendFailed as _e:
                            st.error(f"Gmail API 失敗: {_e}")
                        except Exception as _e:
                            st.error(f"予期せぬエラー: {type(_e).__name__}: {_e}")
                elif _existing_status == "drafted_in_gmail":
                    # 段階 2: Gmail 下書き保存済 → 確認 + 送信ボタン
                    _gmail_draft_url = (
                        f"https://mail.google.com/mail/u/0/#drafts/{_draft_gmail_id}"
                        if _draft_gmail_id else ""
                    )
                    st.success("✓ Gmail に下書き保存済み")
                    if _gmail_draft_url:
                        st.markdown(
                            f'<a href="{_gmail_draft_url}" target="_blank" '
                            f'style="color:#7ab8ff;">📧 Gmail で内容を確認する</a>',
                            unsafe_allow_html=True,
                        )

                    _confirm_key = f"customs_send_confirm_{_req['id']}"
                    if not st.session_state.get(_confirm_key):
                        _btn_cols = st.columns(2)
                        with _btn_cols[0]:
                            if st.button(
                                "🔴 送信",
                                key=f"customs_send_go_{_req['id']}",
                                type="primary",
                            ):
                                st.session_state[_confirm_key] = True
                                st.rerun()
                        with _btn_cols[1]:
                            if st.button(
                                "下書きを更新",
                                key=f"customs_draft_update_{_req['id']}",
                                help="本文や宛先を変えた場合は再度「送信準備」で Gmail 下書きを再作成",
                            ):
                                # H-2 対応: draft_gmail_id を NULL にして強制 create.
                                # H-X2 対応: draft_lock_at もクリアして即時再「送信準備」可
                                # 古い Gmail 下書きは Gmail UI で user が削除可能.
                                from monitor.database import get_conn as _gc
                                with _gc() as _c:
                                    _c.execute(
                                        "UPDATE customs_requests SET "
                                        "status='drafted', draft_gmail_id=NULL, "
                                        "draft_lock_at=NULL "
                                        "WHERE id=? AND status='drafted_in_gmail'",
                                        (_req["id"],),
                                    )
                                st.info(
                                    "ステータスを drafted に戻しました。"
                                    "Gmail 上の旧下書きが残っている場合は手動で削除してください。"
                                )
                                st.rerun()
                    else:
                        st.error(
                            "**最終確認** — Gmail で内容を確認しましたか？"
                            "送信すると撤回できません。"
                        )
                        _bc1, _bc2 = st.columns(2)
                        with _bc1:
                            if st.button(
                                "**本当に送信**",
                                key=f"customs_send_final_{_req['id']}",
                                type="primary",
                            ):
                                try:
                                    from monitor.customs_gmail_sender import (
                                        CustomsSendBlocked, CustomsSendFailed,
                                        send_customs_reply,
                                    )
                                    import json as _json_send
                                    import io as _io_send
                                    with _io_send.open(
                                        "config/schedule_config.json",
                                        encoding="utf-8",
                                    ) as _scf:
                                        _send_cfg = _json_send.load(_scf)
                                    _r = send_customs_reply(
                                        _req["id"], config=_send_cfg,
                                    )
                                    if _r.success:
                                        st.success(
                                            f"送信成功: Gmail ID {_r.gmail_sent_id}"
                                        )
                                    else:
                                        st.error(f"送信失敗: {_r.error}")
                                except CustomsSendBlocked as _e:
                                    st.warning(f"送信ブロック: {_e}")
                                except CustomsSendFailed as _e:
                                    st.error(f"Gmail API 失敗: {_e}")
                                except Exception as _e:
                                    st.error(
                                        f"予期せぬエラー: {type(_e).__name__}: {_e}"
                                    )
                                st.session_state[_confirm_key] = False
                                st.rerun()
                        with _bc2:
                            if st.button(
                                "キャンセル",
                                key=f"customs_send_cancel_{_req['id']}",
                            ):
                                st.session_state[_confirm_key] = False
                                st.rerun()

    # ── 送信済み ──
    with _cust_tab_sent:
        with _cust_conn() as _cc:
            _sent = [dict(r) for r in _cc.execute(
                """SELECT id, carrier, tracking_number, product_title,
                          draft_subject, sent_at, gmail_sent_id, error_msg
                   FROM customs_requests
                   WHERE status = 'sent'
                   ORDER BY sent_at DESC LIMIT 50"""
            ).fetchall()]
        st.markdown(f"**送信済み: {len(_sent)} 件**")
        if not _sent:
            st.info("まだ送信済み案件はありません")
        for _s in _sent:
            # 手動対応マーカー判別 (gmail_sent_id NULL かつ error_msg に marker)
            _is_manual = (
                not _s.get("gmail_sent_id")
                and "manually-handled" in (_s.get("error_msg") or "")
            )
            _label = "✋ 手動対応" if _is_manual else "✓ 送信済"
            _detail = (
                "MONO Deck 経由ではない (user 手動対応)"
                if _is_manual
                else f"Gmail ID: {_s.get('gmail_sent_id')}"
            )
            st.markdown(
                f'- **{_label}** [{_s["carrier"].upper()}] '
                f'TRK# {_s.get("tracking_number") or "?"}  '
                f'{_s.get("product_title") or ""}  \n'
                f'  :gray[完了: {(_s.get("sent_at") or "")[:16]} / {_detail}]'
            )

    # ── 手動対応要 (manual / failed) ──
    with _cust_tab_manual:
        with _cust_conn() as _cc:
            _manual = [dict(r) for r in _cc.execute(
                """SELECT * FROM customs_requests
                   WHERE status IN ('manual', 'failed')
                   ORDER BY detected_at DESC LIMIT 50"""
            ).fetchall()]
        st.markdown(f"**手動対応要 / 失敗: {len(_manual)} 件**")
        if not _manual:
            st.info("手動対応が必要な案件はありません")
        for _m in _manual:
            st.markdown(
                f'- [{_m["carrier"].upper()}] status={_m["status"]} '
                f'TRK# {_m.get("tracking_number") or "?"}  \n'
                f'  :orange[{_m.get("error_msg","")[:200]}]'
            )

    # ── KB 承認 (customs_kb_pending) ──
    with _cust_tab_kb:
        st.markdown("**承認待ち KB エントリ** (Tier 2/3 web 検索結果)")
        try:
            from monitor.customs_kb import (
                approve_kb_entry, list_pending_kb, reject_kb_entry,
            )
            _kb_pending = list_pending_kb()
        except Exception as _e:
            _kb_pending = []
            st.warning(f"KB pending load failed: {_e}")
        if not _kb_pending:
            st.info("承認待ちエントリはありません")
        for _kp in _kb_pending:
            with st.expander(
                f"[{_kp['kind']}] {_kp['brand_or_category']} ({_kp['created_at'][:10]})"
            ):
                try:
                    _pj = _json_cust.loads(_kp.get("proposed_json") or "{}")
                    st.json(_pj)
                except Exception:
                    st.code(_kp.get("proposed_json") or "")
                if _kp.get("source_url"):
                    st.caption(f"Source: {_kp['source_url']}")
                _akc1, _akc2 = st.columns(2)
                with _akc1:
                    if st.button(
                        "承認 (KB 昇格)", key=f"kb_approve_{_kp['id']}",
                        type="primary",
                    ):
                        if approve_kb_entry(_kp["id"]):
                            st.success("承認 → customs_kb.json に追加")
                            st.rerun()
                        else:
                            st.error("承認失敗")
                with _akc2:
                    if st.button("却下", key=f"kb_reject_{_kp['id']}"):
                        if reject_kb_entry(_kp["id"]):
                            st.info("却下しました")
                            st.rerun()


# ========== 市場戦略タブ (W7-A 2026-04-27) ==========
if _w134_sel == "市場戦略":
    from tabs.tab_market_strategy import render_tab as render_market_strategy_tab
    render_market_strategy_tab()


# ========== 動画学習タブ ==========
if _w134_sel == "動画学習":
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
                "done": "rgba(118,255,3,0.8)",
                "processing": "rgba(240,200,48,0.9)",
                "pending": "rgba(180,200,220,0.7)",
                "failed": "rgba(255,80,80,0.9)",
            }.get(_status, "rgba(200,200,200,0.5)")

            _title = _r.get("title") or _r.get("video_id") or ""
            _dur = _r.get("duration_sec") or 0
            _dur_str = f"{_dur//60}分{_dur%60}秒" if _dur else ""
            _added = _r.get("added_at") or ""

            _dur_html = (
                f'<span style="color:rgba(180,220,255,0.5);font-size:11px;">{_dur_str}</span>'
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
                "evergreen": "rgba(180,200,220,0.7)",
            }.get(_era, "rgba(180,180,180,0.5)")
            _era_html = (
                f'<span style="color:{_era_color};font-size:11px;font-weight:700;">[{_era_label}]</span>'
                if _era_label else ''
            )

            # 公開日
            _pub = _r.get("published_date") or ""
            _pub_html = (
                f'<span style="color:rgba(180,220,255,0.6);font-size:11px;">{html.escape(_pub)}</span>'
                if _pub else ''
            )
            _summary_html = (
                f'<div style="margin-top:6px;font-size:13px;color:rgba(255,255,255,0.85);line-height:1.5;">'
                f'{html.escape((_r.get("summary_ja") or "")[:300])}</div>'
                if _r.get("summary_ja") else ''
            )
            _topics_html = (
                f'<div style="margin-top:4px;font-size:11px;color:rgba(180,220,255,0.6);">'
                f'Topics: {html.escape(_r.get("topics") or "")}</div>'
                if _r.get("topics") else ''
            )

            st.markdown(
                f'<div style="border:1px solid rgba(120,180,255,0.3);'
                f'border-radius:6px;padding:10px 14px;margin:6px 0;'
                f'background:rgba(20,30,50,0.4);">'
                f'<div style="display:flex;gap:12px;align-items:center;font-family:Share Tech Mono,monospace;">'
                f'<span style="color:{_st_color};font-size:11px;font-weight:700;">[{_status.upper()}]</span>'
                f'{_era_html}'
                f'{_pub_html}'
                f'<span style="color:rgba(180,220,255,0.8);font-size:12px;">{html.escape(_title[:80])}</span>'
                f'{_dur_html}'
                f'<span style="color:rgba(180,220,255,0.4);font-size:10px;margin-left:auto;">{html.escape(_added[:16])}</span>'
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


# ========== エージェント監視タブ ==========
if _w134_sel == "エージェント監視":
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


# ========== SKU変換ルール設定タブ ==========
if _w134_sel == "SKU変換":
    st.title("SKU → 仕入先URL変換ルール")

    mappings = load_mappings()

    # タブ分割
    tab_view, tab_add, tab_edit, tab_test = st.tabs(["ルール一覧", "新規追加", "編集", "テスト"])

    # ========== ルール一覧 ==========
    with tab_view:
        st.subheader("現在のマッピングルール")

        if not mappings:
            st.info("ルールが登録されていません")
        else:
            # ルール表示用のDataFrame
            rules_data = []
            for prefix, config in mappings.items():
                rules_data.append({
                    "プリフィックス": prefix,
                    "仕入先名": config.get("name", ""),
                    "説明": config.get("description", ""),
                    "URL": config.get("common_url", ""),
                    "パターン": config.get("pattern", "")
                })

            df = pd.DataFrame(rules_data)
            st.dataframe(df, width="stretch", height=400)

            # リセットボタン
            col1, col2 = st.columns([3, 1])
            with col2:
                if st.button("RESET TO DEFAULTS", key="reset_btn"):
                    if reset_to_defaults():
                        st.success("デフォルトルールに戻しました")
                        st.rerun()
                    else:
                        st.error("リセットに失敗しました")

    # ========== 新規追加 ==========
    with tab_add:
        st.subheader("新しいマッピングルールを追加")

        col1, col2 = st.columns(2)
        with col1:
            new_prefix = st.text_input("プリフィックス", placeholder="例: ebayam_", key="add_prefix")
            new_name = st.text_input("仕入先名", placeholder="例: Amazon", key="add_name")
        with col2:
            new_desc = st.text_input("説明", placeholder="例: Amazon.co.jp", key="add_desc")
            new_url = st.text_input("ベースURL", placeholder="https://example.com/item/", key="add_url")

        new_pattern = st.text_input(
            "URLパターン",
            placeholder="例: {item_id} または m{item_id}",
            help="プレースホルダー {item_id} を使用できます",
            key="add_pattern"
        )

        if st.button("ルール追加", type="primary", key="add_btn"):
            if new_prefix and new_name and new_url and new_pattern:
                success, message = add_mapping(new_prefix, new_name, new_url, new_pattern, new_desc)
                if success:
                    st.success(message)
                    st.rerun()
                else:
                    st.error(message)
            else:
                st.error("すべてのフィールドを入力してください")

    # ========== 編集 ==========
    with tab_edit:
        st.subheader("既存ルールを編集")

        if not mappings:
            st.info("編集するルールがありません")
        else:
            # 編集対象を選択
            prefix_to_edit = st.selectbox(
                "編集するプリフィックスを選択",
                options=list(mappings.keys()),
                key="edit_prefix"
            )

            if prefix_to_edit:
                current = mappings[prefix_to_edit]

                col1, col2 = st.columns(2)
                with col1:
                    edit_name = st.text_input(
                        "仕入先名",
                        value=current.get("name", ""),
                        key="edit_name"
                    )
                    edit_desc = st.text_input(
                        "説明",
                        value=current.get("description", ""),
                        key="edit_desc"
                    )
                with col2:
                    edit_url = st.text_input(
                        "ベースURL",
                        value=current.get("common_url", ""),
                        key="edit_url"
                    )
                    edit_pattern = st.text_input(
                        "URLパターン",
                        value=current.get("pattern", ""),
                        key="edit_pattern"
                    )

                col_save, col_delete = st.columns(2)
                with col_save:
                    if st.button("保存", type="primary", key="update_btn"):
                        success, message = update_mapping(
                            prefix_to_edit, edit_name, edit_url, edit_pattern, edit_desc
                        )
                        if success:
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)

                with col_delete:
                    if st.button("削除", key="delete_btn"):
                        success, message = delete_mapping(prefix_to_edit)
                        if success:
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)

    # ========== テスト機能 ==========
    with tab_test:
        st.subheader("SKU → URL 変換テスト")
        st.write("SKUを入力して、実際に生成されるURLを確認します")

        test_sku = st.text_input(
            "テストするSKU",
            placeholder="例: ebayme_m81786287162",
            key="test_sku"
        )

        if test_sku:
            valid, prefix, item_id, message = validate_sku(test_sku)

            st.subheader("検証結果")
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("SKU", test_sku)
            with col2:
                st.metric("プリフィックス", prefix or "不明")
            with col3:
                st.metric("Item ID", item_id or "不明")

            st.write(f"**ステータス**: {message}")

            if valid and prefix and item_id:
                generated_url = generate_url(prefix, item_id)
                if generated_url:
                    st.success(f"生成URL: [{generated_url}]({generated_url})")

                    # クリップボードにコピー機能
                    st.code(generated_url, language="text")
                else:
                    st.error("URLを生成できませんでした")


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
            save_settings(s)
            st.session_state.settings = s
            st.write("▸ 保存完了")
            status.update(label="設定を保存しました", state="complete")
