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
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from monitor.database import (
    get_conn,
    get_japan_competitor_alerts,
    update_alert_action,
)
from calculator import load_settings as _load_calc_settings
from monitor.credentials import get_ebay_credentials
from monitor.ebay_client import revise_fixed_price_with_shipping
from monitor.lowest_price import (
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
                el.competitor_min_price,
                el.quantity_ebay, el.inventory_count,
                el.last_qty_sync_at, el.last_synced_quantity, el.qty_sync_error,
                (SELECT COUNT(*) FROM competitor_products cp
                 WHERE cp.our_item_id = el.ebay_item_id AND cp.is_active = 1
                ) AS competitor_count
            FROM ebay_listings el
            WHERE (el.is_ended IS NULL OR el.is_ended = 0)
              AND el.title IS NOT NULL AND el.title != ''
            ORDER BY el.ebay_item_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


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
    """簡易粗利 = sale_price - breakeven. None で current_price を sale 価格として使う."""
    be = p.get("lp_breakeven_usd")
    if not be or be <= 0:
        return None
    sp = sale_price_usd if sale_price_usd is not None else (p.get("current_price") or 0)
    sh = p.get("shipping_cost") or 0
    total = float(sp) + float(sh)
    return total - float(be)


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

    fcols = st.columns(5)
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

def _render_hero_metrics(p: dict) -> None:
    """商品 expander の最上部に表示する 4 つの主要指標.

    [現在総額] [損益分岐] [現在粗利] [競合最安]

    視覚的に最も重要な情報を一目で把握できるよう、大きな metric card で表示.
    """
    cp, sh, total = _total_price(p)
    be = p.get("lp_breakeven_usd")
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

    st.markdown(
        f'<div style="margin: 4px 0 12px 0;">'
        f'<span class="pm-pill pm-pill-info">ID: {p["ebay_item_id"]}</span>'
        f'<span class="pm-pill pm-pill-info">SKU: {sku}</span>'
        f'<span class="pm-pill pm-pill-info">区分: {market}</span>'
        f'<span class="pm-pill pm-pill-info">Rank: {rank}</span>'
        f'<span class="pm-pill {"pm-pill-bad" if src_status == "out_of_stock" else "pm-pill-ok" if src_status == "in_stock" else "pm-pill-warn"}">仕入先: {src_emoji} {src_status}</span>'
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
            margin = total - be
            st.metric(
                "現在粗利", f"${margin:+.2f}",
                delta=("黒字" if margin > 0 else "赤字" if margin < 0 else "ゼロ"),
                delta_color=("normal" if margin > 0 else "inverse"),
                help=f"現在総額 ${total:.2f} - breakeven ${be:.2f}",
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


def _render_left_basic_and_physical(p: dict, config: dict) -> dict:
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
        editing["new_ship_additional"] = st.number_input(
            "送料 +each (USD)",
            min_value=0.0,
            value=None,
            step=0.5, format="%.2f",
            key=f"pm_ebay_ship_add_{eid}",
            help="2 個目以降の追加送料 (ShippingServiceAdditionalCost)",
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

    return editing


# =============================================================================
# Right column sections
# =============================================================================

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
    comp_inputs: list[str] = []
    rows_count = (_MAX_COMPETITORS + 4) // 5
    for r in range(rows_count):
        cols = st.columns(5)
        for c in range(5):
            idx = r * 5 + c
            if idx >= _MAX_COMPETITORS:
                break
            with cols[c]:
                cur = existing_ids[idx] if idx < len(existing_ids) else ""
                val = st.text_input(
                    f"#{idx + 1}",
                    value=cur,
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

    # 新規発見ライバル alerts
    _render_new_alerts_for_listing(p, config, existing_ids)

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

        selected = [
            (row["item id"]) for _, row in edited_df.iterrows()
            if row.get("✅") and row.get("item id")
        ]

        st.caption(
            f"選択中: {len(selected)} 件 / 追加後の合計: "
            f"{len(registered_ids) + len(selected)} / {_MAX_COMPETITORS}"
        )

        if st.button(
            f"✅ 選択した {len(selected)} 件を追加登録 (既存 maintain)",
            key=f"pm_cli_add_{eid}",
            disabled=(len(selected) == 0 or len(selected) > n_slots_left),
            type="primary", use_container_width=True,
        ):
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
    with get_conn() as conn:
        # SKU 編集 (空文字は許可しない、変更時のみ)
        new_sku = editing.get("sku")
        if new_sku and new_sku.strip():
            conn.execute(
                "UPDATE ebay_listings SET sku=? WHERE ebay_item_id=?",
                (new_sku.strip(), ebay_item_id),
            )
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

    return f"{src} {title} | \\${total:.2f}{profit_str}"


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

        # ── Hero metrics row: 4 主要指標を上部に大きく表示 ──
        _render_hero_metrics(p)

        # ── 2 列 layout: 左 (form 内) / 右 (form 外) ──
        left, right = st.columns([1, 1], gap="medium")

        with left:
            # 左列: 編集 inputs + submit buttons (rerun 抑制)
            with st.form(key=f"pm_form_{eid}", clear_on_submit=False):
                editing = _render_left_basic_and_physical(p, config)
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
                ebay_result = _apply_to_ebay(eid, editing, config)
                if ebay_result["success"]:
                    # 「\$」エスケープで markdown LaTeX 化を回避.
                    np = ebay_result.get('new_price') or 0.0
                    nsh = ebay_result.get('new_ship')
                    nsh_str = f"\\${float(nsh):.2f}" if nsh is not None else "-"
                    messages.append(
                        f"eBay 反映成功: 価格 \\${float(np):.2f} / 送料 {nsh_str}"
                    )
                    # eBay 成功時のみ ebay_listings.current_price / shipping_cost を更新
                    _update_ebay_reflected_fields(eid, editing)
                else:
                    st.error(f"eBay 反映失敗: {ebay_result.get('message', '不明')}")
                    # H5 fix (2026-05-11 code-reviewer): エラー時も expander を開いた
                    # ままにする (user が失敗原因を再確認できるよう).
                    st.session_state["pm_keep_open_eid"] = eid
                    # eBay 失敗時は DB 保存も止める (整合性優先)
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


def _apply_to_ebay(eid: str, editing: dict, config: dict) -> dict:
    """eBay 反映: ReviseFixedPriceItem で価格 + 送料 + 追加送料を更新."""
    new_price = editing.get("new_ebay_price")
    new_ship = editing.get("new_ship_cost")
    new_add = editing.get("new_ship_additional")
    # 価格・送料どちらか変更がなければ skip
    if (new_price is None or new_price <= 0) and new_ship is None:
        return {"success": False, "message": "価格・送料 どちらも未入力"}

    try:
        creds = get_ebay_credentials(config or {})
        app_id = creds.get("app_id", "")
        dev_id = creds.get("dev_id", "")
        cert_id = creds.get("cert_id", "")
        token = creds.get("user_token", "")
        if not (app_id and dev_id and cert_id and token):
            return {"success": False, "message": "eBay credentials 不在"}
    except (KeyError, ValueError, OSError) as e:
        logger.exception("[pm] credentials 取得エラー")
        return {"success": False, "message": f"credentials 取得エラー: {e}"}

    result = revise_fixed_price_with_shipping(
        item_id=eid,
        new_price_usd=float(new_price) if new_price else None,
        ship_cost_usd=float(new_ship) if new_ship is not None else None,
        ship_additional_usd=float(new_add) if new_add is not None else None,
        app_id=app_id, dev_id=dev_id, cert_id=cert_id, user_token=token,
    )
    return {
        "success": bool(result.get("success")),
        "message": result.get("message", ""),
        "new_price": new_price,
        "new_ship": new_ship,
    }


def _update_ebay_reflected_fields(eid: str, editing: dict) -> None:
    """eBay 反映成功後、ebay_listings.current_price / shipping_cost を DB 同期."""
    new_price = editing.get("new_ebay_price")
    new_ship = editing.get("new_ship_cost")
    with get_conn() as conn:
        if new_price is not None and new_price > 0:
            conn.execute(
                "UPDATE ebay_listings SET current_price=?, last_synced_at=datetime('now') "
                "WHERE ebay_item_id=?",
                (float(new_price), eid),
            )
        if new_ship is not None:
            conn.execute(
                "UPDATE ebay_listings SET shipping_cost=? WHERE ebay_item_id=?",
                (float(new_ship), eid),
            )


# =============================================================================
# Public API
# =============================================================================

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

    products = _fetch_all_products()
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
