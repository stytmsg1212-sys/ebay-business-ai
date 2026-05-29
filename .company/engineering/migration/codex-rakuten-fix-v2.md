Reading additional input from stdin...
OpenAI Codex v0.130.0
--------
workdir: C:\Users\gucch\projects\claude\tools\ebay-manager
model: gpt-5.5
provider: openai
approval: never
sandbox: danger-full-access
reasoning effort: none
reasoning summaries: none
session id: 019e6f00-f723-71f0-99a7-e2cf97eb34f2
--------
user
You are fixing a money-critical bug in a Japanese cross-border eBay reselling tool (Python / Streamlit / SQLite) at tools/ebay-manager. You now have write access — INVESTIGATE, APPLY the fix to the code, and VERIFY it.

BUG: The Rakuten supplier-inventory check reports ebay listing 357039030883 (supplier URL https://item.rakuten.co.jp/tuzukiya/m20-5806/ , Mitutoyo KA-212 counter) as OUT OF STOCK, but the user confirms it is actually IN STOCK / 販売中 in a real browser. False OOS => false fulfillment-risk flag => financial harm.

DECISIVE NEW EVIDENCE (this is the key — your previous "defer to Playwright" idea is likely INSUFFICIENT):
The saved raw httpx HTML (data/tmp/rakuten_357_raw.html) contains an embedded JSON object (itemInfoSku / purchaseInfo) with:
- "purchaseCondition":"enabled"
- "features":{... "displayNormalCartButton":true, "inventoryDisplay":"HIDDEN_STOCK" ...}
- "variantMappedInventories":[{"sku":"m20-5806", ... "quantity":0}]  and  "newPurchaseSku":{... "quantity":0}
- The ONLY schema.org microdata is `<meta itemprop="availability" content="http://schema.org/OutOfStock">` (no InStock).

INTERPRETATION: This Rakuten shop uses inventoryDisplay = "HIDDEN_STOCK" (hides stock count), so it reports quantity:0 AND schema.org/OutOfStock EVEN WHEN THE ITEM IS PURCHASABLE. The authoritative in-stock signal for such pages is purchaseCondition:"enabled" and/or displayNormalCartButton:true — NOT the schema.org microdata and NOT quantity. Therefore:
- Simply deferring raw OutOfStock to Playwright may NOT fix it, because the rendered HTML can still carry the same schema.org/OutOfStock microdata. Prefer a signal that is correct on the raw HTML you already have (purchaseCondition / displayNormalCartButton).

WHERE THE BUG LIVES:
- monitor/scrapers.py: _check_with_httpx() substring-matches raw HTML; finding sold_out (OutOfStock) and no in_stock => _detect_status_single() returns "unavailable" definitively, so check_items_batch() never escalates and the item is wrongly OOS.
- The Rakuten site_config (monitor/database.py, convert_url='ebayRT_') uses in_stock_text1='itemprop="availability" content="http://schema.org/InStock"' / sold_out_text='itemprop="availability" content="http://schema.org/OutOfStock"'.

TASK — apply the fix and verify:
1. Make Rakuten (url contains "item.rakuten") availability detection correct for HIDDEN_STOCK shops. Recommended approach: for Rakuten, treat purchaseCondition:"enabled" and/or displayNormalCartButton:true in the raw HTML as the authoritative IN-STOCK signal, and do NOT let the schema.org OutOfStock microdata (or quantity:0) by itself produce a definitive "unavailable". A genuinely sold-out / disabled Rakuten page should have purchaseCondition NOT enabled (e.g. "disabled") and/or displayNormalCartButton:false — use that to detect true OOS. Decide the exact, robust rule yourself based on the saved HTML; keep it minimal.
2. Do NOT regress genuinely out-of-stock Rakuten items, nor Amazon / other sites (your change must be scoped to Rakuten url detection; other sites keep current behavior).
3. Edit ONLY monitor/scrapers.py and tests/test_w183_ec_direct_url.py. Keep changes minimal. Follow repo rules: Q0 no silent skip (log decisions), no SKU-as-key, no broad refactors.
4. VERIFY before finishing:
   (a) Add a regression test in tests/test_w183_ec_direct_url.py that feeds the relevant snippet (purchaseCondition:"enabled" + schema.org/OutOfStock together, Rakuten url) through _check_with_httpx (mock httpx.get returning that HTML) and asserts the result is "available" (NOT "unavailable").
   (b) Run: python -m pytest tests/test_w183_ec_direct_url.py -q   and make it pass.
   (c) If you have network, also run a quick check against the live URL https://item.rakuten.co.jp/tuzukiya/m20-5806/ via _check_with_httpx and confirm it resolves to "available". If network is blocked, verify against the saved file data/tmp/rakuten_357_raw.html instead (read it, run _check_with_httpx logic against its text).

Report: root cause, exact rule you implemented, files/functions changed, and the pytest + verification output proving the in-stock item now resolves to available.
codex
I’ll inspect the current scraper flow and the existing W183 tests first, then patch only the requested files and run the targeted pytest. After that I’ll do the live or saved-HTML verification through the same `_check_with_httpx` path.
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path monitor\\scrapers.py -TotalCount 260" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command 'rg -n "def _check_with_httpx|def _detect_status_single|item.rakuten|purchaseCondition|displayNormalCartButton|sold_out_text|in_stock_text" monitor/scrapers.py monitor/database.py tests/test_w183_ec_direct_url.py' in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path tests\\test_w183_ec_direct_url.py" in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 1039ms:
tests/test_w183_ec_direct_url.py:64:    rk = find_site_config_by_url("https://item.rakuten.co.jp/shop/abc123/")
tests/test_w183_ec_direct_url.py:124:    manual_url = "https://item.rakuten.co.jp/shop/manual-item/"
tests/test_w183_ec_direct_url.py:192:    is_texts = [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")]
tests/test_w183_ec_direct_url.py:193:    so_texts = [rk.get("sold_out_text", "")]
tests/test_w183_ec_direct_url.py:206:    is_texts = [am.get("in_stock_text1", ""), am.get("in_stock_text2", "")]
tests/test_w183_ec_direct_url.py:207:    so_texts = [am.get("sold_out_text", "")]
tests/test_w183_ec_direct_url.py:239:        {"id": 1, "sku": "stock-direct-1", "source_url": "https://item.rakuten.co.jp/shop/xyz/"},
monitor/scrapers.py:24:def _check_with_httpx(
monitor/scrapers.py:26:    in_stock_texts: list[str],
monitor/scrapers.py:27:    sold_out_texts: list[str],
monitor/scrapers.py:53:        return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
monitor/scrapers.py:64:def _detect_status_single(
monitor/scrapers.py:66:    in_stock_texts: list[str],
monitor/scrapers.py:67:    sold_out_texts: list[str],
monitor/scrapers.py:76:    active_so = [t for t in sold_out_texts if t]
monitor/scrapers.py:77:    active_is = [t for t in in_stock_texts if t]
monitor/scrapers.py:100:    in_stock_texts: list[str],
monitor/scrapers.py:101:    sold_out_texts: list[str],
monitor/scrapers.py:106:    result = _detect_status_single(rendered_text, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
monitor/scrapers.py:110:    result = _detect_status_single(content, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
monitor/scrapers.py:114:    result = _detect_status_single(content + "\n" + rendered_text, in_stock_texts, sold_out_texts, no_page_texts, strict=False)
monitor/scrapers.py:274:    in_stock_texts: list[str],
monitor/scrapers.py:275:    sold_out_texts: list[str],
monitor/scrapers.py:279:    result = _check_with_httpx(url, in_stock_texts, sold_out_texts, no_page_texts)
monitor/scrapers.py:285:    in_stock_texts: list[str],
monitor/scrapers.py:286:    sold_out_texts: list[str],
monitor/scrapers.py:291:    result = _check_with_httpx(url, in_stock_texts, sold_out_texts, no_page_texts)
monitor/scrapers.py:302:                    "in_stock": in_stock_texts,
monitor/scrapers.py:303:                    "sold_out": sold_out_texts,
monitor/scrapers.py:325:                "in_stock": in_stock_texts,
monitor/scrapers.py:326:                "sold_out": sold_out_texts,
monitor/scrapers.py:344:    in_stock = [site_config.get("in_stock_text1", ""), site_config.get("in_stock_text2", "")]
monitor/scrapers.py:345:    sold_out = [site_config.get("sold_out_text", "")]
monitor/scrapers.py:387:            "in_stock": [cfg.get("in_stock_text1", ""), cfg.get("in_stock_text2", "")],
monitor/scrapers.py:388:            "sold_out": [cfg.get("sold_out_text", "")],
monitor/scrapers.py:513:                "SELECT site_name, url_keyword, in_stock_text1, in_stock_text2, "
monitor/scrapers.py:514:                "       sold_out_text, no_page_text FROM site_configs"
monitor/database.py:19:        "in_stock_text1": "購入手続きへ",
monitor/database.py:20:        "in_stock_text2": "",
monitor/database.py:21:        "sold_out_text": "売り切れました",
monitor/database.py:29:        "in_stock_text1": "購入手続きへ",
monitor/database.py:30:        "in_stock_text2": "",
monitor/database.py:31:        "sold_out_text": "売り切れ",
monitor/database.py:39:        "in_stock_text1": "購入に進む",
monitor/database.py:40:        "in_stock_text2": "",
monitor/database.py:41:        "sold_out_text": "SOLD OUT",
monitor/database.py:49:        "in_stock_text1": "購入手続きへ",
monitor/database.py:50:        "in_stock_text2": "",
monitor/database.py:51:        "sold_out_text": "関連商品をアプリで探す",
monitor/database.py:59:        "in_stock_text1": "入札する",
monitor/database.py:60:        "in_stock_text2": "今すぐ落札",
monitor/database.py:61:        "sold_out_text": "このオークションは終了",
monitor/database.py:69:        "url_keyword": "item.rakuten",
monitor/database.py:73:        "in_stock_text1": 'itemprop="availability" content="http://schema.org/InStock"',
monitor/database.py:74:        "in_stock_text2": "",
monitor/database.py:75:        "sold_out_text": 'itemprop="availability" content="http://schema.org/OutOfStock"',
monitor/database.py:83:        "in_stock_text1": "在庫あり",
monitor/database.py:84:        "in_stock_text2": "買い物かごに入れる",
monitor/database.py:85:        "sold_out_text": "再入荷",
monitor/database.py:93:        "in_stock_text1": "カートに入れる",
monitor/database.py:94:        "in_stock_text2": "お届け先の商品をカートに",
monitor/database.py:95:        "sold_out_text": "",
monitor/database.py:105:        "in_stock_text1": 'id="add-to-cart-button"',
monitor/database.py:106:        "in_stock_text2": 'name="submit.add-to-cart"',
monitor/database.py:107:        "sold_out_text": "現在在庫切れ",
monitor/database.py:116:        "in_stock_text1": "カートに入れる",
monitor/database.py:117:        "in_stock_text2": "",
monitor/database.py:118:        "sold_out_text": "この商品は売り切れな",
monitor/database.py:126:        "in_stock_text1": "カートに入れる",
monitor/database.py:127:        "in_stock_text2": "",
monitor/database.py:128:        "sold_out_text": "品切れ中です。",
monitor/database.py:136:        "in_stock_text1": "注文数",
monitor/database.py:137:        "in_stock_text2": "",
monitor/database.py:138:        "sold_out_text": "SOLD OUT",
monitor/database.py:147:        "in_stock_text1": "ショッピングカートに",
monitor/database.py:148:        "in_stock_text2": "",
monitor/database.py:149:        "sold_out_text": "販売を終了しました",
monitor/database.py:157:        "in_stock_text1": "カートに入れる",
monitor/database.py:158:        "in_stock_text2": "",
monitor/database.py:159:        "sold_out_text": "完売御礼",
monitor/database.py:167:        "in_stock_text1": "カートに入れる",
monitor/database.py:168:        "in_stock_text2": "",
monitor/database.py:169:        "sold_out_text": "在庫がありません",
monitor/database.py:177:        "in_stock_text1": "カートに入れる",
monitor/database.py:178:        "in_stock_text2": "",
monitor/database.py:179:        "sold_out_text": "ただいま売り切れ中です",
monitor/database.py:187:        "in_stock_text1": "ショッピングカートへ",
monitor/database.py:188:        "in_stock_text2": "在庫あり",
monitor/database.py:189:        "sold_out_text": "品切れ",
monitor/database.py:197:        "in_stock_text1": "カートに入れる",
monitor/database.py:198:        "in_stock_text2": "",
monitor/database.py:199:        "sold_out_text": "品切れ",
monitor/database.py:208:        "in_stock_text1": "新品をカートに入れる",
monitor/database.py:209:        "in_stock_text2": "",
monitor/database.py:210:        "sold_out_text": "ご注文できません",
monitor/database.py:218:        "in_stock_text1": "残りあと",
monitor/database.py:219:        "in_stock_text2": "",
monitor/database.py:220:        "sold_out_text": "売り切れ",
monitor/database.py:229:        "in_stock_text1": "カートに入れる",
monitor/database.py:230:        "in_stock_text2": "",
monitor/database.py:231:        "sold_out_text": "ただいま品切れ",
monitor/database.py:239:        "in_stock_text1": "カートに入れる",
monitor/database.py:240:        "in_stock_text2": "",
monitor/database.py:241:        "sold_out_text": "ただいま品切れ中です",
monitor/database.py:249:        "in_stock_text1": "買い物かごへ入れる",
monitor/database.py:250:        "in_stock_text2": "",
monitor/database.py:251:        "sold_out_text": "",
monitor/database.py:259:        "in_stock_text1": "カートに追加する",
monitor/database.py:260:        "in_stock_text2": "",
monitor/database.py:261:        "sold_out_text": "売り切れ",
monitor/database.py:269:        "in_stock_text1": "カートに入れる",
monitor/database.py:270:        "in_stock_text2": "在庫あり",
monitor/database.py:271:        "sold_out_text": "在庫無し",
monitor/database.py:279:        "in_stock_text1": "カートに入れる",
monitor/database.py:280:        "in_stock_text2": "在庫あり",
monitor/database.py:281:        "sold_out_text": "申し訳ございません",
monitor/database.py:289:        "in_stock_text1": "カートに入れる",
monitor/database.py:290:        "in_stock_text2": "",
monitor/database.py:291:        "sold_out_text": "この商品は、ただいま在",
monitor/database.py:299:        "in_stock_text1": "バスケットに入れる",
monitor/database.py:300:        "in_stock_text2": "",
monitor/database.py:301:        "sold_out_text": "取扱い終了",
monitor/database.py:309:        "in_stock_text1": "カートに入れる",
monitor/database.py:310:        "in_stock_text2": "",
monitor/database.py:311:        "sold_out_text": "",
monitor/database.py:319:        "in_stock_text1": "カートに入れる",
monitor/database.py:320:        "in_stock_text2": "",
monitor/database.py:321:        "sold_out_text": "売り切れ",
monitor/database.py:329:        "in_stock_text1": "カートに追加する",
monitor/database.py:330:        "in_stock_text2": "",
monitor/database.py:331:        "sold_out_text": "",
monitor/database.py:339:        "in_stock_text1": "カートに入れる",
monitor/database.py:340:        "in_stock_text2": "",
monitor/database.py:341:        "sold_out_text": "",
monitor/database.py:349:        "in_stock_text1": "カートに入れる",
monitor/database.py:350:        "in_stock_text2": "",
monitor/database.py:351:        "sold_out_text": "SOLD OUT",
monitor/database.py:375:                in_stock_text1 TEXT,
monitor/database.py:376:                in_stock_text2 TEXT,
monitor/database.py:377:                sold_out_text TEXT,
monitor/database.py:483:                       (site_name, url_keyword, in_stock_text1, in_stock_text2,
monitor/database.py:484:                        sold_out_text, no_page_text, common_url, convert_url)
monitor/database.py:488:                        cfg["in_stock_text1"], cfg["in_stock_text2"],
monitor/database.py:489:                        cfg["sold_out_text"], cfg["no_page_text"],
monitor/database.py:1708:                       (site_name, url_keyword, in_stock_text1, in_stock_text2,
monitor/database.py:1709:                        sold_out_text, no_page_text, common_url, convert_url, is_active)
monitor/database.py:1710:                       SELECT site_name, url_keyword, in_stock_text1, in_stock_text2,
monitor/database.py:1711:                              sold_out_text, no_page_text, common_url, 'ebayyh_', is_active
monitor/database.py:1917:        # 2) 楽天市場 site_config の sold_out_text / no_page_text を UPDATE (DEFAULT_SITE_CONFIGS は空文字、追加が必要)
monitor/database.py:1934:                       SET sold_out_text = ?, no_page_text = ?
monitor/database.py:1936:                         AND (sold_out_text IS NULL OR sold_out_text = '')""",
monitor/database.py:2627:                    "in_stock_text1=?, in_stock_text2='', sold_out_text=? "
monitor/database.py:2628:                    "WHERE convert_url='ebayRT_' AND url_keyword='item.rakuten'",
monitor/database.py:2634:                    "in_stock_text1=?, in_stock_text2=?, sold_out_text=? "
monitor/database.py:2670:                   site_name=?, url_keyword=?, in_stock_text1=?, in_stock_text2=?,
monitor/database.py:2671:                   sold_out_text=?, no_page_text=?, common_url=?, convert_url=?, is_active=?
monitor/database.py:2675:                    config.get("in_stock_text1", ""), config.get("in_stock_text2", ""),
monitor/database.py:2676:                    config.get("sold_out_text", ""), config.get("no_page_text", ""),
monitor/database.py:2684:                   (site_name, url_keyword, in_stock_text1, in_stock_text2,
monitor/database.py:2685:                    sold_out_text, no_page_text, common_url, convert_url)
monitor/database.py:2689:                    config.get("in_stock_text1", ""), config.get("in_stock_text2", ""),
monitor/database.py:2690:                    config.get("sold_out_text", ""), config.get("no_page_text", ""),

 succeeded in 1146ms:
"""W183 (2026-05-28) EC 逶ｴ謗･ URL 辟｡蝨ｨ蠎ｫ逶｣隕悶・ unit test.

scope (codex-ec-direct-url-design.md 螳溯｣・・step 8):
- migration v55: ebay_listings / monitored_items 縺ｫ source_url_manual + source_url_updated_at
- init_db 2 蝗槭〒繝・・繧ｿ菫晄戟 (蜀ｪ遲画ｧ)
- set_listing_source_url_manual round-trip + find_site_config_by_url
- 謇句虚 URL (source_url_manual=1) 縺・upsert_item / upsert_ebay_listing /
  update_ebay_listing_sku 縺ｮ SKU 螟画峩縺ｧ荳頑嶌縺阪＆繧後↑縺・- 讌ｽ螟ｩ (schema.org microdata) / Amazon (add-to-cart-button) 縺ｮ蝨ｨ蠎ｫ蛻､螳・- Amazon anti-bot (Robot Check) 縺ｯ unknown (隱､ OOS 髦ｲ豁｢)
- prefix 荳堺ｸ閾ｴ縺ｮ逶ｴ謗･ URL 縺・prepare_batch_items 縺九ｉ關ｽ縺｡縺ｪ縺・(url_keyword fallback)
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
    """init_db 2 蝗樣｣邯壹〒謇句虚 URL listing 縺御ｿ晄戟縺輔ｌ繧・(蜀ｪ遲画ｧ)."""
    from monitor.database import init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_IDEM_1"
    url = "https://www.amazon.co.jp/dp/B0TESTIDEM1"
    upsert_ebay_listing(eid, sku="ebayAM_idem", title="idem test")
    assert set_listing_source_url_manual(eid, url, manual=True) is True
    init_db()  # 蜀榊ｮ溯｡・    with get_conn() as c:
        row = c.execute(
            "SELECT source_url, source_url_manual FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert row is not None, "listing 縺・init_db 蜀榊ｮ溯｡後〒豸亥､ｱ (蜀ｪ遲画ｧ驕募渚)"
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
    # 蝗ｺ螳夊ｧ｣髯､
    assert set_listing_source_url_manual(eid, url, manual=False) is True
    with get_conn() as c:
        r2 = c.execute(
            "SELECT source_url_manual FROM ebay_listings WHERE ebay_item_id=?", (eid,)
        ).fetchone()
    assert int(r2[0]) == 0
    # listing 荳榊惠縺ｯ False
    assert set_listing_source_url_manual("NO_SUCH_LISTING", url, manual=True) is False


# ---- 謇句虚 URL 菫晁ｭｷ (SKU 螟画峩縺ｧ荳頑嶌縺阪＆繧後↑縺・ ----

def test_w183_manual_url_survives_upsert_ebay_listing_sku_change():
    from monitor.database import init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_UEL"
    upsert_ebay_listing(eid, sku="ebayAM_old", title="t1")
    manual_url = "https://www.amazon.co.jp/dp/B0MANUAL"
    set_listing_source_url_manual(eid, manual_url, manual=True)
    # SKU 繧貞､画峩縺励※蜀・upsert (sku_changed=True + is_manual=True 邨瑚ｷｯ)
    upsert_ebay_listing(eid, sku="ebayAM_new", title="t2", current_price=9.9)
    with get_conn() as c:
        r = c.execute(
            "SELECT sku, source_url, source_url_manual FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == "ebayAM_new"          # SKU 縺ｯ霑ｽ蠕・    assert r[1] == manual_url            # 謇句虚 URL 縺ｯ邯ｭ謖・    assert int(r[2]) == 1


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
    """蝗槫ｸｰ髦ｲ豁｢: source_url_manual=0 縺ｮ騾壼ｸｸ listing 縺ｯ蠕捺擂騾壹ｊ SKU 螟画峩縺ｧ source_* reset."""
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
    assert r[1] == "unknown"      # reset 縺輔ｌ縺・(蠕捺擂蜍穂ｽ懃ｶｭ謖・
    assert int(r[2]) == 0


def test_w183_manual_url_survives_upsert_item():
    """monitored_items 繧・source_url_manual=1 縺ｪ繧・upsert_item 縺ｧ URL 邯ｭ謖・"""
    from monitor.database import init_db, get_conn, upsert_ebay_listing, upsert_item, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_UI"
    upsert_ebay_listing(eid, sku="ebayAM_old", title="t")
    upsert_item(sku="ebayAM_old", ebay_item_id=eid, title="t")
    manual_url = "https://www.amazon.co.jp/dp/B0UPSERTITEM"
    set_listing_source_url_manual(eid, manual_url, manual=True)
    # SKU 螟画峩縺ｧ upsert_item 蜀榊他蜃ｺ (譌｢蟄倩｡・manual=1 邨瑚ｷｯ)
    upsert_item(sku="ebayAM_new", ebay_item_id=eid, title="t2")
    with get_conn() as c:
        r = c.execute(
            "SELECT sku, source_url, source_url_manual FROM monitored_items WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == "ebayAM_new"
    assert r[1] == manual_url
    assert int(r[2]) == 1


# ---- 蝨ｨ蠎ｫ蛻､螳・(讌ｽ螟ｩ / Amazon site_configs signal) ----

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


def test_w183_amazon_add_to_cart_detection():
    from monitor.database import init_db
    from monitor.scrapers import _detect_status_single
    init_db()
    am = _cfg_by_prefix()["ebayAM_"]
    is_texts = [am.get("in_stock_text1", ""), am.get("in_stock_text2", "")]
    so_texts = [am.get("sold_out_text", "")]
    np_texts = [am.get("no_page_text", "")]
    in_html = '<input id="add-to-cart-button" name="submit.add-to-cart" title="繧ｫ繝ｼ繝医↓蜈･繧後ｋ">'
    oos_html = '<div>迴ｾ蝨ｨ蝨ｨ蠎ｫ蛻・ｌ縺ｧ縺・/div>'
    assert _detect_status_single(in_html, is_texts, so_texts, np_texts, strict=True) == "available"
    assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"


def test_w183_amazon_captcha_is_unknown():
    """Amazon anti-bot 繝壹・繧ｸ (Robot Check) 縺ｯ unknown = None (隱､ OOS 髦ｲ豁｢)."""
    from monitor.scrapers import _check_with_httpx
    fake = MagicMock()
    fake.status_code = 200
    fake.text = "<html><title>Robot Check</title>Enter the characters you see below</html>"
    with patch("monitor.scrapers.httpx.get", return_value=fake):
        r = _check_with_httpx(
            "https://www.amazon.co.jp/dp/B0CAPTCHA",
            ['id="add-to-cart-button"'], ["迴ｾ蝨ｨ蝨ｨ蠎ｫ蛻・ｌ"], ["縺薙・蝠・刀縺ｯ迴ｾ蝨ｨ縺雁叙繧頑桶縺・〒縺阪∪縺帙ｓ"],
        )
    assert r is None


# ---- prepare_batch_items url_keyword fallback ----

def test_w183_prepare_batch_items_url_fallback():
    """prefix 荳堺ｸ閾ｴ縺ｧ繧・source_url 縺ｮ url_keyword 縺ｧ config 隗｣豎ｺ縺輔ｌ batch 縺ｫ谿九ｋ."""
    from monitor.database import init_db
    from monitor.scrapers import prepare_batch_items
    init_db()
    cfgs = _cfg_by_prefix()
    # SKU 縺ｯ譌｢蟄・prefix 縺ｮ縺ｩ繧後↓繧ゆｸ閾ｴ縺励↑縺・(逶ｴ謗･ URL 逶｣隕門膚蜩・ 縺・URL 縺ｯ讌ｽ螟ｩ
    items = [
        {"id": 1, "sku": "stock-direct-1", "source_url": "https://item.rakuten.co.jp/shop/xyz/"},
        {"id": 2, "sku": "ebayAM_known", "source_url": "https://www.amazon.co.jp/dp/B0KNOWN"},
        {"id": 3, "sku": "weird", "source_url": "https://no-config-site.example.com/item/9"},
        {"id": 4, "sku": "nourl", "source_url": ""},
    ]
    batch = prepare_batch_items(items, cfgs)
    ids = {b["id"] for b in batch}
    assert 1 in ids, "url_keyword fallback 縺ｧ讌ｽ螟ｩ逶ｴ謗･ URL 縺・batch 縺ｫ谿九ｋ縺ｹ縺・
    assert 2 in ids, "prefix 荳閾ｴ (Amazon) 縺ｯ蠕捺擂騾壹ｊ谿九ｋ"
    assert 3 not in ids, "config 隗｣豎ｺ荳崎・縺ｯ髯､螟・(繝ｭ繧ｰ貂・"
    assert 4 not in ids, "source_url 遨ｺ縺ｯ髯､螟・


# ---- HIGH-1 蝗槫ｸｰ: ensure_monitor_coverage 縺ｧ謇句虚 URL 縺梧ｱ壽沒縺輔ｌ縺ｪ縺・----

def test_w183_manual_url_survives_ensure_monitor_coverage():
    """謇句虚 URL listing 縺檎屮隕門床蟶ｳ縺ｫ逋ｻ骭ｲ縺輔ｌ縲‘nsure_monitor_coverage 縺ｧ
    SKU 豢ｾ逕・URL 縺ｫ荳頑嶌縺阪＆繧後↑縺・(code-reviewer HIGH-1)."""
    from monitor.database import (
        init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual,
    )
    from tasks.task_ensure_monitor_coverage import run_ensure_monitor_coverage
    init_db()
    eid = "TESTW183_ENSURE"
    # 逶ｴ謗･ URL 逶｣隕門膚蜩・(ebayAM_ SKU 縺縺・URL 縺ｯ SKU 縺九ｉ蟆主・荳崎・縲［onitored 譛ｪ逋ｻ骭ｲ)
    upsert_ebay_listing(eid, sku="ebayAM_directonly1", title="direct url item",
                        quantity_ebay=1)
    manual_url = "https://www.amazon.co.jp/dp/B0DIRECTONLY"
    assert set_listing_source_url_manual(eid, manual_url, manual=True) is True
    # set 譎らせ縺ｧ逶｣隕門床蟶ｳ縺ｫ謇句虚 URL 陦後′菴懊ｉ繧後※縺・ｋ縺ｯ縺・(HIGH-1 fix)
    with get_conn() as c:
        r0 = c.execute(
            "SELECT source_url, source_url_manual FROM monitored_items WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r0 is not None, "set_listing_source_url_manual 縺檎屮隕門床蟶ｳ縺ｫ陦後ｒ菴懊ｉ縺ｪ縺・(silent unmonitored)"
    assert r0[0] == manual_url and int(r0[1]) == 1
    # 逶｣隕門床蟶ｳ陬懷ｮ後ｒ襍ｰ繧峨○縺ｦ繧よ焔蜍・URL 縺・SKU 豢ｾ逕・URL 縺ｫ豎壽沒縺輔ｌ縺ｪ縺・    run_ensure_monitor_coverage({})
    with get_conn() as c:
        r1 = c.execute(
            "SELECT source_url, source_url_manual FROM monitored_items WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r1 is not None
    assert r1[0] == manual_url, f"謇句虚 URL 縺梧ｱ壽沒縺輔ｌ縺・ {r1[0]}"
    assert int(r1[1]) == 1

 succeeded in 1210ms:
"""
莉募・蜈・し繧､繝医・蝨ｨ蠎ｫ繝√ぉ繝・け・・laywright + httpx 繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ・・URL縺ｫ逶ｴ謗･繧｢繧ｯ繧ｻ繧ｹ縺励※蝨ｨ蠎ｫ繝・く繧ｹ繝医ｒ讀懷・縺吶ｋ譁ｹ蠑・"""
import asyncio
import logging
import random
from typing import Optional

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


# ---- httpx 繝吶・繧ｹ縺ｮ繝√ぉ繝・け・磯ｫ倬溘・霆ｽ驥擾ｼ・----

def _check_with_httpx(
    url: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> Optional[str]:
    """httpx 縺ｧ HTML 繧貞叙蠕励＠繧ｭ繝ｼ繝ｯ繝ｼ繝画､懃ｴ｢縲ょ愛螳壻ｸ崎・縺ｪ繧・None縲・""
    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        if resp.status_code == 404:
            return "not_found"
        if resp.status_code != 200:
            logger.debug(f"httpx HTTP {resp.status_code}: {url}")
            return None

        html = resp.text
        # W183 (2026-05-28): Amazon 遲峨・ anti-bot 繝壹・繧ｸ (Robot Check / CAPTCHA)
        # 縺ｯ蝨ｨ蠎ｫ蛻､螳壻ｸ崎・ = unknown 謇ｱ縺・(None 縺ｧ Playwright fallback)縲ょ惠蠎ｫ蛻・ｌ縺ｨ
        # 隱､隱阪☆繧九→荳崎ｦ√↑蛟､荳九￡ / 蜃ｺ蜩∝●豁｢縺ｫ逶ｴ邨舌☆繧九◆繧∝ｿ・★ unknown 縺ｫ蛟偵☆縲・        low = html.lower()
        if "robot check" in low or "validatecaptcha" in low:
            logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
            return None
        return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
    except httpx.TimeoutException:
        logger.debug(f"httpx timeout: {url}")
        return None
    except Exception as e:
        logger.debug(f"httpx error: {url}: {e}")
        return None


# ---- 蛻､螳壹Ο繧ｸ繝・け ----

def _detect_status_single(
    content: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
    strict: bool = False,
) -> Optional[str]:
    """
    蜊倅ｸ繝・く繧ｹ繝茨ｼ・TML or rendered text・峨°繧牙愛螳壹ょ愛螳壻ｸ崎・縺ｪ繧・None縲・    strict=True: 蝨ｨ蠎ｫ譛峨→蝨ｨ蠎ｫ辟｡縺悟酔譎よ､懷・縺輔ｌ縺溷ｴ蜷医・ None・・PA蟇ｾ遲厄ｼ・    """
    active_np = [t for t in no_page_texts if t]
    active_so = [t for t in sold_out_texts if t]
    active_is = [t for t in in_stock_texts if t]

    found_np = any(t in content for t in active_np)
    found_so = any(t in content for t in active_so)
    found_is = active_is and any(t in content for t in active_is)

    # SPA蟇ｾ遲・ 蝨ｨ蠎ｫ譛峨・蝨ｨ蠎ｫ辟｡繝ｻ繝壹・繧ｸ縺ｪ縺励′蜈ｨ縺ｦ隕九▽縺九ｋ蝣ｴ蜷医・JS繝・Φ繝励Ξ繝ｼ繝域ｷｷ蜈･
    if strict and sum([found_np, found_so, found_is]) >= 2:
        logger.debug("Ambiguous detection (SPA?) - deferring to Playwright")
        return None

    if found_np:
        return "not_found"
    if found_so:
        return "unavailable"
    if found_is:
        return "available"
    return None


def _detect_status(
    content: str,
    rendered_text: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> str:
    """繝ｬ繝ｳ繝繝ｪ繝ｳ繧ｰ貂医∩繝・く繧ｹ繝亥━蜈遺・HTML 繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ縲・""
    # Step 1: 繝ｬ繝ｳ繝繝ｪ繝ｳ繧ｰ貂医∩繝・く繧ｹ繝茨ｼ医Θ繝ｼ繧ｶ繝ｼ縺ｫ隕九∴繧句・螳ｹ・峨〒蛻､螳・    result = _detect_status_single(rendered_text, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
    if result is not None:
        return result
    # Step 2: HTML蜈ｨ菴薙〒蛻､螳夲ｼ・S縺ｧ蜍慕噪逕滓・縺輔ｌ繧玖ｦ∫ｴ繧ゅき繝舌・・・    result = _detect_status_single(content, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
    if result is not None:
        return result
    # Step 3: strict辟｡縺励〒蜀榊愛螳夲ｼ・縺､縺ｧ繧りｦ九▽縺九ｌ縺ｰ蛻､螳夲ｼ・    result = _detect_status_single(content + "\n" + rendered_text, in_stock_texts, sold_out_texts, no_page_texts, strict=False)
    return result or "unknown"


# ---- Playwright 繝舌ャ繝√メ繧ｧ繝・け・医ヶ繝ｩ繧ｦ繧ｶ蜀榊茜逕ｨ・・----

async def _check_urls_batch_async(
    items: list[dict],
    headless: bool = True,
    use_chrome: bool = False,
) -> dict[int, str]:
    """
    隍・焚URL繧・縺､縺ｮ繝悶Λ繧ｦ繧ｶ繧､繝ｳ繧ｹ繧ｿ繝ｳ繧ｹ縺ｧ鬆・ｬ｡繝√ぉ繝・け縲・    items: [{id, url, in_stock, sold_out, no_page}, ...]
    Returns: {item_id: status}
    """
    results = {}
    launch_opts = {
        "headless": headless,
        "args": ["--disable-http2", "--disable-blink-features=AutomationControlled"],
    }
    if use_chrome:
        launch_opts["channel"] = "chrome"

    browser = None
    try:
        async with async_playwright() as p:
            # 繝悶Λ繧ｦ繧ｶ襍ｷ蜍輔・繝ｪ繝医Λ繧､・域怙螟ｧ3蝗橸ｼ・            for launch_attempt in range(3):
                try:
                    browser = await p.chromium.launch(**launch_opts)
                    break
                except Exception as e:
                    logger.debug(f"Browser launch attempt {launch_attempt + 1} failed: {e}")
                    if launch_attempt < 2:
                        await asyncio.sleep(2)
                    else:
                        raise

            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1280, "height": 800},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = await context.new_page()

            for item in items:
                item_id = item["id"]
                url = item["url"]
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if response and response.status == 404:
                        results[item_id] = "not_found"
                        continue

                    # SPA蟇ｾ遲・ networkidle + 繧ｳ繝ｳ繝・Φ繝・ｾ・ｩ・                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)

                    # SPA譛ｪ繝ｬ繝ｳ繝繝ｪ繝ｳ繧ｰ讀懷・竊定ｿｽ蜉蠕・ｩ滂ｼ域怙螟ｧ2蝗槭Μ繝医Λ繧､・・                    rendered_text = await page.inner_text("body")
                    for _ in range(2):
                        if len(rendered_text.strip()) >= 1500:
                            break
                        await asyncio.sleep(3)
                        rendered_text = await page.inner_text("body")

                    content = await page.content()

                    status = _detect_status(
                        content, rendered_text,
                        item["in_stock"], item["sold_out"], item["no_page"],
                    )
                    results[item_id] = status

                except PlaywrightTimeout:
                    logger.warning(f"Playwright timeout: {url}")
                    results[item_id] = "error"
                except Exception as e:
                    logger.warning(f"Playwright error: {url}: {e}")
                    results[item_id] = "error"

            if browser:
                await browser.close()

    except Exception as e:
        logger.error(f"Playwright error: {e}")
        for item in items:
            results.setdefault(item["id"], "error")
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    return results


def _run_playwright_batch(items: list[dict], headless: bool = True, use_chrome: bool = False) -> dict[int, str]:
    """蜷梧悄繝ｩ繝・ヱ繝ｼ"""
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            _check_urls_batch_async(items, headless=headless, use_chrome=use_chrome)
        )
    finally:
        loop.close()


# ---- 邨ｱ蜷医メ繧ｧ繝・け・・ttpx 竊・Playwright batch 竊・Chrome headed batch・・----

def check_items_batch(items_with_config: list[dict]) -> dict[int, str]:
    """
    隍・焚繧｢繧､繝・Β繧貞柑邇・噪縺ｫ繝√ぉ繝・け縲・    items_with_config: [{id, url, in_stock, sold_out, no_page}, ...]
    Returns: {item_id: status}
    """
    results = {}
    playwright_needed = []
    chrome_needed = []

    # Step 1: httpx 縺ｧ鬮倬溘メ繧ｧ繝・け
    for item in items_with_config:
        result = _check_with_httpx(item["url"], item["in_stock"], item["sold_out"], item["no_page"])
        if result is not None:
            results[item["id"]] = result
        else:
            playwright_needed.append(item)

    # Step 2: Playwright headless・医ヰ繝・メ縲√ヶ繝ｩ繧ｦ繧ｶ1蝗櫁ｵｷ蜍包ｼ・    if playwright_needed:
        pw_results = _run_playwright_batch(playwright_needed, headless=True, use_chrome=False)
        for item in playwright_needed:
            status = pw_results.get(item["id"], "error")
            if status not in ("error", "unknown"):
                results[item["id"]] = status
            else:
                chrome_needed.append(item)

    # Step 3: Chrome headed・域怙邨よ焔谿ｵ縲√ヰ繝・メ・・    if chrome_needed:
        chrome_results = _run_playwright_batch(chrome_needed, headless=False, use_chrome=True)
        for item in chrome_needed:
            results[item["id"]] = chrome_results.get(item["id"], "error")

    return results


def check_url_sync_httpx_only(
    url: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],

codex
The existing failure path is exactly as described: `_check_with_httpx` delegates raw Rakuten HTML to the generic substring detector, so a lone schema.org `OutOfStock` match becomes definitive before the browser fallback is considered. I’m adding a Rakuten-only precheck in `_check_with_httpx` so the embedded purchase JSON wins for Rakuten pages.
2026-05-28T14:33:51.198348Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines in C:\Users\gucch\projects\claude\tools\ebay-manager\monitor\scrapers.py:
]


# ---- httpx 繝吶・繧ｹ縺ｮ繝√ぉ繝・け・磯ｫ倬溘・霆ｽ驥擾ｼ・----

def _check_with_httpx(
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$i=1; Get-Content monitor'"\\scrapers.py -TotalCount 45 | ForEach-Object {\""'$i`t$_"; $i++}' in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 269ms:
1	"""
2	莉募・蜈・し繧､繝医・蝨ｨ蠎ｫ繝√ぉ繝・け・・laywright + httpx 繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ・・URL縺ｫ逶ｴ謗･繧｢繧ｯ繧ｻ繧ｹ縺励※蝨ｨ蠎ｫ繝・く繧ｹ繝医ｒ讀懷・縺吶ｋ譁ｹ蠑・"""
3	import asyncio
4	import logging
5	import random
6	from typing import Optional
7	
8	import httpx
9	from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
10	
11	logger = logging.getLogger(__name__)
12	
13	USER_AGENTS = [
14	    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
15	    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
16	    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
17	]
18	
19	
20	# ---- httpx 繝吶・繧ｹ縺ｮ繝√ぉ繝・け・磯ｫ倬溘・霆ｽ驥擾ｼ・----
21	
22	def _check_with_httpx(
23	    url: str,
24	    in_stock_texts: list[str],
25	    sold_out_texts: list[str],
26	    no_page_texts: list[str],
27	) -> Optional[str]:
28	    """httpx 縺ｧ HTML 繧貞叙蠕励＠繧ｭ繝ｼ繝ｯ繝ｼ繝画､懃ｴ｢縲ょ愛螳壻ｸ崎・縺ｪ繧・None縲・""
29	    ua = random.choice(USER_AGENTS)
30	    headers = {
31	        "User-Agent": ua,
32	        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
33	        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
34	    }
35	    try:
36	        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
37	        if resp.status_code == 404:
38	            return "not_found"
39	        if resp.status_code != 200:
40	            logger.debug(f"httpx HTTP {resp.status_code}: {url}")
41	            return None
42	
43	        html = resp.text
44	        # W183 (2026-05-28): Amazon 遲峨・ anti-bot 繝壹・繧ｸ (Robot Check / CAPTCHA)
45	        # 縺ｯ蝨ｨ蠎ｫ蛻､螳壻ｸ崎・ = unknown 謇ｱ縺・(None 縺ｧ Playwright fallback)縲ょ惠蠎ｫ蛻・ｌ縺ｨ

2026-05-28T14:34:04.831560Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines in C:\Users\gucch\projects\claude\tools\ebay-manager\monitor\scrapers.py:
]


# ---- httpx 繝吶・繧ｹ縺ｮ繝√ぉ繝・け・磯ｫ倬溘・霆ｽ驥擾ｼ・----
apply patch
patch: completed
C:\Users\gucch\projects\claude\tools\ebay-manager\monitor\scrapers.py
diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..3cb2945ea326c7619fe7a4d00c8ad0a4374c1299
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..3cb2945ea326c7619fe7a4d00c8ad0a4374c1299
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx

2026-05-28T14:34:15.248328Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines in C:\Users\gucch\projects\claude\tools\ebay-manager\monitor\scrapers.py:
# ---- httpx 繝吶・繧ｹ縺ｮ繝√ぉ繝・け・磯ｫ倬溘・霆ｽ驥擾ｼ・----
diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..3cb2945ea326c7619fe7a4d00c8ad0a4374c1299
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx

apply patch
patch: completed
C:\Users\gucch\projects\claude\tools\ebay-manager\monitor\scrapers.py
diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..22c61a73036680398bcc8217717b7ad826d9d91e
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..22c61a73036680398bcc8217717b7ad826d9d91e
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$i=43; Get-Content monitor'"\\scrapers.py | Select-Object -Skip 42 -First 30 | ForEach-Object {\""'$i`t$_"; $i++}' in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 419ms:
43	        return "unavailable"
44	
45	    return None
46	
47	
48	def _check_with_httpx(
49	    url: str,
50	    in_stock_texts: list[str],
51	    sold_out_texts: list[str],
52	    no_page_texts: list[str],
53	) -> Optional[str]:
54	    """httpx 縺ｧ HTML 繧貞叙蠕励＠繧ｭ繝ｼ繝ｯ繝ｼ繝画､懃ｴ｢縲ょ愛螳壻ｸ崎・縺ｪ繧・None縲・""
55	    ua = random.choice(USER_AGENTS)
56	    headers = {
57	        "User-Agent": ua,
58	        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
59	        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
60	    }
61	    try:
62	        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
63	        if resp.status_code == 404:
64	            return "not_found"
65	        if resp.status_code != 200:
66	            logger.debug(f"httpx HTTP {resp.status_code}: {url}")
67	            return None
68	
69	        html = resp.text
70	        # W183 (2026-05-28): Amazon 遲峨・ anti-bot 繝壹・繧ｸ (Robot Check / CAPTCHA)
71	        # 縺ｯ蝨ｨ蠎ｫ蛻､螳壻ｸ崎・ = unknown 謇ｱ縺・(None 縺ｧ Playwright fallback)縲ょ惠蠎ｫ蛻・ｌ縺ｨ
72	        # 隱､隱阪☆繧九→荳崎ｦ√↑蛟､荳九￡ / 蜃ｺ蜩∝●豁｢縺ｫ逶ｴ邨舌☆繧九◆繧∝ｿ・★ unknown 縺ｫ蛟偵☆縲・        low = html.lower()

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..22c61a73036680398bcc8217717b7ad826d9d91e
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$i=1; Get-Content monitor'"\\scrapers.py -TotalCount 50 | ForEach-Object {\""'$i`t$_"; $i++}' in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 344ms:
1	"""
2	莉募・蜈・し繧､繝医・蝨ｨ蠎ｫ繝√ぉ繝・け・・laywright + httpx 繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ・・URL縺ｫ逶ｴ謗･繧｢繧ｯ繧ｻ繧ｹ縺励※蝨ｨ蠎ｫ繝・く繧ｹ繝医ｒ讀懷・縺吶ｋ譁ｹ蠑・"""
3	import asyncio
4	import logging
5	import random
6	import re
7	from typing import Optional
8	
9	import httpx
10	from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
11	
12	logger = logging.getLogger(__name__)
13	
14	USER_AGENTS = [
15	    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
16	    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
17	    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
18	]
19	
20	
21	# ---- httpx 繝吶・繧ｹ縺ｮ繝√ぉ繝・け・磯ｫ倬溘・霆ｽ驥擾ｼ・----
22	
23	def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
24	    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
25	    if "item.rakuten" not in url.lower():
26	        return None
27	
28	    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
29	    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
30	
31	    if any(condition == "enabled" for condition in purchase_conditions):
32	        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
33	        return "available"
34	    if any(button.lower() == "true" for button in cart_buttons):
35	        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
36	        return "available"
37	
38	    if purchase_conditions:
39	        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
40	        return "unavailable"
41	    if any(button.lower() == "false" for button in cart_buttons):
42	        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
43	        return "unavailable"
44	
45	    return None
46	
47	
48	def _check_with_httpx(
49	    url: str,
50	    in_stock_texts: list[str],

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..22c61a73036680398bcc8217717b7ad826d9d91e
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$i=72; Get-Content monitor'"\\scrapers.py | Select-Object -Skip 71 -First 16 | ForEach-Object {\""'$i`t$_"; $i++}' in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 427ms:
72	        # 隱､隱阪☆繧九→荳崎ｦ√↑蛟､荳九￡ / 蜃ｺ蜩∝●豁｢縺ｫ逶ｴ邨舌☆繧九◆繧∝ｿ・★ unknown 縺ｫ蛟偵☆縲・        low = html.lower()
73	        if "robot check" in low or "validatecaptcha" in low:
74	            logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
75	            return None
76	        return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
77	    except httpx.TimeoutException:
78	        logger.debug(f"httpx timeout: {url}")
79	        return None
80	    except Exception as e:
81	        logger.debug(f"httpx error: {url}: {e}")
82	        return None
83	
84	
85	# ---- 蛻､螳壹Ο繧ｸ繝・け ----
86	
87	def _detect_status_single(

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..22c61a73036680398bcc8217717b7ad826d9d91e
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],

apply patch
patch: completed
C:\Users\gucch\projects\claude\tools\ebay-manager\monitor\scrapers.py
diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")

codex
The Rakuten branch now returns before generic schema.org matching when the purchase JSON says it is purchasable. I’m adding the regression test through `_check_with_httpx` with mocked HTTP so it covers the actual short-circuit path that caused the false OOS.
apply patch
patch: completed
C:\Users\gucch\projects\claude\tools\ebay-manager\tests\test_w183_ec_direct_url.py
diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
diff --git a/tools/ebay-manager/tests/test_w183_ec_direct_url.py b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
index f85db190b7f4240442ebf85930d1ffba40c1ea00..2f4a9bb8234742cceaa07a0abcbfe82a4bf89c55
--- a/tools/ebay-manager/tests/test_w183_ec_direct_url.py
+++ b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
@@ -198,6 +198,34 @@
     assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
 
 
+def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():
+    from monitor.database import init_db
+    from monitor.scrapers import _check_with_httpx
+    init_db()
+    rk = _cfg_by_prefix()["ebayRT_"]
+    fake = MagicMock()
+    fake.status_code = 200
+    fake.text = """
+    <html>
+      <meta itemprop="availability" content="http://schema.org/OutOfStock">
+      <script>
+        {"itemInfoSku":{"purchaseCondition":"enabled",
+          "features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
+          "variantMappedInventories":[{"sku":"m20-5806","quantity":0}],
+          "newPurchaseSku":{"quantity":0}}}
+      </script>
+    </html>
+    """
+    with patch("monitor.scrapers.httpx.get", return_value=fake):
+        r = _check_with_httpx(
+            "https://item.rakuten.co.jp/tuzukiya/m20-5806/",
+            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
+            [rk.get("sold_out_text", "")],
+            [rk.get("no_page_text", "")],
+        )
+    assert r == "available"
+
+
 def test_w183_amazon_add_to_cart_detection():
     from monitor.database import init_db
     from monitor.scrapers import _detect_status_single

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
diff --git a/tools/ebay-manager/tests/test_w183_ec_direct_url.py b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
index f85db190b7f4240442ebf85930d1ffba40c1ea00..2f4a9bb8234742cceaa07a0abcbfe82a4bf89c55
--- a/tools/ebay-manager/tests/test_w183_ec_direct_url.py
+++ b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
@@ -198,6 +198,34 @@
     assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
 
 
+def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():
+    from monitor.database import init_db
+    from monitor.scrapers import _check_with_httpx
+    init_db()
+    rk = _cfg_by_prefix()["ebayRT_"]
+    fake = MagicMock()
+    fake.status_code = 200
+    fake.text = """
+    <html>
+      <meta itemprop="availability" content="http://schema.org/OutOfStock">
+      <script>
+        {"itemInfoSku":{"purchaseCondition":"enabled",
+          "features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
+          "variantMappedInventories":[{"sku":"m20-5806","quantity":0}],
+          "newPurchaseSku":{"quantity":0}}}
+      </script>
+    </html>
+    """
+    with patch("monitor.scrapers.httpx.get", return_value=fake):
+        r = _check_with_httpx(
+            "https://item.rakuten.co.jp/tuzukiya/m20-5806/",
+            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
+            [rk.get("sold_out_text", "")],
+            [rk.get("no_page_text", "")],
+        )
+    assert r == "available"
+
+
 def test_w183_amazon_add_to_cart_detection():
     from monitor.database import init_db
     from monitor.scrapers import _detect_status_single

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command 'python -m pytest tests/test_w183_ec_direct_url.py -q' in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 20474ms:
..............                                                           [100%]
14 passed in 19.11s

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
diff --git a/tools/ebay-manager/tests/test_w183_ec_direct_url.py b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
index f85db190b7f4240442ebf85930d1ffba40c1ea00..2f4a9bb8234742cceaa07a0abcbfe82a4bf89c55
--- a/tools/ebay-manager/tests/test_w183_ec_direct_url.py
+++ b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
@@ -198,6 +198,34 @@
     assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
 
 
+def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():
+    from monitor.database import init_db
+    from monitor.scrapers import _check_with_httpx
+    init_db()
+    rk = _cfg_by_prefix()["ebayRT_"]
+    fake = MagicMock()
+    fake.status_code = 200
+    fake.text = """
+    <html>
+      <meta itemprop="availability" content="http://schema.org/OutOfStock">
+      <script>
+        {"itemInfoSku":{"purchaseCondition":"enabled",
+          "features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
+          "variantMappedInventories":[{"sku":"m20-5806","quantity":0}],
+          "newPurchaseSku":{"quantity":0}}}
+      </script>
+    </html>
+    """
+    with patch("monitor.scrapers.httpx.get", return_value=fake):
+        r = _check_with_httpx(
+            "https://item.rakuten.co.jp/tuzukiya/m20-5806/",
+            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
+            [rk.get("sold_out_text", "")],
+            [rk.get("no_page_text", "")],
+        )
+    assert r == "available"
+
+
 def test_w183_amazon_add_to_cart_detection():
     from monitor.database import init_db
     from monitor.scrapers import _detect_status_single

codex
The targeted W183 suite passes with the new regression test. I’m doing the requested direct verification next, first against the live Rakuten URL since network is available; if that is inconclusive I’ll fall back to the saved raw HTML.
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "@'
from monitor.database import init_db, find_site_config_by_url
from monitor.scrapers import _check_with_httpx

url = \"https://item.rakuten.co.jp/tuzukiya/m20-5806/\"
init_db()
cfg = find_site_config_by_url(url)
result = _check_with_httpx(
    url,
    [cfg.get(\"in_stock_text1\", \"\"), cfg.get(\"in_stock_text2\", \"\")],
    [cfg.get(\"sold_out_text\", \"\")],
    [cfg.get(\"no_page_text\", \"\")],
)
print(result)
'@ | python -" in C:\Users\gucch\projects\claude\tools\ebay-manager
2026-05-28T14:35:19.703159Z ERROR codex_core::tools::router: error=Exit code: 1
Wall time: 0.4 seconds
Output:
  File "<stdin>", line 1
    ﻿from monitor.database import init_db, find_site_config_by_url
    ^
SyntaxError: invalid non-printable character U+FEFF

 exited 1 in 403ms:
  File "<stdin>", line 1
    ﻿from monitor.database import init_db, find_site_config_by_url
    ^
SyntaxError: invalid non-printable character U+FEFF

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
diff --git a/tools/ebay-manager/tests/test_w183_ec_direct_url.py b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
index f85db190b7f4240442ebf85930d1ffba40c1ea00..2f4a9bb8234742cceaa07a0abcbfe82a4bf89c55
--- a/tools/ebay-manager/tests/test_w183_ec_direct_url.py
+++ b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
@@ -198,6 +198,34 @@
     assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
 
 
+def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():
+    from monitor.database import init_db
+    from monitor.scrapers import _check_with_httpx
+    init_db()
+    rk = _cfg_by_prefix()["ebayRT_"]
+    fake = MagicMock()
+    fake.status_code = 200
+    fake.text = """
+    <html>
+      <meta itemprop="availability" content="http://schema.org/OutOfStock">
+      <script>
+        {"itemInfoSku":{"purchaseCondition":"enabled",
+          "features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
+          "variantMappedInventories":[{"sku":"m20-5806","quantity":0}],
+          "newPurchaseSku":{"quantity":0}}}
+      </script>
+    </html>
+    """
+    with patch("monitor.scrapers.httpx.get", return_value=fake):
+        r = _check_with_httpx(
+            "https://item.rakuten.co.jp/tuzukiya/m20-5806/",
+            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
+            [rk.get("sold_out_text", "")],
+            [rk.get("no_page_text", "")],
+        )
+    assert r == "available"
+
+
 def test_w183_amazon_add_to_cart_detection():
     from monitor.database import init_db
     from monitor.scrapers import _detect_status_single

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "python -c \"from monitor.database import init_db, find_site_config_by_url; from monitor.scrapers import _check_with_httpx; url='https://item.rakuten.co.jp/tuzukiya/m20-5806/'; init_db(); cfg=find_site_config_by_url(url); print(_check_with_httpx(url, [cfg.get('in_stock_text1',''), cfg.get('in_stock_text2','')], [cfg.get('sold_out_text','')], [cfg.get('no_page_text','')]))\"" in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 11628ms:
available

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
diff --git a/tools/ebay-manager/tests/test_w183_ec_direct_url.py b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
index f85db190b7f4240442ebf85930d1ffba40c1ea00..2f4a9bb8234742cceaa07a0abcbfe82a4bf89c55
--- a/tools/ebay-manager/tests/test_w183_ec_direct_url.py
+++ b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
@@ -198,6 +198,34 @@
     assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
 
 
+def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():
+    from monitor.database import init_db
+    from monitor.scrapers import _check_with_httpx
+    init_db()
+    rk = _cfg_by_prefix()["ebayRT_"]
+    fake = MagicMock()
+    fake.status_code = 200
+    fake.text = """
+    <html>
+      <meta itemprop="availability" content="http://schema.org/OutOfStock">
+      <script>
+        {"itemInfoSku":{"purchaseCondition":"enabled",
+          "features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
+          "variantMappedInventories":[{"sku":"m20-5806","quantity":0}],
+          "newPurchaseSku":{"quantity":0}}}
+      </script>
+    </html>
+    """
+    with patch("monitor.scrapers.httpx.get", return_value=fake):
+        r = _check_with_httpx(
+            "https://item.rakuten.co.jp/tuzukiya/m20-5806/",
+            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
+            [rk.get("sold_out_text", "")],
+            [rk.get("no_page_text", "")],
+        )
+    assert r == "available"
+
+
 def test_w183_amazon_add_to_cart_detection():
     from monitor.database import init_db
     from monitor.scrapers import _detect_status_single

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command 'git diff -- monitor/scrapers.py tests/test_w183_ec_direct_url.py' in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command 'git status --short -- monitor/scrapers.py tests/test_w183_ec_direct_url.py' in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 267ms:
 M monitor/scrapers.py
?? tests/test_w183_ec_direct_url.py

 succeeded in 268ms:
diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 963de13..a5b584c 100644
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@ URLに直接アクセスして在庫テキストを検出する方式
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@ USER_AGENTS = [
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -43,6 +69,22 @@ def _check_with_httpx(
             return None
 
         html = resp.text
+        # W183 (2026-05-28): Amazon 等の anti-bot ページ (Robot Check / CAPTCHA)
+        # は在庫判定不能 = unknown 扱い (None で Playwright fallback)。在庫切れと
+        # 誤認すると不要な値下げ / 出品停止に直結するため必ず unknown に倒す。
+        low = html.lower()
+        if "robot check" in low or "validatecaptcha" in low:
+            logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
+            return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
@@ -341,19 +383,38 @@ def check_item_by_config(item: dict, site_config: dict) -> str:
 
 
 def prepare_batch_items(items: list[dict], configs_by_prefix: dict) -> list[dict]:
-    """DB アイテムリストをバッチチェック用の形式に変換"""
+    """DB アイテムリストをバッチチェック用の形式に変換。
+
+    W183 (2026-05-28): SKU prefix に一致しない直接 URL 監視 (source_url_manual=1 の
+    Amazon/楽天 等、SKU 規則性の無い EC) は source_url の url_keyword で site_config を
+    解決する fallback を追加。除外したものは件数と理由をログに残す (Q0 silent-skip 防止)。
+    """
     batch = []
+    dropped_no_url = 0
+    dropped_no_config: list[dict] = []
     for item in items:
         sku = item.get("sku", "")
         source_url = item.get("source_url", "")
         if not source_url:
+            dropped_no_url += 1
             continue
         cfg = None
+        # 1) SKU prefix 一致 (従来の無在庫 ebay**_ SKU)
         for prefix, c in configs_by_prefix.items():
-            if sku.startswith(prefix):
+            if prefix and sku.startswith(prefix):
                 cfg = c
                 break
-        if not cfg:
+        # 2) W183 fallback: prefix 不一致は source_url の url_keyword で site 解決
+        if cfg is None:
+            for c in configs_by_prefix.values():
+                kw = c.get("url_keyword", "")
+                if kw and kw in source_url:
+                    cfg = c
+                    break
+        if cfg is None:
+            dropped_no_config.append(
+                {"id": item.get("id"), "sku": sku, "url": source_url}
+            )
             continue
         batch.append({
             "id": item["id"],
@@ -362,4 +423,142 @@ def prepare_batch_items(items: list[dict], configs_by_prefix: dict) -> list[dict
             "sold_out": [cfg.get("sold_out_text", "")],
             "no_page": [cfg.get("no_page_text", "")],
         })
+    if dropped_no_url or dropped_no_config:
+        logger.info(
+            "[prepare_batch_items] 除外: no_source_url=%d site_config_missing_url=%d (対象 %d 件)",
+            dropped_no_url, len(dropped_no_config), len(items),
+        )
+        for d in dropped_no_config[:20]:
+            logger.warning(
+                "[prepare_batch_items] site_config_missing_url id=%s sku=%r url=%s",
+                d["id"], d["sku"], d["url"],
+            )
     return batch
+
+
+# ============================================================================
+# W182 (2026-05-28): 候補 URL の在庫 gate
+# ============================================================================
+# sold_out 商品を supplier_candidates に登録する bug の恒久対策。
+# task_supplier_candidate_search.py + task_supplier_sweep.py の発見ロジックから
+# 評価 / 登録の前に呼ぶ。raw HTML レベルで sold_out signal を確実に拾うため、
+# PayPay / Yahoo Auctions は専用 logic、他は既存 site_configs を流用。
+#
+# 設計根拠 (Codex 2026-05-28 調査):
+# - PayPay フリマは raw HTML に "InStock" (古い ld+json) と "SoldOut" が混在
+# - 既存 site_configs の `関連商品をアプリで探す` は JS 描画後にしか出ない
+# - raw HTML で確実に検出できる signal: 購入日時 (購入済の確定 signal)、SoldOut
+# 詳細: .company/engineering/migration/codex-supplier-bug-investigation.md
+# ============================================================================
+
+from datetime import datetime, timezone
+
+
+_AVAILABILITY_HTTPX_TIMEOUT = 10
+_AVAILABILITY_HEADERS_BASE = {
+    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
+    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
+}
+
+
+def _avail_headers() -> dict:
+    h = dict(_AVAILABILITY_HEADERS_BASE)
+    h["User-Agent"] = random.choice(USER_AGENTS)
+    return h
+
+
+def check_candidate_availability(url: str, timeout_sec: int = _AVAILABILITY_HTTPX_TIMEOUT) -> dict:
+    """
+    候補 URL の在庫状態を判定し、availability dict を返す。
+
+    Returns: {
+        'status':       'available' | 'unavailable' | 'not_found' | 'unknown',
+        'signal':       検出 signal (debug 用),
+        'checked_at':   ISO8601 UTC,
+    }
+
+    呼び出し側は `status in ('unavailable', 'not_found')` で reject する想定。
+    'unknown' は判定保留 (現状は通過 = 既存挙動と互換、後段の AI 評価でカバー)。
+    """
+    checked_at = datetime.now(timezone.utc).isoformat()
+    if not url:
+        return {'status': 'unknown', 'signal': 'empty url', 'checked_at': checked_at}
+    if 'paypayfleamarket.yahoo.co.jp' in url:
+        return _check_paypay_availability(url, timeout_sec, checked_at)
+    if 'auctions.yahoo.co.jp' in url:
+        return _check_yahoo_auctions_availability(url, timeout_sec, checked_at)
+    # mercari / fril / 他は既存 site_configs ベース
+    return _check_via_site_configs(url, timeout_sec, checked_at)
+
+
+def _check_paypay_availability(url: str, timeout_sec: int, checked_at: str) -> dict:
+    """PayPay フリマ raw HTML 判定 (W182、Codex 2026-05-28 検証ベース)。"""
+    try:
+        resp = httpx.get(url, headers=_avail_headers(), timeout=timeout_sec, follow_redirects=True)
+    except httpx.TimeoutException:
+        return {'status': 'unknown', 'signal': 'httpx timeout', 'checked_at': checked_at}
+    except httpx.HTTPError as e:
+        return {'status': 'unknown', 'signal': f'httpx error: {type(e).__name__}', 'checked_at': checked_at}
+    if resp.status_code == 404:
+        return {'status': 'not_found', 'signal': 'HTTP 404', 'checked_at': checked_at}
+    if resp.status_code != 200:
+        return {'status': 'unknown', 'signal': f'HTTP {resp.status_code}', 'checked_at': checked_at}
+    html = resp.text
+    if 'この商品は存在しません' in html:
+        return {'status': 'not_found', 'signal': 'no_page_text', 'checked_at': checked_at}
+    # sold_out signals (Codex 検証で raw HTML に必ず入る): 優先順
+    if '購入日時' in html:
+        return {'status': 'unavailable', 'signal': '購入日時 in HTML', 'checked_at': checked_at}
+    if '"SoldOut"' in html or "'SoldOut'" in html:
+        return {'status': 'unavailable', 'signal': 'SoldOut JSON-LD', 'checked_at': checked_at}
+    if '関連商品をアプリで探す' in html:
+        return {'status': 'unavailable', 'signal': 'related items text', 'checked_at': checked_at}
+    # in_stock 確認
+    if '購入手続きへ' in html:
+        return {'status': 'available', 'signal': '購入手続きへ', 'checked_at': checked_at}
+    return {'status': 'unknown', 'signal': 'no signal matched', 'checked_at': checked_at}
+
+
+def _check_yahoo_auctions_availability(url: str, timeout_sec: int, checked_at: str) -> dict:
+    """ヤフオク (auctions.yahoo.co.jp) raw HTML 判定 (W182)。"""
+    try:
+        resp = httpx.get(url, headers=_avail_headers(), timeout=timeout_sec, follow_redirects=True)
+    except httpx.TimeoutException:
+        return {'status': 'unknown', 'signal': 'httpx timeout', 'checked_at': checked_at}
+    except httpx.HTTPError as e:
+        return {'status': 'unknown', 'signal': f'httpx error: {type(e).__name__}', 'checked_at': checked_at}
+    if resp.status_code == 404:
+        return {'status': 'not_found', 'signal': 'HTTP 404', 'checked_at': checked_at}
+    if resp.status_code != 200:
+        return {'status': 'unknown', 'signal': f'HTTP {resp.status_code}', 'checked_at': checked_at}
+    html = resp.text
+    if 'このオークションは終了' in html or 'このオークションは存在しません' in html:
+        return {'status': 'not_found', 'signal': 'auction ended/missing', 'checked_at': checked_at}
+    if '入札する' in html or '今すぐ落札' in html:
+        return {'status': 'available', 'signal': 'bid available', 'checked_at': checked_at}
+    return {'status': 'unknown', 'signal': 'no signal matched', 'checked_at': checked_at}
+
+
+def _check_via_site_configs(url: str, timeout_sec: int, checked_at: str) -> dict:
+    """site_configs から URL に一致する site を引いて httpx 判定 (W182、mercari / fril / 他)。"""
+    try:
+        from monitor.database import get_conn
+        with get_conn() as conn:
+            rows = conn.execute(
+                "SELECT site_name, url_keyword, in_stock_text1, in_stock_text2, "
+                "       sold_out_text, no_page_text FROM site_configs"
+            ).fetchall()
+    except Exception as e:
+        return {'status': 'unknown', 'signal': f'site_configs read error: {type(e).__name__}', 'checked_at': checked_at}
+    for r in rows:
+        if r[1] and r[1] in url:
+            in_stock = [x for x in (r[2], r[3]) if x]
+            sold_out = [r[4]] if r[4] else []
+            no_page = [r[5]] if r[5] else []
+            status = _check_with_httpx(url, in_stock, sold_out, no_page)
+            return {
+                'status': status or 'unknown',
+                'signal': f'site_config: {r[0]}',
+                'checked_at': checked_at,
+            }
+    return {'status': 'unknown', 'signal': 'no matching site_config', 'checked_at': checked_at}
warning: in the working copy of 'tools/ebay-manager/monitor/scrapers.py', LF will be replaced by CRLF the next time Git touches it

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
diff --git a/tools/ebay-manager/tests/test_w183_ec_direct_url.py b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
index f85db190b7f4240442ebf85930d1ffba40c1ea00..2f4a9bb8234742cceaa07a0abcbfe82a4bf89c55
--- a/tools/ebay-manager/tests/test_w183_ec_direct_url.py
+++ b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
@@ -198,6 +198,34 @@
     assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
 
 
+def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():
+    from monitor.database import init_db
+    from monitor.scrapers import _check_with_httpx
+    init_db()
+    rk = _cfg_by_prefix()["ebayRT_"]
+    fake = MagicMock()
+    fake.status_code = 200
+    fake.text = """
+    <html>
+      <meta itemprop="availability" content="http://schema.org/OutOfStock">
+      <script>
+        {"itemInfoSku":{"purchaseCondition":"enabled",
+          "features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
+          "variantMappedInventories":[{"sku":"m20-5806","quantity":0}],
+          "newPurchaseSku":{"quantity":0}}}
+      </script>
+    </html>
+    """
+    with patch("monitor.scrapers.httpx.get", return_value=fake):
+        r = _check_with_httpx(
+            "https://item.rakuten.co.jp/tuzukiya/m20-5806/",
+            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
+            [rk.get("sold_out_text", "")],
+            [rk.get("no_page_text", "")],
+        )
+    assert r == "available"
+
+
 def test_w183_amazon_add_to_cart_detection():
     from monitor.database import init_db
     from monitor.scrapers import _detect_status_single

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "python -c \"from pathlib import Path; from unittest.mock import patch, MagicMock; from monitor.database import init_db, find_site_config_by_url; from monitor.scrapers import _check_with_httpx; url='https://item.rakuten.co.jp/tuzukiya/m20-5806/'; init_db(); cfg=find_site_config_by_url(url); fake=MagicMock(); fake.status_code=200; fake.text=Path('data/tmp/rakuten_357_raw.html').read_text(encoding='utf-8', errors='replace');
with patch('monitor.scrapers.httpx.get', return_value=fake): print(_check_with_httpx(url, [cfg.get('in_stock_text1',''), cfg.get('in_stock_text2','')], [cfg.get('sold_out_text','')], [cfg.get('no_page_text','')]))\"" in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 617ms:
available

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
diff --git a/tools/ebay-manager/tests/test_w183_ec_direct_url.py b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
index f85db190b7f4240442ebf85930d1ffba40c1ea00..2f4a9bb8234742cceaa07a0abcbfe82a4bf89c55
--- a/tools/ebay-manager/tests/test_w183_ec_direct_url.py
+++ b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
@@ -198,6 +198,34 @@
     assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
 
 
+def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():
+    from monitor.database import init_db
+    from monitor.scrapers import _check_with_httpx
+    init_db()
+    rk = _cfg_by_prefix()["ebayRT_"]
+    fake = MagicMock()
+    fake.status_code = 200
+    fake.text = """
+    <html>
+      <meta itemprop="availability" content="http://schema.org/OutOfStock">
+      <script>
+        {"itemInfoSku":{"purchaseCondition":"enabled",
+          "features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
+          "variantMappedInventories":[{"sku":"m20-5806","quantity":0}],
+          "newPurchaseSku":{"quantity":0}}}
+      </script>
+    </html>
+    """
+    with patch("monitor.scrapers.httpx.get", return_value=fake):
+        r = _check_with_httpx(
+            "https://item.rakuten.co.jp/tuzukiya/m20-5806/",
+            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
+            [rk.get("sold_out_text", "")],
+            [rk.get("no_page_text", "")],
+        )
+    assert r == "available"
+
+
 def test_w183_amazon_add_to_cart_detection():
     from monitor.database import init_db
     from monitor.scrapers import _detect_status_single

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command 'rg -n "def _detect_rakuten_purchase_status|rakuten_status|rakuten_sold_out_texts" monitor/scrapers.py' in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command 'rg -n "test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos" tests/test_w183_ec_direct_url.py' in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 293ms:
25:def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
79:        rakuten_status = _detect_rakuten_purchase_status(url, html)
80:        if rakuten_status is not None:
81:            return rakuten_status
83:            rakuten_sold_out_texts = [
87:            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)

 succeeded in 320ms:
201:def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():

diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
diff --git a/tools/ebay-manager/tests/test_w183_ec_direct_url.py b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
index f85db190b7f4240442ebf85930d1ffba40c1ea00..2f4a9bb8234742cceaa07a0abcbfe82a4bf89c55
--- a/tools/ebay-manager/tests/test_w183_ec_direct_url.py
+++ b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
@@ -198,6 +198,34 @@
     assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
 
 
+def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():
+    from monitor.database import init_db
+    from monitor.scrapers import _check_with_httpx
+    init_db()
+    rk = _cfg_by_prefix()["ebayRT_"]
+    fake = MagicMock()
+    fake.status_code = 200
+    fake.text = """
+    <html>
+      <meta itemprop="availability" content="http://schema.org/OutOfStock">
+      <script>
+        {"itemInfoSku":{"purchaseCondition":"enabled",
+          "features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
+          "variantMappedInventories":[{"sku":"m20-5806","quantity":0}],
+          "newPurchaseSku":{"quantity":0}}}
+      </script>
+    </html>
+    """
+    with patch("monitor.scrapers.httpx.get", return_value=fake):
+        r = _check_with_httpx(
+            "https://item.rakuten.co.jp/tuzukiya/m20-5806/",
+            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
+            [rk.get("sold_out_text", "")],
+            [rk.get("no_page_text", "")],
+        )
+    assert r == "available"
+
+
 def test_w183_amazon_add_to_cart_detection():
     from monitor.database import init_db
     from monitor.scrapers import _detect_status_single

codex
Implemented and verified.

Root cause: Rakuten hidden-stock pages can embed `schema.org/OutOfStock` and `quantity:0` even while purchasable. The generic `_check_with_httpx()` substring detector trusted that schema.org marker first, so it returned `unavailable`.

Fix in [monitor/scrapers.py](C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/scrapers.py:25):
- Added Rakuten-only purchase JSON detection for `item.rakuten` URLs.
- Rule:
  - `purchaseCondition:"enabled"` or `displayNormalCartButton:true` => `available`
  - if no available signal, present non-enabled `purchaseCondition` or `displayNormalCartButton:false` => `unavailable`
  - Rakuten schema.org `OutOfStock` alone is no longer allowed to produce definitive OOS.
- Other sites still use the existing generic detection path.

Regression test added in [tests/test_w183_ec_direct_url.py](C:/Users/gucch/projects/claude/tools/ebay-manager/tests/test_w183_ec_direct_url.py:201), mocking `_check_with_httpx()` with Rakuten HTML containing both `purchaseCondition:"enabled"` and schema.org `OutOfStock`.

Verification:
```text
python -m pytest tests/test_w183_ec_direct_url.py -q
..............                                                           [100%]
14 passed in 19.11s
```

Live URL verification through `_check_with_httpx()`:
```text
https://item.rakuten.co.jp/tuzukiya/m20-5806/ -> available
```

Saved raw HTML verification with `data/tmp/rakuten_357_raw.html` through the same mocked `_check_with_httpx()` path:
```text
available
```
diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 141958bd06bc21f96050d18c654358786fcf7405..a5b584c2d3d2bd2c36eb2f04d427ef6883ec9c8a
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -5,6 +5,7 @@
 import asyncio
 import logging
 import random
+import re
 from typing import Optional
 
 import httpx
@@ -21,6 +22,31 @@
 
 # ---- httpx ベースのチェック（高速・軽量） ----
 
+def _detect_rakuten_purchase_status(url: str, html: str) -> Optional[str]:
+    """Rakuten purchase JSON is authoritative when shops hide stock counts."""
+    if "item.rakuten" not in url.lower():
+        return None
+
+    purchase_conditions = re.findall(r'"purchaseCondition"\s*:\s*"([^"]+)"', html)
+    cart_buttons = re.findall(r'"displayNormalCartButton"\s*:\s*(true|false)', html, re.IGNORECASE)
+
+    if any(condition == "enabled" for condition in purchase_conditions):
+        logger.debug(f"Rakuten purchaseCondition enabled -> available: {url}")
+        return "available"
+    if any(button.lower() == "true" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button displayed -> available: {url}")
+        return "available"
+
+    if purchase_conditions:
+        logger.debug(f"Rakuten purchaseCondition not enabled -> unavailable: {url}")
+        return "unavailable"
+    if any(button.lower() == "false" for button in cart_buttons):
+        logger.debug(f"Rakuten normal cart button hidden -> unavailable: {url}")
+        return "unavailable"
+
+    return None
+
+
 def _check_with_httpx(
     url: str,
     in_stock_texts: list[str],
@@ -50,6 +76,15 @@
         if "robot check" in low or "validatecaptcha" in low:
             logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
             return None
+        rakuten_status = _detect_rakuten_purchase_status(url, html)
+        if rakuten_status is not None:
+            return rakuten_status
+        if "item.rakuten" in url.lower():
+            rakuten_sold_out_texts = [
+                t for t in sold_out_texts
+                if t != 'itemprop="availability" content="http://schema.org/OutOfStock"'
+            ]
+            return _detect_status_single(html, in_stock_texts, rakuten_sold_out_texts, no_page_texts, strict=True)
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
diff --git a/tools/ebay-manager/tests/test_w183_ec_direct_url.py b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
index f85db190b7f4240442ebf85930d1ffba40c1ea00..2f4a9bb8234742cceaa07a0abcbfe82a4bf89c55
--- a/tools/ebay-manager/tests/test_w183_ec_direct_url.py
+++ b/tools/ebay-manager/tests/test_w183_ec_direct_url.py
@@ -198,6 +198,34 @@
     assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
 
 
+def test_w183_rakuten_hidden_stock_purchase_json_overrides_schema_oos():
+    from monitor.database import init_db
+    from monitor.scrapers import _check_with_httpx
+    init_db()
+    rk = _cfg_by_prefix()["ebayRT_"]
+    fake = MagicMock()
+    fake.status_code = 200
+    fake.text = """
+    <html>
+      <meta itemprop="availability" content="http://schema.org/OutOfStock">
+      <script>
+        {"itemInfoSku":{"purchaseCondition":"enabled",
+          "features":{"displayNormalCartButton":true,"inventoryDisplay":"HIDDEN_STOCK"},
+          "variantMappedInventories":[{"sku":"m20-5806","quantity":0}],
+          "newPurchaseSku":{"quantity":0}}}
+      </script>
+    </html>
+    """
+    with patch("monitor.scrapers.httpx.get", return_value=fake):
+        r = _check_with_httpx(
+            "https://item.rakuten.co.jp/tuzukiya/m20-5806/",
+            [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")],
+            [rk.get("sold_out_text", "")],
+            [rk.get("no_page_text", "")],
+        )
+    assert r == "available"
+
+
 def test_w183_amazon_add_to_cart_detection():
     from monitor.database import init_db
     from monitor.scrapers import _detect_status_single

tokens used
71,036
Implemented and verified.

Root cause: Rakuten hidden-stock pages can embed `schema.org/OutOfStock` and `quantity:0` even while purchasable. The generic `_check_with_httpx()` substring detector trusted that schema.org marker first, so it returned `unavailable`.

Fix in [monitor/scrapers.py](C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/scrapers.py:25):
- Added Rakuten-only purchase JSON detection for `item.rakuten` URLs.
- Rule:
  - `purchaseCondition:"enabled"` or `displayNormalCartButton:true` => `available`
  - if no available signal, present non-enabled `purchaseCondition` or `displayNormalCartButton:false` => `unavailable`
  - Rakuten schema.org `OutOfStock` alone is no longer allowed to produce definitive OOS.
- Other sites still use the existing generic detection path.

Regression test added in [tests/test_w183_ec_direct_url.py](C:/Users/gucch/projects/claude/tools/ebay-manager/tests/test_w183_ec_direct_url.py:201), mocking `_check_with_httpx()` with Rakuten HTML containing both `purchaseCondition:"enabled"` and schema.org `OutOfStock`.

Verification:
```text
python -m pytest tests/test_w183_ec_direct_url.py -q
..............                                                           [100%]
14 passed in 19.11s
```

Live URL verification through `_check_with_httpx()`:
```text
https://item.rakuten.co.jp/tuzukiya/m20-5806/ -> available
```

Saved raw HTML verification with `data/tmp/rakuten_357_raw.html` through the same mocked `_check_with_httpx()` path:
```text
available
```
