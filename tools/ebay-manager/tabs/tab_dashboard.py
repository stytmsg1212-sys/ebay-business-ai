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

    # ── W24: Research 脳 morning brief セクション (本日分があれば表示) ──
    try:
        from tasks.task_research_morning_brief import get_today_brief as _get_today_brief
        _today_brief = _get_today_brief()
        if _today_brief:
            with st.container(border=True):
                st.markdown(
                    '<div style="font-size:11px;color:#a89d8a;letter-spacing:2px;'
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
                    st.metric("🔴 在庫切れ (0 個)", len(_inv_zero),
                              help="商品管理タブで在庫補充 + eBay 反映を")
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
                'a': 'rgba(120,200,255,0.9)',
                'b': 'rgba(118,255,3,0.85)',
                'c': 'rgba(240,200,48,0.85)',
                'd': 'rgba(255,140,80,0.9)',
            }
            _effort_color = {
                'S': 'rgba(118,255,3,0.7)',
                'M': 'rgba(240,200,48,0.7)',
                'L': 'rgba(240,64,80,0.7)',
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
                    f'style="color:rgba(120,200,255,0.85);text-decoration:none;">'
                    f'{_title}</a>'
                ) if _url else _title
                _sum = html.escape((_r.get('summary_ja') or '')[:240])
                _tgt = html.escape((_r.get('target_module') or '')[:120])
                _intg = html.escape((_r.get('integration_ja') or '')[:300])
                _ben = html.escape((_r.get('benefit_ja') or '')[:200])

                st.markdown(
                    f'<div style="border-left:2px solid {_ax_col};'
                    f'padding:8px 12px;margin-bottom:10px;'
                    f'background:rgba(80,120,180,0.04);border-radius:0 4px 4px 0;">'
                    f'<div style="display:flex;gap:6px;align-items:center;'
                    f'margin-bottom:5px;flex-wrap:wrap;">'
                    f'<span style="background:rgba(0,0,0,0.3);color:{_ax_col};'
                    f'padding:1px 6px;border-radius:3px;font-size:10px;'
                    f'letter-spacing:1px;">[{_ax.upper()}] {_ax_lbl}</span>'
                    f'<span style="background:rgba(0,0,0,0.3);color:{_eff_col};'
                    f'padding:1px 6px;border-radius:3px;font-size:10px;">'
                    f'工数 {_eff}</span>'
                    f'<span style="color:rgba(180,220,255,0.45);font-size:10px;'
                    f'margin-left:4px;">関連度 {_score} / 確度 {_conf_jp}</span>'
                    f'</div>'
                    f'<div style="font-size:13px;color:#e0ecfa;line-height:1.5;'
                    f'margin-bottom:4px;">{_sum}</div>'
                    f'<div style="font-size:11px;color:rgba(160,220,255,0.75);'
                    f'margin-bottom:3px;">'
                    f'<b>組込先</b>: {_tgt}</div>'
                    + (
                        f'<div style="font-size:11px;color:rgba(160,220,255,0.65);'
                        f'margin-bottom:3px;"><b>方法</b>: {_intg}</div>'
                        if _intg else ''
                    )
                    + (
                        f'<div style="font-size:11px;color:rgba(118,255,3,0.7);'
                        f'margin-bottom:4px;"><b>効果</b>: {_ben}</div>'
                        if _ben else ''
                    )
                    + f'<div style="font-size:10px;color:#5a7a96;">{_title_link}</div>'
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
                'x': 'rgba(120,200,255,0.9)',
                'reddit': 'rgba(255,140,80,0.9)',
                'hn': 'rgba(255,180,60,0.9)',
                'web': 'rgba(160,180,200,0.85)',
            }
            # W224 (2026-06-05): 参照元の投稿日 (published_at) を JST + 相対表示。
            # 鮮度重視 = 古い記事 (投稿から N 日経過) はカード全体を視覚的に弱める。
            from datetime import datetime as _dt224, timezone as _tz224
            _now_utc224 = _dt224.now(_tz224.utc)
            for _n in _news_db_rows[:8]:
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
                        _date_color = 'rgba(150,210,255,0.75)' if _age_d < 3 else 'rgba(150,170,190,0.5)'
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
                    f'<a href="{_url}" target="_blank" style="color:rgba(120,200,255,0.9);text-decoration:none;">'
                    f'{html.escape((_n.get("title") or "")[:80])}</a>'
                ) if _url else html.escape((_n.get('title') or '')[:80])
                st.markdown(
                    f'<div style="border-left:2px solid {_accent};padding:6px 12px;margin-bottom:8px;'
                    f'background:rgba(80,120,180,0.03);border-radius:0 4px 4px 0;opacity:{_card_opacity};">'
                    f'<div style="display:flex;gap:8px;align-items:center;margin-bottom:4px;flex-wrap:wrap;">'
                    f'{_src_html}'
                    f'<span style="color:rgba(180,220,255,0.55);font-size:10px;letter-spacing:1px;">{_badge}</span>'
                    f'{_eng_html}'
                    f'{_date_html}'
                    f'</div>'
                    f'<span style="font-size:13px;color:#e0ecfa;line-height:1.5;">{_sum}</span>'
                    + (f'<br><span style="color:rgba(160,220,255,0.7);font-size:11px;">▸ 影響: {_imp}</span>' if _imp else '')
                    + f'<br><span style="font-size:10px;color:#5a7a96;">{_title_or_link}</span>'
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
            "完了": ("完了", "●", "rgba(112,240,128,0.75)", "rgba(112,240,128,0.10)"),
            "進行中": ("進行中", "◐", "rgba(77,217,240,0.85)", "rgba(77,217,240,0.10)"),
            "未着手": ("予定", "○", "rgba(240,200,48,0.75)", "rgba(240,200,48,0.08)"),
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
