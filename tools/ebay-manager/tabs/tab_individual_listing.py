# -*- coding: utf-8 -*-
"""
個別新規出品 タブ UI (W9 Phase 5)

仕入先URL + 参考eBay URL → スクレイプ → 生成 → VerifyAdd → ドラフト保存 を
1画面で完結させるメインフォーム。

全体構成 (サブタブ):
  1. 新規出品        — メインワークフロー (Step 1-5)
  2. 保存済みドラフト — listing_drafts 一覧、編集ロード / 削除マーク
  3. テンプレート設定 — tab_description_templates.render_tab() に delegate

設計方針 (feedback_ui_design.md / feedback_autonomous_work.md):
  - 絵文字禁止。expander 禁止 (checkbox + container(border=True) で代替)。
  - 日本語UI 中心。JARVIS スタイル: 小見出しは letter-spacing 2px の
    大文字英単語ラベル + 日本語補足。
  - Streamlit 1.56 の st.status を scrape / generate / verify / add の
    4箇所で使い、進捗を可視化。
  - 再実行時に Step 3 以降を消すため、Step 1 (URL) が変わると
    以降の session_state をクリアする。
  - Claude 生成でカテゴリ候補3件が返ったら st.radio で選択、手動ID も併設。
  - エラー時は必ず手動入力フォールバックを用意 (scrape_error / generate_error)。
  - 保存は常に DB 先行 → API 呼出し (いつでも再試行できる安全側)。
"""
from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Optional

import streamlit as st

from monitor.database import (
    get_description_template,
    get_description_templates,
    get_listing_draft,
    get_listing_drafts,
    save_listing_draft,
    update_listing_draft,
    update_listing_draft_status,
)
from monitor.ebay_lister import (
    add_fixed_price_item_draft,
    build_draft_params_from_phase3,
    verify_add_fixed_price_item,
)
from monitor.ebay_reference_fetcher import (
    ReferenceListing,
    fetch_reference_listing,
)
from monitor.listing_generator import GeneratedListing, generate_listing
from monitor.rank_classifier import VALID_RANKS, RankClassification, classify_rank
from monitor.shipping_policy_selector import select_shipping_policy
from monitor.supplier_scraper import ScrapedProduct, scrape_supplier_url

logger = logging.getLogger(__name__)

# session_state プレフィクス (他タブとの衝突回避)
_SS = "il_"

# ランク選択 UI 用 (rank_classifier と同期)
_RANK_CHOICES: tuple[str, ...] = VALID_RANKS

# 2026-05-01 追加: ランク手動指定時に「何基準で選ぶか」を一目で判別する補助ラベル.
# 詳細は CLAUDE.md L195-204 (Claude 自動推定の対応表) と整合.
_RANK_LABEL_HINTS: dict[str, str] = {
    "N":     "N — 新品未開封 (シュリンク付き)",
    "S":     "S — 新品同様 (開封済・未使用)",
    "A":     "A — 美品 (小傷、全機能動作)",
    "B":     "B — 並品 (目立つ使用痕、全機能動作)",
    "C":     "C — 使用感あり (強い使用痕、全機能動作)",
    "D":     "D — 難あり (機能限定で動作)",
    "PO":    "PO — 通電のみ (動作未確認)",
    "As-Is": "As-Is — 未確認 / 部品取り (無保証)",
}

# eBay Condition ID の既知値 (手動 override 用ラベル)
_CONDITION_LABELS: dict[str, str] = {
    "1000": "New (1000)",
    "1500": "New Other / Like New (1500)",
    "2000": "Manufacturer Refurbished (2000)",
    "2500": "Seller Refurbished (2500)",
    "3000": "Used (3000)",
    "4000": "Very Good (4000)",
    "5000": "Good (5000)",
    "6000": "Acceptable (6000)",
    "7000": "For parts or not working / As-Is (7000)",
}


# =========================================================================
# session_state init / helpers
# =========================================================================

