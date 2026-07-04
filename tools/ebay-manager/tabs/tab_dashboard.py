#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DASHBOARD タブ (#43 2026-07-04 全面刷新)。

data-viz-lab 学習原則 (色3色・赤は要対応のみ・Z型・粒度統一・全幅引き伸ばし禁止) に
基づき、旧版 (Interstellar Cockpit JS コンソール + MORNING BRIEF + フルリスト羅列)
から専有面積を約半分に圧縮した新レイアウトへ全面改修 (user 承認: 「After 案 +
さらに半分の専有範囲 (2 カラム詰め込み)」)。

新レイアウト:
  1. ヘッダ行 (~50px, 1行): 左 = ⚠ 要対応 N (通関未送信+依頼ボード awaiting_check+
     通知センター未読 error/critical+緊急メール の合算) + 内訳導線。右 = KPI 3 つ
     (本日売上/今週売上/システム24h完了率)。
  2. 左右 2 カラム: 左 = 🔔 通知センター (既存 render 呼出、不変) + 緊急メール
     (urgent のみ)。右 = 📦 在庫・価格の要点 (件数+上位3件+導線、一覧は出さない) +
     フッタ折りたたみ行 (TASKS / 部署 / 燃料 / ▸ニュース / ▸参考メール、既定閉)。

撤去 (旧セクション):
  - MORNING BRIEF (Research 脳) → 通知センターの research 通知に委ねる
  - Interstellar Cockpit JS コンソール (st.components.v1.html) → 軽量ヘッダ行に置換
  - AI 活用アクション の全文カード → ▸ニュース 折りたたみ内の 1 行リンクへ圧縮
  - ROADMAP → app.py の「システム運用」統合タブの 4 番目のサブタブへ移設
    (本ファイルの `render_roadmap_section()` を呼出す、render 本体はここに残置)

同梱ヘルパー (app.py top-level から移動、単一タブ専用): _cd_dash_emails,
_cd_active_tasks, _cd_notification_unread_counts, _cd_notification_rows,
_cd_dashboard_kpis, _cd_stock_price_summary
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


@st.cache_data(ttl=3, show_spinner=False)
def _cd_dashboard_kpis(db_version: int) -> dict:
    """#43: ヘッダ行 KPI (本日/今週売上・24h タスク成功率・要対応内訳)。"""
    from monitor.database import get_dashboard_kpis
    return get_dashboard_kpis()


@st.cache_data(ttl=3, show_spinner=False)
def _cd_stock_price_summary(db_version: int) -> dict:
    """#43: 「在庫・価格の要点」用の集計。一覧は出さず件数+上位3件のみ返す。

    supply_risk の定義は `monitor.database.get_nav_badge_counts()['supply_risk']`
    と完全一致させている (仕入先在庫切れ/ページなし、ebay* SKU、risk_confirmed=0)。
    定義変更時は両方を cascade 更新すること (cascade-update.md)。
    価格急変 (surge/drop) は monitored_items 由来で 在庫監視 タブには表示されない
    dashboard 専有情報のため、件数のみ表示し導線は付けない (一覧なし、詳細は無し)。
    """
    from monitor.database import get_conn
    result = {
        "supply_risk_count": 0, "supply_risk_top3": [],
        "price_surge": 0, "price_drop": 0,
    }
    try:
        with get_conn() as c:
            result["supply_risk_count"] = int(c.execute(
                """SELECT COUNT(*) FROM ebay_listings
                   WHERE quantity_ebay >= 1
                     AND COALESCE(is_ended, 0) = 0
                     AND source_status IN ('在庫無', 'ページなし')
                     AND sku GLOB 'ebay*'
                     AND COALESCE(risk_confirmed, 0) = 0"""
            ).fetchone()[0] or 0)
            result["supply_risk_top3"] = [
                dict(r) for r in c.execute(
                    """SELECT ebay_item_id, title FROM ebay_listings
                       WHERE quantity_ebay >= 1
                         AND COALESCE(is_ended, 0) = 0
                         AND source_status IN ('在庫無', 'ページなし')
                         AND sku GLOB 'ebay*'
                         AND COALESCE(risk_confirmed, 0) = 0
                       ORDER BY ebay_item_id LIMIT 3"""
                ).fetchall()
            ]
            result["price_surge"] = int(c.execute(
                "SELECT COUNT(*) FROM monitored_items "
                "WHERE is_active=1 AND price_alert_state='surge'"
            ).fetchone()[0] or 0)
            result["price_drop"] = int(c.execute(
                "SELECT COUNT(*) FROM monitored_items "
                "WHERE is_active=1 AND price_alert_state='drop'"
            ).fetchone()[0] or 0)
    except Exception as e:  # noqa: BLE001 — 1 セクション失敗で DASHBOARD 全体を落とさない
        logger.warning(f"[#43] 在庫・価格 要点 集計失敗: {e}")
    return result


def _dash_nav(page: str, group: str) -> None:
    """タブ内ナビゲーション (W292 流儀: _w134_sel + _w217a_cat_view を設定して rerun)。"""
    st.session_state["_w134_sel"] = page
    st.session_state["_w217a_cat_view"] = group
    st.rerun()


