#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""商品管理 main tab — 1 商品の全情報を 2 列 layout で統合表示.

設計コンセプト (2026-05-11 v2 再設計):
- expander header に **要約 1 行** + サマリ chip
- 展開時は **2 列 layout** で可読性向上
  - 左: 基本情報 / 物理属性編集 / 仕入価格・下限価格 / 利益計算
  - 右: ライバル dataframe / 仕入先候補 dataframe / 在庫監視 status
- ライバル / 仕入先候補は **dataframe で一覧** (商品価格 / 送料 / 合計 / 配送目安 / 利益額)
- 「在庫切れ」フィルタで在庫切れ workflow に集中可能

user iterative 前提.
"""
from __future__ import annotations

import html
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from ui_cache import bump_db_version, get_db_version, seed_keyed_list_from_db
from tabs._supplier_description_pipeline import (
    apply_description_to_ebay,
    generate_supplier_description,
    prefetch_supplier_product_and_rank,
)

from monitor.database import (
    ack_sale_warning,
    add_or_reactivate_competitor,
    dismiss_sale_warning,
    get_conn,
    get_ebaymag_product,
    get_japan_competitor_alerts,
    get_listing_note,
    get_open_sale_warnings,
    get_rival_discoveries,
    record_ebaymag_apply,
    set_initial_registered,
    set_rival_search_keywords,
    set_rival_watch_enabled,
    update_alert_action,
    update_rival_discovery_status,
    upsert_ebaymag_product,
    upsert_listing_note,
)
from calculator import (
    load_settings as _load_calc_settings,
    SETTINGS_FILE as _SETTINGS_FILE,
    get_ebay_fvf_rate as _get_ebay_fvf_rate,
    category_in_fee_table as _category_in_fee_table,
)
from monitor.credentials import get_ebay_credentials
from monitor.ebay_client import (
    revise_fixed_price_with_shipping,
    revise_item_sku,
    revise_shipping_profile,
)
from monitor.lowest_price import (
    compute_breakeven_price_usd,
    fetch_alert_shipping_usd,
    fetch_supplier_purchase_yen,
    get_competitors_with_pricing,
    refresh_competitor_pricing,
    set_listing_lowest_price_fields,
    update_listing_breakeven,
    upsert_listing_competitors,
)


def _calc_settings() -> dict:
    """breakeven 計算用の settings dict.

    `schedule_config.json` ではなく `calculator.load_settings()` (= settings.json 系) を使う.
    必須キー: exchange_rate, duty_rate, ebay 系手数料, 送料表 etc.
    schedule_config を渡すと calculator が key 不在で RuntimeError になる.
    """
    return _load_calc_settings()

logger = logging.getLogger(__name__)

_MAX_COMPETITORS = 10       # 登録 active 競合の上限 (eBay business logic)
_DISPLAY_CANDIDATES = 20    # CLI 候補 / W184 alerts 表示上限 (登録判断材料を増やすため拡大)

# CLI 一括検索結果 JSON path (scripts/run_w119_bulk_browse.py の出力)
_BULK_RESULTS_JSON = Path(__file__).resolve().parent.parent / "data" / "w119_bulk_results.json"


# =============================================================================
# CLI bulk results loader (session-scoped cache for 1 rerun)
# =============================================================================

def _load_bulk_results_cached() -> tuple[dict, dict]:
    """data/w119_bulk_results.json を load. (meta, results_by_eid) を返す.

    2026-05-12: JSON mtime を cache key に含めて、CLI bulk script の再実行で
    自動 invalidate (旧実装は session_state cache のみで stale data を引きずる bug あり).

    H-NEW-3 (review #2): bulk script 書込中の partial write で `JSONDecodeError` が出ても
    **空 dict を cache に焼き付けない**. 次 rerun で再試行 (atomic-ish recovery).

    削除 / 不在の場合は (空 dict, 空 dict).
    """
    cache_key = "pm_bulk_results_cache"
    try:
        mtime = _BULK_RESULTS_JSON.stat().st_mtime if _BULK_RESULTS_JSON.exists() else 0.0
    except OSError:
        mtime = 0.0
    cache_token = (mtime,)
    cached = st.session_state.get(cache_key)
    if cached is not None and cached.get("token") == cache_token:
        return cached["meta"], cached["results"]

    meta: dict = {}
    results: dict = {}
    load_ok = False
    if _BULK_RESULTS_JSON.exists():
        try:
            data = json.loads(_BULK_RESULTS_JSON.read_text(encoding="utf-8"))
            meta = data.get("meta") or {}
            results = data.get("results") or {}
            load_ok = True
        except (json.JSONDecodeError, OSError) as e:
            # partial write / 中間状態. cache 汚染回避のため記録せず即 return.
            # 次 streamlit rerun で再試行 (mtime 安定後に成功する).
            logger.warning(f"[pm] bulk_results.json load error (skip caching): {e}")
            return meta, results
    else:
        load_ok = True  # ファイル不在 = 確定状態、空 dict を cache OK
    if load_ok:
        st.session_state[cache_key] = {"token": cache_token, "meta": meta, "results": results}
    return meta, results


# =============================================================================
# Data fetch
# =============================================================================

def _fetch_all_products() -> list[dict]:
    """全 active listing の管理情報を取得 (一覧表示用). inventory_count + quantity_ebay 含む."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                el.ebay_item_id, el.sku, el.title, el.current_price, el.shipping_cost,
                el.weight_g, el.length_cm, el.width_cm, el.height_cm,
                el.purchase_yen, el.lp_min_price, el.lp_breakeven_usd,
                el.primary_market, el.duty_rate_pct, el.section232_class,
                -- W222 (2026-06-05): per-listing 実カテゴリ。利益計算 (_cd_profit_breakdown)
                -- と一覧「カテゴリ」列で実カテゴリ FVF を反映 (従来は SELECT 漏れで常に
                -- 58248 fallback = 全 listing 既定 FVF 12.7%/13.6% で表示していた。floor は
                -- 既に実カテゴリ反映済 = lowest_price L161、表示をそれに一致させる)。
                el.category_id,
                -- W227 (2026-06-06): 商品状態 Condition (人気度 rank とは別軸)。
                -- ebay_condition_id=GetItem 実値(真実源) / condition_rank=Used サブランク補完。
                el.ebay_condition_id, el.condition_rank,
                el.rank, el.includes, el.warranty,
                el.point_yen, el.listing_description,
                el.total_sold_count, el.watch_count, el.view_count,
                el.source, el.source_url, el.source_status, el.source_last_checked,
                el.source_out_of_stock_since, el.source_url_manual,
                -- ③/BC2 修正 (2026-05-18): ebay_listings.competitor_min_price は
                -- writer (update_ebay_listing_competitor_info) が全コード未呼出の
                -- dead column = hero「競合最安」が永久に未入力だった。W183
                -- (task_rival_pricing) と同じく competitor_products から都度
                -- MIN(価格+送料) を算出 (新規 DB 書込不要・算出源を一本化)。
                (SELECT MIN(cp.competitor_price_usd
                            + COALESCE(cp.competitor_shipping_usd, 0))
                 FROM competitor_products cp
                 WHERE cp.our_item_id = el.ebay_item_id AND cp.is_active = 1
                   AND cp.competitor_price_usd IS NOT NULL
                ) AS competitor_min_price,
                el.quantity_ebay, el.inventory_count,
                el.last_qty_sync_at, el.last_synced_quantity, el.qty_sync_error,
                el.shipping_profile_id, el.shipping_profile_fetched_at,
                -- W142: +each 表示 source (根本原因#5b)。migration v43 +
                -- _sync_db_to_actual の書込を UI が read back する経路。
                -- last_synced_at は乖離 caption の鮮度 fallback に使う。
                el.shipping_additional_cost, el.shipping_additional_fetched_at,
                el.last_synced_at,
                (SELECT COUNT(*) FROM competitor_products cp
                 WHERE cp.our_item_id = el.ebay_item_id AND cp.is_active = 1
                ) AS competitor_count,
                -- W140: メモ有無 (📎 表示用)。listing 識別は ebay_item_id
                -- (sku-rules: SKU をキーにしない)。空文字 = メモ無し。
                (SELECT 1 FROM listing_notes ln
                 WHERE ln.ebay_item_id = el.ebay_item_id
                   AND ln.note_text IS NOT NULL
                   AND TRIM(ln.note_text) != ''
                ) AS has_note,
                -- W151 (2026-05-22): 初期登録 status (未完了 / 完了).
                -- COALESCE で v49 適用前の listing は 0 (未完了) 扱い.
                COALESCE(el.initial_registered, 0) AS initial_registered,
                el.initial_registered_at,
                -- W153 (2026-05-22): ライバル監視 4 列.
                -- listing 識別は ebay_item_id (sku-rules).
                COALESCE(el.rival_watch_enabled, 0) AS rival_watch_enabled,
                el.rival_search_keywords,
                el.rival_search_keywords_generated_at,
                el.rival_watch_started_at,
                -- W#33: キーワード監視 設定/未設定 フィルタ用。
                -- listing 識別は ebay_item_id (sku-rules.md)。is_active=1 のみ計上。
                (SELECT COUNT(*) FROM keyword_watches kw
                 WHERE kw.ebay_item_id = el.ebay_item_id AND kw.is_active = 1
                ) AS keyword_watch_count
            FROM ebay_listings el
            WHERE (el.is_ended IS NULL OR el.is_ended = 0)
              AND el.title IS NOT NULL AND el.title != ''
            ORDER BY el.ebay_item_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


# W134 Step2: 商品管理タブの全 listing 取得は重い (active 全件 + サブクエリ)。
# st.cache_data(ttl=60) でラップし db_version を cache key に混ぜる。書込側
# (_save_product_data / _update_ebay_reflected_fields / W133 confirm 等) が
# bump_db_version() を呼ぶと次回 read で最新を再取得する。
# 注: 本 wrapper は app.py でなく本モジュール内に置く (呼出元が同一モジュール
# であり app.py 側に置くと app.py⇄tab の循環 import になるため。spec の
# 「database.py 不改修・UI 層で wrap」の意図には沿う。db_version は先頭 _ を
# 付けない = st.cache_data の hash 対象に含めるため)。
@st.cache_data(ttl=3, show_spinner=False)
def _cd_fetch_all_products(db_version: int) -> list[dict]:
    return _fetch_all_products()


def _fetch_supplier_candidates_for_listing(ebay_item_id: str) -> list[dict]:
    """指定 listing の supplier_candidates 一覧 (profit_jpy DESC)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, candidate_url, candidate_title, candidate_price_jpy,
                      profit_jpy, profitable, match_score, status,
                      source_platform, created_at, user_action_at,
                      junk_likely_untested, alt_listing_possible, alt_listing_note
               FROM supplier_candidates
               WHERE ebay_item_id = ?
               ORDER BY status='applied' DESC,
                        (profit_jpy IS NULL), profit_jpy DESC,
                        match_score DESC
               LIMIT 20""",
            (ebay_item_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_monitored_items_for_listing(ebay_item_id: str) -> list[dict]:
    """指定 listing の monitored_items (在庫監視)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source_url, site_config_id, is_active,
                      last_status, last_check
               FROM monitored_items
               WHERE ebay_item_id = ?
               ORDER BY id ASC""",
            (ebay_item_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# Helpers
# =============================================================================

def _total_price(p: dict) -> tuple[float, float, float]:
    """(current_price, shipping_cost, total) を返す."""
    cp = float(p.get("current_price") or 0)
    sh = float(p.get("shipping_cost") or 0)
    return cp, sh, cp + sh


# W227 (2026-06-06): eBay ConditionID → 表示ラベル。
_CONDITION_ID_LABEL = {
    "1000": "New", "1500": "Open Box", "2000": "Manufacturer refurb",
    "2500": "Seller refurb", "2750": "Like New", "3000": "Used",
    "4000": "Very Good", "5000": "Good", "6000": "Acceptable",
    "7000": "For parts/As-Is", "未取得": "未取得",
}


def _condition_widget_initial(p: dict) -> str:
    """商品管理「状態」widget の初期値 (8 段階 N/S/A/B/C/D/PO/As-Is or "")。

    W227 根治: 人気度 rank 列ではなく **商品状態** を返す。優先順位:
      1. condition_rank (user が保存した状態意図) が 8 段階なら それ。
      2. ebay_condition_id (eBay 実値) 由来: 1000→N / 1500→S / 7000→As-Is。
         3000(Used) はサブランク(A/B/C/D/PO)逆引き不能 → "" (未設定、user 選択を促す)。
      3. いずれも無し (eBay 未取得 / 書籍 condition 等) → "".
    """
    sub = (p.get("condition_rank") or "").strip()
    if sub in ("N", "S", "A", "B", "C", "D", "PO", "As-Is"):
        return sub
    cid = str(p.get("ebay_condition_id") or "").strip()
    return {"1000": "N", "1500": "S", "7000": "As-Is"}.get(cid, "")


def _status_emoji(src_status: str) -> str:
    """source_status から emoji を返す."""
    return {
        "in_stock": "🟢",
        "out_of_stock": "🔴",
        "unknown": "⚪",
        "removed": "⚫",
        None: "❓",
        "": "❓",
    }.get(src_status, "❓")


def _delivery_days_from_now(min_date_str: Optional[str]) -> Optional[int]:
    """ISO 8601 配送日から今日までの日数を計算."""
    if not min_date_str:
        return None
    try:
        d = datetime.fromisoformat(min_date_str.replace("Z", "+00:00")).date()
        return (d - date.today()).days
    except (ValueError, TypeError):
        return None


def _is_economy_shipping(service_code: Optional[str], service_type: Optional[str]) -> bool:
    """**配送方法 (carrier)** が Economy 系か判別.

    ⚠️ DDU (関税ポリシー) とは **別概念**. 配送方法のみで判定するため、
    Economy 配送でも DDP 設定なら true を返す. DDU の判別は別関数 `_is_ddu_policy()` を使用.
    出典: `reference_shipping_method_vs_ddu_taxonomy.md`.

    判別: `shippingServiceCode` / `shipping_type` に Economy / SpeedPAK Economy /
    Surface mail / International Economy が含まれるか.
    """
    if not service_code and not service_type:
        return False
    text = ((service_code or "") + " " + (service_type or "")).lower()
    economy_keywords = (
        "speedpak economy",
        "economy",
        "surface mail",
        "international economy",
    )
    return any(kw in text for kw in economy_keywords)


# NOTE: 関税ポリシー (DDU/DDP) 判別ロジックは `tasks.ebay_browse_api.BrowseAPIClient._classify_ddu_from_taxes`
# に集約 (fetch 時に `is_ddu_policy` field として item に付与済).
# UI 側は `c.get("is_ddu_policy")` で True/False/None を直接読み取る.
# 配送方法 (carrier) と関税ポリシーは独立軸. 詳細: `reference_shipping_method_vs_ddu_taxonomy.md`.


def _estimate_profit_usd(p: dict, sale_price_usd: Optional[float] = None) -> Optional[float]:
    """簡易粗利 = sale_price - breakeven. None で current_price を sale 価格として使う.

    W156 fix (2026-05-22 PM): 旧実装は `total = sp + sh; total - be` で送料を
    二重計上していた (be は `compute_breakeven_price_usd` が送料込で binary search
    した item_price = 送料は内部で考慮済み). docstring の「sale_price - breakeven」
    に実装を揃える. _render_hero_metrics の 現在粗利 metric と同根バグ.
    callers: 「赤字のみ」filter (L390) / 商品一覧 (L2045) / 赤字件数 (L3548)
    全てで赤字検知漏れを起こしていた = money-direct.
    """
    be = p.get("lp_breakeven_usd")
    if not be or be <= 0:
        return None
    sp = sale_price_usd if sale_price_usd is not None else (p.get("current_price") or 0)
    return float(sp) - float(be)


# =============================================================================
# Filter / sort
# =============================================================================

def _resolve_pm_search_seed(initial_search: str, current: str) -> str:
    """jump seed の決定ロジック (純関数、Streamlit 非依存)。

    - initial_search が非空 → jump 値を返す (key 既存でも session_state に強制書込させる)
    - initial_search が空  → current (= session_state の既存値) をそのまま返す

    呼び出し側は戻り値を st.session_state["pm_search"] に書いてから
    text_input を key のみで描画する (value= 引数は使わない)。
    """
    return initial_search if initial_search else current


def _apply_filter_and_sort(products: list[dict], initial_search: str = "") -> list[dict]:
    """フィルタ + 並び順 UI を描画し、適用後の products を返す。

    initial_search: W292 jump 時に pm_focus_eid から渡される初期値。
    Streamlit は key の session_state が既存だと value= を無視するため、
    session_state に直書きで seed する (ui_cache.seed_keyed_value_from_db と
    同じ tested パターン)。GC 有無に依存しない確定 seed。
    """
    # jump seed: initial_search が非空のときだけ pm_search を上書き。
    # 非 jump (empty) は既存 user 入力を温存する。
    _seed = _resolve_pm_search_seed(
        initial_search, st.session_state.get("pm_search", "")
    )
    st.session_state["pm_search"] = _seed

    cols = st.columns([3, 2])
    with cols[0]:
        search = st.text_input(
            "🔍 商品名 / SKU / Item ID で検索",
            key="pm_search",
            placeholder="部分一致 (Item ID は 12 桁数字)",
        )
    with cols[1]:
        sort_key = st.selectbox(
            "並び順",
            options=[
                "売れ筋 (sold)",
                "高単価 (price)",
                "watch 多い順",
                "view 多い順",
                "在庫切れ→在庫あり",
                "競合 0 件→多い順",
                "ABC 順",
                "ID 順",
            ],
            key="pm_sort",
        )

    fcols = st.columns(8)
    with fcols[0]:
        only_missing = st.checkbox("⚠️ 未 FIX (仕入¥/重/寸/BE)", key="pm_only_missing")
    with fcols[1]:
        only_no_comp = st.checkbox("⚠️ 競合 0 件のみ", key="pm_only_no_comp")
    with fcols[2]:
        only_oos = st.checkbox("🔴 在庫切れのみ", key="pm_only_oos")
    with fcols[3]:
        only_us = st.checkbox("🇺🇸 US_only のみ", key="pm_only_us")
    with fcols[4]:
        only_negative = st.checkbox("💸 利益マイナスのみ", key="pm_only_neg")
    with fcols[5]:
        # W151 (2026-05-22): 初期登録未完了 listing のみ表示 (user 初期登録作業フォーカス用)
        only_initial_pending = st.checkbox(
            "📝 初期未完了のみ", key="pm_only_initial_pending",
            help="チェック on = 初期登録未完了 listing のみ表示 (商品 hero の "
                 "「📝 初期登録済み」未チェック分)",
        )
    with fcols[6]:
        # W#33: キーワード監視 設定済み listing のみ
        only_kw_set = st.checkbox(
            "🔔 監視 設定済", key="pm_only_kw_set",
            help="キーワード新着監視に 1 件以上紐付いている listing のみ表示",
        )
    with fcols[7]:
        # W#33: キーワード監視 未設定 listing のみ
        only_kw_unset = st.checkbox(
            "🔕 監視 未設定", key="pm_only_kw_unset",
            help="キーワード新着監視が 1 件も紐付いていない listing のみ表示",
        )

    if search:
        s = search.lower()
        products = [
            p for p in products
            if s in (p.get("title") or "").lower()
            or s in (p.get("sku") or "").lower()
            or s in str(p.get("ebay_item_id") or "").lower()
        ]
    if only_missing:
        products = [
            p for p in products
            if not p.get("purchase_yen") or not p.get("weight_g")
            or not (p.get("length_cm") and p.get("width_cm") and p.get("height_cm"))
            or not p.get("lp_breakeven_usd")
        ]
    if only_no_comp:
        products = [p for p in products if not (p.get("competitor_count") or 0)]
    if only_oos:
        products = [p for p in products if p.get("source_status") == "out_of_stock"]
    if only_us:
        products = [p for p in products if p.get("primary_market") == "US_only"]
    if only_negative:
        def _neg(p: dict) -> bool:
            profit = _estimate_profit_usd(p)
            return profit is not None and profit < 0
        products = [p for p in products if _neg(p)]
    if only_initial_pending:
        # W151: 初期登録未完了 (initial_registered=0 or NULL) のみ表示
        products = [p for p in products if not p.get("initial_registered")]
    if only_kw_set:
        # W#33: keyword_watches 紐付き 1 件以上 (is_active=1)
        products = [p for p in products if (p.get("keyword_watch_count") or 0) > 0]
    if only_kw_unset:
        # W#33: keyword_watches 未紐付き (0 件 or NULL)
        products = [p for p in products if not (p.get("keyword_watch_count") or 0)]

    if sort_key == "売れ筋 (sold)":
        products.sort(key=lambda x: -(x.get("total_sold_count") or 0))
    elif sort_key == "高単価 (price)":
        products.sort(key=lambda x: -(x.get("current_price") or 0))
    elif sort_key == "watch 多い順":
        products.sort(key=lambda x: -(x.get("watch_count") or 0))
    elif sort_key == "view 多い順":
        products.sort(key=lambda x: -(x.get("view_count") or 0))
    elif sort_key == "在庫切れ→在庫あり":
        priority = {"out_of_stock": 0, "unknown": 1, "in_stock": 2, "removed": 3}
        products.sort(key=lambda x: priority.get(x.get("source_status") or "", 9))
    elif sort_key == "競合 0 件→多い順":
        products.sort(key=lambda x: (x.get("competitor_count") or 0))
    elif sort_key == "ABC 順":
        products.sort(key=lambda x: (x.get("title") or "").lower())

    return products


# =============================================================================
# Left column sections
# =============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def _cached_shipping_policies():
    """全 active shipping BP (Account API)。UI 層 @st.cache_data ttl=300
    (W134 流儀、client 自体は純関数)。戻りは ShippingPolicyList (frozen)."""
    from monitor.ebay_account_policy import fetch_shipping_policies
    return fetch_shipping_policies({})


def _bp_state_from_db(p: dict) -> dict:
    """W138-A: hero/selectbox 用 BP state を **DB 列から**構築。

    per-render GetItem ゼロ (価格と同じく「最初から自動表示」)。鮮度は
    fetched_at 併記で正直開示 (HIGH-1: GetMyeBaySelling が BP を運ばない
    ため定期同期に相乗り不可、価格より鮮度が劣る)。

    HIGH-2 NULL 多義性解消 = fetched_at で 3 分岐:
      state="unfetched" (a): fetched_at IS NULL → 未取得 (backfill 未 or
            GetItem 失敗)。**Inline と断定しない**。↻ で取得を促す。
      state="inline" (b): fetched_at あり & id 無し → 確定 Inline (非 BP
            管理 listing)。BP 変更不可。
      state="bp" (c): fetched_at あり & id あり → BP あり。selectbox 表示。

    戻り: {state, ok, id, name, fetched_at, error, policies}
    """
    fetched_at = p.get("shipping_profile_fetched_at")
    bp_id = (str(p.get("shipping_profile_id") or "").strip()) or None
    res = {"state": None, "ok": False, "id": bp_id, "name": None,
           "fetched_at": fetched_at, "error": None, "policies": None}
    if not fetched_at:
        res["state"] = "unfetched"
        res["error"] = "BP 未取得 — 「↻ 再取得」で実 eBay から取得"
        return res
    pl = _cached_shipping_policies()
    res["policies"] = pl
    if bp_id is None:
        res["state"] = "inline"
        res["error"] = (
            "この listing は Business Policy 管理ではありません "
            "(Inline shipping)。BP 変更不可"
        )
        return res
    res["state"] = "bp"
    res["ok"] = True
    res["name"] = (pl.name_for(bp_id) if pl.ok else None) or bp_id
    if not pl.ok:
        res["error"] = f"BP 一覧取得失敗 (名前解決不可): {pl.error}"
    return res


def _refresh_bp_from_ebay(eid: str, config: dict) -> None:
    """W138-A 「↻ 再取得」: 単一 listing を 1 回 GetItem し DB の
    shipping_profile_id + shipping_profile_fetched_at を更新 (opt-in、
    毎 render でない)。eBay.com 直接 BP 変更で DB が stale になった時の
    能動更新手段 (HIGH-1 緩和)。

    Q0 (silent skip 防止 / Codex#3 整合): GetItem 失敗時は fetched_at を
    **据置** (成功時刻で上書きしない = 未取得状態(a) 維持、Inline 誤断定
    回避) + session_state に失敗痕跡を残し UI に表示する。成功時は id
    (None=確定 Inline 含む) と fetched_at を同一 UPDATE で原子的に書く。
    """
    errk = f"pm_bprefresh_err_{eid}"
    st.session_state.pop(errk, None)
    try:
        creds = get_ebay_credentials(config or {})
        app_id = creds.get("app_id", "")
        dev_id = creds.get("dev_id", "")
        cert_id = creds.get("cert_id", "")
        token = creds.get("user_token", "")
        if not (app_id and dev_id and cert_id and token):
            st.session_state[errk] = "eBay credentials 不在で再取得失敗"
            return
    except (KeyError, ValueError, OSError) as e:
        st.session_state[errk] = f"credentials 取得エラー: {e}"
        return
    from monitor.ebay_listing_snapshot import fetch_listing_snapshot
    snap = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, token)
    if not snap.ok:
        # fetched_at 据置 (状態(a) 維持) + 痕跡 (Q0)
        st.session_state[errk] = f"再取得失敗 (GetItem): {snap.error}"
        return
    # BP は W138-A 通り常に書く (None=確定 Inline の明示 NULL も含む)。
    # W142: +each も同一 ↻ で取得済 (snap.ship_additional_usd)。None-skip
    # 慣習 (_sync_db_to_actual と同型、database.py v43 設計コメント
    # 「更新元は ... 単発 ↻ 再取得のみ」と整合): snap に出た時のみ
    # shipping_additional_cost/fetched_at を書き、None は据置 (既知 DB 値を
    # NULL 上書きして未取得に劣化させない = R4)。
    _sets = ["shipping_profile_id=?",
             "shipping_profile_fetched_at=datetime('now')"]
    _params: list = [str(snap.shipping_profile_id)
                     if snap.shipping_profile_id else None]
    if getattr(snap, "ship_additional_usd", None) is not None:
        _sets.append("shipping_additional_cost=?")
        _params.append(float(snap.ship_additional_usd))
        _sets.append("shipping_additional_fetched_at=datetime('now')")
    _params.append(eid)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE ebay_listings SET {', '.join(_sets)} "
            "WHERE ebay_item_id=?",
            tuple(_params),
        )
    bump_db_version()  # read-cache 無効化 → 次 render で新 BP 反映


def _hero_effective(p: dict) -> dict:
    """hero metrics 用の実効値を返す (2026-05-17 W137後 fix).

    編集フォームの入力 (st.session_state、st.form は submit 時に確定) があれば
    DB 値より優先する = user の「赤枠を最新化」要求。商品価格/送料の変更、
    仕入価格/重量/寸法の変更を breakeven にライブ反映する。

    **eBay も DB も一切書かない純粋な試算プレビュー** (current_price は実 eBay
    価格を映す列なので calc では触らない = W137 で苦労した DB↔eBay 乖離の
    再生産防止。価格の実反映は 📤eBay反映 ボタンの責務)。
    """
    eid = p["ebay_item_id"]

    def _ss_num(suffix: str) -> Optional[float]:
        v = st.session_state.get(f"pm_{suffix}_{eid}")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    f_price = _ss_num("ebay_price")
    f_ship = _ss_num("ebay_ship")
    f_pyen = _ss_num("pyen")
    f_w = _ss_num("weight")
    f_l = _ss_num("length")
    f_wd = _ss_num("width")
    f_h = _ss_num("height")

    db_price = float(p.get("current_price") or 0)
    db_ship = float(p.get("shipping_cost") or 0)
    price = f_price if (f_price is not None and f_price > 0) else db_price
    ship = f_ship if f_ship is not None else db_ship

    be = p.get("lp_breakeven_usd")
    be_preview = False
    pyen = (f_pyen if (f_pyen is not None and f_pyen > 0)
            else p.get("purchase_yen"))
    wt = f_w if (f_w is not None and f_w > 0) else p.get("weight_g")
    cost_edited = any(
        v is not None for v in (f_pyen, f_w, f_l, f_wd, f_h)
    )
    if pyen and wt and cost_edited:
        try:
            _be = compute_breakeven_price_usd(
                purchase_yen=float(pyen),
                weight_g=float(wt),
                length_cm=float(
                    f_l if f_l is not None else (p.get("length_cm") or 0)),
                width_cm=float(
                    f_wd if f_wd is not None else (p.get("width_cm") or 0)),
                height_cm=float(
                    f_h if f_h is not None else (p.get("height_cm") or 0)),
                settings=_calc_settings(),
            )
            if _be and _be > 0:
                be = _be
                be_preview = True
        except (KeyError, TypeError, ValueError, RuntimeError) as e:
            logger.debug(f"[pm hero] live breakeven 試算 skip ({eid}): {e}")

    # price は f_price>0 の時のみ override する (上記 price= の guard と一致)。
    # f_price=0/負 は DB 値表示なので preview 扱いしない (caption 不整合防止)。
    # ship は free(0) が正当な override なので >0 guard を付けない。
    preview = (
        (f_price is not None and f_price > 0
         and round(f_price, 2) != round(db_price, 2))
        or (f_ship is not None and round(f_ship, 2) != round(db_ship, 2))
        or be_preview
    )
    return {"price": price, "ship": ship, "be": be, "preview": preview}


def _fetched_jst_label(ts) -> str:
    """W138-A: SQLite UTC timestamp 文字列を JST 表示ラベルへ変換.

    `shipping_profile_fetched_at` は datetime('now') = UTC 保存
    (sqlite-timezone.md)。UI は +9h して "M/D HH:MM JST" で表示し、
    stale 時刻の user 誤認を防ぐ。parse 不能/None は "不明"。
    """
    if not ts:
        return "不明"
    s = str(ts).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s[:19] if len(s) >= 19 else s, fmt)
        except ValueError:
            continue
        j = dt + timedelta(hours=9)
        # Windows strftime は %-m/%-d 非対応 → 手組みで前ゼロ無し表記
        return f"{j.month}/{j.day} {j.hour:02d}:{j.minute:02d} JST"
    return "不明"


def _render_profit_value(label: str, yen: float, dim: bool) -> None:
    """W147: 利益 1 値を表示。dim=True (= この listing の primary_market では
    非該当 = 参考にしかならない区分) は淡色 + 「参考値」、それ以外は
    st.metric (黒字 normal / 赤字 inverse) で強調表示する。"""
    if dim:
        st.markdown(
            f'<div style="opacity:0.42;padding:2px 0;">'
            f'<div style="font-size:0.8rem;">{label}</div>'
            f'<div style="font-size:1.5rem;font-weight:600;">'
            f'¥{yen:+,.0f}</div>'
            f'<div style="font-size:0.72rem;">参考値</div></div>',
            unsafe_allow_html=True,
        )
    else:
        st.metric(
            label, f"¥{yen:+,.0f}",
            delta=("黒字" if yen > 0 else "赤字" if yen < 0 else "ゼロ"),
            delta_color=("normal" if yen > 0 else "inverse"),
        )


@st.cache_data(show_spinner=False)  # W221 T1-3: ttl=3 撤廃。key (入力+settings_mtime
# +db_version+actual_duty+point_yen) が完備の純関数 = ttl 不要、3秒毎の無駄 miss を解消。
def _cd_profit_breakdown(
    price: float, pyen: float, weight_g: float,
    length_cm: float, width_cm: float, height_cm: float,
    category_id: int, settings_mtime: float, db_version: int,
    actual_duty_rate: Optional[float] = None,
    point_yen: Optional[float] = None,
) -> Optional[dict]:
    """W147: 現在価格での「還付あり/なし × USA向け(DDP)/US以外(DDU)」利益
    (円) を返す表示専用 純関数。

    calculator.calculate を is_ddu=False (DDP=米国向け、関税 売主負担) と
    is_ddu=True (DDU=米国以外、関税なし) の 2 回呼び、最良送料サービスの
    profit / profit_with_refund を取る。**計算式は一切変えない (W147 は
    表示のみ)。eBay も DB も書かない**。

    country_code は両呼出とも "US" 固定 (意図的)。calculator の is_ddu は
    関税 add-on のみを増減し、配送ゾーン/手数料は country_code 依存。本
    システムの送料モデルは「US 軸差分式」(reference_shipping_tariff_logic
    .md) で "US以外" という単一国は存在しないため、2 値は US 送料基準を
    共通の物差しとし、その差 = 米国輸入関税(Section 232)分 を可視化する
    設計 (既存 compute_breakeven_price_usd も country_code="US" 固定で
    整合)。UI 側で「差 = 関税分・送料 US 基準」を caption 明示する。

    商品管理タブは Streamlit が collapsed expander の body も毎 rerun 実行
    する = 「開いた時だけ」にならないため、W134 流儀で
    @st.cache_data(ttl=3) + db_version key 化し再 rerun コストを抑える
    (既存 _cd_fetch_all_products と同じ idiom)。settings.json 変更時の
    breakeven との一時的非対称を消すため settings_mtime も cache key に
    含める (mtime 変化 = 為替/手数料改定 → 即 cache miss)。
    """
    try:
        from calculator import CalcInput, calculate
        settings = _calc_settings()

        def _calc(is_ddu: bool):
            res = calculate(CalcInput(
                purchase_yen=int(pyen), item_price_usd=float(price),
                weight_g=int(weight_g), length_cm=float(length_cm or 0),
                width_cm=float(width_cm or 0), height_cm=float(height_cm or 0),
                category_id=int(category_id), is_ddu=is_ddu,
                country_code="US",
                # W212: USA向け(DDP)は per-listing 実関税(Section232)を反映。
                # US以外(DDU)は calculator 側で関税ゼロ化されるため actual は無視される。
                actual_duty_rate=actual_duty_rate,
                # W220: per-listing ポイント実額(¥)。指定時 point_return=point_yen。
                point_yen=point_yen,
            ), settings)
            if not res.service_results:
                return None
            return (
                max(s.profit for s in res.service_results),
                max(s.profit_with_refund for s in res.service_results),
                max(s.tax_refund for s in res.service_results),
                res.shipping_usd * settings["exchange_rate"],
                res.point_return,  # ポイント還元 (購入時付与、DDP/DDU 共通)
            )

        us = _calc(False)    # USA向け = DDP (関税 売主負担)
        nonus = _calc(True)  # US以外  = DDU (関税なし)
        if us is None or nonus is None:
            return None
    except (KeyError, TypeError, ValueError, ZeroDivisionError,
            RuntimeError, AttributeError) as e:
        # AttributeError: calculate() が万一 None を返した時の res.* 防御
        # (現契約は CalcResult 常時返却だが hero 全体クラッシュ回避)。
        logger.debug(f"[pm hero] W147 利益試算 skip: {e}")
        return None
    return {
        "refund_us": us[1], "refund_nonus": nonus[1],
        "noref_us": us[0], "noref_nonus": nonus[0],
        "tax_refund": us[2], "ddp_cost_jpy": round(us[3]),
        "point_return": round(us[4]),  # 手取りに含まれるポイント分 (判断材料)
    }


def _profit_breakdown(p: dict) -> Optional[dict]:
    """W147: hero 用に有効入力 (編集フォーム入力を優先、_hero_effective と
    同方針) を解決し _cd_profit_breakdown に渡す。purchase_yen / weight が
    欠ければ None (hero は「未入力」表示を維持)。primary_market を付与
    (区分連動の参考値表示用)。"""
    eid = p["ebay_item_id"]

    def _ss_num(suffix: str) -> Optional[float]:
        v = st.session_state.get(f"pm_{suffix}_{eid}")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    f_price = _ss_num("ebay_price")
    f_pyen = _ss_num("pyen")
    f_w = _ss_num("weight")
    f_l = _ss_num("length")
    f_wd = _ss_num("width")
    f_h = _ss_num("height")

    price = (f_price if (f_price is not None and f_price > 0)
             else float(p.get("current_price") or 0))
    pyen = (f_pyen if (f_pyen is not None and f_pyen > 0)
            else p.get("purchase_yen"))
    wt = f_w if (f_w is not None and f_w > 0) else p.get("weight_g")
    if not price or price <= 0 or not pyen or not wt:
        return None

    try:
        _smt = _SETTINGS_FILE.stat().st_mtime
    except OSError:
        _smt = 0.0  # stat 不能でも算出は継続 (cache 無効化のみ機能低下)
    # W215 (2026-06-03): Section 232 該当品の per-listing 実関税率 (duty_rate_pct
    # 25-55%) は **表示利益に適用しない** (= global duty_rate 11% で試算)。CPaSS 実請求
    # 全数調査で OC SpeedPAK DDP は原産国別 flat (日本発10%) を課金し Section 232 該当品も
    # 例外なく flat だったため (lowest_price.update_listing_breakeven と同方針)。
    # section232_class は警告バッジで別途掲出 (true-up リスク注意喚起)。
    _adr = None
    # W220: per-listing ポイント実額。編集中の入力値 (session) を優先、無ければ DB 値。
    _f_point = _ss_num("pointyen")
    # W220 MEDIUM-1: DB の 0 (ポイント無し明示) を None に潰さない (is not None)。
    _point_yen = (_f_point if _f_point is not None
                  else (float(p["point_yen"]) if p.get("point_yen") is not None else None))
    # W222 (2026-06-05): 利益計算の FVF カテゴリは floor (lowest_price) と同じ
    # `use_category_fvf_floor` flag に連動 (ON=実カテゴリ / OFF=58248 固定)。flag を
    # 揃えることで「hero 表示利益」「一覧粗利 (lp_breakeven 由来)」「自動値下げ下限
    # (floor)」が同一 regime になり、表示と実下限の根拠が乖離しない (code-reviewer
    # MEDIUM: flag OFF 復帰時の hero↔floor 乖離予防)。
    _use_cat_fvf = bool(_calc_settings().get("use_category_fvf_floor", False))
    _cat_for_calc = int(p.get("category_id") or 58248) if _use_cat_fvf else 58248
    bd = _cd_profit_breakdown(
        float(price), float(pyen), float(wt),
        float(f_l if f_l is not None else (p.get("length_cm") or 0)),
        float(f_wd if f_wd is not None else (p.get("width_cm") or 0)),
        float(f_h if f_h is not None else (p.get("height_cm") or 0)),
        _cat_for_calc,
        _smt,
        get_db_version(),
        _adr,
        _point_yen,
    )
    if bd is None:
        return None
    return {**bd,
            "pyen": float(pyen),  # 仕入れ金額 (実効値、安い仕入先探しの判断材料)
            "primary_market": (p.get("primary_market") or "").strip().lower()}


def _render_hero_metrics(p: dict, bp_state: Optional[dict] = None) -> None:
    """商品 expander の最上部に表示する 4 つの主要指標.

    [現在総額] [損益分岐] [現在粗利] [競合最安]

    視覚的に最も重要な情報を一目で把握できるよう、大きな metric card で表示.
    編集フォーム入力があれば実効値でライブ試算 (_hero_effective)。
    bp_state (W138, 開いた expander のみ非 None): 現 Shipping BP を pill 表示。
    """
    _eff = _hero_effective(p)
    cp, sh = _eff["price"], _eff["ship"]
    total = cp + sh
    be = _eff["be"]
    competitor_min = p.get("competitor_min_price")
    market = p.get("primary_market") or "-"

    # SKU + ID + 区分 + Rank を 1 行で簡潔表示
    sku = p.get("sku") or "-"
    # W227: hero は eBay 実 Condition (商品状態) を表示 (人気度 rank 列は混ぜない)。
    _hero_cond_rank = _condition_widget_initial(p)
    _hero_cid = str(p.get("ebay_condition_id") or "").strip()
    rank = _hero_cond_rank or _CONDITION_ID_LABEL.get(_hero_cid, "未取得")
    sold = p.get("total_sold_count") or 0
    watch = p.get("watch_count") or 0
    view = p.get("view_count") or 0
    src_status = p.get("source_status") or "unknown"
    src_emoji = _status_emoji(src_status)

    # W138-A: Shipping BP pill (DB 列駆動で常時表示)。fetched_at は UTC
    # 保存だが UI は JST 変換併記 (sqlite-timezone.md 準拠、stale 時刻の
    # user 誤認防止)。HIGH-2 3 状態を pill 文言で区別。
    bp_state = bp_state or {}
    _bp_st = bp_state.get("state")
    _fa = _fetched_jst_label(bp_state.get("fetched_at"))
    if _bp_st == "bp":
        bp_pill = (
            f'<span class="pm-pill pm-pill-info">🚚 Ship BP: '
            f'{bp_state.get("name")} (取得 {_fa})</span>'
        )
    elif _bp_st == "inline":
        bp_pill = (
            f'<span class="pm-pill pm-pill-warn">🚚 Ship BP: '
            f'Inline (BP なし・取得 {_fa})</span>'
        )
    elif _bp_st == "unfetched":
        bp_pill = (
            '<span class="pm-pill pm-pill-warn">🚚 Ship BP: '
            '未取得 — ↻ で取得</span>'
        )
    else:
        bp_pill = ""

    # W222 (2026-06-05): 実カテゴリ + FVF 実効率 pill。
    # ⚠️ 利益計算 (_profit_breakdown) は floor (lowest_price) と同じ
    # `use_category_fvf_floor` flag に連動する: ON=実カテゴリ FVF / OFF=58248 固定。
    # pill は **実際に計算で使う率** を表示する (flag OFF や CSV 未収録は "既定" 明示で
    # 誤認防止)。flag を hero/floor/一覧粗利で揃え、表示と自動値下げ下限の根拠を一致させる。
    _cs = _calc_settings()
    _use_cat_fvf = bool(_cs.get("use_category_fvf_floor", False))
    _cat = p.get("category_id")
    cat_pill = ""
    if _cat:
        _eff_cat = int(_cat) if _use_cat_fvf else 58248
        try:
            _crate = _get_ebay_fvf_rate(
                _eff_cat, float(total), _cs.get("store_plan", "Premium"))
            _in_tbl = _category_in_fee_table(_eff_cat)
            if not _use_cat_fvf:
                _note, _cls = " 既定(floor未連動)", "pm-pill-warn"
            elif not _in_tbl:
                _note, _cls = " 既定(CSV未収録)", "pm-pill-warn"
            else:
                _note, _cls = "", "pm-pill-info"
            cat_pill = (
                f'<span class="pm-pill {_cls}">'
                f'カテゴリ: {_cat} (FVF {_crate * 100:.1f}%{_note})</span>'
            )
        except (ValueError, TypeError, KeyError):
            cat_pill = f'<span class="pm-pill pm-pill-info">カテゴリ: {_cat}</span>'

    st.markdown(
        f'<div style="margin: 4px 0 12px 0;">'
        f'<span class="pm-pill pm-pill-info">ID: {p["ebay_item_id"]}</span>'
        f'<span class="pm-pill pm-pill-info">SKU: {sku}</span>'
        f'<span class="pm-pill pm-pill-info">区分: {market}</span>'
        f'{cat_pill}'
        f'<span class="pm-pill pm-pill-info">状態: {rank}</span>'
        f'<span class="pm-pill {"pm-pill-bad" if src_status == "out_of_stock" else "pm-pill-ok" if src_status == "in_stock" else "pm-pill-warn"}">仕入先: {src_emoji} {src_status}</span>'
        f'{bp_pill}'
        f'<span class="pm-pill pm-pill-info">📊 sold {sold} / watch {watch} / view {view}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── 採算パネル (v5 redesign 2026-06-03): 買い手視点 / あなたの取り分 の2列 ──
    # 金額軸の散らばり (商品のみ / 送料込み / 還付有無) を「2視点」に正規化。
    # 損益分岐は商品軸 (自動値下げ floor) + 総額換算 (競合と同じ物差し) を併記。
    bd = _profit_breakdown(p)
    mk = (market or "").strip().lower()
    be_total = (be + sh) if (be and be > 0) else None  # 総額軸の損益分岐
    _pcols = st.columns(2)

    with _pcols[0]:  # 左: 買い手視点
        _ph = (f'<span style="font-size:20px;font-weight:700;color:#0f2747">${cp:,.2f}</span>'
               f'<span style="color:#8a93a0;font-size:13px"> + ${sh:,.2f}</span>')
        _html = (
            '<div style="font-size:11px;color:#888;font-weight:600;margin-bottom:6px">◆ 買い手視点（eBayでの見え方）</div>'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;padding:3px 0">'
            f'<span style="color:#888">現在価格</span><span>{_ph}</span></div>'
            f'<div style="text-align:right;font-size:11px;color:#555;margin-bottom:4px">買い手総額 <b>${total:,.2f}</b></div>'
        )
        if competitor_min:
            cmin = float(competitor_min); diff = total - cmin
            _dc = '#147a40' if diff < 0 else '#a52a2a' if diff > 0 else '#888'
            _dl = '有利' if diff < 0 else '高い' if diff > 0 else '同'
            _html += (
                f'<div style="display:flex;justify-content:space-between;padding:3px 0">'
                f'<span style="color:#888">競合最安(総額)</span><span><b>${cmin:,.2f}</b></span></div>'
                f'<div style="display:flex;justify-content:space-between;padding:3px 0">'
                f'<span style="color:#888">競合差</span>'
                f'<span style="color:{_dc};font-weight:700">${diff:+,.2f} {_dl}</span></div>'
            )
        else:
            _html += '<div style="color:#888;padding:3px 0">競合最安: 未登録</div>'
        if be_total:
            _html += (
                '<hr style="border:0;border-top:1px solid #e4e8ee;margin:6px 0">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;padding:3px 0">'
                f'<span style="color:#888">損益分岐</span>'
                f'<span><span style="font-size:18px;font-weight:700;color:#c9821a">${be:,.2f}</span>'
                f'<span style="color:#8a93a0;font-size:12px"> + ${sh:,.2f}</span></span></div>'
                f'<div style="text-align:right;font-size:11px;color:#888">買い手総額 ${be_total:,.2f}'
                f'（競合と同じ物差し）／ 自動値下げ下限=商品 ${be:,.2f}</div>'
            )
        else:
            _html += '<div style="color:#888;padding:3px 0">損益分岐: 未入力（仕入価格+重量が必要）</div>'
        st.markdown(_html, unsafe_allow_html=True)

    with _pcols[1]:  # 右: あなたの取り分
        if bd is None:
            st.markdown(
                '<div style="font-size:11px;color:#888;font-weight:600;margin-bottom:6px">◆ あなたの取り分</div>'
                '<div style="color:#888;padding:8px 0">利益: 未入力（仕入価格+重量を入力 → 利益計算）</div>',
                unsafe_allow_html=True)
        else:
            if mk == "global_only":
                _ml, _mn = "US以外向け（関税なし）", bd["noref_nonus"]
                _rl, _rn = "USA向け（関税自社負担）", bd["noref_us"]
            else:
                _ml, _mn = "USA向け（関税自社負担）", bd["noref_us"]
                _rl, _rn = "US以外向け（関税なし）", bd["noref_nonus"]
            _c = lambda v: '#147a40' if v >= 0 else '#a52a2a'
            _pyen = bd.get("pyen") or 0          # 仕入れ金額(原価) = 安い仕入先探しの判断材料
            _refund = bd.get("tax_refund") or 0  # 消費税還付
            _pt = bd.get("point_return") or 0    # ポイント還元 = 赤字許容の判断材料 (合算しない)
            _m_arefund = _mn + _refund           # 実利益(還付あり) = 還付なし + 消費税還付
            _r_arefund = _rn + _refund           # 参考仕向地の 実利益(還付あり)
            _html = (
                '<div style="font-size:11px;color:#888;font-weight:600;margin-bottom:6px">◆ あなたの取り分（手元にいくら残るか）</div>'
                # 仕入れ金額: 手取りがマイナスでも「いくら安い仕入先を探せば黒字か」を判断
                f'<div style="display:flex;justify-content:space-between;padding:2px 0">'
                f'<span style="color:#888">仕入れ金額（原価）</span>'
                f'<span style="color:#b04a4a;font-weight:600">¥{_pyen:,.0f}</span></div>'
                '<hr style="border:0;border-top:1px solid #e4e8ee;margin:5px 0">'
                f'<div style="font-size:11px;color:#888;margin-bottom:4px">{_ml}</div>'
                f'<div style="display:flex;justify-content:space-between;padding:2px 0">'
                f'<span style="color:#888">実利益（還付なし）</span>'
                f'<span style="color:{_c(_mn)};font-weight:700">¥{_mn:+,.0f}</span></div>'
                f'<div style="display:flex;justify-content:space-between;padding:1px 0;font-size:12px">'
                f'<span style="color:#888">＋ 消費税還付</span>'
                f'<span style="color:#6b46c1">¥{_refund:+,.0f}</span></div>'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;padding:2px 0">'
                f'<span style="font-weight:700">実利益（還付あり）</span>'
                f'<span style="font-size:18px;font-weight:800;color:{_c(_m_arefund)}">¥{_m_arefund:+,.0f}</span></div>'
                # ポイント: 合算せず別表示。「ポイントが XX 円もらえるから赤字も許容」の判断材料
                f'<div style="display:flex;justify-content:space-between;padding:2px 0">'
                f'<span style="color:#888">ポイント還元</span>'
                f'<span style="color:#6b46c1;font-weight:600">¥{_pt:+,.0f}</span></div>'
                f'<div style="font-size:11px;color:#888;margin-top:6px;padding-top:5px;border-top:1px dashed #e4e8ee">'
                f'参考）{_rl}: 実利益（還付あり）¥{_r_arefund:+,.0f}</div>'
            )
            _s232 = p.get("section232_class")
            if _s232:
                # W215: 利益は flat 11% で試算 (OC 実請求準拠)。ただし本品は Section 232
                # 該当 = CBP 実額が OC 推定を超えた場合に true-up (追加請求) されうるため
                # 警告のみ掲出 (率には畳み込まない)。法定分類は記録 (duty_rate_pct) 保持。
                _stat = int(p.get("duty_rate_pct") or 0)
                _statx = f"法定~{_stat}%" if _stat else "法定高率"
                _html += (
                    f'<div style="background:#fdf0d5;color:#9a6a00;font-size:11px;padding:5px 8px;border-radius:4px;margin-top:6px">'
                    f'⚠️ Section232 {_s232}（{_statx}）= 利益はOC実績flat11%で試算。'
                    f'CBP実額超過時は追加請求(true-up)の可能性 → 高額・低粗利は要注意</div>'
                )
            st.markdown(_html, unsafe_allow_html=True)

    st.caption(
        "買い手視点と取り分の2軸に整理。損益分岐は総額換算で競合と直接比較可。"
        "USA向け=DDP(関税自社負担)/US以外=DDU(関税なし)。"
    )

    # 利益内訳の詳細 (折りたたみ)。bd は上の採算パネルで算出済 (重複計算しない)。
    # 区分定義の出典: reference_shipping_tariff_logic.md。
    if bd is not None:
        with st.expander("利益内訳の詳細（還付なし / 税還付 / 関税・両仕向地）",
                         expanded=False):
            st.markdown(
                f"- 還付なし × USA向け (DDP): **¥{bd['noref_us']:+,.0f}** "
                f"／ US以外 (DDU): **¥{bd['noref_nonus']:+,.0f}**\n"
                f"- 手取り(還付込) × USA向け: ¥{bd['refund_us']:+,.0f} "
                f"／ US以外: ¥{bd['refund_nonus']:+,.0f}\n"
                f"- 消費税還付額（目安）: ¥{bd['tax_refund']:,.0f}\n"
                f"- 米国向け関税コスト（DDP・売主負担）: ¥{bd['ddp_cost_jpy']:,.0f}"
            )
            st.caption(
                "calculator と同じ計算式（表示のみ・eBay/DB 未書込）。"
                "Section 232 該当品は per-listing 実関税を反映。送料は US 軸差分式基準。"
            )

    if _eff["preview"]:
        st.caption(
            "↑ 編集フォームの**入力値で試算プレビュー中**（未保存・eBay 未反映）。"
            "実反映は 💾DB保存 / 📤eBay反映 ボタンで。"
        )

    # 値下げ追従シミュレーション (1 行で目立たない位置に表示)
    if be and be > 0 and competitor_min:
        cmin = float(competitor_min)
        sim_target = max(cmin - 0.01, be)
        sim_margin = sim_target - be
        is_safe = sim_margin > 0
        st.caption(
            f"**値下げシミュレーション**: 競合より $0.01 安く (${sim_target:.2f}) → "
            f"粗利 ${sim_margin:+.2f} ({'黒字維持' if is_safe else '赤字'})"
        )


def _render_left_basic_and_physical(
    p: dict, config: dict, bp_state: Optional[dict] = None,
) -> dict:
    """左列: SKU / 在庫数 / 物理属性 / eBay 出品 / 仕入価格 編集 form (form 内呼出前提).

    W217 (2026-06-03): 採算パネルと同じ思想で「💰 money-direct → 📦 属性 →
    📎 メモ (折りたたみ)」の3段配置に再整理。widget の key/value/help/dirty
    変数 (primary_market_render_initial / add_render_initial /
    bp_render_initial_id) は1文字も改変せず、配置順序のみ変更 (K2 surgical)。
    Streamlit は st.markdown で <div> を開けて閉じる pattern が機能しない
    (他 widget が <div> の外に出てしまう) ため、money 枠の border-left+bg
    視覚は完全には再現せず、見出し pm-money-head + 既存 section-label 構造
    で「金額系」と「属性系」の段差を表現する。
    """
    eid = p["ebay_item_id"]
    editing: dict = {}
    current_sku = p.get("sku") or ""

    # ──────────────────────────────────────────────────────────────────────
    # 💰 価格・採算 (money-direct・誤操作注意)
    # ──────────────────────────────────────────────────────────────────────
    # W217-A (2026-06-03): モックアップの「💰金額枠 (amber 左ライン + 背景 +
    # 角丸)」を st.container(border=True, key=...) で実装。CSS hook は
    # div[class*="st-key-pm_money_box_"] で amber border-left を上書き。
    # 旧 issue (st.markdown <div> が後続 widget を囲めない) を、新規 widget
    # 配置を変えずに container で囲むだけで解決する。
    # ⚠️ st.container の key= パラメータは Streamlit 1.36+ で導入された機能
    # (border= は 1.29+ から、key= は 1.36+ から)。本コードは key= を使うため
    # requirements.txt の pin を streamlit>=1.56.0 (現に動作確認済の installed
    # 版) へ引き上げ済。古い streamlit (例 1.32.0) では TypeError で本タブが
    # 即クラッシュするため、requirements.txt の pin を下げてはいけない。
    # widget の key / value / help / dirty-flag 変数
    # (primary_market_render_initial / add_render_initial /
    # bp_render_initial_id) は 1 文字も改変せず、container でラップするのみ。
    with st.container(border=True, key=f"pm_money_box_{eid}"):
        st.markdown(
            '<div class="pm-section-label" '
            'style="color: var(--pm-warning) !important; '
            'border-bottom-color: var(--pm-warning) !important; '
            'margin-top: 0 !important;">'
            '💰 価格・採算 (money-direct・誤操作注意)</div>',
            unsafe_allow_html=True,
        )

        # 💵 eBay 出品価格 + 送料 (商品価格 / Buyer pays / +each)
        eb1, eb2, eb3 = st.columns(3)
        with eb1:
            editing["new_ebay_price"] = st.number_input(
                "商品価格 (USD)",
                min_value=0.0,
                value=float(p.get("current_price") or 0.0) if p.get("current_price") else None,
                step=1.0, format="%.2f",
                key=f"pm_ebay_price_{eid}",
            )
        with eb2:
            editing["new_ship_cost"] = st.number_input(
                "送料 Buyer pays (USD)",
                min_value=0.0,
                value=float(p.get("shipping_cost") or 0.0) if p.get("shipping_cost") is not None else None,
                step=0.5, format="%.2f",
                key=f"pm_ebay_ship_{eid}",
                help="1 個目の送料 (ShippingServiceCost)",
            )
        with eb3:
            # W142 根本原因#5(b): 旧実装は value=None ハードコードで +each が
            # 常時空欄 (ebay_listings に保存列が無かった)。migration v43 の
            # shipping_additional_cost を表示 source に (Buyer pays L771 と対称)。
            # W142 Codex-R3 HIGH-2: +each dirty-flag 用 render 初期値 (= DB 列値)。
            # _apply_to_ebay は「submit 値 != この初期値」の時のみ user が
            # +each を実操作したとみなす。BP の Codex#1 bp_render_initial_id と
            # 同型 = 表示中 DB 値を「変更」と誤認し stale を実 eBay に上書き
            # する経路を遮断 (金銭直結、Phase2 実 eBay 真実原則を守る)。
            _add_init = (float(p.get("shipping_additional_cost"))
                         if p.get("shipping_additional_cost") is not None
                         else None)
            editing["add_render_initial"] = _add_init
            editing["new_ship_additional"] = st.number_input(
                "送料 +each (USD)",
                min_value=0.0,
                value=_add_init,
                step=0.5, format="%.2f",
                key=f"pm_ebay_ship_add_{eid}",
                help="2 個目以降の追加送料 (ShippingServiceAdditionalCost)。"
                     "DB保存値表示。実 eBay との一致は 📤eBay反映 で verify。",
            )
        # W142 (B): 送料は DB保存値表示 + 乖離マーカー (expander 展開で
        # GetItem を呼ばない = 速度/quota 優先、真値は 📤eBay反映 で verify /
        # BP ↻ で取得)。鮮度を fetched_at で正直開示 (R-11/HIGH-1 の精神)。
        _ship_fa = _fetched_jst_label(
            p.get("shipping_additional_fetched_at") or p.get("last_synced_at")
        )
        st.caption(
            f"💡 送料 (Buyer pays / +each) は **DB保存値**表示 "
            f"(実 eBay 最終同期: {_ship_fa})。eBay.com で直接変更していると"
            f"乖離し得ます。真値は 📤eBay反映 時に verify されます。"
        )

        # 🌐 区分 + 💰 仕入価格 + 下限価格 (採算判断に直結する 3 軸を money 枠内に集約)
        from monitor.database import VALID_PRIMARY_MARKETS
        _mkt_labels = {
            "US_only": "US_only — 米国のみ販売 (商品価格に関税包含)",
            "mixed_global": "mixed_global — 米国+他国 混在",
            "global_only": "global_only — 米国以外",
            "unknown": "unknown — 未判定",
        }
        _mkt_opts = list(VALID_PRIMARY_MARKETS)
        # DB が None の listing は表示上 unknown を default に (書込は dirty 時のみ)。
        _cur_mkt = (p.get("primary_market") or "unknown")
        if _cur_mkt not in _mkt_opts:
            _cur_mkt = "unknown"
        # dirty-flag (BP / 送料 と同型): submit 値 != 表示初期値 の時のみ DB 保存。
        # None の listing を無操作で unknown に上書きする stale write を遮断。
        editing["primary_market_render_initial"] = _cur_mkt
        mk1, mk2, mk3 = st.columns(3)
        with mk1:
            editing["primary_market"] = st.selectbox(
                "区分 (送料・関税前提)",
                options=_mkt_opts,
                index=_mkt_opts.index(_cur_mkt),
                format_func=lambda m: _mkt_labels.get(m, m),
                key=f"pm_primary_market_{eid}",
                help="Terapeak 365 日 sold 判定 (W110(2))。送料差分式 + DDP 関税の前提区分。"
                     "保存で DB 更新 + 利益再計算 (eBay 送料反映は別途 📤eBay反映)。",
            )
        with mk2:
            editing["purchase_yen"] = st.number_input(
                "仕入価格 (JPY)",
                min_value=0,
                value=int(p["purchase_yen"]) if p.get("purchase_yen") else None,
                step=100, key=f"pm_pyen_{eid}",
            )
        with mk3:
            editing["lp_min_price"] = st.number_input(
                "下限価格 商品のみ (USD)",
                min_value=0.0,
                value=float(p["lp_min_price"]) if p.get("lp_min_price") else None,
                step=1.0, format="%.2f",
                key=f"pm_minp_{eid}",
                help="W183 自動値下げの絶対下限。**商品価格のみ・送料は含みません**。"
                     "自動値下げはこの商品価格を下回りません。買い手の総額下限 = "
                     "下限価格 + 送料。未入力なら breakeven (損益分岐の商品価格) が下限。",
            )
        # 補足: 下限価格 / breakeven は「商品価格(送料別)」軸 (eBay 出品価格=StartPrice)。
        # 自動値下げ(W183)は competitor 総額 -$0.01 から自社送料を引いた商品価格を狙い、
        # この下限でクランプする (task_rival_pricing._compute_target_price / floor 比較)。
        st.caption(
            "💡 **下限価格・損益分岐は「商品価格」のみ**（送料は含みません）。"
            "買い手が払う**総額の下限 = 下限価格 + 送料**。"
            "自動値下げは商品価格がこの下限を下回らない範囲で実行されます。"
        )

        # W220 (2026-06-04): per-listing ポイント実額(¥)。仕入先/カードで還元率が
        # 違うため実額を入力。採算パネルの「ポイント還元」に反映 (手取り判断材料、
        # 利益には合算しない)。settings.point_reward_rate (global) は実質 0 で機能せず。
        editing["point_yen"] = st.number_input(
            "ポイント還元 (¥)",
            min_value=0,
            # W220 MEDIUM-1: 0 (ポイント無し明示) を None に潰さない (is not None)。
            value=int(p["point_yen"]) if p.get("point_yen") is not None else None,
            step=100, key=f"pm_pointyen_{eid}",
            help="この仕入れで得たポイント実額(¥)。採算の「ポイント還元」に反映 "
                 "(赤字許容の判断材料。利益額には合算しない)。空=ポイントなし。",
        )

        # ── 🚚 Shipping Policy (W138-A: bp_state は DB 列駆動で常時 dict) ──
        editing["new_bp_id"] = None
        # Codex#1 dirty-flag 用: selectbox を render 時に初期化した値 (= DB 列
        # 由来の現 BP id)。_apply_to_ebay は「submit 値 != この初期値」の時のみ
        # 「user が selectbox を操作した」とみなし、無操作の stale 初期値が実
        # eBay へ巻き戻る経路を遮断する (金銭直結)。
        editing["bp_render_initial_id"] = None
        if bp_state is not None:
            pl = bp_state.get("policies")
            if bp_state.get("ok") and pl is not None and pl.ok and pl.policies:
                ids = [pi.policy_id for pi in pl.policies]
                opts = [pi.name for pi in pl.policies]
                cur_id = bp_state.get("id")
                editing["bp_render_initial_id"] = cur_id
                cur_idx = ids.index(cur_id) if cur_id in ids else 0
                sel_i = st.selectbox(
                    "Shipping Policy (BP)",
                    options=list(range(len(ids))),
                    index=cur_idx,
                    format_func=lambda i: opts[i],
                    # Codex#1-fix2 (金銭直結): widget key に **DB 由来 cur_id を
                    # 含める**。Streamlit は key が session_state に在ると
                    # index= を無視し保存値を返す仕様。固定 key だと ↻/同期で
                    # DB BP が A→B に変わっても widget は旧 A を保持 →
                    # bp_render_initial(=fresh B) と new_bp_id(=stale A) が
                    # 食い違い「無操作なのに touched」誤判定 → 実 eBay の B を
                    # stale A へ巻き戻す (DDP buffer 喪失)。key に cur_id を
                    # 織り込むと DB BP 変化時に **別 widget = fresh 初期化**
                    # され dirty-flag 前提 (無操作⟹widget値==render初期値) を
                    # 回復。同一 DB 状態内は key 安定で user の途中選択を保持。
                    key=f"pm_bp_{eid}_{cur_id}",
                    help="変更すると 📤 eBay 反映 で listing の BP を差し替えます",
                )
                editing["new_bp_id"] = ids[sel_i]
                if ids[sel_i] != cur_id:
                    _ov_c = editing.get("new_ship_cost")
                    _ov_a = editing.get("new_ship_additional")
                    _cur = (f"現在 Buyer pays ${float(_ov_c):.2f}"
                            if _ov_c is not None else "現在 Buyer pays 未設定")
                    if _ov_a is not None:
                        _cur += f" / +each ${float(_ov_a):.2f}"
                    st.warning(
                        "⚠️ BP を変更すると送料が**新 BP の default に戻ります**。"
                        f"({_cur})"
                    )
                    st.caption(
                        "現在の custom 送料に **DDP 関税 buffer** が含まれる場合、"
                        "buffer が消え **売主の関税負担 (赤字方向、Section 232 "
                        "該当品は数百ドル/件)** が発生し得ます。新 BP default 額は "
                        "BP 適用後 GetItem で判明 (変更前取得不可、eBay 仕様)。"
                        "変更後に送料を再設定してください。"
                    )
            else:
                st.caption(
                    "🚚 Shipping Policy 変更不可: "
                    f"{bp_state.get('error') or '現在 BP / BP 一覧の取得に失敗'}"
                )

    # ──────────────────────────────────────────────────────────────────────
    # 📦 商品属性 (SKU / 在庫 / 物理属性) — money 系より控えめに
    # ──────────────────────────────────────────────────────────────────────
    st.markdown('<div class="pm-section-label">📦 商品属性</div>',
                unsafe_allow_html=True)

    # 🏷️ SKU + 在庫数 (stock prefix のみ在庫数表示)
    sku_col, inv_col = st.columns(2)
    with sku_col:
        editing["sku"] = st.text_input(
            "SKU (在庫種別)",
            value=current_sku,
            key=f"pm_sku_{eid}",
            help="stock* で始まる = 有在庫 (在庫数管理対象) / ebay* で始まる = 無在庫",
        )
    with inv_col:
        # SKU が現在 stock* で始まるなら在庫数入力欄表示
        # NOTE: form 内のため text_input の変更を即時参照できない (submit 後反映).
        #       なので「保存時点」での current_sku ベース判定で OK.
        sku_is_stock = current_sku.startswith("stock")
        if sku_is_stock:
            current_inv = p.get("inventory_count")
            editing["inventory_count"] = st.number_input(
                "在庫数 (物理在庫)",
                min_value=0,
                value=int(current_inv) if current_inv is not None else None,
                step=1,
                key=f"pm_inv_{eid}",
                help="物理在庫. 売れたら GetOrders API で自動減算 (in_stock SKU のみ).",
            )
            # W133 (2026-05-16): eBay 数量 sync の最終状態を表示 (痕跡層 / Q0).
            _sync_err = p.get("qty_sync_error")
            if _sync_err:
                st.caption(f"⚠️ eBay 数量反映エラー: {str(_sync_err)[:120]}")
            else:
                _sync_at = p.get("last_qty_sync_at")
                _sync_qty = p.get("last_synced_quantity")
                if _sync_at:
                    st.caption(
                        f"✅ eBay 数量反映: {_sync_at} (数量 {_sync_qty})"
                    )
        else:
            # W205 (2026-05-31): 無在庫 (ebay* SKU) は物理在庫を持たないが、
            # eBay 出品数量 (quantity_ebay) は手動で持ち上げられる。
            # 無在庫は Amazon/楽天/Yahoo から無限調達可 = 売れて0になり販売
            # 機会を逃すのを防ぐため、任意の数量を eBay へ即反映する。
            # 自動補充は対象外 (手動編集のみ / K1)。
            editing["inventory_count"] = None
            sku_is_supplier = current_sku.startswith("ebay")
            if sku_is_supplier:
                _cur_qty = p.get("quantity_ebay")
                editing["quantity_ebay_manual"] = st.number_input(
                    "eBay 出品数量 (無在庫)",
                    min_value=0,
                    value=int(_cur_qty) if _cur_qty is not None else 0,
                    step=1,
                    key=f"pm_qtyebay_{eid}",
                    help="無在庫 listing の eBay 出品数量。保存で eBay へ即反映 "
                         "(Amazon/楽天/Yahoo から調達可なので0切れ防止に持ち上げる)。",
                )
                # 痕跡層 (Q0): eBay 数量 sync の最終状態を表示。
                _sync_err = p.get("qty_sync_error")
                if _sync_err:
                    st.caption(f"⚠️ eBay 数量反映エラー: {str(_sync_err)[:120]}")
                else:
                    _sync_at = p.get("last_qty_sync_at")
                    _sync_qty = p.get("last_synced_quantity")
                    if _sync_at:
                        st.caption(
                            f"✅ eBay 数量反映: {_sync_at} (数量 {_sync_qty})"
                        )
            else:
                st.caption("在庫数管理対象外 (stock*/ebay* 以外の SKU)")

    # ── W31 (2026-06-20): タイトル編集 (任意) ──
    # dirty-flag: render 時の DB 値を保持し、無操作時は eBay に push しない。
    # 80 文字制限は _apply_listing_content_to_ebay → revise_item_title で validate。
    _cur_title = (p.get("title") or "").strip()
    editing["title_render_initial"] = _cur_title
    _new_title_val = st.text_input(
        "商品タイトル (eBay Title / 80 文字以内)",
        value=_cur_title,
        max_chars=80,
        key=f"pm_title_{eid}",
        help="変更後に 📤eBay反映 すると eBay Title も更新 (変更した時のみ)。"
             "80 文字超は反映を拒否します。",
    )
    editing["new_title"] = _new_title_val.strip() if _new_title_val else ""
    _title_len = len(editing["new_title"])
    if _title_len > 70:
        st.caption(
            f"⚠️ {'80 文字超 — 反映できません' if _title_len > 80 else f'{_title_len}/80 文字 (残り {80 - _title_len} 文字)'}"
        )

    # 📐 物理属性 (重量 / 長さ / 幅 / 高さ) — モックアップでは控えめなグリッド
    e1, e2 = st.columns(2)
    with e1:
        editing["weight_g"] = st.number_input(
            "重量 (g)",
            min_value=0,
            value=int(p["weight_g"]) if p.get("weight_g") else None,
            step=10, key=f"pm_weight_{eid}",
        )
        editing["length_cm"] = st.number_input(
            "長さ (cm)",
            min_value=0.0,
            value=float(p["length_cm"]) if p.get("length_cm") else None,
            step=1.0, key=f"pm_length_{eid}",
        )
    with e2:
        editing["width_cm"] = st.number_input(
            "幅 (cm)",
            min_value=0.0,
            value=float(p["width_cm"]) if p.get("width_cm") else None,
            step=1.0, key=f"pm_width_{eid}",
        )
        editing["height_cm"] = st.number_input(
            "高さ (cm)",
            min_value=0.0,
            value=float(p["height_cm"]) if p.get("height_cm") else None,
            step=1.0, key=f"pm_height_{eid}",
        )

    # W227 (2026-06-06 根治): 商品「状態」ランク編集。⚠️ 以前は人気度 rank 列
    # (自動ランク更新の S/A/B/C/D/E) を表示していたため、価格編集で人気度Sを eBay
    # Condition Open Box(1500) へ誤上書きする事故が起きた。本 widget は **eBay 実
    # Condition 由来** (_condition_widget_initial: condition_rank 優先 → ebay_condition_id
    # 由来 1000→N/1500→S/7000→As-Is/3000→未設定) を表示し、人気度 rank 列は一切
    # 読み書きしない。未設定 (Used サブランク不明 / eBay未取得) は sentinel で
    # stale write 防止。eBay Condition 反映は 📤eBay反映 (dirty-flag、user 変更時のみ)。
    _RANK_BLANK = "（未設定 / eBay未取得）"
    _RANK_CHOICES = ["N", "S", "A", "B", "C", "D", "PO", "As-Is"]
    _cur_rank = _condition_widget_initial(p)
    _rank_opts = [_RANK_BLANK] + _RANK_CHOICES
    _rank_default = _cur_rank if _cur_rank in _RANK_CHOICES else _RANK_BLANK
    _cid_disp = str(p.get("ebay_condition_id") or "").strip() or "未取得"
    _rank_sel = st.selectbox(
        "商品ランク (eBay 状態)",
        options=_rank_opts,
        index=_rank_opts.index(_rank_default),
        key=f"pm_rank_{eid}",
        help="eBay 実 Condition 由来 (人気度グレードとは別)。N=新品 / S=新品同様 / "
             "A=美品 / B=良品 / C=使用感 / D=難あり / PO=通電のみ / As-Is=未確認。"
             "変更を 📤eBay反映 すると eBay Condition も更新 (変更時のみ)。"
             "Used品は A-PO すべて eBay 上は Used(3000)。",
    )
    editing["rank"] = None if _rank_sel == _RANK_BLANK else _rank_sel
    # dirty-flag: render 時の状態 (eBay Condition 由来) を保持。
    # _apply_listing_content_to_ebay は user が widget を **実際に変更した時のみ**
    # Condition を push する (人気度 stale 値の誤上書き事故を構造的に遮断)。
    editing["rank_render_initial"] = _cur_rank or None
    st.caption(f"現在の eBay Condition: **{_cid_disp}** "
               f"({_CONDITION_ID_LABEL.get(_cid_disp, '—')})"
               + ("　※ Used はサブランク(A-PO)を選ぶと MonoDeck に記録 (eBay は Used のまま)"
                  if _cid_disp == "3000" else ""))
    # W227 (2026-06-06 user 要望): ランク選択時に迷わないよう 8 段階の早見表を
    # 折りたたみで掲示 (CLAUDE.md コンディションランク 8 段階)。eBay Condition との
    # 対応も併記 (N=New / S=Open Box / A-PO=Used / As-Is=For parts)。
    with st.expander("📖 商品ランク早見表 (どれにするか迷ったら)", expanded=False):
        st.markdown(
            "| ランク | 意味 | 外観 × 動作 | eBay Condition |\n"
            "|---|---|---|---|\n"
            "| **N** | 新品・未開封 | シュリンク / 工場出荷 | New (1000) |\n"
            "| **S** | 新品同様 | 開封済だが未使用・使用痕なし | Open Box (1500※) |\n"
            "| **A** | 美品・動作確認済 | 小さな使用痕、全機能動作 | Used (3000) |\n"
            "| **B** | 並品・動作確認済 | 目立つ使用痕、全機能動作 | Used (3000) |\n"
            "| **C** | 使用感あり・動作確認済 | 使用感強い、全機能動作 | Used (3000) |\n"
            "| **D** | 難あり・動作確認済 | 外観/機能に問題、動作は限定的 | Used (3000) |\n"
            "| **PO** | 通電のみ | 電源 ON 確認だけ・動作未確認 | Used (3000) |\n"
            "| **As-Is** | 未確認 / 部品取り | 無保証販売・**理由必須** | For parts (7000) |\n"
        )
        st.caption(
            "判別のコツ: 新品シュリンク=**N** / 未使用だが開封・保管長め=**S** / "
            "動作確認済の中古は使用痕の程度で **A→B→C→D** / 通電だけ=**PO** / "
            "ジャンク・部品取りは**As-Is(理由必須)**。"
            "※ S(Open Box=1500) は一部カテゴリで不可 → eBay 反映時に Used(3000) へ自動降格 (通知あり)。"
        )

    # W220 slice3 (2026-06-04): Condition 理由 (eBay ConditionDescription)。
    # ランクを Used(A/B/C/D/PO) / As-Is に変えて 📤eBay反映 する時に送る状態説明。
    # As-Is(7000) は CLAUDE.md で **必須** (欠落=buyer紛争でDefect確定リスク)。
    # eBay 専用 (DB 非保存)。空なら eBay 側の既存 ConditionDescription を維持。
    editing["condition_description"] = st.text_input(
        "Condition 理由 (中古/As-Is 用・eBay表示)",
        value="",
        key=f"pm_conddesc_{eid}",
        max_chars=1000,
        help="ランクを Used/As-Is に変えて eBay 反映する時の状態説明 (例: "
             "Tested OK / No AC adapter for testing)。As-Is は必須。空なら eBay "
             "側の既存説明を維持。DB には保存しません (eBay 専用)。",
    )

    # ──────────────────────────────────────────────────────────────────────
    # 📎 listing メモ (W140) — 内容あれば自動展開、空なら折りたたみ
    # ──────────────────────────────────────────────────────────────────────
    # eBay へは送信せず MonoDeck DB のみ保持。この listing が売れたら発送前に
    # MonoDeck バナー + Discord で警告 (発送/通関の注意点の見落とし防止)。
    # メモは ebay_item_id 紐付 (sku-rules: SKU をキーにしない)。自動再出品
    # (End→Sell similar) では inherit_listing_on_relist が旧→新へ引き継ぐ。
    _note_body = get_listing_note(eid) or ""
    with st.expander(
        "📎 発送/通関メモ (売れたら警告)" + (" — 入力あり" if _note_body.strip() else " — 空"),
        expanded=bool(_note_body.strip()),
    ):
        editing["note_text"] = st.text_area(
            "listing メモ",
            value=_note_body,
            key=f"pm_note_{eid}",
            max_chars=2000,
            height=80,
            help="例: 電池を抜いて発送 / 通関書類に型番XXX明記。eBay には送信"
                 "されません。保存後この listing が売れると MonoDeck と Discord "
                 "に通知。空にして保存でメモ削除。",
            label_visibility="collapsed",
        )

    # W220 (2026-06-04): description (商品説明文) 編集。ローカル下書きを DB に保持。
    # eBay への反映は slice3 の「📤 eBay反映」(ReviseItem) 経由で明示実行 (即時 push
    # しない=安全側)。内容あれば見出しに「入力あり」、本文は折りたたみ既定。
    _desc_body = p.get("listing_description") or ""
    # C-fix (2026-06-05): listing_description は eBay から一度も取得しておらず
    # (W220 は編集+ReviseItem 送出のみ)、全 listing で空 → 編集欄が空白だった。
    # 「📥 eBayから現在の説明を取得」で GetItem の Description を引いて欄に流し込む。
    # session_state key 方式 (value= 併用は警告 + 上書き不可のため不使用)。
    _desc_key = f"pm_desc_{eid}"
    if _desc_key not in st.session_state:
        st.session_state[_desc_key] = _desc_body
    with st.expander(
        "📝 説明文 (description) 編集"
        + (" — 入力あり" if (st.session_state.get(_desc_key) or "").strip() else " — 空"),
        expanded=False,
    ):
        # W225 (2026-06-05): 旧「📥 eBayから現在の説明を取得」ボタンはここ (st.form 内)
        # にあり、st.form 内に st.button は置けず editor 展開時に StreamlitAPIException
        # でクラッシュしていた (今朝の C-fix で混入、表行選択で必ず開く本 W で顕在化)。
        # 取得は即時アクションのため form **外** ボタン (_render_desc_fetch_button) へ
        # 移設。ここは編集欄のみ。取得値は session_state[pm_desc_{eid}] 経由で連携。
        st.caption("eBay から現在の説明を取り込むには、フォーム上部の "
                   "「📥 eBayから現在の説明を取得」を使用 (取得後この欄に表示)。")
        editing["listing_description"] = st.text_area(
            "description",
            key=_desc_key,
            max_chars=8000,
            height=160,
            help="商品説明文の下書き (HTML 可)。保存で MonoDeck DB に保持。eBay へは "
                 "別途「📤 eBay反映」(ReviseItem) で送信 (slice3)。空保存で下書き削除。",
            label_visibility="collapsed",
        )

    return editing


# =============================================================================
# Right column sections
# =============================================================================

def _render_url_direct_description_section(p: dict) -> None:
    """W152 (2026-05-22 user 要望): 仕入先候補セクションの上に「新規 URL 直接投入」.

    任意の仕入先 URL を直接投入 → 個別出品同等の pipeline (scrape + rank 推定 +
    description 生成) を走らせ、preview 後に user が「✅ eBay に反映」ボタンで
    ReviseItem を発火させる. 既存採用済 supplier_candidate 経由の
    render_supplier_description_section とは別経路 (candidate_id 不要).

    K1 simplicity: 既存 prefetch / generate / apply 関数 3 つを candidate_id=0 で
    流用. supplier_candidates テーブルへの仮 INSERT は行わない (履歴管理は別 W).
    """
    eid = p["ebay_item_id"]
    sku = p.get("sku") or ""
    is_in_stock = sku.startswith("stock")  # 有在庫 = True / 無在庫 = False

    url_key = f"pm_url_direct_input_{eid}"
    result_key = f"pm_url_direct_result_{eid}"

    with st.expander("🔗 引用元 URL / 商品タイトルから description 生成 → eBay 反映",
                     expanded=False):
        st.caption(
            "引用元 URL (メルカリ/ヤフオク/PayPay=専用解析、Amazon/楽天/Yahoo!ショッピング/"
            "ラクマ等=AI解析) を投入すると scrape/AI + rank 推定 + description HTML 生成 (W226)。"
            "URL を空欄にすると商品タイトルから title-only 生成。preview 確認後、画像加工 "
            "(Step B-D) + 3 通りの反映 button で運用 (W158)。"
        )
        product_title = (p.get("title") or "").strip()
        url_input = st.text_input(
            "引用元 URL (空欄なら商品タイトルから生成)",
            value=st.session_state.get(url_key, ""),
            key=url_key,
            placeholder="https://www.amazon.co.jp/dp/... / https://item.rakuten.co.jp/... (空欄=title-only)",
        )
        _url = url_input.strip()
        _is_title_only = not _url

        # 商品ランク選択 (2026-06-07 fix): 商品エディタ『状態』(condition_rank) で
        # 既に設定済みのランクを既定値にして尊重する。
        # 旧挙動の事故: URL 経路が rank_override_code=None で毎回 AI 再判定し、user が
        # 状態=B にしていても description が AI 判定 (例 C) で上書きされ修正不能だった
        # (item 358274830101)。URL/title-only どちらも listing 設定ランクを既定にし、
        # URL 経路では「引用元から AI 自動判定」も選べるようにする (変更可)。
        # eBay Condition へは自動 push しない方針は不変 (W227、状態は商品エディタで確定)。
        from monitor.product_resolver import TITLE_ONLY_DEFAULT_RANK
        _rank_opts = ["N", "S", "A", "B", "C", "D", "PO", "As-Is"]
        _listing_rank = (p.get("condition_rank") or "").strip()
        _rank_override: Optional[str] = None
        if _is_title_only:
            # title-only は URL fetch しない = AI 判定不可。listing 設定 > 既定 N。
            _default = _listing_rank if _listing_rank in _rank_opts else TITLE_ONLY_DEFAULT_RANK
            _rank_override = st.selectbox(
                "商品ランク (商品エディタ『状態』の設定が既定。変更可)",
                options=_rank_opts,
                index=_rank_opts.index(_default),
                key=f"pm_url_direct_rank_{eid}",
                help="生成する description のランク。eBay Condition へは自動反映しません "
                     "(状態は商品エディタ『状態』で確定)。",
            )
            if not product_title:
                st.warning("この商品にはタイトルがありません。URL を入力してください。")
        else:
            # URL 経路: listing 設定ランクを既定で尊重。「引用元から AI 自動判定」も選択可。
            _opts_url = ["(引用元 URL から AI 自動判定)"] + _rank_opts
            _default_idx = _opts_url.index(_listing_rank) if _listing_rank in _rank_opts else 0
            _sel_url = st.selectbox(
                "商品ランク (商品エディタ『状態』の設定が既定。変更可 / AI 自動判定も選択可)",
                options=list(range(len(_opts_url))),
                format_func=lambda i: _opts_url[i],
                index=_default_idx,
                key=f"pm_url_direct_rank_url_{eid}",
                help="既定は商品エディタ『状態』のランク。これで生成すれば AI 判定で"
                     "上書きされません。eBay Condition へは自動反映しません (W227)。",
            )
            _rank_override = _opts_url[_sel_url] if _sel_url > 0 else None

        # 必ず入れたい文言/方針 (任意)。AI が意味を理解し description に自然反映。
        _extra_key = f"pm_url_direct_extra_{eid}"
        _extra_instructions = st.text_area(
            "description に入れたい文言・指示（任意）",
            value=st.session_state.get(_extra_key, ""),
            key=_extra_key,
            placeholder="例: ギフト包装対応可と必ず書いて / バンドル品である点を強調 / 専用ケース付属を明記",
            help="自由記入。AI がこの内容を理解し自然な英語 description に組み込みます。"
                 "（原産国/製造国/Manufacturer の記載は eBay ポリシー上、入れても無視されます）",
        )

        # 依頼ボード#26 (2026-06-17): 旧「① 画像 + description 生成」統合 button を
        # 「① 画像生成」「② description 生成」の 2 button に分割。
        # 画像はそのままで description だけ作り直したい (またはその逆) のニーズに対応。
        # 結果は従来どおり単一 result_key dict を source of truth とし、各 button は
        # 自分が担当する key (image_urls / description_html 等) のみ更新 = 片方再生成で
        # もう片方を破壊しない (後方互換: preview/編集/画像加工セクションは無改変)。
        product_cache_key = f"pm_url_direct_product_{eid}"

        def _existing_result() -> dict:
            cur = st.session_state.get(result_key)
            if isinstance(cur, dict) and cur.get("url") == _url:
                return dict(cur)
            return {
                "url": _url, "title_only": _is_title_only,
                "title_ja": "", "title_en": "", "rank_code": "",
                "rank_reasoning": "", "image_urls": [],
                "description_html": "", "message": "",
            }

        b1, b2 = st.columns(2)
        with b1:
            _gen_img = st.button(
                "① 画像生成",
                key=f"pm_url_direct_gen_img_{eid}",
                disabled=_is_title_only,  # title-only は URL fetch しない = 画像なし
                help="引用元 URL を scrape して商品画像を取得 (description は変更しません)。"
                     if not _is_title_only
                     else "title-only (URL 空欄) では画像を取得できません。",
            )
        with b2:
            _gen_desc = st.button(
                "② description 生成",
                key=f"pm_url_direct_gen_desc_{eid}",
                disabled=(_is_title_only and not product_title),
                help="description HTML を (再) 生成 (画像はそのまま保持)。",
            )

        if _gen_img:
            with st.spinner("scrape/AI 解析 + Claude rank 推定中 (画像取得)..."):
                prefetch = prefetch_supplier_product_and_rank(0, _url)
                if not prefetch.get("success"):
                    # Q0: URL 取得失敗は明示エラー (silent skip 禁止)
                    st.error(
                        f"❌ 画像取得失敗: {prefetch.get('message') or '(原因不明)'}\n\n"
                        f"→ URL を再確認してください。"
                    )
                    return
                product = prefetch.get("product")
                # 2026-06-17 regression fix: cache に URL を紐付ける。eid だけのキーだと
                # URL 変更を検知できず、古い引用元の product で description を生成する事故
                # (item 358046729862 で発覚)。② は URL 一致時のみ再利用する。
                st.session_state[product_cache_key] = {"url": _url, "product": product}
                res = _existing_result()
                res["image_urls"] = list(getattr(product, "image_urls", []) or [])
                if not res.get("title_ja"):
                    res["title_ja"] = getattr(product, "title_ja", "") or ""
                res["message"] = (
                    f"画像 {len(res['image_urls'])} 件取得完了"
                    + ("（description は前回の生成結果を保持）" if res.get("description_html") else "")
                )
                st.session_state[result_key] = res
                st.rerun()

        if _gen_desc:
            with st.spinner("Claude rank 推定 + description 生成中..."):
                rank_reasoning = ""
                if _is_title_only:
                    # title-only: URL fetch せず商品タイトルだけで生成 (捏造しない)
                    from monitor.product_resolver import build_title_only_product
                    product = build_title_only_product(product_title)
                    rank_reasoning = "title-only 生成 (ランクは手動指定)"
                else:
                    # ① 画像生成で取得済みの product があれば再利用 (再 scrape 回避)。
                    # 2026-06-17 regression fix: URL が一致する cache のみ再利用する。
                    # URL を変更した場合は古い product を捨てて新 URL で再 scrape する
                    # (eid だけのキーだと古い引用元で生成される事故 = item 358046729862)。
                    _cached = st.session_state.get(product_cache_key)
                    product = (
                        _cached.get("product")
                        if isinstance(_cached, dict) and _cached.get("url") == _url
                        else None
                    )
                    if product is None:
                        prefetch = prefetch_supplier_product_and_rank(0, _url)
                        if not prefetch.get("success"):
                            # Q0: URL 取得失敗は明示エラー + title-only フォールバック誘導
                            st.error(
                                f"❌ 取得失敗: {prefetch.get('message') or '(原因不明)'}\n\n"
                                f"→ URL 欄を空にすると、この商品のタイトルから title-only で生成できます。"
                            )
                            return
                        product = prefetch.get("product")
                        st.session_state[product_cache_key] = {"url": _url, "product": product}
                        _auto_reasoning = prefetch.get("rank_reasoning") or ""
                    else:
                        _auto_reasoning = ""
                    # 2026-06-07 fix: listing 設定ランク(or user 選択)を尊重。
                    # _rank_override=None の時のみ引用元から AI 自動判定にフォールバック。
                    rank_reasoning = (
                        f"商品エディタ『状態』/手動選択のランク {_rank_override} を使用"
                        if _rank_override
                        else _auto_reasoning
                    )
                gen = generate_supplier_description(
                    candidate_id=0,
                    candidate_url=("" if _is_title_only else _url),
                    in_stock=is_in_stock,
                    prefetched_product=product,
                    rank_override_code=_rank_override,
                    extra_instructions=(_extra_instructions or None),
                )
                if not gen.get("success"):
                    st.error(f"❌ description 生成失敗: {gen.get('message') or '(原因不明)'}")
                    return
                res = _existing_result()
                res["title_only"] = _is_title_only
                res["title_ja"] = getattr(product, "title_ja", "") or res.get("title_ja") or ""
                res["title_en"] = gen.get("title_en") or ""
                res["rank_code"] = gen.get("rank_code") or ""
                res["rank_reasoning"] = rank_reasoning
                res["description_html"] = gen.get("description_html") or ""
                # ① 画像生成をまだ押していない場合、生成 product の画像で埋める (後方互換)。
                if not res.get("image_urls"):
                    res["image_urls"] = list(getattr(product, "image_urls", []) or [])
                res["message"] = (
                    (gen.get("message") or "description 生成完了")
                    + ("（画像は前回取得分を保持）" if st.session_state.get(result_key, {}).get("image_urls") else "")
                )
                st.session_state[result_key] = res
                st.rerun()

        # 結果 preview
        result = st.session_state.get(result_key)
        if result and result.get("url") == url_input.strip():
            st.markdown("---")
            st.success(result.get("message") or "生成完了")
            c1, c2 = st.columns([1, 2])
            with c1:
                imgs = result.get("image_urls") or []
                if imgs:
                    st.image(imgs[0], caption=f"画像 (全 {len(imgs)} 件中 1 枚)",
                             use_container_width=True)
                else:
                    st.caption("画像なし")
            with c2:
                st.markdown(f"**Title (JP)**: {result.get('title_ja', '')[:100]}")
                st.markdown(f"**Title (EN)**: `{result.get('title_en', '')[:100]}`")
                st.markdown(f"**推定 Rank**: `{result.get('rank_code', '?')}`")
                if result.get("rank_reasoning"):
                    with st.expander("rank 判定根拠", expanded=False):
                        st.caption(result["rank_reasoning"])

            # 2026-06-01: description を編集可能化 (個別出品 W190 / 仕入先候補フロー
            # と同等)。widget key を source of truth にし、再生成 (gen_desc 変化) 時
            # のみリセット。編集値 edited_desc を画像加工 + 反映 button へ渡す。
            gen_desc = result.get("description_html") or ""
            sk_ed = f"pm_url_direct_edited_desc_{eid}"
            sk_ed_src = f"pm_url_direct_edited_desc_src_{eid}"
            if st.session_state.get(sk_ed_src) != gen_desc:
                st.session_state[sk_ed] = gen_desc
                st.session_state[sk_ed_src] = gen_desc
            with st.expander(
                f"✏️ description (HTML) を編集 ({len(gen_desc)} 文字)",
                expanded=False,
            ):
                st.text_area(
                    "Description HTML (禁止語句や文言をここで直接修正可)",
                    height=400,
                    key=sk_ed,
                )
                if st.button("↩ 生成結果に戻す", key=f"pm_url_direct_resetdesc_{eid}"):
                    st.session_state[sk_ed] = gen_desc
                    st.rerun()
            edited_desc = st.session_state.get(sk_ed) or ""
            if edited_desc != gen_desc:
                st.caption(
                    f"✏️ 編集済み ({len(edited_desc)} 文字) — この内容で eBay 反映されます"
                )

            from tabs._supplier_description_pipeline import apply_listing_update_to_ebay

            # 依頼ボード#26 (2026-06-17): description 単独で eBay へ反映する button。
            # 画像は触らず description だけアップロード (money-direct = 対象 item_id 明示)。
            # 画像も含めた一括反映は下の W158 画像加工セクションの 3 反映 button を使う。
            if edited_desc.strip():
                st.markdown(f"**反映対象**: `{eid}` (Description のみ / 画像は変更なし)")
                if st.button(
                    "✅ Description を eBay に反映",
                    key=f"pm_url_direct_apply_desc_{eid}",
                    type="primary",
                ):
                    with st.spinner(f"ReviseItem (description) 反映中... item={eid}"):
                        _ap = apply_listing_update_to_ebay(
                            eid, description_html=edited_desc,
                        )
                    if _ap.get("success"):
                        st.success(
                            f"✅ Description を eBay に反映しました (item={eid}, "
                            f"{_ap.get('description_len', len(edited_desc))} 文字)"
                        )
                    else:
                        # Q0: 失敗は必ず痕跡表示 (silent skip 禁止)
                        st.error(
                            f"❌ Description 反映失敗 (item={eid}): "
                            f"{_ap.get('message') or '(原因不明)'}"
                        )

            # ── W158 (2026-05-23): 画像加工 + 3 反映 button (個別出品同等) ──
            from tabs._image_pipeline_ui import (
                render_image_pipeline_section, clear_pipeline_keys,
            )

            w158_prefix = f"pm_url_direct_{eid}_w158_"

            # cascade clear: url_input 変化検知時に shared 内 key 一括クリア
            # (前回 URL の hero/additional 画像が残留しないように)
            w158_url_key = f"pm_url_direct_{eid}_w158_last_url"
            if st.session_state.get(w158_url_key) != url_input.strip():
                clear_pipeline_keys(w158_prefix)
                st.session_state[w158_url_key] = url_input.strip()

            def _on_apply_image_pm(urls: list[str]) -> dict:
                return apply_listing_update_to_ebay(eid, picture_urls=urls)

            def _on_apply_desc_pm(desc: str) -> dict:
                return apply_listing_update_to_ebay(eid, description_html=desc)

            def _on_apply_both_pm(desc: str, urls: list[str]) -> dict:
                return apply_listing_update_to_ebay(
                    eid, description_html=desc, picture_urls=urls,
                )

            render_image_pipeline_section(
                prefix=w158_prefix,
                source_urls=imgs,
                sku_hint=f"eid_{eid}",
                ebay_item_id=eid,
                description_html=edited_desc,
                on_apply_image=_on_apply_image_pm,
                on_apply_description=_on_apply_desc_pm,
                on_apply_both=_on_apply_both_pm,
            )

            if st.button("結果をクリア", key=f"pm_url_direct_clear_{eid}"):
                clear_pipeline_keys(w158_prefix)
                st.session_state.pop(result_key, None)
                st.rerun()


def _render_supplier_section(p: dict, config: dict) -> None:
    """仕入先 (📊 在庫状態 + 🏪 候補 dataframe). W217-B v2 (2026-06-04) で
    _render_right_inventory_supplier_rival から分割。

    K2 surgical: 関数本体の中身・呼出ロジック・「🔍 在庫を今すぐ確認」button・
    仕入先候補採用フロー・dirty-flag は 1 行も変えない (配置移動のみ)。
    form 外であることを呼出側で維持すること (左列でも form block の外に置く)。
    """
    eid = p["ebay_item_id"]

    # ── 📊 仕入先 在庫状態 ──
    st.markdown('<div class="pm-section-label">📊 仕入先 在庫状態</div>',
                unsafe_allow_html=True)
    src_status = p.get("source_status") or "unknown"
    src_last = p.get("source_last_checked") or "-"
    oos_since = p.get("source_out_of_stock_since")
    emoji = _status_emoji(src_status)
    pill_class = ("pm-pill-bad" if src_status == "out_of_stock"
                  else "pm-pill-ok" if src_status == "in_stock"
                  else "pm-pill-warn")
    extra = f" / 在庫切れ since: {oos_since}" if src_status == "out_of_stock" and oos_since else ""
    st.markdown(
        f'<span class="pm-pill {pill_class}">{emoji} {src_status}</span> '
        f'<small>最終 check: {src_last}{extra}</small>',
        unsafe_allow_html=True,
    )

    if p.get("source_url"):
        st.markdown(f"[🔗 仕入先 URL]({p['source_url']})")

    # ── 🔧 W183 (2026-05-28): Amazon / 楽天 等を直接 URL で無在庫監視 ──
    # SKU 規則性の無い EC サイトは source_url を手動固定 (source_url_manual=1)。
    # 固定すると eBay 同期 / SKU 変更で URL が上書きされない
    # (set_listing_source_url_manual)。識別は ebay_item_id (sku-rules 準拠)。
    if p.get("source_url_manual"):
        st.caption("📌 この仕入先 URL は固定中 (eBay 同期 / SKU 変更で上書きされません)")
    with st.expander("🔧 仕入先 URL を直接指定 (Amazon / 楽天 等)", expanded=False):
        _u = st.text_input(
            "仕入先 URL",
            value=p.get("source_url") or "",
            key=f"pm_src_url_{eid}",
            placeholder="https://www.amazon.co.jp/dp/... / https://item.rakuten.co.jp/...",
        )
        _m = st.checkbox(
            "この URL を固定 (eBay 同期・SKU 変更で上書きしない)",
            value=bool(p.get("source_url_manual")),
            key=f"pm_src_manual_{eid}",
            help="Amazon / 楽天 など SKU から URL を生成できない EC サイト向け。",
        )
        if st.button("💾 仕入先 URL を保存", key=f"pm_src_save_{eid}"):
            from monitor.database import (
                set_listing_source_url_manual, find_site_config_by_url,
            )
            _u2 = (_u or "").strip()
            if _m and not _u2:
                st.error("固定 ON の場合は URL を入力してください。")
            elif _u2 and not _u2.startswith(("http://", "https://")):
                st.error("URL は http:// または https:// で始めてください。")
            else:
                if set_listing_source_url_manual(eid, _u2, manual=_m):
                    if _m and _u2 and find_site_config_by_url(_u2) is None:
                        st.warning(
                            "保存しました。ただしこの URL のサイト設定 (site_configs) が"
                            "未登録のため、在庫判定は unknown になり得ます。"
                        )
                    else:
                        st.success("仕入先 URL を保存しました。")
                    bump_db_version()
                    st.rerun()
                else:
                    st.error("保存に失敗しました (listing が見つかりません)。")

        # 在庫を今すぐ確認 (定時 02:30 を待たず、この URL を同一ロジックで判定)。
        # 結果表示のみ = DB の source_status は更新しない (定時チェックが正式反映)。
        # 新ショップ URL/サイト設定の動作テストにも使える。
        if st.button("🔍 在庫を今すぐ確認", key=f"pm_src_check_{eid}",
                     help="定時(02:30)を待たず仕入先URLの在庫を今チェック。結果表示のみ(DB非更新)"):
            from monitor.database import (
                find_site_config_by_url, find_site_config_by_sku,
            )
            from monitor.scrapers import check_item_by_config
            _curl = (_u or p.get("source_url") or "").strip()
            if not _curl:
                st.error("仕入先 URL がありません (上に入力して保存してください)。")
            else:
                _cfg = (find_site_config_by_url(_curl)
                        or find_site_config_by_sku(p.get("sku") or ""))
                if not _cfg:
                    st.warning(
                        "このサイトの在庫判定設定 (site_configs) が未登録です。"
                        "「在庫監視 > サイト別設定」で 売切/在庫あり テキストを登録すると判定できます。"
                    )
                else:
                    with st.spinner("在庫確認中… (httpx → Playwright)"):
                        try:
                            _status = check_item_by_config(
                                {"source_url": _curl}, _cfg)
                        except Exception as _e:  # noqa: BLE001
                            _status = "unknown"
                            logger.warning(f"[pm 在庫確認] {eid}: {_e}")
                    _em = {
                        "available": "🟢 在庫あり", "unavailable": "🔴 在庫切れ",
                        "not_found": "⚠️ ページなし(削除/404)", "unknown": "❓ 判定不能",
                    }.get(_status, f"❓ {_status}")
                    st.info(f"判定: **{_em}** （サイト設定: {_cfg.get('site_name')}）")
                    st.caption(
                        f"使用マーカー — 在庫あり「{_cfg.get('in_stock_text1','')}」"
                        f"／ 売切「{_cfg.get('sold_out_text','')}」。"
                        f"結果表示のみ・DB 非更新（正式反映は 02:30 定時チェック）。"
                        f"❓ の場合はこのサイト専用ロジックが必要（URLを伝えてください）。"
                    )

    # 監視 URL 一覧 (折りたたみ、簡潔)
    monitored = _fetch_monitored_items_for_listing(eid)
    if monitored:
        with st.expander(f"監視 URL: {len(monitored)} 件", expanded=False):
            for m in monitored:
                active = "🟢" if m.get("is_active") else "⚫"
                st.markdown(
                    f"{active} {m.get('last_status') or '-'} | "
                    f"最終 {m.get('last_check') or '-'} | "
                    f"[URL]({m.get('source_url') or ''})"
                )

    # ── 🔗 W152 (2026-05-22): 新規 URL 直接投入 → 画像 + description 生成 → eBay 反映 ──
    _render_url_direct_description_section(p)

    # ── 🏪 仕入先候補 dataframe ──
    st.markdown('<div class="pm-section-label">🏪 仕入先候補 (利益額高い順)</div>',
                unsafe_allow_html=True)
    candidates = _fetch_supplier_candidates_for_listing(eid)
    if not candidates:
        st.caption("候補なし (毎日 02:30 自動検出、または手動 supplier 検索)")
    else:
        df = pd.DataFrame([
            {
                "状態": {
                    "applied": "✅採用", "accepted": "🟢採用済",
                    "pending": "⏳保留", "rejected": "❌却下",
                }.get(c.get("status"), c.get("status") or "?"),
                "利益¥": int(c["profit_jpy"]) if c.get("profit_jpy") else None,
                "仕入¥": int(c["candidate_price_jpy"]) if c.get("candidate_price_jpy") else None,
                "match": c.get("match_score"),
                "platform": c.get("source_platform") or "-",
                "title": (c.get("candidate_title") or "")[:50],
                "URL": c.get("candidate_url") or "",
                "備考": _supplier_note(c),
            }
            for c in candidates[:10]
        ])
        # M2 fix: 桁区切り (¥123,456) format で大きい金額の可読性向上
        st.dataframe(
            df,
            column_config={
                "状態": st.column_config.TextColumn("状態", width="small"),
                "利益¥": st.column_config.NumberColumn("利益¥", format="¥%,d", width="small"),
                "仕入¥": st.column_config.NumberColumn("仕入¥", format="¥%,d", width="small"),
                "match": st.column_config.NumberColumn("match", width="small"),
                "platform": st.column_config.TextColumn("platform", width="small"),
                "title": st.column_config.TextColumn("title", width="large"),
                "URL": st.column_config.LinkColumn("URL", display_text="開く", width="small"),
                "備考": st.column_config.TextColumn("備考", width="medium"),
            },
            hide_index=True,
            use_container_width=True,
            key=f"pm_supplier_df_{eid}",
        )


def _render_ebaymag_section(p: dict) -> None:
    """eBaymag 国別出品管理 (依頼ボード#10 / 2026-06-13).

    ebaymag_products (v75) のマッピングがある listing のみ操作 UI を表示。
    操作実体は monitor/ebaymag_driver.py (CDP Chrome 経由、実証済 5 step +
    安全弁 3 種: itm 照合 / 変動数チェック / リロード定着検証)。

    誤 OFF 防止: 状態キャッシュが無い間は「反映」を無効化 (checkbox 既定 False の
    まま反映すると ON 中の国を意図せず OFF にするため、先に「状態取得」必須)。
    """
    eid = str(p.get("ebay_item_id") or "")
    mapping = get_ebaymag_product(eid)
    with st.expander("🌍 eBaymag 国別出品 (UK / DE / FR / IT / ES / CA / AU)",
                     expanded=False):
        # ── W284 (2026-06-20): 区分4択 + 希望保存 (mapping 有無に関わらず常時表示) ──
        # 希望は ebay_listings を単一真実源に保存し、apply_queue へ enqueue。
        # 実反映は Phase2 のキュー消化 (CDP 在席時に discover→実態取得→差分適用)。
        from tabs._ebaymag_section import (
            render_segment_selector, infer_segment_from_sites,
        )
        from monitor.database import (
            get_ebaymag_desired, set_ebaymag_desired, enqueue_ebaymag_apply,
        )
        # ── 初期値の決定 (user 要望 2026-06-20: 既設定商品は eBaymag 実態をデフォルト) ──
        # 優先: ①保存済み希望 ②eBaymag 実態キャッシュ(現在 ON の国) ③出さない(新規)。
        # ②③の安全弁: mapping 有(=eBaymag設定済)だが実態未取得の時は、誤って『出さない』
        # 保存→全各国版 取り下げ事故を防ぐため警告 + 希望保存を無効化し『状態取得』を促す。
        _cur = get_ebaymag_desired(eid)
        _states = (mapping or {}).get("site_states") or {}
        _has_actual = bool(_states)
        _mapped_uncached = mapping is not None and not _has_actual
        if _cur and _cur.get("segment"):
            _def_seg = _cur.get("segment")
            _def_des = _cur.get("desired_sites")
        elif _has_actual:
            _inf = infer_segment_from_sites([c for c, v in _states.items() if v])
            _def_seg, _def_des = _inf["segment"], _inf["desired_sites"]
        else:
            _def_seg, _def_des = None, None  # render_segment_selector が『出さない』既定
        if _mapped_uncached:
            st.warning(
                "⚠️ この商品は eBaymag 設定済みですが、現在の国別状態が未取得です。"
                "誤って取り下げないよう、先に下の『🔄 eBaymag から現在状態を取得』を実行"
                "してから希望を保存してください。"
            )
        _sel = render_segment_selector(
            f"ebaymag_seg_{eid}", ebay_item_id=eid,
            current_segment=_def_seg, current_desired=_def_des,
        )
        if st.button("💾 希望を保存 (eBaymag 反映待ちに登録)",
                     key=f"ebaymag_savedesired_{eid}",
                     disabled=_mapped_uncached):
            set_ebaymag_desired(eid, _sel["segment"], _sel["desired_sites"])
            enqueue_ebaymag_apply(eid, "segment_change")
            st.toast("希望を保存しました。ログイン中の自動反映で各国へ適用されます。",
                     icon="✅")
            st.rerun()
        st.divider()

        if mapping is None:
            st.caption(
                "eBaymag 連携 (productId) 未登録。上で希望を保存すると、"
                "ログイン中の自動反映時に eBaymag を検索して登録・公開します。"
                "(手動の即時反映は連携登録後に使えます)"
            )
            return

        from monitor import ebaymag_driver  # playwright import をタブ読込から遅延

        states: dict = mapping.get("site_states") or {}
        synced = mapping.get("last_synced_at")
        applied = mapping.get("last_applied_at")
        st.caption(
            f"productId: {mapping['product_id']} / 状態取得: {synced or '未'} / "
            f"最終反映: {applied or '無'}"
            + (f" ({mapping.get('last_apply_result')})"
               if mapping.get("last_apply_result") else "")
        )

        # ── 状態取得 (read-only、CDP Chrome 必須) ──
        if st.button("🔄 eBaymag から現在状態を取得", key=f"ebaymag_sync_{eid}",
                     help="CDP Chrome (port 9222) で eBaymag ログイン済タブが必要"):
            with st.spinner("eBaymag panel を開いて状態取得中 (~30 秒)..."):
                res = ebaymag_driver.fetch_site_states(
                    mapping["product_id"], expected_itm=eid)
            if res.ok:
                upsert_ebaymag_product(eid, mapping["product_id"], res.site_states)
                # checkbox 既定値を最新状態に追従 (stale session_state 残留防止)
                for code in ebaymag_driver.SITE_MAP:
                    st.session_state[f"ebaymag_cb_{eid}_{code}"] = bool(
                        res.site_states.get(code))
                # st.success は直後の rerun で描画されない → toast (rerun 跨ぎ表示)
                st.toast(f"eBaymag 状態取得完了: {res.site_states}", icon="✅")
                st.rerun()
            else:
                st.error(res.error or "状態取得失敗")
            return

        if not states:
            st.warning("国別状態が未取得です。先に「現在状態を取得」を実行してください "
                       "(誤 OFF 防止のため反映は無効化中)。")
            return

        # ── 国別 checkbox (既定 = キャッシュ状態) ──
        codes = list(ebaymag_driver.SITE_MAP)
        cols = st.columns(len(codes))
        desired: dict[str, bool] = {}
        for col, code in zip(cols, codes):
            with col:
                desired[code] = st.checkbox(
                    code, value=bool(states.get(code)),
                    key=f"ebaymag_cb_{eid}_{code}")

        diff_on = [c for c in codes if desired[c] and not states.get(c)]
        diff_off = [c for c in codes if not desired[c] and states.get(c)]
        if diff_on or diff_off:
            st.caption(f"変更予定: ON={diff_on or 'なし'} / OFF={diff_off or 'なし'}")

        if st.button("📤 eBaymag に反映", key=f"ebaymag_apply_{eid}",
                     type="primary", disabled=not (diff_on or diff_off)):
            with st.spinner("eBaymag へ反映中 (トグル→保存→定着検証、~1-2 分)..."):
                res = ebaymag_driver.apply_site_changes(
                    mapping["product_id"], expected_itm=eid,
                    turn_on=diff_on, turn_off=diff_off)
            record_ebaymag_apply(
                eid, "ok" if res.ok else (res.error or "ng")[:200],
                site_states=res.site_states if res.site_states else None)
            if res.ok:
                # W284 HIGH-1 (code-reviewer 2026-06-20): 手動反映後の最終 checkbox
                # 状態を desired にも保存し、希望(ebaymag_desired_sites_json)と実態の
                # 乖離を防ぐ。これが無いと Phase2 のキュー消化 (desired 再読込で差分
                # 適用) が手動反映を巻き戻す (サイレント rollback)。手動 ON/OFF は
                # 実質「カスタム」操作なので segment='カスタム' + 最終状態で保存。
                _final_sites = [c for c in codes if desired[c]]
                set_ebaymag_desired(eid, "カスタム", _final_sites)
                # st.success は直後の rerun で描画されない → toast (rerun 跨ぎ表示)
                st.toast(f"eBaymag 反映完了 (定着検証済): {res.site_states}",
                         icon="✅")
                st.rerun()
            else:
                st.error(res.error or "反映失敗")
                if res.log:
                    with st.expander("実行ログ", expanded=False):
                        st.code("\n".join(res.log))


def _kw_prefill_values(existing, title: str, eid: str) -> dict:
    """W#33 v2: キーワード新着監視フォームの pre-fill 値を計算する純関数 (UI から分離=テスト可能).

    - existing=None → 新規 default。本家 tab_keyword_watch._render_add_form と同じ
      (下限なし=ON / 上限なし=OFF、keyword=商品タイトル先頭60字、item_id=この商品 eid)。
    - existing あり → その watch の値。price_min/max が None または 0 は「なし」扱い
      (_build_search_url が 0/None を同一視するのと一貫)。
    """
    if existing:
        pmin_raw = existing.get("price_min_jpy")
        pmax_raw = existing.get("price_max_jpy")
        return {
            "keyword": existing.get("keyword") or "",
            "pmin": int(pmin_raw) if pmin_raw else 0,
            "pmin_unset": not pmin_raw,
            "pmax": int(pmax_raw) if pmax_raw else 0,
            "pmax_unset": not pmax_raw,
            "memo": existing.get("memo") or "",
            "item_id": existing.get("ebay_item_id") or eid,
        }
    return {
        "keyword": title[:60],
        "pmin": 0, "pmin_unset": True,
        "pmax": 0, "pmax_unset": False,
        "memo": "",
        "item_id": eid,
    }


def _render_keyword_watch_toggle(p: dict) -> None:
    """W#33 v2 (2026-06-21 user 要望): 商品エディタ内 キーワード新着監視。

    旧版 (site=yahoo 固定・価格設定なしで title を盲目 add) を廃し、
    本物の新規追加フォーム (サイト/キーワード/価格帯/メモ/eBay Item ID) を
    商品文脈で開く。tab_keyword_watch の新規追加と同等の入力を提供する。

    - 現在紐付いている is_active=1 watches を一覧表示 (解除可)
    - サイトを切り替えると、その (eid, site) に**設定済みの値を表示**
      (メルカリ→メルカリ設定値 / ヤフオク→ヤフオク設定値)。未設定なら新規 default
    - 既存あり=「更新」(update_watch) / なし=「追加」(add_watch)
    - 登録/更新失敗は必ず可視化 (Q0 silent skip 禁止)
    - form 外で呼ぶ前提 (本関数内 st.form はネストしない: caller は form 外で呼出)
    """
    from monitor.keyword_watch_db import (
        add_watch, delete_watch, list_watches, update_watch,
    )

    eid = p["ebay_item_id"]
    title = p.get("title") or ""

    st.markdown(
        '<div class="pm-section-label">🔔 キーワード新着監視</div>',
        unsafe_allow_html=True,
    )

    # 現在紐付いている watches を取得 (is_active=1 のみ)
    all_watches = list_watches(active_only=True)
    linked = [w for w in all_watches if w.get("ebay_item_id") == eid]

    if linked:
        for w in linked:
            wcols = st.columns([5, 1])
            with wcols[0]:
                st.caption(
                    f"[{w['site']}] {w['keyword']} "
                    f"{'(上限¥' + str(w['price_max_jpy']) + ')' if w.get('price_max_jpy') else ''}"
                )
            with wcols[1]:
                if st.button("解除", key=f"pm_kw_del_{eid}_{w['id']}"):
                    try:
                        deleted = delete_watch(w["id"])
                        if deleted:
                            bump_db_version()
                            st.rerun()
                        else:
                            st.warning(f"解除失敗: watch id={w['id']} が見つかりません")
                    except Exception as e:
                        st.error(f"解除エラー: {e}")
    else:
        st.caption("監視未登録")

    # ── 新規追加 / 既存編集 フォーム (本物の キーワード新着監視 新規追加と同等) ──
    # site selectbox は form 外 = 切替で (eid, site) の既存値を即時反映するため。
    from tabs.tab_keyword_watch import _build_search_url

    _SITES = ["mercari", "yahoo_auctions"]
    _site_label = lambda x: "🛒 メルカリ" if x == "mercari" else "🔨 ヤフオク"
    site = st.selectbox(
        "サイト", _SITES, format_func=_site_label, key=f"pm_kw_site_{eid}",
        help="サイトを切り替えると、そのサイトに設定済みの値が表示されます",
    )
    # 選択中サイトの既存 watch (あれば編集、なければ新規)
    existing = next((w for w in linked if w.get("site") == site), None)
    if existing:
        st.caption(f"✅ このサイトは設定済み (#{existing['id']}) — 値を編集して更新できます")

    # pre-fill は純関数で計算 (テスト可能化、HIGH-2)。
    pf = _kw_prefill_values(existing, title, eid)
    # HIGH-1 fix: widget key に existing.id (なければ 'new') を織り込む。
    # 解除→rerun や 既存↔新規 遷移で別 widget 化 → value= が再評価される
    # (key を {eid}_{site} だけにすると、解除後も古い session_state 値が残る Streamlit 落とし穴)。
    _wkey = f"{eid}_{site}_{existing['id'] if existing else 'new'}"

    with st.form(f"pm_kw_form_{_wkey}", clear_on_submit=False):
        keyword = st.text_input(
            "キーワード", value=pf["keyword"],
            placeholder="例: Astell&Kern A&norma SR35",
            key=f"pm_kw_kw_{_wkey}",
        )
        fc1, fc2 = st.columns(2)
        with fc1:
            pmin = st.number_input(
                "下限 (¥、空欄=なし)", min_value=0, value=pf["pmin"], step=1000,
                key=f"pm_kw_pmin_{_wkey}",
            )
            pmin_unset = st.checkbox(
                "下限なし", value=pf["pmin_unset"], key=f"pm_kw_pminun_{_wkey}",
            )
        with fc2:
            pmax = st.number_input(
                "上限 (¥、空欄=なし)", min_value=0, value=pf["pmax"], step=1000,
                key=f"pm_kw_pmax_{_wkey}",
            )
            pmax_unset = st.checkbox(
                "上限なし", value=pf["pmax_unset"], key=f"pm_kw_pmaxun_{_wkey}",
            )
        st.caption("注: 両方なしだと通知無効 (履歴のみ)")
        memo = st.text_area(
            "メモ (任意)", value=pf["memo"],
            placeholder="採用後のアクション、注意点 等", height=80,
            key=f"pm_kw_memo_{_wkey}",
        )
        item_id = st.text_input(
            "eBay Item ID", value=pf["item_id"],
            key=f"pm_kw_iid_{_wkey}",
            help="紐づく自社 eBay 出品の Item ID (既定でこの商品)。通知時に販売価格を併記。",
        )

        if st.form_submit_button("💾 更新" if existing else "➕ 追加"):
            kw = keyword.strip()
            if not kw:
                st.error("キーワードは必須です")
                return
            pmin_val = None if pmin_unset else int(pmin)
            pmax_val = None if pmax_unset else int(pmax)
            iid_val = item_id.strip() or None
            try:
                url = _build_search_url(site, kw, pmin_val, pmax_val)
            except ValueError as e:
                st.error(str(e))
                return
            try:
                if existing:
                    ok = update_watch(
                        existing["id"], keyword=kw, price_min_jpy=pmin_val,
                        price_max_jpy=pmax_val, memo=memo, search_url=url,
                        ebay_item_id=iid_val,
                    )
                    if ok:
                        st.success(f"#{existing['id']} を更新しました")
                    else:
                        st.warning(f"更新対象が見つかりません (#{existing['id']})")
                else:
                    wid, new = add_watch(
                        site=site, search_url=url, keyword=kw,
                        price_min_jpy=pmin_val, price_max_jpy=pmax_val,
                        memo=memo, source="product_management", ebay_item_id=iid_val,
                    )
                    st.success(
                        f"#{wid} を追加しました" if new
                        else f"#{wid} は同 site + 同 URL で登録済みです"
                    )
                bump_db_version()
                st.rerun()
            except Exception as e:
                st.error(f"保存エラー: {e}")

    st.markdown("---")


def _render_rival_section(p: dict, config: dict) -> None:
    """ライバル集約パネル (🎯 監視 + 登録済 dataframe). W217-B v2 (2026-06-04)
    で _render_right_inventory_supplier_rival から分割。

    K2 surgical:
      - _render_rival_watch_section / _render_rival_dataframe の本体は不変
      - 競合再シード (_pm_seed_comp_session), widget key (pm_comp_{eid}_{idx}),
        upsert_listing_competitors, set_rival_search_keywords, dirty-flag は
        1 行も変更しない (空欄全消失事故の機序を回避)
      - form 外で呼ぶこと (個別 button 即時反応を維持)
    """
    _render_rival_watch_section(p, config)
    _render_rival_dataframe(p, config)


def _supplier_note(c: dict) -> str:
    """supplier_candidate の備考 (junk / alt_listing)."""
    notes = []
    if c.get("junk_likely_untested"):
        notes.append("⚠️ジャンク表記")
    if c.get("alt_listing_possible"):
        n = c.get("alt_listing_note") or ""
        notes.append(f"💡別SKU: {n[:20]}")
    return " / ".join(notes)


def _pm_seed_comp_session(
    session_state, eid: str, existing_ids: list[str], max_competitors: int
) -> None:
    """③同型 データ損失修正の core (純関数、Streamlit 非依存=テスト可能).

    Streamlit は key 付き text_input の value= を「その key が session_state
    既出の後」無視する。その結果 DB 登録済み競合が編集欄に出ず空欄 →
    💾DB保存/📤eBay反映 が upsert_listing_competitors の全置換で **登録済み
    active 競合を全消滅** させていた (app.py commit 35f87d9 の ③ 修正と同型。
    本ファイルは未適用だったため最安値チェックで登録しても商品管理タブ保存
    で消えていた = user 実報告)。

    対策 = ③ と同一: DB 競合 id 集合を signature 化し、(widget key 不在 =
    listing 切替で Streamlit が未描画 widget state を破棄 / 初回) OR
    signature 変化 の時だけ session_state を DB 値で再シード。plain rerun
    (signature 不変) では再シードせず user 入力途中を温存。意図的 clear-all
    (表示された id を user が空欄化) は再シードされず削除として機能 (③同様)。
    `st.session_state` は dict 互換のため plain dict で単体検証可。
    """
    # K1: 3rd occurrence で共通化済。本関数は薄い wrapper (既存 6 回帰
    # テストは _pm_seed_comp_session 経由で挙動を固定、委譲後も不変)。
    seed_keyed_list_from_db(
        session_state, f"pm_comp_{eid}_", f"_pm_comp_loaded_sig_{eid}",
        existing_ids, max_competitors,
    )


def _render_rival_dataframe(p: dict, config: dict) -> None:
    """ライバル (active competitor_products) を dataframe で表示 + 編集 + 再取得."""
    eid = p["ebay_item_id"]
    st.markdown('<div class="pm-section-label">🎯 ライバル (登録済 active)</div>',
                unsafe_allow_html=True)

    pricing_rows = get_competitors_with_pricing(eid)
    if not pricing_rows:
        st.caption("登録ライバルなし. 下の編集 form で item id 入力 or W184 alerts から追加.")
    else:
        df_rows = []
        for r in pricing_rows:
            min_d = r.get("min_delivery_date")
            handling = _delivery_days_from_now(min_d)
            handling_str = f"{handling} 日後" if handling is not None else "-"
            df_rows.append({
                "item id": r["competitor_item_id"],
                "リンク": f"https://www.ebay.com/itm/{r['competitor_item_id']}",
                "商品価格": r["price_usd"],
                "送料": r["shipping_usd"],
                "合計": r["total_usd"],
                "発送目安": handling_str,
                "最終取得": r["last_priced_at"] or "-",
            })
        # W217-A (2026-06-03): モックアップ準拠で HTML table 化.
        # 旧 st.dataframe (canvas 描画) は行背景色不可で、最安行を 🥇 絵文字
        # のみで表現していた。モックの「最安行 緑背景 + 合計緑文字」を満たす
        # ため、純関数 _render_rival_table_html で HTML <table> を組み立てる。
        # 既存 _lowest_rival_marker (🥇 prefix 付与) はそのまま流用、テーブル
        # 側で 🥇 prefix を見て pm-rival-best 行 class を当てる。
        # リンク列は <a target="_blank">開く</a> で LinkColumn と等価。
        df_rows = _lowest_rival_marker(df_rows)
        _rival_table_html = _render_rival_table_html(df_rows)
        st.markdown(_rival_table_html, unsafe_allow_html=True)

        # W217 (2026-06-03): 自社総額 vs 競合最安 → 競合差 1 行 (採算パネルと
        # 同じ色ロジック)。競合最安 = ebay_listings.competitor_min_price
        # (L165 の集計列)、自社総額 = 編集中なら form 値、なければ DB 値
        # (_hero_effective と同じ source of truth)。
        _eff_own = _hero_effective(p)
        _own_total = _eff_own["price"] + _eff_own["ship"]
        _gap_html = _competitor_gap_line(
            _own_total, p.get("competitor_min_price"),
        )
        if _gap_html:
            st.markdown(_gap_html, unsafe_allow_html=True)

    existing_ids = [r["competitor_item_id"] for r in pricing_rows]

    # W217 (2026-06-03): ライバル item id 編集グリッドを expander 降格 (誤操作保護).
    # 編集グリッド (5x2) は upsert_listing_competitors の全置換挙動で、
    # signature 駆動再シードが正しく動かないと「空欄保存で全消失」事故になる
    # ため、⚠️ 警告つきで折りたたみ default にして誤操作を防ぐ。
    # 重要: _pm_seed_comp_session の widget key (pm_comp_{eid}_{idx}) は不変。
    # expander 本体は折りたたんでも body は毎 rerun 実行されるため (Streamlit
    # 仕様)、シードと session_state 集計は折りたたみ時も正常動作する。
    with st.expander(
        "⚠️ ライバル item id を編集 (空欄=削除・全置換注意)",
        expanded=False,
    ):
        st.caption("**ライバル item id 編集** (eBay 12-13 桁、空欄で削除)")
        # ③同型 データ損失修正 (2026-05-18、user 実報告): DB 登録済み競合が
        # 編集欄に出ず空欄のまま 💾DB保存/📤eBay反映 で全消滅していた。
        # signature 駆動再シードで根治 (詳細・機序は _pm_seed_comp_session
        # docstring)。純関数化しテストで本物を検証 (drift 防止)。
        _pm_seed_comp_session(
            st.session_state, eid, existing_ids, _MAX_COMPETITORS
        )
        comp_inputs: list[str] = []
        rows_count = (_MAX_COMPETITORS + 4) // 5
        for r in range(rows_count):
            cols = st.columns(5)
            for c in range(5):
                idx = r * 5 + c
                if idx >= _MAX_COMPETITORS:
                    break
                with cols[c]:
                    # value= は渡さない: session_state[key] を唯一の真実源に
                    # (value= と session_state 併用は Streamlit が警告)。③同型。
                    val = st.text_input(
                        f"#{idx + 1}",
                        key=f"pm_comp_{eid}_{idx}",
                        placeholder="(空)",
                        label_visibility="collapsed",
                    )
                    comp_inputs.append(val.strip())
        st.session_state[f"pm_comp_list_{eid}"] = [c for c in comp_inputs if c]

    # W217 (2026-06-03): 価格再取得 + CLI 一括候補を共通 expander に格納 (普段非表示).
    # 「🔄 価格再取得 (Browse API)」と「🆕 CLI 一括候補」は普段見ない情報
    # (再取得は cron 自動、CLI 候補は初期登録専用) のためまとめて折りたたみ。
    # 2026-06-11 user 指示 (保存フロー一体化): ボタン 1 押下で「編集グリッドの
    # 内容を DB 保存 → 価格再取得」を連続実行する。従来は 直打ち→💾DB保存→
    # 反映確認→再取得 の 3 ステップで、保存を忘れると旧ライバルを再取得する
    # 罠があった。ボタン名も「保存される」ことが伝わる名前に変更。
    # 保存内容 = pm_comp_list (編集グリッドの現在値、空欄=削除・全置換)。
    # グリッド expander body は折りたたみ時も毎 rerun 実行されるため、
    # ここでの読出しは常に最新 (上の W217 コメント参照)。
    with st.expander(
        "💾 ライバル保存 + 価格再取得 (Browse API) / 🆕 CLI 一括候補 (初期登録用)",
        expanded=False,
    ):
        comp_list = st.session_state.get(f"pm_comp_list_{eid}", [])
        if (pricing_rows or comp_list) and st.button(
            "💾 ライバル保存 + 価格再取得 (Browse API)",
            key=f"pm_refresh_comp_{eid}", use_container_width=True,
            help="編集グリッドの item id を DB 保存してから Browse API で価格を再取得します (空欄=削除・全置換)",
        ):
            with st.spinner("ライバル保存 + Browse API で価格再取得中..."):
                try:
                    upsert_listing_competitors(
                        our_item_id=eid,
                        competitor_item_ids=comp_list,
                    )
                    bump_db_version()  # W134 Step2: 競合更新後 read-cache 無効化
                    result = refresh_competitor_pricing(eid, config or {})
                    f, fl = result.get("fetched", 0), result.get("failed", 0)
                    if not comp_list:
                        st.warning("ライバル 0 件を保存 (全削除) しました")
                    else:
                        st.success(
                            f"ライバル {len(comp_list)} 件保存 / "
                            f"価格取得 成功 {f} / 失敗 {fl}"
                        )
                    st.rerun()
                except (sqlite3.OperationalError, ValueError, TypeError, KeyError) as e:
                    st.error(f"エラー: {e}")

        # W184「新規発見ライバル alerts」は 2026-05-22 PM の W153 v3
        # (per-listing 検出) で上位互換となったため非表示化. 旧 new_competitor_alerts
        # テーブルは our_item_id 紐付け無しでグローバル pending が全 listing に
        # 混在表示される構造的バグがあった. 関数本体 _render_new_alerts_for_listing
        # と DB table / helper 群 (get_japan_competitor_alerts /
        # fetch_alert_shipping_usd / update_alert_action) は dead code として
        # 残置, 物理削除は別 W で実施.

        # CLI 一括検索結果からの候補追加 (初期登録専用、後で削除予定)
        _render_cli_bulk_candidates(p, existing_ids)


def _render_cli_bulk_candidates(p: dict, registered_ids: list[str]) -> None:
    """CLI 一括検索結果 (data/w119_bulk_results.json) からこの listing 向け候補を表示.

    user が checkbox で選択 → 「✅ 追加登録」で既存 active 競合に **append (merge)**.
    置換ではなく merge なので user が手動追加した既存競合は維持される.
    最大 _MAX_COMPETITORS (10) 件で cap.

    **注意**: 初期登録専用機能. ある程度競合が登録されたら本 section を削除予定.
    """
    eid = p["ebay_item_id"]
    meta, results = _load_bulk_results_cached()

    # 2026-05-12: 3 状態を user に明示 (Q0 silent skip 解消).
    #   - eid not in results        : 未検索 (bulk script でまだ処理されていない)
    #   - results[eid] is None      : API 失敗 (前回 search で 429 等)
    #   - results[eid] == []        : 真の 0 件 (eBay で該当 JP seller なし)
    #   - results[eid] is non-empty : 通常表示
    if eid not in results:
        with st.expander("🆕 CLI 一括検索結果 (未検索)", expanded=False):
            st.caption(
                f"data/w119_bulk_results.json にこの listing の検索結果がありません. "
                f"`python scripts/run_w119_bulk_browse.py` を実行して候補生成してください."
            )
        return
    candidates = results.get(eid)
    if candidates is None:
        # API 失敗 sentinel: 旧実装はここで silent return = 「何も表示しない」=
        # user が困惑する原因. 失敗を明示し、retry 予定または抑制状態を表示.
        # H-NEW-2: errorId 2001 観測時刻が直近 24h 以内なら「本日は無効」と切替.
        from datetime import datetime, timedelta, timezone
        last_2001 = meta.get("last_quota_2001_at")
        quota_locked = False
        if last_2001:
            try:
                _ts = datetime.fromisoformat(last_2001)
                _now = datetime.now(timezone.utc) if _ts.tzinfo else datetime.now()
                quota_locked = (_now - _ts) < timedelta(hours=24)
            except (ValueError, TypeError):
                pass

        if quota_locked:
            with st.expander("🆕 CLI 一括検索結果 (eBay daily quota 制限中)",
                             expanded=False):
                st.error(
                    f"🚫 eBay の **daily quota saturation** (errorId 2001) を "
                    f"{last_2001} に観測しました. "
                    f"自動 retry は **24h 抑制窓** で skip されます (cron も同じ). "
                    f"強制再試行: `python scripts/run_w119_bulk_browse.py "
                    f"--saturated-only 99 --no-getitem --force`"
                )
                st.caption(f"生成: {meta.get('generated_at', '?')}")
        else:
            with st.expander("🆕 CLI 一括検索結果 (前回 API 失敗、自動 retry 予定)",
                             expanded=False):
                st.warning(
                    f"⚠️ 前回の Browse API 検索が失敗しました (HTTP 429 等). "
                    f"次回 cron 発火 (5/12 17:33 primary / 5/13 09:07 safety net) で "
                    f"自動 retry されます. "
                    f"または手動: `python scripts/run_w119_bulk_browse.py "
                    f"--saturated-only 99 --no-getitem`"
                )
                st.caption(f"生成: {meta.get('generated_at', '?')}")
        return
    if candidates == []:
        with st.expander("🆕 CLI 一括検索結果 (真の 0 件)", expanded=False):
            st.caption(
                f"eBay で日本 seller の該当競合が 0 件でした. "
                f"検索キーワード `{p.get('search_keyword', '?')}` が広すぎ / 狭すぎないか確認してください."
            )
        return

    # 既登録は除外
    registered_set = set(registered_ids)
    new_candidates = [c for c in candidates if c.get("legacy_item_id") not in registered_set]

    if not new_candidates:
        with st.expander("🆕 CLI 一括検索結果 (全候補が既登録済)", expanded=False):
            st.caption(
                f"data/w119_bulk_results.json の候補 {len(candidates)} 件は "
                f"すべて active 競合として登録済."
            )
        return

    n_slots_left = _MAX_COMPETITORS - len(registered_ids)
    title_label = (
        f"🆕 CLI 一括検索からの候補追加 ({len(new_candidates)} 件未登録、"
        f"残 slot {max(n_slots_left, 0)})"
    )
    # UX: 既登録 0 件の listing では「CLI 候補追加」こそが主要 action なので default expanded.
    # 既登録ありなら user の今の関心は別所にある想定で折りたたみ.
    expand_default = (len(registered_ids) == 0)

    with st.expander(title_label, expanded=expand_default):
        st.caption(
            f"CLI 一括検索結果からの自動候補. "
            f"(生成: {meta.get('generated_at', '?')})"
        )
        # 2026-05-12: slot 0 でも dataframe は描画する (user が候補を比較・評価できるよう).
        # 旧実装は早期 return で候補非表示 → 「20 件まで表示」requirement に反していた.
        # 登録ボタンのみ disabled、候補閲覧は可能.
        slot_full = (n_slots_left <= 0)
        if slot_full:
            st.warning(
                f"⚠️ 既登録 {len(registered_ids)} 件で上限. "
                f"新候補を登録するには既存 active 競合を先に削除してください. "
                f"(候補は閲覧のみ可能 ↓)"
            )

        # 候補 dataframe (checkbox 列付き).
        # 「⚠️ Economy」= 配送方法 (carrier) 軸の警告. 関税ポリシー (DDU/DDP) は別軸で「⚠️ DDU」.
        # 詳細: `reference_shipping_method_vs_ddu_taxonomy.md`.
        df_rows = []
        for c in new_candidates[:_DISPLAY_CANDIDATES]:  # 表示は最大 20 件 (登録は 10 件まで)
            legacy = c.get("legacy_item_id") or ""
            handling = _delivery_days_from_now(c.get("min_delivery_date"))
            handling_str = f"{handling} 日後" if handling is not None else "-"
            # 軸 1: 配送方法 (carrier) — shipping_service_code から判定
            svc_code = c.get("shipping_service_code")
            svc_type = c.get("shipping_type")
            is_economy = _is_economy_shipping(svc_code, svc_type)
            # 軸 2: 関税ポリシー (DDU/DDP) — Browse API getItem `taxes` field 由来 (独立判定)
            ddu_flag = c.get("is_ddu_policy")  # True/False/None
            if svc_code:
                svc_display = f"⚠️ {svc_code}" if is_economy else svc_code
            elif svc_type:
                svc_display = f"⚠️ {svc_type}" if is_economy else svc_type
            else:
                svc_display = "(未取得)"
            # 関税表示: DDU 確定なら "⚠️ DDU", DDP 確定なら "DDP", 不明は "?"
            if ddu_flag is True:
                duty_display = "⚠️ DDU"
            elif ddu_flag is False:
                duty_display = "DDP"
            else:
                duty_display = "?"
            df_rows.append({
                "✅": False,
                "item id": legacy,
                "リンク": f"https://www.ebay.com/itm/{legacy}" if legacy else "",
                "商品価格": c.get("price_usd"),
                "送料": c.get("shipping_cost_usd"),
                "合計": c.get("total_cost_usd"),
                "発送方法": svc_display,
                "関税": duty_display,
                "発送目安": handling_str,
                "title": (c.get("title") or "")[:60],
                "seller": c.get("seller") or "-",
            })
        # W150 (2026-05-22): form ラップで data_editor チェック変更時の rerun 抑制.
        # 旧実装は data_editor の "✅" 列を 1 件チェックする度に Streamlit が画面全体を
        # rerun → 並び順 / フィルタ再評価で編集中 listing が画面から消失していた
        # (user 報告 W150 ③). form_submit_button 押下まで rerun を抑制し編集状態維持.
        # selected count を表示する caption は form 内では update されないため、ボタン
        # disabled も削除 (submit 後の server-side 判定で 0 件 / 上限超過を弾く).
        with st.form(key=f"pm_cli_form_{eid}", clear_on_submit=False):
            df = pd.DataFrame(df_rows)
            edited_df = st.data_editor(
                df,
                column_config={
                    "✅": st.column_config.CheckboxColumn("追加", default=False, width="small"),
                    "item id": st.column_config.TextColumn("item id", width="small", disabled=True),
                    "リンク": st.column_config.LinkColumn(
                        "リンク", display_text="開く", width="small",
                    ),
                    "商品価格": st.column_config.NumberColumn("商品価格", format="$%.2f", width="small"),
                    "送料": st.column_config.NumberColumn("送料", format="$%.2f", width="small"),
                    "合計": st.column_config.NumberColumn("合計", format="$%.2f", width="small"),
                    "発送方法": st.column_config.TextColumn(
                        "発送方法", width="medium", disabled=True,
                        help="配送業者 (carrier) ⚠️ = SpeedPAK Economy / Surface mail 等 Economy 配送",
                    ),
                    "関税": st.column_config.TextColumn(
                        "関税", width="small", disabled=True,
                        help="関税ポリシー: ⚠️ DDU = buyer 負担 / DDP = seller 負担 / ? = 不明. "
                             "配送方法とは別軸の独立判定.",
                    ),
                    "発送目安": st.column_config.TextColumn("発送目安", width="small", disabled=True),
                    "title": st.column_config.TextColumn("title", width="medium", disabled=True),
                    "seller": st.column_config.TextColumn("seller", width="small", disabled=True),
                },
                hide_index=True,
                use_container_width=True,
                key=f"pm_cli_candidates_df_{eid}",
            )
            # 候補内の Economy / DDU の各軸独立カウントを警告表示
            n_economy = sum(
                1 for c in new_candidates[:_DISPLAY_CANDIDATES]
                if _is_economy_shipping(c.get("shipping_service_code"), c.get("shipping_type"))
            )
            n_ddu = sum(
                1 for c in new_candidates[:_DISPLAY_CANDIDATES]
                if c.get("is_ddu_policy") is True
            )
            if n_economy > 0:
                st.warning(
                    f"⚠️ 配送方法 (carrier) が **Economy 配送系**: **{n_economy} 件** "
                    f"(SpeedPAK Economy / Surface mail 等). 配送窓 10-14 日想定で defect リスク高. "
                    f"判定根拠: `feedback_competitor_jp_sellers_only.md` 「Economy 配送 seller は除外」."
                )
            if n_ddu > 0:
                st.warning(
                    f"⚠️ 関税ポリシーが **DDU (buyer 負担)**: **{n_ddu} 件**. "
                    f"buyer に追加関税請求が発生 → defect リスク. 配送方法軸とは独立の判定. "
                    f"判定根拠: `reference_shipping_method_vs_ddu_taxonomy.md`."
                )
            if n_economy == 0 and n_ddu == 0:
                st.caption("✅ 候補に Economy 配送 / DDU policy は検出されません.")

            st.caption(
                f"候補をチェックして下のボタンで一括追加. "
                f"残 slot {max(n_slots_left, 0)} / {_MAX_COMPETITORS}"
            )

            submitted = st.form_submit_button(
                "✅ 選択した候補を追加登録 (既存 maintain)",
                type="primary", use_container_width=True,
            )

        if submitted:
            # form_submit_button 押下後のみ実行. edited_df は最新 widget state.
            selected = [
                (row["item id"]) for _, row in edited_df.iterrows()
                if row.get("✅") and row.get("item id")
            ]
            if len(selected) == 0:
                st.warning("候補を 1 件以上選択してください.")
                return
            if len(selected) > n_slots_left:
                st.error(
                    f"❌ 上限 {_MAX_COMPETITORS} 件超過: 選択 {len(selected)} 件 + 既存 "
                    f"{len(registered_ids)} 件 > 残 slot {n_slots_left}. "
                    f"既存 active 競合を先に削除してください."
                )
                return
            # Plan B verify: 選択候補に Economy carrier OR DDU policy が含まれていたら登録 reject
            economy_ids = [
                c.get("legacy_item_id") for c in new_candidates[:_DISPLAY_CANDIDATES]
                if _is_economy_shipping(c.get("shipping_service_code"), c.get("shipping_type"))
                and c.get("legacy_item_id") in selected
            ]
            ddu_ids = [
                c.get("legacy_item_id") for c in new_candidates[:_DISPLAY_CANDIDATES]
                if c.get("is_ddu_policy") is True
                and c.get("legacy_item_id") in selected
            ]
            blocked = set(filter(None, economy_ids)) | set(filter(None, ddu_ids))
            if blocked:
                reasons = []
                if economy_ids:
                    reasons.append(f"Economy 配送 {len(set(filter(None, economy_ids)))} 件")
                if ddu_ids:
                    reasons.append(f"DDU policy {len(set(filter(None, ddu_ids)))} 件")
                st.error(
                    f"❌ 選択候補に除外対象が含まれます ({' / '.join(reasons)}): "
                    f"{', '.join(sorted(blocked))}. ⚠️ マークの候補チェックを外してから再実行してください."
                )
                return

            # 既存 + 新規 を merge してから upsert
            merged = registered_ids + selected
            seen: set = set()
            deduped: list[str] = []
            for cid in merged:
                if cid and cid not in seen:
                    seen.add(cid)
                    deduped.append(cid)
            deduped = deduped[:_MAX_COMPETITORS]
            try:
                upsert_listing_competitors(
                    our_item_id=eid, competitor_item_ids=deduped,
                )
                bump_db_version()  # W134 Step2: 競合更新後 read-cache 無効化
                st.success(
                    f"✅ {len(selected)} 件追加完了 (合計 {len(deduped)} 件 active)"
                )
                st.session_state.pop("pm_bulk_results_cache", None)
                st.rerun()
            except (sqlite3.OperationalError, ValueError, TypeError) as e:
                st.error(f"追加エラー: {e}")


def _render_new_alerts_for_listing(
    p: dict, config: dict, registered_ids: list[str]
) -> None:
    """新規発見ライバル (W184 alerts、この listing 向け候補)."""
    eid = p["ebay_item_id"]
    try:
        alerts = get_japan_competitor_alerts(action="pending")
    except (sqlite3.OperationalError, ValueError, KeyError) as e:
        logger.exception(f"[pm] alerts load failed eid={eid}")
        st.caption(f"alerts 読込エラー: {e}")
        return

    def _is_real_iid(iid: Optional[str]) -> bool:
        if not iid:
            return False
        return (not iid.startswith("synthetic_") and iid.isdigit()
                and 11 <= len(iid) <= 14)

    real_alerts = [a for a in alerts if _is_real_iid(a.get("found_item_id", ""))]
    registered_set = set(registered_ids)
    show_alerts = [
        a for a in real_alerts if a.get("found_item_id") not in registered_set
    ][:10]

    if not show_alerts:
        return

    with st.expander(f"新規発見ライバル alerts ({len(show_alerts)} 件)",
                     expanded=False):
        for alert in show_alerts:
            aid = alert["id"]
            iid = alert["found_item_id"]
            url = f"https://www.ebay.com/itm/{iid}"
            ship = alert.get("found_shipping")
            pr = alert.get("found_price") or 0
            row = st.columns([3, 1, 1, 1, 1])
            with row[0]:
                st.markdown(
                    f"[`{iid}`]({url}) / _{alert.get('found_seller', '-')}_"
                )
            with row[1]:
                st.markdown(f"${pr:.2f}")
            with row[2]:
                if ship is None:
                    if st.button("送料取得", key=f"pm_alert_fetch_{eid}_{aid}"):
                        try:
                            f = fetch_alert_shipping_usd(aid, config or {})
                            if f is None:
                                st.error("送料取得失敗")
                            else:
                                st.rerun()
                        except (sqlite3.OperationalError, ValueError, KeyError, RuntimeError) as e:
                            logger.exception(
                                f"[pm] alert shipping fetch failed aid={aid}"
                            )
                            st.error(f"送料取得エラー: {e}")
                else:
                    st.markdown(f"+${float(ship):.2f}")
            with row[3]:
                if st.button("追加", key=f"pm_alert_add_{eid}_{aid}",
                             type="primary"):
                    try:
                        if len(registered_ids) >= _MAX_COMPETITORS:
                            st.error(f"上限 {_MAX_COMPETITORS} 件")
                        else:
                            upsert_listing_competitors(
                                our_item_id=eid,
                                competitor_item_ids=registered_ids + [iid],
                            )
                            update_alert_action(aid, "registered")
                            bump_db_version()  # W134 Step2: 競合更新後 read-cache 無効化
                            st.success(f"追加: {iid}")
                            st.rerun()
                    except (sqlite3.OperationalError, ValueError, TypeError) as e:
                        logger.exception(
                            f"[pm] alert add failed aid={aid} iid={iid}"
                        )
                        st.error(f"追加エラー: {e}")
            with row[4]:
                if st.button("無視", key=f"pm_alert_ignore_{eid}_{aid}"):
                    try:
                        update_alert_action(aid, "ignored")
                        st.rerun()
                    except (sqlite3.OperationalError, ValueError) as e:
                        logger.exception(
                            f"[pm] alert ignore failed aid={aid}"
                        )
                        st.error(f"無視処理エラー: {e}")


# =============================================================================
# Save
# =============================================================================

def _save_product_data(
    *,
    ebay_item_id: str,
    editing: dict,
    competitors: list[str],
    recalc_breakeven: bool,
    config: dict,
) -> None:
    """編集内容を DB に保存. breakeven 自動再計算 (optional)."""
    # SKU 編集 (空文字は許可しない、変更時のみ)。
    # W139-fix HIGH-1 (Codex 2026-05-19): 直接 `UPDATE ebay_listings SET sku=?`
    # は source_url 再構築 + monitored_items 追従 (_sync_monitored_items_sku)
    # を bypass する。商品管理から SKU を編集すると監視台帳が旧 sku/source_url
    # に取り残され、新 find_coverage_gaps が ebay_item_id 一致で「監視あり」と
    # 誤判定 → silent gap (仕入先OOS見逃し→履行不能)。update_ebay_listing_sku
    # に統一 (自前 conn で commit。後続 with ブロックと逐次実行 = WAL 単一
    # writer のロック競合を回避)。
    # W139-fix HIGH (Codex round-2 2026-05-19): editing["sku"] は SKU 入力欄の
    # 値で **未変更でも常に現在 sku が渡る** (L686 value=current_sku)。無条件で
    # update_ebay_listing_sku を呼ぶと weight/寸法だけの保存でも
    # source_status='unknown'/source_out_of_stock_since=NULL/risk_confirmed=0 が
    # リセットされ、既知の仕入先 OOS リスクが supplier_sweep/UI から消失 →
    # 履行不能。**実際に SKU が変わった時のみ** update_ebay_listing_sku を
    # 呼ぶ (現行 ebay_listings.sku と比較)。未変更なら no-op = 原 raw UPDATE
    # (同値書込で無害) と同じ作用 + OOS リスク state を保全。
    new_sku = editing.get("sku")
    if new_sku and new_sku.strip():
        new_sku = new_sku.strip()
        with get_conn() as conn:
            _cur = conn.execute(
                "SELECT sku FROM ebay_listings WHERE ebay_item_id=?",
                (ebay_item_id,),
            ).fetchone()
        _cur_sku = (_cur[0] if _cur else None) or ""
        if new_sku != _cur_sku:
            from monitor.database import update_ebay_listing_sku
            update_ebay_listing_sku(ebay_item_id, new_sku)

    # 区分 (primary_market) — 2026-06-01 編集可化。dirty 時のみ DB 保存
    # (None→unknown の stale write を遮断 = BP / 送料 dirty-flag と同型)。
    # update_ebay_listing_sku と同様 with ブロック外で自前 conn を使い WAL
    # 単一 writer のロック競合を回避。eBay 送料反映は別経路 (user 判断で 📤)。
    _mkt = editing.get("primary_market")
    _mkt_init = editing.get("primary_market_render_initial")
    if _mkt and _mkt != _mkt_init:
        from monitor.database import update_ebay_listing_primary_market
        update_ebay_listing_primary_market(ebay_item_id, _mkt)
        st.success(f"🌐 区分を {_mkt} に更新しました (利益は再表示で再計算)")

    with get_conn() as conn:
        # inventory_count (None 渡しでも触らない、明示的に値があれば UPDATE)
        inv = editing.get("inventory_count")
        if inv is not None:
            conn.execute(
                "UPDATE ebay_listings SET inventory_count=? WHERE ebay_item_id=?",
                (int(inv), ebay_item_id),
            )
            _inv_changed = True
        else:
            _inv_changed = False
        if editing.get("weight_g") is not None:
            conn.execute(
                "UPDATE ebay_listings SET weight_g=?, weight_source='manual_edit', "
                "weight_estimated_at=datetime('now') WHERE ebay_item_id=?",
                (int(editing["weight_g"]), ebay_item_id),
            )
        for col in ("length_cm", "width_cm", "height_cm"):
            v = editing.get(col)
            if v is not None:
                conn.execute(
                    f"UPDATE ebay_listings SET {col}=? WHERE ebay_item_id=?",
                    (float(v), ebay_item_id),
                )

    # W133 (2026-05-16): 在庫数を手動編集したら eBay 出品数量へ反映.
    # listing 識別は ebay_item_id (SKU 不使用). 失敗はメッセージ表示のみで
    # DB は維持 (Q0: 失敗を握り潰さず st.warning + qty_sync_error 列に痕跡).
    if _inv_changed:
        from monitor import inventory_sync
        _sync = inventory_sync.sync_listing_quantity(ebay_item_id)
        if not _sync.get("success"):
            if _sync.get("skipped_zero_unsafe"):
                st.warning(
                    "在庫0 ですが eBay 数量反映を抑止しました "
                    "(Out-of-Stock Control 未確認 = listing 自動 End 防止)。"
                    f" {_sync.get('message') or ''}"
                )
            else:
                st.warning(
                    "在庫数は DB 保存しましたが eBay 数量反映に失敗しました: "
                    f"{_sync.get('message') or '不明'}"
                )

    # W205 (2026-05-31): 無在庫 (ebay* SKU) の eBay 出品数量を手動編集 → eBay 即反映.
    # listing 識別は ebay_item_id (SKU 不使用)。inventory_count を持たないので
    # explicit_quantity 経路で UI 入力値を直接 eBay へ送る。quantity_ebay 列・痕跡層
    # (last_qty_sync_at/last_synced_quantity/qty_sync_error) は sync_listing_quantity
    # 内で一元更新。成功時のみ「反映済」、失敗は握り潰さず st.warning + qty_sync_error
    # に痕跡 (Q0)。現在の eBay 数量と異なる時のみ反映 (無駄な API call / 同値書込回避)。
    _qty_manual = editing.get("quantity_ebay_manual")
    if _qty_manual is not None:
        with get_conn() as conn:
            _qrow = conn.execute(
                "SELECT quantity_ebay FROM ebay_listings WHERE ebay_item_id=?",
                (ebay_item_id,),
            ).fetchone()
        _cur_qty = (_qrow[0] if _qrow else None)
        if _cur_qty is None or int(_cur_qty) != int(_qty_manual):
            from monitor import inventory_sync
            _qsync = inventory_sync.sync_listing_quantity(
                ebay_item_id, explicit_quantity=int(_qty_manual)
            )
            if _qsync.get("success"):
                st.success(
                    f"📤 eBay 出品数量を {int(_qty_manual)} に反映しました "
                    "(GetItem で実反映の最終 verify 推奨)"
                )
            elif _qsync.get("skipped_zero_unsafe"):
                st.warning(
                    "数量0 ですが eBay 反映を抑止しました "
                    "(Out-of-Stock Control 未確認 = listing 自動 End 防止)。"
                    f" {_qsync.get('message') or ''}"
                )
            else:
                st.warning(
                    "eBay 出品数量の反映に失敗しました: "
                    f"{_qsync.get('message') or '不明'}"
                )

    pyen = editing.get("purchase_yen")
    minp = editing.get("lp_min_price")
    if pyen is not None or minp is not None:
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT purchase_yen, lp_min_price FROM ebay_listings WHERE ebay_item_id=?",
                (ebay_item_id,),
            ).fetchone()
        epyen = existing[0] if existing else None
        eminp = existing[1] if existing else None
        merged_pyen = float(pyen) if pyen is not None else (float(epyen) if epyen is not None else None)
        merged_minp = float(minp) if minp is not None else (float(eminp) if eminp is not None else None)
        set_listing_lowest_price_fields(
            ebay_item_id=ebay_item_id,
            purchase_yen=merged_pyen,
            lp_min_price=merged_minp,
        )

    try:
        upsert_listing_competitors(
            our_item_id=ebay_item_id, competitor_item_ids=competitors
        )
    except (sqlite3.OperationalError, ValueError, TypeError) as e:
        logger.warning(f"[pm_save] competitor upsert error: {e}")

    if recalc_breakeven:
        try:
            update_listing_breakeven(ebay_item_id, config or {})
        except (sqlite3.OperationalError, TypeError, ValueError, KeyError) as e:
            logger.warning(f"[pm_save] breakeven recalc error: {e}")

    # W140: listing メモを change-guard 保存 (実変更時のみ書込 = W139-fix 流儀。
    # 未変更で upsert すると updated_at だけ動く無害書込だが、無駄を避け
    # 変更時のみ)。eBay 非送信・MonoDeck DB のみ・ebay_item_id 紐付。
    _note = editing.get("note_text")
    if _note is not None:
        _new_note = _note.strip()
        _cur_note = (get_listing_note(ebay_item_id) or "").strip()
        if _new_note != _cur_note:
            upsert_listing_note(ebay_item_id, _new_note)

    # W220 (2026-06-04): ポイント実額(¥)。値ありで UPDATE (None=未入力は触らない)。
    # 採算パネルのポイント還元表示 (money-direct 判断材料) に効く。同値書込は無害。
    _pt = editing.get("point_yen")
    if _pt is not None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE ebay_listings SET point_yen=? WHERE ebay_item_id=?",
                (float(_pt), ebay_item_id),
            )

    # W227 (2026-06-06 根治): 商品状態ランクは **condition_rank** 列へ保存する
    # (人気度 rank 列には書かない = 二重使用解消。事故根治)。change-guard (実変更
    # 時のみ。None=「未設定」sentinel は触らない)。eBay Condition 反映は 📤eBay反映
    # (本 DB 保存は user の状態意図を condition_rank に記録するのみ)。
    _rank = editing.get("rank")
    if _rank is not None:
        with get_conn() as conn:
            _cr = conn.execute(
                "SELECT condition_rank FROM ebay_listings WHERE ebay_item_id=?",
                (ebay_item_id,),
            ).fetchone()
        if (_cr[0] if _cr else None) != _rank:
            from monitor.database import update_ebay_listing_condition
            update_ebay_listing_condition(ebay_item_id, condition_rank=_rank)

    # W220: description 下書き。change-guard (note と同型)。eBay 非送信 (slice3 で送信)。
    _desc = editing.get("listing_description")
    if _desc is not None:
        with get_conn() as conn:
            _cd = conn.execute(
                "SELECT listing_description FROM ebay_listings WHERE ebay_item_id=?",
                (ebay_item_id,),
            ).fetchone()
        if ((_cd[0] if _cd else None) or "") != _desc:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE ebay_listings SET listing_description=? WHERE ebay_item_id=?",
                    (_desc, ebay_item_id),
                )

    # W134 Step2: 全書込完了後に read-cache 無効化 (商品管理一覧へ即時反映)
    bump_db_version()


