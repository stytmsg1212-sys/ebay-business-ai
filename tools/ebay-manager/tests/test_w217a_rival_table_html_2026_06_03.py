"""W217-A (2026-06-03): ライバル登録済 HTML table 純関数の回帰.

モックアップ準拠で `st.dataframe` を HTML <table> に置換した際に追加した
純関数 `_render_rival_table_html` の挙動を網羅。

- 最安行 (🥇 prefix) は `pm-rival-best` 行 class + 合計列 `pm-rival-tot-ok` 緑文字
- リンクは `<a target="_blank" rel="noopener noreferrer">開く</a>` (LinkColumn 等価)
- 価格 / 送料 / 合計 None は "—" 表記 (UI が落ちない)
- 入力 dict を mutate しない (純関数性、再描画安全)
- リンクの XSS 防御 (html.escape 経由)

保存ロジック / 競合 DB 書込 / dirty-flag 機構には一切触れない (表示のみ純関数)。
"""
from __future__ import annotations


def test_rival_table_html_empty_rows_returns_empty_string():
    """空 rows → 空文字列 (Streamlit 側で render skip 判定可)."""
    from tabs.tab_product_management import _render_rival_table_html
    assert _render_rival_table_html([]) == ""


def test_rival_table_html_basic_table_structure():
    """1 行入力で <table><thead><tbody><tr><td> の基本構造を含む."""
    from tabs.tab_product_management import _render_rival_table_html
    rows = [{
        "item id": "🥇 3357...1241",
        "リンク": "https://www.ebay.com/itm/335712341241",
        "商品価格": 78.0, "送料": 22.0, "合計": 100.0,
        "発送目安": "3 日後", "最終取得": "6/2",
    }]
    html = _render_rival_table_html(rows)
    assert "<table" in html
    assert "pm-rival-tbl" in html
    assert "<thead>" in html and "<tbody>" in html
    assert "<th" in html and "<td" in html
    # ヘッダ全列
    for col in ("item id", "価格", "送料", "合計", "発送", "取得", "リンク"):
        assert col in html


def test_rival_table_html_best_row_gets_best_class():
    """🥇 prefix の行は `pm-rival-best` class、合計列に `pm-rival-tot-ok` class."""
    from tabs.tab_product_management import _render_rival_table_html
    rows = [
        {"item id": "111", "リンク": "", "商品価格": 95.0, "送料": 25.0,
         "合計": 120.0, "発送目安": "5 日後", "最終取得": "6/2"},
        {"item id": "🥇 222", "リンク": "", "商品価格": 78.0, "送料": 22.0,
         "合計": 100.0, "発送目安": "3 日後", "最終取得": "6/2"},
    ]
    html = _render_rival_table_html(rows)
    # 最安行 (2 行目) に best class
    assert "pm-rival-best" in html
    # 合計列の緑強調 class
    assert "pm-rival-tot-ok" in html
    # 非最安行に best class が「混ざらない」検証
    # = pm-rival-best が "111" を含む <tr> に出ない
    # 簡易チェック: best class 出現位置が最初の "111" 行よりも後
    pos_best = html.find("pm-rival-best")
    pos_222 = html.find("222")
    pos_111 = html.find("111")
    assert pos_best > pos_111, "best class が非最安行に付いた"
    assert pos_best < pos_222, "best class が最安行の <tr> に付いていない"


def test_rival_table_html_link_column_target_blank():
    """リンク列は <a target="_blank" rel="noopener noreferrer">開く</a> 形式."""
    from tabs.tab_product_management import _render_rival_table_html
    rows = [{
        "item id": "111", "リンク": "https://www.ebay.com/itm/111",
        "商品価格": 100.0, "送料": 20.0, "合計": 120.0,
        "発送目安": "5 日後", "最終取得": "6/2",
    }]
    html = _render_rival_table_html(rows)
    assert 'href="https://www.ebay.com/itm/111"' in html
    assert 'target="_blank"' in html
    assert "noopener" in html
    assert ">開く<" in html


