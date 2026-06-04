#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SKU → 仕入先URL変換ルール タブ (W221 Tier2 抽出、2026-06-04)。

app.py L7182-7347 から body をそのまま移植。挙動不変 (K2 surgical)。
SKU の用途は 2 つのみ (有/無在庫判定 + 無在庫の仕入先 URL 変換)。本タブは
後者 (sku_mapping_manager によるプリフィックス→URL 変換ルールの編集) のみを扱う。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st


def render_sku_conversion_tab() -> None:
    """SKU → 仕入先 URL 変換ルールの一覧 / 追加 / 編集 / テスト タブ."""
    from sku_mapping_manager import (
        load_mappings, add_mapping, update_mapping,
        delete_mapping, reset_to_defaults, generate_url, validate_sku,
    )

    st.title("SKU → 仕入先URL変換ルール")

    mappings = load_mappings()

    # タブ分割
    tab_view, tab_add, tab_edit, tab_test = st.tabs(["ルール一覧", "新規追加", "編集", "テスト"])

    # ========== ルール一覧 ==========
    with tab_view:
        st.subheader("現在のマッピングルール")

        if not mappings:
            st.info("ルールが登録されていません")
        else:
            # ルール表示用のDataFrame
            rules_data = []
            for prefix, config in mappings.items():
                rules_data.append({
                    "プリフィックス": prefix,
                    "仕入先名": config.get("name", ""),
                    "説明": config.get("description", ""),
                    "URL": config.get("common_url", ""),
                    "パターン": config.get("pattern", "")
                })

            df = pd.DataFrame(rules_data)
            st.dataframe(df, width="stretch", height=400)

            # リセットボタン
            col1, col2 = st.columns([3, 1])
            with col2:
                if st.button("RESET TO DEFAULTS", key="reset_btn"):
                    if reset_to_defaults():
                        st.success("デフォルトルールに戻しました")
                        st.rerun()
                    else:
                        st.error("リセットに失敗しました")

    # ========== 新規追加 ==========
    with tab_add:
        st.subheader("新しいマッピングルールを追加")

        col1, col2 = st.columns(2)
        with col1:
            new_prefix = st.text_input("プリフィックス", placeholder="例: ebayam_", key="add_prefix")
            new_name = st.text_input("仕入先名", placeholder="例: Amazon", key="add_name")
        with col2:
            new_desc = st.text_input("説明", placeholder="例: Amazon.co.jp", key="add_desc")
            new_url = st.text_input("ベースURL", placeholder="https://example.com/item/", key="add_url")

        new_pattern = st.text_input(
            "URLパターン",
            placeholder="例: {item_id} または m{item_id}",
            help="プレースホルダー {item_id} を使用できます",
            key="add_pattern"
        )

        if st.button("ルール追加", type="primary", key="add_btn"):
            if new_prefix and new_name and new_url and new_pattern:
                success, message = add_mapping(new_prefix, new_name, new_url, new_pattern, new_desc)
                if success:
                    st.success(message)
                    st.rerun()
                else:
                    st.error(message)
            else:
                st.error("すべてのフィールドを入力してください")

    # ========== 編集 ==========
    with tab_edit:
        st.subheader("既存ルールを編集")

        if not mappings:
            st.info("編集するルールがありません")
        else:
            # 編集対象を選択
            prefix_to_edit = st.selectbox(
                "編集するプリフィックスを選択",
                options=list(mappings.keys()),
                key="edit_prefix"
            )

            if prefix_to_edit:
                current = mappings[prefix_to_edit]

                col1, col2 = st.columns(2)
                with col1:
                    edit_name = st.text_input(
                        "仕入先名",
                        value=current.get("name", ""),
                        key="edit_name"
                    )
                    edit_desc = st.text_input(
                        "説明",
                        value=current.get("description", ""),
                        key="edit_desc"
                    )
                with col2:
                    edit_url = st.text_input(
                        "ベースURL",
                        value=current.get("common_url", ""),
                        key="edit_url"
                    )
                    edit_pattern = st.text_input(
                        "URLパターン",
                        value=current.get("pattern", ""),
                        key="edit_pattern"
                    )

                col_save, col_delete = st.columns(2)
                with col_save:
                    if st.button("保存", type="primary", key="update_btn"):
                        success, message = update_mapping(
                            prefix_to_edit, edit_name, edit_url, edit_pattern, edit_desc
                        )
                        if success:
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)

                with col_delete:
                    if st.button("削除", key="delete_btn"):
                        success, message = delete_mapping(prefix_to_edit)
                        if success:
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)

    # ========== テスト機能 ==========
    with tab_test:
        st.subheader("SKU → URL 変換テスト")
        st.write("SKUを入力して、実際に生成されるURLを確認します")

        test_sku = st.text_input(
            "テストするSKU",
            placeholder="例: ebayme_m81786287162",
            key="test_sku"
        )

        if test_sku:
            valid, prefix, item_id, message = validate_sku(test_sku)

            st.subheader("検証結果")
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("SKU", test_sku)
            with col2:
                st.metric("プリフィックス", prefix or "不明")
            with col3:
                st.metric("Item ID", item_id or "不明")

            st.write(f"**ステータス**: {message}")

            if valid and prefix and item_id:
                generated_url = generate_url(prefix, item_id)
                if generated_url:
                    st.success(f"生成URL: [{generated_url}]({generated_url})")

                    # クリップボードにコピー機能
                    st.code(generated_url, language="text")
                else:
                    st.error("URLを生成できませんでした")