# =============================================================================
# Expander header
# =============================================================================

def _build_expander_header(p: dict) -> str:
    """1 行 expander タイトル. M3 fix で重要 3 項目のみに圧縮.

    [在庫 icon] タイトル | 総額 | 粗利
    `$` は markdown LaTeX 化を防ぐため全て `\\$` でエスケープ.
    """
    _, _, total = _total_price(p)
    title = (p.get("title") or "")[:60]
    src = _status_emoji(p.get("source_status"))

    profit = _estimate_profit_usd(p)
    profit_str = ""
    if profit is not None:
        sign = "+" if profit >= 0 else ""
        # 「\$」escape で KaTeX 数式化を回避.
        profit_str = f" | 粗利 {sign}\\${profit:.0f}"

    # W140: メモ付き listing は 📎 (発送/通関の注意点あり = 売れたら警告対象)。
    note_mk = "📎" if p.get("has_note") else ""

    return f"{src}{note_mk} {title} | \\${total:.2f}{profit_str}"


# =============================================================================
# W153 (2026-05-22): ライバル監視 section
# =============================================================================

_W153_UI_COOLDOWN_SEC = 60.0  # H-H: 「今すぐ検索」連打防止


# =============================================================================
# W217 (2026-06-03): 純関数 helpers (unit test 可、Streamlit 非依存)
# =============================================================================

