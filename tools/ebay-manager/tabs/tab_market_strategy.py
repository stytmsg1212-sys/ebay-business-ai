"""W7-A 市場戦略タブ.

機能:
  - 全 SKU の primary_market 表示 (US_only / mixed_global / unknown / 未判定)
  - pending_market_changes (区分跨ぎ提案) のまとめて承認 UI
  - 「市場分析を実行」ボタン (CDP Chrome 必須)
  - 国別 buyer location breakdown 表示
  - 直近の market_strategy_decisions 履歴
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import streamlit as st


def _conn() -> sqlite3.Connection:
    db = Path(__file__).resolve().parent.parent / "data" / "monitor.db"
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    return c


def _check_cdp_status() -> bool:
    """CDP port 9222 が listen しているか."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(("127.0.0.1", 9222))
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def render_tab() -> None:
    st.subheader("市場戦略 (W7-A)")
    st.caption(
        "Terapeak (Research Products) の Buyer Location データを基に SKU 毎の "
        "primary_market を判定. 動画 [60JJUZaMdpo] 70% threshold."
    )

    # ── 状況サマリ ──
    with _conn() as c:
        # 2026-05-06: quantity_ebay >= 1 削除. buyer 分布は在庫数と無関係.
        # 無在庫商品 (qty=0 が正常) を集計から除外していた silent skip を解消.
        dist = {r["pm"]: r["n"] for r in c.execute(
            """SELECT COALESCE(primary_market,'未判定') AS pm, COUNT(*) AS n
               FROM ebay_listings
               WHERE COALESCE(is_ended, 0) = 0
               GROUP BY pm"""
        ).fetchall()}
        total_active = sum(dist.values())

        pending_count = c.execute(
            "SELECT COUNT(*) FROM pending_market_changes"
        ).fetchone()[0]

        latest_run = c.execute(
            "SELECT MAX(scraped_at) FROM market_analysis"
        ).fetchone()[0]

    cols = st.columns(5)
    cols[0].metric("US_only", dist.get("US_only", 0))
    cols[1].metric("mixed_global", dist.get("mixed_global", 0))
    cols[2].metric("unknown", dist.get("unknown", 0))
    cols[3].metric("未判定", dist.get("未判定", 0))
    cols[4].metric("提案待ち", pending_count, delta="要承認" if pending_count else None)

    if latest_run:
        st.caption(f"最終 refresh: {latest_run}")

    st.markdown("---")

    # ── CDP Chrome 状態 ──
    cdp_ok = _check_cdp_status()
    if cdp_ok:
        st.success("CDP Chrome (port 9222) 接続可能")
    else:
        st.warning(
            "CDP Chrome が未起動です. `scripts/start_chrome_cdp.bat` を実行 → "
            "eBay Seller Hub にログイン → このタブから refresh 実行可能になります."
        )

    # ── refresh 実行ボタン ──
    refresh_cols = st.columns([2, 2, 2, 4])
    with refresh_cols[0]:
        run_test = st.button("テスト実行 (1 件)", disabled=not cdp_ok)
    with refresh_cols[1]:
        run_small = st.button("少数 (10 件)", disabled=not cdp_ok)
    with refresh_cols[2]:
        run_full = st.button("全件 refresh", disabled=not cdp_ok, type="primary")

    if run_test or run_small or run_full:
        from tasks.task_market_analysis_refresh import run_market_analysis_refresh
        limit = 1 if run_test else (10 if run_small else None)
        with st.status(f"市場分析 refresh 実行中 (limit={limit or '全件'})...", expanded=True) as status:
            cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            try:
                result = run_market_analysis_refresh(cfg, limit=limit, use_ai_keyword=True)
            except Exception as e:
                import traceback
                result = {
                    "success": False,
                    "error": f"exception: {e}",
                    "trace": traceback.format_exc(),
                }
            # 結果を session_state に保存して rerun でも消えないように
            st.session_state["market_strategy_last_result"] = result
            if result.get("success"):
                status.update(label="完了", state="complete")
            else:
                status.update(label="失敗", state="error")

    # 直近の結果を常時表示 (rerun 後も保持)
    last = st.session_state.get("market_strategy_last_result")
    if last:
        if last.get("success"):
            st.success(last.get("message", ""))
        else:
            st.error(last.get("message") or last.get("error", "不明なエラー"))
            if last.get("trace"):
                with st.expander("詳細トレース"):
                    st.code(last["trace"])

    st.markdown("---")

    # ── 全在庫一括判定 (検索ボックス自動入力方式) ──
    st.markdown("### 🚀 全在庫 一括判定")
    st.caption(
        "全販売中商品を一括で Terapeak 市場分析する. "
        "事前準備: CDP Chrome で Terapeak ページを 1 回だけ開く (Last 90 days, Seller=Japan, Sold tab). "
        "あとは寝てて OK. 1 件 約 30 秒 × 件数. 中断は MonoDeck タブを閉じて停止."
    )

    bulk_col1, bulk_col2, bulk_col3, bulk_col4 = st.columns([1, 1, 1, 1])
    with bulk_col1:
        bulk_mode = st.radio(
            "対象", options=["未処理のみ", "全件再判定", "テスト 5 件"],
            index=0, key="bulk_mode",
            help=(
                "未処理のみ: 直近 24h 以内に成功したものは skip (推奨)\n"
                "全件: 全在庫を再判定 (時間 + eBay 規制リスク大)\n"
                "テスト: 5 件のみ"
            ),
        )
    with bulk_col2:
        bulk_use_ai = st.checkbox(
            "Haiku で keyword 抽出", value=True, key="bulk_use_ai",
        )
    with bulk_col3:
        bulk_day_range = st.selectbox(
            "集計期間 (days)", options=[7, 30, 90, 180, 365],
            index=2, key="bulk_day_range",
        )
    with bulk_col4:
        bulk_sleep = st.slider(
            "間隔秒数", min_value=2.0, max_value=10.0, value=3.0, step=0.5,
            key="bulk_sleep",
            help="eBay 規制回避のため. 朝の事故では 1.5s で 76 件後に block. 3-5s 推奨",
        )

    # 未処理件数のプレビュー
    from tasks.task_market_analysis_refresh import _get_active_listings
    skip_h = 24 if bulk_mode == "未処理のみ" else None
    preview_count = len(_get_active_listings(skip_recent_hours=skip_h))
    if bulk_mode == "テスト 5 件":
        preview_count = min(5, preview_count)
    est_min = preview_count * (bulk_sleep + 60) / 60  # 1 件 約 (sleep + 60s)
    st.caption(
        f"対象 **{preview_count} 件** / 想定時間 約 **{est_min:.0f} 分** "
        f"({(est_min/60):.1f} 時間) / 連続 5 件失敗で自動停止"
    )

    if st.button("🚀 一括判定 開始", type="primary", key="bulk_market_analysis_start"):
        from tasks.task_market_analysis_refresh import run_market_analysis_refresh

        progress_bar = st.progress(0.0, text="開始中...")
        progress_label = st.empty()
        log_box = st.empty()
        log_lines: list[str] = []
        counters = {"ok": 0, "ng": 0}

        def _on_progress(i, n, title, phase, payload):
            ratio = i / n if n > 0 else 0.0
            short_title = (title or "(no title)")[:60]
            if phase == "scraping":
                progress_bar.progress(
                    max(0.0, ratio - 1.0/n) if n > 0 else 0.0,
                    text=f"[{i}/{n}] {short_title} ... 抽出中",
                )
                progress_label.markdown(f"**現在処理中**: {short_title}")
            elif phase == "done" and payload and payload.get("success"):
                counters["ok"] += 1
                pm = payload.get("primary_market", "?")
                us = payload.get("us_count", "?")
                total = payload.get("total_sold", "?")
                log_lines.append(
                    f"✅ [{i}/{n}] {short_title[:55]} → {pm} (US {us}/{total})"
                )
                progress_bar.progress(ratio, text=f"[{i}/{n}] 完了 (成功 {counters['ok']} / 失敗 {counters['ng']})")
            elif phase == "failed":
                counters["ng"] += 1
                err = (payload or {}).get("error", "?")
                log_lines.append(f"⚠️ [{i}/{n}] {short_title[:55]} → {str(err)[:80]}")
                progress_bar.progress(ratio, text=f"[{i}/{n}] スキップ (成功 {counters['ok']} / 失敗 {counters['ng']})")
            log_box.code("\n".join(log_lines[-20:]))  # 直近 20 件

        with st.spinner("実行中... CDP Chrome は触らないでください"):
            summary = run_market_analysis_refresh(
                config={},
                limit=5 if bulk_mode == "テスト 5 件" else None,
                use_ai_keyword=bulk_use_ai,
                on_progress=_on_progress,
                day_range=int(bulk_day_range),
                skip_recent_hours=24 if bulk_mode == "未処理のみ" else None,
                stop_on_consecutive_failures=5,
                sleep_seconds=float(bulk_sleep),
            )

        if summary.get("success"):
            box = st.warning if summary.get("aborted_by_block") else st.success
            box(
                f"### 完了\n\n"
                f"- 処理: **{summary['processed']}/{summary.get('total_target', '?')} 件**\n"
                f"- 成功: **{summary['succeeded']} 件**\n"
                f"- 失敗: {summary['failed']} 件\n"
                f"- 区分変更提案: **{summary['proposed_changes']} 件** (要承認)\n"
                f"- 所要時間: {summary['duration_sec']:.0f} 秒\n"
                + (f"\n⚠️ **eBay 規制疑いで途中停止**. 残りは 1-3 時間後に「未処理のみ」で再実行してください."
                   if summary.get("aborted_by_block") else "")
            )
            st.info("「区分変更提案 (まとめて承認)」セクションで承認してください")
        else:
            st.error(f"失敗: {summary.get('message', summary.get('error', '?'))}")

    st.markdown("---")

    # ── 手動 SKU 解析 (Plan A: user 手動 navigate → script 抽出のみ) ──
    st.markdown("### 手動 SKU 解析 (1 件ずつ)")
    st.caption(
        "**動作確実な方式**. CDP Chrome で Terapeak ページを手動で開いて、"
        "render 完了したら下のボタンで現在の DOM から抽出します. "
        "(自動 navigation 方式は React render 不安定のため非推奨)"
    )

    filter_text = st.text_input(
        "🔍 商品名でフィルタ (部分一致, 空欄で全件表示)",
        key="market_strategy_manual_filter",
        placeholder="例: Audio-Technica / Maxell / Sony 等",
    ).strip()

    with _conn() as c:
        # 2026-05-06: quantity_ebay >= 1 削除. buyer 分布は在庫数と無関係.
        # 手動 SKU 解析の dropdown も無在庫を含めて全 active を選択可能に.
        if filter_text:
            like_pat = f"%{filter_text}%"
            listing_options = c.execute(
                """SELECT sku, title, ebay_item_id
                   FROM ebay_listings
                   WHERE COALESCE(is_ended, 0) = 0
                     AND title LIKE ? COLLATE NOCASE
                   ORDER BY title COLLATE NOCASE ASC""",
                (like_pat,),
            ).fetchall()
        else:
            listing_options = c.execute(
                """SELECT sku, title, ebay_item_id
                   FROM ebay_listings
                   WHERE COALESCE(is_ended, 0) = 0
                   ORDER BY title COLLATE NOCASE ASC"""
            ).fetchall()
        listing_options = [dict(r) for r in listing_options]

    if not listing_options:
        st.info("該当する商品がありません。フィルタ語を見直してください。")
    else:
        st.caption(f"{len(listing_options)} 件ヒット" + ("" if filter_text else " (全在庫)"))

    if listing_options:
        def _label(l: dict) -> str:
            title = (l.get("title") or "(no title)")[:80]
            tail = (l.get("ebay_item_id") or "")[-4:]
            return f"{title}  … #{tail}"

        opts_dict: dict = {}
        for l in listing_options:
            label = _label(l)
            if label in opts_dict:
                label = f"{label} ({l['sku']})"
            opts_dict[label] = l

        selected_label = st.selectbox(
            "商品を選択", options=list(opts_dict.keys()),
            key="market_strategy_manual_sku",
        )
        selected = opts_dict.get(selected_label)
        if selected:
            from monitor.keyword_extractor import extract_keyword
            from urllib.parse import quote as _q

            # keyword 自動生成 (fallback ベース、AI 起動時のみ AI 使用)
            use_ai_kw = st.checkbox(
                "Haiku で keyword 抽出 (より精度高、約 $0.0005/SKU)",
                value=True, key="manual_use_ai_kw",
            )

            # dropdown 切り替え検知 → keyword を session_state に再注入
            # (text_input は key 指定時 session_state を優先し value 引数を無視するため).
            # AI 切替時は user 編集を保護するため自動再生成しない (明示ボタンで再抽出).
            # AI keyword は session 内 cache (sku × use_ai 単位) して同 SKU の重複課金を防ぐ.
            sel_key = f"{selected['sku']}|{selected['ebay_item_id']}"
            ai_cache_key = f"_ai_kw_cache_{selected['sku']}_{selected['ebay_item_id']}_{use_ai_kw}"
            if ai_cache_key not in st.session_state:
                st.session_state[ai_cache_key] = extract_keyword(
                    selected["title"] or selected["sku"], use_ai=use_ai_kw,
                )
            if st.session_state.get("manual_selected_key") != sel_key:
                st.session_state["manual_kw"] = st.session_state[ai_cache_key]
                st.session_state["manual_selected_key"] = sel_key

            kw_col1, kw_col2 = st.columns([4, 1])
            with kw_col1:
                kw = st.text_input("検索 keyword (調整可能)", key="manual_kw")
            with kw_col2:
                st.markdown("&nbsp;", unsafe_allow_html=True)  # ラベル分の縦調整
                if st.button("AI 再抽出", key="regen_kw",
                             help="現在の AI on/off 設定で keyword を再生成. user 編集は破棄されます"):
                    st.session_state["manual_kw"] = st.session_state[ai_cache_key]
                    st.rerun()

            # Terapeak URL 生成
            url = (
                "https://www.ebay.com/sh/research?marketplace=EBAY-US"
                f"&keywords={_q(kw)}&dayRange=90&tabName=SOLD"
                f"&sellerCountry={_q('SellerLocation:::JP')}"
                "&offset=0&limit=50&tz=Asia%2FTokyo"
            )

            st.markdown("**Step 1**: 下の URL をコピーして CDP Chrome で開く")
            st.code(url, language="text")
            st.markdown(
                "**Step 2**: ページ完全表示を待つ (Avg sold price / Buyer Locations 等が見える)"
            )
            st.markdown("**Step 3**: 下のボタン押下 → 現在の DOM から抽出")

            col_a, col_b = st.columns(2)
            with col_a:
                extract_btn = st.button(
                    f"📥 SKU {selected['sku']} を抽出 (現在ページから)",
                    type="primary",
                    key="manual_extract_btn",
                    help="user が手動で URL を開いた前提. 現在の DOM から抽出のみ.",
                )
            with col_b:
                searchbox_btn = st.button(
                    f"🔄 検索 box 自動 ({selected['sku']})",
                    key="manual_searchbox_btn",
                    help="user が初期検索済の Terapeak タブで, 検索 box にこの keyword を入力 → Research クリック → 抽出. "
                         "Plan A 自動化方式 (動けば全件 batch 可能).",
                )

            if searchbox_btn:
                from monitor.terapeak_scraper import (
                    scrape_via_search_box, save_to_db,
                    propose_market_change_for_listing,
                )
                with st.status("検索 box 自動化で抽出中...", expanded=True) as status:
                    result = scrape_via_search_box(selected["sku"], kw)
                    if result.success:
                        inserted_id = save_to_db(result, ebay_item_id=selected["ebay_item_id"])
                        if inserted_id and result.primary_market:
                            # 1 listing 1 propose (異商品共有 SKU の cascade 排除).
                            propose_market_change_for_listing(
                                ebay_item_id=selected["ebay_item_id"],
                                sku=selected["sku"],
                                market_analysis_id=inserted_id,
                                proposed_market=result.primary_market,
                                reason=result.primary_market_reason or "",
                            )
                        status.update(label="抽出完了", state="complete")
                        st.session_state["market_strategy_manual_result"] = {
                            "success": True,
                            "sku": selected["sku"],
                            "primary_market": result.primary_market,
                            "us": result.us_count,
                            "non_us": result.non_us_count,
                            "ratio": result.us_ratio,
                            "countries": result.countries_breakdown,
                            "method": "search_box",
                        }
                    else:
                        status.update(label="検索 box 自動化失敗", state="error")
                        st.session_state["market_strategy_manual_result"] = {
                            "success": False,
                            "sku": selected["sku"],
                            "error": result.error,
                            "method": "search_box",
                        }

            if extract_btn:
                from monitor.terapeak_scraper import (
                    extract_from_current_page, save_to_db,
                    propose_market_change_for_listing,
                )
                with st.status("DOM から抽出中...", expanded=True) as status:
                    result = extract_from_current_page(
                        selected["sku"], expected_keyword=kw,
                    )
                    if result.success:
                        inserted_id = save_to_db(result, ebay_item_id=selected["ebay_item_id"])
                        if inserted_id and result.primary_market:
                            # 1 listing 1 propose (異商品共有 SKU の cascade 排除).
                            propose_market_change_for_listing(
                                ebay_item_id=selected["ebay_item_id"],
                                sku=selected["sku"],
                                market_analysis_id=inserted_id,
                                proposed_market=result.primary_market,
                                reason=result.primary_market_reason or "",
                            )
                        status.update(label="抽出完了", state="complete")
                        st.session_state["market_strategy_manual_result"] = {
                            "success": True,
                            "sku": selected["sku"],
                            "primary_market": result.primary_market,
                            "us": result.us_count,
                            "non_us": result.non_us_count,
                            "ratio": result.us_ratio,
                            "countries": result.countries_breakdown,
                            "avg_price": result.avg_sold_price_usd,
                            "avg_ship": result.avg_shipping_usd,
                        }
                    else:
                        status.update(label="抽出失敗", state="error")
                        st.session_state["market_strategy_manual_result"] = {
                            "success": False,
                            "sku": selected["sku"],
                            "error": result.error,
                        }

    # 直近の手動抽出結果表示
    last_manual = st.session_state.get("market_strategy_manual_result")
    if last_manual:
        if last_manual.get("success"):
            st.success(
                f"✅ {last_manual['sku']} → **{last_manual['primary_market']}** "
                f"(US {last_manual.get('us')}/{(last_manual.get('us') or 0)+(last_manual.get('non_us') or 0)} = "
                f"{(last_manual.get('ratio') or 0)*100:.0f}%)"
            )
            if last_manual.get("countries"):
                with st.expander("国別 breakdown"):
                    for c_ in sorted(last_manual["countries"], key=lambda x: -x["count"])[:15]:
                        st.write(f"  {c_['code']} {c_['name']}: {c_['count']}")
        else:
            st.error(f"❌ {last_manual.get('sku')}: {last_manual.get('error')}")

    st.markdown("---")

    # ── 区分跨ぎ提案 (まとめて承認) ──
    # W7-A Phase 3 (2026-04-29): listing 単位化済. 1 行 = 1 listing. PK = ebay_item_id.
    # 旧: 1 SKU 承認 → 40 listing 巻添え cascade で大事故.
    # 新: 各 listing を独立に承認/却下. 同 SKU でも別決定可能.
    # W109(2) (2026-05-09): 提案区分での絞り込みフィルタ追加.
    # 米国専売 / 世界向けは W109(1) で auto-approve 済. 残る mixed_global / unknown を
    # 区分別に絞って user が確認できるようにする.
    st.markdown("### 区分変更提案 (まとめて承認)")
    _filter_market = st.selectbox(
        "提案区分で絞り込み",
        options=["すべて", "mixed_global", "US_only", "global_only", "unknown"],
        index=0,
        key="ms_pending_filter_market",
        help=(
            "W109(1) で US_only / global_only は auto-approve 済 (2026-05-09)、"
            "mixed_global は user 目視で承認を推奨 (DDP 関税判定リスク高)。"
        ),
    )
    _market_where = ""
    _market_params: list = []
    if _filter_market != "すべて":
        _market_where = "WHERE pmc.proposed_market = ?"
        _market_params = [_filter_market]
    with _conn() as c:
        pending = c.execute(
            # FINDING 7 (2026-05-05): listing_count を ebay_item_id 単位 (= 常に 1) に修正.
            # 旧 sku 単位だと stock pool SKU 紛入時に「58 件影響」等の誤誘導表示が出ていた.
            # migration v26 後 pending_market_changes は ebay_item_id 単位 = 1:1 が正規.
            f"""SELECT pmc.ebay_item_id, pmc.sku, pmc.current_market,
                      pmc.proposed_market, pmc.proposed_at, pmc.reason,
                      pmc.market_analysis_id,
                      ma.us_count, ma.non_us_count, ma.total_sold,
                      ma.countries_breakdown,
                      el.title,
                      1 AS listing_count
               FROM pending_market_changes pmc
               LEFT JOIN market_analysis ma
                 ON pmc.market_analysis_id = ma.id
               LEFT JOIN ebay_listings el
                 ON pmc.ebay_item_id = el.ebay_item_id
               {_market_where}
               ORDER BY pmc.sku, pmc.proposed_at DESC""",
            _market_params,
        ).fetchall()
        pending_list = [dict(r) for r in pending]

    if not pending_list:
        st.info("提案待ちの区分変更はありません.")
    else:
        st.caption(
            f"{len(pending_list)} 件の listing 単位提案. "
            "チェックして「承認」または「却下」"
        )
        if "market_strategy_selected" not in st.session_state:
            st.session_state["market_strategy_selected"] = set()

        # 表示中 ebay_item_id 一覧 (上位 50 件).
        displayed_ids = [p["ebay_item_id"] for p in pending_list[:50]]

        # 一括選択 / 承認後の cleanup を flag → rerun → 冒頭で処理 の 2 段階.
        # checkbox 描画後の session_state 書換は StreamlitAPIException になるため.
        action = st.session_state.pop("ms_action", None)
        if action == "select_all":
            for eid in displayed_ids:
                st.session_state[f"ms_sel_{eid}"] = True
            st.session_state["market_strategy_selected"] = set(displayed_ids)
        elif action in ("deselect_all", "approve_done", "reject_done"):
            for eid in displayed_ids:
                if f"ms_sel_{eid}" in st.session_state:
                    st.session_state[f"ms_sel_{eid}"] = False
            st.session_state["market_strategy_selected"] = set()

        for p in pending_list[:50]:
            eid = p["ebay_item_id"]
            sku = p["sku"]
            cur = p.get("current_market") or "未判定"
            prop = p["proposed_market"]
            title = p.get("title") or "(no title)"
            listing_count = p.get("listing_count") or 1
            with st.container(border=True):
                col1, col2, col3, col4 = st.columns([0.5, 3, 2, 2])
                with col1:
                    sel_key = f"ms_sel_{eid}"
                    if sel_key not in st.session_state:
                        st.session_state[sel_key] = (
                            eid in st.session_state["market_strategy_selected"]
                        )
                    sel = st.checkbox(
                        f"{title[:30]}", key=sel_key,
                        label_visibility="collapsed",
                    )
                    if sel:
                        st.session_state["market_strategy_selected"].add(eid)
                    else:
                        st.session_state["market_strategy_selected"].discard(eid)
                with col2:
                    st.markdown(f"**{title[:60]}**")
                    # listing 単位なので ebay_item_id を主表示, sku は SKU グループ説明
                    sku_suffix = (
                        f" / 同 SKU 内 {listing_count} listings"
                        if listing_count > 1 else ""
                    )
                    st.caption(f"item={eid} / sku=`{sku}`{sku_suffix}")
                with col3:
                    st.markdown(f"`{cur}` → **`{prop}`**")
                    st.caption(p.get("reason") or "")
                with col4:
                    cb = p.get("countries_breakdown")
                    if cb:
                        try:
                            countries = json.loads(cb)
                            tops = sorted(countries, key=lambda x: -x["count"])[:3]
                            top_str = " ".join(
                                f"{x['code']}={x['count']}" for x in tops)
                            st.caption(f"sold {p.get('total_sold')}件: {top_str}")
                        except (json.JSONDecodeError, KeyError, TypeError):
                            pass

        st.markdown("**選択操作:**")
        select_cols = st.columns(3)
        with select_cols[0]:
            if st.button(f"✅ 表示中 {len(displayed_ids)} 件を全選択",
                          key="ms_select_all"):
                # widget 描画後の session_state 書換禁止. flag を立てて rerun し
                # 次 render の冒頭で処理.
                st.session_state["ms_action"] = "select_all"
                st.rerun()
        with select_cols[1]:
            if st.button("⬜ 全解除", key="ms_deselect_all"):
                st.session_state["ms_action"] = "deselect_all"
                st.rerun()
        with select_cols[2]:
            sel_count = len(st.session_state["market_strategy_selected"])
            st.markdown(f"**選択中: {sel_count} 件**")

        st.markdown("**選択中を一括処理:**")
        action_cols = st.columns(2)
        with action_cols[0]:
            if st.button("選択を一括承認", type="primary", key="ms_bulk_approve"):
                if not st.session_state["market_strategy_selected"]:
                    st.warning("選択中の項目がありません")
                else:
                    _bulk_decision(st.session_state["market_strategy_selected"], "approved")
                    st.session_state["ms_action"] = "approve_done"
                    st.success("承認しました")
                    st.rerun()
        with action_cols[1]:
            if st.button("選択を一括却下", key="ms_bulk_reject"):
                if not st.session_state["market_strategy_selected"]:
                    st.warning("選択中の項目がありません")
                else:
                    _bulk_decision(st.session_state["market_strategy_selected"], "rejected")
                    st.session_state["ms_action"] = "reject_done"
                    st.info("却下しました")
                    st.rerun()

    st.markdown("---")

    # ── 直近 market_analysis 履歴 ──
    st.markdown("### 直近の市場分析結果 (上位 20 件)")
    with _conn() as c:
        recent = c.execute(
            """SELECT ma.sku, ma.keyword, ma.total_sold, ma.us_count, ma.non_us_count,
                      ma.primary_market, ma.scraped_at, ma.avg_sold_price_usd,
                      ma.avg_shipping_usd, el.title
               FROM market_analysis ma
               LEFT JOIN ebay_listings el ON ma.ebay_item_id = el.ebay_item_id
               ORDER BY ma.scraped_at DESC
               LIMIT 20"""
        ).fetchall()
        recent_list = [dict(r) for r in recent]

    if recent_list:
        for r in recent_list:
            cols = st.columns([3, 2, 1, 1, 2])
            cols[0].markdown(f"**{r['sku']}** {(r.get('title') or '')[:40]}")
            cols[1].caption(f"keyword: {r.get('keyword', '')}")
            cols[2].metric("US", r.get("us_count") or 0)
            cols[3].metric("非US", r.get("non_us_count") or 0)
            cols[4].markdown(f"`{r.get('primary_market', '')}`")
    else:
        st.info("market_analysis データがまだありません. 上の「テスト実行」を試してください.")


