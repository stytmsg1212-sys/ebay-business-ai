#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W221 Tier2: 抽出タブモジュールの import smoke test.

`streamlit run` なしで import 時の SyntaxError / NameError / 循環 import を検出する。
各タブ抽出コミットでここに 1 関数追記する (設計: code-architect ブループリント §6)。
"""
import importlib


def _assert_renderable(module_name: str, func_name: str) -> None:
    m = importlib.import_module(module_name)
    assert hasattr(m, func_name), f"{module_name}.{func_name} が見つからない"
    assert callable(getattr(m, func_name)), f"{module_name}.{func_name} が callable でない"


def test_tab_sku_conversion_importable():
    _assert_renderable("tabs.tab_sku_conversion", "render_sku_conversion_tab")


def test_tab_video_learning_importable():
    _assert_renderable("tabs.tab_video_learning", "render_video_learning_tab")


def test_tab_agent_monitor_importable():
    _assert_renderable("tabs.tab_agent_monitor", "render_agent_monitor_tab")


def test_tab_model_comparison_importable():
    _assert_renderable("tabs.tab_model_comparison", "render_model_comparison_tab")


def test_tab_profit_calc_importable():
    _assert_renderable("tabs.tab_profit_calc", "render_profit_calc_tab")


def test_tab_customs_importable():
    _assert_renderable("tabs.tab_customs", "render_customs_tab")


def test_tab_ebay_sync_importable():
    _assert_renderable("tabs.tab_ebay_sync", "render_ebay_sync_tab")


def test_tab_manual_run_importable():
    _assert_renderable("tabs.tab_manual_run", "render_manual_run_tab")


def test_tab_lowest_price_importable():
    _assert_renderable("tabs.tab_lowest_price", "render_lowest_price_tab")


def test_tab_supplier_candidates_importable():
    _assert_renderable("tabs.tab_supplier_candidates", "render_supplier_candidates_tab")


def test_tab_inventory_monitor_importable():
    _assert_renderable("tabs.tab_inventory_monitor", "render_inventory_monitor_tab")


def test_tab_dashboard_importable():
    _assert_renderable("tabs.tab_dashboard", "render_dashboard_tab")
