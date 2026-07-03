#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DASHBOARD (本日の要対応・通関・在庫リスク・ニュース・ROADMAP) タブ (W221 Tier2 抽出、2026-06-04)。

app.py の `if _w134_sel == "DASHBOARD":` 分岐 body をそのまま移植。挙動不変 (K2 surgical)。
同梱ヘルパー (app.py top-level から移動、単一タブ専用): _cd_execution_summary, _cd_dash_emails, _cd_customs_pending_count, _cd_active_tasks
"""
from __future__ import annotations

import html
import logging
from pathlib import Path
import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)


def _parse_news_published(s: str):
    """W224 (2026-06-05): news_items.published_at を datetime(UTC, aware) に parse。

    RSS 由来でフォーマット混在: ISO 8601 (例 '2026-06-04T16:15:12Z') と
    RFC822 (例 'Fri, 17 Apr 2026 00:00:00 +0000')。両対応。parse 不能/空は None。
    """
    from datetime import datetime, timezone
    s = (s or "").strip()
    if not s:
        return None
    # ISO 8601 ('Z' を +00:00 に置換して fromisoformat)。外部 (RSS) 由来の汚染文字列を
    # 扱うため parse 例外は全て None に倒す (chip 非表示で degrade = 仕様)。
    # OverflowError: 極端な未来/過去日付の astimezone で発生し得る。
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass
    # RFC822
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _fmt_news_freshness(dt_utc, now_utc):
    """W224: 投稿日時を JST 絶対表示 + 相対表示 + 経過日数 で返す。

    Returns: (jst_abs: str 'M/D HH:MM', relative: str 'N分前'/'N時間前'/'N日前'/'M/D', age_days: float)
    """
    from datetime import timezone, timedelta
    jst = dt_utc.astimezone(timezone(timedelta(hours=9)))
    secs = (now_utc - dt_utc).total_seconds()
    if secs < 0:  # 未来日付 (feed 側の時計ズレ) は 0 扱い
        secs = 0
    if secs < 3600:
        rel = f"{int(secs // 60)}分前"
    elif secs < 86400:
        rel = f"{int(secs // 3600)}時間前"
    elif secs < 7 * 86400:
        rel = f"{int(secs // 86400)}日前"
    else:
        rel = f"{jst.month}/{jst.day}"
    jst_abs = f"{jst.month}/{jst.day} {jst.hour:02d}:{jst.minute:02d}"
    return jst_abs, rel, secs / 86400.0


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
def _cd_notification_unread_counts(db_version: int) -> dict:
    """依頼ボード #39 Phase A S4: カテゴリ別未読件数 (S1 monitor.notification_log_db 経由)。"""
    from monitor.notification_log_db import get_unread_count_by_category
    return get_unread_count_by_category()


@st.cache_data(ttl=3, show_spinner=False)
def _cd_notification_rows(db_version: int, unread_only: bool, category, limit: int):
    """依頼ボード #39 Phase A S4: 通知一覧 (カテゴリ別 expander / 既読7日 折りたたみ 共用)。"""
    from monitor.notification_log_db import get_notifications
    return get_notifications(unread_only=unread_only, category=category, limit=limit)


