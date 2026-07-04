#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手動実行 (各定時タスクの即時実行) タブ (W221 Tier2 抽出、2026-06-04)。

app.py の `if _w134_sel == "手動実行":` 分岐 body をそのまま移植。挙動不変 (K2 surgical)。
"""
from __future__ import annotations

import logging
from pathlib import Path
import streamlit as st

logger = logging.getLogger(__name__)

# タブ密度化リファクタ C2 (2026-07-04): このタブ配下だけに効くスコープ CSS
# (st.container(key="manualrun_root") 内側 div に付く class="st-key-manualrun_root"
# を掴む。他タブ / app.py のグローバル密度 CSS には触れない = K2 surgical)。
# user 承認済み密度スペック: フォント12px / 行高22-28px / 常時caption→help化。
_MANUALRUN_DENSITY_CSS = """<style>
div[class*="st-key-manualrun_root"] [data-testid="stMarkdownContainer"] p {
    font-size: 12px !important;
    line-height: 24px !important;
    margin: 2px 0 !important;
}
div[class*="st-key-manualrun_root"] [data-testid="stCaptionContainer"] p {
    font-size: 11px !important;
    line-height: 20px !important;
    margin: 2px 0 !important;
}
div[class*="st-key-manualrun_root"] [data-testid="stAlert"] {
    padding: 6px 10px !important;
    font-size: 12px !important;
}
div[class*="st-key-manualrun_root"] [data-testid="stButton"] > button {
    min-height: 28px !important;
    padding: 3px 10px !important;
    font-size: 12px !important;
    line-height: 22px !important;
}
div[class*="st-key-manualrun_root"] [data-testid="stMetricLabel"] {
    font-size: 11px !important;
}
div[class*="st-key-manualrun_root"] [data-testid="stMetricValue"] {
    font-size: 20px !important;
}
</style>"""


def render_manual_run_tab(s: dict) -> None:
    st.subheader(
        "タスク手動実行",
        help="ここからタスクを即時実行できます。通常は定時実行（5:00 / 11:00 / 17:00 / 22:00）で自動実行されます。",
    )
    _root = st.container(key="manualrun_root")
    _root.markdown(_MANUALRUN_DENSITY_CSS, unsafe_allow_html=True)
    with _root:
        _render_manual_run_body(s)


def _render_manual_run_body(s: dict) -> None:
    # W221 Tier2 fix (2026-06-05): app.py top-level import をグローバル参照していた
    # 名前を関数内 lazy import で補完 (抽出漏れ修正、render 実行時 NameError 防止)。
    from execution_logger import get_execution_statistics, log_execution_result, save_execution_history, send_discord_notification
    import json
    from monitor.database import get_active_items, get_recent_logs
    from monitor.ebay_sync import auto_rank_all_listings_in_db, sync_listings_from_ebay
    import sys
    import time

    # ────────────────────────────────
    # クイック実行セクション
    # ────────────────────────────────
    # C2 密度化: 常時 caption → 見出し横の hover tooltip (ⓘ) 化 (K2 surgical、
    # 他見出しと視覚サイズを揃えるため markdown 見出しのまま維持)。
    st.markdown(
        '### クイック実行 <span title="ボタン1つで即時実行。'
        '結果は組織(.company)に自動配信されます。" '
        'style="font-size:11px;color:#8d927f;cursor:help;'
        'border-bottom:1px dotted #8d927f;">ⓘ</span>',
        unsafe_allow_html=True,
    )

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
        # W160 (2026-05-24): 'sales' (task_sales_tracking) 削除. W149 で task_order_alert.GetOrders に置換済.
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

                    # config を読み込み (W243: タブ分割後の parent.parent 修正)
                    _qconfig_path = Path(__file__).resolve().parent.parent / 'config' / 'schedule_config.json'
                    _qconfig = {}
                    if _qconfig_path.exists():
                        with open(_qconfig_path, 'r', encoding='utf-8') as _cf:
                            _qconfig = json.load(_cf)
                    else:
                        st.warning(f"schedule_config.json が見つかりません ({_qconfig_path}), 空 config で続行")

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
                            'price': 'price_optimization',
                            # 'sales': 'sales_tracking' は W160 で削除
                        }
                        _qroute({_rkey_map.get(_qkey, _qkey): _qresult})
                    except Exception as _route_e:
                        logger.warning("手動実行 組織ルーティング失敗 (%s): %s", _qkey, _route_e)

                    # 実行ログ記録
                    try:
                        _qdetails = _qresult if isinstance(_qresult, dict) else {}
                        result_data = log_execution_result(
                            _qkey, "success", f"{_qdisplay} 完了",
                            details=_qdetails, execution_time_sec=_qelapsed)
                        save_execution_history(_qkey, result_data)
                    except Exception as _hist_e:
                        logger.warning("手動実行 履歴記録失敗 (%s): %s", _qkey, _hist_e)

                except Exception as _qe:
                    _qelapsed = time.time() - _qstart
                    _qstatus.update(label=f"{_qdisplay} — エラー", state="error")
                    st.error(f"エラー: {_qe}")
                    try:
                        log_execution_result(_qkey, "failed", str(_qe), execution_time_sec=_qelapsed)
                    except Exception as _logfail_e:
                        logger.warning("手動実行 失敗ログ記録失敗 (%s): %s", _qkey, _logfail_e)

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

                            _cfg_path = _Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
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
                    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tasks"))
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
