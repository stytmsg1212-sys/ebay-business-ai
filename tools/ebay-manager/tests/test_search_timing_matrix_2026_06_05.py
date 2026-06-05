#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仕入先候補検索の timing matrix テスト (2026-06-05 user 仕様)。

matrix:
  メルカリ 売り切れ(在庫無)        -> 即検索
  メルカリ ページ削除(ページなし)  -> 24h後
  ヤフオク 売り切れ(落札済)        -> 即検索
  ヤフオク オークション終了(落札なし) -> 24h後
  ヤフオク ページ削除(ページなし)  -> 24h後

検証する不変条件: detect_inventory_changes の became_out_of_stock (= 即検索トリガー) は
「在庫無」遷移のみを含み、「ページなし」は含めない (ページなしは continuing_oos が
source_out_of_stock_since 24h 経過後に拾うため)。Yahoo「在庫無」のうち落札なし終了は
下流 _classify_yahoo_grace が 24h grace に振り分ける (本テストの範囲外、W100 テスト参照)。
"""
from tasks.task_inventory_check import detect_inventory_changes


def _changes(cur_status, prev_status, url="https://example.com/x", sku="ebay_t"):
    cur = [{"url": url, "sku": sku, "source": "テスト", "status": cur_status}]
    prev = {"results": [{"url": url, "status": prev_status}]}
    return detect_inventory_changes(cur, prev)


def test_zaikonashi_is_immediate_search():
    """売り切れ(在庫無) は即検索トリガー (became_out_of_stock に入る)."""
    r = _changes("在庫無", "在庫有")
    assert len(r["became_out_of_stock"]) == 1


def test_pagenashi_not_immediate():
    """ページ削除(ページなし) は即検索しない (24h待ち = became に入らない)."""
    r = _changes("ページなし", "在庫有")
    assert r["became_out_of_stock"] == []


def test_zaikoari_no_trigger():
    """在庫有のまま は何もトリガーしない."""
    r = _changes("在庫有", "在庫有")
    assert r["became_out_of_stock"] == []


def test_error_unknown_not_immediate():
    """エラー/不明 は即検索トリガーにしない (誤検知で空振り探索しない)."""
    assert _changes("エラー", "在庫有")["became_out_of_stock"] == []
    assert _changes("unknown", "在庫有")["became_out_of_stock"] == []
