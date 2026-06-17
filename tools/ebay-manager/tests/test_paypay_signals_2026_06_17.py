#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依頼(2026-06-17): PayPayフリマ売切候補が次々提示される事故の回帰テスト.

PayPay が HTML 構造を変更し旧シグナルが消失 → 全候補 unknown 化 → 売切が
検出されず提示され続けていた。schema.org availability + __NEXT_DATA__ status を
新シグナルにした _detect_paypay_signals の判定を固定する。

実機検証(2026-06-17): 売切=schema.org/OutOfStock + "status":"SOLD" /
在庫あり=schema.org/InStock + "status":"OPEN"。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.scrapers import _detect_paypay_signals  # noqa: E402


def test_outofstock_schema_unavailable():
    st, sig = _detect_paypay_signals('...<x>"availability":"https://schema.org/OutOfStock"</x>...')
    assert st == 'unavailable'
    assert 'OutOfStock' in sig


def test_status_sold_unavailable():
    st, sig = _detect_paypay_signals('foo "status":"SOLD" bar')
    assert st == 'unavailable'
    # 空白入りも許容
    st2, _ = _detect_paypay_signals('foo "status" : "SOLD" bar')
    assert st2 == 'unavailable'


def test_instock_schema_available():
    st, sig = _detect_paypay_signals('...<x>"availability":"https://schema.org/InStock"</x>...')
    assert st == 'available'
    assert 'InStock' in sig


def test_status_open_available():
    st, _ = _detect_paypay_signals('foo "status":"OPEN" bar')
    assert st == 'available'


def test_not_found_priority():
    st, sig = _detect_paypay_signals('この商品は存在しません')
    assert st == 'not_found'


def test_sold_page_with_stale_instock_marked_unavailable():
    """売切ページに stale な InStock JSON-LD が混在しても OutOfStock を優先し
    unavailable と判定する(誤『在庫あり』=オーバーセル/Defect を防ぐ最重要ケース)."""
    html = (
        '"availability":"https://schema.org/InStock" ... '
        '"availability":"https://schema.org/OutOfStock" ... "status":"SOLD"'
    )
    st, _ = _detect_paypay_signals(html)
    assert st == 'unavailable'


def test_old_signals_still_fallback():
    # 旧 server-side シグナルが残っていれば従来通り検出(HTML 揺れ対策)
    assert _detect_paypay_signals('購入日時 2026-06-01')[0] == 'unavailable'
    assert _detect_paypay_signals('関連商品をアプリで探す')[0] == 'unavailable'
    assert _detect_paypay_signals('購入手続きへ')[0] == 'available'


def test_no_signal_returns_unknown():
    st, sig = _detect_paypay_signals('<html>nothing relevant here</html>')
    assert st is None
    assert sig == 'no signal matched'
