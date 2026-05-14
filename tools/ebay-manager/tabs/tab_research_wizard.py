#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W119 商品リサーチ自動化 wizard (4 step).

最安値チェックタブの前段に統合し、user 主導で 4 step を順次実行:
  Step 1: 物理属性一括取得 (重量推定 Haiku + Trading API GetItem)
  Step 2: 損益分岐価格一括計算 (compute_breakeven_price_usd → lp_breakeven_usd)
  Step 3: 検索ワード一括生成 (Opus 4.7 batch → search_keyword)
  Step 4: 競合検索 (Browse API + client-side sort) → checkbox 選択 → 競合 DB 登録

UI 設計核心:
  - 4 step を独立 expander で並列描画 (user は任意順で実行可能)
  - 各 step の「実行」ボタンは承認制 (Q1=C: DB のみ反映、eBay へは別途承認後)
  - 競合検索は 1 listing 単位 (selectbox で対象選択 → 30 件取得 → 上位 10 件表示)
  - eBay リンクボタンは LH_LocatedIn=1&_salic=104 で日本 seller filter

詳細: `data/system_improvements.json` id=203 / W119 entry.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Optional
from urllib.parse import quote_plus

import httpx

import streamlit as st

from monitor.database import get_conn
from monitor.lowest_price import (
    fetch_supplier_purchase_yen,
    update_listing_breakeven,
    update_listing_purchase_yen,
    upsert_listing_competitors,
)
from tasks.task_generate_search_keywords import (
    run_generate_search_keywords,
    update_search_keyword_manual,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Step 3 ページサイズ
# =============================================================================
_STEP3_PAGE_SIZE = 30


# =============================================================================
# Constants
# =============================================================================

# eBay 検索 URL pattern (Q2-B 確定: 日本 seller filter + Price+Shipping lowest first)
# Test 9 回で確認 (2026-05-10): &LH_LocatedIn=1&_salic=104 で JP filter, &_sop=15 で sort.
_EBAY_SEARCH_URL_TEMPLATE = (
    "https://www.ebay.com/sch/i.html"
    "?_nkw={kw}"
    "&_sacat=0"
    "&_sop=15"
    "&_from=R40"
    "&_trksid=m570.l1313"
    "&LH_LocatedIn=1"
    "&_salic=104"
)

# Step 4 競合検索: Browse API limit (buffer) と表示件数.
_BROWSE_API_LIMIT = 50
_DISPLAY_TOP_N = 20

# Browse API burst rate-limit 緩和. eBay 公式 spec は 5 RPS (Q3 reference)、
# Browse API 5 RPS spec に対し 0.7s sleep = ~1.4 RPS で保守抑制.
# 2026-05-12 saturated re-search で末尾 10 件 429 連発 → 0.5→0.7 に引き上げ.
# (getItem call が 1 listing で最大 20 件 × 0.5s = 10s 連続発火する区間で burst 化していた)
_BULK_BROWSE_SLEEP_SEC = 0.7

# Economy 系配送 (SpeedPAK Economy / Surface mail 等) の **proxy 判別閾値** (delivery window).
#
# ⚠️ これは **「Economy carrier 系」** の近似判別であって、**DDU (関税ポリシー) 判別ではない**.
# 詳細: `reference_shipping_method_vs_ddu_taxonomy.md` (配送方法 ≠ 関税ポリシー、独立軸).
# DDU 判別は `tab_product_management._is_ddu_policy()` (Browse API getItem `taxes` field) を使用.
#
# 出典:
# - `feedback_competitor_jp_sellers_only.md` (3 軸独立除外: 国 / 配送方法 / 関税ポリシー)
# - 動画学習 KB `.company/ebay-knowledge/topics/operation-rules.md`
#
# heuristic:
#   - delivery window (max - min) が _LIKELY_ECONOMY_DELIVERY_WINDOW_DAYS 日以上 → Economy 系
#   - express 配送 (FedEx / DHL 等) は window 通常 1-3 日
#   - SpeedPAK Economy 等は window 10-14 日が標準
_LIKELY_ECONOMY_DELIVERY_WINDOW_DAYS = 10


# =============================================================================
# Helpers
# =============================================================================

def extract_legacy_item_id(rest_id: str) -> str:
    """Browse API itemId 'v1|285999999001|0' → '285999999001' (legacy ID).

    eBay Browse API の itemId は RESTful 形式 'v1|<legacy>|<variant>' で返るが、
    本プロジェクトの DB (`competitor_products.competitor_item_id`) と
    `monitor/lowest_price.py:fetch_competitor_pricing` (cid.isdigit() check) は
    legacy ID (12 桁前後の数字) のみを期待する.

    既存の `task_rival_detection.py:300-308` と同等の抽出ロジックを wizard でも使う.
    """
    if not rest_id:
        return ""
    parts = rest_id.split("|")
    return parts[1] if len(parts) >= 2 else rest_id


def _has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def build_ebay_search_url(keyword: str) -> str:
    """検索ワードから eBay 検索 URL を構築. 空 / 空白のみは空文字を返す.

    >>> build_ebay_search_url("maxell MXCP-P100")
    'https://www.ebay.com/sch/i.html?_nkw=maxell+MXCP-P100&_sacat=0&_sop=15&_from=R40&_trksid=m570.l1313&LH_LocatedIn=1&_salic=104'
    """
    if not keyword:
        return ""
    stripped = keyword.strip()
    if not stripped:
        return ""
    return _EBAY_SEARCH_URL_TEMPLATE.format(kw=quote_plus(stripped))


def _count_listings_state() -> dict:
    """各 step の進捗カウント (active listings ベース)."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN weight_g IS NOT NULL THEN 1 ELSE 0 END) AS with_weight,
                SUM(CASE WHEN length_cm IS NOT NULL AND width_cm IS NOT NULL
                             AND height_cm IS NOT NULL THEN 1 ELSE 0 END) AS with_size,
                SUM(CASE WHEN lp_breakeven_usd IS NOT NULL THEN 1 ELSE 0 END) AS with_breakeven,
                SUM(CASE WHEN search_keyword IS NOT NULL AND search_keyword != ''
                              THEN 1 ELSE 0 END) AS with_keyword,
                COUNT(*) AS total
            FROM ebay_listings
            WHERE (is_ended IS NULL OR is_ended=0)
              AND title IS NOT NULL AND title != ''
            """
        ).fetchone()
    return {
        "with_weight": int(row[0] or 0),
        "with_size": int(row[1] or 0),
        "with_breakeven": int(row[2] or 0),
        "with_keyword": int(row[3] or 0),
        "total": int(row[4] or 0),
    }


