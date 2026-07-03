"""ebaymag_driver: discover_product_id 検索 URL の URL エンコード回帰テスト (依頼ボード#40 / 2026-07-03).

L725 付近の検索 URL が f"...&name={query}" の生文字列連結で組まれており、
& / # / スペース連続 / 非 ASCII を含むタイトルでクエリ境界が壊れ discover 未発見の
一因になっていたバグの固定化。_build_stock_search_url に切り出し urllib.parse.quote
(safe="") で percent-encode する。
"""
import urllib.parse

import monitor.ebaymag_driver as D


def test_ascii_title_no_special_chars():
    """通常の ASCII タイトルは percent-encode されても素通し (スペースのみ %20 化)."""
    url = D._build_stock_search_url("Sony WH-1000XM4 Headphones")
    assert url == "https://ebaymag.com/stock?archived=true&name=Sony%20WH-1000XM4%20Headphones"


def test_ampersand_and_hash_and_double_space_encoded():
    """& / # / スペース連続はクエリ境界を壊すため必ず percent-encode される."""
    query = "Cable & Adapter #1  Set"
    url = D._build_stock_search_url(query)
    # クエリ文字列部分に生の & / # / 連続スペースが残っていないこと (境界破壊防止)
    query_part = url.split("&name=", 1)[1]
    assert "&" not in query_part
    assert "#" not in query_part
    assert "  " not in query_part
    assert url == (
        "https://ebaymag.com/stock?archived=true&name="
        "Cable%20%26%20Adapter%20%231%20%20Set"
    )


def test_non_ascii_title_encoded():
    """非 ASCII (アクセント記号 / 丸数字) も percent-encode される."""
    query = "Cordón Español ①"
    url = D._build_stock_search_url(query)
    query_part = url.split("&name=", 1)[1]
    # 非 ASCII の生文字がそのまま残っていない (percent-encode 済み = ASCII のみ)
    assert query_part.isascii()
    assert urllib.parse.unquote(query_part) == query


def test_url_has_exactly_two_query_params():
    """検索 URL のクエリパラメータは archived / name の 2 つのみ (エンコード漏れで増えない)."""
    url = D._build_stock_search_url("A & B # C")
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert set(params.keys()) == {"archived", "name"}
    assert params["name"] == ["A & B # C"]


def test_roundtrip_decodes_back_to_original_query():
    """encode → decode で元の query 文字列に戻ること (情報欠落なし)."""
    for query in ["普通の検索語", "Item & Co. #42", "  leading/trailing  ", "①②③"]:
        url = D._build_stock_search_url(query)
        query_part = url.split("&name=", 1)[1]
        assert urllib.parse.unquote(query_part) == query
