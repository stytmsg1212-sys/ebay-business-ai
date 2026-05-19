#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W119 補助 UI: 商品データ FIX (per-listing 編集).

最安値チェックタブの先頭に配置. 全 active listing で
仕入価格 / 重量 / 寸法 / lp_min_price / 競合 item id を listing 単位で編集できる
ダッシュボード + 編集 form.

設計核心:
- 統計 bar で FIX 状況を可視化
- フィルタ: 未 FIX のみ / 検索ワード / 並び順
- ページング (30 件/page) で大規模 listing 対応
- 各 listing は expander で展開 → 編集 form
- 保存時に breakeven 自動再計算 (Q1 整合)

最初のイテレーション. user が見てフィードバック後に拡張する想定.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

import streamlit as st

from ui_cache import bump_db_version, seed_keyed_list_from_db
from monitor.database import get_conn
from monitor.lowest_price import (
    fetch_supplier_purchase_yen,
    set_listing_lowest_price_fields,
    update_listing_breakeven,
    upsert_listing_competitors,
)

logger = logging.getLogger(__name__)

# ── 2026-05-18 Bug B 修正 (未FIXフィルタ作業中に保存/反映でページ消失) ──
# 真因: パネルが st.expander(expanded=False) ハードコードで再実行毎に畳まれ、
# かつ「未FIXのみ」フィルタが保存後の新DB値で再適用 → FIX済になった編集中
# listing が一覧から脱落して「修正中ページが消えた」ように見える。
# 対策キー: 編集済 listing をセッション中ピン留め / パネル開維持 / 直近編集
# listing を開いたまま / 保存メッセージを rerun 跨ぎ表示.
_FIX_EDITED_KEY = "_w119_fix_session_edited_ids"  # set[ebay_item_id]
_FIX_PANEL_OPEN_KEY = "_w119_fix_panel_open"
_FIX_OPEN_EID_KEY = "_w119_fix_last_edited_eid"
_FIX_MSG_KEY = "_w119_fix_pending_msg"


def _fix_flush_msg() -> None:
    m = st.session_state.pop(_FIX_MSG_KEY, None)
    if m:
        (st.success if m[0] == "ok" else st.error)(m[1])

_PAGE_SIZE = 30
_MAX_COMPETITORS = 10


# =============================================================================
# Data fetch
# =============================================================================

