"""W50 / 2026-04-30 Step A: tasks/task_inventory_check.py の統合 helper 単体テスト.

scheduler 経路と Streamlit 経路を 1 本体に統合する前段階で:
  - EN→JP status マッピング全 5 値網羅
  - monitored_items 形式 + check_items_batch 戻り値 → 既存 json schema 整形
  - silent skip 防止 (raw 欠落 item_id は "error" 扱い)
  - by_source 集計 + Unknown フォールバック
を確認する.
"""

import pytest

from tasks.task_inventory_check import (
    _EN_TO_JP_STATUS,
    _aggregate_stats,
    _build_results,
    _resolve_source_label,
)


# ──────────────────────────────────────────────────────────
# EN→JP status マッピング
# ──────────────────────────────────────────────────────────

def test_status_mapping_covers_5_known_values():
    assert _EN_TO_JP_STATUS == {
        "available":   "在庫有",
        "unavailable": "在庫無",
        "not_found":   "ページなし",
        "error":       "エラー",
        "unknown":     "不明",
    }


# ──────────────────────────────────────────────────────────
# _resolve_source_label
# ──────────────────────────────────────────────────────────

@pytest.fixture
def configs_by_prefix():
    return {
        "ebayme_": {"site_name": "メルカリ"},
        "ebayh_":  {"site_name": "ヤフオク"},
        "ebayPF_": {"site_name": "Paypayフリマ"},
    }


def test_resolve_source_label_mercari(configs_by_prefix):
    assert _resolve_source_label("ebayme_44731528581", configs_by_prefix) == "メルカリ"


def test_resolve_source_label_yahoo_auctions(configs_by_prefix):
    assert _resolve_source_label("ebayh_g1225005638", configs_by_prefix) == "ヤフオク"


def test_resolve_source_label_unknown_prefix(configs_by_prefix):
    assert _resolve_source_label("ebayUNKNOWN_123", configs_by_prefix) == "Unknown"


def test_resolve_source_label_empty_sku(configs_by_prefix):
    assert _resolve_source_label("", configs_by_prefix) == "Unknown"


def test_resolve_source_label_missing_site_name():
    bad = {"ebayme_": {}}  # site_name キー欠落
    assert _resolve_source_label("ebayme_xxx", bad) == "Unknown"


# ──────────────────────────────────────────────────────────
# _build_results
# ──────────────────────────────────────────────────────────

def test_build_results_basic(configs_by_prefix):
    items = [
        {"id": 1, "ebay_item_id": "358278773058", "sku": "ebayme_44731528581",
         "source_url": "https://jp.mercari.com/item/m44731528581"},
        {"id": 2, "ebay_item_id": "356663753260", "sku": "ebayh_g1225005638",
         "source_url": "https://page.auctions.yahoo.co.jp/jp/auction/g1225005638"},
    ]
    raw = {1: "available", 2: "unavailable"}
    out = _build_results(items, raw, configs_by_prefix)

    assert len(out) == 2
    assert out[0]["ebay_id"] == "358278773058"
    assert out[0]["sku"]     == "ebayme_44731528581"
    assert out[0]["source"]  == "メルカリ"
    assert out[0]["status"]  == "在庫有"
    assert out[0]["url"]     == "https://jp.mercari.com/item/m44731528581"
    assert "checked_at" in out[0]
    assert out[1]["source"]  == "ヤフオク"
    assert out[1]["status"]  == "在庫無"


def test_build_results_missing_id_falls_back_to_error(configs_by_prefix):
    """Q0 silent skip 防止: raw に無い item_id は status='エラー' になる."""
    items = [
        {"id": 1, "ebay_item_id": "X1", "sku": "ebayme_a", "source_url": "u1"},
        {"id": 99, "ebay_item_id": "X99", "sku": "ebayme_b", "source_url": "u99"},  # raw に無い
    ]
    raw = {1: "available"}  # id=99 欠落
    out = _build_results(items, raw, configs_by_prefix)

    assert out[0]["status"] == "在庫有"
    assert out[1]["status"] == "エラー"  # 「結果が来なかった = success」を許さない


def test_build_results_unknown_status_falls_back_to_jp_unknown(configs_by_prefix):
    items = [{"id": 1, "ebay_item_id": "X", "sku": "ebayme_a", "source_url": "u"}]
    raw = {1: "weird_unknown_value"}
    out = _build_results(items, raw, configs_by_prefix)
    assert out[0]["status"] == "不明"


# ──────────────────────────────────────────────────────────
# _aggregate_stats
# ──────────────────────────────────────────────────────────

def test_aggregate_stats_counts_by_status():
    results = [
        {"sku": "a", "source": "メルカリ", "status": "在庫有"},
        {"sku": "b", "source": "メルカリ", "status": "在庫無"},
        {"sku": "c", "source": "ヤフオク", "status": "在庫有"},
        {"sku": "d", "source": "ヤフオク", "status": "ページなし"},
        {"sku": "e", "source": "メルカリ", "status": "エラー"},
        {"sku": "f", "source": "メルカリ", "status": "不明"},  # 不明も error 集計
    ]
    s = _aggregate_stats(results)

    # top-level stats
    assert s["in_stock"]       == 2
    assert s["out_of_stock"]   == 1
    assert s["page_not_found"] == 1
    assert s["error"]          == 2  # エラー1 + 不明1

    # by_source: メルカリ
    m = s["by_source"]["メルカリ"]
    assert m["total"]         == 4
    assert m["in_stock"]      == 1
    assert m["out_of_stock"]  == 1
    assert m["error"]         == 2  # エラー1 + 不明1

    # by_source: ヤフオク
    y = s["by_source"]["ヤフオク"]
    assert y["total"]         == 2
    assert y["in_stock"]      == 1
    assert y["page_not_found"] == 1


def test_aggregate_stats_empty_returns_all_zero():
    s = _aggregate_stats([])
    assert s["in_stock"]       == 0
    assert s["out_of_stock"]   == 0
    assert s["page_not_found"] == 0
    assert s["error"]          == 0
    assert s["by_source"]      == {}


# ──────────────────────────────────────────────────────────
# 統合 invariance: build_results → aggregate_stats のラウンドトリップ
# ──────────────────────────────────────────────────────────

def test_invariance_build_then_aggregate(configs_by_prefix):
    items = [
        {"id": i, "ebay_item_id": f"E{i}", "sku": "ebayme_x", "source_url": f"u{i}"}
        for i in range(5)
    ]
    raw = {0: "available", 1: "available", 2: "unavailable", 3: "not_found", 4: "error"}
    results = _build_results(items, raw, configs_by_prefix)
    s = _aggregate_stats(results)

    assert s["in_stock"]       == 2
    assert s["out_of_stock"]   == 1
    assert s["page_not_found"] == 1
    assert s["error"]          == 1
    assert s["by_source"]["メルカリ"]["total"] == 5