def _kw_state_badge(eid: str, db_kw: str, session_state) -> str:
    """ライバル監視「検索ワード」状態バッジ HTML 文字列を返す.

    session_state[`pm_rival_kw_{eid}`] (UI 入力値) と db_kw (DB 保存値) を
    比較し、3 状態のバッジ HTML を返す (render せず文字列返却 = unit test 可、
    保存ロジックは読まない・比較のみ).

    状態:
      - 🟢 cron 反映済 (一致 or UI 未編集) — DB 値が cron に渡る
      - 🟡 未保存 (UI 編集中で不一致) — 保存ボタンで cron 反映
      - ⚪ 未設定 (両方空)

    比較は空白 normalize 済の strip 一致で実施 (legacy \n 区切り data の
    UI 表示時 normalize と整合)。session_state は dict 互換のため plain
    dict で単体検証可。
    """
    import re as _re
    def _norm(s):
        return _re.sub(r"\s+", " ", (s or "")).strip()
    db_norm = _norm(db_kw)
    ui_key = f"pm_rival_kw_{eid}"
    if ui_key in session_state:
        ui_norm = _norm(session_state.get(ui_key) or "")
        if not db_norm and not ui_norm:
            return ('<span class="pm-kw-badge pm-kw-badge-idle">'
                    '⚪ 未設定</span>')
        if ui_norm == db_norm:
            return ('<span class="pm-kw-badge pm-kw-badge-ok">'
                    '🟢 cron 反映済 (保存値=これ)</span>')
        return ('<span class="pm-kw-badge pm-kw-badge-warn">'
                '🟡 未保存 (保存で cron 反映)</span>')
    # UI 未描画 = DB 値そのまま
    if not db_norm:
        return ('<span class="pm-kw-badge pm-kw-badge-idle">'
                '⚪ 未設定</span>')
    return ('<span class="pm-kw-badge pm-kw-badge-ok">'
            '🟢 cron 反映済 (保存値=これ)</span>')


