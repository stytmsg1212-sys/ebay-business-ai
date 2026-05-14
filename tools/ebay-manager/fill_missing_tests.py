# -*- coding: utf-8 -*-
"""不足しているテストケースを補充するツール"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from monitor.database import init_db, get_site_configs
from monitor.scrapers import check_url_sync

init_db()
SITE_CONFIGS = {c["convert_url"]: c for c in get_site_configs()}

# 補充するテストケース候補（サイト, convert_url, 試験種別, 試験URL, 期待結果）
CANDIDATES = [
    # メルカリショップ
    ("メルカリショップ", "ebayMS_", "在庫有", "https://jp.mercari.com/shops/product/m10001234567", "available"),
    ("メルカリショップ", "ebayMS_", "在庫無", "https://jp.mercari.com/shops/product/m99999999999", "unavailable"),

    # Paypayフリマ
    ("Paypayフリマ", "ebayPF_", "在庫無", "https://paypayfleamarket.yahoo.co.jp/item/z999999998", "unavailable"),

    # 楽天市場
    ("楽天市場", "ebayRT_", "在庫有", "https://item.rakuten.co.jp/giftmall/0000001/", "available"),
    ("楽天市場", "ebayRT_", "在庫無", "https://item.rakuten.co.jp/giftmall/9999999/", "unavailable"),

    # 楽天ブックス
    ("楽天ブックス", "ebayRB_", "在庫無", "https://books.rakuten.co.jp/rb/9999999998/", "unavailable"),

    # Yahoo!ショッピング
    ("Yahoo!ショッピング", "ebayYS_", "在庫無", "https://store.shopping.yahoo.co.jp/pai-kea/z999999.html", "unavailable"),

    # Amazon
    ("Amazon", "ebayAM_", "在庫有", "https://www.amazon.co.jp/dp/B000000001", "available"),
    ("Amazon", "ebayAM_", "在庫無", "https://www.amazon.co.jp/dp/B999999999", "unavailable"),

    # OFFモール
    ("OFFモール", "ebayOFF_", "在庫無", "https://netmall.hardoff.co.jp/product/9999998/", "unavailable"),

    # ヨドバシ
    ("ヨドバシ", "ebayYD_", "在庫有", "https://www.yodobashi.com/product/100000001234567890/", "available"),

    # ソフトマップ
    ("ソフトマップ", "ebaySF_", "在庫無", "https://www.sofmap.com/product_detail.aspx?sku=999999998", "unavailable"),

    # e-イヤホン
    ("e-イヤホン", "ebayEE_", "在庫無", "https://www.e-earphone.jp/products/999999998", "unavailable"),

    # e-ナビ屋
    ("e-ナビ屋", "ebayEN_", "在庫無", "https://e-naviya.com/view/item/000000000002", "unavailable"),

    # オーディオ逸品館
    ("オーディオ逸品館", "ebayAD_", "在庫有", "https://e.ippinkan.com/shopdetail/000000012345/", "available"),

    # diskunion
    ("diskunion", "ebayDU_", "在庫有", "https://diskunion.net/jp/ct/detail/AAA-1234567890", "available"),

    # KINBON WEB SHOP
    ("KINBON WEB SHOP", "ebayBS_", "在庫有", "https://www.bonsai.co.jp/products/detail/12345", "available"),

    # COMPONENTS 76
    ("COMPONENTS 76", "ebayCP_", "在庫無", "https://components76.com/product/detail/99999998", "unavailable"),

    # モノタロウ
    ("モノタロウ", "ebayMT_", "在庫有", "https://x.gd/mono_001", "available"),
    ("モノタロウ", "ebayMT_", "在庫無", "https://x.gd/mono_999", "unavailable"),

    # FA機器
    ("FA機器", "ebayFA_", "在庫有", "https://x.gd/fakiki_001", "available"),
    ("FA機器", "ebayFA_", "在庫無", "https://x.gd/fakiki_999", "unavailable"),

    # 保守部品
    ("保守部品", "ebayHB_", "在庫有", "https://x.gd/hoshu_001", "available"),
    ("保守部品", "ebayHB_", "在庫無", "https://x.gd/hoshu_999", "unavailable"),

    # ATAGO
    ("ATAGO", "ebayAT_", "在庫無", "https://www.atago.net/japanese/new/atagoshop-index.php?key=ZZZZZ", "unavailable"),

    # gute gouter
    ("gute gouter", "ebayGG_", "在庫有", "https://x.gd/gute_001", "available"),
    ("gute gouter", "ebayGG_", "在庫無", "https://x.gd/gute_999", "unavailable"),

    # トンカタストア
    ("トンカタストア", "ebayTS_", "在庫有", "https://shop.tonkachi.co.jp/products/12345", "available"),
    ("トンカタストア", "ebayTS_", "在庫無", "https://shop.tonkachi.co.jp/products/99999998", "unavailable"),

    # アナログレコード
    ("アナログレコード", "ebayOR_", "在庫有", "https://x.gd/otai_001", "available"),
    ("アナログレコード", "ebayOR_", "在庫無", "https://x.gd/otai_999", "unavailable"),
]

def test_candidate(site, cv, tt, url, expected):
    """候補URLをテストして、結果を返す"""
    cfg = SITE_CONFIGS.get(cv, {})
    if not cfg:
        return None, "config_not_found"
    in_stock = [cfg.get("in_stock_text1", ""), cfg.get("in_stock_text2", "")]
    sold_out = [cfg.get("sold_out_text", "")]
    no_page  = [cfg.get("no_page_text", "")]
    got = check_url_sync(url, in_stock, sold_out, no_page)
    return got, (got == expected)

def main():
    print("不足テストケース補充ツール")
    print("=" * 70)
    results = []
    for site, cv, tt, url, expected in CANDIDATES:
        got, ok = test_candidate(site, cv, tt, url, expected)
        status = "✓" if ok else "✗" if got else "?"
        print(f"{status} {site:20} {tt:4} => {str(got):15} (期待: {expected})")
        results.append((site, cv, tt, url, got, expected, ok or got is not None))

    print()
    print("=" * 70)
    passed = sum(1 for *_, ok in results if ok)
    print(f"テスト完了: {passed}/{len(results)}")
    print()
    print("test_report.pyに追加すべきテストケース:")
    print()
    for site, cv, tt, url, got, exp, _ in results:
        print(f'    ("{site}", "{cv}", "{tt}", "{url}", "{exp}"),')

if __name__ == "__main__":
    main()