def test_rival_table_html_link_empty_shows_dash():
    """リンク列が空文字列 → "—" 表記 (リンクなし、UI が落ちない)."""
    from tabs.tab_product_management import _render_rival_table_html
    rows = [{
        "item id": "111", "リンク": "",
        "商品価格": 100.0, "送料": 20.0, "合計": 120.0,
        "発送目安": "5 日後", "最終取得": "6/2",
    }]
    html = _render_rival_table_html(rows)
    assert "—" in html
    assert "<a href" not in html


def test_rival_table_html_none_price_safe_dash():
    """価格 / 送料 / 合計 None → "—" 表記 (UI が落ちない)."""
    from tabs.tab_product_management import _render_rival_table_html
    rows = [{
        "item id": "111", "リンク": "",
        "商品価格": None, "送料": None, "合計": None,
        "発送目安": "—", "最終取得": "—",
    }]
    html = _render_rival_table_html(rows)
    # "—" が 3 個以上 (価格 / 送料 / 合計 + リンク + 発送 + 取得)
    assert html.count("—") >= 3


def test_rival_table_html_dollar_format_two_decimals():
    """価格 / 送料 / 合計 は $XX.XX 2 桁小数."""
    from tabs.tab_product_management import _render_rival_table_html
    rows = [{
        "item id": "111", "リンク": "",
        "商品価格": 78.0, "送料": 22.5, "合計": 100.5,
        "発送目安": "3 日後", "最終取得": "6/2",
    }]
    html = _render_rival_table_html(rows)
    assert "$78.00" in html
    assert "$22.50" in html
    assert "$100.50" in html


def test_rival_table_html_does_not_mutate_input():
    """純関数性: 入力 rows を mutate しない (再 render 安全)."""
    from tabs.tab_product_management import _render_rival_table_html
    rows = [{
        "item id": "🥇 222", "リンク": "https://www.ebay.com/itm/222",
        "商品価格": 78.0, "送料": 22.0, "合計": 100.0,
        "発送目安": "3 日後", "最終取得": "6/2",
    }]
    import copy
    snapshot = copy.deepcopy(rows)
    _ = _render_rival_table_html(rows)
    assert rows == snapshot, "入力 rows が mutate された"


def test_rival_table_html_escapes_link_xss():
    """リンク URL に <script> 等が混入しても escape されて出力."""
    from tabs.tab_product_management import _render_rival_table_html
    rows = [{
        "item id": "111",
        "リンク": 'https://evil.test/" onclick="alert(1)',
        "商品価格": 100.0, "送料": 20.0, "合計": 120.0,
        "発送目安": "5 日後", "最終取得": "6/2",
    }]
    html = _render_rival_table_html(rows)
    # 生 <script> や onclick は escape されているはず
    assert "onclick=" not in html or "&quot;" in html or "&#x27;" in html
    # double quote は escape された形
    assert 'onclick="alert(1)"' not in html


def test_rival_table_html_multi_row_only_one_best():
    """複数行で best class が 1 行のみ (重複付与なし)."""
    from tabs.tab_product_management import _render_rival_table_html
    rows = [
        {"item id": "111", "リンク": "", "商品価格": 95.0, "送料": 25.0,
         "合計": 120.0, "発送目安": "5 日後", "最終取得": "6/2"},
        {"item id": "🥇 222", "リンク": "", "商品価格": 78.0, "送料": 22.0,
         "合計": 100.0, "発送目安": "3 日後", "最終取得": "6/2"},
        {"item id": "333", "リンク": "", "商品価格": 110.0, "送料": 28.0,
         "合計": 138.0, "発送目安": "7 日後", "最終取得": "6/1"},
    ]
    html = _render_rival_table_html(rows)
    # pm-rival-best は 1 回のみ
    assert html.count("pm-rival-best") == 1
    # 各 item id は escape されて含まれる
    assert "111" in html
    assert "222" in html
    assert "333" in html