def _lowest_rival_marker(rows: list) -> list:
    """合計 (total) 列が最小の行に 🥇 を付与した行リストを返す (純関数).

    - rows: dict のリスト。各 dict は最低限 "合計" key を持つ
            (NumberColumn 用 float / None)
    - 戻り値: 同じ shape のリスト。最安行の "item id" key 先頭に "🥇 " を付与
            (Streamlit dataframe は行背景色不可なので絵文字付与で代替).
    - None 安全: "合計" が None / 欠損の行は最安対象外、全行 None なら何も付与しない
    """
    if not rows:
        return rows
    # 合計が数値である行のみ評価
    valid = [(i, r) for i, r in enumerate(rows)
             if r.get("合計") is not None]
    if not valid:
        return rows
    min_i, _ = min(valid, key=lambda x: x[1]["合計"])
    out = []
    for i, r in enumerate(rows):
        if i == min_i:
            new_r = dict(r)
            iid = str(r.get("item id") or "")
            if not iid.startswith("🥇"):
                new_r["item id"] = f"🥇 {iid}"
            out.append(new_r)
        else:
            out.append(r)
    return out


def _competitor_gap_line(total_usd: float, competitor_min) -> str:
    """自社総額 vs 競合最安 → 競合差の 1 行 HTML を返す (採算パネルと同じ色ロジック).

    - total_usd: 自社 buyer 総額 (商品価格 + 送料)
    - competitor_min: 競合最安 (None なら空文字列を返す = render しない)
    - 戻り値: HTML 1 行。自社 > 競合 = 🔴 劣位 (赤背景) / 自社 < 競合 = 🟢 有利
            (緑背景) / 同 = ⚪ (中立)。
    色ロジックは _render_hero_metrics の採算パネル (L854-) と同じ.
    """
    if competitor_min is None:
        return ""
    try:
        cmin = float(competitor_min)
    except (TypeError, ValueError):
        return ""
    diff = float(total_usd) - cmin
    if diff > 0:
        # 自社が高い = 競合劣位
        cls = "pm-gap-line pm-gap-line-bad"
        diff_label = f"競合差 +${diff:.2f} 🔴 劣位"
        diff_color = "#FECACA"  # red-200
    elif diff < 0:
        cls = "pm-gap-line pm-gap-line-ok"
        diff_label = f"競合差 ${diff:.2f} 🟢 有利"
        diff_color = "#A7F3D0"  # emerald-200
    else:
        cls = "pm-gap-line pm-gap-line-eq"
        diff_label = "競合差 $0.00 ⚪ 同"
        diff_color = "#E5E7EB"
    return (
        f'<div class="{cls}">'
        f'<span>自社 買い手総額 <b>${total_usd:.2f}</b> '
        f'vs 競合最安 <b>${cmin:.2f}</b></span>'
        f'<span style="color:{diff_color};font-weight:700">{diff_label}</span>'
        f'</div>'
    )