# INBOX urgent 判定 (2026-05-21 Phase A 由来)。header の「⚠ 要対応」合算と
# 緊急メール一覧の両方から呼ばれる単一の定義 (二重化によるロジックずれ防止)。
_URGENT_PRIORITIES = {'urgent', 'high'}
_URGENT_CATEGORIES = {'buyer_message', 'sale', 'offer', 'return', 'customs_request'}
_INBOX_EXCLUDED_CATEGORIES = {'supplier_purchase', 'sale', 'listing_notification'}


def _is_urgent_email(em: dict) -> bool:
    cat_rule = em.get('category', 'other')
    cat_ai = em.get('category_ai') or ''
    if cat_rule in _INBOX_EXCLUDED_CATEGORIES or cat_ai in _INBOX_EXCLUDED_CATEGORIES:
        return False
    pri_ai = em.get('priority_ai') or ''
    cat = cat_ai or cat_rule
    if pri_ai:
        return pri_ai in _URGENT_PRIORITIES
    return cat in _URGENT_CATEGORIES


def render_roadmap_section() -> None:
    """ROADMAP (システム改善タスク一覧)。#43 (2026-07-04) で DASHBOARD から
    「システム運用」統合タブの 4 番目のサブタブへ移設 (render 本体は不変、
    呼出元のみ変更)。data/system_improvements.json を唯一のソースとする
    データ駆動 UI。ユーザーはチェック (完了化) / 削除 / 未着手戻し を画面から実行可能。
    """
    import json
    import re as _re_roadmap
    from datetime import date as _date_today_cls

    st.markdown(
        """
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
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="sec-header">ROADMAP</div>', unsafe_allow_html=True)
    _imp_path = Path(__file__).resolve().parent.parent / "data" / "system_improvements.json"

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


def render_dashboard_tab(s: dict) -> None:
    # W221 Tier2 fix (2026-06-05): app.py top-level import をグローバル参照していた
    # 名前を関数内 lazy import で補完 (抽出漏れ修正、render 実行時 NameError 防止)。
    from company_integration import complete_task, get_archived_tasks, get_company_status, get_today_routine_result
    from fuel_surcharge_manager import UPDATE_WARNING_DAYS, get_days_since_last_update
    from scheduler_integration import get_latest_execution_logs
    from shipping_rate_manager import SHIPPING_RATE_WARNING_DAYS, get_shipping_rate_days_since_update
    import sqlite3
    from ui_cache import bump_db_version, get_db_version
    import re as _re_dash
    from monitor.database import set_email_confirmed

    _dbv = get_db_version()

    # 軽量 CSS (#43: 巨大 JS コンソールを撤去し 11-12px / 行高 22-26px の
    # コンパクトな装飾に統一。全幅ストレッチはしない)。
    st.markdown(
        """
        <style>
        .dash-header {
            display:flex; justify-content:space-between; align-items:center;
            flex-wrap:wrap; gap:6px 18px;
            padding:8px 14px; margin-bottom:10px; border-radius:6px;
            font-family:'Inter',sans-serif;
        }
        .dash-header.alert { background:rgba(168,52,27,0.08); border:1px solid rgba(168,52,27,0.35); }
        .dash-header.clear { background:rgba(46,125,91,0.06); border:1px solid rgba(46,125,91,0.25); }
        .dash-badge { font-size:13px; font-weight:700; }
        .dash-pill { font-size:11px; color:#5f6557; margin-left:10px; }
        .dash-kpi { font-family:'JetBrains Mono',monospace; font-size:11px; color:#5f6557; margin-left:14px; }
        .dash-kpi b { font-size:13px; color:#2a2e2a; }
        .mail-row {
            padding: 6px 12px; margin-bottom: 5px; font-size: 12px; line-height: 1.4;
            background: #ffffff; border-radius: 0 4px 4px 0;
            border: 1px solid rgba(14,79,75,0.1); border-left: 3px solid rgba(14,79,75,0.35);
        }
        /* #43-relayout 差戻し (2026-07-04): 赤ベタ背景をやめ左ボーダー色のみで
           優先度を示す (最優先=赤/高=橙)。背景は全件 白 (.mail-row 既定) に統一。 */
        .mail-row.pri-urgent { border-left-color: #a8341b; }
        .mail-row.pri-high { border-left-color: #b8860b; }
        .task-section {
            font-family: Inter, sans-serif; font-size: 11px; font-weight: 400;
            color: #5f6557; letter-spacing: 1.5px; text-transform: uppercase;
            padding: 4px 0; border-bottom: 1px solid rgba(95,101,87,0.2); margin: 8px 0 4px 0;
        }
        .pri-hi { color: #a8341b; font-family: 'JetBrains Mono', monospace; font-size: 11px; }
        .pri-md { color: #b8860b; font-family: 'JetBrains Mono', monospace; font-size: 11px; }
        .clear-status { font-family: 'JetBrains Mono', monospace; color: #2e7d5b; font-size: 12px; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ================= ヘッダ行 (~50px、要対応 + KPI3つ) =================
    kpis = _cd_dashboard_kpis(_dbv)
    _dash_emails_all = _cd_dash_emails(_dbv, 50)
    _urgent_emails = [em for em in _dash_emails_all if _is_urgent_email(em)]
    _urgent_n = len(_urgent_emails)
    _action_total = kpis["action_needed_total"] + _urgent_n

    _header_cls = "alert" if _action_total > 0 else "clear"
    if _action_total > 0:
        _badge_html = f'<span class="dash-badge" style="color:#a8341b;">⚠ 要対応 {_action_total}</span>'
        _pill_parts = []
        if kpis["customs_pending"] > 0:
            _pill_parts.append(f'通関 {kpis["customs_pending"]}')
        if _urgent_n > 0:
            _pill_parts.append(f'緊急メール {_urgent_n}')
        if kpis["request_board_awaiting"] > 0:
            _pill_parts.append(f'依頼ボード {kpis["request_board_awaiting"]}')
        if kpis["notification_unread_critical"] > 0:
            _pill_parts.append(f'通知 {kpis["notification_unread_critical"]}')
        _pill_html = f'<span class="dash-pill">{html.escape(" ・ ".join(_pill_parts))}</span>' if _pill_parts else ''
    else:
        _badge_html = '<span class="dash-badge" style="color:#2e7d5b;">✓ 要対応なし</span>'
        _pill_html = ''

    _today_usd = kpis["today_sales_usd"]
    _today_cnt = kpis["today_sales_count"]
    _week_usd = kpis["week_sales_usd"]
    _week_cnt = kpis["week_sales_count"]
    _task_rate = kpis["task_24h_rate"]
    _task_rate_html = (
        f'{_task_rate:.0f}% ({kpis["task_24h_completed"]}/{kpis["task_24h_completed"] + kpis["task_24h_failed"]})'
        if _task_rate is not None else "実行なし"
    )

    st.markdown(
        f'<div class="dash-header {_header_cls}">'
        f'<div>{_badge_html}{_pill_html}</div>'
        f'<div>'
        f'<span class="dash-kpi">本日売上 <b>${_today_usd:,.2f}</b> / {_today_cnt}件</span>'
        f'<span class="dash-kpi">今週売上 <b>${_week_usd:,.2f}</b> / {_week_cnt}件</span>'
        f'<span class="dash-kpi">システム24h <b>{_task_rate_html}</b></span>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # 要対応の内訳のうち他タブへ遷移可能なものだけ小型ボタンで導線を出す
    # (通知/緊急メールは DASHBOARD 内に表示があるため遷移不要、W292 流儀)。
    if kpis["customs_pending"] > 0 or kpis["request_board_awaiting"] > 0:
        _hb1, _hb2, _hb_sp = st.columns([1, 1, 3])
        with _hb1:
            if kpis["customs_pending"] > 0:
                if st.button(f"⚖ 通関対応 {kpis['customs_pending']} →", key="dash_goto_customs"):
                    _dash_nav("通関対応", "⏺ 出品")
        with _hb2:
            if kpis["request_board_awaiting"] > 0:
                if st.button(f"📋 依頼ボード {kpis['request_board_awaiting']} →", key="dash_goto_board"):
                    _dash_nav("依頼ボード", "★ 毎日")

    # ================= 通知センター データ取得 (P1/P2 分割、列レンダリング前に一括 fetch) =================
    # #43-relayout (2026-07-04 user フィードバック「開かないと確認できない」対応):
    # P1 (開かずフラット表示) = 注文/要対応/競合・最安値/在庫。P2 (閉じておく、
    # クリックで開く) = キーワード新着/リサーチ/価格/システム運用/その他。
    # 左右カラムで別々に render するため、fetch は列分岐の前に 1 回だけ行う
    # (Q0: silent 非表示にせず、未整備時は「準備中」を両カラムに明示 fallback)。
    _P1_NOTIF_CATEGORIES = {"order", "action_required", "rival", "inventory"}
    _NC_SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    _nc_available = False
    _nc_unread_counts: dict = {}
    _nc_total_unread = 0
    _nc_p1_rows: list = []
    _nc_p2_cats: list = []
    _nc_read_recent: list = []
    _nc_render_row = _nc_cat_emoji = _nc_cat_label = _nc_get_nav_target = None
    _nc_mark_read = _nc_mark_category_read = _nc_mark_all_read = None
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

        _nc_available = True
        _nc_unread_counts = _cd_notification_unread_counts(_dbv)
        _nc_total_unread = sum(_nc_unread_counts.values())

        if _nc_total_unread > 0:
            _nc_all_unread = _cd_notification_rows(_dbv, True, None, 200)
            _nc_p1_rows = [
                r for r in _nc_all_unread
                if (r.get("category") or "") in _P1_NOTIF_CATEGORIES
            ]
            # get_notifications() は created_at DESC で返す。severity のみで
            # stable sort すれば同一 severity 内の新しい順は保持される。
            _nc_p1_rows.sort(key=lambda r: _NC_SEVERITY_RANK.get(r.get("severity") or "info", 3))

        _nc_p2_cats = sorted(
            (
                (cat, cnt) for cat, cnt in _nc_unread_counts.items()
                if cnt > 0 and cat not in _P1_NOTIF_CATEGORIES
            ),
            key=lambda kv: (-kv[1], kv[0]),
        )

        # 既読 (7日) — 空セクションは非表示 (実機 fb「既読 (7日) (0)」ノイズ根治)。
        _nc_recent_rows = _cd_notification_rows(_dbv, False, None, 100)
        _nc_read_recent = [
            _r for _r in _nc_recent_rows
            if _r.get("read_at") and _nc_is_within_days(_r.get("created_at") or "", 7)
        ]
    except (ImportError, sqlite3.Error) as _nc_e:
        logger.info(f"通知センター 表示 skip (未整備): {_nc_e}")

    # 通知センター 1 行 HTML の CSS (_NC_CSS) を DASHBOARD で 1 回だけ inject。
    # 仕上げ 2026-07-04: `include_css=False` (既定) で全 row 呼び出していたため、
    # nc-line CSS が DOM に注入されず flex/nowrap/ellipsis が全て無効化 → 実機で
    # 2-3 行折り返し + hover tooltip 効かず。以下 dict flag を row 描画の最初の
    # `_nc_render_row` 呼出しに `include_css=True` を渡すゲートとして使う
    # (`_supplier_card_html.py` の慣例と同じパターン)。
    _nc_css_state = {"emitted": False}

    def _render_nc_row(_n: dict) -> str:
        _first = not _nc_css_state["emitted"]
        _nc_css_state["emitted"] = True
        return _nc_render_row(_n, include_css=_first) if _nc_render_row else ""

    def _render_nc_action_row(_n: dict, key_prefix: str) -> None:
        """1 通知行 + アクションボタンを一貫レイアウトで描画 (P1/P2 共通ヘルパ)。

        #43-relayout 差戻し (2026-07-04):
        - 旧 [12,1,1] 比率は側カラムが ~7% 幅しかなく、ボタン文字列が折り返して
          ✓ ボタンと縦積みに崩れて見える不具合の原因だった → [8,2,2]/[10,2] へ拡幅。
        - `vertical_alignment="center"` (Streamlit 1.56 対応、tab_today_tasks.py で
          既採用の流儀) で HTML 行 (~26px) と Streamlit ボタン (~38px) の高さ差を
          縦中央合わせし、右端に横並び 1 行で揃える。
        - 「開く」は意味不明 (fb) のため「詳細」に統一。
        """
        _nav = _nc_get_nav_target(_n.get("link_target") or _n.get("category"))
        if _nav:
            _c_body, _c_detail, _c_read = st.columns([8, 2, 2], vertical_alignment="center")
        else:
            _c_body, _c_read = st.columns([10, 2], vertical_alignment="center")
            _c_detail = None
        with _c_body:
            st.markdown(_render_nc_row(_n), unsafe_allow_html=True)
        if _c_detail is not None:
            with _c_detail:
                if st.button("詳細", key=f"{key_prefix}_detail_{_n['id']}"):
                    st.session_state["_w134_sel"] = _nav[0]
                    st.session_state["_w217a_cat_view"] = _nav[1]
                    _nc_mark_read([_n["id"]])
                    bump_db_version()
                    st.rerun()
        with _c_read:
            if st.button("✓", key=f"{key_prefix}_read_{_n['id']}"):
                _nc_mark_read([_n["id"]])
                bump_db_version()
                st.rerun()

    # ================= 左右 2 カラム =================
    col_left, col_right = st.columns(2, gap="large")

    with col_left:
        # ── 🔔 通知センター (重要度 P1、開かずフラット表示) ──
        # #43-relayout 差戻し: 「全て既読にする」ボタンを見出し行右端の小型ボタンへ移動
        # (旧: 見出しの下に単独行でフル幅表示、視覚ノイズだった)。
        _nc_h_l, _nc_h_r = st.columns([3, 2])
        with _nc_h_l:
            st.markdown("**🔔 通知センター**")
        if _nc_available and _nc_total_unread > 0:
            with _nc_h_r:
                if st.button(f"✓ 全て既読 ({_nc_total_unread})", key="nc_mark_all_read"):
                    _nc_mark_all_read()
                    bump_db_version()
                    st.rerun()
        if not _nc_available:
            st.caption("準備中 (基盤実装待ち)")
        elif _nc_total_unread == 0:
            st.markdown('<span class="clear-status">✓ 新しい通知はありません</span>', unsafe_allow_html=True)
        else:
            if not _nc_p1_rows:
                st.caption("重要カテゴリ (注文/要対応/競合・最安値/在庫) の未読はありません")
            else:
                _NC_P1_SHOW = 10
                for _nc_n in _nc_p1_rows[:_NC_P1_SHOW]:
                    _render_nc_action_row(_nc_n, "nc_p1")
                _nc_p1_over = len(_nc_p1_rows) - _NC_P1_SHOW
                if _nc_p1_over > 0:
                    with st.expander(f"他 {_nc_p1_over} 件 (重要通知の続き)", expanded=False):
                        for _nc_n in _nc_p1_rows[_NC_P1_SHOW:]:
                            st.markdown(_render_nc_row(_nc_n), unsafe_allow_html=True)

        # ── 緊急メール (urgent のみ、#43 で INBOX を圧縮、#43-relayout で 1件2-3行に再圧縮) ──
        st.markdown("**📨 緊急メール**")

        def _extract_buyer_message(body: str) -> str:
            """メール本文からバイヤーの実際のメッセージを抽出"""
            if not body:
                return ""
            m = _re_dash.search(r'New message:\s*(.+?)(?:\n|$)', body)
            if m:
                msg = m.group(1).strip()
                if msg and len(msg) > 2:
                    return msg[:80]
            for line in body.split('\n'):
                line = line.strip()
                if line and len(line) > 5 and not line.startswith('New message from') and 'Reply' not in line:
                    return line[:80]
            return ""

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
                else:
                    rel = f"{int(total_sec//86400)}日前"
                try:
                    from datetime import timedelta as _td
                    dt_local = dt.astimezone(_tz(_td(hours=9)))
                    abs_str = dt_local.strftime("%m/%d %H:%M")
                except Exception:
                    abs_str = dt.strftime("%m/%d %H:%M")
                return rel, abs_str
            except Exception:
                return "", (date_str[:20] if date_str else "")

        _inbox_confirm_ids = []

        def _render_urgent_email(_em: dict) -> None:
            """緊急メール 1 件をコンパクト 2-3 行 (checkbox + meta + 本文1行要約) で描画。

            #43-relayout (2026-07-04): summary/quote/action を個別 div で並べる旧実装は
            1 件あたり 4-5 行に肥大化していた。本文は「バイヤーの声 → 対応」を 1 行に
            結合し ellipsis で強制 1 行化することで、件数が増えてもカード高さが揃う。
            """
            cat = (_em.get('category_ai') or '') or _em.get('category', 'other')
            subj = _em.get('subject', '')
            sender = _em.get('sender', '').split('<')[0].strip().strip('"').replace('eBay - ', '')
            gmail_id = _em.get('gmail_id', '')
            gmail_link = f"https://mail.google.com/mail/u/0/#inbox/{gmail_id}" if gmail_id else ""
            body = _em.get('body_text', '')
            summary_ja = (_em.get('summary_ja') or '').strip()
            action_ja = (_em.get('action_ja') or '').strip()
            buyer_msg_ja = (_em.get('buyer_message_ja') or '').strip()
            _pri_ai = _em.get('priority_ai') or ''

            _pm = _re_dash.search(r'about (.+?)(?:\s*#\d|$)', subj)
            product = _pm.group(1).strip()[:40] if _pm else ''

            if cat == 'buyer_message':
                is_reply = subj.startswith('Re:')
                type_label = f'{sender} — {"返信" if is_reply else "問い合わせ"}'
            elif cat == 'offer':
                type_label = f'{sender} — オファー'
            elif cat == 'return':
                type_label = f'{sender} — 返品リクエスト'
            else:
                type_label = f'{sender} — {cat}'

            # #43-relayout 差戻し (2026-07-04): 左ボーダー色/背景は「優先度」基準に
            # 統一 (最優先=赤/高=橙)。優先度未設定時のみカテゴリ由来の accent に
            # フォールバック (背景は常に白、赤ベタ背景を廃止)。
            if _pri_ai == 'urgent':
                action_color = '#a8341b'
                row_cls = 'mail-row pri-urgent'
            elif _pri_ai == 'high':
                action_color = '#b8860b'
                row_cls = 'mail-row pri-high'
            elif cat == 'return':
                action_color = '#a8341b'
                row_cls = 'mail-row pri-urgent'
            elif cat == 'offer':
                action_color = '#b8860b'
                row_cls = 'mail-row pri-high'
            else:
                action_color = '#5f6557'
                row_cls = 'mail-row'

            _pri_badge = ''
            if _pri_ai == 'urgent':
                _pri_badge = '<span style="color:#a8341b;font-size:11px;font-weight:700;margin-right:6px;">[最優先]</span>'
            elif _pri_ai == 'high':
                _pri_badge = '<span style="color:#b8860b;font-size:11px;font-weight:700;margin-right:6px;">[高]</span>'

            _rel, _abs = _format_email_date(_em.get('date', ''))
            _date_html = ''
            if _rel or _abs:
                _age_color = '#2e7d5b' if '分前' in _rel or '時間前' in _rel else '#5f6557'
                _date_html = (
                    f'<span style="color:{_age_color};font-size:11px;margin-right:6px;">'
                    f'{html.escape(_rel)}'
                    + (f' <span style="color:#8d927f;">({html.escape(_abs)})</span>' if _abs else '')
                    + '</span>'
                )

            _chk = st.checkbox(
                f"{type_label} — {product[:25]}" if product else type_label,
                key=f"inbox_{gmail_id}",
            )
            if _chk:
                _inbox_confirm_ids.append(gmail_id)

            _link_safe = html.escape(gmail_link or "", quote=True)
            link_btn = (
                f'<a href="{_link_safe}" target="_blank" '
                f'style="font-size:11px;color:#156a63;text-decoration:none;flex-shrink:0;'
                f'margin-left:auto;padding-left:8px;">▸ Gmailで開く</a>'
                if gmail_link else ''
            )

            if not buyer_msg_ja and body:
                buyer_msg_ja = _extract_buyer_message(body)

            # 本文要約: バイヤーの声 (あれば) + 対応 を 1 行へ結合 (ellipsis で強制 1 行)。
            # #43-relayout 仕上げ (2026-07-04): メタ行も flex + nowrap で 1 行に強制。
            # 全体で「メタ行 + 本文 1 行 = 2 行以内」を厳守。
            _best_body = buyer_msg_ja or summary_ja or ''
            _combo_text = (f"「{_best_body[:90]}」 " if _best_body else '') + f"▸ {action_ja or '対応を検討してください'}"
            _combo_full_tt = html.escape(
                (buyer_msg_ja or summary_ja or '') + ' / ' + (action_ja or '対応を検討してください'),
                quote=True,
            )
            _combo_html = (
                f'<div title="{_combo_full_tt}" '
                f'style="color:{action_color};font-size:12px;line-height:1.4;white-space:nowrap;'
                f'overflow:hidden;text-overflow:ellipsis;">{html.escape(_combo_text)}</div>'
            )

            _product_html = (
                f'<span style="color:#5f6557;font-size:12px;flex:1 1 auto;min-width:0;'
                f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
                f'{html.escape(product or "")}</span>'
            )
            _meta_row = (
                f'<div style="display:flex;align-items:center;flex-wrap:nowrap;gap:6px;'
                f'white-space:nowrap;overflow:hidden;">'
                f'{_pri_badge}{_date_html}{_product_html}{link_btn}'
                f'</div>'
            )
            st.markdown(
                f'<div class="{row_cls}" style="margin-top:-8px;">'
                f'{_meta_row}'
                f'{_combo_html}'
                f'</div>', unsafe_allow_html=True)

        if not _urgent_emails:
            st.markdown('<span class="clear-status">✓ 緊急メールなし</span>', unsafe_allow_html=True)
        else:
            _URGENT_SHOW = 3
            for _em in _urgent_emails[:_URGENT_SHOW]:
                _render_urgent_email(_em)
            _urgent_rest = _urgent_emails[_URGENT_SHOW:]
            if _urgent_rest:
                with st.expander(f"他 {len(_urgent_rest)} 件の緊急メール", expanded=False):
                    for _em in _urgent_rest:
                        _render_urgent_email(_em)

        if _inbox_confirm_ids:
            if st.button(f"{len(_inbox_confirm_ids)}件を確認済みにする", type="primary", key="inbox_confirm"):
                set_email_confirmed(_inbox_confirm_ids)
                bump_db_version()  # W134 Step2: 書込後 read-cache 無効化
                st.rerun()

    with col_right:
        # ── 在庫・価格の要点 (#43: 一覧は出さず件数+上位3件+導線のみ) ──
        # #43-relayout: 本文とボタンを st.columns で左右に分け縦積みによる
        # 重なり (実機 screenshot 実証) を解消。
        st.markdown("**📦 在庫・価格の要点**")
        _sp = _cd_stock_price_summary(_dbv)
        if _sp["supply_risk_count"] > 0:
            # #43-relayout 差戻し (2026-07-04): ボタンは見出し行と同じ行内の右端に
            # 揃える (旧: 3行分の caption と並ぶ 1 ボタンで浮いて見えた不具合)。
            # top3 一覧は行の下に全幅で表示する。
            _c_sr_txt, _c_sr_btn = st.columns([3, 1], vertical_alignment="center")
            with _c_sr_txt:
                st.markdown(f"🔴 仕入先 在庫切れ **{_sp['supply_risk_count']}件**")
            with _c_sr_btn:
                if st.button("在庫監視で対応 →", key="dash_goto_inventory"):
                    _dash_nav("在庫監視", "★ 毎日")
            for _r in _sp["supply_risk_top3"]:
                _title_disp = (_r.get('title') or _r.get('ebay_item_id') or '?')[:40]
                st.caption(f"・{_title_disp}")
        else:
            st.markdown('<span class="clear-status">✓ 仕入先在庫切れなし</span>', unsafe_allow_html=True)

        # #43 fix: 自社在庫 (stock% SKU) の 切れ / 未入力 (旧 DASHBOARD 復元、1 行)。
        _own_out = kpis.get("own_stock_out", 0)
        _own_unset = kpis.get("own_stock_unset", 0)
        if _own_out or _own_unset:
            _c_os_txt, _c_os_btn = st.columns([3, 1], vertical_alignment="center")
            with _c_os_txt:
                st.markdown(
                    f'<div style="font-size:12px;line-height:1.5;">'
                    f'🏠 自社在庫: 切れ <b>{_own_out}</b> / 未入力 <b>{_own_unset}</b>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with _c_os_btn:
                if st.button("商品管理で対応 →", key="dash_goto_pm"):
                    _dash_nav("商品管理", "★ 毎日")
        else:
            st.markdown('<span class="clear-status">✓ 自社在庫 問題なし</span>', unsafe_allow_html=True)

        # #43 fix: 価格急変 + 在庫復活 併記 (v2 モック要素復元、1 行 12px)。
        _surge, _drop = _sp["price_surge"], _sp["price_drop"]
        _restock = kpis.get("price_restock", 0)
        if _surge or _drop or _restock:
            _restock_suffix = (
                f' ・ 在庫復活 <b>{_restock}</b> (参考)' if _restock else ''
            )
            st.markdown(
                f'<div style="font-size:12px;line-height:1.5;">'
                f'💰 価格急変 <b>{_surge + _drop}</b>件 '
                f'(📈{_surge} / 📉{_drop}){_restock_suffix}'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown('<span class="clear-status">✓ 価格急変なし</span>', unsafe_allow_html=True)

        # ── 🗂 その他の通知 (P2、閉じておく。#43-relayout で通知センターの
        #    左カラム脱出分をこちらへ集約し、右カラムの空白を解消) ──
        if _nc_available and _nc_p2_cats:
            # 見出し-expander 間隔を詰める (#43-relayout 差戻し: st.markdown 既定の
            # 段落マージンを回避し、タイトなインライン div にする)。
            st.markdown(
                '<div style="font-weight:600;font-size:14px;margin:2px 0 2px 0;">'
                '🗂 その他の通知</div>',
                unsafe_allow_html=True,
            )
            for _nc_cat, _nc_cnt in _nc_p2_cats:
                _nc_label = _nc_cat_label(_nc_cat)
                _nc_emoji_c = _nc_cat_emoji(_nc_cat)
                with st.expander(f"{_nc_emoji_c} {_nc_label} ({_nc_cnt})", expanded=False):
                    _nc_rows = _cd_notification_rows(_dbv, True, _nc_cat, 10)
                    for _nc_n in _nc_rows:
                        _render_nc_action_row(_nc_n, f"nc_{_nc_cat}")
                    _nc_over = _nc_cnt - len(_nc_rows)
                    if _nc_over > 0:
                        st.caption(f"... 他 {_nc_over} 件 (上位10件表示)")
                    if st.button(f"「{_nc_label}」を全て既読にする", key=f"nc_cat_read_{_nc_cat}"):
                        _nc_mark_category_read(_nc_cat)
                        bump_db_version()
                        st.rerun()

        if _nc_available and _nc_read_recent:
            with st.expander(f"既読 (7日) ({len(_nc_read_recent)})", expanded=False):
                for _nc_n in _nc_read_recent[:50]:
                    st.markdown(_render_nc_row(_nc_n), unsafe_allow_html=True)

    st.markdown("---")

    # ── フッタ折りたたみ行 (TASKS / 部署 / 燃料 / ▸ニュース / ▸参考メール) ──
    active = _cd_active_tasks(_dbv)
    _f_c1, _f_c2, _f_c3, _f_c4, _f_c5 = st.columns(5)

    with _f_c1:
        with st.expander(f"📋 TASKS ({len(active)})", expanded=False):
            show_archive = st.toggle("完了タスクを表示", value=False, key="show_archive")
            if show_archive:
                archived = get_archived_tasks()
                if archived:
                    for task in archived:
                        st.markdown(
                            f"~~{task['name']}~~ "
                            f"<span style='color:#8d927f;font-size:11px;'>({task['completed_date']})</span>",
                            unsafe_allow_html=True,
                        )
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

                        _task_key = f"task_done_{task.get('section','')}_{task.get('name','')[:40]}_{i}"
                        done = st.checkbox(f"{task['name']}{dl}{link}", key=_task_key)
                        if pri_html:
                            st.markdown(
                                f"<div style='margin-top:-18px;margin-bottom:4px;padding-left:28px;'>{pri_html}</div>",
                                unsafe_allow_html=True,
                            )
                        if done:
                            done_tasks.append(task['name'])

                    if done_tasks:
                        if st.button(f"{len(done_tasks)}件を完了にする", type="primary", key="dash_tasks_complete"):
                            for name in done_tasks:
                                complete_task(name)
                            st.rerun()
                else:
                    st.markdown('<span class="clear-status">全タスク完了</span>', unsafe_allow_html=True)

    with _f_c2:
        company_status = get_company_status()
        _dept_online = sum(
            1 for ok in (
                company_status.get('has_secretary'),
                company_status.get('has_research'),
                company_status.get('has_finance'),
            ) if ok
        ) if company_status.get('exists') else 0
        with st.expander(f"🏢 部署 ({_dept_online}/3)", expanded=False):
            if company_status['exists']:
                for name, ok in [
                    ("SECRETARY", company_status['has_secretary']),
                    ("RESEARCH", company_status['has_research']),
                    ("FINANCE", company_status['has_finance']),
                ]:
                    color = "#2e7d5b" if ok else "#a8341b"
                    dot = "●" if ok else "○"
                    label = "ONLINE" if ok else "OFFLINE"
                    st.markdown(
                        f'<div style="font-family:Share Tech Mono,monospace;font-size:11px;color:{color};padding:2px 0;">'
                        f'{dot} {name} — {label}</div>',
                        unsafe_allow_html=True,
                    )
            routine_result = get_today_routine_result()
            if routine_result.get('exists'):
                _todo = routine_result.get('todo', {})
                _rsch = routine_result.get('research', {})
                st.caption(f"秘書ルーティン: 繰越 {_todo.get('carried_over', 0)} / リサーチ {len(_rsch.get('topics', []))}")

    with _f_c3:
        _fuel_days = get_days_since_last_update(s)
        _ship_days = get_shipping_rate_days_since_update(s)
        _fuel_warn = _fuel_days is None or _fuel_days >= UPDATE_WARNING_DAYS
        _ship_warn = _ship_days is None or _ship_days >= SHIPPING_RATE_WARNING_DAYS
        _fuel_label = "⚠ 要確認" if (_fuel_warn or _ship_warn) else "OK"
        with st.expander(f"⛽ 燃料/送料 ({_fuel_label})", expanded=False):
            if _fuel_warn:
                _msg = "未記録" if _fuel_days is None else f"{_fuel_days}日経過"
                st.markdown(
                    f'<div style="font-family:Share Tech Mono,monospace;font-size:11px;color:#b8860b;padding:3px 0;">'
                    f'▲ 燃料サーチャージ更新 — {_msg}（設定タブで値を確認）</div>',
                    unsafe_allow_html=True,
                )
            if _ship_warn:
                _msg = "未記録" if _ship_days is None else f"{_ship_days}日経過"
                st.markdown(
                    f'<div style="font-family:Share Tech Mono,monospace;font-size:11px;color:#b8860b;padding:3px 0;">'
                    f'▲ 運送料PDF更新 — {_msg}（設定タブで最新運送料PDFをアップロード）</div>',
                    unsafe_allow_html=True,
                )
            if not _fuel_warn and not _ship_warn:
                st.markdown('<span class="clear-status">更新済み</span>', unsafe_allow_html=True)

    with _f_c4:
        # ── NEWS (既定閉、AI 活用アクションは 1 行リンクへ圧縮) ──
        _action_rows: list[dict] = []
        try:
            from monitor.database import get_news_action_reports_recent
            _action_rows = get_news_action_reports_recent(days=7, limit=5)
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"get_news_action_reports_recent 失敗: {_e}")
            _action_rows = []

        from datetime import date as _date_cls
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
        except Exception:
            _news_db_rows = []

        with st.expander(f"▸ ニュース ({len(_news_db_rows)})", expanded=False):
            if _action_rows:
                _top_action = _action_rows[0]
                _at_title = html.escape((_top_action.get('title') or '')[:60])
                _at_url = html.escape(_top_action.get('url') or '', quote=True)
                _at_link = f'<a href="{_at_url}" target="_blank">{_at_title}</a>' if _at_url else _at_title
                st.caption(f"🤖 AI活用アクション {len(_action_rows)}件 — 最新: ")
                st.markdown(f'<span style="font-size:11px;">{_at_link}</span>', unsafe_allow_html=True)

            if _news_db_rows:
                _src_label = {'x': 'X', 'reddit': 'Reddit', 'hn': 'HN', 'web': 'Web'}
                _src_color = {
                    'x': '#156a63', 'reddit': '#b35a2e', 'hn': '#b8860b', 'web': '#5f6557',
                }
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
                        f'<span style="color:#8d927f;font-size:10px;margin-left:6px;">♥ {_eng:,}</span>'
                    ) if _eng > 0 else ''
                    _date_html = ''
                    _card_opacity = 1.0
                    try:
                        _pub_dt = _parse_news_published(_n.get('published_at') or '')
                        if _pub_dt is not None:
                            _jst_abs, _rel, _age_d = _fmt_news_freshness(_pub_dt, _now_utc224)
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
            else:
                _news_file = Path(__file__).resolve().parent.parent / "data" / "news" / f"{_date_cls.today().isoformat()}-news.json"
                if _news_file.exists():
                    import json as _nj
                    _news_items = _nj.loads(_news_file.read_text(encoding="utf-8"))
                    _high_news = [n for n in _news_items if n.get("impact") == "high"]
                    _med_news = [n for n in _news_items if n.get("impact") == "medium"]
                    if _high_news:
                        for _n in _high_news:
                            _title = html.escape((_n.get("title") or "")[:60])
                            _source = html.escape(_n.get("source") or "")
                            st.markdown(
                                f'<div style="border-left:2px solid rgba(168,52,27,0.45);padding:4px 10px;'
                                f'margin-bottom:4px;background:rgba(168,52,27,0.12);border-radius:0 4px 4px 0;">'
                                f'<strong>{_title}</strong><br>'
                                f'<span style="color:#8d927f;font-size:11px;">{_source}</span></div>',
                                unsafe_allow_html=True,
                            )
                    if _med_news:
                        for _n in _med_news[:3]:
                            _title = html.escape((_n.get("title") or "")[:55])
                            _source = html.escape(_n.get("source") or "")
                            st.markdown(
                                f'<div style="border-left:2px solid rgba(184,134,11,0.40);padding:4px 10px;'
                                f'margin-bottom:4px;background:rgba(184,134,11,0.12);border-radius:0 4px 4px 0;">'
                                f'<span style="font-size:13px;">{_title}</span><br>'
                                f'<span style="color:#8d927f;font-size:11px;">{_source}</span></div>',
                                unsafe_allow_html=True,
                            )
                    if not _high_news and not _med_news:
                        st.caption("重要なニュースはありません")
                else:
                    st.caption("本日のニュースはまだ取得されていません")

    with _f_c5:
        # ── REFERENCE (参考メール、既定閉) ──
        _ref_excluded_categories = {'supplier_purchase', 'sale', 'listing_notification'}
        _non_urgent = []
        for _em in _dash_emails_all:
            _pri_ai = _em.get('priority_ai') or ''
            _cat_ai = _em.get('category_ai') or ''
            _cat_rule = _em.get('category', 'other')
            if _is_urgent_email(_em):
                continue
            if _cat_rule in _ref_excluded_categories or _cat_ai in _ref_excluded_categories:
                continue
            if _cat_ai == 'promo' and _pri_ai not in ('high', 'urgent'):
                continue
            _non_urgent.append(_em)

        with st.expander(f"▸ 参考メール ({len(_non_urgent)})", expanded=False):
            if _non_urgent:
                _ref_confirm_ids = []
                for _em in _non_urgent[:30]:
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
                        f'<div class="mail-row" style="margin-top:-6px;'
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
                        bump_db_version()
                        st.rerun()
            else:
                st.caption("—")

    # ── LOG (デバッグ用、既定閉) ──
    st.divider()
    _show_log = st.checkbox("実行ログを表示", key="dash_show_log")
    if _show_log:
        logs = get_latest_execution_logs(limit=10)
        if logs:
            log_data = [{"時刻": l['timestamp_str'].split('.')[0] if l['timestamp'] else "", "内容": l['message'][:80]} for l in logs]
            st.dataframe(pd.DataFrame(log_data), width="stretch", hide_index=True, height=250)