def _fetch_all_listings_for_fix() -> list[dict]:
    """全 active listing の FIX 関連フィールドを取得."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ebay_item_id, sku, title, current_price, shipping_cost,
                   weight_g, length_cm, width_cm, height_cm,
                   purchase_yen, lp_min_price, lp_breakeven_usd,
                   primary_market, rank, includes, warranty,
                   total_sold_count, watch_count, view_count
            FROM ebay_listings
            WHERE (is_ended IS NULL OR is_ended=0)
              AND title IS NOT NULL AND title != ''
            ORDER BY ebay_item_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_competitors_for_listing(ebay_item_id: str) -> list[str]:
    """指定 listing の active 競合 item_id 一覧 (登録順)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT competitor_item_id FROM competitor_products
               WHERE our_item_id=? AND is_active=1
               ORDER BY id ASC""",
            (ebay_item_id,),
        ).fetchall()
    return [r[0] for r in rows]


# =============================================================================
# Statistics
# =============================================================================

def _compute_stats(listings: list[dict]) -> dict:
    n = len(listings)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        # 0 は設定済み (2026-05-19 user 指示、_render_status_chips と整合)
        "pyen_done": sum(
            1 for it in listings if it.get("purchase_yen") is not None),
        "weight_done": sum(
            1 for it in listings if it.get("weight_g") is not None),
        "dim_done": sum(
            1 for it in listings
            if it.get("length_cm") is not None
            and it.get("width_cm") is not None
            and it.get("height_cm") is not None
        ),
        "breakeven_done": sum(
            1 for it in listings if it.get("lp_breakeven_usd") is not None),
        "min_price_done": sum(
            1 for it in listings if it.get("lp_min_price") is not None),
    }


def _render_stats(stats: dict) -> None:
    n = stats["n"]
    if n == 0:
        return
    cols = st.columns(5)
    items = [
        ("仕入価格", stats["pyen_done"]),
        ("重量", stats["weight_done"]),
        ("寸法", stats["dim_done"]),
        ("損益分岐", stats["breakeven_done"]),
        ("下限価格", stats["min_price_done"]),
    ]
    for col, (label, done) in zip(cols, items):
        with col:
            pct = (done / n * 100) if n else 0
            st.metric(label, f"{done} / {n}", f"{pct:.0f}%")


# =============================================================================
# Filter + sort
# =============================================================================

def _apply_filter(listings: list[dict]) -> list[dict]:
    # Bug B 修正: フィルタ前の全 listing を保持 (編集済行のピン留め用)
    _all_map = {it["ebay_item_id"]: it for it in listings}

    # 検索ワード
    search = st.text_input(
        "🔍 商品名で検索",
        key="w119_fix_search",
        placeholder="商品名の一部を入力",
    )
    if search:
        search_lc = search.lower()
        listings = [it for it in listings if search_lc in (it.get("title") or "").lower()]

    # 未 FIX フィルタ
    miss_cols = st.columns(5)
    with miss_cols[0]:
        miss_pyen = st.checkbox("仕入価格 未 FIX のみ", key="w119_fix_miss_pyen")
    with miss_cols[1]:
        miss_weight = st.checkbox("重量 未 FIX のみ", key="w119_fix_miss_weight")
    with miss_cols[2]:
        miss_dim = st.checkbox("寸法 未 FIX のみ", key="w119_fix_miss_dim")
    with miss_cols[3]:
        miss_be = st.checkbox("breakeven 未計算のみ", key="w119_fix_miss_be")
    with miss_cols[4]:
        miss_min = st.checkbox("下限価格 未 FIX のみ", key="w119_fix_miss_min")

    # 0 は設定済み扱い = 未FIX フィルタに残さない (2026-05-19 user 指示、
    # _render_status_chips / _compute_stats と整合)。
    if miss_pyen:
        listings = [it for it in listings if it.get("purchase_yen") is None]
    if miss_weight:
        listings = [it for it in listings if it.get("weight_g") is None]
    if miss_dim:
        listings = [
            it for it in listings
            if (it.get("length_cm") is None or it.get("width_cm") is None
                or it.get("height_cm") is None)
        ]
    if miss_be:
        listings = [it for it in listings if it.get("lp_breakeven_usd") is None]
    if miss_min:
        listings = [it for it in listings if it.get("lp_min_price") is None]

    # 並び順
    sort_key = st.selectbox(
        "並び順",
        options=["ABC 順 (商品名)", "高単価 (現在価格)", "売れ筋 (total_sold_count)",
                 "watch_count 順", "view_count 順", "ID 順"],
        key="w119_fix_sort",
    )
    if sort_key == "ABC 順 (商品名)":
        listings.sort(key=lambda x: (x.get("title") or "").lower())
    elif sort_key == "高単価 (現在価格)":
        listings.sort(key=lambda x: -(x.get("current_price") or 0))
    elif sort_key == "売れ筋 (total_sold_count)":
        listings.sort(key=lambda x: -(x.get("total_sold_count") or 0))
    elif sort_key == "watch_count 順":
        listings.sort(key=lambda x: -(x.get("watch_count") or 0))
    elif sort_key == "view_count 順":
        listings.sort(key=lambda x: -(x.get("view_count") or 0))
    # ID 順は既に order by で適用済

    # Bug B 修正: 本セッションで編集した listing は、未FIXフィルタで
    # FIX済になっても一覧から脱落させない (修正直後にページから消える対策).
    # 全 listing から拾い直して先頭にピン留め (作業継続性を担保).
    edited = st.session_state.get(_FIX_EDITED_KEY) or set()
    if edited:
        present = {it["ebay_item_id"] for it in listings}
        pinned = [
            _all_map[e] for e in edited
            if e in _all_map and e not in present
        ]
        if pinned:
            listings = pinned + listings

    return listings


# =============================================================================
# Per-listing edit form
# =============================================================================

def _render_status_chips(it: dict) -> str:
    """listing の FIX 状況を chip 文字列で返す (expander タイトル用)."""
    chips: list[str] = []
    # 0 と空白(None)を区別 (2026-05-19 user 指示): US_only 送料等を **あえて
    # 0** に設定する運用があるため、0 は「設定済み」。未設定 (DB NULL→None)
    # のみ未FIX扱いとする (falsy `not x` は 0 も未設定扱いになり誤判定)。
    if it.get("purchase_yen") is None:
        chips.append("⚠️ 仕入¥")
    if it.get("weight_g") is None:
        chips.append("⚠️ 重量")
    if (it.get("length_cm") is None or it.get("width_cm") is None
            or it.get("height_cm") is None):
        chips.append("⚠️ 寸法")
    if it.get("lp_breakeven_usd") is None:
        chips.append("⚠️ breakeven")
    return " ".join(chips) if chips else "✅ 全 FIX 済"


def _render_edit_form(it: dict, config: dict) -> None:
    """1 listing の編集 form. expander 内で描画."""
    ebay_item_id = it["ebay_item_id"]

    # ── 基本情報 (read-only) ──
    cp = float(it.get("current_price") or 0)
    sh = float(it.get("shipping_cost") or 0)
    total = cp + sh
    info_cols = st.columns(4)
    with info_cols[0]:
        st.markdown(f"**ID**  \n`{ebay_item_id}`")
    with info_cols[1]:
        st.markdown(f"**SKU**  \n`{it.get('sku', '')}`")
    with info_cols[2]:
        st.markdown(f"**区分**  \n{it.get('primary_market') or '-'}")
    with info_cols[3]:
        st.markdown(f"**現在総額**  \n${cp:.2f} + ${sh:.2f} = **${total:.2f}**")

    # ── 必須データ編集 ──
    st.markdown("##### 📦 必須データ (breakeven 計算に使用)")
    edit_cols = st.columns(5)
    with edit_cols[0]:
        pyen = st.number_input(
            "仕入価格 (JPY)",
            min_value=0,
            value=int(it["purchase_yen"]) if it.get("purchase_yen") else None,
            step=100,
            key=f"w119_fix_pyen_{ebay_item_id}",
        )
    with edit_cols[1]:
        weight = st.number_input(
            "重量 (g)",
            min_value=0,
            value=int(it["weight_g"]) if it.get("weight_g") else None,
            step=10,
            key=f"w119_fix_weight_{ebay_item_id}",
        )
    with edit_cols[2]:
        length = st.number_input(
            "長さ (cm)",
            min_value=0.0,
            value=float(it["length_cm"]) if it.get("length_cm") else None,
            step=1.0,
            key=f"w119_fix_length_{ebay_item_id}",
        )
    with edit_cols[3]:
        width = st.number_input(
            "幅 (cm)",
            min_value=0.0,
            value=float(it["width_cm"]) if it.get("width_cm") else None,
            step=1.0,
            key=f"w119_fix_width_{ebay_item_id}",
        )
    with edit_cols[4]:
        height = st.number_input(
            "高さ (cm)",
            min_value=0.0,
            value=float(it["height_cm"]) if it.get("height_cm") else None,
            step=1.0,
            key=f"w119_fix_height_{ebay_item_id}",
        )

    # ── breakeven 表示 + 下限価格入力 ──
    be = it.get("lp_breakeven_usd")
    opt_cols = st.columns(3)
    with opt_cols[0]:
        if be and be > 0:
            st.markdown(f"💡 **損益分岐: ${be:.2f}**")
            st.caption("これ以下で売れたら粗利マイナス")
        else:
            st.markdown("💡 損益分岐: 未計算")
            st.caption("仕入価格 + 重量 + 寸法を保存すると自動計算")
    with opt_cols[1]:
        min_price = st.number_input(
            "下限価格 (USD, lp_min_price)",
            min_value=0.0,
            value=float(it["lp_min_price"]) if it.get("lp_min_price") else None,
            step=1.0,
            format="%.2f",
            key=f"w119_fix_minp_{ebay_item_id}",
            help="W183 自動値下げの絶対下限. 未入力なら breakeven が下限.",
        )
    with opt_cols[2]:
        st.write("")  # 縦位置調整
        if it.get("sku", "").startswith("ebay"):
            if st.button(
                "🔄 仕入価格 自動取得",
                key=f"w119_fix_fetch_pyen_{ebay_item_id}",
            ):
                with st.spinner("仕入先から取得中 (~15s)..."):
                    fetched = fetch_supplier_purchase_yen(ebay_item_id)
                if fetched:
                    st.success(f"取得: ¥{fetched:,}")
                    pyen = int(fetched)
                else:
                    st.error("取得失敗")

    # ── 競合 item id 編集 (最大 10 件) ──
    st.markdown("##### 🎯 ライバル登録 (最大 10 件、eBay 12-13 桁数字)")
    existing_competitors = _fetch_competitors_for_listing(ebay_item_id)
    # ③同型 データ損失修正 (2026-05-18 Codex 監査で第3インスタンス確定):
    # value=cur + key= の③型。DB 登録済競合が編集欄に出ず空欄 → 保存で
    # upsert_listing_competitors 全置換により登録済 active 競合 silent
    # 全消滅していた。共通ヘルパーで signature 再シード (詳細は
    # ui_cache.seed_keyed_list_from_db)。value= は撤去し session_state を
    # 唯一真実源に。
    seed_keyed_list_from_db(
        st.session_state, f"w119_fix_comp_{ebay_item_id}_",
        f"_w119_fix_comp_sig_{ebay_item_id}",
        existing_competitors, _MAX_COMPETITORS,
    )
    comp_inputs: list[str] = []
    rows = (_MAX_COMPETITORS + 4) // 5  # 5 件/row
    for r in range(rows):
        comp_cols = st.columns(5)
        for c in range(5):
            idx = r * 5 + c
            if idx >= _MAX_COMPETITORS:
                break
            with comp_cols[c]:
                # value= は渡さない: session_state[key] を唯一の真実源に
                # (value= と session_state 併用は Streamlit 警告)。③同型。
                val = st.text_input(
                    f"#{idx + 1}",
                    key=f"w119_fix_comp_{ebay_item_id}_{idx}",
                    placeholder="(空欄で登録なし)",
                )
                comp_inputs.append(val.strip())

    # ── 保存ボタン ──
    save_cols = st.columns([1, 1, 3])
    with save_cols[0]:
        save_btn = st.button(
            "💾 保存 + breakeven 再計算",
            key=f"w119_fix_save_{ebay_item_id}",
            type="primary",
        )
    with save_cols[1]:
        save_no_recalc = st.button(
            "💾 保存のみ",
            key=f"w119_fix_save_no_recalc_{ebay_item_id}",
            help="breakeven 再計算をスキップ (高速保存)",
        )

    if save_btn or save_no_recalc:
        _save_listing_data(
            ebay_item_id=ebay_item_id,
            purchase_yen=pyen,
            lp_min_price=min_price,
            weight_g=weight,
            length_cm=length,
            width_cm=width,
            height_cm=height,
            competitors=[c for c in comp_inputs if c],
            recalc_breakeven=save_btn,
            config=config,
        )
        # Bug B 修正: 編集行をセッション中ピン留め (未FIXフィルタで脱落
        # させない) + この listing を開いたまま + パネル開維持 + 即時反映 +
        # メッセージを rerun 跨ぎ表示.
        edited = st.session_state.setdefault(_FIX_EDITED_KEY, set())
        edited.add(ebay_item_id)
        st.session_state[_FIX_OPEN_EID_KEY] = ebay_item_id
        st.session_state[_FIX_PANEL_OPEN_KEY] = True
        st.session_state[_FIX_MSG_KEY] = (
            "ok",
            "✅ 保存しました" + (" + breakeven 再計算" if save_btn else "")
            + " (この商品は作業継続のため一覧に残しています)",
        )
        bump_db_version()
        st.rerun()


def _save_listing_data(
    *,
    ebay_item_id: str,
    purchase_yen: Optional[int],
    lp_min_price: Optional[float],
    weight_g: Optional[int],
    length_cm: Optional[float],
    width_cm: Optional[float],
    height_cm: Optional[float],
    competitors: list[str],
    recalc_breakeven: bool,
    config: dict,
) -> None:
    """1 listing の編集内容を DB に保存. breakeven 自動再計算 (optional)."""
    # 物理属性 (weight, dimensions)
    with get_conn() as conn:
        # 値 None なら touch しない. 0 / 値ありなら set.
        # set_listing_lowest_price_fields は両カラム同時 UPDATE なので個別 UPDATE で.
        if weight_g is not None:
            conn.execute(
                "UPDATE ebay_listings SET weight_g=?, weight_source='manual_edit', "
                "weight_estimated_at=datetime('now') WHERE ebay_item_id=?",
                (int(weight_g), ebay_item_id),
            )
        if length_cm is not None:
            conn.execute(
                "UPDATE ebay_listings SET length_cm=? WHERE ebay_item_id=?",
                (float(length_cm), ebay_item_id),
            )
        if width_cm is not None:
            conn.execute(
                "UPDATE ebay_listings SET width_cm=? WHERE ebay_item_id=?",
                (float(width_cm), ebay_item_id),
            )
        if height_cm is not None:
            conn.execute(
                "UPDATE ebay_listings SET height_cm=? WHERE ebay_item_id=?",
                (float(height_cm), ebay_item_id),
            )

    # purchase_yen + lp_min_price (両カラム同時)
    # set_listing_lowest_price_fields は両カラム強制 UPDATE なので
    # 値 None の方は既存値で上書き禁止. 既存値を読み戻して結合.
    if purchase_yen is not None or lp_min_price is not None:
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT purchase_yen, lp_min_price FROM ebay_listings WHERE ebay_item_id=?",
                (ebay_item_id,),
            ).fetchone()
        existing_pyen = existing[0] if existing else None
        existing_minp = existing[1] if existing else None
        merged_pyen = float(purchase_yen) if purchase_yen is not None else (
            float(existing_pyen) if existing_pyen is not None else None
        )
        merged_minp = float(lp_min_price) if lp_min_price is not None else (
            float(existing_minp) if existing_minp is not None else None
        )
        set_listing_lowest_price_fields(
            ebay_item_id=ebay_item_id,
            purchase_yen=merged_pyen,
            lp_min_price=merged_minp,
        )

    # 競合 (置換 upsert)
    try:
        upsert_listing_competitors(
            our_item_id=ebay_item_id, competitor_item_ids=competitors
        )
    except (sqlite3.OperationalError, ValueError, TypeError) as e:
        logger.warning(f"[w119_fix_save] competitor upsert error: {e}")

    # breakeven 再計算
    if recalc_breakeven:
        try:
            update_listing_breakeven(ebay_item_id, config or {})
        except (sqlite3.OperationalError, TypeError, ValueError, KeyError) as e:
            logger.warning(f"[w119_fix_save] breakeven recalc error: {e}")


# =============================================================================
# Public API
# =============================================================================

def render_data_fix(config: dict) -> None:
    """商品データ FIX UI のエントリーポイント. 最安値チェックタブの先頭で呼出."""
    listings = _fetch_all_listings_for_fix()
    stats = _compute_stats(listings)

    # Codex HIGH (2026-05-18): 外側 st.expander 内に listing 別 st.expander が
    # あり「expander ネスト禁止」例外で UI が壊れる。外側を トグルボタン +
    # st.container 化して根治 (内側 expander 有効化 + 再実行で畳まれない).
    # 前 run の保存メッセージを rerun 跨ぎ表示.
    _fix_flush_msg()
    _fix_open = bool(st.session_state.get(_FIX_PANEL_OPEN_KEY, False))
    _fix_label = (
        f"📋 商品データ FIX — 全 {stats.get('n', 0)} listing "
        f"(仕入価格 {stats.get('pyen_done', 0)}/{stats.get('n', 0)} | "
        f"重量 {stats.get('weight_done', 0)}/{stats.get('n', 0)} | "
        f"寸法 {stats.get('dim_done', 0)}/{stats.get('n', 0)} | "
        f"breakeven {stats.get('breakeven_done', 0)}/{stats.get('n', 0)} | "
        f"下限 {stats.get('min_price_done', 0)}/{stats.get('n', 0)})"
    )
    if st.button(
        ("▼ 閉じる ｜ " if _fix_open else "▶ 開く ｜ ") + _fix_label,
        key="w119_fix_panel_toggle",
        use_container_width=True,
    ):
        if _fix_open:
            # 閉じる: 作業セッション状態を解放
            st.session_state[_FIX_PANEL_OPEN_KEY] = False
            st.session_state.pop(_FIX_EDITED_KEY, None)
            st.session_state.pop(_FIX_OPEN_EID_KEY, None)
        else:
            st.session_state[_FIX_PANEL_OPEN_KEY] = True
        st.rerun()
    if not _fix_open:
        return
    with st.container():
        if stats["n"] == 0:
            st.info("active listing がありません.")
            return

        # 統計 metric
        _render_stats(stats)
        st.markdown("---")

        # フィルタ + 並び順
        filtered = _apply_filter(listings)
        st.caption(f"表示: {len(filtered)} / {stats['n']} listing")
        st.markdown("---")

        # ページング
        total_pages = max(1, (len(filtered) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = st.number_input(
            "ページ",
            min_value=1,
            max_value=total_pages,
            value=1,
            key="w119_fix_page",
        )
        start = (int(page) - 1) * _PAGE_SIZE
        page_items = filtered[start : start + _PAGE_SIZE]
        st.caption(f"ページ {int(page)} / {total_pages} ({len(page_items)} 件表示)")

        # listing 一覧
        # Bug B 修正: 直近に保存した listing は再実行後も開いたまま
        # (保存→畳まれて作業中商品が見えなくなる対策).
        _last_eid = st.session_state.get(_FIX_OPEN_EID_KEY)
        for it in page_items:
            title_preview = (it.get("title") or "")[:60]
            cp = float(it.get("current_price") or 0)
            sh = float(it.get("shipping_cost") or 0)
            chips = _render_status_chips(it)
            _is_last = it["ebay_item_id"] == _last_eid
            with st.expander(
                ("📝 " if _is_last else "")
                + f"{title_preview} | ${cp:.2f} + ${sh:.2f} "
                f"= ${cp + sh:.2f} | {chips}",
                expanded=_is_last,
            ):
                _render_edit_form(it, config)