def _render_rival_table_html(rows: list) -> str:
    """登録済 active ライバル dataframe を HTML table 文字列にする (純関数).

    W217-A (2026-06-03): モックアップに合わせ、st.dataframe (canvas 描画で
    行背景色不可) を HTML <table> に置換。最安行 (item id が "🥇 " で始まる)
    に CSS class "pm-rival-best" を付与し、行背景緑 + 合計緑文字。

    入力 rows: dict のリスト (`_lowest_rival_marker` の戻り値と同形)。
      期待 key: "item id" (🥇 prefix 有りも有り得る) / "リンク" (URL str) /
      "商品価格" (float|None) / "送料" (float|None) / "合計" (float|None) /
      "発送目安" (str) / "最終取得" (str)
    リンク列が空文字列なら "—" 表記。

    Returns:
        HTML table 文字列 (st.markdown unsafe_allow_html=True で描画)。
        rows が空なら空文字列を返す。
    """
    if not rows:
        return ""
    parts: list[str] = []
    parts.append('<table class="pm-rival-tbl">')
    parts.append(
        '<thead><tr>'
        '<th class="pm-rival-th-left">item id</th>'
        '<th>価格</th><th>送料</th><th>合計</th>'
        '<th>発送</th><th>取得</th><th class="pm-rival-th-link">リンク</th>'
        '</tr></thead><tbody>'
    )
    for r in rows:
        iid = str(r.get("item id") or "")
        is_best = iid.startswith("🥇")
        row_cls = "pm-rival-best" if is_best else ""
        tot = r.get("合計")
        tot_cls = "pm-rival-tot-ok" if is_best else ""
        # 価格 / 送料 / 合計 (None safe)
        def _fmt_usd(v):
            if v is None:
                return "—"
            try:
                return f"${float(v):.2f}"
            except (TypeError, ValueError):
                return "—"
        price_str = _fmt_usd(r.get("商品価格"))
        ship_str = _fmt_usd(r.get("送料"))
        tot_str = _fmt_usd(tot)
        ship_disp = str(r.get("発送目安") or "—")
        last_disp = str(r.get("最終取得") or "—")
        # リンク (st.column_config.LinkColumn 等価: display_text="開く")。
        link = str(r.get("リンク") or "")
        if link:
            link_cell = (
                f'<a href="{html.escape(link)}" target="_blank" '
                f'rel="noopener noreferrer">開く</a>'
            )
        else:
            link_cell = "—"
        # item id は 🥇 prefix を含む可能性あり (HTML escape する必要なし、
        # 純数字 + 絵文字 prefix のみ。安全側で escape する)。
        iid_esc = html.escape(iid)
        parts.append(
            f'<tr class="{row_cls}">'
            f'<td class="pm-rival-td-iid">{iid_esc}</td>'
            f'<td>{price_str}</td>'
            f'<td>{ship_str}</td>'
            f'<td class="{tot_cls}">{tot_str}</td>'
            f'<td>{html.escape(ship_disp)}</td>'
            f'<td>{html.escape(last_disp)}</td>'
            f'<td class="pm-rival-td-link">{link_cell}</td>'
            f'</tr>'
        )
    parts.append('</tbody></table>')
    return "".join(parts)


# v51 (2026-05-22 PM): Economy 系発送方法 (中国大量出品 noise) を UI で hide.
# 業務知識: reference_ebay_economy_shipping_seller_pattern.md
# 検索段階 skip ではなく UI hide にする = 同 seller の高 value listing は別 listing として残る.
_W153_UI_ECONOMY_THRESHOLD_DAYS = 10


def _is_economy_for_display(d: dict) -> bool:
    """Economy 系 shipping と判定し UI 上 hide する.

    判定優先順 (OR 結合):
      1. shipping_service_code に "Economy" 含む (v52、name 判定)
      2. 配達日数 (max-min) > 10 日 (v51 fallback、name 不在時)

    seller_id ベースの block list は誤り (同 seller が安商品では Economy、
    高商品では別発送方法を使うため). listing 単位で発送方法名で判定.

    Returns:
        True: Economy 系 (UI hide 対象).
        False: Express / Standard / 情報なし (表示する).
    """
    # (1) v52: shipping_service_code name で判定 (最も確実)
    service_code = (d.get("shipping_service_code") or "")
    if "Economy" in service_code:
        return True
    # (2) v51: 配達日数 fallback
    min_date = d.get("min_delivery_date")
    max_date = d.get("max_delivery_date")
    if not (min_date and max_date):
        return False  # 情報なし = 表示する (安全側、判断は user に委ねる)
    try:
        min_dt = datetime.fromisoformat(min_date.replace("Z", "+00:00"))
        max_dt = datetime.fromisoformat(max_date.replace("Z", "+00:00"))
        window_days = (max_dt - min_dt).total_seconds() / 86400
        return window_days > _W153_UI_ECONOMY_THRESHOLD_DAYS
    except Exception:
        return False  # parse 失敗 = 表示する (安全側)


def _format_shipping_method(d: dict) -> str:
    """発送方法名を取得 (v52 shipping_service_code 優先).

    例: "USPS Priority" / "FedEx International" / "—" (情報なし)
    """
    code = (d.get("shipping_service_code") or "").strip()
    return code if code else "—"


def _format_delivery_window(d: dict) -> str:
    """配達日数 string 整形 (発送方法名の補足).

    例: "7-12 days" / "—"
    """
    min_date = d.get("min_delivery_date")
    max_date = d.get("max_delivery_date")
    if not (min_date and max_date):
        return "—"
    try:
        from datetime import datetime as _dt
        now = _dt.now(tz=None).replace(tzinfo=None)
        min_dt = _dt.fromisoformat(min_date.replace("Z", "+00:00")).replace(tzinfo=None)
        max_dt = _dt.fromisoformat(max_date.replace("Z", "+00:00")).replace(tzinfo=None)
        min_days = max(0, int((min_dt - now).total_seconds() / 86400))
        max_days = max(0, int((max_dt - now).total_seconds() / 86400))
        return f"{min_days}-{max_days} days"
    except Exception:
        return "—"