def render_dashboard_tab(s: dict) -> None:
    # W221 Tier2 fix (2026-06-05): app.py top-level import をグローバル参照していた
    # 名前を関数内 lazy import で補完 (抽出漏れ修正、render 実行時 NameError 防止)。
    from company_integration import complete_task, get_archived_tasks, get_company_status, get_today_routine_result
    from fuel_surcharge_manager import UPDATE_WARNING_DAYS, get_days_since_last_update
    import json
    from scheduler_integration import get_latest_execution_logs
    from shipping_rate_manager import SHIPPING_RATE_WARNING_DAYS, get_shipping_rate_days_since_update
    import sqlite3
    from ui_cache import bump_db_version, get_db_version
    import re as _re_dash
    from monitor.database import get_recent_emails as _dash_get_emails, set_email_confirmed
    import streamlit.components.v1 as _components

    # ── 通知センター (依頼ボード #39 Phase A S4, 2026-07-03) ──
    # S1 (monitor/notification_log_db.py) と並行実装中。テーブル/モジュール未整備の
    # 期間は ImportError / sqlite3.Error を「準備中」caption で明示 fallback する
    # (Q0: silent 非表示にせず、未整備であることが分かる状態を保つ)。
    try:
        from monitor.notification_log_db import (
            mark_read as _nc_mark_read,
            mark_category_read as _nc_mark_category_read,
            mark_all_read as _nc_mark_all_read,
        )
        from tabs._notification_center_html import (
            render_notification_row_html as _nc_render_row,
            category_emoji as _nc_cat_emoji,
            category_label_ja as _nc_cat_label,
            get_nav_target as _nc_get_nav_target,
            is_within_days as _nc_is_within_days,
        )

        st.markdown(
            '<div style="font-family:Inter,sans-serif;font-size:11px;font-weight:700;'
            'color:#0e4f4b;letter-spacing:2.5px;text-transform:uppercase;'
            'padding:10px 16px;margin:0 0 10px 0;border:1px solid rgba(14,79,75,0.18);'
            'border-left:3px solid #0e4f4b;border-radius:4px;background:rgba(14,79,75,0.04);">'
            '🔔 通知センター</div>',
            unsafe_allow_html=True,
        )

        _nc_unread_counts = _cd_notification_unread_counts(get_db_version())
        _nc_total_unread = sum(_nc_unread_counts.values())

        if _nc_total_unread == 0:
            st.caption("新しい通知はありません")
        else:
            if st.button(f"✓ 全て既読にする ({_nc_total_unread}件)", key="nc_mark_all_read"):
                _nc_mark_all_read()
                bump_db_version()
                st.rerun()

            for _nc_cat, _nc_cnt in sorted(
                _nc_unread_counts.items(), key=lambda kv: (-kv[1], kv[0])
            ):
                if _nc_cnt <= 0:
                    continue
                _nc_label = _nc_cat_label(_nc_cat)
                _nc_emoji_c = _nc_cat_emoji(_nc_cat)
                with st.expander(f"{_nc_emoji_c} {_nc_label} ({_nc_cnt})", expanded=False):
                    _nc_rows = _cd_notification_rows(get_db_version(), True, _nc_cat, 10)
                    for _nc_n in _nc_rows:
                        # 1 通知 = コンパクト 1 行 (~30px)。ボタンは同一行右端に小型配置。
                        # link_target で「開く」ボタンの有無を切り替える (マップ外は左詰め)。
                        _nc_nav = _nc_get_nav_target(_nc_n.get("link_target") or _nc_cat)
                        if _nc_nav:
                            _c_body, _c_open, _c_read = st.columns([12, 1, 1])
                        else:
                            _c_body, _c_read = st.columns([13, 1])
                            _c_open = None
                        with _c_body:
                            st.markdown(_nc_render_row(_nc_n), unsafe_allow_html=True)
                        if _c_open is not None:
                            with _c_open:
                                if st.button("開く", key=f"nc_open_{_nc_cat}_{_nc_n['id']}"):
                                    st.session_state["_w134_sel"] = _nc_nav[0]
                                    st.session_state["_w217a_cat_view"] = _nc_nav[1]
                                    _nc_mark_read([_nc_n["id"]])
                                    bump_db_version()
                                    st.rerun()
                        with _c_read:
                            if st.button("✓", key=f"nc_read_{_nc_cat}_{_nc_n['id']}"):
                                _nc_mark_read([_nc_n["id"]])
                                bump_db_version()
                                st.rerun()
                    _nc_over = _nc_cnt - len(_nc_rows)
                    if _nc_over > 0:
                        st.caption(f"... 他 {_nc_over} 件 (上位10件表示)")
                    if st.button(f"「{_nc_label}」を全て既読にする", key=f"nc_cat_read_{_nc_cat}"):
                        _nc_mark_category_read(_nc_cat)
                        bump_db_version()
                        st.rerun()

        # 既読 (7日) — 空セクションは非表示 (実機 fb「既読 (7日) (0)」ノイズ根治)。
        _nc_recent_rows = _cd_notification_rows(get_db_version(), False, None, 100)
        _nc_read_recent = [
            _r for _r in _nc_recent_rows
            if _r.get("read_at") and _nc_is_within_days(_r.get("created_at") or "", 7)
        ]
        if _nc_read_recent:
            with st.expander(f"既読 (7日) ({len(_nc_read_recent)})", expanded=False):
                for _nc_n in _nc_read_recent[:50]:
                    st.markdown(_nc_render_row(_nc_n), unsafe_allow_html=True)
    except (ImportError, sqlite3.Error) as _nc_e:
        st.caption("🔔 通知センター: 準備中 (基盤実装待ち)")
        logger.info(f"通知センター 表示 skip (未整備): {_nc_e}")

    # ── W24: Research 脳 morning brief セクション (本日分があれば表示) ──
    try:
        from tasks.task_research_morning_brief import get_today_brief as _get_today_brief
        _today_brief = _get_today_brief()
        if _today_brief:
            with st.container(border=True):
                st.markdown(
                    '<div style="font-size:11px;color:#8d927f;letter-spacing:2px;'
                    'margin-bottom:6px;">M O R N I N G &nbsp; B R I E F &nbsp; — &nbsp; '
                    'Research 脳 (Opus 4.8)</div>',
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

    # ── W119 (2026-05-12): 在庫通知 (有在庫 SKU で在庫切れ・在庫未入力) ──
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

        if _inv_zero or _inv_unset:
            with st.container(border=True):
                st.markdown("### 📦 在庫通知")
                metric_cols = st.columns(3)
                with metric_cols[0]:
                    st.metric("🔴 在庫切れ (有在庫)", len(_inv_zero),
                              help="有在庫 (stock SKU) で在庫数が 0 個になっている listing 数。"
                                   "商品管理タブで在庫補充 + eBay 反映を")
                with metric_cols[1]:
                    st.metric("⚪ 在庫数未入力", len(_inv_unset),
                              help="stock prefix SKU だが inventory_count=NULL. "
                                   "売れても自動減算されないため oversell リスク. "
                                   "商品管理タブで在庫数を入力してください.")
                with metric_cols[2]:
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

    # ── W120+W121 (2026-05-12) + W193 (2026-05-30): 仕入先 価格変動 alert ──
    # ±5% 急騰/急落 + 在庫切れ→復活 を別 metric で表示. 急騰/急落は基準 (最初の価格) から
    # ±5% 超で遷移した瞬間に inventory_check が Discord 通知も送る (W193、圏内復帰まで再通知なし).
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
                    st.metric("📈 急騰 (+5%以上)", _surge_total,
                              help="販売停止 / 価格改定リスク。商品価格見直し推奨. 遷移時 Discord 通知あり.")
                with _pcols[1]:
                    st.metric("📉 急落 (-5%以下)", _drop_total,
                              help="仕入チャンス。即発注検討. 遷移時 Discord 通知あり.")
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
        except Exception as _epoch_e:
            logger.debug("mission epoch file write skipped: %s", _epoch_e)
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
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');
    *{{margin:0;padding:0;box-sizing:border-box;}}
    html,body{{background:#ede7da;overflow:hidden;}}

    :root {{
        --bg:        #ede7da;
        --surface:   #f2ecdf;
        --well:      #e4dcca;
        --text-main: #2a2e2a;
        --text-sub:  #5f6557;
        --text-muted:#8d927f;
        --teal:      #0e4f4b;
        --teal-h:    #156a63;
        --nominal:   #2e7d5b;
        --caution:   #b8860b;
        --alert:     #a8341b;
        --sh-out:    6px 6px 14px rgba(166,150,121,0.5),-6px -6px 14px rgba(255,255,255,0.9);
        --sh-in:     inset 3px 3px 7px rgba(166,150,121,0.5),inset -3px -3px 7px rgba(255,255,255,0.9);
        --f-ui:      'Inter', sans-serif;
        --f-mono:    'JetBrains Mono', 'Consolas', monospace;
    }}

    .console {{
        background: var(--surface);
        border-radius: 16px;
        box-shadow: var(--sh-out);
        padding: 20px 24px 18px;
        display: flex;
        gap: 28px;
        align-items: stretch;
        animation: nmFadeIn 0.4s ease both;
    }}
    @keyframes nmFadeIn {{
        from {{ opacity:0; transform:translateY(4px); }}
        to   {{ opacity:1; transform:translateY(0); }}
    }}
    @media (prefers-reduced-motion: reduce) {{
        .console {{ animation:none; }}
        .dot {{ animation:none !important; }}
    }}
    @media (max-width: 640px) {{
        .console {{ flex-direction:column; }}
    }}

    .zone-left {{
        display: flex;
        flex-direction: column;
        gap: 10px;
        min-width: 168px;
        justify-content: center;
    }}
    .brand {{
        display: flex;
        align-items: center;
        gap: 10px;
    }}
    .logo-sq {{
        width: 36px; height: 36px;
        background: var(--teal);
        border-radius: 8px;
        display: flex; align-items: center; justify-content: center;
        flex-shrink: 0;
        box-shadow: 3px 3px 8px rgba(166,150,121,0.45),-2px -2px 6px rgba(255,255,255,0.8);
    }}
    .logo-sq span {{
        font-family: var(--f-ui);
        font-size: 18px; font-weight: 700;
        color: #fff;
        line-height: 1;
        letter-spacing: -1px;
    }}
    .wordmark {{
        font-family: var(--f-ui);
        font-size: 18px; font-weight: 700;
        color: var(--teal);
        letter-spacing: -0.3px;
        line-height: 1;
    }}
    .temporal {{
        display: flex;
        flex-direction: column;
        gap: 4px;
        padding-left: 2px;
    }}
    .t-date {{
        font-family: var(--f-mono);
        font-size: 11px;
        color: var(--text-sub);
        letter-spacing: 0.5px;
        font-variant-numeric: tabular-nums;
    }}
    .t-clock {{
        font-family: var(--f-mono);
        font-size: 20px; font-weight: 500;
        color: var(--text-main);
        letter-spacing: 1px;
        font-variant-numeric: tabular-nums;
    }}
    .t-mission {{
        font-family: var(--f-mono);
        font-size: 11px; font-weight: 500;
        color: var(--teal);
        letter-spacing: 1px;
        font-variant-numeric: tabular-nums;
    }}
    .t-label {{
        font-family: var(--f-ui);
        font-size: 9px;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 1.5px;
        margin-bottom: 1px;
    }}

    .divider {{
        width: 1px;
        background: linear-gradient(180deg,transparent,rgba(166,150,121,0.35),transparent);
        align-self: stretch;
        flex-shrink: 0;
    }}

    .zone-right {{
        flex: 1;
        display: flex;
        gap: 14px;
        align-items: center;
    }}
    .well {{
        flex: 1;
        background: var(--well);
        box-shadow: var(--sh-in);
        border-radius: 12px;
        padding: 14px 16px 12px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        min-width: 0;
    }}
    .well-label {{
        font-family: var(--f-ui);
        font-size: 10px;
        color: var(--text-sub);
        text-transform: lowercase;
        letter-spacing: 0.3px;
    }}
    .well-row {{
        display: flex;
        align-items: center;
        gap: 8px;
    }}
    .well-val {{
        font-family: var(--f-mono);
        font-size: 22px; font-weight: 700;
        color: var(--text-main);
        font-variant-numeric: tabular-nums;
        line-height: 1;
    }}
    .well-sub {{
        font-family: var(--f-mono);
        font-size: 12px;
        color: var(--text-muted);
        font-variant-numeric: tabular-nums;
    }}
    .dot {{
        width: 8px; height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
        animation: nmBreath 3s ease-in-out infinite;
    }}
    .dot.nominal {{ background: var(--nominal); }}
    .dot.caution {{ background: var(--caution); }}
    .dot.alert   {{ background: var(--alert); animation-duration: 1.6s; }}
    @keyframes nmBreath {{
        0%,100% {{ opacity:1; }}
        50%      {{ opacity:0.45; }}
    }}
    .well-status {{
        font-family: var(--f-ui);
        font-size: 9px;
        text-transform: uppercase;
        letter-spacing: 1px;
    }}
    .well-status.nominal {{ color: var(--nominal); }}
    .well-status.caution {{ color: var(--caution); }}
    .well-status.alert   {{ color: var(--alert); }}
    </style>

    <div class="console">
        <div class="zone-left">
            <div class="brand">
                <div class="logo-sq"><span>M</span></div>
                <div class="wordmark">MonoDeck</div>
            </div>
            <div class="temporal">
                <div class="t-label">date</div>
                <div class="t-date">{_date_str}</div>
                <div class="t-label" style="margin-top:4px;">local time &middot; jst</div>
                <div class="t-clock" id="live-clock">{_now_str}</div>
                <div class="t-label" style="margin-top:4px;">mission elapsed</div>
                <div class="t-mission">{_mission_clock}</div>
            </div>
        </div>

        <div class="divider"></div>

        <div class="zone-right">
            <div class="well">
                <div class="well-label">受信箱 未確認</div>
                <div class="well-row">
                    <div class="well-val">{len(_dash_unconf)}</div>
                    <div class="well-sub">/{len(_dash_emails_all)}</div>
                    <div class="dot {_inbox_cls}"></div>
                </div>
                <div class="well-status {_inbox_cls}">{'attn' if _dash_unconf else 'clear'}</div>
            </div>

            <div class="well">
                <div class="well-label">高優先タスク</div>
                <div class="well-row">
                    <div class="well-val">{len(_high_tasks)}</div>
                    <div class="well-sub">/{len(active)} active</div>
                    <div class="dot {_tasks_cls}"></div>
                </div>
                <div class="well-status {_tasks_cls}">{'caution' if _high_tasks else 'nominal'}</div>
            </div>

            <div class="well">
                <div class="well-label">実行成功率 (24h)</div>
                <div class="well-row">
                    <div class="well-val">{_sr:.0f}%</div>
                    <div class="well-sub">{exec_summary['success']}/{exec_summary['total']}</div>
                    <div class="dot {_sr_cls}"></div>
                </div>
                <div class="well-status {_sr_cls}">{'alert' if _sr<80 and exec_summary['total']>0 else 'nominal'}</div>
            </div>
        </div>
    </div>

    <script>
    (function(){{
        var el=document.getElementById('live-clock');
        if(!el)return;
        function tick(){{
            var d=new Date();
            var h=String(d.getHours()).padStart(2,'0');
            var m=String(d.getMinutes()).padStart(2,'0');
            var s=String(d.getSeconds()).padStart(2,'0');
            el.textContent=h+':'+m+':'+s;
        }}
        tick();
        setInterval(tick,1000);
    }})();
    </script>
    """, height=270)

    # セクションヘッダーCSS
    _section_css = """
    <style>
    .sec-header {
        font-family: Inter, sans-serif;
        font-size: 11px; font-weight: 700;
        color: #0e4f4b;
        letter-spacing: 2.5px;
        text-transform: uppercase;
        padding: 10px 16px;
        margin: 16px 0 10px 0;
        border: 1px solid rgba(14,79,75,0.18);
        border-left: 3px solid #0e4f4b;
        border-radius: 4px;
        background: rgba(14,79,75,0.04);
        box-shadow: none;
        position: relative;
    }
    .sec-header::before {
        content: ''; position: absolute; top: 3px; bottom: 3px; left: -1px; width: 2px;
        background: #0e4f4b;
    }
    .sec-header::after {
        content: ''; position: absolute; top: 0; right: 10px; left: 60%; height: 1px;
        background: linear-gradient(90deg, transparent, rgba(14,79,75,0.2));
    }
    .task-section {
        font-family: Inter, sans-serif;
        font-size: 11px; font-weight: 400;
        color: #5f6557;
        letter-spacing: 1.5px;
        text-transform: uppercase;
        padding: 4px 0;
        border-bottom: 1px solid rgba(95,101,87,0.2);
        margin: 8px 0 4px 0;
    }
    .pri-hi { color: #a8341b; font-family: 'JetBrains Mono', monospace; font-size: 11px; }
    .pri-md { color: #b8860b; font-family: 'JetBrains Mono', monospace; font-size: 11px; }
    .mail-row {
        padding: 8px 14px;
        margin-bottom: 6px;
        background: rgba(14,79,75,0.03);
        border-radius: 0 4px 4px 0;
        border: 1px solid rgba(14,79,75,0.1);
        border-left: 3px solid rgba(14,79,75,0.35);
    }
    .mail-row.sale { border-left-color: #2e7d5b; background: rgba(46,125,91,0.04); }
    .mail-row.return { border-left-color: #a8341b; background: rgba(168,52,27,0.04); }
    .mail-row.offer { border-left-color: #b8860b; background: rgba(184,134,11,0.04); }
    .clear-status {
        font-family: 'JetBrains Mono', monospace;
        color: #2e7d5b; font-size: 12px; letter-spacing: 1px;
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
                action_color = '#b8860b'
                row_cls = 'mail-row'
                is_reply = subj.startswith('Re:')
                type_label = f'{sender} — {"返信" if is_reply else "問い合わせ"}'
            elif cat == 'sale':
                action_color = '#2e7d5b'
                row_cls = 'mail-row sale'
                type_label = '売上通知'
            elif cat == 'offer':
                action_color = '#b8860b'
                row_cls = 'mail-row offer'
                type_label = f'{sender} — オファー'
            elif cat == 'return':
                action_color = '#a8341b'
                row_cls = 'mail-row return'
                type_label = f'{sender} — 返品リクエスト'
            else:
                action_color = '#5f6557'
                row_cls = 'mail-row'
                type_label = f'{sender} — {cat}'

            # 優先度バッジ（Claude 判定）
            _pri_badge = ''
            if _pri_ai == 'urgent':
                _pri_badge = '<span style="color:#a8341b;font-size:11px;font-weight:700;margin-right:6px;">[最優先]</span>'
            elif _pri_ai == 'high':
                _pri_badge = '<span style="color:#b8860b;font-size:11px;font-weight:700;margin-right:6px;">[高]</span>'

            # 受信日時（相対＋絶対）
            _rel, _abs = _format_email_date(_em.get('date', ''))
            _date_html = ''
            if _rel or _abs:
                # 1時間以内は緑、1日以内は通常、1日以上は薄灰
                _age_color = '#2e7d5b' if '分前' in _rel or '時間前' in _rel else \
                             '#5f6557'
                _date_html = (
                    f'<span style="color:{_age_color};font-size:11px;margin-right:6px;">'
                    f'{html.escape(_rel)}'
                    + (f' <span style="color:#8d927f;">({html.escape(_abs)})</span>'
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
            link_btn = f'<a href="{_link_safe}" target="_blank" style="font-size:11px;color:#156a63;float:right;">▸ Gmailで開く</a>' if gmail_link else ''

            # バイヤー実メッセージ（Claude の buyer_message_ja、なければ body_text抽出）
            if not buyer_msg_ja and body:
                buyer_msg_ja = _extract_buyer_message(body)
            quote_html = ''
            if buyer_msg_ja:
                quote_html = (
                    f'<div style="color:#2a2e2a;font-size:12px;margin:3px 0;padding:4px 8px;'
                    f'background:rgba(14,79,75,0.06);border-radius:3px;border-left:2px solid rgba(14,79,75,0.4);">'
                    f'「{html.escape(buyer_msg_ja[:150])}」</div>'
                )

            # 要約 (Claude)
            summary_html = ''
            if summary_ja:
                summary_html = (
                    f'<div style="color:#2a2e2a;font-size:12px;margin:4px 0 2px 0;">'
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
                f'<span style="color:#5f6557;font-size:12px;">{html.escape(product or "")}</span>'
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
                    f'style="font-size:11px;color:#156a63;float:right;">▸ Gmail</a>'
                    if gmail_link else ''
                )
                _summary_line = (
                    f'<div style="color:#5f6557;font-size:11px;margin-top:2px;">'
                    f'{html.escape(summary_ja[:180])}</div>'
                ) if summary_ja else ''
                st.markdown(
                    f'<div class="mail-row" style="margin-top:-6px;margin-bottom:6px;'
                    f'border-left-color:rgba(166,150,121,0.3);background:rgba(168,196,216,0.03);">'
                    f'{_link_btn}'
                    f'<span style="color:#8d927f;font-size:10px;">{html.escape(_date_str)}</span>'
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
                    st.markdown(f"~~{task['name']}~~ <span style='color:#8d927f;font-size:11px;'>({task['completed_date']})</span>", unsafe_allow_html=True)
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

        # ── AI 活用アクション (W209, 2026-06-02) ──
        # ニュース取得タスク (06:00) の Phase 3 で深掘りされた組み込み案を表示.
        # 関連度上位 3 件 (>=60) を Opus 4.8 で深掘り → news_action_reports に永続化.
        # Q0: 0 件の時は「閾値未満」プレースホルダで空表示を避ける.
        st.markdown(
            '<div class="sec-header">AI 活用アクション</div>',
            unsafe_allow_html=True,
        )
        _action_rows: list[dict] = []
        try:
            from monitor.database import get_news_action_reports_recent
            _action_rows = get_news_action_reports_recent(days=7, limit=5)
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"get_news_action_reports_recent 失敗: {_e}")
            _action_rows = []

        if _action_rows:
            _axis_label = {
                'a': 'Claude/Agent', 'b': 'eBay応用', 'c': '関税/EC', 'd': 'スクレイピング',
            }
            _axis_color = {
                'a': '#156a63',
                'b': '#2e7d5b',
                'c': '#b8860b',
                'd': '#b35a2e',
            }
            _effort_color = {
                'S': '#2e7d5b',
                'M': '#b8860b',
                'L': '#a8341b',
            }
            _conf_label = {'high': '高', 'medium': '中', 'low': '低'}
            for _r in _action_rows:
                _ax = (_r.get('axis') or 'a').lower()
                _ax_lbl = _axis_label.get(_ax, _ax.upper())
                _ax_col = _axis_color.get(_ax, 'rgba(160,180,200,0.8)')
                _eff = (_r.get('effort_estimate') or 'M').upper()[:1]
                _eff_col = _effort_color.get(_eff, 'rgba(160,180,200,0.7)')
                _conf = (_r.get('confidence') or 'medium').lower()
                _conf_jp = _conf_label.get(_conf, _conf)
                _score = int(_r.get('relevance_score') or 0)
                _title = html.escape((_r.get('title') or '')[:80])
                _url = html.escape(_r.get('url') or '', quote=True)
                _title_link = (
                    f'<a href="{_url}" target="_blank" '
                    f'style="color:#156a63;text-decoration:none;">'
                    f'{_title}</a>'
                ) if _url else _title
                _sum = html.escape((_r.get('summary_ja') or '')[:240])
                _tgt = html.escape((_r.get('target_module') or '')[:120])
                _intg = html.escape((_r.get('integration_ja') or '')[:300])
                _ben = html.escape((_r.get('benefit_ja') or '')[:200])

                st.markdown(
                    f'<div style="border-left:2px solid {_ax_col};'
                    f'padding:8px 12px;margin-bottom:10px;'
                    f'background:rgba(166,150,121,0.08);border-radius:0 4px 4px 0;">'
                    f'<div style="display:flex;gap:6px;align-items:center;'
                    f'margin-bottom:5px;flex-wrap:wrap;">'
                    f'<span style="background:rgba(166,150,121,0.18);color:{_ax_col};'
                    f'padding:1px 6px;border-radius:3px;font-size:10px;'
                    f'letter-spacing:1px;">[{_ax.upper()}] {_ax_lbl}</span>'
                    f'<span style="background:rgba(166,150,121,0.18);color:{_eff_col};'
                    f'padding:1px 6px;border-radius:3px;font-size:10px;">'
                    f'工数 {_eff}</span>'
                    f'<span style="color:#8d927f;font-size:10px;'
                    f'margin-left:4px;">関連度 {_score} / 確度 {_conf_jp}</span>'
                    f'</div>'
                    f'<div style="font-size:13px;color:#2a2e2a;line-height:1.5;'
                    f'margin-bottom:4px;">{_sum}</div>'
                    f'<div style="font-size:11px;color:#5f6557;'
                    f'margin-bottom:3px;">'
                    f'<b>組込先</b>: {_tgt}</div>'
                    + (
                        f'<div style="font-size:11px;color:#5f6557;'
                        f'margin-bottom:3px;"><b>方法</b>: {_intg}</div>'
                        if _intg else ''
                    )
                    + (
                        f'<div style="font-size:11px;color:#2e7d5b;'
                        f'margin-bottom:4px;"><b>効果</b>: {_ben}</div>'
                        if _ben else ''
                    )
                    + f'<div style="font-size:10px;color:#156a63;">{_title_link}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            # Q0: 空表示を避け、なぜ 0 件かを明示
            st.caption(
                "本日は深掘り対象なし (関連度上位が閾値未満、または budget 到達)"
            )

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
                              published_at,
                              COALESCE(source_type, 'web') AS source_type,
                              COALESCE(source_handle, '') AS source_handle,
                              COALESCE(engagement_count, 0) AS engagement_count
                       FROM news_items
                       WHERE checked_at >= datetime('now','-3 days')
                       ORDER BY checked_at DESC,
                                CASE impact_level
                                 WHEN 'high' THEN 0
                                 WHEN 'medium' THEN 1
                                 WHEN 'low' THEN 2
                                 ELSE 3 END,
                                id DESC
                       LIMIT 20"""
                ).fetchall()]
        except Exception as _e:
            _news_db_rows = []

        if _news_db_rows:
            # 2026-06-02 (user 要望): 新着順表示。SQL 側で checked_at DESC →
            # impact → id DESC に並べ替え済 = 当日バッチが上、当日内は影響度順。
            # 旧 impact-first は高影響ニュースが数日上位固定で新鮮味が出ない問題があった。
            # Claude 要約ベース表示 (W13: ソースタグ + engagement 追加、表示数 8 件)
            _src_label = {
                'x': 'X', 'reddit': 'Reddit', 'hn': 'HN', 'web': 'Web',
            }
            _src_color = {
                'x': '#156a63',
                'reddit': '#b35a2e',
                'hn': '#b8860b',
                'web': '#5f6557',
            }
            # W224 (2026-06-05): 参照元の投稿日 (published_at) を JST + 相対表示。
            # 鮮度重視 = 古い記事 (投稿から N 日経過) はカード全体を視覚的に弱める。
            from datetime import datetime as _dt224, timezone as _tz224
            _now_utc224 = _dt224.now(_tz224.utc)
            for _n in _news_db_rows[:8]:
                _lvl = _n.get('impact_level') or 'low'
                _accent = {'high': 'rgba(168,52,27,0.55)', 'medium': 'rgba(184,134,11,0.55)',
                           'low': 'rgba(46,125,91,0.45)'}.get(_lvl, 'rgba(166,150,121,0.4)')
                _badge = {'high': '[高影響]', 'medium': '[中影響]', 'low': '[低影響]'}.get(_lvl, '')
                _st = (_n.get('source_type') or 'web').lower()
                _src_tag = _src_label.get(_st, 'Web')
                _src_tag_color = _src_color.get(_st, 'rgba(160,180,200,0.85)')
                _handle = html.escape((_n.get('source_handle') or '')[:24])
                _handle_part = f' {_handle}' if _handle else ''
                _src_html = (
                    f'<span style="background:rgba(166,150,121,0.18);color:{_src_tag_color};'
                    f'padding:1px 6px;border-radius:3px;font-size:10px;letter-spacing:1px;">'
                    f'{_src_tag}{_handle_part}</span>'
                )
                _eng = int(_n.get('engagement_count') or 0)
                _eng_html = (
                    f'<span style="color:#8d927f;font-size:10px;'
                    f'margin-left:6px;">♥ {_eng:,}</span>'
                ) if _eng > 0 else ''
                # W224: 投稿日 (published_at) を JST + 相対表示。古いほど色を弱める。
                # 二重防御: parse/format の想定外例外を握って 1 記事の異常が NEWS section
                # 全体 render を落とさないよう隔離 (chip 非表示で degrade)。
                _date_html = ''
                _card_opacity = 1.0
                try:
                    _pub_dt = _parse_news_published(_n.get('published_at') or '')
                    if _pub_dt is not None:
                        _jst_abs, _rel, _age_d = _fmt_news_freshness(_pub_dt, _now_utc224)
                        # 鮮度: 3 日以内は明るく、それ以降は段階的に弱める (下限 0.5)。
                        _date_color = '#5f6557' if _age_d < 3 else '#8d927f'
                        if _age_d >= 7:
                            _card_opacity = 0.5
                        elif _age_d >= 3:
                            _card_opacity = 0.7
                        _date_html = (
                            f'<span style="color:{_date_color};font-size:10px;margin-left:6px;" '
                            f'title="{html.escape(_jst_abs)} (JST)">{html.escape(_jst_abs)} '
                            f'({html.escape(_rel)})</span>'
                        )
                except Exception as _e224:
                    logger.warning(f"[W224] news date render skip: {_e224}")
                    _date_html = ''
                    _card_opacity = 1.0
                _src = html.escape(_n.get('source') or '')
                _sum = html.escape((_n.get('summary_ja') or _n.get('title') or '')[:200])
                _imp = html.escape((_n.get('impact_ja') or '')[:150])
                _url = html.escape(_n.get('url') or '', quote=True)
                _title_or_link = (
                    f'<a href="{_url}" target="_blank" style="color:#156a63;text-decoration:none;">'
                    f'{html.escape((_n.get("title") or "")[:80])}</a>'
                ) if _url else html.escape((_n.get('title') or '')[:80])
                st.markdown(
                    f'<div style="border-left:2px solid {_accent};padding:6px 12px;margin-bottom:8px;'
                    f'background:rgba(166,150,121,0.06);border-radius:0 4px 4px 0;opacity:{_card_opacity};">'
                    f'<div style="display:flex;gap:8px;align-items:center;margin-bottom:4px;flex-wrap:wrap;">'
                    f'{_src_html}'
                    f'<span style="color:#8d927f;font-size:10px;letter-spacing:1px;">{_badge}</span>'
                    f'{_eng_html}'
                    f'{_date_html}'
                    f'</div>'
                    f'<span style="font-size:13px;color:#2a2e2a;line-height:1.5;">{_sum}</span>'
                    + (f'<br><span style="color:#5f6557;font-size:11px;">▸ 影響: {_imp}</span>' if _imp else '')
                    + f'<br><span style="font-size:10px;color:#156a63;">{_title_or_link}</span>'
                    f'</div>', unsafe_allow_html=True)
            # 早期 return（旧 file-based 表示はスキップ）
            _news_file = None
        else:
            _news_file = Path(__file__).resolve().parent.parent / "data" / "news" / f"{_date_cls.today().isoformat()}-news.json"
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
                st.markdown('<div style="border:1px solid rgba(46,125,91,0.45);border-radius:6px;padding:8px 12px;margin-bottom:8px;background:rgba(46,125,91,0.10);">'
                    '<span style="color:#2e7d5b;font-size:11px;letter-spacing:1px;">CONSTRAINT CHECK — 技術制約に関連</span></div>', unsafe_allow_html=True)
                for _ch in _constraint_hits[:3]:
                    _n = _ch["news"]
                    _title = html.escape((_n.get("title") or "")[:55])
                    _source = html.escape(_n.get("source") or "")
                    _constraint = html.escape(_ch.get("constraint") or "")
                    st.markdown(f'<div style="border-left:2px solid rgba(46,125,91,0.45);padding:4px 10px;margin-bottom:4px;background:rgba(46,125,91,0.10);border-radius:0 4px 4px 0;">'
                        f'<span style="font-size:13px;">{_title}</span><br>'
                        f'<span style="color:#2e7d5b;font-size:11px;">▸ {_constraint}</span> '
                        f'<span style="color:#8d927f;font-size:11px;">({_source})</span></div>', unsafe_allow_html=True)

            if _high_news:
                for _n in _high_news:
                    _title = html.escape((_n.get("title") or "")[:60])
                    _source = html.escape(_n.get("source") or "")
                    _kw = html.escape(_n.get("matched_keyword") or "")
                    st.markdown(f'<div style="border-left:2px solid rgba(168,52,27,0.45);padding:4px 10px;margin-bottom:4px;background:rgba(168,52,27,0.12);border-radius:0 4px 4px 0;">'
                        f'<strong>{_title}</strong><br>'
                        f'<span style="color:#8d927f;font-size:11px;">{_source} — [{_kw}]</span></div>', unsafe_allow_html=True)

            if _med_news:
                _remaining_med = [n for n in _med_news if not any(c["news"].get("title") == n.get("title") for c in _constraint_hits)]
                for _n in _remaining_med[:3]:
                    _title = html.escape((_n.get("title") or "")[:55])
                    _source = html.escape(_n.get("source") or "")
                    _kw = html.escape(_n.get("matched_keyword") or "")
                    st.markdown(f'<div style="border-left:2px solid rgba(184,134,11,0.40);padding:4px 10px;margin-bottom:4px;background:rgba(184,134,11,0.12);border-radius:0 4px 4px 0;">'
                        f'<span style="font-size:13px;">{_title}</span><br>'
                        f'<span style="color:#8d927f;font-size:11px;">{_source} — [{_kw}]</span></div>', unsafe_allow_html=True)
                if len(_remaining_med) > 3:
                    st.caption(f"他 {len(_remaining_med)-3}件")

            if not _high_news and not _med_news and not _constraint_hits:
                st.caption("重要なニュースはありません")
        elif not _news_db_rows:
            # DB ニュース (news_items) を表示済みの時は _news_file=None でこの
            # else に落ちる → 「まだ取得されていません」誤表示の既存バグ (2026-06-02 fix)。
            # DB 行が無く file も無い時のみ「まだ」を出す。
            st.caption("本日のニュースはまだ取得されていません")

        # ── SYSTEMS ──
        st.markdown('<div class="sec-header">SYSTEMS</div>', unsafe_allow_html=True)
        company_status = get_company_status()
        if company_status['exists']:
            for name, ok in [("SECRETARY", company_status['has_secretary']), ("RESEARCH", company_status['has_research']), ("FINANCE", company_status['has_finance'])]:
                color = "#2e7d5b" if ok else "#a8341b"
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
                f'<div style="font-family:Share Tech Mono,monospace;font-size:11px;color:#b8860b;padding:3px 0;">'
                f'▲ 燃料サーチャージ更新 — {_msg}（設定タブで値を確認）</div>',
                unsafe_allow_html=True,
            )

        # 運送料PDF更新警告
        _ship_days = get_shipping_rate_days_since_update(s)
        if _ship_days is None or _ship_days >= SHIPPING_RATE_WARNING_DAYS:
            _msg = "未記録" if _ship_days is None else f"{_ship_days}日経過"
            st.markdown(
                f'<div style="font-family:Share Tech Mono,monospace;font-size:11px;color:#b8860b;padding:3px 0;">'
                f'▲ 運送料PDF更新 — {_msg}（設定タブで最新運送料PDFをアップロード）</div>',
                unsafe_allow_html=True,
            )

        # ── ROADMAP (システム改善タスク一覧) ──
        # data/system_improvements.json を唯一のソースとするデータ駆動UI。
        # ユーザーはチェック (完了化) / 削除 / 未着手戻し を画面から実行可能。
        from datetime import date as _date_today_cls
        st.markdown('<div class="sec-header" style="margin-top:18px;">ROADMAP</div>', unsafe_allow_html=True)
        _imp_path = Path(__file__).resolve().parent.parent / "data" / "system_improvements.json"  # W243: タブ分割後の parent.parent 修正

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
            "完了": ("完了", "●", "#2e7d5b", "rgba(46,125,91,0.10)"),
            "進行中": ("進行中", "◐", "#156a63", "rgba(21,106,99,0.10)"),
            "未着手": ("予定", "○", "#b8860b", "rgba(184,134,11,0.08)"),
        }

        # status の表記揺れ ("completed"/"実装中"/"保留"/"一部完了" 等) を
        # 3 区分 (完了 / 進行中 / 未着手) に正規化。これをしないと未認識 status が
        # 全て「予定」に化け、英語 completed の完了済みが残り続ける (2026-06-05 fix)。
        def _norm_status(raw) -> str:
            sx = (raw or "").strip()
            if sx.startswith("一部完了"):
                return "進行中"
            if (sx.startswith("完了") or sx.startswith("実装完了")
                    or sx in ("completed", "implemented", "done")):
                return "完了"
            if (sx.startswith("進行中") or sx.startswith("実装中")
                    or sx.startswith("着手中") or sx in ("in_progress", "wip")):
                return "進行中"
            return "未着手"

        import re as _re_roadmap

        def _roadmap_wnum(t) -> int:
            _m = _re_roadmap.search(r"W(\d+)", t.get("tag") or "")
            return int(_m.group(1)) if _m else -1

        _status_order = {"進行中": 0, "未着手": 1, "完了": 2}

        def _roadmap_sort_key(t):
            # 状態 (進行中→未着手→完了) → W 番号降順 (新しい依頼が上)。
            # 旧 priority サブグループ廃止 (W 番号が逆戻りして見える混乱を解消)。
            return (
                _status_order.get(_norm_status(t.get("status")), 1),
                -_roadmap_wnum(t),
            )

        _pending_cnt = sum(1 for t in _roadmap_all if _norm_status(t.get("status")) == "未着手")
        _wip_cnt = sum(1 for t in _roadmap_all if _norm_status(t.get("status")) == "進行中")
        _done_cnt = sum(1 for t in _roadmap_all if _norm_status(t.get("status")) == "完了")

        st.caption(
            f"残 {_pending_cnt + _wip_cnt} 件 "
            f"(未着手 {_pending_cnt} / 進行中 {_wip_cnt} / 完了 {_done_cnt})"
        )

        _col_done_cb, _col_tech_cb = st.columns(2)
        with _col_done_cb:
            _show_done_tasks = st.checkbox("完了済みも表示", key="dash_roadmap_show_done", value=False)
        with _col_tech_cb:
            # category="internal" = 内部開発タスク (テスト/migration/リファクタ等)。
            # 既定 OFF = 「実装したいシステム (business)」だけ表示 (2026-06-05 user 要望)。
            # category 未設定の項目は business 扱い (= 表示)。
            _show_internal = st.checkbox(
                "技術タスクも表示", key="dash_roadmap_show_internal", value=False,
                help="内部開発タスク (テスト/migration/リファクタ等)。OFF=実装したいシステムのみ表示",
            )

        _display_tasks = sorted(_roadmap_all, key=_roadmap_sort_key)
        if not _show_done_tasks:
            _display_tasks = [t for t in _display_tasks if _norm_status(t.get("status")) != "完了"]
        if not _show_internal:
            _display_tasks = [
                t for t in _display_tasks if (t.get("category") or "business") != "internal"
            ]

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
            _status = _norm_status(_t.get("status"))
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
                    f'color:#2a2e2a;display:flex;align-items:center;gap:8px;">'
                    f'<span style="color:{_fg};">{_icon}</span>'
                    f'{_tag_html}'
                    f'<span style="flex:1;">{html.escape(_title)}</span>'
                    f'<span style="color:#8d927f;font-size:10px;margin-right:6px;">[{html.escape(_priority)}]</span>'
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
