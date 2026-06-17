#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依頼(2026-06-17): Yahoo!ショッピング在庫判定不能の回帰テスト.

site_configs の Yahoo!ショッピング設定は sold_out_text='在庫がありません' のみで
in_stock シグナルが空 → 在庫あり品が unknown 化していた。schema.org availability を
併用する _detect_yahoo_shopping_signals の判定を固定する。

実機検証(2026-06-17): 在庫あり=schema.org/InStock + 「カートに入れる」。
売切は実績ある '在庫がありません' を最優先(オーバーセル防止)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.scrapers import _detect_yahoo_shopping_signals  # noqa: E402


def test_instock_schema_available():
    st, sig = _detect_yahoo_shopping_signals('...<x>"availability":"http://schema.org/InStock"</x>...')
    assert st == 'available'
    assert 'InStock' in sig


def test_zaiko_nashi_unavailable():
    st, sig = _detect_yahoo_shopping_signals('...この商品は在庫がありません...')
    assert st == 'unavailable'
    assert '在庫がありません' in sig


def test_outofstock_schema_unavailable():
    st, sig = _detect_yahoo_shopping_signals('"availability":"https://schema.org/OutOfStock"')
    assert st == 'unavailable'
    assert 'OutOfStock' in sig


def test_sold_with_stale_instock_marked_unavailable():
    """売切ページに stale な InStock が混在しても、売切シグナル(在庫がありません/
    OutOfStock)を優先し unavailable と判定(誤『在庫あり』=オーバーセル防止)."""
    html = '"availability":"http://schema.org/InStock" ... 在庫がありません'
    assert _detect_yahoo_shopping_signals(html)[0] == 'unavailable'
    html2 = 'schema.org/InStock ... schema.org/OutOfStock'
    assert _detect_yahoo_shopping_signals(html2)[0] == 'unavailable'


def test_no_signal_unknown():
    st, sig = _detect_yahoo_shopping_signals('<html>nothing</html>')
    assert st is None
    assert sig == 'no signal matched'