def _render_rival_watch_section(p: dict, config: dict) -> None:
    """W153: 商品 hero 内「🎯 ライバル監視」section. form 外で個別 button 即時反応.

    設計書: .company/engineering/docs/2026-05-22-W153-rival-per-listing-detection-design.md (v2.1)
    """
    import time as _time

    eid = p["ebay_item_id"]
    # H-A 確定式: rival_watch_started_at 優先、None なら initial_at fallback
    since_base = p.get("rival_watch_started_at") or p.get("initial_registered_at")

    st.markdown(
        '<div class="pm-section-label">🎯 ライバル監視 (W153)</div>',
        unsafe_allow_html=True,
    )

    # ── ① 監視 ON checkbox ──
    cur_on = bool(p.get("rival_watch_enabled"))
    new_on = st.checkbox(
        "ライバル監視 ON (cron 巡回対象に含める)",
        value=cur_on, key=f"pm_rival_on_{eid}",
    )
    if new_on != cur_on:
        set_rival_watch_enabled(eid, new_on)
        st.rerun()

    if not new_on:
        st.caption("OFF: このセクションでの編集・検索は無効")
        st.markdown("---")
        return  # K1 simplicity

    # ── ② text_input + ③ 生成 + ④ 保存 + ⑤ 今すぐ検索 ──
    # M-internal-1: session_state 上書きは *_pending key 経由
    pending_key = f"pm_rival_kw_{eid}_pending"
    if pending_key in st.session_state:
        initial_kw = st.session_state.pop(pending_key)
    else:
        # v2 (2026-05-22 PM): legacy \n 区切り data を UI 表示時に空白 normalize.
        # Streamlit text_input は \n を value に含めると削除して結合してしまうため、
        # 事前に空白化しないと user に「maxellMXCP-P100Black」のような連結文字列が
        # 表示される (legacy data UX bug 修正).
        import re as _re_ui_load
        raw_kw = p.get("rival_search_keywords") or ""
        initial_kw = _re_ui_load.sub(r"\s+", " ", raw_kw).strip()

    gen_at = p.get("rival_search_keywords_generated_at") or "未生成"
    st.caption(f"検索ワード (空白区切り、Browse API に AND 検索で 1 query 投入。最終生成: {gen_at} UTC)")

    if not since_base:
        st.caption(
            "⚠️ W151 初期登録未完了 = since filter は rival_watch_started_at のみで動作"
        )

    new_kw = st.text_input(
        "検索ワード",
        value=initial_kw,
        key=f"pm_rival_kw_{eid}",
        label_visibility="collapsed",
        help="空白区切り 1 query (3-8 word). 例: 'maxell MXCP-P100 Cassette Player'. brand + model + 商品カテゴリ語を含めると精度向上.",
    )

    # W217 (2026-06-03): 検索ワード状態バッジ (🟢 cron 反映済 / 🟡 未保存 / ⚪ 未設定).
    # DB 値 (= cron が読む値) と UI 編集値の同期状態を色付き badge で明示。
    # 純関数 _kw_state_badge で render せず文字列を組み立て (unit test 可、
    # 保存ロジックは読まない・比較のみ)。
    _db_kw_for_badge = p.get("rival_search_keywords") or ""
    _badge_html = _kw_state_badge(eid, _db_kw_for_badge, st.session_state)
    if _badge_html:
        st.markdown(
            f'{_badge_html} '
            f'<span style="font-size:11px;color:var(--pm-text-dim);'
            f'margin-left:8px">最終生成: {gen_at} UTC</span>',
            unsafe_allow_html=True,
        )

    # W217 (2026-06-03): ボタン優先度を [今すぐ検索 > 保存 > 生成] へ.
    # モックアップ判断: 主アクション「今すぐ検索」を 2 倍幅 (primary) で
    # 大きく配置、副次的な「保存」「生成」は等幅で控えめに。各ハンドラ本体は
    # 不変、widget 順序と column 比率のみ変更 (K2 surgical)。
    btn_cols = st.columns([2, 1, 1])
    with btn_cols[0]:
        # H-H: UI cooldown 60s (v2.1 MED-5 admit: single-process limit)
        cooldown_key = f"pm_rival_search_at_{eid}"
        last_at = st.session_state.get(cooldown_key, 0.0)
        now_s = _time.monotonic()
        on_cooldown = (now_s - last_at) < _W153_UI_COOLDOWN_SEC
        if st.button(
            "🔍 今すぐ検索",
            key=f"pm_rival_search_btn_{eid}",
            type="primary",
            disabled=on_cooldown,
            help="この listing を今すぐ Browse API 巡回 (60 秒 cooldown)",
        ):
            # v2: 改行混じり入力も normalize して 1 query AND 検索
            import re as _re_ui
            q = _re_ui.sub(r"\s+", " ", new_kw or "").strip()
            if not q:
                st.warning("検索ワードが空です")
            elif len(q.split(" ")) < 2:
                st.warning(
                    f"検索ワードが短すぎます (1 word では AND 検索が成立せず noise 過多)。"
                    f"3-8 word 推奨。現在: {q!r}"
                )
            else:
                st.session_state[cooldown_key] = now_s
                with st.spinner("Browse API 巡回中..."):
                    from monitor.database import get_self_ebay_item_ids
                    from tasks.task_rival_detection import (
                        run_rival_per_listing_detection_one,
                    )
                    res = run_rival_per_listing_detection_one(
                        eid, config,
                        query_override=q,
                        sleep_between=0.0,  # M-internal-7: UI 経路 0
                        self_item_ids=get_self_ebay_item_ids(),  # W308
                    )
                if res["success"]:
                    st.success(
                        f"新規 {res['new_discoveries']} / "
                        f"既知更新 {res['refreshed']} / "
                        f"err {res['errors']} / "
                        f"bad_iid {res['skipped_bad_item_id']}"
                    )
                else:
                    st.error(f"失敗: {res['message']}")
                st.rerun()
        if on_cooldown:
            remaining = int(_W153_UI_COOLDOWN_SEC - (now_s - last_at))
            st.caption(f"cooldown {remaining}s")
    with btn_cols[1]:
        if st.button(
            "💾 検索ワード保存", key=f"pm_rival_save_{eid}",
            help="text_input 内容を DB 保存 (空白区切り 2 word 以上必須)",
        ):
            # HIGH-1 fix (2026-05-22 PM internal review): 保存経路にも 1-word guard
            # (今すぐ検索だけでなく cron も同じ DB 値を読むため、保存時点で止める).
            import re as _re_save
            q_save = _re_save.sub(r"\s+", " ", new_kw or "").strip()
            if not q_save:
                # 空文字 = 削除目的、DB layer の set_rival_search_keywords が許可する
                ok = set_rival_search_keywords(eid, "", mark_generated=False)
                if ok:
                    st.success("検索ワード削除完了 (空文字保存)")
                else:
                    st.error("保存失敗 (listing 不在?)")
            elif len(q_save.split(" ")) < 2:
                st.warning(
                    f"検索ワードが短すぎます (1 word では AND 検索が成立せず noise 過多)。"
                    f"3-8 word 推奨。現在: {q_save!r} — 保存をキャンセルしました"
                )
            else:
                ok = set_rival_search_keywords(eid, new_kw, mark_generated=False)
                if ok:
                    st.success("保存完了")
                else:
                    st.error("保存失敗 (listing 不在 or DB 拒否)")

    with btn_cols[2]:
        if st.button(
            "🤖 Claude 生成", key=f"pm_rival_gen_{eid}",
            help="Claude Haiku で 1 best query (3-8 word) を生成 (text_input を上書き)",
        ):
            with st.spinner("Haiku 生成中..."):
                try:
                    from monitor.rival_keyword_generator import generate_keywords
                    cand = generate_keywords(title=p.get("title") or "")
                    # Codex LOW (2026-05-22 PM): set_rival_search_keywords 戻り値を check
                    # (現状 generator は 3-8 word 保証だが、defense in depth で
                    # listing 不在 / 将来 stricter guard rejection も検出).
                    ok = set_rival_search_keywords(eid, cand, mark_generated=True)
                    if ok:
                        st.session_state[pending_key] = cand  # M-internal-1
                        st.success(f"生成 → DB 保存: {cand}")
                        st.rerun()
                    else:
                        st.error(
                            f"生成 query が DB layer に拒否されました: {cand!r}. "
                            f"(listing 不在 or 1-word). 手動入力してください."
                        )
                except ValueError as e:
                    st.error(f"Haiku 出力異常: {e}. 手動入力してください")
                except RuntimeError as e:
                    st.error(f"API key 未設定: {e}")
                except Exception as e:
                    st.error(f"生成失敗: {type(e).__name__}: {e}")

    # ── ⑥ 検出済 expander (status 3 tab + 件数バッジ L-internal-4) ──
    # v51 (2026-05-22 PM): Economy 系を UI hide + 送料/配達日数表示 + 一括却下.
    raw_new = get_rival_discoveries(eid, status='new', since=since_base)
    visible_new = [
        d for d in raw_new if not _is_economy_for_display(d)
    ]
    hidden_eco_count = len(raw_new) - len(visible_new)
    new_count_label = len(visible_new)

    with st.expander(
        f"📋 検出済 rival 一覧 ({new_count_label} 新規)", expanded=False,
    ):
        tab_new, tab_added, tab_dismissed = st.tabs(
            ["🆕 新規 (未対応)", "✅ 監視追加済", "🗑️ 却下"],
        )
        with tab_new:
            if hidden_eco_count > 0:
                st.caption(
                    f"⚙️ Economy 系 {hidden_eco_count} 件を hide 中 "
                    f"(配達日数 > 10 日 = 中国大量出品 noise)"
                )
            if not visible_new:
                st.caption(
                    "新規 rival なし"
                    + (f" (since {since_base} UTC)" if since_base else "")
                )
            else:
                # 上限 50 件 (UI 描画パフォーマンス)
                display_list = visible_new[:50]

                # ── 一括処理 form (☑ = 監視追加対象、未入力 = 却下) ──
                # v52 (2026-05-22 PM): UI 逆転.
                # user 業務: 50 件のうち真の rival は 1-3 件 = 登録分を ☑ で選び、
                # submit 1 回で「☑ → W183 監視追加 / 未 ☑ → 却下」を一括処理.
                with st.form(key=f"pm_rdisc_form_{eid}"):
                    st.caption(
                        "✅ **監視追加したい rival** を ☑ で選択。"
                        "未チェックの rival は 自動で却下されます。"
                    )
                    for d in display_list:
                        cols = st.columns([0.3, 5])
                        with cols[0]:
                            st.checkbox(
                                " ", key=f"pm_rdisc_chk_{d['id']}",
                                label_visibility="collapsed",
                            )
                        with cols[1]:
                            # 商品価格
                            price_val = d.get('competitor_price_usd')
                            price_str = (
                                f"${price_val:.2f}"
                                if price_val is not None else "—"
                            )
                            # 送料
                            ship_val = d.get('competitor_shipping_cost_usd')
                            ship_str = (
                                f"${ship_val:.2f}"
                                if ship_val is not None else "—"
                            )
                            # 合計
                            if price_val is not None and ship_val is not None:
                                total_str = f"${price_val + ship_val:.2f}"
                            elif price_val is not None:
                                total_str = f"${price_val:.2f} (+送料不明)"
                            else:
                                total_str = "—"
                            # 発送方法名 (v52)
                            method_str = _format_shipping_method(d)
                            delivery_str = _format_delivery_window(d)

                            st.markdown(
                                f"**{d['competitor_seller']}** | "
                                f"[{(d['competitor_title'] or '')[:60]}]"
                                f"(https://www.ebay.com/itm/{d['competitor_item_id']})"
                            )
                            st.caption(
                                f"💰 価格 **{price_str}** + 送料 **{ship_str}** "
                                f"= 合計 **{total_str}** | "
                                f"📦 {method_str} ({delivery_str}) | "
                                f"first_seen: {d['first_seen_at']} UTC | "
                                f"keyword: {d['search_keyword']}"
                            )
                    submitted = st.form_submit_button(
                        f"✅ 選択を全登録 (未チェックは全却下)",
                        type="primary",
                    )

                if submitted:
                    add_ids = []
                    dismiss_ids = []
                    for d in display_list:
                        if st.session_state.get(
                            f"pm_rdisc_chk_{d['id']}", False,
                        ):
                            add_ids.append(d)
                        else:
                            dismiss_ids.append(d['id'])

                    # 監視追加 (status=monitoring_added) — 1 件ずつ DB 操作
                    added_count = 0
                    conflict_count = 0
                    error_count = 0
                    for d in add_ids:
                        try:
                            _id, action = add_or_reactivate_competitor(
                                our_item_id=eid,
                                our_sku=p.get('sku', '') or '',
                                competitor_seller=d['competitor_seller'],
                                competitor_item_id=d['competitor_item_id'],
                            )
                            if action in ('added', 'reactivated'):
                                update_rival_discovery_status(
                                    d['id'], 'monitoring_added',
                                )
                                added_count += 1
                            else:  # conflict
                                conflict_count += 1
                        except Exception as e:
                            logger.warning(
                                f"[W153 UI bulk] add failed for "
                                f"{d['competitor_item_id']}: {e}"
                            )
                            error_count += 1

                    # 残り全却下
                    for did in dismiss_ids:
                        update_rival_discovery_status(did, 'dismissed')

                    msg_parts = []
                    if added_count > 0:
                        msg_parts.append(f"✅ 監視追加 {added_count} 件")
                    if conflict_count > 0:
                        msg_parts.append(
                            f"⚠️ conflict {conflict_count} 件 "
                            f"(他 listing で監視中、new tab に残ります)"
                        )
                    if error_count > 0:
                        msg_parts.append(f"❌ エラー {error_count} 件")
                    if dismiss_ids:
                        msg_parts.append(
                            f"🗑️ 自動却下 {len(dismiss_ids)} 件"
                        )
                    st.success(" / ".join(msg_parts) or "処理対象なし")
                    st.rerun()
        with tab_added:
            added = get_rival_discoveries(eid, status='monitoring_added')
            # L-internal-2: 「監視解除」button は本 W scope 外
            st.caption("(監視解除 button は本 W scope 外 / UX 一貫性は別 W で議論)")
            if not added:
                st.caption("監視追加済の rival なし")
            else:
                import pandas as pd
                st.dataframe(
                    pd.DataFrame([
                        {"seller": d["competitor_seller"],
                         "item_id": d["competitor_item_id"],
                         "title": (d["competitor_title"] or "")[:60],
                         "first_seen": d["first_seen_at"],
                         "status_at": d["status_changed_at"]}
                        for d in added[:30]
                    ]),
                    hide_index=True, use_container_width=True,
                )
        with tab_dismissed:
            dis = get_rival_discoveries(eid, status='dismissed')
            if not dis:
                st.caption("却下した rival なし")
            else:
                for d in dis[:30]:
                    cols = st.columns([4, 1])
                    with cols[0]:
                        st.caption(
                            f"{d['competitor_seller']} | "
                            f"{(d['competitor_title'] or '')[:60]} | "
                            f"dismissed at {d['status_changed_at']}"
                        )
                    with cols[1]:
                        if st.button("↩️ 差戻", key=f"pm_rdisc_undo_{d['id']}"):
                            update_rival_discovery_status(d['id'], 'new')
                            st.rerun()

    st.markdown("---")


# =============================================================================
# Per-product render
# =============================================================================

def _render_desc_fetch_button(eid: str, config: dict) -> None:
    """description を eBay GetItem で取得し編集欄 (session_state[pm_desc_{eid}])
    に流し込む form **外** ボタン (W225 2026-06-05).

    st.form 内に st.button は置けないため、フォーム外の即時アクションとして分離
    (旧 C-fix は form 内に置き editor 展開時クラッシュしていた)。取得値は form 内
    description text_area (key=pm_desc_{eid}) が次 rerun で読む (同 key 連携)。
    """
    _desc_key = f"pm_desc_{eid}"
    with st.expander("📥 eBay から現在の説明を取得 (description)", expanded=False):
        if st.button(
            "📥 eBayから現在の説明を取得", key=f"pm_desc_fetchbtn_{eid}",
            help="eBay GetItem で現在の商品説明 (Description) を取得し、下の編集"
                 "フォームの説明欄に表示。保存で MonoDeck DB に保持 (eBay へは未送信)。",
        ):
            from monitor.ebay_client import get_single_listing
            _creds = get_ebay_credentials(config or {})
            if not all(_creds.get(k) for k in ("app_id", "dev_id", "cert_id", "user_token")):
                st.error("eBay API 認証情報が未設定です (設定タブ参照)")
            else:
                with st.spinner("eBay から説明を取得中..."):
                    _snap = get_single_listing(
                        eid, _creds["app_id"], _creds["dev_id"],
                        _creds["cert_id"], _creds["user_token"],
                    )
                if _snap is not None and _snap.get("description") is not None:
                    st.session_state[_desc_key] = _snap.get("description") or ""
                    st.success("取得しました。下の編集フォームの説明欄に表示中。"
                               "保存で DB に保持します。")
                    st.rerun()
                else:
                    st.error("取得失敗 (GetItem 応答なし / Description 空)")


def _render_product_editor(p: dict, config: dict) -> None:
    """表で選択された 1 商品の編集ゾーンを描画 (2 列 layout + st.form + 3 submit).

    UI 改修 (2026-05-11 v3):
      - 編集 inputs は `st.form` で囲み、submit まで rerun が走らない (user 入力中の画面暗化解消)
      - submit button 3 種: 💾 DB保存 / 📤 DB + eBay 反映 / 💡 利益計算 (breakeven 再計算)
    W225 (2026-06-05): 一覧を eBay連携タブと同じ st.dataframe 表形式に変更。
      旧 W221 のトグルボタン + `if is_open:` アコーディオンは廃止し、表の行選択
      (render_product_management 側の single-row 選択) で開いた 1 商品だけを
      この関数で描画する。body は従来の `if is_open:` 内容を不変で実行 (K2)。
      body 内の `pm_keep_open_eid` 書込は表選択 (widget state) と役割が重なるが
      実害なしのため残置。
    """
    eid = p["ebay_item_id"]
    # W225: 呼出元が選択行の 1 商品のみを渡す = 常に編集ゾーンを描画する。
    # body を再 indent しないため guard を残す (差分最小・K2 surgical)。
    if True:
        # ── Title (商品名 full text) ──
        st.markdown(f"### {p.get('title', '')}")

        # ── W151 (2026-05-22): 初期登録済み checkbox ──
        # user 業務 = 初期登録 (ライバル登録 / 物理属性入力 / 仕入先候補確定 等)
        # 完了 listing にチェック → フィルタ「📝 初期未完了のみ」で残作業を絞り込み.
        # W153 (新規ライバル発見) の base point として initial_registered_at を参照予定.
        _init_current = bool(p.get("initial_registered"))
        _init_cols = st.columns([1, 4])
        with _init_cols[0]:
            _init_new = st.checkbox(
                "📝 初期登録済み",
                value=_init_current,
                key=f"pm_init_reg_{eid}",
            )
        with _init_cols[1]:
            if _init_current and p.get("initial_registered_at"):
                st.caption(f"完了時刻: {p['initial_registered_at']} UTC")
        if _init_new != _init_current:
            set_initial_registered(eid, _init_new)
            bump_db_version()
            st.rerun()

        # W153 (2026-05-22): ライバル監視 section の呼出位置を移動.
        # W217-B v2 (2026-06-04 mockup): 2 列構図に再整理。
        # 左列下段=_render_supplier_section / 右列=_render_rival_section。
        # form 外・個別 button 即時反応の挙動は移設後も不変。

        # W138-A (2026-05-17): BP は DB 列駆動で **価格同様「最初から自動
        # 表示」** (per-render GetItem ゼロ、表示ボタン廃止)。鮮度は
        # fetched_at 併記で正直開示 (HIGH-1)。eBay.com 直接変更で stale に
        # なった時のための「↻ 再取得」は単一 listing 1 回 GetItem (opt-in)。
        bp_state = _bp_state_from_db(p)
        if st.button("↻ Shipping BP 再取得",
                     key=f"pm_bprefresh_{eid}",
                     help="この listing の BP を実 eBay から 1 回取得し DB を"
                          "最新化 (eBay 側で直接 BP を変えた後に使用)"):
            _refresh_bp_from_ebay(eid, config)
            st.rerun()
        _bprefresh_err = st.session_state.get(f"pm_bprefresh_err_{eid}")
        if _bprefresh_err:
            st.warning(f"↻ {_bprefresh_err}")

        # ── Hero metrics row: 4 主要指標を上部に大きく表示 ──
        _render_hero_metrics(p, bp_state=bp_state)

        # ── 2 列 layout: 左 (編集 form + 仕入先) / 右 (ライバル) ──
        # W217-B v2 (2026-06-04 mockup): 左=編集+仕入先, 右=ライバルのみ。
        # 配置のみ変更、money-direct ロジック・dirty-flag は不変。
        left, right = st.columns([1, 1], gap="medium")

        with left:
            # W225 (2026-06-05): description の eBay 取得は **即時アクション** (GetItem
            # → 欄へ流し込み) のため form **外** に配置。st.form 内に st.button は置けず、
            # 今朝の C-fix で form 内 (_render_left_basic_and_physical) に混入していた
            # = editor 展開時に StreamlitAPIException でクラッシュしていた (表行選択で
            # 必ず開く本 W で顕在化)。取得値は session_state[pm_desc_{eid}] に書き、
            # form 内 description text_area (同 key) が次 rerun で読む。
            _render_desc_fetch_button(eid, config)
            # 左列上段: 編集 inputs + submit buttons (form 内、rerun 抑制)
            with st.form(key=f"pm_form_{eid}", clear_on_submit=False):
                editing = _render_left_basic_and_physical(
                    p, config, bp_state=bp_state)
                # ── Action button 群 (3 列) ──
                st.markdown('<div class="pm-section-label">アクション</div>',
                            unsafe_allow_html=True)
                btn_cols = st.columns(3)
                with btn_cols[0]:
                    save_db = st.form_submit_button(
                        "💾 DB 保存", use_container_width=True,
                    )
                with btn_cols[1]:
                    save_ebay = st.form_submit_button(
                        "📤 eBay 反映",
                        type="primary", use_container_width=True,
                        help="DB + ReviseFixedPriceItem で eBay 出品価格 / 送料を更新",
                    )
                with btn_cols[2]:
                    calc_be = st.form_submit_button(
                        "💡 利益計算", use_container_width=True,
                        help="DB 保存 + breakeven 再計算",
                    )
            # 左列下段: 仕入先 (form **外** = 「🔍 在庫を今すぐ確認」など
            # 個別 button の即時反応を維持)
            _render_supplier_section(p, config)

        with right:
            # 右列 (form 外): ライバル監視 + 登録済 dataframe
            _render_rival_section(p, config)

        # W#33: キーワード新着監視 トグル (full-width / form 外)
        _render_keyword_watch_toggle(p)

        # eBaymag 国別出品管理 (依頼ボード#10、full-width / form 外 =
        # 状態取得/反映 button の即時反応を維持)
        _render_ebaymag_section(p)

        # ── form 外: submit 結果処理 ──
        if save_db or save_ebay or calc_be:
            comp_list = st.session_state.get(f"pm_comp_list_{eid}", [])
            messages: list[str] = []

            # 1. eBay 反映 (save_ebay の時のみ)
            if save_ebay:
                ebay_result = _apply_to_ebay(
                    eid, editing, config, current_sku=p.get("sku")
                )
                _msg = ebay_result.get("message", "不明")
                snap2 = ebay_result.get("post_snapshot")
                # revise が実行され反映後 GetItem が取れた場合は、verify 成否に
                # 関わらず DB を **実 eBay 値** へ同期する (DB:=真実)。これで
                # HIGH-1 (価格 eBay 反映済・DB 旧値の永続乖離) も部分 verify 乖離も
                # 構造的に消える。snap2 が None = revise 未実行 or verify GetItem
                # 失敗 = DB は触らない (eBay 不明値を DB に書かない=Q0)。
                if snap2 is not None:
                    _sync_db_to_actual(eid, snap2)
                if ebay_result["success"]:
                    messages.append(f"eBay 反映成功 → {_msg}")
                elif ebay_result.get("no_change"):
                    # W227 (2026-06-06 緊急): 価格/送料は差分なし (benign no-op)。
                    # ここで早期 return すると、user が商品ランク (Condition) や説明文を
                    # 変更しても反映されない (S→N 修正不能の不具合)。DB は post_snapshot
                    # で実 eBay へ同期済 (上の _sync_db_to_actual)。Condition/説明文の
                    # 反映 (_apply_listing_content_to_ebay) へ **継続する**。
                    messages.append("価格/送料は eBay と差分なし (反映不要)")
                else:
                    if snap2 is not None:
                        st.warning(
                            f"⚠️ 一部未反映 (DB は実 eBay 値へ同期済): {_msg} / "
                            f"https://www.ebay.com/itm/{eid} で確認してください。"
                            "(仕入価格/在庫数など他の編集値は未保存、"
                            "再度フォームを保存してください)"
                        )
                    else:
                        st.error(f"eBay 反映できず (DB 変更なし): {_msg}")
                    # H5 fix: エラー時も expander を開いたままにする
                    st.session_state["pm_keep_open_eid"] = eid
                    # 未反映項目の editing 値を一括 DB 保存しない (整合性優先)。
                    return

                # W220 slice3: price/shipping 反映成功後、説明文 + ランク→Condition
                # を eBay へ反映 (独立関数、money-direct 送料ロジック非干渉)。DB 保存
                # (下記) より前に呼ぶ = description draft の「変更前 DB 値」と比較できる。
                _content = _apply_listing_content_to_ebay(eid, editing, config)
                if _content.get("changed"):
                    if _content.get("success"):
                        messages.append(
                            _content.get("message") or "説明文/Condition 反映")
                    else:
                        st.warning(
                            "⚠️ 説明文/Condition の eBay 反映に一部失敗: "
                            f"{_content.get('message', '不明')} / "
                            f"https://www.ebay.com/itm/{eid} で確認してください。"
                        )
                        st.session_state["pm_keep_open_eid"] = eid
                    # HIGH-3 (Codex/code-reviewer): eBay 反映に失敗した項目は DB に
                    # 保存しない (DB↔eBay 乖離防止)。desc_ok/cond_ok が False の項目
                    # だけ editing から除外 = _save_product_data が skip (None ガード)。
                    # None=未試行 / True=成功 は通常保存 (DB が実 eBay と一致)。
                    if _content.get("desc_ok") is False:
                        editing["listing_description"] = None
                    if _content.get("cond_ok") is False:
                        editing["rank"] = None
                    # W31: Title 反映失敗時は DB への title 書込を抑止
                    # (title_ok=True の場合は update_ebay_listing_title が既に呼ばれており
                    # _save_product_data は title を上書きしないため競合しない)
                    if _content.get("title_ok") is False:
                        editing["new_title"] = None

            # 2. DB 保存 (save_db / save_ebay 両方で実行)
            # H9 (Wave C): eBay 反映 success 後の DB save 失敗を transparent に報告.
            # 旧実装は例外 propagate → user に generic Streamlit error、DB-eBay 不整合の説明なし.
            if save_db or save_ebay:
                try:
                    _save_product_data(
                        ebay_item_id=eid,
                        editing=editing,
                        competitors=comp_list,
                        recalc_breakeven=False,  # breakeven は別ボタンで明示的に
                        config=config,
                    )
                    if save_ebay:
                        messages.append(
                            "💾 DB 保存完了 (※ eBay GetItem で実反映の最終 verify 推奨)"
                        )
                    else:
                        messages.append("💾 DB 保存完了")
                except (sqlite3.IntegrityError, sqlite3.OperationalError,
                        ValueError, KeyError) as db_e:
                    logger.exception(f"[pm] DB save failed (eid={eid})")
                    if save_ebay:
                        # eBay 反映 success 済み → DB 不整合の警告
                        st.error(
                            f"⚠️ eBay 反映は **成功**しましたが、DB 同期に失敗しました: {db_e}. "
                            f"`https://www.ebay.com/itm/{eid}` で eBay 側を verify し、"
                            f"DB を手動で再保存してください. "
                            f"(price/shipping/SKU/inventory が DB と eBay で一時的に乖離します)"
                        )
                    else:
                        st.error(f"❌ DB 保存失敗: {db_e}")
                    st.session_state["pm_keep_open_eid"] = eid
                    return

            # 3. 利益計算 (calc_be の時のみ)
            if calc_be:
                # まず DB 保存してから breakeven 再計算 (input 値を反映)
                if not (save_db or save_ebay):
                    _save_product_data(
                        ebay_item_id=eid,
                        editing=editing,
                        competitors=comp_list,
                        recalc_breakeven=False,
                        config=config,
                    )
                try:
                    # calculator.load_settings() を使用 (schedule_config.json ではない)
                    be = update_listing_breakeven(eid, _calc_settings())
                    if be and be > 0:
                        # 「\$」escape で markdown LaTeX 化を回避.
                        messages.append(f"損益分岐: **\\${be:.2f}** (再計算完了、上部 metric 参照)")
                    else:
                        messages.append(
                            "breakeven 計算は仕入価格 + 重量 + 寸法が必要 (未入力 listing)"
                        )
                except (sqlite3.OperationalError, TypeError, ValueError, KeyError, RuntimeError) as e:
                    st.error(f"利益計算エラー: {e}")
                    # H5 fix: エラー時も expander 維持
                    st.session_state["pm_keep_open_eid"] = eid
                    return

            if messages:
                st.success(" | ".join(messages))

            # 保存後もアコーディオンを開いたままにする
            st.session_state["pm_keep_open_eid"] = eid


def _money_eq(a: Optional[float], b: Optional[float]) -> bool:
    """USD 金額の 0.01 丸め一致 (None はどちらかでも不一致)."""
    if a is None or b is None:
        return False
    return round(float(a), 2) == round(float(b), 2)


# W220 slice3 (2026-06-04): 商品ランク → eBay ConditionID マップ (CLAUDE.md 8段階)。
# S=1500 は category 依存で不可な場合があり、revise 失敗/verify 不一致時に
# 3000 (Used) へ fallback する (Q0: 降格を明示通知、サイレント不可)。
_RANK_TO_CONDITION_ID = {
    "N": "1000", "S": "1500",
    "A": "3000", "B": "3000", "C": "3000", "D": "3000", "PO": "3000",
    "As-Is": "7000",
}