def _get_active_listings_for_keyword_edit() -> list[dict]:
    """Step 3 / Step 4 用 listing dataset.
    search_keyword + breakeven + 現在価格 + 送料 + primary_market 含む.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ebay_item_id, title, search_keyword, search_keyword_source,
                   lp_breakeven_usd, current_price, primary_market, shipping_cost
            FROM ebay_listings
            WHERE (is_ended IS NULL OR is_ended=0)
              AND title IS NOT NULL AND title != ''
            ORDER BY ebay_item_id
            """
        ).fetchall()
    return [
        {
            "ebay_item_id": r[0],
            "title": r[1],
            "search_keyword": r[2],
            "search_keyword_source": r[3],
            "lp_breakeven_usd": r[4],
            "current_price": r[5],
            "primary_market": r[6],
            "shipping_cost": r[7],
        }
        for r in rows
    ]


# =============================================================================
# Step 1: 物理属性一括取得
# =============================================================================

def _render_step1(config: dict, counts: dict) -> None:
    st.markdown("#### Step 1: 物理属性一括取得")
    st.caption(
        f"重量と寸法を全 active listing で取得 → 送料計算 + Step 2 breakeven の前提情報. "
        f"対象 {counts['total']} 件中、重量取得済 {counts['with_weight']} 件 / "
        f"寸法取得済 {counts['with_size']} 件."
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔮 重量推定 (Haiku, 最大 50 件)", key="w119_btn_weight",
                     use_container_width=True):
            try:
                from tasks.task_estimate_weights_claude import run_estimate_weights_claude
                with st.spinner("Claude Haiku で重量推定中..."):
                    result = run_estimate_weights_claude(config)
                if result.get("success"):
                    st.success(
                        f"✅ 処理 {result.get('processed', 0)} 件 / "
                        f"更新 {result.get('updated', 0)} 件"
                    )
                else:
                    st.warning(f"⚠️ {result.get('message', '失敗')}")
            except Exception as e:
                logger.exception("[w119_step1] 重量推定エラー")
                st.error(f"重量推定エラー: {e}")

    with col2:
        if st.button("🌐 物理属性取得 (eBay Trading API)", key="w119_btn_physical",
                     use_container_width=True):
            try:
                from tasks.task_enrich_listings_physical import run_enrich_listings_physical
                with st.spinner("eBay Trading API で物理属性取得中..."):
                    result = run_enrich_listings_physical(config)
                if result.get("success"):
                    st.success(
                        f"✅ 処理 {result.get('processed', 0)} 件 / "
                        f"更新 {result.get('updated', 0)} 件"
                    )
                else:
                    st.warning(f"⚠️ {result.get('message', '失敗')}")
            except Exception as e:
                logger.exception("[w119_step1] 物理属性取得エラー")
                st.error(f"物理属性取得エラー: {e}")


# =============================================================================
# Step 2: 損益分岐価格一括計算
# =============================================================================

