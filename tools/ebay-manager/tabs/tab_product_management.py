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
    get_japan_competitor_alerts,
    get_listing_note,
    get_open_sale_warnings,
    get_rival_discoveries,
    set_initial_registered,
    set_rival_search_keywords,
    set_rival_watch_enabled,
    update_alert_action,
    update_rival_discovery_status,
    upsert_listing_note,
)
from calculator import (
    load_settings as _load_calc_settings,
    SETTINGS_FILE as _SETTINGS_FILE,
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

_PAGE_SIZE = 25
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
                el.primary_market, el.rank, el.includes, el.warranty,
                el.total_sold_count, el.watch_count, el.view_count,
                el.source, el.source_url, el.source_status, el.source_last_checked,
                el.source_out_of_stock_since,
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
                el.rival_watch_started_at
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

def _apply_filter_and_sort(products: list[dict]) -> list[dict]:
    cols = st.columns([3, 2])
    with cols[0]:
        search = st.text_input(
            "🔍 商品名 / SKU で検索",
            key="pm_search",
            placeholder="部分一致",
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

    fcols = st.columns(6)
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

    if search:
        s = search.lower()
        products = [
            p for p in products
            if s in (p.get("title") or "").lower()
            or s in (p.get("sku") or "").lower()
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


@st.cache_data(ttl=3, show_spinner=False)
def _cd_profit_breakdown(
    price: float, pyen: float, weight_g: float,
    length_cm: float, width_cm: float, height_cm: float,
    category_id: int, settings_mtime: float, db_version: int,
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
            ), settings)
            if not res.service_results:
                return None
            return (
                max(s.profit for s in res.service_results),
                max(s.profit_with_refund for s in res.service_results),
                max(s.tax_refund for s in res.service_results),
                res.shipping_usd * settings["exchange_rate"],
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
    bd = _cd_profit_breakdown(
        float(price), float(pyen), float(wt),
        float(f_l if f_l is not None else (p.get("length_cm") or 0)),
        float(f_wd if f_wd is not None else (p.get("width_cm") or 0)),
        float(f_h if f_h is not None else (p.get("height_cm") or 0)),
        int(p.get("category_id") or 58248),
        _smt,
        get_db_version(),
    )
    if bd is None:
        return None
    return {**bd,
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
    rank = p.get("rank") or "-"
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

    st.markdown(
        f'<div style="margin: 4px 0 12px 0;">'
        f'<span class="pm-pill pm-pill-info">ID: {p["ebay_item_id"]}</span>'
        f'<span class="pm-pill pm-pill-info">SKU: {sku}</span>'
        f'<span class="pm-pill pm-pill-info">区分: {market}</span>'
        f'<span class="pm-pill pm-pill-info">Rank: {rank}</span>'
        f'<span class="pm-pill {"pm-pill-bad" if src_status == "out_of_stock" else "pm-pill-ok" if src_status == "in_stock" else "pm-pill-warn"}">仕入先: {src_emoji} {src_status}</span>'
        f'{bp_pill}'
        f'<span class="pm-pill pm-pill-info">📊 sold {sold} / watch {watch} / view {view}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 4 主要指標 (st.metric)
    # M4 fix: 未入力表記を「未入力」に統一 ("未計算" / "未取得" / "-" を排除)
    st.markdown('<div class="pm-hero-row">', unsafe_allow_html=True)
    cols = st.columns(4)
    with cols[0]:
        st.metric("現在総額", f"${total:.2f}",
                  delta=f"${cp:.2f} + 送料 ${sh:.2f}",
                  delta_color="off",
                  help="eBay 表示価格 + 送料")
    with cols[1]:
        if be and be > 0:
            st.metric("損益分岐", f"${be:.2f}",
                      help="これ以下で売ったら赤字")
        else:
            st.metric("損益分岐", "未入力",
                      help="仕入価格 + 重量 + 寸法を入力 → 利益計算ボタンで自動算出")
    with cols[2]:
        if be and be > 0:
            # W156 fix (2026-05-22 PM): 旧計算 `margin = total - be` は次元不整合バグ.
            # `be` = breakeven item_price (`compute_breakeven_price_usd` が
            # `is_ddu=False, country_code='US'` で binary search した結果, 送料込
            # 計算で profit=0 になる item_price). `total = cp + sh` で sh を足すと
            # be 内に既に組み込まれた送料を二重計上する → 黒字偽装.
            # 正しい比較は `cp` (現 item_price) vs `be` (breakeven item_price).
            # 検証例: 357039873158 cp=$95 sh=$16 be=$101.94 → 旧 +$9.06 (黒字偽)
            # → 新 -$6.94 (赤字真) + 消費税還付 ≈ ¥-37 (還付あり × USA向け と整合).
            margin = cp - be
            st.metric(
                "現在粗利", f"${margin:+.2f}",
                delta=("黒字" if margin > 0 else "赤字" if margin < 0 else "ゼロ"),
                delta_color=("normal" if margin > 0 else "inverse"),
                help=(
                    f"現 item_price ${cp:.2f} - breakeven item_price ${be:.2f} "
                    f"(送料は両側で同じく差し引き済 = 二重計上回避)"
                ),
            )
        else:
            st.metric("現在粗利", "未入力", help="breakeven 未計算")
    with cols[3]:
        if competitor_min:
            cmin = float(competitor_min)
            diff = total - cmin
            st.metric("競合最安", f"${cmin:.2f}",
                      delta=f"差 ${diff:+.2f}",
                      delta_color=("inverse" if diff > 0 else "normal"),
                      help="登録競合の総額 (商品+送料) 最安値")
        else:
            st.metric("競合最安", "未入力",
                      help="競合登録 + 価格再取得で表示")
    st.markdown('</div>', unsafe_allow_html=True)

    # ── W147: 利益サマリ (還付あり/なし × USA向け(DDP)/US以外(DDU)) ──
    # calculator が算出済の区分を hero に可視化 (計算式は不変 = 表示のみ)。
    # 主 2 値 = 還付あり (= user の実入金に最も近い前提)。primary_market 連動で
    # 非該当 (US_only→「US以外」/ global_only→「USA向け」) は淡色 + 参考値注記。
    # mixed_global / unknown / NULL は両方そのまま強調 (安全側)。
    # 区分定義の出典: reference_shipping_tariff_logic.md。
    bd = _profit_breakdown(p)
    if bd is not None:
        mk = bd["primary_market"]
        us_dim = (mk == "global_only")   # この listing は USA で売れない
        nonus_dim = (mk == "us_only")    # この listing は US 以外で売れない
        st.markdown('<div class="pm-hero-row">', unsafe_allow_html=True)
        pc = st.columns(2)
        with pc[0]:
            _render_profit_value(
                "還付あり × USA向け(DDP・関税自社負担)",
                bd["refund_us"], us_dim)
        with pc[1]:
            _render_profit_value(
                "還付あり × US以外(DDU・関税なし)",
                bd["refund_nonus"], nonus_dim)
        st.markdown('</div>', unsafe_allow_html=True)
        # Codex 2段 HIGH 対応: "US以外" を非US送料 lane と誤読させない。
        # 2 値の差 = 米国輸入関税分 (送料は両値とも US 基準)。
        st.caption(
            "2 値の差 = 米国輸入関税(Section 232)分。送料は US 基準"
            "（本システムは US 軸差分式）・US以外は関税なし(DDU)前提。"
        )
        if us_dim or nonus_dim:
            _w = "USA向け" if us_dim else "US以外"
            # 区分名は pill (区分: {market}) と同じ生値で表示 (大小文字不一致防止)
            st.caption(
                f"※ この listing は区分 **{market}** のため"
                f"「{_w}」は参考値（薄字）です。"
            )
        with st.expander("利益内訳（還付なし・税還付・関税）",
                         expanded=False):
            st.markdown(
                f"- 還付なし × USA向け (DDP): "
                f"**¥{bd['noref_us']:+,.0f}**\n"
                f"- 還付なし × US以外 (DDU): "
                f"**¥{bd['noref_nonus']:+,.0f}**\n"
                f"- 消費税還付額（目安）: ¥{bd['tax_refund']:,.0f}\n"
                f"- 米国向け関税コスト（DDP・売主負担）: "
                f"¥{bd['ddp_cost_jpy']:,.0f}"
            )
            st.caption(
                "利益は現在の eBay 表示価格・最良送料サービス基準。"
                "calculator と同じ計算式（W147 は表示のみ）。"
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
    """左列: SKU / 在庫数 / 物理属性 / eBay 出品 / 仕入価格 編集 form (form 内呼出前提)."""
    eid = p["ebay_item_id"]
    editing: dict = {}

    # ── 🏷️ SKU + 在庫数 (stock prefix のみ在庫数表示) ──
    st.markdown('<div class="pm-section-label">🏷️ SKU + 在庫数</div>',
                unsafe_allow_html=True)
    sku_col, inv_col = st.columns(2)
    with sku_col:
        current_sku = p.get("sku") or ""
        editing["sku"] = st.text_input(
            "SKU",
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
            st.caption("無在庫 SKU (在庫数管理対象外)")
            editing["inventory_count"] = None

    # ── 📦 物理属性 ──
    st.markdown('<div class="pm-section-label">📦 物理属性 (送料計算 + breakeven に必須)</div>',
                unsafe_allow_html=True)
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

    # ── 💵 eBay 出品 ──
    st.markdown(
        '<div class="pm-section-label">💵 eBay 出品 (📤 eBay 反映で ReviseFixedPriceItem)</div>',
        unsafe_allow_html=True,
    )
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

    # ── 💰 仕入価格 + 下限価格 ──
    st.markdown(
        '<div class="pm-section-label">💰 仕入価格 + 下限価格</div>',
        unsafe_allow_html=True,
    )
    p1, p2 = st.columns(2)
    with p1:
        editing["purchase_yen"] = st.number_input(
            "仕入価格 (JPY)",
            min_value=0,
            value=int(p["purchase_yen"]) if p.get("purchase_yen") else None,
            step=100, key=f"pm_pyen_{eid}",
        )
    with p2:
        editing["lp_min_price"] = st.number_input(
            "下限価格 (USD)",
            min_value=0.0,
            value=float(p["lp_min_price"]) if p.get("lp_min_price") else None,
            step=1.0, format="%.2f",
            key=f"pm_minp_{eid}",
            help="W183 自動値下げの絶対下限. 未入力なら breakeven が下限.",
        )

    # ── 📎 listing メモ (W140) ──
    # eBay へは送信せず MonoDeck DB のみ保持。この listing が売れたら発送前に
    # MonoDeck バナー + Discord で警告 (発送/通関の注意点の見落とし防止)。
    # メモは ebay_item_id 紐付 (sku-rules: SKU をキーにしない)。自動再出品
    # (End→Sell similar) では inherit_listing_on_relist が旧→新へ引き継ぐ。
    st.markdown(
        '<div class="pm-section-label">📎 listing メモ '
        '(発送/通関の注意点・売れたら警告)</div>',
        unsafe_allow_html=True,
    )
    editing["note_text"] = st.text_area(
        "listing メモ",
        value=get_listing_note(eid) or "",
        key=f"pm_note_{eid}",
        max_chars=2000,
        height=80,
        help="例: 電池を抜いて発送 / 通関書類に型番XXX明記。eBay には送信"
             "されません。保存後この listing が売れると MonoDeck と Discord "
             "に通知。空にして保存でメモ削除。",
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

    with st.expander("🔗 新規 URL 直接投入 → 画像 + description 生成 → eBay 反映",
                     expanded=False):
        st.caption(
            "仕入先 URL (メルカリ / ヤフオク / 楽天 等) を投入すると、個別出品と "
            "同じ流れで scrape + rank 自動推定 + description HTML 生成. "
            "preview を確認後、画像加工 (Step B-D) + 3 通りの反映 button で運用 (W158, 2026-05-23)."
        )
        url_input = st.text_input(
            "新規 URL",
            value=st.session_state.get(url_key, ""),
            key=url_key,
            placeholder="https://jp.mercari.com/item/m... or https://page.auctions.yahoo.co.jp/...",
        )
        if st.button(
            "① 画像 + description 生成 (eBay 反映はまだ)",
            key=f"pm_url_direct_gen_{eid}",
            disabled=not url_input.strip(),
        ):
            with st.spinner("仕入先 scrape + Claude rank 推定 + description 生成中..."):
                prefetch = prefetch_supplier_product_and_rank(0, url_input.strip())
                if not prefetch.get("success"):
                    st.error(f"❌ 取得失敗: {prefetch.get('message') or '(原因不明)'}")
                    st.session_state.pop(result_key, None)
                    return
                gen = generate_supplier_description(
                    candidate_id=0,
                    candidate_url=url_input.strip(),
                    in_stock=is_in_stock,
                    prefetched_product=prefetch.get("product"),
                    rank_override_code=None,
                )
                if not gen.get("success"):
                    st.error(f"❌ description 生成失敗: {gen.get('message') or '(原因不明)'}")
                    st.session_state.pop(result_key, None)
                    return
                product = prefetch.get("product")
                st.session_state[result_key] = {
                    "url": url_input.strip(),
                    "title_ja": getattr(product, "title_ja", "") or "",
                    "title_en": gen.get("title_en") or "",
                    "rank_code": gen.get("rank_code") or "",
                    "rank_reasoning": prefetch.get("rank_reasoning") or "",
                    "image_urls": list(getattr(product, "image_urls", []) or []),
                    "description_html": gen.get("description_html") or "",
                    "message": gen.get("message") or "",
                }
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

            with st.expander(
                f"description HTML preview ({len(result.get('description_html', ''))} 文字)",
                expanded=False,
            ):
                st.code(result.get("description_html", "")[:5000], language="html")
                if len(result.get("description_html", "")) > 5000:
                    st.caption("⚠️ 先頭 5000 文字のみ表示 (eBay 反映は全文)")

            # ── W158 (2026-05-23): 画像加工 + 3 反映 button (個別出品同等) ──
            from tabs._image_pipeline_ui import (
                render_image_pipeline_section, clear_pipeline_keys,
            )
            from tabs._supplier_description_pipeline import apply_listing_update_to_ebay

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
                description_html=result.get("description_html") or "",
                on_apply_image=_on_apply_image_pm,
                on_apply_description=_on_apply_desc_pm,
                on_apply_both=_on_apply_both_pm,
            )

            if st.button("結果をクリア", key=f"pm_url_direct_clear_{eid}"):
                clear_pipeline_keys(w158_prefix)
                st.session_state.pop(result_key, None)
                st.rerun()


def _render_right_inventory_supplier_rival(p: dict, config: dict) -> None:
    """右列: 在庫監視 + 仕入先候補 + ライバル dataframe."""
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

    st.markdown("---")

    # ── 🎯 ライバル dataframe ──
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
        df = pd.DataFrame(df_rows)
        st.dataframe(
            df,
            column_config={
                "item id": st.column_config.TextColumn("item id", width="small"),
                "リンク": st.column_config.LinkColumn(
                    "リンク", display_text="開く", width="small",
                ),
                "商品価格": st.column_config.NumberColumn("商品価格", format="$%.2f", width="small"),
                "送料": st.column_config.NumberColumn("送料", format="$%.2f", width="small"),
                "合計": st.column_config.NumberColumn("合計", format="$%.2f", width="small"),
                "発送目安": st.column_config.TextColumn("発送目安", width="small"),
                "最終取得": st.column_config.TextColumn("最終取得", width="medium"),
            },
            hide_index=True,
            use_container_width=True,
            key=f"pm_pricing_df_{eid}",
        )

    # ライバル item id 編集 (max 10、横一列 5×2)
    st.caption("**ライバル item id 編集** (eBay 12-13 桁、空欄で削除)")
    existing_ids = [r["competitor_item_id"] for r in pricing_rows]
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

    if pricing_rows and st.button(
        "🔄 ライバル価格 再取得 (Browse API)",
        key=f"pm_refresh_comp_{eid}", use_container_width=True,
    ):
        with st.spinner("Browse API で価格再取得中..."):
            try:
                result = refresh_competitor_pricing(eid, config or {})
                f, fl = result.get("fetched", 0), result.get("failed", 0)
                if f == 0 and fl == 0:
                    st.info("登録ライバルなし")
                else:
                    st.success(f"取得成功 {f} / 失敗 {fl}")
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

    btn_cols = st.columns([1, 1, 1])
    with btn_cols[0]:
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
                    from tasks.task_rival_detection import (
                        run_rival_per_listing_detection_one,
                    )
                    res = run_rival_per_listing_detection_one(
                        eid, config,
                        query_override=q,
                        sleep_between=0.0,  # M-internal-7: UI 経路 0
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

def _render_one_product(p: dict, config: dict) -> None:
    """1 商品 expander + 2 列 layout + st.form (rerun 抑制) + 3 submit buttons.

    UI 改修 (2026-05-11 v3):
      - 編集 inputs は `st.form` で囲み、submit まで rerun が走らない (user 入力中の画面暗化解消)
      - submit button 3 種: 💾 DB保存 / 📤 DB + eBay 反映 / 💡 利益計算 (breakeven 再計算)
      - 保存後の expander 状態維持: session_state['pm_keep_open_eid'] で次回 expanded=True
    """
    eid = p["ebay_item_id"]
    header = _build_expander_header(p)

    # 保存後 expander 開き続けるための state (1 回 render で消費)
    keep_open_eid = st.session_state.get("pm_keep_open_eid")
    is_open = (eid == keep_open_eid)

    with st.expander(header, expanded=is_open):
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

        # W153 (2026-05-22): ライバル監視 section (form 外、個別 button 即時反応).
        _render_rival_watch_section(p, config)

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

        # ── 2 列 layout: 左 (form 内) / 右 (form 外) ──
        left, right = st.columns([1, 1], gap="medium")

        with left:
            # 左列: 編集 inputs + submit buttons (rerun 抑制)
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

        with right:
            # 右列 (form 外): dataframes + action button 群
            _render_right_inventory_supplier_rival(p, config)

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

            # 保存後も expander を開いたままにする
            st.session_state["pm_keep_open_eid"] = eid


def _money_eq(a: Optional[float], b: Optional[float]) -> bool:
    """USD 金額の 0.01 丸め一致 (None はどちらかでも不一致)."""
    if a is None or b is None:
        return False
    return round(float(a), 2) == round(float(b), 2)


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


def render_product_management(config: dict) -> None:
    """商品管理 main tab エントリーポイント."""
    # ========================================================================
    # 商品管理タブ Design System v4 (2026-05-12 「見やすさ最大」最優先)
    # - 強コントラスト (light/dark 両対応)
    # - 大きめ font sizes
    # - 明確な border / 影
    # - colorful pill chips
    # ========================================================================
    st.markdown(
        """<style>
        /* === Design tokens (dark theme 前提固定、Streamlit body bg #1A1817) === */
        :root {
            --pm-primary:        #818CF8;  /* indigo-400 light、dark bg で見える */
            --pm-primary-strong: #4F46E5;  /* indigo-600、border 等 */
            --pm-primary-light:  #A5B4FC;  /* indigo-300、ハイライト */
            --pm-success:        #34D399;  /* emerald-400 */
            --pm-warning:        #FBBF24;  /* amber-400 */
            --pm-danger:         #F87171;  /* red-400 */
            --pm-info:           #60A5FA;  /* blue-400 */
            --pm-text-dim:       #9CA3AF;  /* slate-400 */
            --pm-bg-card:        rgba(255,255,255,0.04);
            --pm-bg-card2:       rgba(255,255,255,0.07);
            --pm-border:         rgba(255,255,255,0.12);
            --pm-border-strong:  rgba(255,255,255,0.2);
            --pm-shadow:         0 2px 6px rgba(0,0,0,0.3);
        }

        /* === expander caret icon を unicode 三角で代替 (2026-05-12 fix) === */
        /* 実 test-id は `stIconMaterial` (Streamlit 新版)、text content は
           "keyboard_arrow_right" / "keyboard_arrow_down". 完全に非表示 + ::before で
           ▶ / ▼ を unicode 描画. */
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
        /* DB 保存 (1番目 = neutral): slate */
        [data-testid="stForm"] [data-testid="column"]:nth-of-type(1)
            button[data-testid="stBaseButton-secondaryFormSubmit"] {
            background: #475569;
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

        /* === Status pill — dark theme 前提、!important で Streamlit inherit を上書き === */
        .pm-pill {
            display: inline-block !important;
            padding: 4px 12px !important;
            border-radius: 14px !important;
            font-size: 0.9em !important;
            font-weight: 700 !important;
            margin: 3px 5px 3px 0 !important;
            border: 1px solid transparent;
        }
        /* OK (green): emerald-200 text on emerald-500 bg 20% */
        .pm-pill-ok {
            background: rgba(16, 185, 129, 0.22) !important;
            color: #A7F3D0 !important;
            border-color: #34D399 !important;
        }
        /* WARN (yellow): amber-200 text on amber-500 bg 20% */
        .pm-pill-warn {
            background: rgba(245, 158, 11, 0.22) !important;
            color: #FDE68A !important;
            border-color: #FBBF24 !important;
        }
        /* BAD (red): red-200 text on red-500 bg 20% */
        .pm-pill-bad {
            background: rgba(239, 68, 68, 0.22) !important;
            color: #FECACA !important;
            border-color: #F87171 !important;
        }
        /* INFO (blue): blue-200 text on blue-500 bg 20% */
        .pm-pill-info {
            background: rgba(59, 130, 246, 0.22) !important;
            color: #BFDBFE !important;
            border-color: #60A5FA !important;
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
            background: rgba(129, 140, 248, 0.06) !important;
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
        </style>""",
        unsafe_allow_html=True,
    )

    st.title("商品管理")
    st.caption(
        "全 active listing を一覧表示. expander で展開すると 1 商品の "
        "基本情報 / 物理属性 / 仕入先候補 / 在庫監視 / 利益計算 / ライバル を 2 列 layout で表示. "
        "編集 + 保存で DB 反映 + breakeven 自動再計算."
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

    # ── フィルタ + 並び順 ──
    filtered = _apply_filter_and_sort(products)
    st.caption(f"表示: {len(filtered)} / {n} listing")

    # ── ページング ──
    total_pages = max(1, (len(filtered) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = st.number_input(
        "ページ",
        min_value=1,
        max_value=total_pages,
        value=1,
        key="pm_page",
    )
    start = (int(page) - 1) * _PAGE_SIZE
    page_items = filtered[start : start + _PAGE_SIZE]
    st.caption(f"ページ {int(page)} / {total_pages} ({len(page_items)} 件表示)")

    # ── 一覧 ──
    st.markdown("---")
    for p in page_items:
        _render_one_product(p, config)