def _apply_listing_content_to_ebay(eid: str, editing: dict, config: dict) -> dict:
    """W220 slice3 + W31: description / 商品ランク→Condition / Title を eBay へ反映。

    price/shipping の _apply_to_ebay とは **独立** (money-direct に干渉しない)。

    - description: 編集 draft が DB 保存値と異なり非空なら ReviseItem で push。
    - condition: rank dirty-flag (user 変更時のみ) → ReviseFixedPriceItem + post verify。
    - title (W31): title dirty-flag (user 変更時のみ) → ReviseItem + GetItem verify。
      80 文字超は revise_item_title 側で reject (eBay 送出しない)。

    eBay 書込は呼出側「📤 eBay反映」クリック時のみ発火 (自動実行されない)。

    Returns: {'success': bool, 'message': str, 'changed': bool,
              'desc_ok': Optional[bool], 'cond_ok': Optional[bool],
              'title_ok': Optional[bool]}
    """
    new_desc = editing.get("listing_description")
    new_rank = editing.get("rank")
    # W227: rank dirty-flag — render 初期値と同じなら push しない (stale 誤上書き防止)
    _rank_initial = editing.get("rank_render_initial")
    _rank_user_changed = bool(new_rank) and new_rank != _rank_initial
    target_cid = _RANK_TO_CONDITION_ID.get(new_rank) if _rank_user_changed else None

    # W31: title dirty-flag — render 初期値と同じなら push しない
    new_title = (editing.get("new_title") or "").strip()
    _title_initial = (editing.get("title_render_initial") or "").strip()
    _title_user_changed = bool(new_title) and new_title != _title_initial
    title_to_push = new_title if _title_user_changed else None

    # description は「draft が DB 現値と異なり非空」の時だけ push。
    desc_changed = False
    if new_desc is not None and new_desc.strip():
        with get_conn() as conn:
            _r = conn.execute(
                "SELECT listing_description FROM ebay_listings WHERE ebay_item_id=?",
                (eid,),
            ).fetchone()
        cur_desc = (_r[0] if _r else None) or ""
        desc_changed = new_desc.strip() != cur_desc.strip()

    if not desc_changed and target_cid is None and title_to_push is None:
        return {"success": True, "message": "", "changed": False,
                "desc_ok": None, "cond_ok": None, "title_ok": None}

    try:
        creds = get_ebay_credentials(config or {})
        app_id = creds.get("app_id", "")
        dev_id = creds.get("dev_id", "")
        cert_id = creds.get("cert_id", "")
        token = creds.get("user_token", "")
        if not (app_id and dev_id and cert_id and token):
            return {"success": False, "message": "eBay credentials 不在",
                    "changed": True, "desc_ok": None, "cond_ok": None, "title_ok": None}
    except (KeyError, ValueError, OSError) as e:
        return {"success": False, "message": f"credentials 取得エラー: {e}",
                "changed": True, "desc_ok": None, "cond_ok": None, "title_ok": None}

    from monitor.ebay_client import (
        revise_item_condition, revise_item_description, revise_item_title,
    )
    from monitor.ebay_listing_snapshot import fetch_listing_snapshot
    from monitor.database import update_ebay_listing_condition, update_ebay_listing_title

    ok = True
    msgs: list[str] = []
    desc_ok = None   # None=未試行 / True/False=反映結果 (HIGH-3 per-part DB 保存用)
    cond_ok = None
    title_ok = None  # W31: None=未試行 / True=verify OK / False=失敗
    # ConditionDescription (eBay 表示用、DB 非保存)。used/As-Is で送る。
    cd = (editing.get("condition_description") or "").strip() or None

    # 1. description (ReviseItem)
    if desc_changed:
        _rd = revise_item_description(
            eid, new_desc, app_id, dev_id, cert_id, token)
        desc_ok = bool(_rd.get("success"))
        if desc_ok:
            msgs.append("説明文を eBay に反映")
        else:
            ok = False
            msgs.append(f"説明文 反映失敗: {_rd.get('message', '不明')}")

    # 2. 商品ランク → ConditionID (pre GetItem で差分判定 → revise → post verify)
    if target_cid is not None:
        # HIGH-1 (Q0): As-Is(7000) は ConditionDescription 必須 (CLAUDE.md)。
        # 理由欠落なら silent push せず明示ブロック (buyer紛争 Defect 防止)。
        if target_cid == "7000" and not cd:
            cond_ok = False
            ok = False
            msgs.append("As-Is は『Condition 理由』が必須です。状態説明を入力して"
                        "再反映してください (未入力のため Condition は変更せず)")
        else:
            snap = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, token)
            if not snap.ok:
                cond_ok = False
                ok = False
                msgs.append(f"Condition 反映中止 (現状 GetItem 失敗): {snap.error}")
            elif (snap.condition_id or "") == target_cid:
                # 既に一致 = eBay 変更不要。DB を実値 + user 意図へ同期 (表示一致)。
                # 例: Used(3000) 品に user が初めてサブランク(B 等)を付与するケース。
                update_ebay_listing_condition(
                    eid, ebay_condition_id=target_cid, condition_rank=new_rank)
            else:
                _rc = revise_item_condition(
                    eid, target_cid, app_id, dev_id, cert_id, token,
                    condition_description=cd)
                snap2 = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, token)
                actual = snap2.condition_id if snap2.ok else None
                if actual == target_cid:
                    cond_ok = True
                    # W227: 検証済み実 ConditionID + user 状態を DB へ同期。
                    update_ebay_listing_condition(
                        eid, ebay_condition_id=target_cid, condition_rank=new_rank)
                    msgs.append(f"Condition を {new_rank} ({target_cid}) に反映")
                elif target_cid == "1500":
                    # S=1500 が category 不可 → 3000 (Used) へ fallback (明示通知)
                    _rf = revise_item_condition(
                        eid, "3000", app_id, dev_id, cert_id, token,
                        condition_description=cd)
                    snap3 = fetch_listing_snapshot(
                        eid, app_id, dev_id, cert_id, token)
                    if snap3.ok and snap3.condition_id == "3000":
                        cond_ok = True
                        # eBay 実値は 3000(Used)。user 意図"S"は実態と乖離するので
                        # condition_rank に "S" を残さない: ebay_condition_id のみ実値
                        # 同期し、editing["rank"]=None で下流 _save_product_data の
                        # condition_rank="S" 誤保存を抑止 (user が後で Used サブランク指定)。
                        update_ebay_listing_condition(eid, ebay_condition_id="3000")
                        editing["rank"] = None
                        msgs.append("Condition: S(新品同様)はこのカテゴリで不可の"
                                    "ため Used(3000) で反映しました (Used サブランクは"
                                    "別途指定してください)")
                    else:
                        cond_ok = False
                        ok = False
                        msgs.append("Condition 反映失敗 (S=1500 不可・3000 "
                                    f"fallback も失敗): {_rc.get('message', '不明')}")
                else:
                    cond_ok = False
                    ok = False
                    msgs.append(f"Condition 反映 verify 失敗 (実値={actual}): "
                                f"{_rc.get('message', '不明')}")

    # 3. Title (W31): dirty-flag で user 変更時のみ push → GetItem verify
    if title_to_push is not None:
        _rt = revise_item_title(eid, title_to_push, app_id, dev_id, cert_id, token)
        title_ok = bool(_rt.get("success"))
        if title_ok:
            # eBay verify 済み → DB 更新 (verify 失敗時は DB を書かない = 乖離防止)
            try:
                update_ebay_listing_title(eid, title_to_push)
            except ValueError as _te:
                title_ok = False
                ok = False
                msgs.append(f"Title DB 保存失敗: {_te}")
            else:
                msgs.append(f"Title を eBay に反映 ({len(title_to_push)} 文字)")
        else:
            ok = False
            msgs.append(f"Title 反映失敗: {_rt.get('message', '不明')}")

    return {"success": ok, "message": " / ".join(msgs), "changed": True,
            "desc_ok": desc_ok, "cond_ok": cond_ok, "title_ok": title_ok}


def _apply_to_ebay(
    eid: str, editing: dict, config: dict,
    current_sku: Optional[str] = None,
) -> dict:
    """eBay 反映: 反映前 GetItem で実 eBay と差分検出 → 差分のみ revise →
    反映後 GetItem で実値一致を verify (Ack でなく実値で成功判定).

    W137 (2026-05-17) 再設計の核心:
      - **DB を信頼しない**: 変更検出は form vs 実 eBay GetItem (A1). DB↔eBay
        既存乖離 (例 DB 'STOCK' vs eBay 'stock:01') を確実に検出する。
        `current_sku` 引数は signature 互換のため残すが未使用。
      - **W136 送料 fix**: 送料 override 時、反映前 snapshot の 3 profile ID
        (Payment/Return/Shipping) を seller_profiles で同梱。BP 管理 listing は
        SellerProfiles 同梱が無いと override が無音失敗 (真因 2 段検証済)。
      - **fake success 排除 (B1)**: 反映後 GetItem で実値が form 期待値と
        一致した項目のみ ✅。Ack=Success でも実値不一致なら ❌ + 実値併記。
      - 反映前/後 snapshot 取得失敗時は revise 中止 or 不明として success:False
        (Q0: 不明を「変更なし」「成功」と偽らない)。
      - 戻り `post_snapshot` を呼出側へ渡し、DB は **実 eBay 値へ同期**
        (DB:=真実 で HIGH-1 / 部分 verify 乖離を構造排除)。
    """
    from monitor.ebay_listing_snapshot import fetch_listing_snapshot

    new_price = editing.get("new_ebay_price")
    new_ship = editing.get("new_ship_cost")
    new_add = editing.get("new_ship_additional")
    form_sku = (editing.get("sku") or "").strip()

    new_bp_id = editing.get("new_bp_id")  # W138: selectbox 選択 BP id
    base = {
        "success": False, "message": "", "new_price": new_price,
        "new_ship": new_ship, "sku_pushed": False,
        "price_ship_ok": None, "sku_ok": None, "bp_ok": None,
        "add_ok": None,  # W142: +each verify 結果
        "post_snapshot": None,
    }

    try:
        creds = get_ebay_credentials(config or {})
        app_id = creds.get("app_id", "")
        dev_id = creds.get("dev_id", "")
        cert_id = creds.get("cert_id", "")
        token = creds.get("user_token", "")
        if not (app_id and dev_id and cert_id and token):
            return {**base, "message": "eBay credentials 不在"}
    except (KeyError, ValueError, OSError) as e:
        logger.exception("[pm] credentials 取得エラー")
        return {**base, "message": f"credentials 取得エラー: {e}"}

    # ── Phase 1: 反映前 snapshot (実 eBay = 真実源) ──
    snap = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, token)
    if not snap.ok:
        return {**base,
                "message": f"反映前 GetItem 失敗のため反映中止: {snap.error}"}

    # ── Phase 2: 差分検出 (form vs 実 eBay、DB 不参照) ──
    sku_changed = bool(form_sku) and form_sku != (snap.sku or "")
    price_changed = (
        new_price is not None and new_price > 0
        and not _money_eq(float(new_price), snap.start_price_usd)
    )
    ship_changed = (
        new_ship is not None
        and not _money_eq(float(new_ship), snap.ship_cost_usd)
    )
    # W142 根本原因#5(a): 旧実装の ship_changed は new_ship (Buyer pays)
    # のみ判定し new_add (+each) を一切含めなかった → +each のみ変更が
    # 永久に「差分なし」扱い (no-diff branch) → 外側で紛らわしい
    # 「⚠️一部未反映」warning。add_changed を独立追加。
    # W142 Codex-R3 HIGH-2 (金銭直結): +each dirty-flag。user が +each
    # widget を**実際に操作**した時のみ変更とみなす。snap.ship_additional
    # _usd が None (GetItem が +each を返さない) でも、表示中の DB 初期値
    # (add_render_initial) を「変更」と誤認すると stale DB 値を実 eBay へ
    # 上書きし真の DDP buffer を喪失する (Phase2「実 eBay 真実・DB 非権威」
    # 違反)。BP の Codex#1 bp_user_touched と完全同型。比較相手は依然
    # pre-snapshot 実 eBay (W137)。
    # ⚠️ 呼出契約 (Codex-R3 再確認 MEDIUM): `editing` は商品管理タブの
    # render 経由で構築すること。render (L~806) が `add_render_initial`
    # (= +each widget の DB 初期値) を `bp_render_initial_id` と同様に
    # **無条件 set** する。本番 caller は `_apply_to_ebay` 呼出 1 箇所
    # (UI submit、render 由来 editing) のみ = 契約は本番で常に充足。
    # render を経ない直接/プログラム呼出は add_render_initial を明示
    # 供給すること (省略すると None 比較で stale 値を誤って user 編集
    # 扱いし得る = money-direct)。BP dirty-flag と同じ既存契約構造。
    add_render_initial = editing.get("add_render_initial")
    add_user_touched = new_add != add_render_initial
    add_changed = (
        add_user_touched
        and new_add is not None
        and not _money_eq(float(new_add), snap.ship_additional_usd)
    )
    # Codex#1 dirty-flag (金銭直結): user が selectbox を**実際に操作**した
    # 時のみ BP 変更とみなす。無操作 = submit 値が render 時 DB 初期値と
    # 同一 → eBay.com 外部変更で DB が stale でも、その stale 値が実 eBay
    # へ送られて B→A に巻き戻る経路を構造的に遮断 (DDP buffer 喪失 =
    # Section 232 数百ドル/件 防止)。変更検出の比較相手は依然 pre-snapshot
    # 実 eBay (W137 真実源、DB 基準にしない)。無操作時は BP を一切 touch
    # せず、DB は post-snapshot 経由 _sync_db_to_actual で実 eBay へ resync。
    bp_render_initial = editing.get("bp_render_initial_id")
    bp_user_touched = bool(new_bp_id) and new_bp_id != bp_render_initial
    bp_changed = (
        bp_user_touched
        and new_bp_id != (snap.shipping_profile_id or None)
    )
    if not (sku_changed or price_changed or ship_changed
            or add_changed or bp_changed):
        # MED-2-fix: 差分なしでも pre-snapshot (= 実 eBay 値、ここに来る
        # 時点で snap.ok=True) を post_snapshot として返す。呼出側の
        # _sync_db_to_actual が DB を実 eBay へ resync = stale DB BP/価格/
        # SKU を自己治癒 (W137「DB:=真実」、冪等)。dirty-flag が無操作を
        # 正しく抑止しても DB が古いままだと次 render で stale 表示が残り
        # HIGH を助長するため、no-diff でも heal させる (Codex#1 補完)。
        return {**base,
                # W227 (2026-06-06): 価格/送料の「差分なし」は失敗ではなく benign な
                # no-op。呼出側はこれを早期 return せず Condition/説明文の反映へ継続する。
                "no_change": True,
                "post_snapshot": snap,
                "message": "実 eBay と差分なし (反映不要、DB は実 eBay へ"
                "同期)。"
                f"eBay 実値: SKU={snap.sku} 価格={snap.start_price_usd} "
                f"送料={snap.ship_cost_usd} +each={snap.ship_additional_usd} "
                f"BP={snap.shipping_profile_id}"}
    # ── W142: Q-3 撤廃 + combined-新BP 判定/preflight ──
    # 旧 Q-3 は「BP変更 ∧ 価格/送料変更」を明示拒否していた (W138-A 過剰
    # 保守)。eBay は ShippingServiceCostOverrideList と SellerProfiles を
    # 同一 ReviseFixedPriceItem に同梱可 (W136 実証、Add 経路と同機構)。
    # combined で新BP を SellerProfiles に入れ override を同梱すれば
    # custom 送料 (DDP buffer) を新BP に維持できる (user 確定方針 A)。
    bp_combined = bp_changed and (
        price_changed or ship_changed or add_changed)
    combined_prio = 1  # combined-新BP の <ShippingServicePriority>
    if bp_combined:
        # R2 (Codex/W136): combined は SellerProfiles を 新BP shipping_id +
        # 現 payment/return で同梱する。payment/return が pre-snapshot から
        # 取れないと不完全 SellerProfiles = Ack=Fail or 意図せぬ
        # payment/return policy 適用 (money/account risk) → revise せず
        # 痕跡 (Q0、revise_shipping_profile の 3ID 必須ガードと同型)。
        if not (snap.payment_profile_id and snap.return_profile_id):
            logger.warning(
                f"[pm] {eid} combined-新BP 中止: pre-snapshot に "
                f"payment/return profile ID 不足 "
                f"(pay={snap.payment_profile_id} "
                f"ret={snap.return_profile_id})"
            )
            return {**base,
                    "post_snapshot": snap,
                    "message": "BP+価格/送料の同時反映を中止 (実 eBay から "
                    "payment/return policy ID が取得できず、不完全な "
                    "SellerProfiles 送信は account リスク)。BP のみ先に "
                    "📤eBay反映 で変更してから価格/送料を調整してください。"}
        # preflight: 新BP の domestic service sortOrder から
        # ShippingServicePriority を解決。eBay 公式 (Sell Account API):
        # priority は BP の matching service の sortOrder と一致させる。
        # 1 ハードコードは W136 無音失敗の根 (Ack=Success だが override
        # 黙殺 = DDP buffer 喪失 = Section 232 数百ドル/件)。
        from monitor.ebay_account_policy import resolve_domestic_priority
        _pol_list = _cached_shipping_policies()
        _pol = None
        if _pol_list.ok:
            for _p in _pol_list.policies:
                if _p.policy_id == new_bp_id:
                    _pol = _p
                    break
        if _pol is None:
            logger.warning(
                f"[pm] {eid} combined-新BP 中止: 新BP {new_bp_id} の "
                f"shipping policy 取得不能 "
                f"(list ok={_pol_list.ok} err={_pol_list.error})"
            )
            return {**base,
                    "post_snapshot": snap,
                    "message": "BP+価格/送料の同時反映を中止 (新BP の "
                    "shipping policy 情報を取得できず、送料 override の "
                    "priority を解決不能)。BP のみ先に変更してから価格/"
                    "送料を調整してください。"}
        _prio, _reason = resolve_domestic_priority(_pol)
        if _prio is None:
            logger.warning(
                f"[pm] {eid} combined-新BP 中止: priority 解決不能 "
                f"reason={_reason} bp={new_bp_id} "
                f"dom_count={_pol.domestic_service_count}"
            )
            return {**base,
                    "post_snapshot": snap,
                    "message": "BP+価格/送料の同時反映を中止 (新BP の "
                    f"domestic 送料 priority を解決できません: {_reason})。"
                    "送料 override が無音失敗し DDP buffer を喪失する恐れ"
                    "があるため、BP のみ先に 📤eBay反映 で変更してから"
                    "価格/送料を調整してください。"}
        combined_prio = _prio

    parts: list[str] = []
    sku_pushed = False
    # W142: combined で実際に override を送ったか / 送った ship/+each
    # (Phase4 verify が新BP bind と無音失敗を判定するのに使う)。
    sent_override = False
    sent_ship_cost: Optional[float] = None
    sent_ship_add: Optional[float] = None

    # ── W142 Codex-R3 統一安全ガード (HIGH-1/HIGH-3 一括根治) ──
    # override を出す/保持する経路 (bp_combined ∨ ship_changed ∨
    # add_changed) で base(Buyer pays) か +each が **不確定** = pre-snapshot
    # 実 eBay が None ∧ user 未入力、の時、None を 0.00 / BP-default に
    # 捏造して見えない DDP buffer (Section 232 数百ドル/件) を黙って喪失
    # する経路を物理排除し **明示 abort** する (Q0 / silent-skip-
    # prevention)。snap.ship_cost/ship_additional が None = GetItem が
    # その送料を返さなかった = 不確定。本 system が出す override は常に
    # 明示 0.00 を持つため正常 listing は非None。None は anomaly /
    # 未 revise / calculated shipping 等の少数 = 安全側で degrade。
    # 旧 add_no_base 特殊機構 (+each 単独 base 無) は本ガードに統合。
    _emits_override = bp_combined or ship_changed or add_changed
    if _emits_override:
        _base_src = new_ship if ship_changed else snap.ship_cost_usd
        _add_src = new_add if add_changed else snap.ship_additional_usd
        _indet = []
        if _base_src is None:
            _indet.append("Buyer pays (基本送料)")
        if _add_src is None:
            _indet.append("+each (追加送料)")
        if _indet:
            logger.warning(
                f"[pm] {eid} 送料状態不確定で変更 abort: "
                f"{'/'.join(_indet)} が pre-snapshot に無く user 未入力 "
                f"(bp_combined={bp_combined} ship_changed={ship_changed} "
                f"add_changed={add_changed})"
            )
            return {**base, "post_snapshot": snap,
                    "message": (
                        "実 eBay の送料 (" + " / ".join(_indet) + ") が "
                        "GetItem で取得できず不確定です。BP/送料の変更は "
                        "見えない DDP buffer (Section 232 で数百ドル/件) を "
                        "黙って喪失する恐れがあるため中止しました。"
                        "↻ Shipping BP 再取得 で実 eBay を取り込むか、"
                        "Buyer pays / +each を明示入力してから再反映して "
                        "ください。")}

    # ── Phase 3: revise (差分項目のみ) ──
    # revise API の失敗原因 (eBay ErrorCode / token 失効等) は最も価値ある
    # 診断情報。実値 verify が最終 gate だが、API message を握り潰すと
    # 「W136 無音失敗」と「送信拒否(token失効等)」を user が区別できない
    # → 必ず message に合流させる (HIGH-1 2026-05-17 / silent-skip 精神)。
    revise_errs: list[str] = []
    if bp_combined:
        # W142: combined ReviseFixedPriceItem = 新BP + override + 価格 を
        # 1 回で送信。user 確定方針 A: ship 未変更でも現 override を再送し
        # 新BP に custom 送料 (DDP buffer) を維持 (combined ケース i)。
        # R4 (状態リセット副作用防止): +each も未変更なら現 +each を再送。
        # ⚠️ Codex-R3 HIGH-3 訂正: 「snap.ship_cost_usd None = 元々 custom
        # 送料なし = BP default で正しい」は **誤り** (None は GetItem が
        # 送料を返さなかった = 不確定であり、見えない override を喪失し得る)。
        # 上流の統一安全ガードが indeterminate (base/+each いずれか None)
        # を既に明示 abort 済 = ここに来る時点で base/+each は確定 (実 eBay
        # 値 or user 入力)。combined では常に override を再送し新BP に DDP
        # buffer を維持する (この前提でガードを弱めないこと)。
        _c_ship = float(new_ship) if ship_changed else snap.ship_cost_usd
        _c_add = (float(new_add) if add_changed
                  else snap.ship_additional_usd)
        sent_ship_cost = (float(_c_ship) if _c_ship is not None else None)
        sent_ship_add = (float(_c_add) if _c_add is not None else None)
        sent_override = sent_ship_cost is not None
        _r = revise_fixed_price_with_shipping(
            item_id=eid,
            new_price_usd=float(new_price) if price_changed else None,
            ship_cost_usd=sent_ship_cost,
            ship_additional_usd=sent_ship_add,
            app_id=app_id, dev_id=dev_id, cert_id=cert_id,
            user_token=token,
            # W142: 新BP を SellerProfiles に入れる (combined の核心)。
            # payment/return は preflight で非None確認済 (R2)。
            seller_profiles={
                "payment_id": snap.payment_profile_id,
                "return_id": snap.return_profile_id,
                "shipping_id": new_bp_id,
            },
            ship_priority=combined_prio,
            # W142 HIGH-1 fix: override 無し (custom 送料を持たない) listing
            # でも新BPを SellerProfiles で必ず送る (combined ケース i で
            # BP 変更が無音欠落するのを防ぐ)。
            force_seller_profiles=True,
        )
        if not _r.get("success"):
            revise_errs.append(
                "combined(BP+価格/送料) revise API 失敗: "
                f"{_r.get('message', '不明')}"
            )
    elif price_changed or ship_changed or add_changed:
        # 非 combined (BP 据置)。W142 根本原因#5: +each のみ変更でも
        # override block を出すため現 ship_cost を再送。R4: ship のみ
        # 変更で +each 未操作なら現 +each を再送 (0 に潰さない)。
        # HIGH-2: 送料/+each 変更だが BP shipping profile ID 不在 =
        # SellerProfiles 非同梱 = W136 無音失敗の確定条件 → 痕跡。
        if (ship_changed or add_changed) and not snap.shipping_profile_id:
            logger.warning(
                f"[pm] {eid} 送料/+each変更だが pre-snapshot に "
                "shipping_profile_id 無し → SellerProfiles 非同梱で "
                "override 無音失敗の恐れ (W136 条件)"
            )
            parts.append(
                "⚠️ BP shipping profile ID が GetItem から取得できず、"
                "送料 override が無音失敗する可能性 (要 eBay 手動確認)"
            )
        # 統一ガード通過後: ship/+each 変更時は base が確定 (実 eBay or
        # user 入力)。price のみ変更 (_emits_override=False でガード非該当)
        # は ship_cost None で override 非出力 = 旧 W137 price-only 挙動。
        _nc_ship = (float(new_ship) if ship_changed
                    else (snap.ship_cost_usd if add_changed else None))
        _nc_add = (float(new_add) if add_changed
                   else (snap.ship_additional_usd if ship_changed
                         else None))
        sent_ship_cost = (float(_nc_ship) if _nc_ship is not None
                          else None)
        sent_ship_add = (float(_nc_add) if _nc_add is not None else None)
        sent_override = sent_ship_cost is not None
        _r = revise_fixed_price_with_shipping(
            item_id=eid,
            new_price_usd=float(new_price) if price_changed else None,
            ship_cost_usd=sent_ship_cost,
            ship_additional_usd=sent_ship_add,
            app_id=app_id, dev_id=dev_id, cert_id=cert_id,
            user_token=token,
            # W136: BP 参照を同梱 (反映前 snapshot 由来の実 3 ID、現BP維持)
            seller_profiles={
                "payment_id": snap.payment_profile_id,
                "return_id": snap.return_profile_id,
                "shipping_id": snap.shipping_profile_id,
            },
        )
        if not _r.get("success"):
            revise_errs.append(
                f"価格/送料 revise API 失敗: {_r.get('message', '不明')}"
            )
    if sku_changed:
        if not (form_sku.startswith("stock")
                or form_sku.startswith("ebay")):
            # off-spec は自動正規化せず抑止 (sku-rules / Q0 痕跡)
            parts.append(
                f"SKU: ⚠️ '{form_sku}' は規約外形式 "
                "(stock* 有在庫 / ebay**_***** 無在庫 以外)。"
                "sku-rules により自動正規化せず eBay 反映を抑止。"
                "正しい形式で再入力してください (大文字 STOCK 等は off-spec)"
            )
        else:
            _rs = revise_item_sku(
                eid, form_sku, app_id=app_id, dev_id=dev_id,
                cert_id=cert_id, user_token=token,
            )
            if _rs.get("success"):
                sku_pushed = True
            else:
                revise_errs.append(
                    f"SKU revise API 失敗: {_rs.get('message', '不明')}"
                )
    if bp_changed and not bp_combined:
        # W138: BP 単独変更 専用経路 (price/ship/+each 全未変更時のみ。
        # W136 override gate 非経由、SellerProfiles 3 ID 同梱)。override
        # 非同梱 = eBay 仕様で送料は新 BP default にリセット (custom 送料
        # を持たない listing の純粋な BP 差し替え)。combined 時はこの経路
        # を通らず上の combined revise が新BP+override を 1 回で送る。
        _rb = revise_shipping_profile(
            eid,
            {
                "payment_id": snap.payment_profile_id,
                "return_id": snap.return_profile_id,
                "shipping_id": new_bp_id,
            },
            app_id, dev_id, cert_id, token,
        )
        if not _rb.get("success"):
            revise_errs.append(
                f"BP 変更 revise API 失敗: {_rb.get('message', '不明')}"
            )
        # W138-A: 旧 pm_curbp session cache は廃止。BP の DB 反映は呼出側
        # の _sync_db_to_actual (post-snapshot 実値 + bump_db_version) が
        # 担う = 価格/送料と同一機構。ここでの cache 破棄は不要。
    if revise_errs:
        parts.append("⚠️ API: " + " / ".join(revise_errs))

    # ── Phase 4: 反映後 snapshot で実値 verify (Ack でなく実値) ──
    snap2 = fetch_listing_snapshot(eid, app_id, dev_id, cert_id, token)
    if not snap2.ok:
        return {
            **base, "sku_pushed": sku_pushed,
            "message": "revise 送信後の verify 用 GetItem 失敗 = 実反映不明 "
                       f"(成功と断定しない): {snap2.error}",
        }

    price_ship_ok: Optional[bool] = None
    sku_ok: Optional[bool] = None
    add_ok: Optional[bool] = None
    overall_ok = True

    # 送料 verify の期待値: ship_changed なら新値、combined で override を
    # 再送した時は ship 未変更でも「送った ship_cost が新BPに乗ったか」を
    # verify (W136 無音失敗 = Ack=Success だが override 黙殺 を post-state
    # で検出)。
    _ship_expect: Optional[float] = None
    if ship_changed:
        _ship_expect = float(new_ship)
    elif bp_combined and sent_ship_cost is not None:
        _ship_expect = float(sent_ship_cost)
    if price_changed or _ship_expect is not None:
        pv = (not price_changed) or _money_eq(
            snap2.start_price_usd, float(new_price))
        sv = (_ship_expect is None) or _money_eq(
            snap2.ship_cost_usd, _ship_expect)
        price_ship_ok = pv and sv
        overall_ok = overall_ok and price_ship_ok
        det = []
        if price_changed:
            det.append(
                f"価格 期待{float(new_price):.2f}/実{snap2.start_price_usd}"
                f"{'✅' if pv else '❌'}")
        if _ship_expect is not None:
            det.append(
                f"送料 期待{_ship_expect:.2f}/実{snap2.ship_cost_usd}"
                f"{'✅' if sv else '❌'}")
        parts.append(
            f"価格/送料: {'✅' if price_ship_ok else '❌ 実値不一致'} "
            f"({' '.join(det)})")

    # W142 +each verify (R7: 送ったのに snap2 に出ない = 不明 = fail。
    # Ack を success に偽装しない / silent-skip-prevention)。統一ガード
    # 通過後なので indeterminate base/+each は既に abort 済 = ここは
    # 確定値を送った後の post-state 照合のみ。
    _add_expect: Optional[float] = None
    if add_changed:
        _add_expect = float(new_add)
    elif bp_combined and sent_override and sent_ship_add is not None:
        _add_expect = float(sent_ship_add)
    if _add_expect is not None:
        if snap2.ship_additional_usd is None:
            add_ok = False
            overall_ok = False
            parts.append(
                f"+each: ❌ 実 eBay GetItem に +each が出ず verify 不能 "
                f"(期待{_add_expect:.2f}、override 無音失敗の疑い→"
                f"要 eBay 手動確認)")
        else:
            add_ok = _money_eq(snap2.ship_additional_usd, _add_expect)
            overall_ok = overall_ok and add_ok
            parts.append(
                f"+each: {'✅' if add_ok else '❌ 実値不一致'} "
                f"期待{_add_expect:.2f}/実{snap2.ship_additional_usd}")

    if sku_changed and sku_pushed:
        sku_ok = (snap2.sku == form_sku)
        overall_ok = overall_ok and sku_ok
        parts.append(
            f"SKU: {'✅' if sku_ok else '❌ 実値不一致'} "
            f"期待'{form_sku}'/実'{snap2.sku}'")
    elif sku_changed and not sku_pushed:
        sku_ok = False
        overall_ok = False

    bp_ok: Optional[bool] = None
    if bp_changed:
        # 実値 verify (Ack でなく snap2 の shipping_profile_id 一致)。
        bp_ok = (snap2.shipping_profile_id == new_bp_id)
        overall_ok = overall_ok and bp_ok
        if bp_combined and sent_override:
            # W142/R1/R3: combined で override を送った → 新BP に override
            # が bind したか (存在 + priority 一致) を実値照合。Ack=Success
            # でも priority 不一致等で override が黙殺される W136 無音失敗
            # (= DDP buffer 喪失 = Section 232 数百ドル/件) を post-state
            # で検出 (Ack を success に偽装しない / silent-skip-prevention)。
            if not snap2.ship_override_present:
                overall_ok = False
                ov_msg = (" / ⚠️ override 不在 = custom 送料が新BPに "
                          "bind せず無音失敗の疑い (DDP buffer 喪失、"
                          "要 eBay 手動確認)")
            elif snap2.ship_override_priority != combined_prio:
                overall_ok = False
                ov_msg = (f" / ⚠️ override priority 不一致 (期待"
                          f"{combined_prio}/実"
                          f"{snap2.ship_override_priority}) = 無音失敗の疑い")
            else:
                ov_msg = (f" / override ✅ priority="
                          f"{snap2.ship_override_priority}")
            parts.append(
                f"BP: {'✅' if bp_ok else '❌ 実値不一致'} "
                f"期待'{new_bp_id}'/実'{snap2.shipping_profile_id}' "
                f"(combined: 送料 ${snap2.ship_cost_usd} を新BPに維持)"
                f"{ov_msg}")
        else:
            # BP 単独変更: 送料は新 BP default に変化 → snap2.ship_cost_usd
            # を caller の _sync_db_to_actual が DB へ同期 (HIGH-3、
            # DB:=真実)。
            parts.append(
                f"BP: {'✅' if bp_ok else '❌ 実値不一致'} "
                f"期待'{new_bp_id}'/実'{snap2.shipping_profile_id}' "
                f"(送料は新 BP default ${snap2.ship_cost_usd} に変化)")

    return {
        "success": overall_ok,
        "message": " / ".join(parts) if parts else "変更なし",
        "new_price": new_price,
        "new_ship": new_ship,
        "sku_pushed": sku_pushed,
        "price_ship_ok": price_ship_ok,
        "sku_ok": sku_ok,
        "bp_ok": bp_ok,
        "add_ok": add_ok,  # W142: +each verify 結果
        # 呼出側が DB を実 eBay 値へ同期するための反映後 snapshot.
        "post_snapshot": snap2,
    }


def _sync_db_to_actual(eid: str, snap) -> None:
    """W137: DB の price/shipping/sku を **反映後 GetItem の実 eBay 値**へ同期.

    editing の意図値でなく実 eBay 値 (post snapshot) を書くことで、
    revise の部分成功・送料 override 無音失敗・HIGH-1 などに関わらず
    DB は常に eBay の真実を映す (乖離を構造的に排除)。price/ship/sku は
    snap の値が None (GetItem に出なかった) の項目は触らない。冪等。

    W138-A (Codex#3): GetItem 成立時 (snap.ok) は shipping_profile_id +
    shipping_profile_fetched_at も同期。snap.shipping_profile_id が None
    (= 確定 Inline) でも **明示 NULL 書込** (既存 None-skip 慣習の例外。
    旧 BP id が残存すると HIGH-2 の 3 分岐 (b 確定Inline)/(c BPあり) が
    崩れるため)。id と fetched_at は同一 UPDATE で原子的に書く。
    GetItem 失敗 (not snap.ok) 時は BP 2 列を触らない (fetched_at 据置
    = 未取得状態(a) 維持、Inline と誤断定しない)。
    """
    sets = []
    params = []
    if snap.start_price_usd is not None:
        sets.append("current_price=?")
        params.append(float(snap.start_price_usd))
    if snap.ship_cost_usd is not None:
        sets.append("shipping_cost=?")
        params.append(float(snap.ship_cost_usd))
    # W142: +each (ShippingServiceAdditionalCost) を DB 同期。None-skip
    # 慣習 (shipping_cost と同型): snap に出なかった (None) 時は触らない。
    # R4 状態リセット防止: GetItem が +each を返さない listing で既知 DB
    # 値を NULL 上書きして「未取得」に劣化させない (shipping_profile_id の
    # 明示 NULL 例外とは異なり、+each の NULL は多義なので保守的に skip)。
    if getattr(snap, "ship_additional_usd", None) is not None:
        sets.append("shipping_additional_cost=?")
        params.append(float(snap.ship_additional_usd))
        sets.append("shipping_additional_fetched_at=datetime('now')")
    if snap.sku is not None:
        sets.append("sku=?")
        params.append(str(snap.sku))
    # W227 (2026-06-06): eBay 実 ConditionID を DB へ同期 (商品状態 widget の真実源)。
    # snap.condition_id は GetItem 抽出済 (ebay_listing_snapshot)。None-skip 慣習。
    # 人気度 rank 列は触らない (別軸)。これで価格/送料反映のたびに condition も
    # eBay 実値へ resync = 表示が常に eBay と一致 (誤 push 後も自己治癒)。
    if getattr(snap, "condition_id", None):
        sets.append("ebay_condition_id=?")
        params.append(str(snap.condition_id))
    if getattr(snap, "ok", False):
        sets.append("shipping_profile_id=?")
        params.append(
            str(snap.shipping_profile_id)
            if snap.shipping_profile_id else None
        )
        sets.append("shipping_profile_fetched_at=datetime('now')")
    if not sets:
        return
    sets.append("last_synced_at=datetime('now')")
    params.append(eid)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE ebay_listings SET {', '.join(sets)} "
            "WHERE ebay_item_id=?",
            tuple(params),
        )
    bump_db_version()  # W134 Step2: DB 同期 → read-cache 無効化


