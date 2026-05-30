# -*- coding: utf-8 -*-
"""W192 (2026-05-30): Yahoo!ショッピングを 3 つ目の EC 直 URL 仕入先に追加.

検証スコープ:
  - price_extractor.extract_price_yahoo_shopping (meta og / itemprop / JSON-LD, value>0 ガード)
  - extract_price の ebayYS_ prefix routing
  - extract_price_by_url の URL ドメイン routing (W183 手動 URL 仕入先用)
  - DEFAULT_SITE_CONFIGS の Yahoo entry + migration v58 (実 HTML 一致値, 冪等性)
  - 在庫判定: 売切 (「在庫がありません」) → unavailable / 在庫有 → unknown (false-OOS を出さない)

実 HTML 検証 (2026-05-30, httpx): 価格は <meta property="product:price:amount"> が最安定、
CSS class は React ハッシュ化で利用不可、在庫有の clean marker 不在 = 在庫有は unknown 扱い.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


# =============================================================================
# 価格抽出: extract_price_yahoo_shopping
# =============================================================================

def test_yahoo_price_og_meta():
    """meta property=product:price:amount を最優先で抽出 (実機で最安定)."""
    from monitor.price_extractor import extract_price_yahoo_shopping
    html = '<meta property="product:price:amount" content="12800"/>'
    assert extract_price_yahoo_shopping(html) == 12800


def test_yahoo_price_og_meta_with_comma():
    """カンマ区切りの og price も抽出."""
    from monitor.price_extractor import extract_price_yahoo_shopping
    html = '<meta property="product:price:amount" content="1,280"/>'
    assert extract_price_yahoo_shopping(html) == 1280


def test_yahoo_price_itemprop_capital_p():
    """itemProp (大文字 P) の React 出力も case-insensitive で抽出 (fallback)."""
    from monitor.price_extractor import extract_price_yahoo_shopping
    html = '<meta itemProp="price" content="3980"/>'
    assert extract_price_yahoo_shopping(html) == 3980


def test_yahoo_price_jsonld_fallback():
    """JSON-LD "price" を最終 fallback で抽出."""
    from monitor.price_extractor import extract_price_yahoo_shopping
    html = '{"@type":"Product","offers":{"price":"5400","priceCurrency":"JPY"}}'
    assert extract_price_yahoo_shopping(html) == 5400


def test_yahoo_price_priority_og_over_others():
    """og price が存在すれば itemprop / JSON-LD より優先."""
    from monitor.price_extractor import extract_price_yahoo_shopping
    html = (
        '<meta property="product:price:amount" content="1000"/>'
        '<meta itemprop="price" content="2000"/>'
        '"price":"3000"'
    )
    assert extract_price_yahoo_shopping(html) == 1000


def test_yahoo_price_none_when_no_match():
    """価格要素が無ければ None (silent skip でなく明示)."""
    from monitor.price_extractor import extract_price_yahoo_shopping
    assert extract_price_yahoo_shopping("<html><body>no price</body></html>") is None


def test_yahoo_price_zero_guard():
    """value=0 は None (0 を baseline 確定すると永久 sticky silent skip, H2 同方針)."""
    from monitor.price_extractor import extract_price_yahoo_shopping
    assert extract_price_yahoo_shopping(
        '<meta property="product:price:amount" content="0"/>'
    ) is None


# =============================================================================
# routing: extract_price (SKU prefix) / extract_price_by_url (URL ドメイン)
# =============================================================================

def test_extract_price_routes_ebayYS_prefix():
    """ebayYS_ prefix で Yahoo 抽出器に振り分け."""
    from monitor.price_extractor import extract_price
    html = '<meta property="product:price:amount" content="7700"/>'
    assert extract_price(html, "ebayYS_p123") == 7700
    # Amazon / 楽天 selector では取れない (Yahoo 専用 selector)
    assert extract_price(html, "ebayAM_p123") is None


def test_extract_price_by_url_yahoo_domain():
    """shopping.yahoo.co.jp ドメインで Yahoo 抽出器に振り分け (W183 手動 URL)."""
    from monitor.price_extractor import extract_price_by_url
    html = '<meta property="product:price:amount" content="8800"/>'
    url = "https://store.shopping.yahoo.co.jp/someshop/abc123.html"
    assert extract_price_by_url(html, url) == 8800


def test_extract_price_by_url_amazon_and_rakuten():
    """amazon.co.jp / item.rakuten ドメインもそれぞれの抽出器に振り分け."""
    from monitor.price_extractor import extract_price_by_url
    html_am = '<span class="a-offscreen">￥4,580</span>'
    html_rt = '<meta itemprop="price" content="3980">'
    assert extract_price_by_url(html_am, "https://www.amazon.co.jp/dp/B0XXXX") == 4580
    assert extract_price_by_url(html_rt, "https://item.rakuten.co.jp/shop/x/") == 3980


def test_extract_price_by_url_unrelated_domain_none():
    """対象外ドメインは None (価格追跡対象外)."""
    from monitor.price_extractor import extract_price_by_url
    html = '<meta property="product:price:amount" content="100"/>'
    assert extract_price_by_url(html, "https://mercari.com/item/m123") is None


def test_extract_price_by_url_empty_inputs():
    """空 input は None."""
    from monitor.price_extractor import extract_price_by_url
    assert extract_price_by_url("", "https://amazon.co.jp/x") is None
    assert extract_price_by_url("<html/>", "") is None


# =============================================================================
# Yahoo site_config: DEFAULT_SITE_CONFIGS + migration v58
# =============================================================================

def test_default_site_configs_yahoo_values():
    """DEFAULT_SITE_CONFIGS の Yahoo entry が実 HTML 一致値 (url_keyword=ドメイン, 在庫有空)."""
    from monitor.database import DEFAULT_SITE_CONFIGS
    yahoo = next(
        (c for c in DEFAULT_SITE_CONFIGS if c.get("convert_url") == "ebayYS_"), None
    )
    assert yahoo is not None, "DEFAULT_SITE_CONFIGS に Yahoo entry がない"
    assert yahoo["url_keyword"] == "shopping.yahoo.co.jp"
    assert yahoo["sold_out_text"] == "在庫がありません"
    # 在庫有 clean marker 不在 → 在庫有 signal は空 (strict で売切が unknown 化するのを防ぐ)
    assert yahoo["in_stock_text1"] == ""
    assert yahoo["in_stock_text2"] == ""


def test_v58_yahoo_site_config_applied(tmp_db):
    """migration 適用後 site_configs の Yahoo 行が新値、user_version>=58."""
    from monitor.database import get_conn
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        row = c.execute(
            "SELECT url_keyword, sold_out_text, in_stock_text1, in_stock_text2 "
            "FROM site_configs WHERE convert_url='ebayYS_'"
        ).fetchone()
    assert ver >= 58, f"schema_ver={ver} < 58"
    assert row is not None, "Yahoo site_config 不在"
    assert row["url_keyword"] == "shopping.yahoo.co.jp"
    assert row["sold_out_text"] == "在庫がありません"
    assert (row["in_stock_text1"] or "") == ""
    assert (row["in_stock_text2"] or "") == ""


def test_v58_idempotent(tmp_db):
    """init_db 2 回連続で Yahoo site_config が保持される (Q2 冪等性)."""
    from monitor.database import get_conn, init_db
    init_db()  # 2 回目
    with get_conn() as c:
        row = c.execute(
            "SELECT url_keyword, sold_out_text FROM site_configs WHERE convert_url='ebayYS_'"
        ).fetchone()
    assert row["url_keyword"] == "shopping.yahoo.co.jp"
    assert row["sold_out_text"] == "在庫がありません"


# =============================================================================
# 在庫判定: 売切 → unavailable / 在庫有 → unknown (false-OOS を出さない)
# =============================================================================

_YAHOO_IN_STOCK = [""]  # clean marker 不在のため空 (実 site_config と同じ)
_YAHOO_SOLD_OUT = ["在庫がありません"]
_YAHOO_NO_PAGE = [""]


def test_yahoo_sold_out_detected():
    """売切ページ (「在庫がありません」含む) → unavailable."""
    from monitor.scrapers import _detect_status_single
    html = '<div>この商品は現在<span>在庫がありません</span></div>'
    status = _detect_status_single(
        html, _YAHOO_IN_STOCK, _YAHOO_SOLD_OUT, _YAHOO_NO_PAGE, strict=True
    )
    assert status == "unavailable"


def test_yahoo_in_stock_returns_unknown_not_false_oos():
    """在庫有ページ (売切文字列なし、「カートに入れる」あり) → unknown.

    在庫有 signal が空のため available とも unavailable とも判定せず unknown に倒す.
    = false-OOS (在庫有を在庫無と誤判定) を出さない安全方向 (W192 MVP の許容仕様).
    """
    from monitor.scrapers import _detect_status_single
    html = '<button>カートに入れる</button><div>数量</div>'
    status = _detect_status_single(
        html, _YAHOO_IN_STOCK, _YAHOO_SOLD_OUT, _YAHOO_NO_PAGE, strict=True
    )
    assert status is None, "在庫有ページが unknown 以外に判定された (false-OOS リスク)"


def test_yahoo_empty_in_stock_does_not_match_everything():
    """空 in_stock_text が全ページに match する事故が無い (active_is フィルタ確認)."""
    from monitor.scrapers import _detect_status_single
    # 売切でも在庫有 marker でもない無関係 HTML → unknown
    html = '<html><body>random content</body></html>'
    status = _detect_status_single(
        html, _YAHOO_IN_STOCK, _YAHOO_SOLD_OUT, _YAHOO_NO_PAGE, strict=False
    )
    assert status is None