def _render_step2(config: dict, counts: dict) -> None:
    st.markdown("#### Step 2: 損益分岐価格一括計算")
    st.caption(
        f"全 listing で `lp_breakeven_usd` を埋める. 対象 {counts['total']} 件中、計算済 "
        f"{counts['with_breakeven']} 件. 重量・寸法・仕入価格の不足 listing は skip. "
        f"※ 内部的に country_code='US' 軸で計算 (W110(2) 4 区分別 BE 計算は別タスク)."
    )

    if st.button("💰 全 listing で損益分岐価格を計算", key="w119_btn_breakeven",
                 use_container_width=False):
        # 全 active listing を loop. 既存 helper update_listing_breakeven(eid, settings)
        # が ebay_listings から SELECT → compute_breakeven_price_usd → DB UPDATE を
        # ワンストップで実行する設計なので、wizard 側は purchase_yen 補完だけを担う.
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT ebay_item_id, weight_g, length_cm, width_cm, height_cm,
                          purchase_yen
                   FROM ebay_listings
                   WHERE (is_ended IS NULL OR is_ended=0)
                     AND title IS NOT NULL AND title != ''
                   ORDER BY ebay_item_id"""
            ).fetchall()

        if not rows:
            st.info("対象 listing がありません.")
            return

        progress = st.progress(0.0, text=f"breakeven 計算中... 0/{len(rows)}")
        updated = 0
        skipped = 0
        errored = 0

        for idx, row in enumerate(rows):
            ebay_item_id = row[0]
            weight_g = row[1]
            length_cm = row[2]
            width_cm = row[3]
            height_cm = row[4]
            purchase_yen = row[5]

            # 仕入価格未設定なら supplier_candidates から fetch して ebay_listings に永続化.
            # update_listing_breakeven は内部で ebay_listings.purchase_yen を読むので、
            # ここで書いておかないと None のまま breakeven 計算が NULL UPDATE に倒れる.
            if not purchase_yen:
                try:
                    fetched_yen = fetch_supplier_purchase_yen(ebay_item_id)
                except (sqlite3.OperationalError, KeyError, TypeError, ValueError) as e:
                    logger.warning(
                        f"[w119_step2] fetch_supplier_purchase_yen failed "
                        f"{ebay_item_id}: {e}"
                    )
                    fetched_yen = None
                if fetched_yen and fetched_yen > 0:
                    try:
                        # ✅ C-3 fix: lp_min_price は触らず purchase_yen のみ単独 UPDATE.
                        # set_listing_lowest_price_fields は両カラム同時 UPDATE で
                        # user 設定の lp_min_price を NULL に上書きしてしまう.
                        update_listing_purchase_yen(ebay_item_id, float(fetched_yen))
                        purchase_yen = fetched_yen
                    except (sqlite3.OperationalError, TypeError, ValueError) as e:
                        logger.warning(
                            f"[w119_step2] update_listing_purchase_yen failed "
                            f"{ebay_item_id}: {e}"
                        )

            # 必要条件揃っていなければ skip
            if not (weight_g and length_cm and width_cm and height_cm and purchase_yen):
                skipped += 1
                progress.progress(
                    (idx + 1) / len(rows),
                    text=f"breakeven 計算中... {idx + 1}/{len(rows)} (skip {skipped})",
                )
                continue

            # ✅ C-1 fix: 第二引数は settings dict (config), float 渡しだと NULL UPDATE 事故.
            try:
                breakeven = update_listing_breakeven(ebay_item_id, config)
                if breakeven is not None and breakeven > 0:
                    updated += 1
                else:
                    skipped += 1
            except (sqlite3.OperationalError, TypeError, ValueError, KeyError) as e:
                logger.warning(f"[w119_step2] breakeven 計算エラー {ebay_item_id}: {e}")
                errored += 1

            progress.progress(
                (idx + 1) / len(rows),
                text=f"breakeven 計算中... {idx + 1}/{len(rows)} (更新 {updated} / skip {skipped})",
            )

        progress.empty()
        st.success(
            f"✅ 完了: 更新 {updated} 件 / skip {skipped} 件 (重量/寸法/仕入価格不足) / "
            f"エラー {errored} 件"
        )


# =============================================================================
# Step 3: 検索ワード一括生成
# =============================================================================

def _render_step3(config: dict, counts: dict) -> None:
    st.markdown("#### Step 3: 検索ワード一括生成 (Opus 4.7)")
    st.caption(
        f"全 listing の title から検索ワードを Opus 4.7 で抽出. "
        f"対象 {counts['total']} 件中、生成済 {counts['with_keyword']} 件. "
        f"未生成のみが対象 (force_all=True で全件再生成)."
    )

    force_all = st.checkbox(
        "全件再生成 (既存 search_keyword を上書き)",
        value=False,
        key="w119_keyword_force_all",
        help="未生成 listing のみ対象がデフォルト. ON で全件再生成.",
    )

    col1, col2 = st.columns(2)
    with col1:
        batch_btn = st.button(
            "🔑 検索ワード一括生成 (batch / 安価 ~$3)",
            key="w119_btn_keyword_batch",
            use_container_width=True,
            help="Anthropic Message Batches API (50% off). 通常 30 分前後で完了. "
                 "queue 障害時は遅延あり.",
        )
    with col2:
        sync_btn = st.button(
            "⚡ 検索ワード一括生成 (通常 API / 確実 ~$6)",
            key="w119_btn_keyword_sync",
            use_container_width=True,
            help="通常 API で 1 件ずつ loop. ~11 分で確実完走. batch 障害時の fallback. "
                 "進捗を逐次 DB 保存するので中断時も再開可能.",
        )

    if batch_btn or sync_btn:
        if not _has_anthropic_key():
            st.error(
                "ANTHROPIC_API_KEY が未設定. .env または環境変数で設定してから再実行してください."
            )
        elif batch_btn:
            with st.spinner(
                "Opus 4.7 batch submit + poll 中... (通常 30 分前後、長くて 4h)"
            ):
                try:
                    result = run_generate_search_keywords(force_all=force_all)
                    if result.error_message:
                        st.error(f"❌ {result.error_message}")
                    else:
                        st.success(
                            f"✅ 完了 (batch): 投入 {result.submitted} 件 / "
                            f"成功 {result.succeeded} 件 / エラー {result.errored} 件 / "
                            f"所要 {result.duration_sec:.0f}s"
                        )
                        if result.errored > 0:
                            st.info(
                                f"ℹ️ エラー {result.errored} 件は DB NULL のまま. "
                                f"下表の手動編集で補完できます."
                            )
                except Exception as e:
                    logger.exception("[w119_step3] batch 生成例外")
                    st.error(f"batch 生成エラー: {e}")
        elif sync_btn:
            from tasks.task_generate_search_keywords import run_generate_search_keywords_sync
            progress_bar = st.progress(0.0, text="準備中...")
            status_box = st.empty()

            def _cb(idx, total, succ, err):
                progress_bar.progress(
                    idx / total,
                    text=f"通常 API 実行中... [{idx}/{total}] succ={succ} err={err}",
                )

            try:
                result = run_generate_search_keywords_sync(
                    force_all=force_all, progress_callback=_cb
                )
                progress_bar.empty()
                if result.error_message:
                    st.error(f"❌ {result.error_message}")
                else:
                    st.success(
                        f"✅ 完了 (sync): 投入 {result.submitted} 件 / "
                        f"成功 {result.succeeded} 件 / エラー {result.errored} 件 / "
                        f"所要 {result.duration_sec:.0f}s"
                    )
                    if result.errored > 0:
                        st.info(
                            f"ℹ️ エラー {result.errored} 件は DB NULL のまま. "
                            f"下表の手動編集で補完できます."
                        )
            except Exception as e:
                progress_bar.empty()
                logger.exception("[w119_step3] sync 生成例外")
                st.error(f"sync 生成エラー: {e}")

    # ─── 手動編集表 (UI で個別修正) ───
    st.markdown("##### 手動編集 (生成結果の調整)")
    listings = _get_active_listings_for_keyword_edit()
    if not listings:
        st.info("active listing がありません.")
        return

    # 表示件数制限
    show_only_missing = st.checkbox(
        "未生成のみ表示", value=False, key="w119_kw_filter_missing"
    )
    visible = [
        it for it in listings if (not show_only_missing) or not it["search_keyword"]
    ]
    st.caption(f"表示中: {len(visible)} / {len(listings)} 件")

    # ページング (M-4 fix: PAGE_SIZE を file top の constants に移動)
    total_pages = max(1, (len(visible) + _STEP3_PAGE_SIZE - 1) // _STEP3_PAGE_SIZE)
    page = st.number_input(
        "ページ",
        min_value=1,
        max_value=total_pages,
        value=1,
        key="w119_kw_page",
    )
    start = (int(page) - 1) * _STEP3_PAGE_SIZE
    page_items = visible[start : start + _STEP3_PAGE_SIZE]

    for it in page_items:
        cols = st.columns([4, 3, 1])
        with cols[0]:
            st.markdown(
                f"**{it['title'][:60]}**  \n"
                f"<small>ID: {it['ebay_item_id']} / source: "
                f"{it['search_keyword_source'] or '未生成'}</small>",
                unsafe_allow_html=True,
            )
        with cols[1]:
            new_kw = st.text_input(
                "search_keyword",
                value=it["search_keyword"] or "",
                key=f"w119_kw_input_{it['ebay_item_id']}",
                label_visibility="collapsed",
                placeholder="(未生成)",
            )
            if new_kw != (it["search_keyword"] or ""):
                if st.button(
                    "💾 保存",
                    key=f"w119_kw_save_{it['ebay_item_id']}",
                ):
                    if update_search_keyword_manual(it["ebay_item_id"], new_kw):
                        st.success("✅ 保存しました")
                        st.rerun()
                    else:
                        st.error("空文字は保存できません")
        with cols[2]:
            if it["search_keyword"]:
                url = build_ebay_search_url(it["search_keyword"])
                st.link_button("🔗 eBay", url, use_container_width=True)


# =============================================================================
# Step 4: 競合検索 + 一括承認
# =============================================================================

def _render_step4(config: dict, counts: dict) -> None:
    st.markdown("#### Step 4: 競合検索 + 競合 DB 登録")
    st.caption(
        "**📦 一括モード** (初回推奨): 全 listing で Browse API call → listing 別の expander で "
        "review → select-all で一括登録. **🎯 単件モード** (個別調整用): listing を 1 件選んで "
        "詳細検索 + 競合選択."
    )

    listings = _get_active_listings_for_keyword_edit()
    listings_with_kw = [it for it in listings if it["search_keyword"]]

    if not listings_with_kw:
        st.warning(
            "search_keyword 設定済 listing がありません. Step 3 で生成または手動編集してください."
        )
        return

    # モード切替 tab
    mode_tab_bulk, mode_tab_single = st.tabs(["📦 一括モード (初回推奨)", "🎯 単件モード"])

    with mode_tab_bulk:
        _render_step4_bulk(config, listings_with_kw)

    with mode_tab_single:
        _render_step4_single(config, listings_with_kw)


def _render_step4_single(config: dict, listings_with_kw: list[dict]) -> None:
    """単件モード: listing を 1 件選んで詳細レビュー + 登録 (既存 Step 4 ロジック)."""
    # 対象 listing 選択
    options = {
        f"{it['title'][:60]} (ID:{it['ebay_item_id'][-6:]})": it
        for it in listings_with_kw
    }
    selected_label = st.selectbox(
        "競合検索する listing を選択",
        options=list(options.keys()),
        key="w119_step4_listing_select",
    )
    selected = options.get(selected_label)
    if not selected:
        return

    ebay_item_id = selected["ebay_item_id"]
    keyword = selected["search_keyword"]
    breakeven = selected.get("lp_breakeven_usd")
    current_price = selected.get("current_price")
    shipping_cost = selected.get("shipping_cost")
    primary_market = selected.get("primary_market") or "-"

    # 現在価格 + 送料 + 合計
    if current_price is not None and current_price > 0:
        cp = float(current_price)
        sh = float(shipping_cost) if shipping_cost is not None else 0.0
        my_price_str = (
            f"現在価格: ${cp:.2f} + 送料 ${sh:.2f} = **合計 ${cp + sh:.2f}**"
        )
    else:
        my_price_str = "現在価格: -"

    breakeven_str = (
        f"**breakeven (損益分岐): ${breakeven:.2f}**"
        if breakeven is not None and breakeven > 0
        else "breakeven: 未計算 (Step 2)"
    )
    st.markdown(
        f"**検索ワード**: `{keyword}` | ID: `{ebay_item_id}` | "
        f"市場区分: **{primary_market}** | "
        f"{my_price_str} | {breakeven_str} | "
        f"[🔗 eBay で開く]({build_ebay_search_url(keyword)})"
    )

    # Browse API 検索ボタン
    search_key = f"w119_step4_search_{ebay_item_id}"
    if st.button("🔍 類似商品 30 件検索 (Browse API)", key=f"{search_key}_btn"):
        try:
            # 2026-05-11 fix: monitor.credentials.get_ebay_credentials() 経由 (W183 と同方式).
            # 旧 path `config["ebay"]["api"]["client_id"]` は schedule_config.json 構造と不一致.
            from monitor.credentials import get_ebay_credentials
            creds = get_ebay_credentials(config or {})
            app_id = creds.get("app_id", "")
            cert_id = creds.get("cert_id", "")
            if not (app_id and cert_id):
                st.error("eBay API credentials が config に設定されていません.")
                return
            from tasks.ebay_browse_api import BrowseAPIClient
            client = BrowseAPIClient(app_id=app_id, cert_id=cert_id)
            with st.spinner(f"Browse API で '{keyword}' を検索中..."):
                items = client.search_items(
                    query=keyword,
                    limit=_BROWSE_API_LIMIT,
                    item_location_country="JP",
                    delivery_country="US",
                    sort="price",
                )
            st.session_state[search_key] = items
        except Exception as e:
            logger.exception("[w119_step4] Browse API 検索エラー")
            st.error(f"Browse API 検索エラー: {e}")
            return

    items = st.session_state.get(search_key)
    if not items:
        return

    # ✅ C-2 fix: Browse API itemId は 'v1|<legacy>|0' 形式. legacy ID に変換しないと
    # competitor_products.competitor_item_id への保存後、W183 の cid.isdigit() check で
    # 全件 reject されて値下げ pipeline が壊れる. 全 item で legacy_item_id を計算.
    for it in items:
        it["legacy_item_id"] = extract_legacy_item_id(it.get("item_id", ""))

    # client-side sort: total_cost = price + shipping_cost (None は末尾)
    for it in items:
        price = it.get("price_usd") or 0.0
        ship = it.get("shipping_cost_usd")
        it["total_cost_usd"] = (price + ship) if ship is not None else None

    items_sorted = sorted(
        items,
        key=lambda x: (x["total_cost_usd"] is None, x["total_cost_usd"] or float("inf")),
    )
    top_items = items_sorted[:_DISPLAY_TOP_N]

    # 自分自身の出品は除外 (legacy ID で比較. 既存 ebay_item_id も legacy 形式)
    top_items = [it for it in top_items if it.get("legacy_item_id") != ebay_item_id]

    if not top_items:
        st.info("検索結果が空 or 自分の出品のみでした.")
        return

    st.markdown(f"##### 検索結果 上位 {len(top_items)} 件 (Price + Shipping 安い順)")

    # 既存の競合 DB を取得 (legacy ID で marker)
    with get_conn() as conn:
        existing_competitor_ids = {
            r[0] for r in conn.execute(
                "SELECT competitor_item_id FROM competitor_products WHERE our_item_id=?",
                (ebay_item_id,),
            ).fetchall()
        }

    # H-1 (silent destruction 防止): upsert_listing_competitors は全置換セマンティクス.
    # top 10 に含まれない既存登録は inactive 化される旨を user に明示.
    if existing_competitor_ids:
        existing_in_top = {
            it["legacy_item_id"]
            for it in top_items
            if it["legacy_item_id"] in existing_competitor_ids
        }
        outside_count = len(existing_competitor_ids) - len(existing_in_top)
        if outside_count > 0:
            st.warning(
                f"⚠️ 既存登録 {len(existing_competitor_ids)} 件のうち、本検索結果 top 10 に "
                f"含まれない {outside_count} 件は登録ボタン押下で **inactive 化** されます "
                f"(置換セマンティクス). 既存を維持したい場合は「登録済」chip が ON になっている "
                f"件を含めて選択してください."
            )

    selected_ids: list[str] = []
    for idx, it in enumerate(top_items):
        cols = st.columns([0.5, 4, 1.5, 1.5, 1])
        legacy_iid = it["legacy_item_id"]
        total = it.get("total_cost_usd")
        # 競合の合計が自分の breakeven を下回る = 値下げ追従すると赤字
        below_breakeven = (
            breakeven is not None and breakeven > 0
            and total is not None and total < breakeven
        )
        with cols[0]:
            already = legacy_iid in existing_competitor_ids
            checked = st.checkbox(
                " ",
                value=already,
                key=f"w119_step4_chk_{ebay_item_id}_{legacy_iid}",
                label_visibility="collapsed",
                help="登録済" if already else "未登録",
            )
            if checked and legacy_iid:
                selected_ids.append(legacy_iid)
        with cols[1]:
            title_short = (it.get("title") or "")[:80]
            st.markdown(
                f"<small>{title_short}</small><br>"
                f"<small>id: <code>{legacy_iid}</code> | "
                f"seller: <code>{it.get('seller', '')}</code> "
                f"({it.get('feedback_score', 0)} / {it.get('feedback_percentage', '')}%) "
                f" cond: {it.get('condition', '')}</small>",
                unsafe_allow_html=True,
            )
        with cols[2]:
            price = it.get("price_usd") or 0.0
            ship = it.get("shipping_cost_usd")
            ship_str = f"+${ship:.2f}" if ship is not None else "+?"
            st.markdown(f"**${price:.2f}** {ship_str}")
        with cols[3]:
            total_str = f"${total:.2f}" if total is not None else "?"
            if below_breakeven:
                st.markdown(
                    f"⚠️ **合計 {total_str}**<br>"
                    f"<small style='color:#ff6b6b'>(breakeven 以下、追従赤字)</small>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(f"**合計 {total_str}**")
        with cols[4]:
            if it.get("item_url"):
                st.link_button("🔗", it["item_url"])

    # 一括登録ボタン (置換セマンティクス: H-1 fix で文言明示)
    st.markdown("---")
    register_btn = st.button(
        f"✅ 競合リストを選択した {len(selected_ids)} 件で置き換え",
        key=f"w119_step4_register_{ebay_item_id}",
        disabled=(len(selected_ids) == 0),
        type="primary",
        help="既存の競合登録を inactive 化し、選択した件のみ active 化します.",
    )
    if register_btn and selected_ids:
        try:
            upsert_listing_competitors(
                our_item_id=ebay_item_id,
                competitor_item_ids=selected_ids,
            )
            st.success(
                f"✅ {len(selected_ids)} 件を競合 DB に置き換え登録しました. "
                f"次回 W183 値下げ scheduler (00:45/06:45/12:45/18:45 JST) で自動チェック."
            )
        except (sqlite3.OperationalError, ValueError, TypeError) as e:
            logger.exception("[w119_step4] 競合 DB 登録エラー")
            st.error(f"競合 DB 登録エラー: {e}")


# =============================================================================
# Step 4 一括モード: 全 listing Browse API search → expander review → 一括 upsert
# =============================================================================

def _render_step4_bulk(config: dict, listings_with_kw: list[dict]) -> None:
    """一括モード: 全 listing で Browse API search → listing 別に top 10 表示 → 一括 upsert.

    UX:
      Step A: 「🔍 全 N listing で一括検索」ボタン → 進捗 bar (~1.5 sec/listing で 6-10 分)
      Step B: 結果を listing 別の expander で表示 (各 expander 内に top 10 + checkbox + select-all)
      Step C: 「✅ 全 listing で選択分を一括登録」ボタン → listing 別に upsert_listing_competitors

    結果は `st.session_state["w119_step4_bulk_results"]` に保持. 再生成不要 (再 search ボタンで上書き).
    """
    n_listings = len(listings_with_kw)
    st.markdown(f"**対象 listing: {n_listings} 件** "
                f"(search_keyword 設定済の active listing 全件)")

    # ── Step A: 一括検索 ──
    bulk_key = "w119_step4_bulk_results"
    has_results = bulk_key in st.session_state and st.session_state[bulk_key]

    if has_results:
        prev = st.session_state[bulk_key]
        n_with_results = sum(1 for v in prev.values() if v)  # 競合あり (top_items 非空)
        n_zero = sum(1 for v in prev.values() if v == [])    # 真の競合 0 件
        n_failed = sum(1 for v in prev.values() if v is None)  # Browse API 失敗
        msg = (
            f"📊 前回の検索結果: {n_with_results} 件競合あり / "
            f"{n_zero} 件競合 0 / **{n_failed} 件 API 失敗** "
            f"({n_listings} listing 中). 下の expander で listing 別 review → 一括登録."
        )
        if n_failed > 0:
            st.warning(msg + f" ⚠ 失敗 {n_failed} 件は再検索推奨.")
        else:
            st.info(msg)
        col1, col2 = st.columns(2)
        with col1:
            search_btn = st.button(
                "🔄 全 listing で再検索 (前回結果を破棄)",
                key="w119_step4_bulk_refresh", use_container_width=True,
            )
        with col2:
            if st.button("🗑️ 検索結果をクリア",
                         key="w119_step4_bulk_clear", use_container_width=True):
                st.session_state.pop(bulk_key, None)
                # 全 checkbox state も clear
                for k in list(st.session_state.keys()):
                    if k.startswith("w119_step4_bulk_chk_"):
                        del st.session_state[k]
                st.rerun()
    else:
        # 1 call ≒ 0.5s sleep + ~0.3s HTTP latency = 0.8s/listing
        est_min = n_listings * (_BULK_BROWSE_SLEEP_SEC + 0.3) / 60
        col1, col2 = st.columns(2)
        with col1:
            search_btn = st.button(
                f"🔍 全 {n_listings} listing で一括検索 (~{est_min:.0f} 分)",
                key="w119_step4_bulk_search", use_container_width=True, type="primary",
            )
        with col2:
            # 2026-05-11: CLI 経由で先行実行した結果 (data/w119_bulk_results.json) を load.
            # browser session を又いで結果を共有する経路.
            load_btn = st.button(
                "📁 CLI 実行結果から読込 (JSON)",
                key="w119_step4_bulk_load_json", use_container_width=True,
                help="scripts/run_w119_bulk_browse.py で先行実行した結果を読込 "
                     "(data/w119_bulk_results.json).",
            )
        if load_btn:
            _load_bulk_results_from_json(bulk_key)
            st.rerun()

    if search_btn:
        _execute_bulk_browse_search(config, listings_with_kw, bulk_key)
        st.rerun()  # 完了後に結果表示するため

    # ── Step B/C: 結果表示 + 一括登録 ──
    results: dict = st.session_state.get(bulk_key) or {}
    if not results:
        return

    _render_bulk_results_and_register(results, listings_with_kw)


def _load_bulk_results_from_json(bulk_key: str) -> None:
    """data/w119_bulk_results.json から検索結果を読み込み session_state に注入.

    CLI script (`scripts/run_w119_bulk_browse.py`) で先行実行した結果を共有する経路.
    Streamlit session_state は browser session ごとに独立するため、
    JSON 経由で異なる session 間の結果共有を可能にする.
    """
    import json
    from pathlib import Path
    json_path = Path(__file__).resolve().parent.parent / "data" / "w119_bulk_results.json"
    if not json_path.exists():
        st.error(
            f"JSON ファイル不在: {json_path.name}. 先に CLI で実行: "
            f"`python scripts/run_w119_bulk_browse.py`"
        )
        return
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        st.error(f"JSON 読込エラー: {e}")
        return

    results = data.get("results") or {}
    meta = data.get("meta") or {}
    st.session_state[bulk_key] = results
    st.success(
        f"✅ JSON 読込完了: {meta.get('n_listings', '?')} listing "
        f"(競合あり {meta.get('n_with_competitors', '?')} / "
        f"0 件 {meta.get('n_zero_competitors', '?')} / "
        f"失敗 {meta.get('n_failed', '?')}). "
        f"生成時刻: {meta.get('generated_at', '?')}"
    )


def _execute_bulk_browse_search(
    config: dict, listings_with_kw: list[dict], bulk_key: str
) -> None:
    """Step A の本体: 全 listing で Browse API 順次 call + 進捗表示."""
    # 2026-05-11 fix: monitor.credentials.get_ebay_credentials() 経由 (W183 と同方式).
    from monitor.credentials import get_ebay_credentials
    creds = get_ebay_credentials(config or {})
    app_id = creds.get("app_id", "")
    cert_id = creds.get("cert_id", "")
    if not (app_id and cert_id):
        st.error("eBay API credentials が config に設定されていません.")
        return
    from tasks.ebay_browse_api import BrowseAPIClient
    client = BrowseAPIClient(app_id=app_id, cert_id=cert_id)

    # results value 規約 (silent skip 防止):
    #   list (≥1 件)  → 競合発見
    #   []           → 検索成功だが 0 件 (真の競合なし)
    #   None         → Browse API call 失敗 (429 / timeout / 認証エラー等、要再検索)
    results: dict = {}
    progress_bar = st.progress(0.0, text="準備中...")
    n = len(listings_with_kw)
    n_with_results = 0  # M-2 fix: O(N²) → O(1) counter
    n_failed = 0
    last_failed_id = ""

    for idx, it in enumerate(listings_with_kw, start=1):
        ebay_item_id = it["ebay_item_id"]
        keyword = it["search_keyword"]
        # H-1 fix: time.sleep で 429 burst rate-limit 緩和 (5 RPS spec → 2 RPS 自主)
        if idx > 1:
            time.sleep(_BULK_BROWSE_SLEEP_SEC)
        try:
            items = client.search_items(
                query=keyword,
                limit=_BROWSE_API_LIMIT,
                item_location_country="JP",
                delivery_country="US",
                sort="price",
            )
        except (httpx.HTTPError, httpx.TimeoutException, ValueError, KeyError) as e:
            logger.warning(f"[w119_bulk] Browse API failed {ebay_item_id}: "
                           f"{type(e).__name__}: {e}")
            results[ebay_item_id] = None  # 失敗 sentinel (真の 0 と区別)
            n_failed += 1
            last_failed_id = ebay_item_id
            progress_bar.progress(
                idx / n,
                text=f"検索中... [{idx}/{n}] 競合 {n_with_results} / 失敗 {n_failed}",
            )
            continue

        top_items = _process_browse_items(items, ebay_item_id)
        results[ebay_item_id] = top_items
        if top_items:
            n_with_results += 1

        progress_bar.progress(
            idx / n,
            text=f"検索中... [{idx}/{n}] 競合 {n_with_results} / 失敗 {n_failed}",
        )

    progress_bar.empty()
    st.session_state[bulk_key] = results
    msg = (f"一括検索完了: {n} listing 中 競合あり {n_with_results} / 競合 0 件 "
           f"{n - n_with_results - n_failed} / API 失敗 {n_failed}")
    if n_failed > 0:
        st.warning(f"⚠ {msg}. 失敗 {n_failed} 件 (例: {last_failed_id}) は再検索推奨.")
    else:
        st.success(f"✅ {msg}")


def is_likely_long_window_shipping(item: dict) -> bool:
    """**Economy carrier (長配送窓) の proxy 判別**. SpeedPAK Economy / Surface mail 等.

    ⚠️ DDU (関税ポリシー) 判別ではない. carrier (配送方法) のみ判定する.
    詳細: `reference_shipping_method_vs_ddu_taxonomy.md` (配送方法と関税ポリシーは独立軸).

    判別ロジック (delivery window heuristic):
      - max_delivery - min_delivery >= _LIKELY_ECONOMY_DELIVERY_WINDOW_DAYS (10) 日 → Economy 系
        (express 配送は window 1-3 日、Economy は 10-14 日が標準)
      - delivery 情報なし → 判別不能 (False、残す方針 = false negative 許容)

    Returns True なら Economy 配送として除外対象.
    """
    from datetime import datetime
    min_str = item.get("min_delivery_date")
    max_str = item.get("max_delivery_date")
    if not (min_str and max_str):
        return False
    try:
        min_d = datetime.fromisoformat(min_str.replace("Z", "+00:00"))
        max_d = datetime.fromisoformat(max_str.replace("Z", "+00:00"))
        window_days = (max_d - min_d).days
        return window_days >= _LIKELY_ECONOMY_DELIVERY_WINDOW_DAYS
    except (ValueError, TypeError):
        return False


# 後方互換 alias (旧 test / 旧 caller 用). 新規 caller は `is_likely_long_window_shipping` を使うこと.
is_likely_ddu_shipping = is_likely_long_window_shipping


def _process_browse_items(items: list[dict], my_ebay_item_id: str) -> list[dict]:
    """Browse API 結果を加工して top N 件抽出.

    手順:
      1. legacy_item_id 抽出 + total_cost (price + shipping) 計算
      2. 自分自身を除外
      3. **Economy 配送 (carrier 軸、SpeedPAK Economy 等) を除外** (delivery window heuristic)
      4. total_cost 安い順で sort
      5. 上位 _DISPLAY_TOP_N 件返却

    ⚠️ Economy 除外は **carrier 軸** のみ. 関税ポリシー (DDU/DDP) 軸は別途
    `tab_product_management._is_ddu_policy()` で判定 (`taxes` field 利用、別軸独立).
    詳細: `reference_shipping_method_vs_ddu_taxonomy.md`.
    """
    for it in items:
        it["legacy_item_id"] = extract_legacy_item_id(it.get("item_id", ""))
        price = it.get("price_usd") or 0.0
        ship = it.get("shipping_cost_usd")
        it["total_cost_usd"] = (price + ship) if ship is not None else None
        it["is_long_window_shipping"] = is_likely_long_window_shipping(it)
        it["is_likely_ddu"] = it["is_long_window_shipping"]  # 後方互換 (旧 caller 用)

    items_sorted = sorted(
        items,
        key=lambda x: (x["total_cost_usd"] is None, x["total_cost_usd"] or float("inf")),
    )
    top = [
        it for it in items_sorted
        if it["legacy_item_id"]
        and it["legacy_item_id"] != my_ebay_item_id
        and not it["is_long_window_shipping"]
    ][:_DISPLAY_TOP_N]
    return top


def _render_bulk_results_and_register(
    results: dict, listings_with_kw: list[dict]
) -> None:
    """Step B/C: listing 別 expander 表示 + 「一括登録」ボタン."""
    title_by_id = {it["ebay_item_id"]: it for it in listings_with_kw}

    # 既存 competitor_products を一括取得
    with get_conn() as conn:
        all_existing = conn.execute(
            "SELECT our_item_id, competitor_item_id FROM competitor_products WHERE is_active=1"
        ).fetchall()
    existing_map: dict = {}
    for our, comp in all_existing:
        existing_map.setdefault(our, set()).add(comp)

    # フィルタ: listing 別に「結果あり」 (top_items が非空) のみ expander 表示.
    # None (失敗) / [] (真 0 件) は expander 表示しない (登録対象外).
    listings_with_results = [
        (lid, results[lid]) for lid in results
        if results[lid]  # truthy = list with items
        and lid in title_by_id
    ]
    st.markdown(f"##### 競合発見済: {len(listings_with_results)} listing (top 10 各)")

    # ── 全体 select-all (H-2 fix) ──
    # Streamlit checkbox は同 key で再描画されると `value=` が無視される性質を回避するため、
    # select_all の bool 値を子 checkbox key に注入. select_all トグル時に key 名前空間が
    # 切り替わり、初期値 (value=) が反映される. 副作用: user が個別 uncheck した状態は
    # select_all 切替時に reset される (= select_all トグルは "fresh selection state" を意味する).
    select_all_key = "w119_step4_bulk_select_all"
    select_all = st.checkbox(
        "🔘 全 listing で top 10 全件選択 (チェックで全 ON、外して個別選択モードに戻す)",
        value=False,
        key=select_all_key,
        help="トグルすると個別 checkbox 状態は reset される (key 名前空間切替). "
             "ON: 全件選択 / OFF: 既存登録のみ ON. その後 expander 内で個別調整可.",
    )
    # key suffix で名前空間切替
    sa_suffix = "all" if select_all else "ind"

    # ── listing 別 expander ──
    total_selected = 0
    listing_selections: dict = {}  # ebay_item_id → list[legacy_iid]

    for ebay_item_id, top_items in listings_with_results:
        meta = title_by_id[ebay_item_id]
        title_preview = (meta["title"] or "")[:60]
        n_existing = len(existing_map.get(ebay_item_id, set()))
        breakeven = meta.get("lp_breakeven_usd")
        current_price = meta.get("current_price")
        shipping_cost = meta.get("shipping_cost")
        primary_market = meta.get("primary_market") or "-"

        # 現在価格 + 送料 + 合計 を組み立て
        my_total = None
        if current_price is not None and current_price > 0:
            cp = float(current_price)
            sh = float(shipping_cost) if shipping_cost is not None else 0.0
            my_total = cp + sh
            my_price_str = (
                f"現在価格: ${cp:.2f} + 送料 ${sh:.2f} = **合計 ${my_total:.2f}**"
            )
        else:
            my_price_str = "現在価格: -"

        with st.expander(
            f"{title_preview} ({len(top_items)} 候補 / 既存登録 {n_existing} 件) "
            f"[{primary_market}]",
            expanded=False,
        ):
            # 自分の listing 情報: 現在価格 + 損益分岐 + 市場区分
            breakeven_str = (
                f"**breakeven (損益分岐): ${breakeven:.2f}**"
                if breakeven is not None and breakeven > 0
                else "breakeven: 未計算 (仕入価格 + 重量 + 寸法が揃ったら Step 2 で計算可)"
            )
            st.caption(
                f"検索ワード: `{meta['search_keyword']}` | "
                f"ID: {ebay_item_id} | "
                f"市場区分: **{primary_market}** | "
                f"{my_price_str} | "
                f"{breakeven_str} | "
                f"[🔗 eBay で開く]({build_ebay_search_url(meta['search_keyword'])})"
            )

            selected_for_listing: list = []
            existing_for_listing = existing_map.get(ebay_item_id, set())

            for it in top_items:
                legacy = it["legacy_item_id"]
                already = legacy in existing_for_listing
                # default 値: 既存登録 OR select_all モード
                default_checked = already or select_all
                # 競合の合計が自分の breakeven を下回る = 値下げ追従すると赤字
                total = it.get("total_cost_usd")
                below_breakeven = (
                    breakeven is not None and breakeven > 0
                    and total is not None and total < breakeven
                )
                cols = st.columns([0.5, 4, 1.5, 1.5, 1])
                with cols[0]:
                    checked = st.checkbox(
                        " ",
                        value=default_checked,
                        # H-2 fix: select_all state を key に含めて名前空間切替
                        key=f"w119_step4_bulk_chk_{sa_suffix}_{ebay_item_id}_{legacy}",
                        label_visibility="collapsed",
                        help="登録済" if already else "未登録",
                    )
                    if checked and legacy:
                        selected_for_listing.append(legacy)
                with cols[1]:
                    title_short = (it.get("title") or "")[:80]
                    st.markdown(
                        f"<small>{title_short}</small><br>"
                        f"<small>id: <code>{legacy}</code> | "
                        f"seller: <code>{it.get('seller', '')}</code> "
                        f"({it.get('feedback_score', 0)} / {it.get('feedback_percentage', '')}%)"
                        f" cond: {it.get('condition', '')}</small>",
                        unsafe_allow_html=True,
                    )
                with cols[2]:
                    price = it.get("price_usd") or 0.0
                    ship = it.get("shipping_cost_usd")
                    ship_str = f"+${ship:.2f}" if ship is not None else "+?"
                    st.markdown(f"**${price:.2f}** {ship_str}")
                with cols[3]:
                    total_str = f"${total:.2f}" if total is not None else "?"
                    # breakeven 下回る場合は警告色
                    if below_breakeven:
                        st.markdown(
                            f"⚠️ **合計 {total_str}**<br>"
                            f"<small style='color:#ff6b6b'>(breakeven 以下、追従赤字)</small>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(f"**合計 {total_str}**")
                with cols[4]:
                    if it.get("item_url"):
                        st.link_button("🔗", it["item_url"])

            listing_selections[ebay_item_id] = selected_for_listing
            total_selected += len(selected_for_listing)

    # ── Step C: 一括登録ボタン ──
    st.markdown("---")

    # 警告: 置換セマンティクスにより既存外件数を表示
    will_remove_total = 0
    for ebay_item_id, selected in listing_selections.items():
        existing = existing_map.get(ebay_item_id, set())
        will_remove = existing - set(selected)
        will_remove_total += len(will_remove)
    if will_remove_total > 0:
        st.warning(
            f"⚠️ upsert_listing_competitors は **置換セマンティクス**. "
            f"現在の選択で **{will_remove_total} 件の既存 active 競合が inactive 化** されます. "
            f"既存維持したい場合は対応 expander で checkbox を ON にしてください."
        )

    register_btn = st.button(
        f"✅ 全 listing で選択した合計 {total_selected} 件を競合 DB に一括登録",
        key="w119_step4_bulk_register",
        disabled=(total_selected == 0),
        type="primary",
        help="各 listing で「現在選択中の competitor 集合」で置き換え upsert.",
    )
    if register_btn:
        _execute_bulk_register(listing_selections)


def _execute_bulk_register(listing_selections: dict) -> None:
    """Step C 本体: listing 別に upsert_listing_competitors を順次実行.

    M-3 fix: 「選択 0 listing は skip」を結果メッセージで明示.
    """
    ok = 0
    failed = 0
    skipped_zero = 0  # 選択 0 listing 数 (silent skip 化させない、UI に明示)
    listing_ok = 0
    total = sum(len(v) for v in listing_selections.values())
    for ebay_item_id, selected_ids in listing_selections.items():
        if not selected_ids:
            # 選択 0 = この listing の active 競合を全て inactive 化することになるが、
            # 「初回一括 listing で選択なし」は意図不明なので skip (置換しない、既存維持).
            skipped_zero += 1
            continue
        try:
            upsert_listing_competitors(
                our_item_id=ebay_item_id,
                competitor_item_ids=selected_ids,
            )
            ok += len(selected_ids)
            listing_ok += 1
        except (sqlite3.OperationalError, ValueError, TypeError) as e:
            logger.exception(f"[w119_bulk_register] 競合 DB 登録エラー {ebay_item_id}: {e}")
            failed += len(selected_ids)

    skip_msg = (f" / 選択 0 で skip {skipped_zero} listing (既存維持)"
                if skipped_zero else "")

    if failed == 0:
        st.success(
            f"✅ 一括登録完了: 競合 {ok} 件 ({listing_ok} listing){skip_msg}. "
            f"次回 W183 値下げ scheduler (00:45/06:45/12:45/18:45 JST) で全 listing 自動チェック."
        )
    else:
        st.error(
            f"⚠ 部分失敗: 成功 {ok} / 失敗 {failed} / 合計 {total}{skip_msg}. ログ確認推奨."
        )


# =============================================================================
# Public API
# =============================================================================

def render_research_wizard(config: dict) -> None:
    """W119 商品リサーチ自動化 wizard を最安値チェックタブの前段に描画.

    Args:
        config: schedule_config.json 内容 (Browse API + Anthropic credentials).
    """
    counts = _count_listings_state()

    with st.expander(
        f"📊 商品リサーチ自動化ウィザード (W119) — "
        f"重量 {counts['with_weight']}/{counts['total']} | "
        f"寸法 {counts['with_size']}/{counts['total']} | "
        f"損益分岐 {counts['with_breakeven']}/{counts['total']} | "
        f"検索ワード {counts['with_keyword']}/{counts['total']}",
        expanded=False,
    ):
        st.caption(
            "4 step を順次実行することで、最安値チェック → W183 自動値下げ pipeline が "
            "active な競合データで動作します. 各 step は独立で再実行可能 (idempotent)."
        )
        _render_step1(config, counts)
        st.markdown("---")
        _render_step2(config, counts)
        st.markdown("---")
        _render_step3(config, counts)
        st.markdown("---")
        _render_step4(config, counts)