# =============================================================================
# Public API
# =============================================================================

def _render_sale_warning_banner() -> None:
    """W140: メモ付き listing が売れた未対応 (status='open') 警告を商品管理
    タブ最上部にバナー表示。[了解]=acked / [不要]=dismissed (誤検知)。

    Discord は初回 1 回のみ (再送なし)。本バナーが open の限り出続けるのが
    発送見落とし防止の主経路 (= Discord 送信失敗でも user は MonoDeck で
    気付ける)。detected_at は UTC 保存 → _fetched_jst_label で JST 表示
    (sqlite-timezone.md)。
    """
    warns = get_open_sale_warnings()
    if not warns:
        return
    st.warning(
        f"📎 メモ付き listing が {len(warns)} 件 売れました — "
        f"発送前にメモを必ず確認してください"
    )
    for w in warns:
        title = (w.get("title") or "").strip()
        eid = str(w.get("ebay_item_id") or "")
        label = f"{title[:60]} ({eid[-4:]})" if title else eid
        detected = _fetched_jst_label(w.get("detected_at"))
        st.markdown(
            f"- **{label}** | Order #{w.get('order_id')} | 売却 {detected}  \n"
            f"  📝 {w.get('note_snapshot') or '(メモ空)'}"
        )
        c1, c2, _sp = st.columns([1, 1, 8])
        with c1:
            if st.button("了解", key=f"pm_wack_{w['id']}",
                         help="確認済。バナーから消す (履歴は残る)"):
                ack_sale_warning(w["id"])
                bump_db_version()
                st.rerun()
        with c2:
            if st.button("不要", key=f"pm_wdis_{w['id']}",
                         help="誤検知/対応不要として消す"):
                dismiss_sale_warning(w["id"])
                bump_db_version()
                st.rerun()
    st.markdown("---")


def _build_list_dataframe(products: list[dict]) -> pd.DataFrame:
    """一覧表 (eBay連携タブと同じ st.dataframe 形式) 用の DataFrame を返す
    (W225 2026-06-05).

    行順 = products の順 (= _apply_filter_and_sort 済)。"Item ID" 列に ebay_item_id
    を持たせ、行選択後はこの列値で listing を解決する (sku-rules: SKU で束ねず
    ebay_item_id で識別)。金額・粗利は eBay連携タブと同じく整形済文字列で表示
    (見た目を揃える、計算は既存 helper を流用)。
    """
    rows: list[dict] = []
    for p in products:
        cp, sh, total = _total_price(p)
        profit = _estimate_profit_usd(p)
        cmin = p.get("competitor_min_price")
        title = p.get("title") or ""
        _eid = str(p.get("ebay_item_id") or "")
        rows.append({
            "在庫": _status_emoji(p.get("source_status")),
            "📎": "📎" if p.get("has_note") else "",
            "Title": (title[:50] + "…") if len(title) > 50 else title,
            "Item ID": _eid,
            "eBay": f"https://www.ebay.com/itm/{_eid}" if _eid else "",
            "SKU": p.get("sku") or "",
            "区分": p.get("primary_market") or "-",
            # W222: 実カテゴリ (利益計算 FVF の根拠)。未設定は "-"。
            "カテゴリ": str(p.get("category_id")) if p.get("category_id") else "-",
            # W227: eBay 実 Condition (商品状態)。人気度 rank 列は混ぜない
            # (editor/hero と一貫)。N/S/A-PO/As-Is or eBay Condition ラベル。
            "状態": (
                _condition_widget_initial(p)
                or _CONDITION_ID_LABEL.get(
                    str(p.get("ebay_condition_id") or "").strip(), "-")
            ),
            "価格": f"${cp:,.2f}",
            "送料": f"${sh:,.2f}",
            "総額": f"${total:,.2f}",
            "粗利": (
                f"{'+' if profit >= 0 else '-'}${abs(profit):,.0f}"
                if profit is not None else "—"
            ),
            "競合最安": (f"${float(cmin):,.2f}" if cmin else "—"),
            "sold": int(p.get("total_sold_count") or 0),
            "watch": int(p.get("watch_count") or 0),
        })
    return pd.DataFrame(rows)


# =============================================================================
# W#33: レガシーキーワードリスト 一括突合 UI
# =============================================================================

_LEGACY_EXPORT_PATH = Path(__file__).resolve().parent.parent / "data" / "alertcrawler_legacy_export.json"
# 良マッチの初期チェック threshold (この値以上は初期チェック ON)
_LEGACY_GOOD_MATCH_THRESHOLD = 0.6


def _load_legacy_export() -> list[dict]:
    """data/alertcrawler_legacy_export.json を読み込む. 不在は []."""
    if not _LEGACY_EXPORT_PATH.exists():
        return []
    try:
        data = json.loads(_LEGACY_EXPORT_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get("exported", [])
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[pm/W#33] legacy export read error: {e}")
        return []


def _render_legacy_bulk_match(products: list[dict]) -> None:
    """W#33 一括突合 UI (expander 内).

    - alertcrawler_legacy_export.json を読み、各 legacy keyword と
      eBay 出品 title を類似突合 → チェックボックス一覧 → 一括登録。
    - 既に keyword_watches.ebay_item_id に紐付いている listing は除外。
    - 登録は add_watch() を呼ぶ (UNIQUE で重複 skip 自動)。
    - 登録失敗・skip 件数は必ず可視化 (Q0)。
    - sku-rules: 紐付けキーは ebay_item_id (title はマッチング判定のみ)。
    """
    from tabs._keyword_watch_match import (
        LegacyEntry,
        ListingEntry,
        match_legacy_to_listings,
    )
    from monitor.keyword_watch_db import add_watch, list_watches

    with st.expander("🔔 レガシーリスト 一括監視登録 (W#33)", expanded=False):
        st.caption(
            f"旧リスト ({_LEGACY_EXPORT_PATH.name}) とアクティブ出品を類似突合し、"
            "選択分をキーワード監視に一括登録します。"
        )

        legacy_raw = _load_legacy_export()
        if not legacy_raw:
            st.info(f"{_LEGACY_EXPORT_PATH.name} が見つからないか空です。")
            return

        # 既に ebay_item_id 紐付き済みの watches を取得 (除外用)
        all_watches = list_watches(active_only=True)
        already_linked_eids: set[str] = {
            w["ebay_item_id"] for w in all_watches
            if w.get("ebay_item_id")
        }

        # LegacyEntry / ListingEntry に変換
        legacy_entries = [
            LegacyEntry(
                legacy_id=r.get("legacy_id", i),
                site=r.get("site", "yahoo_auctions"),
                search_url=r.get("search_url", ""),
                keyword=r.get("keyword", ""),
                price_min_jpy=r.get("price_min_jpy"),
                price_max_jpy=r.get("price_max_jpy"),
            )
            for i, r in enumerate(legacy_raw)
        ]

        # 既に紐付き済み listing を除外した eBay 出品
        listing_entries = [
            ListingEntry(
                ebay_item_id=str(p["ebay_item_id"]),
                title=p.get("title") or "",
            )
            for p in products
            if str(p.get("ebay_item_id") or "") not in already_linked_eids
            and p.get("title")
        ]

        if not listing_entries:
            st.info("未紐付けの listing が 0 件です (全 listing に監視設定済み)。")
            return

        # ボタン押下で突合実行。結果は session_state にキャッシュ (rerun 跨ぎで保持)。
        # Streamlit button は押した rerun でのみ True = 同一 rerun 内で matcher を走らせる。
        _btn_pressed = st.button(
            "突合を実行",
            key="pm_legacy_run_match",
            help="旧リスト × 出品 を総当たり突合します",
        )

        cached_results = st.session_state.get("pm_legacy_match_results")

        if _btn_pressed:
            # ボタン押下: 常に再突合してキャッシュ更新
            # MEDIUM-2 (code-reviewer 2026-06-20): 前回の checkbox 状態 (pm_legacy_chk_*)
            # が残ると Streamlit が value=init_checked を無視するため、再突合時に
            # クリアして良マッチの初期チェックを作り直す。
            for _k in [k for k in list(st.session_state) if k.startswith("pm_legacy_chk_")]:
                del st.session_state[_k]
            with st.spinner("突合中..."):
                results = match_legacy_to_listings(
                    legacy_entries, listing_entries, score_threshold=0.0
                )
            st.session_state["pm_legacy_match_results"] = results
        elif cached_results is not None:
            # キャッシュあり: そのまま使う
            results = cached_results
        else:
            # 初回訪問 or キャッシュなし: 突合前の案内
            st.info(
                f"旧リスト {len(legacy_entries)} 件 / 未紐付け listing {len(listing_entries)} 件。"
                "「突合を実行」を押すと候補一覧が表示されます。"
            )
            return

        if not results:
            st.info("マッチ候補が 0 件でした。")
            return

        # スコア 0 超のみ表示 (完全 unmatch は除外)
        visible = [r for r in results if r.score > 0.0]
        zero_score_count = len(results) - len(visible)
        st.caption(
            f"突合結果: {len(visible)} 件表示 "
            f"(スコア 0 除外: {zero_score_count} 件 / "
            f"良マッチ閾値 {_LEGACY_GOOD_MATCH_THRESHOLD:.0%})"
        )

        if not visible:
            st.info("スコア > 0 の候補がありません。")
            return

        # チェックボックス一覧 (良マッチは初期チェック ON)。選択状態は session_state
        # (pm_legacy_chk_*) を唯一の真実源とし、下で読み直す。
        for r in visible:
            init_checked = r.score >= _LEGACY_GOOD_MATCH_THRESHOLD
            key = f"pm_legacy_chk_{r.legacy.legacy_id}_{r.listing.ebay_item_id}"
            st.checkbox(
                f"[{r.score:.0%}] 「{r.legacy.keyword[:40]}」→ 「{r.listing.title[:50]}」"
                f" (item_id={r.listing.ebay_item_id})"
                + (" ※良マッチ" if r.score >= _LEGACY_GOOD_MATCH_THRESHOLD else ""),
                value=init_checked,
                key=key,
            )

        selected = [
            r for r in visible
            if st.session_state.get(
                f"pm_legacy_chk_{r.legacy.legacy_id}_{r.listing.ebay_item_id}", False
            )
        ]

        st.caption(f"選択: {len(selected)} 件")

        if st.button(
            f"チェック選択分 ({len(selected)} 件) を一括登録",
            key="pm_legacy_bulk_register",
            disabled=len(selected) == 0,
            type="primary",
        ):
            ok_count = 0
            skip_count = 0
            err_messages: list[str] = []

            for r in selected:
                try:
                    _, inserted_new = add_watch(
                        site=r.legacy.site,
                        search_url=r.legacy.search_url,
                        keyword=r.legacy.keyword,
                        price_min_jpy=r.legacy.price_min_jpy,
                        price_max_jpy=r.legacy.price_max_jpy,
                        memo=f"legacy_id={r.legacy.legacy_id} 一括移行",
                        source="legacy_bulk_import",
                        ebay_item_id=r.listing.ebay_item_id,
                    )
                    if inserted_new:
                        ok_count += 1
                    else:
                        skip_count += 1
                except Exception as e:
                    err_messages.append(
                        f"legacy_id={r.legacy.legacy_id}: {e}"
                    )

            # 結果サマリ (Q0: 全件可視化)
            summary_parts = [f"登録: {ok_count} 件"]
            if skip_count:
                summary_parts.append(f"重複 skip: {skip_count} 件")
            if err_messages:
                summary_parts.append(f"エラー: {len(err_messages)} 件")

            if err_messages:
                st.error("一括登録エラー\n" + "\n".join(err_messages[:5]))
            elif ok_count > 0:
                st.success(" / ".join(summary_parts))
                bump_db_version()
                # 突合キャッシュをクリアして次回再突合
                st.session_state.pop("pm_legacy_match_results", None)
                st.rerun()
            else:
                st.info(" / ".join(summary_parts))


def render_product_management(config: dict) -> None:
    """商品管理 main tab エントリーポイント."""
    # W292: 本日の作業タブからの jump 着地。pm_focus_eid を 1 度だけ消費し
    # 検索欄 (pm_search) に Item ID を seed → 表が当該 1 行に絞られる。
    # (st.dataframe は事前行選択 API が無いため検索で 1 行化 = user が即クリック可能。
    #  pop で 1 回消費 = 以後 user が検索を消しても再 seed されない。)
    # _focus を _apply_filter_and_sort に initial_search として渡す。
    # 内部で session_state["pm_search"] に直書きして seed する
    # (value= 引数は key 既存時に Streamlit が無視するため使用しない。
    #  HIGH-1 修正: _resolve_pm_search_seed + session_state 直書きパターン)。
    _focus = st.session_state.pop("pm_focus_eid", None)
    _focus_str = str(_focus) if _focus else ""
    if _focus_str:
        st.info(
            f"📝 午後の作業から遷移: Item ID `{_focus_str}` を検索欄に設定しました。"
            "下の表で行をクリックすると編集ゾーンが開きます。"
        )

    # ========================================================================
    # 商品管理タブ Design System v4 (2026-05-12 「見やすさ最大」最優先)
    # - 強コントラスト (light/dark 両対応)
    # - 大きめ font sizes
    # - 明確な border / 影
    # - colorful pill chips
    # ========================================================================
    st.markdown(
        """<style>
        /* === Design tokens (light cream 前提 (W261 2026-06-11、body bg #ede7da)) === */
        :root {
            --pm-primary:        #0e4f4b;  /* 深緑ティール、cream bg で見える */
            --pm-primary-strong: #0a3d3a;  /* ティール濃い、border 等 */
            --pm-primary-light:  #156a63;  /* ティール中間、ハイライト */
            --pm-success:        #2e7d5b;  /* 緑 */
            --pm-warning:        #b8860b;  /* 琥珀 */
            --pm-danger:         #a8341b;  /* 赤 */
            --pm-info:           #156a63;  /* ティール */
            --pm-text-dim:       #5f6557;  /* sub 文字 */
            --pm-bg-card:        rgba(166,150,121,0.10);
            --pm-bg-card2:       rgba(166,150,121,0.16);
            --pm-border:         rgba(166,150,121,0.30);
            --pm-border-strong:  rgba(166,150,121,0.45);
            --pm-shadow:         3px 3px 7px rgba(166,150,121,0.5),-3px -3px 7px rgba(255,255,255,0.9);
        }

        /* === expander caret icon を unicode 三角で代替 (2026-05-12 fix) === */
        /* 実 test-id は `stIconMaterial` (Streamlit 新版)、text content は
           "keyboard_arrow_right" / "keyboard_arrow_down". 完全に非表示 + ::before で
           ▶ / ▼ を unicode 描画. */
        /* (W258 2026-06-11: ui_themes.py へグローバル昇格済。本ブロックは重複だが無害のため残置) */
        [data-testid="stIconMaterial"] {
            display: none !important;
        }
        [data-testid="stExpander"] details summary::before {
            content: '▶';
            display: inline-block;
            margin-right: 0.6em;
            font-size: 0.85em;
            color: var(--pm-primary);
            font-weight: 700;
            transition: transform 0.15s;
        }
        [data-testid="stExpander"] details[open] summary::before {
            content: '▼';
        }

        /* === number_input +/- ステッパーを非表示 === */
        button[data-testid="stNumberInputStepUp"],
        button[data-testid="stNumberInputStepDown"],
        [data-testid="stNumberInput"] button {
            display: none !important;
        }
        [data-testid="stNumberInput"] input {
            text-align: right;
            font-variant-numeric: tabular-nums;
        }

        /* === Expander 全体: card風 + 強い border === */
        [data-testid="stExpander"] {
            margin-bottom: 14px;
        }
        [data-testid="stExpander"] details {
            border-radius: 10px;
            border: 2px solid var(--pm-border);
            background: var(--pm-bg-card);
            box-shadow: var(--pm-shadow);
            transition: all 0.2s;
        }
        [data-testid="stExpander"] details:hover {
            border-color: var(--pm-primary-light);
        }
        [data-testid="stExpander"] details[open] {
            border-color: var(--pm-primary);
            border-left-width: 5px;
        }
        [data-testid="stExpander"] details summary {
            padding: 12px 16px;
            font-size: 1.05em;
            font-weight: 600;
        }

        /* === Hero metrics row: 大きく目立つ === */
        .pm-hero-row [data-testid="stMetric"] {
            background: var(--pm-bg-card2);
            border-radius: 10px;
            padding: 12px 16px;
            border: 1px solid var(--pm-border-strong);
        }
        .pm-hero-row [data-testid="stMetricLabel"] {
            color: var(--pm-text-dim);
            font-size: 0.85em;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }
        .pm-hero-row [data-testid="stMetricValue"] {
            font-size: 1.7em !important;
            font-weight: 800 !important;
            color: var(--pm-text);
        }
        .pm-hero-row [data-testid="stMetricDelta"] {
            font-size: 0.95em;
            font-weight: 600;
        }

        /* === Section header label: 明るい indigo で contrast 強化 === */
        .pm-section-label {
            color: var(--pm-primary-light) !important;
            font-size: 1em !important;
            font-weight: 800 !important;
            margin: 20px 0 12px 0 !important;
            border-bottom: 2px solid var(--pm-primary) !important;
            padding-bottom: 6px !important;
        }

        /* === Form 内の divider === */
        [data-testid="stForm"] [data-testid="stMarkdownContainer"] hr {
            margin: 0.4em 0;
            opacity: 0.4;
        }

        /* === number_input / text_input: 大きく見やすく === */
        [data-testid="stNumberInput"] input,
        [data-testid="stTextInput"] input {
            font-size: 1.1em !important;
            font-weight: 600;
            padding: 8px 10px !important;
        }
        /* label は inherit でテーマに自然追従 (固定色しない) */
        [data-testid="stNumberInput"] label,
        [data-testid="stTextInput"] label {
            font-size: 0.95em !important;
            font-weight: 600 !important;
        }

        /* === Action buttons: 色分けで意図明確化 === */
        /* DB 保存 (1番目 = neutral): 中間グレー */
        [data-testid="stForm"] [data-testid="column"]:nth-of-type(1)
            button[data-testid="stBaseButton-secondaryFormSubmit"] {
            background: #5f6557;
            color: white;
        }
        /* eBay 反映 (2番目 = primary): indigo */
        [data-testid="stForm"] button[data-testid="stBaseButton-primaryFormSubmit"] {
            background: var(--pm-primary) !important;
            color: white;
            font-weight: 600;
        }
        /* 利益計算 (3番目 = info): emerald */
        [data-testid="stForm"] [data-testid="column"]:nth-of-type(3)
            button[data-testid="stBaseButton-secondaryFormSubmit"] {
            background: var(--pm-success);
            color: white;
        }
        /* button hover */
        [data-testid="stForm"] button:hover {
            filter: brightness(1.15);
        }

        /* === Status pill — light cream 前提、!important で Streamlit inherit を上書き === */
        .pm-pill {
            display: inline-block !important;
            padding: 4px 12px !important;
            border-radius: 14px !important;
            font-size: 0.9em !important;
            font-weight: 700 !important;
            margin: 3px 5px 3px 0 !important;
            border: 1px solid transparent;
        }
        /* OK (green): ティール文字 on ティール bg 12% */
        .pm-pill-ok {
            background: rgba(46, 125, 91, 0.15) !important;
            color: #2e7d5b !important;
            border-color: #2e7d5b !important;
        }
        /* WARN (yellow): 琥珀文字 on 琥珀 bg 12% */
        .pm-pill-warn {
            background: rgba(184, 134, 11, 0.15) !important;
            color: #7a5800 !important;
            border-color: #b8860b !important;
        }
        /* BAD (red): 赤文字 on 赤 bg 12% */
        .pm-pill-bad {
            background: rgba(168, 52, 27, 0.15) !important;
            color: #a8341b !important;
            border-color: #a8341b !important;
        }
        /* INFO (teal): ティール文字 on ティール bg 10% */
        .pm-pill-info {
            background: rgba(14, 79, 75, 0.10) !important;
            color: #0e4f4b !important;
            border-color: #156a63 !important;
        }

        /* === Dataframe: 見やすい === */
        [data-testid="stDataFrame"] {
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--pm-border);
        }

        /* === Form 枠を視覚化 === */
        [data-testid="stForm"] {
            border: 2px solid var(--pm-primary) !important;
            border-radius: 10px !important;
            padding: 16px !important;
            background: rgba(14, 79, 75, 0.05) !important;
            box-shadow: var(--pm-shadow);
        }
        [data-testid="stForm"]::before {
            content: '✎ 編集ゾーン';
            display: block;
            color: var(--pm-primary-light) !important;
            font-size: 1em;
            font-weight: 800;
            margin-bottom: 14px;
            padding-bottom: 8px;
            border-bottom: 1px dashed var(--pm-primary);
        }

        /* === Caption / 小さい text もはっきり === */
        [data-testid="stCaptionContainer"],
        small {
            color: var(--pm-text-dim) !important;
        }

        /* === W217 (2026-06-03): money-direct 編集枠 強調 === */
        /* 既存 --pm-warning (amber) / --pm-bg-card2 を流用 (新変数を作らない / K1).
           モックアップの border-left + bg + radius を踏襲し、money 系入力
           (商品価格 / 自動値下げ下限 / 送料 / 区分 / 仕入価格 / BP) を
           「誤操作注意」として視覚的に隔離。属性 (SKU/寸法/メモ) と段差をつける。 */
        .pm-edit-money {
            border-left: 4px solid var(--pm-warning);
            background: var(--pm-bg-card2);
            border-radius: 8px;
            padding: 12px 13px;
            margin-bottom: 12px;
        }
        .pm-money-head {
            font-size: 11px;
            font-weight: 700;
            color: var(--pm-warning);
            letter-spacing: 0.03em;
            margin-bottom: 9px;
        }

        /* === W217: ライバル監視 状態バッジ === */
        .pm-kw-badge {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            font-size: 11px;
            font-weight: 600;
            padding: 2px 9px;
            border-radius: 20px;
            margin-top: 4px;
        }
        .pm-kw-badge-ok {
            background: rgba(46, 125, 91, 0.15);
            color: #2e7d5b;
            border: 1px solid #2e7d5b;
        }
        .pm-kw-badge-warn {
            background: rgba(184, 134, 11, 0.15);
            color: #7a5800;
            border: 1px solid #b8860b;
        }
        .pm-kw-badge-idle {
            background: rgba(166, 150, 121, 0.12);
            color: var(--pm-text-dim);
            border: 1px solid var(--pm-border);
        }

        /* === W217: 競合差 1 行 (採算パネルと同じ色ロジック) === */
        .pm-gap-line {
            margin-top: 10px;
            font-size: 12px;
            padding: 7px 10px;
            border-radius: 6px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .pm-gap-line-bad {
            background: rgba(168, 52, 27, 0.12);
        }
        .pm-gap-line-ok {
            background: rgba(46, 125, 91, 0.12);
        }
        .pm-gap-line-eq {
            background: rgba(166, 150, 121, 0.10);
        }

        /* === W217-A: ライバル登録済 HTML table (モック準拠、最安行緑背景) === */
        .pm-rival-tbl {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
            margin: 4px 0 0 0;
            color: var(--pm-text, #2a2e2a);
        }
        .pm-rival-tbl th {
            text-align: right;
            color: var(--pm-text-dim);
            font-weight: 600;
            font-size: 10.5px;
            padding: 5px 7px;
            border-bottom: 1px solid var(--pm-border-strong);
        }
        .pm-rival-tbl th.pm-rival-th-left {
            text-align: left;
        }
        .pm-rival-tbl th.pm-rival-th-link {
            text-align: center;
            width: 50px;
        }
        .pm-rival-tbl td {
            padding: 6px 7px;
            border-bottom: 1px solid rgba(166, 150, 121, 0.18);
            text-align: right;
            font-variant-numeric: tabular-nums;
        }
        .pm-rival-tbl td.pm-rival-td-iid {
            text-align: left;
            font-family: var(--f-mono, monospace);
        }
        .pm-rival-tbl td.pm-rival-td-link {
            text-align: center;
        }
        .pm-rival-tbl td.pm-rival-td-link a {
            color: var(--pm-info);
            text-decoration: none;
            font-weight: 600;
        }
        .pm-rival-tbl td.pm-rival-td-link a:hover {
            text-decoration: underline;
        }
        /* 最安行: 緑背景 + 合計緑文字 (モックの tr.best と .tot-ok 相当) */
        .pm-rival-tbl tr.pm-rival-best td {
            background: rgba(46, 125, 91, 0.12);
        }
        .pm-rival-tbl td.pm-rival-tot-ok {
            color: var(--pm-success);
            font-weight: 700;
        }

        /* === W217-A: 金額枠 (st.container(border=True, key="pm_money_box_*")) === */
        /* Streamlit 1.36+ で st.container(key=K) は内側 div に
           class="st-key-K" を付与する (border=True は 1.29+)。これを selector
           で掴んで amber 左ラインを適用する。border=True で既存 box 枠が出る
           ため、border-left を強い amber に上書きするだけでモック「💰金額枠」
           を再現できる。requirements.txt pin = streamlit>=1.56.0。 */
        div[class*="st-key-pm_money_box_"] {
            border-left: 4px solid var(--pm-warning) !important;
            background: var(--pm-bg-card2) !important;
            border-radius: 8px !important;
        }
        /* 金額枠内の number_input / selectbox 高さをコンパクト化 (モック準拠) */
        div[class*="st-key-pm_money_box_"] [data-testid="stNumberInput"] input,
        div[class*="st-key-pm_money_box_"] [data-testid="stTextInput"] input {
            padding: 5px 8px !important;
            font-size: 1em !important;
        }
        div[class*="st-key-pm_money_box_"] [data-testid="stNumberInput"] label,
        div[class*="st-key-pm_money_box_"] [data-testid="stSelectbox"] label,
        div[class*="st-key-pm_money_box_"] [data-testid="stTextInput"] label {
            font-size: 0.78em !important;
            margin-bottom: 2px !important;
            color: var(--pm-text-dim) !important;
        }
        </style>""",
        unsafe_allow_html=True,
    )

    st.title("商品管理")
    st.caption(
        "全 active listing を表形式で一覧表示 (eBay連携タブと同じ). 行をクリックすると "
        "1 商品の基本情報 / 物理属性 / 仕入先候補 / 在庫監視 / 利益計算 / ライバル を "
        "表の下に 2 列 layout で展開. 編集 + 保存で DB 反映 + breakeven 自動再計算."
    )

    # W140: メモ付き listing 売却の未対応警告を最上部に表示 (発送見落とし防止)
    _render_sale_warning_banner()

    products = _cd_fetch_all_products(get_db_version())
    if not products:
        st.info("active listing がありません.")
        return

    # ── 統計 metric ──
    n = len(products)
    n_pyen = sum(1 for p in products if p.get("purchase_yen"))
    n_be = sum(1 for p in products if p.get("lp_breakeven_usd"))
    n_comp = sum(1 for p in products if (p.get("competitor_count") or 0) > 0)
    n_oos = sum(1 for p in products if p.get("source_status") == "out_of_stock")
    n_neg = sum(
        1 for p in products
        if _estimate_profit_usd(p) is not None and _estimate_profit_usd(p) < 0
    )

    cols = st.columns(6)
    with cols[0]:
        st.metric("総 listing", n)
    with cols[1]:
        st.metric("仕入価格設定済", f"{n_pyen} / {n}")
    with cols[2]:
        st.metric("breakeven 計算済", f"{n_be} / {n}")
    with cols[3]:
        st.metric("競合登録済", f"{n_comp} / {n}")
    with cols[4]:
        st.metric("🔴 在庫切れ", n_oos)
    with cols[5]:
        st.metric("💸 利益マイナス", n_neg, delta_color="inverse")

    st.markdown("---")

    # ── W#33: レガシー一括突合 UI ──
    _render_legacy_bulk_match(products)

    # ── フィルタ + 並び順 ──
    # W292 jump 時は _focus_str を initial_search として渡す。session_state 直書きで確実 seed
    # (value= は key 既存時に Streamlit が無視するため不使用 / HIGH-1 修正)。
    filtered = _apply_filter_and_sort(products, initial_search=_focus_str)
    st.caption(f"表示: {len(filtered)} / {n} listing")

    # ── 一覧 (W225: eBay連携タブと同じ st.dataframe 表形式。行クリックで編集) ──
    # 旧: page_items を _render_one_product でトグル縦積み (W221 アコーディオン)。
    # 新: filtered 全件を 1 つの表に出し、行選択 (single-row) でその商品の
    # 編集ゾーンを表の下に展開する。表は仮想スクロールで全件描画コストが軽い
    # ため、ページング (旧 _PAGE_SIZE) は廃止。listing 識別は ebay_item_id
    # (sku-rules: SKU で束ねない)。
    #
    # ⚠️ money-direct ガード (W225 code-reviewer HIGH-1/HIGH-2):
    #  HIGH-2: フィルタ/並び順を変えると filtered が再構成され行数・順序が変わる。
    #          dataframe の選択 (selection.rows) は widget key に紐づき rerun を跨いで
    #          残留するため、残留 index が **別 listing** を指して誤って編集ゾーンを
    #          開く恐れ (価格/送料の誤編集 = 金銭直結)。→ フィルタ/並び順の signature
    #          変化を検出したら選択を破棄 (dataframe 再描画前に widget state を削除)。
    #  HIGH-1: st.dataframe は組込みカラムソート (ヘッダクリック) を無効化できない
    #          (Streamlit 未対応)。組込みソート後は selection.rows index と視覚行が
    #          ずれ得る (streamlit#11345)。→ 解決を **表示中の Item ID 列値** で行い、
    #          編集ゾーン冒頭に「編集中 listing」を明示 (クリック行と不一致を即視認)。
    #          並び順は本タブの「並び順」selectbox を使う運用を推奨 (caption で案内)。
    _filter_sig = (
        st.session_state.get("pm_search", ""),
        st.session_state.get("pm_sort", ""),
        st.session_state.get("pm_only_missing", False),
        st.session_state.get("pm_only_no_comp", False),
        st.session_state.get("pm_only_oos", False),
        st.session_state.get("pm_only_us", False),
        st.session_state.get("pm_only_neg", False),
        st.session_state.get("pm_only_initial_pending", False),
        # W#33: キーワード監視フィルタ (変更で行集合が変わる → 選択破棄対象)
        st.session_state.get("pm_only_kw_set", False),
        st.session_state.get("pm_only_kw_unset", False),
    )
    if st.session_state.get("pm_list_filter_sig") != _filter_sig:
        st.session_state["pm_list_filter_sig"] = _filter_sig
        # フィルタ/並び順が変わった = 行集合が変わった → 残留選択を破棄
        # (dataframe 再描画前に widget state を削除し選択を空に戻す)。
        st.session_state.pop("pm_list_table", None)
    st.markdown("---")
    st.caption(
        "📋 行をクリックすると、その商品の編集ゾーンが下に開きます。"
        "並び替えは上の「並び順」を使用してください "
        "(表ヘッダのソートは選択行とずれる場合があります)。"
    )
    _list_df = _build_list_dataframe(filtered)
    _event = st.dataframe(
        _list_df,
        width="stretch",
        hide_index=True,
        height=560,
        on_select="rerun",
        selection_mode="single-row",
        key="pm_list_table",
        column_config={
            "在庫": st.column_config.TextColumn("在庫", width="small"),
            "📎": st.column_config.TextColumn("📎", width="small"),
            "Title": st.column_config.TextColumn("Title", width="large"),
            "eBay": st.column_config.LinkColumn("eBay", display_text="開く", width="small"),
            "粗利": st.column_config.TextColumn(
                "粗利", help="現在価格 − 損益分岐 (USD)。未入力は —"),
            "競合最安": st.column_config.TextColumn(
                "競合最安", help="競合の最安総額 (商品+送料)。未登録は —"),
        },
    )

    # 行選択 → その listing の編集ゾーンを表の下に描画。解決は **表示中の DataFrame の
    # Item ID 列値** (iloc[idx]) で行う = 表示と一致 (sku-rules: ebay_item_id で識別)。
    _sel_rows = list(_event.selection.rows) if _event and _event.selection else []
    st.markdown("---")
    if _sel_rows:
        _idx = _sel_rows[0]
        _sel_p = None
        _sel_eid = None
        if 0 <= _idx < len(_list_df):
            _sel_eid = str(_list_df.iloc[_idx]["Item ID"])
            _sel_p = next(
                (x for x in filtered
                 if str(x.get("ebay_item_id")) == _sel_eid),
                None,
            )
        if _sel_p is not None:
            # money-direct 確認バナー: どの listing を編集中か明示 (HIGH-1 視認防御)
            _bt = (_sel_p.get("title") or "")[:70]
            st.success(
                f"✏️ 編集中: **{_bt}** — Item ID `{_sel_eid}` "
                f"／ クリックした行と一致するか確認のうえ編集してください。"
            )
            _render_product_editor(_sel_p, config)
        else:
            st.info("選択した行の商品が見つかりません (フィルタ変更後は行を再選択してください)。")
    else:
        st.info("☝️ 上の表から商品の行をクリックすると、ここに編集ゾーンが開きます。")
