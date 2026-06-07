#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""利益計算タブ (W221 Tier2 抽出、2026-06-04)。

app.py L2219-2351 から body をそのまま移植。挙動不変 (K2 surgical)。
仕入れ値・販売価格・関税パターン・送料を入力し calculator で利益を試算する。
money 表示タブ: 数値・計算ロジックは1文字も変えていない。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st


def render_profit_calc_tab(s: dict) -> None:
    """利益計算タブ. s = st.session_state.settings (参照渡し)."""
    from calculator import calculate, CalcInput

    col1, col2, col3 = st.columns([1.2, 1, 1.2])

    # col3（レート・設定）を先に評価 = 関税率の live 値を col1 の関税プレビュー /
    # ②③送料prefill で参照可能にする（settings 既定値ではなく画面入力を真値化、UI内不整合を防ぐ）
    with col3:
        st.subheader("レート・設定")
        fx = st.number_input("為替レート（JPY/USD）", min_value=1.0, value=float(s["exchange_rate"]), step=1.0)
        duty_rate_input = st.number_input("関税率（%）", min_value=0.0, value=float(s["duty_rate"]), step=1.0)
        pl_rate = st.number_input("PL広告費（%）", min_value=0.0, value=float(s["promoted_listing_rate"]), step=0.5)
        tax_rate = st.number_input("消費税（%）", min_value=0.0, value=float(s["consumption_tax_rate"]), step=1.0)
        point_rate = st.number_input("ポイント付与（%）", min_value=0.0, value=float(s["point_reward_rate"]), step=0.5)

    with col1:
        st.subheader("商品・コスト情報")
        purchase = st.number_input("仕入れ値（円）", min_value=0, value=52400, step=100)
        item_price = st.number_input("販売価格（USD）", min_value=0.0, value=500.0, step=1.0, format="%.2f")
        category_id = st.number_input("カテゴリーID", min_value=0, value=58248, step=1)
        # W222 (2026-06-05): カテゴリID入力時に落札手数料(FVF)実効レートをライブ表示。
        # CSV未収録カテゴリは既定 12.7%(Store)/13.6%(NoStore) にフォールバック (その旨明示)。
        from calculator import get_ebay_fvf_rate, category_in_fee_table as _category_in_fee_table
        _store_plan = s.get("store_plan", "Premium")
        _fvf_preview = get_ebay_fvf_rate(int(category_id), float(item_price) or 1.0, _store_plan)
        _in_csv = _category_in_fee_table(int(category_id))
        st.caption(
            f"落札手数料 (このカテゴリ): **{_fvf_preview*100:.2f}%**"
            + ("" if _in_csv else " ⚠️ カテゴリ未収録のため既定レート")
        )
        duty_pattern_label = st.selectbox(
            "関税パターン",
            [
                "① US向けDDP・US_Only（関税を商品価格に含む）",
                "②③ US向けDDP（関税を送料に乗せる）",
                "④ US以外DDU（バイヤーが現地で関税負担）",
            ],
            index=1,
        )
        shipping_override = None
        actual_duty_rate_for_calc = None  # ②③で「実際に払う関税」を分離計上する時のみ設定
        if duty_pattern_label.startswith("①"):
            duty_pattern = "included"
            duty_cost_preview = item_price * duty_rate_input / 100
            st.info(
                f"送料: $0.00（Free）／関税は実費計上: ${duty_cost_preview:.2f}"
                f"（商品価格 × {duty_rate_input:.0f}%）"
            )
        elif duty_pattern_label.startswith("④"):
            duty_pattern = "ddu"
            st.info("送料: $0.00／関税: バイヤー負担（seller負担なし）")
        else:  # ②③
            duty_pattern = "shipping"
            _prefill = round(item_price * duty_rate_input / 100, 2)
            st.markdown("**① お客様から徴収する額（＝eBay送料欄に設定する額・収入）**")
            shipping_override = st.number_input(
                "① 送料＝バイヤー徴収関税（USD・手入力）",
                min_value=0.0, value=float(_prefill), step=1.0, format="%.2f",
                help="eBayの送料欄に設定してバイヤーから受け取る額（=収入）。"
                     "初期値=商品価格×関税率。実際の出品送料に変更してください。",
                # 販売価格 or 関税率を変えたら prefill を再計算（widget state の古い値固定を防ぐ）
                key=f"calc_ship_ovr_{item_price}_{duty_rate_input}",
            )
            st.caption(f"自動算出値: ${_prefill:.2f}（商品価格 × {duty_rate_input:.0f}%）")

            # ② 自分が実際に払う関税（コスト）。レート設定の関税率(%)から自動計算、手で変更可。
            # ①(収入)と②(コスト)を分離計上 = 相殺しない (W212)。floor には非適用 (本タブ表示専用)。
            _duty_prefill = round(item_price * duty_rate_input / 100, 2)
            st.markdown("**② 自分が実際に払う関税（コスト・自動=販売価格×関税率・変更可）**")
            actual_duty_usd = st.number_input(
                "② 実際に払う関税（USD・自動計算）",
                min_value=0.0, value=float(_duty_prefill), step=1.0, format="%.2f",
                help="あなたが通関で実際に払う関税（=コスト）。レート設定の関税率(%)から"
                     "自動計算(販売価格×関税率)。違う場合は手で変更できます。",
                key=f"calc_actual_duty_{item_price}_{duty_rate_input}",
            )
            st.caption(
                f"自動算出値: ${_duty_prefill:.2f}（販売価格 × {duty_rate_input:.0f}%）／"
                "①(お客様から徴収=収入) と ②(実際に払う関税=コスト) は相殺せず別々に計上します。"
            )
            actual_duty_rate_for_calc = (
                actual_duty_usd / item_price if item_price > 0 else 0.0
            )

    with col2:
        st.subheader("発送情報")
        weight_g = st.number_input("重量（g）", min_value=0, value=3000, step=100)
        st.markdown("**サイズ（cm）**")
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            length = st.number_input("L", min_value=0.0, value=0.0, step=1.0)
        with sc2:
            width = st.number_input("W", min_value=0.0, value=0.0, step=1.0)
        with sc3:
            height = st.number_input("H", min_value=0.0, value=0.0, step=1.0)
        if length > 0 and width > 0 and height > 0:
            from calculator import get_chargeable_weight_kg
            charged_kg = get_chargeable_weight_kg(weight_g, length, width, height, 5000)
            actual_kg = weight_g / 1000
            vol_kg = (length * width * height) / 5000
            st.caption(f"実重量: {actual_kg:.2f}kg　容積重量: {vol_kg:.2f}kg　**課金重量: {charged_kg:.1f}kg**")
        else:
            st.caption(f"実重量のみ: {weight_g/1000:.2f}kg（サイズ未入力）")

    st.divider()
    if st.button("▶ 計算実行", type="primary", width="stretch"):
        calc_settings = dict(s)
        calc_settings.update({
            "exchange_rate": fx, "duty_rate": duty_rate_input,
            "promoted_listing_rate": pl_rate, "consumption_tax_rate": tax_rate,
            "point_reward_rate": point_rate,
        })
        inp = CalcInput(
            purchase_yen=purchase, item_price_usd=item_price,
            weight_g=weight_g, length_cm=length, width_cm=width, height_cm=height,
            category_id=int(category_id), country_code="US",
            duty_pattern=duty_pattern, shipping_usd_override=shipping_override,
            actual_duty_rate=actual_duty_rate_for_calc,
        )
        result = calculate(inp, calc_settings)

        st.subheader("費用内訳")
        left, right = st.columns(2)
        with left:
            fvf_pct = result.fvf_rate * 100
            data = {
                "項目": [
                    "売上（円）", "ポイント還元",
                    f"落札手数料 ({fvf_pct:.2f}%)",
                    f"海外決済手数料 ({calc_settings['intl_payment_rate']:.2f}%)",
                    "取引手数料", f"広告費 ({pl_rate:.2f}%)",
                    f"Payoneer手数料 ({calc_settings['payoneer_fee_rate']:.2f}%)",
                ],
                "金額（円）": [
                    f"¥{result.revenue:,.0f}",
                    f"¥{result.point_return:,.0f} (0.00%)" if result.point_return == 0 else f"¥{result.point_return:,.0f}",
                    f"¥{result.fvf:,.0f}", f"¥{result.intl_payment:,.0f}",
                    f"¥{result.transaction_fee:.2f}", f"¥{result.ad_fee:,.0f}",
                    f"¥{result.payoneer:,.0f}",
                ],
            }
            st.dataframe(pd.DataFrame(data), hide_index=True, width="stretch")
            st.metric("合計コスト（仕入れ除く）", f"¥{result.ebay_cost_subtotal:,.0f}")
        with right:
            if result.service_results:
                st.subheader("送料別利益")
                for sr in result.service_results:
                    color = "●" if sr.is_listable else "○"
                    st.markdown(f"**{color} {sr.service_name}　利益: ¥{sr.profit:,}　({sr.profit_rate*100:.1f}%)**")
                    sc1, sc2 = st.columns(2)
                    with sc1:
                        st.write(f"**課金重量**: {sr.charged_weight_kg:.1f}kg")
                        st.write(f"**ベース送料**: ¥{sr.base_rate:,.0f}")
                        st.write(f"**燃料サーチャージ**: ¥{sr.fuel_surcharge_amount:,.0f}")
                        st.write(f"**送料**: ¥{sr.shipping_display:,.0f}")
                        for name, amt in sr.additional_fees.items():
                            st.write(f"**{name}**: ¥{amt:,.0f}")
                        st.write(f"**合計送料**: ¥{sr.total_shipping:,.0f}")
                    with sc2:
                        st.metric("利益", f"¥{sr.profit:,}", delta=f"{sr.profit_rate*100:.1f}%")
                        st.metric("還付込利益", f"¥{sr.profit_with_refund:,}", delta=f"{sr.profit_with_refund_rate*100:.1f}%")
                        st.metric("消費税還付", f"¥{sr.tax_refund:,}")
                        if sr.is_listable:
                            st.success("推奨")
                        else:
                            st.error("利益不足")
            else:
                st.warning("選択されたサービスのデータが見つかりませんでした。設定タブでサービスを確認してください。")
