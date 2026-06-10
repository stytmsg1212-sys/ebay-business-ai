#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBay連携 (出品同期・在庫数量・ランク自動付与) タブ (W221 Tier2 抽出、2026-06-04)。

app.py の `if _w134_sel == "eBay連携":` 分岐 body をそのまま移植。挙動不変 (K2 surgical)。
"""
from __future__ import annotations

import logging
import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)


def render_ebay_sync_tab(s: dict) -> None:
    # W221 Tier2 fix (2026-06-05): app.py top-level import をグローバル参照していた
    # 名前を関数内 lazy import で補完 (抽出漏れ修正、render 実行時 NameError 防止)。
    from monitor.database import get_ebay_listings_by_rank, get_rank_distribution_details, get_rank_stats, update_ebay_listing_rank
    from monitor.ebay_sync import auto_rank_all_listings_in_db, get_sync_report, sync_listings_from_ebay, sync_single_listing
    from monitor.rank_calculator import check_shipping_cost
    from tabs.tab_inventory_monitor import rank_to_stars
    from ui_cache import bump_db_version
    st.subheader("eBay出品との同期")
    ebay_col1, ebay_col2 = st.columns([2, 1])

    with ebay_col1:
        if st.button("eBay出品取得・同期", type="primary", width="stretch"):
            app_id = s.get("ebay_app_id", "")
            dev_id = s.get("ebay_dev_id", "")
            cert_id = s.get("ebay_cert_id", "")
            user_token = s.get("ebay_user_token", "")

            if not all([app_id, dev_id, cert_id, user_token]):
                st.error("eBay API認証情報が未設定です（設定タブ参照）")
            else:
                with st.status("eBay同期を実行中...", expanded=True) as status:
                    try:
                        st.write("▸ eBay APIから出品データを取得中...")
                        result = sync_listings_from_ebay(app_id, dev_id, cert_id, user_token)

                        st.write(f"▸ 同期: {result['synced']}件 / マッチ: {result['matched']}件")

                        if result["errors"] > 0:
                            st.write(f"▸ エラー: {result['errors']}件")
                            status.update(label=f"同期完了（エラー {result['errors']}件）", state="complete")
                        else:
                            status.update(label=f"同期完了 — {result['synced']}件取得", state="complete")

                        st.rerun()
                    except Exception as e:
                        status.update(label="同期失敗", state="error")
                        st.error(f"エラー: {e}")

    with ebay_col2:
        if st.button("自動ランク更新", type="secondary", width="stretch"):
            with st.status("ランク再計算中...", expanded=True) as status:
                try:
                    st.write("▸ Watch数・販売数をベースにスコア計算中...")
                    result = auto_rank_all_listings_in_db()
                    st.write(f"▸ {result['rank_assigned']}件のランクを更新")
                    if result["errors"] > 0:
                        st.write(f"▸ エラー: {result['errors']}件")
                        status.update(label=f"ランク更新完了（エラー {result['errors']}件）", state="complete")
                    else:
                        status.update(label=f"ランク更新完了 — {result['rank_assigned']}件", state="complete")
                    st.rerun()
                except Exception as e:
                    status.update(label="ランク更新失敗", state="error")
                    st.error(f"エラー: {e}")
        if st.button("レポート表示"):
            try:
                report = get_sync_report()
                st.json(report)
            except Exception as e:
                st.error(f"レポート生成エラー: {e}")

    # W176-followup (2026-05-27): 単一 listing 同期 (Item ID 指定)
    # 用途: 個別出品直後の確認 / 特定 listing の即時 metrics 更新 (~3 秒)。
    # 全体同期 (~1-2 分) を回さずに 1 件だけ最新化したい時用。
    st.markdown("---")
    st.markdown("**🎯 単一 listing 同期** (Item ID 指定で 1 件だけ最速 sync、~3 秒)")
    single_col1, single_col2 = st.columns([3, 1])
    with single_col1:
        target_item_id = st.text_input(
            "Item ID",
            key="ebay_sync_single_id",
            placeholder="358602711505 等の 12 桁数字",
            label_visibility="collapsed",
        )
    with single_col2:
        # HIGH-3 fix: button 連打防御 (eBay Trading API 5000 calls/day 保護)。
        # session_state flag で実行中は disabled、完了時に解除。
        _busy = st.session_state.get("_ebay_sync_single_busy", False)
        if st.button("1件のみ同期", type="secondary", width="stretch",
                     key="ebay_sync_single_btn", disabled=_busy):
            st.session_state["_ebay_sync_single_busy"] = True
            app_id = s.get("ebay_app_id", "")
            dev_id = s.get("ebay_dev_id", "")
            cert_id = s.get("ebay_cert_id", "")
            user_token = s.get("ebay_user_token", "")
            target = (target_item_id or "").strip()
            try:
                if not all([app_id, dev_id, cert_id, user_token]):
                    st.error("eBay API認証情報が未設定です（設定タブ参照）")
                elif not target:
                    st.warning("Item ID を入力してください")
                else:
                    with st.status(f"単一同期実行中... item_id={target}",
                                   expanded=True) as status:
                        try:
                            result = sync_single_listing(
                                target, app_id, dev_id, cert_id, user_token
                            )
                            st.write(f"▸ {result.get('message', '')}")
                            if result.get("success"):
                                st.write(
                                    f"▸ Title: {result.get('title', '')[:80]}"
                                )
                                bump_db_version()
                                status.update(
                                    label=f"同期成功 — {target}",
                                    state="complete",
                                )
                                # LOW fix: 連打防御 flag を解除してから rerun
                                st.session_state["_ebay_sync_single_busy"] = False
                                st.rerun()
                            else:
                                status.update(
                                    label=f"同期失敗 — {result.get('message', '')[:80]}",
                                    state="error",
                                )
                        except Exception as e:  # noqa: BLE001
                            logger.exception(
                                "sync_single_listing UI exception"
                            )
                            status.update(label=f"例外発生: {e}", state="error")
                            st.error(f"エラー: {e}")
            finally:
                # 失敗 / 例外 path も含めて必ず flag 解除 (st.rerun 通過時は
                # rerun 前に解除済のため重複代入のみ、害なし)。
                st.session_state["_ebay_sync_single_busy"] = False

    st.divider()
    st.subheader("eBay出品一覧")

    # ランク統計
    try:
        rank_stats = get_rank_stats()
        col1, col2, col3, col4, col5, col6, col7 = st.columns(7)
        with col1:
            st.metric("全体", sum(rank_stats.values()))
        with col2:
            st.metric("S (最優先)", rank_stats.get('S', 0))
        with col3:
            st.metric("A (高)", rank_stats.get('A', 0))
        with col4:
            st.metric("B (中高)", rank_stats.get('B', 0))
        with col5:
            st.metric("C (中)", rank_stats.get('C', 0))
        with col6:
            st.metric("D (低)", rank_stats.get('D', 0))
        with col7:
            st.metric("E (最低)", rank_stats.get('E', 0))
    except Exception as e:
        st.warning(f"ランク統計取得エラー: {e}")

    st.divider()

    try:
        ebay_items = get_ebay_listings_by_rank(order_by_rank=True)
        if not ebay_items:
            st.info("eBay出品が登録されていません。上記で同期してください。")
        else:
            # 出品テーブル表示
            df_data = []
            shipping_warnings = []

            for item in ebay_items:
                price = item.get('current_price', 0)
                shipping = item.get('shipping_cost', 0)

                # 送料検証
                shipping_check = check_shipping_cost(price, shipping)
                warning_indicator = "[!]" if shipping_check['status'] == "WARNING" else ""

                # 警告がある場合は記録
                if shipping_check['status'] == "WARNING":
                    shipping_warnings.append({
                        'item_id': item['ebay_item_id'],
                        'sku': item['sku'],
                        'title': item.get('title', ''),
                        'price': price,
                        'shipping': shipping,
                        'check': shipping_check
                    })

                eid = item["ebay_item_id"]
                df_data.append({
                    "WARN": warning_indicator,
                    "Rank": rank_to_stars(item.get("rank", "C")),
                    "Item ID": eid,
                    "eBay": f"https://www.ebay.com/itm/{eid}" if eid else "",
                    "SKU": item["sku"],
                    "Title": item["title"][:50] + "..." if len(item["title"] or "") > 50 else item["title"],
                    "Price": f"${price:.2f}",
                    "Shipping": f"${shipping:.2f}",
                    "eBay Qty": item.get("quantity_ebay", 0),
                    "Source Status": item.get("source_status", "unknown"),
                    "Last Sync": item.get("last_synced_at", "未同期")[:10] if item.get("last_synced_at") else "未同期",
                })

            df = pd.DataFrame(df_data)
            st.dataframe(
                df,
                width="stretch",
                hide_index=True,
                column_config={
                    "eBay": st.column_config.LinkColumn("eBay", display_text="開く", width="small"),
                },
            )

            st.caption(f"合計: {len(ebay_items)}件 | ソース紐付: {len([x for x in ebay_items if x.get('source_status')])}件 | 送料警告: {len(shipping_warnings)}件")

            # 送料警告詳細（クリック可能）
            if shipping_warnings:
                _show_ship_warn = st.checkbox(f"[!] Shipping cost warnings ({len(shipping_warnings)})", key="chk_ship_warn")
                if _show_ship_warn:
                    st.subheader("送料が適正範囲外の商品")
                    st.caption("商品価格の20%を基準に、±15%の範囲内に収まっていない商品を表示します")
                    st.divider()

                    for warning in shipping_warnings:
                        with st.container(border=True):
                            col1, col2 = st.columns([3, 2])

                            with col1:
                                st.markdown(f"**{warning['sku']}** - {warning['title'][:60]}")
                                st.caption(f"Item ID: {warning['item_id']}")

                            with col2:
                                check = warning['check']
                                st.metric("誤差率", f"{check['error_pct']:+.1f}%")

                            # 詳細情報を3列で表示
                            detail_col1, detail_col2, detail_col3 = st.columns(3)

                            with detail_col1:
                                st.caption("商品価格")
                                st.write(f"**${warning['price']:.2f}**")

                            with detail_col2:
                                st.caption("期待される送料（20%）")
                                st.write(f"**${check['expected']:.2f}**")
                                st.caption(f"許容範囲: ${check['expected'] * 0.85:.2f} - ${check['expected'] * 1.15:.2f}")

                            with detail_col3:
                                st.caption("実際の送料")
                                st.write(f"**${check['actual']:.2f}**")

                            # 警告メッセージを表示
                            if check['message']:
                                st.error(f"[!] {check['message']}")

                            st.caption(f"状態: {check['status']}")

            # ランク分布詳細（クリック可能）
            _show_rank_dist = st.checkbox("ランク分布（View/Watch/伸び率）", key="chk_rank_dist")
            if _show_rank_dist:
                try:
                    dist_details = get_rank_distribution_details()
                    for rank in ['S', 'A', 'B', 'C', 'D', 'E']:
                        if rank in dist_details:
                            info = dist_details[rank]
                            col_rank, col_count = st.columns([2, 1])
                            with col_rank:
                                st.markdown(f"**{rank} {rank_to_stars(rank)[0:10]}**")
                            with col_count:
                                st.metric("件数", info.get('count', 0))

                            if info.get('count', 0) > 0:
                                detail_col1, detail_col2, detail_col3, detail_col4, detail_col5 = st.columns(5)
                                with detail_col1:
                                    st.caption(f"平均Watch: {info.get('avg_watch', 0):.1f}")
                                with detail_col2:
                                    st.caption(f"平均View: {info.get('avg_view', 0):.1f}")
                                with detail_col3:
                                    st.caption(f"平均販売数(30d): {info.get('avg_sales', 0):.1f}")
                                with detail_col4:
                                    st.caption(f"平均Watch伸び: {info.get('avg_watch_growth', 0):.1f}%")
                                with detail_col5:
                                    st.caption(f"平均View伸び: {info.get('avg_view_growth', 0):.1f}%")
                except Exception as e:
                    st.warning(f"ランク分布詳細取得エラー: {e}")

            # ランク編集セクション
            _show_rank_edit = st.checkbox("ランク手動変更", key="chk_rank_edit")
            if _show_rank_edit:
                st.subheader("商品別ランク設定")
                st.caption("S（最優先）→ A（高）→ B（中高）→ C（中）→ D（低）→ E（最低）")

                edit_cols = st.columns([2, 1, 1])
                with edit_cols[0]:
                    selected_sku = st.selectbox(
                        "商品を選択",
                        options=[f"{item['sku']} - {item['title'][:40]}" for item in ebay_items],
                        key="rank_edit_sku"
                    )

                if selected_sku:
                    # 2026-05-20: item['sku']='' (eBay 側で SKU 空) でも誤マッチしない
                    # よう空文字ガード追加 (空文字は全 string に startswith True で
                    # 最初の item を誤選択する footgun、Codex 指摘)。
                    selected_item = next(
                        (item for item in ebay_items
                         if item['sku'] and selected_sku.startswith(item['sku'])),
                        None,
                    )
                    if selected_item:
                        current_rank = selected_item.get('rank', 'C')

                        with edit_cols[1]:
                            new_rank = st.selectbox(
                                "新しいランク",
                                options=['S', 'A', 'B', 'C', 'D', 'E'],
                                index=['S', 'A', 'B', 'C', 'D', 'E'].index(current_rank) if current_rank in ['S', 'A', 'B', 'C', 'D', 'E'] else 2,
                                key="rank_edit_value"
                            )

                        with edit_cols[2]:
                            if st.button("更新", key="rank_update_btn"):
                                try:
                                    update_ebay_listing_rank(selected_item['ebay_item_id'], new_rank)
                                    bump_db_version()  # W134 Step2: ランク変更後 read-cache 無効化
                                    st.success(f"{new_rank}に更新しました")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"更新エラー: {e}")
    except Exception as e:
        st.error(f"出品一覧取得エラー: {e}")