def _bulk_decision(ebay_item_ids: set, action: str, reviewer: str = "user") -> None:
    """選択された listing (ebay_item_id) の pending_market_changes を一括承認/却下.

    W7-A Phase 3 (2026-04-29 SKU cascade 事故再発防止):
      - WHERE は ebay_item_id 単位. 同 SKU の他 listing は独立で残る (cascade 物理排除).
      - market_strategy_decisions.ebay_item_id は NOT NULL (B option).

    W109 (2026-05-08): reviewer を引数化し、one-shot script (auto_w109) からの
    呼出を許容. UI 経由は default 'user' で後方互換.
    """
    if not ebay_item_ids:
        return
    approved_eids: list[str] = []  # W212: commit 後に breakeven 再計算する対象
    with _conn() as c:
        for eid in ebay_item_ids:
            row = c.execute(
                "SELECT * FROM pending_market_changes WHERE ebay_item_id = ?",
                (eid,),
            ).fetchone()
            if not row:
                continue
            row = dict(row)

            final_market = (
                row["proposed_market"] if action == "approved"
                else row.get("current_market")
            )
            c.execute(
                """INSERT INTO market_strategy_decisions
                   (sku, ebay_item_id, previous_market, proposed_market,
                    final_market, action, decided_at, reason, reviewer)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["sku"], eid, row.get("current_market"),
                 row["proposed_market"], final_market, action,
                 datetime.now().isoformat(), row.get("reason"), reviewer),
            )

            # 承認: 当該 listing 1 件のみ primary_market 更新 (cascade 排除)。
            # W212 fail-closed: 同 transaction で lp_breakeven_usd=NULL 無効化
            # (floor=NULL → auto-pricedown skip で stale floor の時間窓を閉じる)。
            if action == "approved":
                c.execute(
                    "UPDATE ebay_listings SET primary_market = ?, "
                    "lp_breakeven_usd = NULL WHERE ebay_item_id = ?",
                    (row["proposed_market"], eid),
                )
                approved_eids.append(eid)

            c.execute(
                "DELETE FROM pending_market_changes WHERE ebay_item_id = ?",
                (eid,),
            )

    # W212 (2026-06-03, Codex HIGH fix v2): commit 後 (write-lock 競合回避) に floor 再計算。
    # 上で floor=NULL 済 = 再計算失敗/load_settings 失敗でも floor=NULL のまま fail-closed。
    if approved_eids:
        import logging
        try:
            from monitor.lowest_price import update_listing_breakeven
            from calculator import load_settings
            _settings = load_settings()
            for eid in approved_eids:
                try:
                    update_listing_breakeven(eid, _settings)
                except Exception as e:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        f"区分承認後の breakeven 再計算失敗 ({eid}): {e}. floor=NULL のまま安全。"
                    )
        except Exception as e:  # noqa: BLE001 — load_settings 等の失敗も fail-closed
            logging.getLogger(__name__).warning(
                f"区分承認後の breakeven 再計算 setup 失敗: {e}. "
                f"承認 {len(approved_eids)} 件は floor=NULL のまま (自動値下げ skip で安全)。"
            )
