"""W183 (2026-05-28) EC 直接 URL 無在庫監視の unit test.

scope (codex-ec-direct-url-design.md 実装順 step 8):
- migration v55: ebay_listings / monitored_items に source_url_manual + source_url_updated_at
- init_db 2 回でデータ保持 (冪等性)
- set_listing_source_url_manual round-trip + find_site_config_by_url
- 手動 URL (source_url_manual=1) が upsert_item / upsert_ebay_listing /
  update_ebay_listing_sku の SKU 変更で上書きされない
- 楽天 (schema.org microdata) / Amazon (add-to-cart-button) の在庫判定
- Amazon anti-bot (Robot Check) は unknown (誤 OOS 防止)
- prefix 不一致の直接 URL が prepare_batch_items から落ちない (url_keyword fallback)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---- migration v55 ----

def test_w183_v55_migration_columns_present():
    from monitor.database import init_db, get_conn
    init_db()
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        el = {r[1] for r in c.execute("PRAGMA table_info(ebay_listings)").fetchall()}
        mi = {r[1] for r in c.execute("PRAGMA table_info(monitored_items)").fetchall()}
    assert ver >= 55, f"expected user_version >= 55, got {ver}"
    for col in ("source_url_manual", "source_url_updated_at"):
        assert col in el, f"ebay_listings.{col} missing"
        assert col in mi, f"monitored_items.{col} missing"


def test_w183_init_db_idempotent_retains_data():
    """init_db 2 回連続で手動 URL listing が保持される (冪等性)."""
    from monitor.database import init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_IDEM_1"
    url = "https://www.amazon.co.jp/dp/B0TESTIDEM1"
    upsert_ebay_listing(eid, sku="ebayAM_idem", title="idem test")
    assert set_listing_source_url_manual(eid, url, manual=True) is True
    init_db()  # 再実行
    with get_conn() as c:
        row = c.execute(
            "SELECT source_url, source_url_manual FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert row is not None, "listing が init_db 再実行で消失 (冪等性違反)"
    assert row[0] == url
    assert int(row[1]) == 1


# ---- find_site_config_by_url ----

def test_w183_find_site_config_by_url():
    from monitor.database import init_db, find_site_config_by_url
    init_db()
    rk = find_site_config_by_url("https://item.rakuten.co.jp/shop/abc123/")
    am = find_site_config_by_url("https://www.amazon.co.jp/dp/B0XXXX")
    assert rk is not None and rk.get("convert_url") == "ebayRT_"
    assert am is not None and am.get("convert_url") == "ebayAM_"
    assert find_site_config_by_url("") is None
    assert find_site_config_by_url("https://unknown-ec.example.com/item/1") is None


# ---- set / unset ----

def test_w183_set_and_unset_manual_url():
    from monitor.database import init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_SETUNSET"
    upsert_ebay_listing(eid, sku="ebayAM_x", title="t")
    url = "https://www.amazon.co.jp/dp/B0SETUNSET"
    assert set_listing_source_url_manual(eid, url, manual=True) is True
    with get_conn() as c:
        r = c.execute(
            "SELECT source_url, source_url_manual FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == url and int(r[1]) == 1
    # 固定解除
    assert set_listing_source_url_manual(eid, url, manual=False) is True
    with get_conn() as c:
        r2 = c.execute(
            "SELECT source_url_manual FROM ebay_listings WHERE ebay_item_id=?", (eid,)
        ).fetchone()
    assert int(r2[0]) == 0
    # listing 不在は False
    assert set_listing_source_url_manual("NO_SUCH_LISTING", url, manual=True) is False


# ---- 手動 URL 保護 (SKU 変更で上書きされない) ----

def test_w183_manual_url_survives_upsert_ebay_listing_sku_change():
    from monitor.database import init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_UEL"
    upsert_ebay_listing(eid, sku="ebayAM_old", title="t1")
    manual_url = "https://www.amazon.co.jp/dp/B0MANUAL"
    set_listing_source_url_manual(eid, manual_url, manual=True)
    # SKU を変更して再 upsert (sku_changed=True + is_manual=True 経路)
    upsert_ebay_listing(eid, sku="ebayAM_new", title="t2", current_price=9.9)
    with get_conn() as c:
        r = c.execute(
            "SELECT sku, source_url, source_url_manual FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == "ebayAM_new"          # SKU は追従
    assert r[1] == manual_url            # 手動 URL は維持
    assert int(r[2]) == 1


def test_w183_manual_url_survives_update_ebay_listing_sku():
    from monitor.database import init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual, update_ebay_listing_sku
    init_db()
    eid = "TESTW183_UELS"
    upsert_ebay_listing(eid, sku="ebayRT_old", title="t1")
    manual_url = "https://item.rakuten.co.jp/shop/manual-item/"
    set_listing_source_url_manual(eid, manual_url, manual=True)
    update_ebay_listing_sku(eid, "ebayRT_new")
    with get_conn() as c:
        r = c.execute(
            "SELECT sku, source_url, source_url_manual FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == "ebayRT_new"
    assert r[1] == manual_url
    assert int(r[2]) == 1


def test_w183_non_manual_listing_sku_change_still_resets():
    """回帰防止: source_url_manual=0 の通常 listing は従来通り SKU 変更で source_* reset."""
    from monitor.database import init_db, get_conn, upsert_ebay_listing, update_ebay_listing_sku
    init_db()
    eid = "TESTW183_NONMANUAL"
    upsert_ebay_listing(eid, sku="ebayme_old", title="t1")
    with get_conn() as c:
        c.execute(
            "UPDATE ebay_listings SET source_status='out_of_stock', risk_confirmed=1 WHERE ebay_item_id=?",
            (eid,),
        )
    update_ebay_listing_sku(eid, "ebayme_new")
    with get_conn() as c:
        r = c.execute(
            "SELECT sku, source_status, risk_confirmed FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == "ebayme_new"
    assert r[1] == "unknown"      # reset された (従来動作維持)
    assert int(r[2]) == 0


def test_w183_manual_url_survives_upsert_item():
    """monitored_items も source_url_manual=1 なら upsert_item で URL 維持."""
    from monitor.database import init_db, get_conn, upsert_ebay_listing, upsert_item, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_UI"
    upsert_ebay_listing(eid, sku="ebayAM_old", title="t")
    upsert_item(sku="ebayAM_old", ebay_item_id=eid, title="t")
    manual_url = "https://www.amazon.co.jp/dp/B0UPSERTITEM"
    set_listing_source_url_manual(eid, manual_url, manual=True)
    # SKU 変更で upsert_item 再呼出 (既存行 manual=1 経路)
    upsert_item(sku="ebayAM_new", ebay_item_id=eid, title="t2")
    with get_conn() as c:
        r = c.execute(
            "SELECT sku, source_url, source_url_manual FROM monitored_items WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == "ebayAM_new"
    assert r[1] == manual_url
    assert int(r[2]) == 1


# ---- 在庫判定 (楽天 / Amazon site_configs signal) ----

def _cfg_by_prefix():
    from monitor.database import get_site_configs
    return {c["convert_url"]: c for c in get_site_configs() if c.get("convert_url")}


def test_w183_rakuten_schema_org_detection():
    from monitor.database import init_db
    from monitor.scrapers import _detect_status_single
    init_db()
    rk = _cfg_by_prefix()["ebayRT_"]
    is_texts = [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")]
    so_texts = [rk.get("sold_out_text", "")]
    np_texts = [rk.get("no_page_text", "")]
    in_html = '<html><meta itemprop="availability" content="http://schema.org/InStock"></html>'
    oos_html = '<html><meta itemprop="availability" content="http://schema.org/OutOfStock"></html>'
    assert _detect_status_single(in_html, is_texts, so_texts, np_texts, strict=True) == "available"
    assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"


def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():
    from monitor.database import init_db
    from monitor.scrapers import _check_with_httpx
    init_db()
    rk = _cfg_by_prefix()["ebayRT_"]
    fake = MagicMock()
    fake.status_code = 200
    fake.text = """
    <html>
      <meta itemprop="availability" content="http://schema.org/OutOfStock">
      <script>
        {"itemInfoSku":{"features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
          "purchaseInfo":{"purchaseBySellType":{"purchaseCondition":"enabled"}},
          "variantMappedInventories":[{"sku":"m20-5806","quantity":0}],
          "newPurchaseSku":{"quantity":0}}}
      </script>
    </html>
    """
    with patch("monitor.scrapers.httpx.get", return_value=fake):
        r = _check_with_httpx(
            "https://item.rakuten.co.jp/tuzukiya/m20-5806/",
            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
            [rk.get("sold_out_text", "")],
            [rk.get("no_page_text", "")],
        )
    assert r == "available"


def test_w183_rakuten_sold_out_hidden_stock_is_unavailable():
    """真の売り切れ楽天 HIDDEN_STOCK 品は unavailable (code-review CRITICAL-1 回帰).

    実 OOS サンプル (rakuten_oos_raw.html) は purchaseCondition='sold-out' だが
    displayNormalCartButton=true (常時 true)。cart button を在庫信号にすると
    売り切れを在庫あり誤判定 → 受注後仕入れ不能 = eBay Defect 直結。
    """
    from monitor.database import init_db
    from monitor.scrapers import _check_with_httpx
    init_db()
    rk = _cfg_by_prefix()["ebayRT_"]
    fake = MagicMock()
    fake.status_code = 200
    fake.text = """
    <html>
      <meta itemprop="availability" content="http://schema.org/OutOfStock">
      <script>
        {"itemInfoSku":{"features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
          "purchaseInfo":{"purchaseBySellType":{"purchaseCondition":"sold-out"}},
          "variantMappedInventories":[{"sku":"240","quantity":0}]}}
      </script>
    </html>
    """
    with patch("monitor.scrapers.httpx.get", return_value=fake):
        r = _check_with_httpx(
            "https://item.rakuten.co.jp/kunishirodenki/249/",
            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
            [rk.get("sold_out_text", "")],
            [rk.get("no_page_text", "")],
        )
    assert r == "unavailable", f"真の売り切れ品が {r} と誤判定 (false in-stock = 仕入れ不能リスク)"


def test_w183_rakuten_body_oos_ignores_related_in_stock():
    """本体 sold-out + 関連商品 enabled 混在でも本体判定 (mirror bug 防止 / HIGH-1)."""
    from monitor.database import init_db
    from monitor.scrapers import _check_with_httpx
    init_db()
    rk = _cfg_by_prefix()["ebayRT_"]
    fake = MagicMock()
    fake.status_code = 200
    fake.text = """
    <html><script>
      {"itemInfoSku":{"purchaseInfo":{"purchaseBySellType":{"purchaseCondition":"sold-out"}}}}
      {"recommendItem":{"purchaseCondition":"enabled"}}
    </script></html>
    """
    with patch("monitor.scrapers.httpx.get", return_value=fake):
        r = _check_with_httpx(
            "https://item.rakuten.co.jp/shop/body-oos/",
            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
            [rk.get("sold_out_text", "")],
            [rk.get("no_page_text", "")],
        )
    assert r == "unavailable", f"関連商品 enabled に引きずられ本体 OOS が {r} 誤判定"


def test_w183_amazon_add_to_cart_detection():
    from monitor.database import init_db
    from monitor.scrapers import _detect_status_single
    init_db()
    am = _cfg_by_prefix()["ebayAM_"]
    is_texts = [am.get("in_stock_text1", ""), am.get("in_stock_text2", "")]
    so_texts = [am.get("sold_out_text", "")]
    np_texts = [am.get("no_page_text", "")]
    in_html = '<input id="add-to-cart-button" name="submit.add-to-cart" title="カートに入れる">'
    oos_html = '<div>現在在庫切れです</div>'
    assert _detect_status_single(in_html, is_texts, so_texts, np_texts, strict=True) == "available"
    assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"


def test_w183_amazon_captcha_is_unknown():
    """Amazon anti-bot ページ (Robot Check) は unknown = None (誤 OOS 防止)."""
    from monitor.scrapers import _check_with_httpx
    fake = MagicMock()
    fake.status_code = 200
    fake.text = "<html><title>Robot Check</title>Enter the characters you see below</html>"
    with patch("monitor.scrapers.httpx.get", return_value=fake):
        r = _check_with_httpx(
            "https://www.amazon.co.jp/dp/B0CAPTCHA",
            ['id="add-to-cart-button"'], ["現在在庫切れ"], ["この商品は現在お取り扱いできません"],
        )
    assert r is None


# ---- prepare_batch_items url_keyword fallback ----

def test_w183_prepare_batch_items_url_fallback():
    """prefix 不一致でも source_url の url_keyword で config 解決され batch に残る."""
    from monitor.database import init_db
    from monitor.scrapers import prepare_batch_items
    init_db()
    cfgs = _cfg_by_prefix()
    # SKU は既存 prefix のどれにも一致しない (直接 URL 監視商品) が URL は楽天
    items = [
        {"id": 1, "sku": "stock-direct-1", "source_url": "https://item.rakuten.co.jp/shop/xyz/"},
        {"id": 2, "sku": "ebayAM_known", "source_url": "https://www.amazon.co.jp/dp/B0KNOWN"},
        {"id": 3, "sku": "weird", "source_url": "https://no-config-site.example.com/item/9"},
        {"id": 4, "sku": "nourl", "source_url": ""},
    ]
    batch = prepare_batch_items(items, cfgs)
    ids = {b["id"] for b in batch}
    assert 1 in ids, "url_keyword fallback で楽天直接 URL が batch に残るべき"
    assert 2 in ids, "prefix 一致 (Amazon) は従来通り残る"
    assert 3 not in ids, "config 解決不能は除外 (ログ済)"
    assert 4 not in ids, "source_url 空は除外"


# ---- HIGH-1 回帰: ensure_monitor_coverage で手動 URL が汚染されない ----

def test_w183_manual_url_survives_ensure_monitor_coverage():
    """手動 URL listing が監視台帳に登録され、ensure_monitor_coverage で
    SKU 派生 URL に上書きされない (code-reviewer HIGH-1)."""
    from monitor.database import (
        init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual,
    )
    from tasks.task_ensure_monitor_coverage import run_ensure_monitor_coverage
    init_db()
    eid = "TESTW183_ENSURE"
    # 直接 URL 監視商品 (ebayAM_ SKU だが URL は SKU から導出不能、monitored 未登録)
    upsert_ebay_listing(eid, sku="ebayAM_directonly1", title="direct url item",
                        quantity_ebay=1)
    manual_url = "https://www.amazon.co.jp/dp/B0DIRECTONLY"
    assert set_listing_source_url_manual(eid, manual_url, manual=True) is True
    # set 時点で監視台帳に手動 URL 行が作られているはず (HIGH-1 fix)
    with get_conn() as c:
        r0 = c.execute(
            "SELECT source_url, source_url_manual FROM monitored_items WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r0 is not None, "set_listing_source_url_manual が監視台帳に行を作らない (silent unmonitored)"
    assert r0[0] == manual_url and int(r0[1]) == 1
    # 監視台帳補完を走らせても手動 URL が SKU 派生 URL に汚染されない
    run_ensure_monitor_coverage({})
    with get_conn() as c:
        r1 = c.execute(
            "SELECT source_url, source_url_manual FROM monitored_items WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r1 is not None
    assert r1[0] == manual_url, f"手動 URL が汚染された: {r1[0]}"
    assert int(r1[1]) == 1