def _init_session_state() -> None:
    """本タブ用 session_state の初期化。"""
    defaults = {
        # Step 1
        f"{_SS}supplier_url": "",
        f"{_SS}reference_url": "",
        # Step 2 (scrape 結果) -- 全て dict 化して JSON 可搬に
        f"{_SS}scraped_product": None,       # dict or None
        f"{_SS}reference_listing": None,     # dict or None
        f"{_SS}selected_image_urls": [],     # list[str]
        f"{_SS}manual_fallback": False,      # scrape_error 時の手動入力モード
        # Step 3 (出品設定)
        f"{_SS}sku": "",
        f"{_SS}qty": 1,
        f"{_SS}price_usd": 0.0,
        f"{_SS}weight_g": 0,
        f"{_SS}in_stock": False,  # 2026-04-22 FIX: 無在庫出品が業務デフォルト、out_of_stock policy 適用
        f"{_SS}selected_template_id": None,
        f"{_SS}rank_manual_override": "",    # "" = auto, else rank_code
        f"{_SS}manual_category_id": "",
        # Step 4 (生成結果)
        f"{_SS}rank_classification": None,   # dict
        f"{_SS}generated_listing": None,     # dict
        f"{_SS}selected_category_id": "",
        f"{_SS}selected_condition_id": "",
        f"{_SS}edited_title": "",
        # Step 5 (shipping / verify / add)
        f"{_SS}shipping_policy_id": "",
        f"{_SS}shipping_policy_label": "",
        f"{_SS}shipping_policy_manual": "",
        f"{_SS}verify_result": None,
        f"{_SS}add_result": None,
        f"{_SS}current_draft_id": None,
        # 手動入力フォールバック用フィールド
        f"{_SS}manual_title_ja": "",
        f"{_SS}manual_condition_ja": "",
        f"{_SS}manual_description_ja": "",
        f"{_SS}manual_image_urls_text": "",  # 改行区切り
        # Brand Hero Compose (Photoroom + Gemini multi-image)
        f"{_SS}hero_candidates": None,        # list[{plate_id, score, path, reasoning}]
        f"{_SS}hero_selected_path": None,     # Path (user が 1 候補採用)
        f"{_SS}hero_studio_path": None,       # Path (Photoroom 中間結果、再生成で流用)
        f"{_SS}hero_source_url": None,        # str (元画像 URL、source 変更検知用)
        # 他画像 Photoroom 統一処理 (非 hero の背景を揃える)
        f"{_SS}additional_processed": None,   # list[{source_url, path}] or None
        # Phase D: EPS アップロード済 URL (ローカル path → 公開 URL)
        f"{_SS}processed_image_urls": [],     # list[str] — eBay Trading に渡せる公開 URL 群
        # フラグ
        f"{_SS}last_info": None,
        f"{_SS}last_error": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _clear_from_step(step: int) -> None:
    """指定した step 以降の session_state をクリアする。

    Step 定義:
      1: supplier_url / reference_url   → scraped_product, reference_listing, 以降全てクリア
      2: scraped_product / manual inputs → rank / generated 以降クリア
      3: 出品設定 (sku/price/...)       → generated 以降クリア
      4: 生成結果                       → verify/add のみクリア
      5: verify/add 結果                → add_result のみクリア
    """
    step2_keys = [
        f"{_SS}scraped_product", f"{_SS}reference_listing",
        f"{_SS}selected_image_urls", f"{_SS}manual_fallback",
        # hero compose は source 画像が変わればすべてクリア (source_url は step1 入力に連動)
        f"{_SS}hero_candidates", f"{_SS}hero_selected_path",
        f"{_SS}hero_studio_path", f"{_SS}hero_source_url",
        f"{_SS}additional_processed",
        # Phase D: EPS 済みの公開 URL もクリア
        f"{_SS}processed_image_urls",
    ]
    step4_keys = [
        f"{_SS}rank_classification", f"{_SS}generated_listing",
        f"{_SS}selected_category_id", f"{_SS}selected_condition_id",
        f"{_SS}edited_title",
    ]
    step5_keys = [
        f"{_SS}shipping_policy_id", f"{_SS}shipping_policy_label",
        f"{_SS}verify_result", f"{_SS}add_result",
        f"{_SS}current_draft_id",
        f"{_SS}pl_result",  # 2026-04-21: 前回の Promoted Listings 結果が残って誤表示されるのを防ぐ
    ]

    if step <= 1:
        for k in step2_keys:
            if k in st.session_state:
                if isinstance(st.session_state[k], list):
                    st.session_state[k] = []
                elif isinstance(st.session_state[k], bool):
                    st.session_state[k] = False
                else:
                    st.session_state[k] = None
    if step <= 3:
        for k in step4_keys:
            st.session_state[k] = None if k in (
                f"{_SS}rank_classification", f"{_SS}generated_listing",
            ) else ""
    if step <= 4:
        for k in step5_keys:
            st.session_state[k] = None if k in (
                f"{_SS}verify_result", f"{_SS}add_result",
                f"{_SS}current_draft_id", f"{_SS}pl_result",
            ) else ""


def _dataclass_to_dict(obj: Any) -> Optional[dict]:
    """dataclass を session_state で安全に保存可能な dict に変換。"""
    if obj is None:
        return None
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    return None


# =========================================================================
# load from saved draft (保存済みドラフト → 新規出品フォームに展開)
# =========================================================================

def _load_draft_into_form(draft: dict) -> None:
    """listing_drafts の 1行を新規出品フォームに展開する。"""
    _clear_from_step(1)

    st.session_state[f"{_SS}supplier_url"] = draft.get("supplier_url") or ""
    st.session_state[f"{_SS}reference_url"] = draft.get("reference_ebay_url") or ""

    # 仕入れスクレイプ相当のダミー dict (再スクレイプは不要、再編集用)
    st.session_state[f"{_SS}scraped_product"] = {
        "url": draft.get("supplier_url") or "",
        "platform": draft.get("supplier_platform") or "unknown",
        "title_ja": draft.get("supplier_title_ja"),
        "price_jpy": draft.get("supplier_price_jpy"),
        "condition_ja": draft.get("supplier_condition_ja"),
        "includes_ja": draft.get("supplier_includes_ja"),
        "image_urls": draft.get("supplier_image_urls") or [],
        "description_ja": None,
        "seller_name": None,
        "weight_hint_g": draft.get("weight_g"),
        "scrape_error": None,
    }

    if draft.get("reference_ebay_item_id"):
        st.session_state[f"{_SS}reference_listing"] = {
            "item_id": draft.get("reference_ebay_item_id") or "",
            "category_id": draft.get("reference_category_id"),
            "category_name": None,
            "condition_id": draft.get("reference_condition_id"),
            "condition_display_name": None,
            "item_specifics_keys": draft.get("reference_item_specifics_keys") or [],
            "title_sample": None,
            "fetch_error": None,
        }

    st.session_state[f"{_SS}selected_image_urls"] = list(
        draft.get("selected_image_urls") or draft.get("processed_image_urls") or []
    )
    st.session_state[f"{_SS}sku"] = draft.get("sku") or ""
    # qty は listing_drafts に単独カラムがない (Quantity=1 固定前提)。1 を既定にする。
    st.session_state[f"{_SS}qty"] = 1
    st.session_state[f"{_SS}price_usd"] = float(draft.get("listing_price_usd") or 0.0)
    st.session_state[f"{_SS}weight_g"] = int(draft.get("weight_g") or 0)
    st.session_state[f"{_SS}in_stock"] = bool(draft.get("in_stock"))
    st.session_state[f"{_SS}selected_template_id"] = draft.get("template_id")
    st.session_state[f"{_SS}rank_manual_override"] = draft.get("rank_code") or ""
    st.session_state[f"{_SS}manual_category_id"] = draft.get("ebay_category_id") or ""

    # 擬似的な rank_classification / generated_listing (再生成なしで表示復元)
    if draft.get("rank_code"):
        st.session_state[f"{_SS}rank_classification"] = {
            "rank_code": draft.get("rank_code"),
            "rank_label": draft.get("rank_label") or "",
            "rank_jp": "",
            "ebay_condition_id": draft.get("ebay_condition_id") or "3000",
            "confidence": 0.0,
            "reasoning": "(保存済みドラフトから復元)",
        }
    if draft.get("ebay_title"):
        st.session_state[f"{_SS}generated_listing"] = {
            "ebay_title": draft.get("ebay_title") or "",
            "ebay_description": draft.get("ebay_description") or "",
            "ebay_category_id": draft.get("ebay_category_id") or "",
            "ebay_category_name": draft.get("ebay_category_name") or "",
            "item_specifics": draft.get("item_specifics") or {},
            "category_candidates": [],
            "listing_price_usd": draft.get("listing_price_usd"),
            "mode_class": "default",
            "generate_error": None,
        }
        st.session_state[f"{_SS}edited_title"] = draft.get("ebay_title") or ""
        st.session_state[f"{_SS}selected_category_id"] = draft.get("ebay_category_id") or ""
        st.session_state[f"{_SS}selected_condition_id"] = draft.get("ebay_condition_id") or ""

    st.session_state[f"{_SS}shipping_policy_id"] = draft.get("shipping_policy_id") or ""
    st.session_state[f"{_SS}current_draft_id"] = draft.get("id")
    st.session_state[f"{_SS}last_info"] = (
        f"ドラフト #{draft.get('id')} をフォームに読み込みました。"
    )


# =========================================================================
# Step 1: URL 入力 + スクレイプ
# =========================================================================

def _render_step1_urls() -> None:
    st.markdown(
        '<div style="font-size:12px;color:rgba(180,220,255,0.55);letter-spacing:2px;'
        'margin:4px 0 6px;">S T E P &nbsp; 1 &nbsp; — &nbsp; S O U R C E &nbsp; U R L</div>',
        unsafe_allow_html=True,
    )
    _c1, _c2 = st.columns([3, 3])
    with _c1:
        supplier = st.text_input(
            "仕入先URL (必須)",
            value=st.session_state[f"{_SS}supplier_url"],
            placeholder="https://auctions.yahoo.co.jp/jp/auction/xxxxx",
            key=f"{_SS}input_supplier_url",
            help="ヤフオク / メルカリ / PayPayフリマ の商品ページURL",
        )
    with _c2:
        reference = st.text_input(
            "参考eBay URL / ItemID (任意・推奨)",
            value=st.session_state[f"{_SS}reference_url"],
            placeholder="https://www.ebay.com/itm/358463512773 または 358463512773",
            key=f"{_SS}input_reference_url",
            help="参考 listing の CategoryID と Item Specifics Keys を自動取得 (Description / 画像 / 価格 はコピーしません)",
        )

    # 入力が変わったら以降を自動クリア (スクレイプ後の鮮度保持)
    changed = False
    if supplier != st.session_state[f"{_SS}supplier_url"]:
        st.session_state[f"{_SS}supplier_url"] = supplier
        changed = True
    if reference != st.session_state[f"{_SS}reference_url"]:
        st.session_state[f"{_SS}reference_url"] = reference
        changed = True
    if changed:
        _clear_from_step(1)

    _b1, _b2, _b3 = st.columns([1, 1, 5])
    with _b1:
        scrape_clicked = st.button("スクレイプ", key=f"{_SS}btn_scrape", type="primary")
    with _b2:
        if st.button("クリア", key=f"{_SS}btn_clear_all"):
            _clear_from_step(1)
            # M1 対策: widget key の内部 state も pop (ウィジェット再描画で初期値取得)
            for _popkey in [
                f"{_SS}supplier_url", f"{_SS}reference_url",
                f"{_SS}input_supplier_url", f"{_SS}input_reference_url",
                f"{_SS}chk_confirm_production",  # M6: 本番確認 checkbox もリセット
                f"{_SS}add_result", f"{_SS}verify_result",
                f"{_SS}current_draft_id",
            ]:
                st.session_state.pop(_popkey, None)
            st.rerun()

    if scrape_clicked:
        url = (supplier or "").strip()
        if not url:
            st.error("仕入先URLは必須です。")
            return
        _do_scrape(url, (reference or "").strip())
        st.rerun()


def _do_scrape(supplier_url: str, reference_url: str) -> None:
    """スクレイプと参考 listing fetch を実行し session_state に格納する。"""
    with st.status("スクレイプ中...", expanded=True) as status:
        # 仕入先スクレイプ
        st.write(f"仕入先URL を解析中: {html.escape(supplier_url[:80])}")
        try:
            product = scrape_supplier_url(supplier_url)
        except Exception as e:  # noqa: BLE001
            logger.exception("scrape_supplier_url raised")
            status.update(label="スクレイプで例外発生", state="error")
            st.session_state[f"{_SS}last_error"] = f"scrape exception: {e}"
            return

        product_dict = _dataclass_to_dict(product) or {}
        st.session_state[f"{_SS}scraped_product"] = product_dict

        if product.scrape_error:
            st.write(f"スクレイプに失敗しました: {product.scrape_error}")
            st.session_state[f"{_SS}manual_fallback"] = True
        else:
            st.write(
                f"platform={product.platform}, title={product.title_ja[:50] if product.title_ja else '(不明)'}, "
                f"price={product.price_jpy}円, images={len(product.image_urls)}枚"
            )
            # 画像は取得分を全て初期選択
            st.session_state[f"{_SS}selected_image_urls"] = list(product.image_urls)
            st.session_state[f"{_SS}manual_fallback"] = False

        # 重量ヒントがあれば初期値に採用
        if product.weight_hint_g:
            st.session_state[f"{_SS}weight_g"] = int(product.weight_hint_g)

        # 参考 eBay listing fetch (任意)
        if reference_url:
            st.write(f"参考eBay listing を取得中: {html.escape(reference_url[:80])}")
            try:
                ref = fetch_reference_listing(reference_url)
            except Exception as e:  # noqa: BLE001
                logger.exception("fetch_reference_listing raised")
                ref = ReferenceListing(
                    item_id="",
                    fetch_error=f"exception: {e}",
                )
            ref_dict = _dataclass_to_dict(ref) or {}
            st.session_state[f"{_SS}reference_listing"] = ref_dict
            if ref.fetch_error:
                st.write(f"参考listing取得失敗 (無視して続行): {ref.fetch_error}")
            else:
                st.write(
                    f"参考 CategoryID={ref.category_id}, "
                    f"Specifics Keys={len(ref.item_specifics_keys)}件"
                )
                # reference から category / condition を初期値に採用
                if ref.category_id:
                    st.session_state[f"{_SS}manual_category_id"] = ref.category_id
        else:
            st.session_state[f"{_SS}reference_listing"] = None

        status.update(label="スクレイプ完了", state="complete")


# =========================================================================
# Step 2: スクレイプ結果表示 + 手動フォールバック
# =========================================================================

def _render_step2_scrape_result() -> None:
    product = st.session_state.get(f"{_SS}scraped_product")
    if not product:
        return

    st.markdown(
        '<div style="font-size:12px;color:rgba(180,220,255,0.55);letter-spacing:2px;'
        'margin:16px 0 6px;">S T E P &nbsp; 2 &nbsp; — &nbsp; S C R A P E D &nbsp; D A T A</div>',
        unsafe_allow_html=True,
    )

    manual = bool(st.session_state.get(f"{_SS}manual_fallback")) or bool(product.get("scrape_error"))

    with st.container(border=True):
        if product.get("scrape_error"):
            st.error(
                f"自動スクレイプに失敗しました ({product.get('scrape_error')})。"
                "下の手動入力フォームで情報を補ってください。"
            )

        # スクレイプ結果の可視化 (手動入力モード時も参考情報として表示)
        if not manual:
            _c1, _c2 = st.columns([2, 1])
            with _c1:
                st.markdown(
                    f"**タイトル (日本語)**: {html.escape(product.get('title_ja') or '(不明)')}"
                )
                st.markdown(
                    f"**価格**: {product.get('price_jpy') or '?'} 円"
                )
                st.markdown(
                    f"**platform**: {product.get('platform') or 'unknown'}"
                )
            with _c2:
                st.markdown(
                    f"**コンディション**: {html.escape(product.get('condition_ja') or '(不明)')}"
                )
                st.markdown(
                    f"**付属品**: {html.escape(product.get('includes_ja') or '(不明)')}"
                )
                st.markdown(
                    f"**重量ヒント**: {product.get('weight_hint_g') or '-'} g"
                )

            desc = product.get("description_ja") or ""
            if desc:
                show_full = st.checkbox(
                    f"商品説明を全て表示（長さ {len(desc)} 文字）",
                    value=False,
                    key=f"{_SS}chk_show_full_desc",
                )
                shown = desc if show_full else (desc[:500] + ("..." if len(desc) > 500 else ""))
                st.text_area(
                    "商品説明 (プレビュー)",
                    value=shown,
                    height=140,
                    disabled=True,
                    key=f"{_SS}ta_desc_preview",
                )

            # 画像選択 (checkbox 配列)
            all_imgs = list(product.get("image_urls") or [])
            if all_imgs:
                st.markdown("**画像選択 (最大10枚)**")
                selected = list(st.session_state.get(f"{_SS}selected_image_urls") or [])
                cols = st.columns(min(len(all_imgs), 5)) if len(all_imgs) else []
                new_selected: list[str] = []
                for idx, url in enumerate(all_imgs[:10]):
                    col = cols[idx % 5] if cols else st
                    with col:
                        checked = url in selected
                        new_checked = st.checkbox(
                            f"#{idx + 1}",
                            value=checked,
                            key=f"{_SS}img_chk_{idx}",
                        )
                        try:
                            st.image(url, use_container_width=True)
                        except Exception:  # noqa: BLE001
                            st.caption(f"(画像プレビュー失敗) {url[:50]}...")
                        if new_checked:
                            new_selected.append(url)
                st.session_state[f"{_SS}selected_image_urls"] = new_selected
            else:
                st.info("画像が取得できませんでした。手動入力モードで URL を補えます。")

            # --- BRAND HERO COMPOSE (Photoroom + Gemini 多画像合成) ---
            _render_hero_compose_section()

            # 手動入力モードへの切替ボタン (スクレイプ成功でも必要に応じて補正可)
            if st.checkbox(
                "手動入力モードに切り替える (スクレイプ内容を補正する場合)",
                value=False,
                key=f"{_SS}chk_force_manual",
            ):
                manual = True
                st.session_state[f"{_SS}manual_fallback"] = True

        if manual:
            _render_manual_fallback_form(product)


def _render_hero_compose_section() -> None:
    """Photoroom + Gemini でブランド hero 画像を合成するセクション.

    Step 2 (scrape/image selection) 内に表示。選択済画像の 1 枚目を source に
    Photoroom studio 化 → W3 pinned + top-3 Gemini 合成 → 候補ラジオ選択。
    採用した hero は session_state['hero_selected_path'] に保持、Phase D で
    processed_image_urls にマージされる予定 (現状は UI 表示のみ)。

    コスト: Photoroom $0.02 + Gemini x3 $0.12 = $0.14/回
    """
    selected_urls = list(st.session_state.get(f"{_SS}selected_image_urls") or [])
    if not selected_urls:
        return

    st.markdown(
        '<div style="font-size:12px;color:rgba(180,220,255,0.55);letter-spacing:2px;'
        'margin:20px 0 6px;">S T E P &nbsp; 2 . 5 &nbsp; — &nbsp; B R A N D &nbsp; '
        'H E R O &nbsp; C O M P O S E</div>',
        unsafe_allow_html=True,
    )

    source_url = selected_urls[0]
    candidates = st.session_state.get(f"{_SS}hero_candidates") or []
    last_source = st.session_state.get(f"{_SS}hero_source_url")

    with st.container(border=True):
        st.caption(
            "選択画像の 1 枚目に MonoHonpo プレートを合成して eBay hero 画像を "
            "生成します (約 $0.14 = 21 円 / 回、所要 30-50 秒)"
        )

        # source 変更時はキャッシュ破棄
        if last_source and last_source != source_url:
            st.info("source 画像が変わったため前回の合成候補は破棄されました。再生成してください。")
            st.session_state[f"{_SS}hero_candidates"] = None
            st.session_state[f"{_SS}hero_selected_path"] = None
            candidates = []

        _b1, _b2, _b3, _b4 = st.columns([1.2, 1.4, 1.2, 4])
        with _b1:
            if st.button(
                "プレート合成実行" if not candidates else "再使用 (課金0)",
                key=f"{_SS}btn_hero_compose",
                type="primary",
                help="既存合成結果があれば API skip して復元 (リトライ時のコスト 0)",
            ):
                _do_hero_compose(source_url, force_regenerate=False)
                st.rerun()
        with _b2:
            # 明示的な再生成 (有料): Photoroom + Gemini を再実行
            if st.button(
                "再生成 ($0.14)",
                key=f"{_SS}btn_hero_regen",
                help="既存合成結果を破棄して Photoroom + Gemini で再合成 (約 $0.14)",
            ):
                _do_hero_compose(source_url, force_regenerate=True)
                st.rerun()
        with _b3:
            if candidates and st.button(
                "候補クリア",
                key=f"{_SS}btn_hero_clear",
            ):
                st.session_state[f"{_SS}hero_candidates"] = None
                st.session_state[f"{_SS}hero_selected_path"] = None
                st.rerun()
        with _b4:
            if candidates:
                st.caption(f"{len(candidates)} 候補 / source: {source_url[:60]}...")

        if not candidates:
            return

        # 候補を横並びで表示
        st.markdown("**3 候補から 1 枚選択してください**")
        cols = st.columns(len(candidates))
        selected_path = st.session_state.get(f"{_SS}hero_selected_path")
        for idx, cand in enumerate(candidates):
            with cols[idx]:
                cpath = str(cand.get("path") or "")
                try:
                    st.image(cpath, use_container_width=True)
                except Exception:  # noqa: BLE001
                    st.caption(f"(画像読込失敗) {cpath}")
                is_picked = (selected_path == cpath)
                btn_label = "採用中" if is_picked else "採用"
                st.caption(
                    f"**#{idx+1} [{cand.get('plate_id')}]** score={cand.get('score', 0):.0f}"
                )
                if st.button(
                    btn_label,
                    key=f"{_SS}btn_hero_pick_{idx}",
                    type="primary" if is_picked else "secondary",
                    use_container_width=True,
                ):
                    st.session_state[f"{_SS}hero_selected_path"] = cpath
                    st.rerun()

        if selected_path:
            eps_urls = st.session_state.get(f"{_SS}processed_image_urls") or []
            st.success(
                f"採用済: {selected_path.split(chr(92))[-1]}"
                + (f"\n\neBay EPS アップロード済 ({len(eps_urls)} 枚)。出品時は加工版が使われます。"
                   if eps_urls else "")
            )

            # hero 採用後、他画像も Photoroom で統一処理する UI を出す
            _render_additional_photoroom_section()

            # Phase D: EPS アップロードボタン (hero + additional 全てを一括 upload)
            _render_eps_upload_section()


def _render_eps_upload_section() -> None:
    """加工画像を eBay EPS にアップロードして公開 URL を確保するセクション.

    hero 採用 OR additional 処理 OR 両方あれば表示.
    ボタン 1 発で hero + additional の全てを並列アップロード、
    結果を processed_image_urls に保存して _resolve_listing_image_urls から優先使用される.
    """
    hero = st.session_state.get(f"{_SS}hero_selected_path")
    additional = st.session_state.get(f"{_SS}additional_processed") or []
    if not hero and not additional:
        return
    uploaded = st.session_state.get(f"{_SS}processed_image_urls") or []
    total_local = (1 if hero else 0) + len(additional)

    st.markdown(
        '<div style="font-size:12px;color:rgba(180,220,255,0.55);letter-spacing:2px;'
        'margin:18px 0 6px;">S T E P &nbsp; 2 . 7 &nbsp; — &nbsp; E B A Y &nbsp; '
        'E P S &nbsp; U P L O A D</div>',
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        st.caption(
            f"加工画像 {total_local} 枚を eBay EPS (公式画像ホスト) にアップロードし、"
            f"出品時に公開 URL として使用します。Phase D 実装済 (2026-04-23)。"
            f"同一画像の重複 upload は DB cache で自動回避。"
        )
        _b1, _b2, _b3 = st.columns([1.4, 1.2, 4])
        with _b1:
            label = "EPS アップロード実行" if not uploaded else "再アップロード"
            if st.button(label, key=f"{_SS}btn_eps_upload", type="primary"):
                _upload_processed_to_eps_sync()
                st.rerun()
        with _b2:
            if uploaded and st.button("結果クリア", key=f"{_SS}btn_eps_clear"):
                st.session_state[f"{_SS}processed_image_urls"] = []
                st.rerun()
        with _b3:
            if uploaded:
                st.caption(f"{len(uploaded)}/{total_local} 枚アップロード済")

        if uploaded:
            # URL プレビュー (短縮表示)
            st.markdown("**公開 URL (出品時に使用)**")
            for i, u in enumerate(uploaded[:5]):
                st.caption(f"#{i+1}  {u}")
            if len(uploaded) > 5:
                st.caption(f"... 他 {len(uploaded) - 5} 枚")


def _render_additional_photoroom_section() -> None:
    """hero 以外の選択画像を Photoroom で同一背景に統一する UI.

    hero と同じ #c0c0c0 + DEPTH_STRONG で全画像が揃うので
    eBay 出品時に視覚的一貫性が生まれる。コスト: $0.02/枚。
    """
    selected_urls = list(st.session_state.get(f"{_SS}selected_image_urls") or [])
    hero_source = st.session_state.get(f"{_SS}hero_source_url")
    others = [u for u in selected_urls if u != hero_source]
    if not others:
        return

    processed = st.session_state.get(f"{_SS}additional_processed")
    st.markdown(
        '<div style="font-size:12px;color:rgba(180,220,255,0.55);letter-spacing:2px;'
        'margin:18px 0 6px;">S T E P &nbsp; 2 . 6 &nbsp; — &nbsp; O T H E R &nbsp; '
        'I M A G E S &nbsp; B A C K G R O U N D &nbsp; U N I F Y</div>',
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        cost = len(others) * 0.02
        st.caption(
            f"hero 以外の {len(others)} 枚を Photoroom で同一背景 (#c0c0c0 + depth) に "
            f"統一します (約 ${cost:.2f} = {int(cost * 150)} 円、所要 ~{len(others) * 10} 秒)"
        )

        _b1, _b2, _b3, _b4 = st.columns([1.4, 1.4, 1.2, 4])
        with _b1:
            label = "統一処理実行" if not processed else "再使用 (課金0)"
            if st.button(label, key=f"{_SS}btn_process_others", type="primary"):
                # force_regenerate=False で既存ファイルあれば API skip
                _do_process_additional_images(others, force_regenerate=False)
                st.rerun()
        with _b2:
            # 明示的な再生成 (有料): 既存ファイルを削除してから再合成
            if st.button(
                "再生成 ($0.08)",
                key=f"{_SS}btn_regen_others",
                help="既存合成結果を破棄して Photoroom API で再合成 (約 $0.08)",
            ):
                _do_process_additional_images(others, force_regenerate=True)
                st.rerun()
        with _b3:
            if processed and st.button("結果クリア", key=f"{_SS}btn_clear_others"):
                st.session_state[f"{_SS}additional_processed"] = None
                st.rerun()
        with _b4:
            if processed:
                st.caption(f"{len(processed)} 枚処理済")

        if not processed:
            return

        # 結果サムネイル表示
        st.markdown("**処理後の画像 (背景統一済)**")
        n = len(processed)
        cols_per_row = min(n, 5)
        for i in range(0, n, cols_per_row):
            batch = processed[i:i + cols_per_row]
            cols = st.columns(cols_per_row)
            for j, item in enumerate(batch):
                with cols[j]:
                    path = item.get("path") or ""
                    try:
                        st.image(path, use_container_width=True)
                    except Exception:  # noqa: BLE001
                        st.caption(f"(表示失敗) {path}")
                    st.caption(f"#{i + j + 2}")


def _do_process_additional_images(urls: list[str], force_regenerate: bool = False) -> None:
    """非 hero の画像を並列 Photoroom で処理して session_state に保存.

    W159 (2026-05-24): W158 shared unify_additional_backgrounds_cached pure
    関数を呼ぶ thin wrapper に縮小. 内部の Photoroom 並列実行 + cache 復元 +
    manifest 管理は全て shared module に集約.
    """
    from pathlib import Path as _Path

    try:
        from monitor.image_pipeline_shared import unify_additional_backgrounds_cached
    except ImportError as e:
        st.error(f"image_pipeline_shared import 失敗: {e}")
        return

    sku = st.session_state.get(f"{_SS}sku") or f"temp_{int(time.time())}"
    out_base = _Path(f"data/hero_candidates/{sku}")

    with st.status(
        f"{len(urls)} 枚を Photoroom で並列処理中...", expanded=False,
    ) as _s:
        results = unify_additional_backgrounds_cached(
            urls, out_base,
            force_regenerate=force_regenerate, max_workers=3,
        )
        # session_state 互換維持: 旧 dict 形 {source_url, path} で書込
        st.session_state[f"{_SS}additional_processed"] = [r.to_dict() for r in results]
        if len(results) < len(urls):
            _s.update(
                label=f"部分完了: {len(results)}/{len(urls)} 枚成功",
                state="complete",
            )
        else:
            _s.update(
                label=f"完了: {len(results)}/{len(urls)} 枚処理成功",
                state="complete",
            )


def _upload_processed_to_eps_sync() -> None:
    """hero + additional 加工画像を eBay EPS にアップロードして公開 URL を確保する.

    Phase D (2026-04-23): ローカルファイル → UploadSiteHostedPictures →
    eBay 公開 URL → session_state['processed_image_urls'] に保存.
    DB cache (eps_upload_cache) により同一ファイルの重複 upload を回避する.

    2026-04-26 フィードバック改善:
        - st.status を expanded=True に変更 (進捗が見えないバグの再発防止)
        - 失敗時は具体的エラーメッセージを st.error で明示
        - 処理対象 0 件時の警告にデバッグ情報追記
    """
    from pathlib import Path as _Path
    try:
        from monitor.ebay_eps_uploader import upload_images_parallel
    except Exception as e:  # noqa: BLE001
        st.error(f"EPS アップローダ import 失敗: {e}")
        return

    hero = st.session_state.get(f"{_SS}hero_selected_path")
    additional = st.session_state.get(f"{_SS}additional_processed") or []

    paths: list[_Path] = []
    missing: list[str] = []
    if hero:
        hp = _Path(hero)
        if hp.exists():
            paths.append(hp)
        else:
            missing.append(f"hero: {hp}")
    for item in additional:
        p = item.get("path") if isinstance(item, dict) else None
        if p:
            pp = _Path(p)
            if pp.exists():
                paths.append(pp)
            else:
                missing.append(f"additional: {pp}")

    if missing:
        st.error(
            f"加工ファイルが見つかりません ({len(missing)} 件): "
            + " / ".join(missing[:3])
            + (" ..." if len(missing) > 3 else "")
            + " — Step 2.5 / 2.6 を再実行してください"
        )

    if not paths:
        st.warning(
            f"EPS アップロード対象の加工画像がありません。"
            f" (hero_selected_path={hero}, additional_processed={len(additional)} 件)"
        )
        return

    # W159 (2026-05-24): W158 shared upload_to_eps_cached 経由に置換
    # 旧 upload_images_parallel 直接呼出は撤回. shared module で missing path の
    # explicit failed 化 (Q0 silent drop 防止) + dataclass 構造化が入る.
    from monitor.image_pipeline_shared import upload_to_eps_cached

    with st.status(
        f"eBay EPS に {len(paths)} 枚アップロード中 (~{len(paths)*3} 秒)...",
        expanded=True,  # ユーザーが進捗確認できるよう常時展開
    ) as _s:
        st.write(f"対象ファイル: {[p.name for p in paths]}")
        outcome = upload_to_eps_cached(paths, max_workers=3, use_cache=True)
        urls = list(outcome.eps_urls)
        st.session_state[f"{_SS}processed_image_urls"] = urls[:24]  # AddFixedPriceItem 上限 24

        for url in urls:
            st.write(f"  ✅ {url[:80]}")

        if outcome.success and not outcome.failed:
            _s.update(label=f"完了: {len(urls)} 枚 EPS にアップロード", state="complete")
        elif urls:
            _s.update(
                label=f"部分成功: {len(urls)}/{len(paths)} 枚 ({len(outcome.failed)} 枚失敗)",
                state="complete",  # 部分成功は warning 扱い
            )
            for fname, err in outcome.failed:
                st.warning(f"失敗: {fname}: {err}")
        else:
            _s.update(
                label=f"全失敗: {len(outcome.failed)}/{len(paths)} 枚",
                state="error",
            )
            for fname, err in outcome.failed:
                st.error(f"失敗: {fname}: {err}")


def _do_hero_compose(source_url: str, force_regenerate: bool = False) -> None:
    """Photoroom + Gemini パイプライン実行 (session_state に結果を詰める).

    W159 (2026-05-24): W158 で新設した monitor.image_pipeline_shared の
    compose_hero_candidates_cached pure 関数を呼ぶ thin wrapper に縮小.
    内部の Photoroom + Gemini + manifest 管理は全て shared module に集約.

    旧実装の cache 判定はファイル存在のみ (source URL 不一致 silent gap risk あり
    = W158 Codex GPT-5.5 HIGH-Codex-2 で指摘) だったが、shared 版は manifest.json
    による source URL + sha256 + pipeline_version 完全一致 check に強化されている.
    既存 data/hero_candidates/{sku}/ に manifest なしの場合は初回 cache miss で
    1 度 API 再課金 ($0.14) 発生、以降は manifest 経由で skip 復元.

    Q1 DoD pytest 113 件 + 既存 test_individual_listing 系互換維持確認済.

    失敗時は st.error で通知して fail-soft (旧挙動と同じ).
    """
    from pathlib import Path as _Path

    try:
        from monitor.image_pipeline_shared import compose_hero_candidates_cached
    except ImportError as e:
        st.error(f"image_pipeline_shared import 失敗: {e}")
        return

    sku = st.session_state.get(f"{_SS}sku") or f"temp_{int(time.time())}"
    out_base = _Path(f"data/hero_candidates/{sku}")

    with st.status(
        "Photoroom + Gemini で 3 候補生成中 (~40 秒)..." if force_regenerate
        else "既存合成結果を確認中...",
        expanded=False,
    ) as _s:
        candidates, studio_path = compose_hero_candidates_cached(
            source_url, out_base,
            force_regenerate=force_regenerate,
            k=3, max_parallel=3,
        )
        if not candidates:
            _s.update(label="hero 合成失敗 (logger 参照)", state="error")
            st.error("hero 合成失敗. PHOTOROOM_API_KEY / FAL_KEY / GOOGLE_API_KEY 設定 + logs/scheduler.log を確認.")
            return

        # session_state 互換維持: 旧 dict 形 (plate_id / score / path / reasoning) で書込
        st.session_state[f"{_SS}hero_candidates"] = [c.to_dict() for c in candidates]
        st.session_state[f"{_SS}hero_source_url"] = source_url
        if studio_path:
            st.session_state[f"{_SS}hero_studio_path"] = str(studio_path)
        # cache restored vs fresh で hero_selected_path の扱いを分岐 (旧挙動互換)
        # cache restored 時は旧 hero_selected_path を残す (user 採用済を保持)
        # fresh generation 時は None リセット (新候補から再選択促す)
        if force_regenerate:
            st.session_state[f"{_SS}hero_selected_path"] = None

        _s.update(
            label=f"完了: {len(candidates)} 候補 (cache hit or API 実行)",
            state="complete",
        )


def _render_manual_fallback_form(product: dict) -> None:
    """スクレイプ失敗 / 手動補正モード用の入力フォーム。"""
    st.markdown(
        '<div style="font-size:11px;color:rgba(240,200,48,0.75);margin:4px 0 8px;">'
        'M A N U A L &nbsp; F A L L B A C K</div>',
        unsafe_allow_html=True,
    )
    _c1, _c2 = st.columns(2)
    with _c1:
        m_title = st.text_input(
            "商品タイトル (日本語)",
            value=st.session_state[f"{_SS}manual_title_ja"] or (product.get("title_ja") or ""),
            key=f"{_SS}input_manual_title",
        )
        m_cond = st.text_input(
            "商品の状態 (日本語、例: 中古 美品)",
            value=st.session_state[f"{_SS}manual_condition_ja"] or (product.get("condition_ja") or ""),
            key=f"{_SS}input_manual_cond",
        )
    with _c2:
        st.caption("画像URL (1行1URL、最大10件)")
        m_imgs_text = st.text_area(
            "画像URL",
            value=st.session_state[f"{_SS}manual_image_urls_text"] or "\n".join(
                product.get("image_urls") or []
            ),
            height=100,
            key=f"{_SS}input_manual_imgs",
            label_visibility="collapsed",
        )

    m_desc = st.text_area(
        "商品説明 (日本語)",
        value=st.session_state[f"{_SS}manual_description_ja"] or (product.get("description_ja") or ""),
        height=120,
        key=f"{_SS}input_manual_desc",
    )

    # 反映
    if st.button("手動入力を反映", key=f"{_SS}btn_apply_manual"):
        st.session_state[f"{_SS}manual_title_ja"] = m_title
        st.session_state[f"{_SS}manual_condition_ja"] = m_cond
        st.session_state[f"{_SS}manual_description_ja"] = m_desc
        st.session_state[f"{_SS}manual_image_urls_text"] = m_imgs_text

        # scraped_product を手動値で上書き (新規 dict 生成、破壊的更新回避)
        img_urls = [
            u.strip() for u in (m_imgs_text or "").splitlines() if u.strip()
        ][:10]
        merged = dict(product)
        merged["title_ja"] = m_title or None
        merged["condition_ja"] = m_cond or None
        merged["description_ja"] = m_desc or None
        merged["image_urls"] = img_urls
        merged["scrape_error"] = None  # 手動で補えたら解消扱い
        st.session_state[f"{_SS}scraped_product"] = merged
        st.session_state[f"{_SS}selected_image_urls"] = list(img_urls)
        st.session_state[f"{_SS}last_info"] = "手動入力を反映しました。"
        st.rerun()


# =========================================================================
# Step 3: 出品設定 + 生成
# =========================================================================

def _render_step3_listing_settings(templates: list[dict]) -> None:
    product = st.session_state.get(f"{_SS}scraped_product")
    if not product:
        return

    st.markdown(
        '<div style="font-size:12px;color:rgba(180,220,255,0.55);letter-spacing:2px;'
        'margin:16px 0 6px;">S T E P &nbsp; 3 &nbsp; — &nbsp; L I S T I N G &nbsp; S E T T I N G S</div>',
        unsafe_allow_html=True,
    )

    # 2026-04-21 追加: SKU が空なら仕入先URL から自動生成 (url_to_sku)
    # 自動生成後にユーザーは手動編集も可能
    _stored_sku = st.session_state.get(f"{_SS}sku") or ""
    if not _stored_sku:
        _supplier_url = st.session_state.get(f"{_SS}supplier_url") or ""
        if _supplier_url:
            try:
                from sku_mapping_manager import url_to_sku as _u2s
                _auto_sku = _u2s(_supplier_url)
                if _auto_sku:
                    st.session_state[f"{_SS}sku"] = _auto_sku
                    _stored_sku = _auto_sku
            except Exception as _e:  # noqa: BLE001
                logger.warning(f"SKU auto-gen failed: {_e}")

    # 2026-04-23 追加: 出品価格が未設定なら calculator で最低 listable USD を自動計算
    # 仕入れ価格 (price_jpy) + 重量 (weight_hint_g) + カテゴリ (あれば) から算出
    if not st.session_state.get(f"{_SS}price_usd"):
        try:
            from calculator import find_min_listable_price_usd, load_settings
            _price_jpy = float(product.get("price_jpy") or 0)
            _weight_g = float(product.get("weight_hint_g") or 500)  # 不明時は 500g で暫定
            _cat_id = 0
            _listing = st.session_state.get(f"{_SS}generated_listing") or {}
            try:
                _cat_id = int(_listing.get("ebay_category_id") or 0)
            except (TypeError, ValueError):
                _cat_id = 0
            if _price_jpy > 0:
                _min_usd = find_min_listable_price_usd(
                    purchase_yen=_price_jpy,
                    weight_g=_weight_g,
                    category_id=_cat_id,
                    settings=load_settings(),
                )
                if _min_usd:
                    st.session_state[f"{_SS}price_usd"] = _min_usd
                    logger.info(
                        f"auto min listable price: purchase={_price_jpy}yen "
                        f"weight={_weight_g}g → {_min_usd} USD"
                    )
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"auto min-price calc failed: {_e}")

    _c1, _c2, _c3 = st.columns(3)
    with _c1:
        sku = st.text_input(
            "SKU (仕入先URLから自動生成／空欄時は未設定で送信)",
            value=_stored_sku,
            key=f"{_SS}input_sku",
            max_chars=50,
        )
        qty = st.number_input(
            "在庫 (qty)", min_value=1, max_value=99, value=int(st.session_state[f"{_SS}qty"] or 1),
            step=1, key=f"{_SS}input_qty",
        )
    with _c2:
        _auto_price = float(st.session_state[f"{_SS}price_usd"] or 0.0)
        price_usd = st.number_input(
            "出品価格 USD"
            + (f" (自動計算: {_auto_price:.2f})" if _auto_price > 0 else " (要手動設定)"),
            min_value=0.0, max_value=99999.0, step=0.50,
            value=_auto_price,
            key=f"{_SS}input_price_usd",
            help="仕入れ値 + 重量 + 為替 + 手数料から最低利益ライン (USD) を自動算出。値調整可。",
        )
        weight_g = st.number_input(
            "重量 g (Shipping Policy 選択用)",
            min_value=0, max_value=30000, step=50,
            value=int(st.session_state[f"{_SS}weight_g"] or 0),
            key=f"{_SS}input_weight_g",
        )
    with _c3:
        in_stock = st.checkbox(
            "即時出荷 (在庫あり policy を使う)",
            value=bool(st.session_state[f"{_SS}in_stock"]),
            key=f"{_SS}input_in_stock",
            help="チェック時は 1day 出荷系、外すと 7day 出荷系ポリシーを自動選択",
        )
        # テンプレ selectbox
        tpl_options = [(None, "(未選択)")]
        for t in templates:
            label = t.get("name") or f"id={t.get('id')}"
            if t.get("is_default"):
                label = f"{label}  [DEFAULT]"
            tpl_options.append((t.get("id"), label))

        # デフォルト選択決定
        current_tpl = st.session_state.get(f"{_SS}selected_template_id")
        if current_tpl is None:
            # default のテンプレを初期選択
            for t in templates:
                if t.get("is_default"):
                    current_tpl = t.get("id")
                    break

        selected_idx = 0
        for idx, (tid, _lbl) in enumerate(tpl_options):
            if tid == current_tpl:
                selected_idx = idx
                break

        tpl_choice = st.selectbox(
            "description テンプレート",
            options=list(range(len(tpl_options))),
            format_func=lambda i: tpl_options[i][1],
            index=selected_idx,
            key=f"{_SS}sel_template",
        )
        selected_template_id = tpl_options[tpl_choice][0]

    # ランク / カテゴリ 手動オーバーライド
    _r1, _r2 = st.columns(2)
    with _r1:
        rank_manual_idx = 0
        rank_options = ["(Claude 自動推定)"] + list(_RANK_CHOICES)
        current_rank_override = st.session_state.get(f"{_SS}rank_manual_override") or ""
        if current_rank_override in _RANK_CHOICES:
            rank_manual_idx = rank_options.index(current_rank_override)

        def _format_rank_option(i: int) -> str:
            opt = rank_options[i]
            if opt == "(Claude 自動推定)":
                return opt
            return _RANK_LABEL_HINTS.get(opt, opt)

        rank_manual_sel = st.selectbox(
            "ランク手動指定",
            options=list(range(len(rank_options))),
            format_func=_format_rank_option,
            index=rank_manual_idx,
            key=f"{_SS}sel_rank_override",
            help=(
                "自動推定の結果が不適切な場合のみ手動で上書き。"
                "判断基準は下の「ランクの選び方」を参照。"
            ),
        )
        rank_manual = rank_options[rank_manual_sel] if rank_manual_sel > 0 else ""

        with st.expander("ランクの選び方 (8 段階の判定基準)"):
            st.markdown(
                "| ランク | 状態 | 仕入先キーワード例 |\n"
                "|---|---|---|\n"
                "| **N** 新品未開封 | シュリンク付き、工場出荷状態 | 「新品」「未開封」「シュリンク付き」 |\n"
                "| **S** 新品同様 | 開封済みだが未使用、使用痕なし | 「新品同様」「未使用」「開封品」 |\n"
                "| **A** 美品 | 小さな使用痕、全機能動作 | 「美品」「美品に近い」 |\n"
                "| **B** 並品 | 目立つ使用痕、全機能動作 | 「良品」「並品」「普通」 |\n"
                "| **C** 使用感あり | 使用感強い、全機能動作 | 「使用感あり」 |\n"
                "| **D** 難あり | 外観/機能に問題、動作するが限定 | 「傷あり」「難あり」「訳あり」 |\n"
                "| **PO** 通電のみ | 電源 ON 確認だけ、動作未確認 | 「通電確認のみ」「通電のみ」 |\n"
                "| **As-Is** 未確認 | 部品取り、無保証 | 「動作未確認」「ジャンク」「部品取り」「故障」 |\n"
                "\n"
                "**eBay Condition ID 対応**: N=1000 / S=1500 (カテゴリ依存) / "
                "A〜D・PO=3000 (Used) / As-Is=7000 (For parts)\n\n"
                "**VeRO リスク**: Apple / Nintendo 等の非正規ルート品は **S 以下** が安全。\n\n"
                "**As-Is 注意**: 必ず理由を quick_notes / Item Specifics に明示 "
                "(eBay 仕様で 65 字以内)。"
            )
    with _r2:
        manual_cat = st.text_input(
            "カテゴリID 手動指定 (参考URLなしで、Claude候補も使わない時)",
            value=st.session_state[f"{_SS}manual_category_id"],
            key=f"{_SS}input_manual_cat",
            help="空欄なら参考listing / Claude 候補 / Claude 自動選択 の優先順で決定",
        )

    # 状態変更を反映
    st.session_state[f"{_SS}sku"] = sku
    st.session_state[f"{_SS}qty"] = int(qty)
    st.session_state[f"{_SS}price_usd"] = float(price_usd)
    st.session_state[f"{_SS}weight_g"] = int(weight_g)
    st.session_state[f"{_SS}in_stock"] = bool(in_stock)
    st.session_state[f"{_SS}selected_template_id"] = selected_template_id
    st.session_state[f"{_SS}rank_manual_override"] = rank_manual
    st.session_state[f"{_SS}manual_category_id"] = manual_cat

    # 生成ボタン
    _b1, _b2 = st.columns([1, 5])
    with _b1:
        gen_clicked = st.button("生成", key=f"{_SS}btn_generate", type="primary")

    # 画像加工ステータス表示 (3 状態に分岐、Phase D 済み)
    img_urls = st.session_state.get(f"{_SS}selected_image_urls") or []
    hero_selected = st.session_state.get(f"{_SS}hero_selected_path")
    additional_processed = st.session_state.get(f"{_SS}additional_processed") or []
    eps_uploaded = st.session_state.get(f"{_SS}processed_image_urls") or []
    if not img_urls:
        st.info("画像が1枚も選択されていません (eBay は画像必須の category が多い点に注意)。")
    elif eps_uploaded:
        st.success(
            f"加工画像 {len(eps_uploaded)} 枚を eBay EPS にアップロード済。"
            f"出品時にはこれらの公開 URL が **そのまま eBay に送信されます** (Phase D 完了)。"
        )
    elif hero_selected or additional_processed:
        local_count = (1 if hero_selected else 0) + len(additional_processed)
        st.warning(
            f"加工画像 {local_count} 枚はローカル保存済ですが、**eBay EPS 未アップロード**。"
            f"このまま出品すると仕入先 URL ({len(img_urls)} 枚) が使われます。\n\n"
            f"Step 2.7 で「EPS アップロード実行」ボタンを押すと加工版が出品に反映されます。"
        )
    else:
        st.info(
            f"画像加工未実施: 仕入先の画像URL ({len(img_urls)} 枚) をそのまま使用します。"
            f" ブランドプレート合成は Step 2.5、背景統一は Step 2.6 で実行できます。"
        )

    if gen_clicked:
        if not selected_template_id:
            st.error("description テンプレートを選択してください。")
            return
        _do_generate()
        st.rerun()


def _do_generate() -> None:
    """Claude Haiku ランク推定 → Sonnet 出品データ生成を実行。"""
    product_dict = st.session_state.get(f"{_SS}scraped_product") or {}
    reference_dict = st.session_state.get(f"{_SS}reference_listing")
    template_id = st.session_state.get(f"{_SS}selected_template_id")
    rank_manual = st.session_state.get(f"{_SS}rank_manual_override")

    if not product_dict:
        st.error("先にスクレイプを実行してください。")
        return
    if not template_id:
        st.error("テンプレートが選択されていません。")
        return

    with st.status("生成中... (Claude 推論に 10-30 秒)", expanded=True) as status:
        # dict → dataclass 風の軽量オブジェクトに復元 (listing_generator は duck typing)
        product_obj = _DictBox(product_dict)
        reference_obj = _DictBox(reference_dict) if reference_dict else None

        # Step A: rank classify (manual override 優先)
        if rank_manual and rank_manual in _RANK_CHOICES:
            st.write(f"ランク手動指定: {rank_manual}")
            from monitor.rank_classifier import _build_result  # 内部関数だが正当な用途
            rank = _build_result(
                rank_manual,
                confidence=1.0,
                reasoning="manual override",
            )
        else:
            st.write("Claude Haiku でランク推定中...")
            try:
                rank = classify_rank(
                    supplier_condition_ja=product_dict.get("condition_ja") or "",
                    supplier_description_ja=product_dict.get("description_ja"),
                    supplier_title_ja=product_dict.get("title_ja"),
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("classify_rank raised")
                status.update(label="ランク推定で例外発生", state="error")
                st.session_state[f"{_SS}last_error"] = f"rank classify exception: {e}"
                return
            st.write(f"推定ランク: {rank.rank_code} ({rank.rank_label}) conf={rank.confidence}")

        st.session_state[f"{_SS}rank_classification"] = _dataclass_to_dict(rank)

        # Step B: テンプレ本文を取得
        try:
            tpl = get_description_template(int(template_id))
        except Exception as e:  # noqa: BLE001
            logger.exception("get_description_template failed")
            status.update(label="テンプレ取得失敗", state="error")
            st.session_state[f"{_SS}last_error"] = f"template load: {e}"
            return
        if not tpl:
            status.update(label="テンプレが見つかりません", state="error")
            st.session_state[f"{_SS}last_error"] = f"template id={template_id} not found"
            return
        template_body = tpl.get("body") or ""

        # Step C: listing generate
        # 2026-04-22 FIX: 実際に選択された shipping policy (in_stock/out_of_stock) に
        # 沿った handling/delivery 日付を description に反映するため、in_stock + config
        # を generate_listing に渡す。settings は file から直接ロード (Streamlit の
        # flow で引数 thread するより読み直しが単純で副作用も少ない)。
        _gen_in_stock = bool(st.session_state.get(f"{_SS}in_stock"))
        _gen_cfg: Optional[dict] = None
        try:
            from monitor.shipping_policy_selector import load_settings_policies
            _gen_cfg = load_settings_policies()
        except Exception as _cfg_err:  # noqa: BLE001
            logger.warning(f"settings 読込失敗 (shipping_timing 反映なし): {_cfg_err}")
        st.write("Claude Sonnet で eBay 出品データ生成中...")
        try:
            listing = generate_listing(
                product=product_obj,
                reference=reference_obj,
                rank=rank,
                template_body=template_body,
                in_stock=_gen_in_stock,
                config=_gen_cfg,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("generate_listing raised")
            status.update(label="生成で例外発生", state="error")
            st.session_state[f"{_SS}last_error"] = f"generate exception: {e}"
            return

        listing_dict = _dataclass_to_dict(listing)
        st.session_state[f"{_SS}generated_listing"] = listing_dict

        if listing.generate_error:
            st.write(f"生成にエラー: {listing.generate_error}")
            status.update(label=f"生成失敗 ({listing.generate_error[:40]})", state="error")
            return

        # カテゴリ ID は 参考URL > manual > Claude 返却値 > 候補の1位、の優先順
        manual_cat = (st.session_state.get(f"{_SS}manual_category_id") or "").strip()
        ref_cat = (reference_dict or {}).get("category_id") if reference_dict else None
        final_cat = (
            ref_cat or manual_cat or (listing.ebay_category_id or "")
            or (listing.category_candidates[0].get("category_id") if listing.category_candidates else "")
        )
        st.session_state[f"{_SS}selected_category_id"] = final_cat
        st.session_state[f"{_SS}selected_condition_id"] = rank.ebay_condition_id
        st.session_state[f"{_SS}edited_title"] = listing.ebay_title

        # 参考URL がある場合の condition 上書き判定はユーザーに委ねるため表示のみ
        st.write(
            f"生成完了: title={len(listing.ebay_title)}字, "
            f"specifics={len(listing.item_specifics)}項目, mode={listing.mode_class}"
        )
        status.update(label="生成完了", state="complete")


class _DictBox:
    """dict を属性アクセス可能にする軽量ラッパ。

    listing_generator は duck typing なので getattr ベースで値を引く。
    本クラスは dict → attribute 変換するだけで、JSON 可搬性を崩さない。
    """
    def __init__(self, d: dict):
        self._d = dict(d or {})

    def __getattr__(self, name: str):
        return self._d.get(name)


# =========================================================================
# Step 4: 生成結果プレビュー + 編集
# =========================================================================

def _do_research_brain_review() -> None:
    """W27: Research 脳に出品ドラフトの相場感・コンプライアンスチェックを依頼.

    Method A (Max plan 内、API 課金 $0) で subagent 呼出.
    結果を session_state['research_review'] に保存して preview に表示.
    """
    listing = st.session_state.get(f"{_SS}generated_listing") or {}
    rank = st.session_state.get(f"{_SS}rank_classification") or {}
    sku = st.session_state.get(f"{_SS}sku") or ""
    price_input = st.session_state.get(f"{_SS}listing_price_usd") or 0
    in_stock = st.session_state.get(f"{_SS}in_stock") or False

    if not listing.get("ebay_title"):
        st.warning("生成済みの listing が無い (先に Step 4 生成を実行)")
        return

    specifics = listing.get("item_specifics") or {}
    specifics_lines = "\n".join(f"  {k}: {v}" for k, v in list(specifics.items())[:8])
    desc_head = (listing.get("ebay_description") or "")[:300]

    prompt = f"""監修依頼: eBay 出品ドラフトの相場感・コンプライアンスチェック (W27)

商品 SKU: {sku}
状態 (8 段階): {rank.get('rank_code', '?')} - {rank.get('rank_label', '')}
在庫: {'在庫有' if in_stock else '無在庫'}

生成ドラフト:
  Title ({len(listing.get('ebay_title', ''))} 字): {listing.get('ebay_title', '')}
  Category ID: {listing.get('ebay_category_id', '?')} - {listing.get('ebay_category_name', '?')}
  Brand: {specifics.get('Brand', '(未設定)')}
  Item Specifics:
{specifics_lines}
  Description (head 300): {desc_head}
  Price (USD): ${price_input}

確認してほしいこと (各項目 OK/要修正/確信度低 のいずれかで明示):
1. Title は SEO + 80 字制約 + 誇大表現禁止 を満たすか
2. Item Specifics に推測値 / "Unknown" 等の placeholder が無いか
3. Section 232 派生品の場合、価格に関税 buffer 25% 内包されているか (Section 232 該当性も判定)
4. Country of Origin / Manufacture が空 (出品文に絶対記載しない) か
5. 動画 KB に「この商品カテゴリの注意点」があるか (例: PIONEER ジャンク不可)
6. VeRO 抵触ブランドを商標として書いていないか
7. primary_market 区分別の価格・送料設計が `reference_shipping_tariff_logic.md` v1.0 § 4.2 と整合しているか:
   - US_only (US≥70%): 商品価格に DDP 関税包含、表示送料 $0 Free 全国
   - mixed_global (30%<US<70%): 商品価格は商品代のみ、DDP 関税は送料欄に上乗せ
   - global_only (US≤30%): DDP 関税は売主自腹リスク許容 (機会損失分)
   - unknown (sample<5): mixed_global と同 default

各観点で 1 行コメント + 確信度 (高/中/低). 規制業務 (HS code / VeRO) の最終責任は人間.
"""

    with st.status(
        "Research 脳 (Opus 4.7) で監修中... (60-90 秒)",
        expanded=True,
    ) as status:
        try:
            from monitor.research_brain import ask
            ans = ask(
                prompt,
                source="listing_review",
                context_hints={},  # W75: SKU rule 準拠、draft で ebay_item_id 不在のため listing block skip
                force_model="opus",
                enable_thinking=True,
                save_history=True,
                timeout=180,
            )
            if ans.error:
                status.update(label=f"監修失敗: {ans.error}", state="error")
                logger.warning(f"Research 脳 review skipped: {ans.error}")
                st.warning(f"Research 脳監修失敗: {ans.error}")
                # 失敗状態も session_state に保存して可視化 (silent skip 禁止)
                st.session_state[f"{_SS}research_review"] = {
                    "answer_md": f"★ Research 脳監修未実施 (理由: {ans.error})",
                    "qa_id": ans.qa_id,
                    "duration_ms": ans.duration_ms,
                    "via": "error",
                }
            else:
                st.session_state[f"{_SS}research_review"] = {
                    "answer_md": ans.answer_md,
                    "qa_id": ans.qa_id,
                    "duration_ms": ans.duration_ms,
                    "cost_usd": ans.cost_usd,
                    "citations": ans.citations,
                    "via": ans.via,
                }
                status.update(
                    label=f"監修完了: {ans.duration_ms//1000}s, citations {len(ans.citations)} 件",
                    state="complete",
                )
        except Exception as e:  # noqa: BLE001
            status.update(label=f"例外: {e}", state="error")
            logger.exception("Research 脳 review failed")
            st.error(f"監修例外: {e}")


def _render_step4_generation_preview() -> None:
    listing = st.session_state.get(f"{_SS}generated_listing")
    rank = st.session_state.get(f"{_SS}rank_classification")
    if not listing or not rank:
        return

    st.markdown(
        '<div style="font-size:12px;color:rgba(180,220,255,0.55);letter-spacing:2px;'
        'margin:16px 0 6px;">S T E P &nbsp; 4 &nbsp; — &nbsp; G E N E R A T E D &nbsp; P R E V I E W</div>',
        unsafe_allow_html=True,
    )

    if listing.get("generate_error"):
        st.error(
            f"Claude 生成が失敗しました: {listing.get('generate_error')}。"
            "下の手動編集項目で必要情報を埋めれば、そのまま VerifyAdd / ドラフト保存に進めます。"
        )

    # W27: Research 脳 監修ボタン (任意、Method A、$0)
    _research_review = st.session_state.get(f"{_SS}research_review")
    _b1, _b2, _b3 = st.columns([1.6, 1.4, 4])
    with _b1:
        if st.button(
            "Research 脳監修" if not _research_review else "再監修",
            key=f"{_SS}btn_research_review",
            help="Opus 4.7 が動画 KB + memory feedback を踏まえて出品ドラフトを最終レビュー (60-90 秒、$0 Method A)",
        ):
            _do_research_brain_review()
            st.rerun()
    with _b2:
        if _research_review and st.button("監修クリア", key=f"{_SS}btn_clear_review"):
            st.session_state[f"{_SS}research_review"] = None
            st.rerun()
    with _b3:
        if _research_review:
            st.caption(
                f"Research 脳 qa_id #{_research_review.get('qa_id')} "
                f"({_research_review.get('duration_ms', 0)//1000}s)"
            )

    # Research 脳監修コメント表示
    if _research_review:
        with st.container(border=True):
            st.markdown("**Research 脳 監修コメント (Opus 4.7)**")
            st.markdown(_research_review.get("answer_md") or "(回答無し)")

    # タイトル (編集可, 80字カウンタ)
    edited = st.text_area(
        "英語タイトル (80字以内 SEO)",
        value=st.session_state.get(f"{_SS}edited_title") or listing.get("ebay_title") or "",
        height=80,
        max_chars=80,
        key=f"{_SS}input_edited_title",
    )
    st.session_state[f"{_SS}edited_title"] = edited
    st.caption(f"{len(edited)} / 80 字")

    # カテゴリ選択
    reference_dict = st.session_state.get(f"{_SS}reference_listing")
    candidates = listing.get("category_candidates") or []
    _render_category_selector(reference_dict, listing, candidates)

    # コンディションID
    _render_condition_selector(rank)

    # Item Specifics
    specifics = listing.get("item_specifics") or {}
    if specifics:
        st.markdown("**Item Specifics**")
        with st.container(border=True):
            rows_html = "".join(
                f'<tr><td style="padding:3px 8px;font-family:monospace;font-size:11px;'
                f'color:rgba(160,220,255,0.75);letter-spacing:1px;">{html.escape(k)}</td>'
                f'<td style="padding:3px 8px;font-size:12px;color:rgba(220,235,250,0.95);">'
                f'{html.escape(str(v))}</td></tr>'
                for k, v in specifics.items()
            )
            st.markdown(
                f'<table style="border-collapse:collapse;width:100%;">{rows_html}</table>',
                unsafe_allow_html=True,
            )

    # description プレビュー (iframe でレンダリング)
    desc_html = listing.get("ebay_description") or ""
    if desc_html:
        show_preview = st.checkbox(
            "Description プレビューを表示 (HTML レンダリング)",
            value=False,
            key=f"{_SS}chk_show_desc_preview",
        )
        if show_preview:
            import streamlit.components.v1 as _components
            try:
                _components.html(desc_html, height=600, scrolling=True)
            except Exception as e:  # noqa: BLE001
                st.error(f"プレビュー描画失敗: {e}")

        show_src = st.checkbox(
            f"Description ソース HTML を表示 (長さ {len(desc_html)} 文字)",
            value=False,
            key=f"{_SS}chk_show_desc_src",
        )
        if show_src:
            st.code(desc_html, language="html")
    else:
        st.warning("Description が空です。テンプレ本文 / Claude 生成結果を確認してください。")


def _render_category_selector(
    reference_dict: Optional[dict],
    listing: dict,
    candidates: list,
) -> None:
    """カテゴリ選択 UI。参考URL > 候補radio > 手動入力 の優先順。"""
    st.markdown("**カテゴリ**")
    ref_cat = (reference_dict or {}).get("category_id") if reference_dict else None

    if ref_cat:
        st.info(
            f"参考URL の CategoryID `{ref_cat}` を採用します "
            f"(名前: {(reference_dict or {}).get('category_name') or '?'})"
        )
        st.session_state[f"{_SS}selected_category_id"] = ref_cat
        return

    # 候補 3件を radio
    if candidates:
        options: list[tuple[str, str]] = []
        for c in candidates:
            cid = str(c.get("category_id") or "")
            cname = str(c.get("category_name") or "")
            reason = str(c.get("reasoning") or "")
            options.append((cid, f"{cid} — {cname}   ({reason[:50]})"))

        # 現在選択値から idx を決定
        cur = st.session_state.get(f"{_SS}selected_category_id") or (
            listing.get("ebay_category_id") or ""
        )
        idx = 0
        for i, (cid, _lbl) in enumerate(options):
            if cid == cur:
                idx = i
                break

        chosen_idx = st.radio(
            "Claude 候補から選択",
            options=list(range(len(options))),
            format_func=lambda i: options[i][1],
            index=idx,
            key=f"{_SS}radio_cat_candidates",
        )
        selected = options[chosen_idx][0]
        st.session_state[f"{_SS}selected_category_id"] = selected
    else:
        # 候補なし、手動入力のみ
        cat_manual = st.text_input(
            "CategoryID (手動入力)",
            value=st.session_state.get(f"{_SS}selected_category_id") or "",
            key=f"{_SS}input_cat_manual",
            help="eBay Category ID (数字) を直接入力",
        )
        st.session_state[f"{_SS}selected_category_id"] = cat_manual.strip()

    # 手動上書き (常に併設)
    override = st.text_input(
        "カテゴリID を手動で上書き (任意)",
        value="",
        key=f"{_SS}input_cat_override",
        help="空欄なら上記の選択結果を使用、値を入れるとそれで上書き",
    )
    if override.strip():
        st.session_state[f"{_SS}selected_category_id"] = override.strip()


def _render_condition_selector(rank: dict) -> None:
    """コンディションID の override UI。"""
    st.markdown("**コンディション (eBay Condition ID)**")
    recommended = rank.get("ebay_condition_id") or "3000"

    options = list(_CONDITION_LABELS.items())
    # recommended が list に含まれなければ先頭に追加
    option_ids = [k for k, _v in options]
    if recommended not in option_ids:
        options.insert(0, (recommended, f"Recommended ({recommended})"))

    cur = st.session_state.get(f"{_SS}selected_condition_id") or recommended
    idx = 0
    for i, (cid, _lbl) in enumerate(options):
        if cid == cur:
            idx = i
            break
    chosen = st.selectbox(
        f"ランク {rank.get('rank_code')} の推奨は {recommended}",
        options=list(range(len(options))),
        format_func=lambda i: options[i][1],
        index=idx,
        key=f"{_SS}sel_condition_id",
    )
    st.session_state[f"{_SS}selected_condition_id"] = options[chosen][0]


# =========================================================================
# Step 5: Shipping policy 決定 + Verify + Add
# =========================================================================

def _render_step5_verify_add(settings: dict) -> None:
    product = st.session_state.get(f"{_SS}scraped_product")
    listing = st.session_state.get(f"{_SS}generated_listing")
    rank = st.session_state.get(f"{_SS}rank_classification")
    if not (product and listing and rank):
        return

    st.markdown(
        '<div style="font-size:12px;color:rgba(180,220,255,0.55);letter-spacing:2px;'
        'margin:16px 0 6px;">S T E P &nbsp; 5 &nbsp; — &nbsp; V E R I F Y &nbsp; &amp; &nbsp; S A V E</div>',
        unsafe_allow_html=True,
    )

    # Shipping policy 自動決定 (失敗時は手動入力)
    weight_g = int(st.session_state.get(f"{_SS}weight_g") or 0)
    in_stock = bool(st.session_state.get(f"{_SS}in_stock"))
    policy_id = ""
    policy_label = ""
    try:
        policy_id, policy_label = select_shipping_policy(
            weight_g=weight_g or None,
            in_stock=in_stock,
            config=settings,
        )
    except ValueError as e:
        st.error(f"Shipping Policy 自動選択失敗: {e}. 手動で指定してください。")
    except Exception as e:  # noqa: BLE001
        logger.exception("select_shipping_policy raised")
        st.error(f"Shipping Policy 自動選択で例外: {e}")

    st.session_state[f"{_SS}shipping_policy_id"] = policy_id
    st.session_state[f"{_SS}shipping_policy_label"] = policy_label

    _s1, _s2 = st.columns(2)
    with _s1:
        if policy_id:
            st.markdown(
                f"**Shipping Policy (自動)**: `{policy_id}` ({policy_label})"
            )
        else:
            st.warning("Shipping Policy が自動決定できていません。")
    with _s2:
        manual_shipping = st.text_input(
            "Shipping Policy ID 手動上書き (任意)",
            value=st.session_state.get(f"{_SS}shipping_policy_manual") or "",
            key=f"{_SS}input_manual_shipping",
        )
        st.session_state[f"{_SS}shipping_policy_manual"] = manual_shipping

    effective_shipping = (
        manual_shipping.strip() if manual_shipping and manual_shipping.strip()
        else policy_id
    )
    if not effective_shipping:
        st.error("Shipping Policy ID が決定していません。Verify / Add は実行できません。")

    # draft_params 構築
    draft_params = _build_current_draft_params(effective_shipping, settings)
    if not draft_params:
        return

    # Verify / Add ボタン
    _b1, _b2, _b3 = st.columns([1.2, 1.4, 4])
    with _b1:
        verify_clicked = st.button(
            "VerifyAdd (dry-run)", key=f"{_SS}btn_verify", type="secondary",
            disabled=not effective_shipping,
        )
    with _b2:
        # 二重送信ガード (HIGH-1 対策): 直前の Add が成功していれば再押下禁止。
        # UI disable だけでなく _do_add 冒頭でも再入チェック (二重防御)。
        verify = st.session_state.get(f"{_SS}verify_result") or {}
        has_errors = bool(verify.get("errors"))
        prev_add = st.session_state.get(f"{_SS}add_result") or {}
        already_submitted = bool(prev_add.get("success") and prev_add.get("ebay_item_id"))
        # M6 対策: 本番実行を了承 checkbox (明示承認制、誤クリック防止)
        confirm_key = f"{_SS}chk_confirm_production"
        confirmed = st.checkbox(
            "最終確認: eBay に Active 出品 (即時公開)",
            key=confirm_key, value=False,
            disabled=has_errors or already_submitted or (not effective_shipping),
            help="チェック後に「ドラフト保存 & eBay登録」ボタンが有効化されます",
        )
        add_disabled = (
            (not effective_shipping) or has_errors or already_submitted or (not confirmed)
        )
        if already_submitted:
            add_help = (
                f"既に ItemID={prev_add.get('ebay_item_id')} で登録済。"
                "再出品するには『クリア』で新規開始。"
            )
        elif has_errors:
            add_help = "VerifyAdd でエラーが出ている間は無効。"
        elif not confirmed:
            add_help = "先に「最終確認: eBay に Active 出品」にチェックを入れてください。"
        else:
            add_help = (
                "listing_drafts に保存し、Trading API AddFixedPriceItem を実行 "
                "(即時 Active 公開)"
            )
        add_clicked = st.button(
            "保存 & Active 出品", key=f"{_SS}btn_add", type="primary",
            disabled=add_disabled, help=add_help,
        )

    if verify_clicked:
        _do_verify(draft_params, settings)
        st.rerun()

    _render_verify_result()

    if add_clicked:
        _do_add(draft_params, settings)
        st.rerun()

    _render_add_result()


def _resolve_listing_image_urls() -> list[str]:
    """draft_params に渡す image_urls を session_state から解決する。

    契約 (database.py マイグレーション v15 コメント参照):
        processed_image_urls > selected_image_urls > supplier_image_urls[:24]

    Phase A (2026-04-23) 時点では W10 画像加工未実装のため processed は常に空で、
    実質 selected のみを返す。Phase D で processed を優先する実装に差し替える。
    """
    processed = list(st.session_state.get(f"{_SS}processed_image_urls") or [])
    if processed:
        return processed[:24]
    selected = list(st.session_state.get(f"{_SS}selected_image_urls") or [])
    if selected:
        return selected[:24]
    # supplier_image_urls fallback (scraped_product.image_urls が原源)
    product_dict = st.session_state.get(f"{_SS}scraped_product") or {}
    raw = list(product_dict.get("image_urls") or [])
    return raw[:24]


def _build_current_draft_params(shipping_policy_id: str, settings: dict) -> Optional[dict]:
    """UI state から draft_params を構築する。"""
    product_dict = st.session_state.get(f"{_SS}scraped_product") or {}
    reference_dict = st.session_state.get(f"{_SS}reference_listing")
    listing_dict = st.session_state.get(f"{_SS}generated_listing") or {}
    rank_dict = st.session_state.get(f"{_SS}rank_classification") or {}

    # listing dict を上書き (UI 編集結果を反映)
    listing_edited = dict(listing_dict)
    listing_edited["ebay_title"] = st.session_state.get(f"{_SS}edited_title") or listing_dict.get("ebay_title") or ""
    listing_edited["ebay_category_id"] = st.session_state.get(f"{_SS}selected_category_id") or listing_dict.get("ebay_category_id") or ""
    # rank も condition 選択を反映
    rank_edited = dict(rank_dict)
    rank_edited["ebay_condition_id"] = st.session_state.get(f"{_SS}selected_condition_id") or rank_dict.get("ebay_condition_id") or "3000"

    image_urls = _resolve_listing_image_urls()
    sku = st.session_state.get(f"{_SS}sku") or ""
    price = float(st.session_state.get(f"{_SS}price_usd") or 0.0)

    # W84 (2026-05-02): 個別 UI 出品で primary_market / hs_code を明示伝搬.
    # 新規 W9 listing は Terapeak 未分析のため "unknown" default = mixed_global 等価
    # 動作 (商品代 + 送料欄に DDP 関税近似値). user UI override は別 W で検討.
    primary_market_val = listing_dict.get("primary_market") or "unknown"
    hs_code_val = listing_dict.get("hs_code")

    try:
        params = build_draft_params_from_phase3(
            product=_DictBox(product_dict),
            reference=_DictBox(reference_dict) if reference_dict else None,
            rank=_DictBox(rank_edited),
            listing=_DictBox(listing_edited),
            shipping_policy_id=shipping_policy_id,
            sku=sku,
            listing_price_usd=price,
            image_urls=image_urls,
            config=settings,
            primary_market=primary_market_val,
            hs_code=hs_code_val,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("build_draft_params_from_phase3 raised")
        st.error(f"draft_params 構築で例外: {e}")
        return None

    # 2026-05-01: scheduled_days_offset の制御は settings.json `w9_draft_mode`
    # に集約 (0 = Active 即時公開 / >0 = Scheduled). UI 側で hardcode 上書き
    # しないことで設定経路と挙動を一致させる. 21 日 Scheduled に戻したい時は
    # settings.json `w9_draft_mode.scheduled_days_offset = 21` で対応可.

    # 事前バリデーション (UI でも軽く弾く)
    issues = []
    if not params.get("ebay_title"):
        issues.append("英語タイトルが空")
    if not params.get("ebay_category_id"):
        issues.append("CategoryID が未確定")
    if not params.get("payment_policy_id"):
        issues.append("PaymentPolicyID が settings.json で未定義")
    if not params.get("return_policy_id"):
        issues.append("ReturnPolicyID が settings.json で未定義")
    if price <= 0:
        issues.append("出品価格 USD が 0 以下")
    if not image_urls:
        issues.append("画像が0枚 (category によっては eBay が拒否する可能性あり)")
    if issues:
        st.warning("事前チェック: " + " / ".join(issues))

    return params


def _do_verify(draft_params: dict, settings: dict) -> None:
    """VerifyAddFixedPriceItem を実行 (dry-run)。"""
    with st.status("VerifyAddFixedPriceItem 実行中...", expanded=True) as status:
        try:
            result = verify_add_fixed_price_item(
                draft_params=draft_params,
                config=settings,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("verify_add_fixed_price_item raised")
            result = {
                "success": False,
                "ack": None,
                "fees": [],
                "errors": [f"exception: {e}"],
                "warnings": [],
                "raw_xml": "",
            }
        st.session_state[f"{_SS}verify_result"] = result
        if result.get("success"):
            st.write(
                f"Ack={result.get('ack')}, "
                f"fees={len(result.get('fees') or [])}件, "
                f"warnings={len(result.get('warnings') or [])}件"
            )
            status.update(label="VerifyAdd 成功", state="complete")
        else:
            st.write(
                f"Ack={result.get('ack')}, "
                f"errors={len(result.get('errors') or [])}件"
            )
            status.update(label="VerifyAdd 失敗 (エラー確認)", state="error")


def _render_verify_result() -> None:
    result = st.session_state.get(f"{_SS}verify_result")
    if not result:
        return

    with st.container(border=True):
        ack = result.get("ack") or "?"
        st.markdown(f"**VerifyAdd 結果**: Ack={ack}")

        fees = result.get("fees") or []
        if fees:
            st.markdown("**手数料 (VerifyAdd 推計)**")
            for f in fees:
                st.markdown(
                    f"- {html.escape(str(f.get('name') or ''))} : "
                    f"{html.escape(str(f.get('fee') or ''))} "
                    f"{html.escape(str(f.get('currency') or ''))}"
                )

        errors = result.get("errors") or []
        if errors:
            st.error("**エラー** (出品は実行できません):")
            for e in errors:
                st.markdown(f"- {html.escape(str(e))}")

        warnings = result.get("warnings") or []
        if warnings:
            st.warning("**警告** (出品は実行可能):")
            for w in warnings:
                st.markdown(f"- {html.escape(str(w))}")

        show_xml = st.checkbox(
            "生 XML レスポンスを表示",
            value=False,
            key=f"{_SS}chk_show_verify_xml",
        )
        if show_xml:
            st.code(result.get("raw_xml") or "(空)", language="xml")


def _do_add(draft_params: dict, settings: dict) -> None:
    """ドラフトを DB に保存 → AddFixedPriceItem 実行 → 結果を反映。

    安全側: 先に DB に 'submitted' で INSERT し、API 失敗時も履歴が残る。
    再入防止 (HIGH-1 対策): 直前に同一セッションで出品成功している場合は no-op。
    UI disable が先行ガードだが、キーボードショートカット等の連打対策として二重防御。
    """
    prev = st.session_state.get(f"{_SS}add_result") or {}
    if prev.get("success") and prev.get("ebay_item_id"):
        logger.warning(
            f"_do_add blocked (double-submit): already as ItemID={prev.get('ebay_item_id')}"
        )
        st.warning(
            "この内容は既に出品済みです。再出品するにはフォームをクリアしてください。"
        )
        return

    # Step A: DB 先行 INSERT
    draft_data = _compose_draft_record(draft_params, status="submitted")
    try:
        draft_id = save_listing_draft(draft_data)
    except Exception as e:  # noqa: BLE001
        logger.exception("save_listing_draft failed")
        st.error(f"DB 保存に失敗: {e}")
        return

    st.session_state[f"{_SS}current_draft_id"] = draft_id

    # Step B: eBay API 呼出し
    with st.status(f"AddFixedPriceItem 実行中... (draft_id={draft_id})", expanded=True) as status:
        try:
            result = add_fixed_price_item_draft(
                draft_params=draft_params,
                config=settings,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("add_fixed_price_item_draft raised")
            result = {
                "success": False,
                "ebay_item_id": None,
                "ack": None,
                "fees": [],
                "scheduled_time": "",
                "errors": [f"exception: {e}"],
                "warnings": [],
                "raw_xml": "",
            }

        st.session_state[f"{_SS}add_result"] = result

        # Step C: DB に結果を反映
        try:
            if result.get("success") and result.get("ebay_item_id"):
                update_listing_draft_status(
                    draft_id=draft_id,
                    status="applied",
                    ebay_item_id=result.get("ebay_item_id"),
                )
                update_listing_draft(draft_id, {
                    "scheduled_time": result.get("scheduled_time"),
                })
                _sched = result.get('scheduled_time')
                if _sched:
                    st.write(
                        f"eBay ItemID={result.get('ebay_item_id')}, "
                        f"ScheduledTime={_sched}"
                    )
                    status.update(label="AddFixedPriceItem 成功 (Scheduled 登録)", state="complete")
                else:
                    st.write(
                        f"eBay ItemID={result.get('ebay_item_id')} (Active 即時公開)"
                    )
                    status.update(label="AddFixedPriceItem 成功 (Active 公開)", state="complete")

                # Step D: Promoted Listings Standard に自動 enroll (2026-04-21 追加)
                # settings.json の ebay_promoted_listings が enabled=true のときのみ走る。
                # 失敗しても Add 本体は成功扱い (enroll は事後操作で手動も可能)。
                try:
                    from monitor.ebay_promoted_listings import enroll_new_listing
                    pl_result = enroll_new_listing(
                        ebay_item_id=str(result.get("ebay_item_id")),
                        config=settings,
                    )
                    st.session_state[f"{_SS}pl_result"] = pl_result
                    if pl_result.get("skipped"):
                        st.write(f"Promoted Listings: skip ({pl_result.get('message')})")
                    elif pl_result.get("success"):
                        st.write(
                            f"Promoted Listings 登録成功: {pl_result.get('message')}"
                        )
                    else:
                        st.write(
                            "Promoted Listings 登録失敗 (Add は成功済): "
                            + "; ".join(pl_result.get("errors") or [])[:200]
                        )
                except Exception as pe:  # noqa: BLE001
                    logger.exception("enroll_new_listing raised")
                    st.session_state[f"{_SS}pl_result"] = {
                        "success": False, "errors": [f"exception: {pe}"],
                        "skipped": False, "message": "enroll 中に例外",
                    }
            else:
                err_msg = "; ".join(result.get("errors") or [])[:500]
                update_listing_draft_status(
                    draft_id=draft_id,
                    status="api_failed",
                    api_error_message=err_msg or "unknown",
                )
                st.write(f"API 失敗: {err_msg or '(メッセージなし)'}")
                status.update(label="AddFixedPriceItem 失敗 (DBに記録)", state="error")
        except Exception as e:  # noqa: BLE001
            logger.exception("update_listing_draft_status failed")
            st.warning(f"DB の status 更新に失敗: {e}")


def _render_add_result() -> None:
    result = st.session_state.get(f"{_SS}add_result")
    if not result:
        return

    with st.container(border=True):
        if result.get("success"):
            _sched_disp = result.get("scheduled_time")
            if _sched_disp:
                st.success(
                    f"eBay に登録成功: ItemID={result.get('ebay_item_id')}, "
                    f"Scheduled={_sched_disp}"
                )
            else:
                st.success(
                    f"eBay に Active 出品成功: ItemID={result.get('ebay_item_id')} "
                    f"(即時公開、Active タブで shipping 反映を確認可)"
                )
        else:
            st.error("eBay 登録に失敗しました。")

        fees = result.get("fees") or []
        if fees:
            st.markdown("**確定手数料**")
            for f in fees:
                st.markdown(
                    f"- {html.escape(str(f.get('name') or ''))}: "
                    f"{html.escape(str(f.get('fee') or ''))} "
                    f"{html.escape(str(f.get('currency') or ''))}"
                )

        errors = result.get("errors") or []
        if errors:
            st.error("**エラー**")
            for e in errors:
                st.markdown(f"- {html.escape(str(e))}")

        # Promoted Listings 結果表示
        pl_result = st.session_state.get(f"{_SS}pl_result")
        if pl_result:
            st.markdown("---")
            if pl_result.get("skipped"):
                st.info(
                    f"Promoted Listings: skip — {pl_result.get('message', '')}"
                )
            elif pl_result.get("success"):
                st.success(
                    f"Promoted Listings Standard 登録成功: "
                    f"{pl_result.get('message', '')}"
                )
            else:
                pl_errors = pl_result.get('errors') or []
                st.warning(
                    "Promoted Listings 登録失敗 (Add は成功済、後で手動登録可):"
                )
                for e in pl_errors[:5]:
                    st.markdown(f"- {html.escape(str(e))}")

        warnings = result.get("warnings") or []
        if warnings:
            st.warning("**警告**")
            for w in warnings:
                st.markdown(f"- {html.escape(str(w))}")

        draft_id = st.session_state.get(f"{_SS}current_draft_id")
        if draft_id:
            st.caption(f"listing_drafts.id = {draft_id}")


def _compose_draft_record(draft_params: dict, status: str = "submitted") -> dict:
    """draft_params + session_state を元に listing_drafts INSERT 用 dict を作る。"""
    product = st.session_state.get(f"{_SS}scraped_product") or {}
    reference = st.session_state.get(f"{_SS}reference_listing") or {}
    rank = st.session_state.get(f"{_SS}rank_classification") or {}
    listing = st.session_state.get(f"{_SS}generated_listing") or {}

    return {
        "sku": draft_params.get("sku") or None,
        "supplier_url": product.get("url"),
        "supplier_platform": product.get("platform"),
        "supplier_title_ja": product.get("title_ja"),
        "supplier_price_jpy": product.get("price_jpy"),
        "supplier_condition_ja": product.get("condition_ja"),
        "supplier_includes_ja": product.get("includes_ja"),
        "supplier_image_urls": list(product.get("image_urls") or []),
        "selected_image_urls": list(st.session_state.get(f"{_SS}selected_image_urls") or []),
        "reference_ebay_url": st.session_state.get(f"{_SS}reference_url") or None,
        "reference_ebay_item_id": (reference or {}).get("item_id") or None,
        "reference_category_id": (reference or {}).get("category_id") or None,
        "reference_item_specifics_keys": list((reference or {}).get("item_specifics_keys") or []),
        "reference_condition_id": (reference or {}).get("condition_id") or None,
        "rank_code": rank.get("rank_code"),
        "rank_label": rank.get("rank_label"),
        "quick_notes": None,  # Phase 5 では保持しない (Claude 生成の中間データ)
        "ebay_title": draft_params.get("ebay_title"),
        "ebay_description": listing.get("ebay_description"),
        "ebay_category_id": draft_params.get("ebay_category_id"),
        "ebay_category_name": listing.get("ebay_category_name"),
        "ebay_condition_id": draft_params.get("ebay_condition_id"),
        "item_specifics": dict(draft_params.get("item_specifics") or {}),
        "listing_price_usd": float(draft_params.get("listing_price_usd") or 0.0),
        "weight_g": int(st.session_state.get(f"{_SS}weight_g") or 0),
        "in_stock": 1 if st.session_state.get(f"{_SS}in_stock") else 0,
        "shipping_policy_id": draft_params.get("shipping_policy_id"),
        "template_id": st.session_state.get(f"{_SS}selected_template_id"),
        "status": status,
    }


# =========================================================================
# 保存済みドラフト サブタブ
# =========================================================================

def _render_saved_drafts_subtab() -> None:
    st.markdown(
        '<div style="font-size:12px;color:rgba(180,220,255,0.55);letter-spacing:2px;'
        'margin-bottom:6px;">S A V E D &nbsp; D R A F T S</div>',
        unsafe_allow_html=True,
    )

    _f1, _f2, _f3 = st.columns([1.2, 1.2, 4])
    with _f1:
        filter_options = ["(全て)", "draft", "submitted", "applied", "api_failed"]
        filter_idx = st.selectbox(
            "ステータス",
            options=list(range(len(filter_options))),
            format_func=lambda i: filter_options[i],
            index=0,
            key=f"{_SS}sel_draft_filter",
        )
    with _f2:
        limit = st.number_input(
            "最大表示件数", min_value=5, max_value=200, step=5, value=50,
            key=f"{_SS}input_draft_limit",
        )

    status_filter = filter_options[filter_idx] if filter_idx > 0 else None

    try:
        drafts = get_listing_drafts(status=status_filter, limit=int(limit))
        # M2 対策: 「(全て)」選択時に soft-deleted 行を除外 (誤って編集→Add 防止)
        if status_filter is None:
            drafts = [d for d in drafts if d.get("status") != "deleted"]
    except Exception as e:  # noqa: BLE001
        logger.exception("get_listing_drafts failed")
        st.error(f"保存済みドラフト一覧取得に失敗: {e}")
        return

    st.markdown(
        f'<div style="font-size:12px;color:rgba(180,220,255,0.6);margin:4px 0 10px;">'
        f'{len(drafts)} 件を表示</div>',
        unsafe_allow_html=True,
    )

    if not drafts:
        st.info("該当するドラフトがありません。")
        return

    for d in drafts:
        _render_draft_card(d)


def _render_draft_card(draft: dict) -> None:
    did = draft.get("id")
    status = draft.get("status") or "?"
    status_color = {
        "draft": "rgba(180,220,255,0.8)",
        "submitted": "rgba(240,200,48,0.9)",
        "applied": "rgba(118,255,3,0.9)",
        "api_failed": "rgba(255,120,120,0.9)",
        "deleted": "rgba(128,128,128,0.6)",
    }.get(status, "rgba(180,180,180,0.8)")

    with st.container(border=True):
        _h1, _h2, _h3, _h4, _h5 = st.columns([3.5, 1.3, 1.0, 1.0, 1.0])
        with _h1:
            title = (draft.get("ebay_title") or draft.get("supplier_title_ja") or "(タイトル未生成)")[:80]
            st.markdown(
                f'<div style="font-size:13px;color:rgba(220,235,250,0.95);padding-top:4px;">'
                f'{html.escape(title)}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div style="font-size:10px;color:rgba(180,220,255,0.55);font-family:monospace;">'
                f'#{did} / {html.escape(draft.get("supplier_platform") or "?")} / '
                f'created {html.escape(str(draft.get("created_at") or "?"))}</div>',
                unsafe_allow_html=True,
            )
        with _h2:
            st.markdown(
                f'<div style="padding-top:4px;">'
                f'<span style="display:inline-block;padding:2px 8px;border:1px solid {status_color};'
                f'color:{status_color};border-radius:3px;font-size:10px;letter-spacing:2px;">'
                f'{html.escape(status.upper())}</span></div>',
                unsafe_allow_html=True,
            )
        with _h3:
            price = draft.get("listing_price_usd")
            st.markdown(
                f'<div style="padding-top:4px;font-size:12px;color:rgba(180,220,255,0.8);">'
                f'${price:.2f}</div>' if price else
                f'<div style="padding-top:4px;font-size:12px;color:rgba(180,220,255,0.4);">-</div>',
                unsafe_allow_html=True,
            )
        with _h4:
            if st.button("編集", key=f"{_SS}btn_load_draft_{did}"):
                try:
                    full = get_listing_draft(int(did))
                    if full:
                        _load_draft_into_form(full)
                        st.success("フォームに読み込みました。「新規出品」サブタブに戻って確認してください。")
                    else:
                        st.error(f"draft id={did} が見つかりません。")
                except Exception as e:  # noqa: BLE001
                    logger.exception("load draft failed")
                    st.error(f"読み込み失敗: {e}")
                st.rerun()
        with _h5:
            if st.button("削除マーク", key=f"{_SS}btn_softdel_{did}"):
                try:
                    update_listing_draft_status(int(did), status="deleted")
                    st.success(f"draft id={did} を削除マークしました。")
                except Exception as e:  # noqa: BLE001
                    logger.exception("soft delete failed")
                    st.error(f"削除マーク失敗: {e}")
                st.rerun()

        ebay_id = draft.get("ebay_item_id")
        err = draft.get("api_error_message")
        scheduled = draft.get("scheduled_time")
        if ebay_id:
            _sched_label = scheduled or "Active (即時公開)"
            st.caption(
                f"eBay ItemID: {ebay_id}  /  {_sched_label}"
            )
        if err:
            st.error(f"最終 API エラー: {err}")


# =========================================================================
# 公開 API
# =========================================================================

def render_tab(settings: dict) -> None:
    """個別新規出品 メインタブを描画する。

    Args:
        settings: app.py の st.session_state.settings (dict)
    """
    _init_session_state()

    st.markdown(
        '<div style="font-size:13px;color:rgba(180,220,255,0.75);margin-bottom:14px;">'
        '仕入先URL から商品情報をスクレイプし、Claude が英語タイトルと description を生成、'
        'eBay Trading API で Active 出品 (即時公開) を行う。</div>',
        unsafe_allow_html=True,
    )

    # グローバル info / error
    info = st.session_state.get(f"{_SS}last_info")
    if info:
        st.info(info)
        st.session_state[f"{_SS}last_info"] = None
    err = st.session_state.get(f"{_SS}last_error")
    if err:
        st.error(err)
        st.session_state[f"{_SS}last_error"] = None

    sub_new, sub_saved, sub_templates = st.tabs([
        "新規出品", "保存済みドラフト", "テンプレート設定",
    ])

    with sub_new:
        try:
            templates = get_description_templates()
        except Exception as e:  # noqa: BLE001
            logger.exception("get_description_templates failed")
            st.error(f"テンプレ取得失敗 (「テンプレート設定」サブタブで作成してください): {e}")
            templates = []

        _render_step1_urls()
        _render_step2_scrape_result()
        _render_step3_listing_settings(templates)
        _render_step4_generation_preview()
        _render_step5_verify_add(settings)

    with sub_saved:
        _render_saved_drafts_subtab()

    with sub_templates:
        # delegate
        from tabs.tab_description_templates import render_tab as _tpl_render
        _tpl_render(settings)
