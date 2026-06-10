"""PayPayフリマ Next.js ビルド資産を商品画像として誤取得する不具合の回帰テスト (2026-06-08).

出典: 個別出品で PayPayフリマ商品 (z606464462 GRAPHTEC GL840-SDM) を出品しようと
したら、画像取得が商品写真でなく `/_next/static/media/banner_down...` (サイトの
バナー資産) を拾った。原因は `_is_branding_image` の除外パターンに `/_next/` が
無く、`_banner_` (前後アンダースコア) が `banner_down` にマッチしなかったため。

修正: `_BRANDING_URL_PATTERNS` に `/_next/` を追加 (Next.js ビルド資産は商品画像
ではない)。商品画像 (auctions.c.yimg.jp/images...) は除外されないことも固定する。
"""
from monitor.supplier_scraper import _is_branding_image, _dedupe_ordered


# 実際に z606464462 で誤取得された / 正取得すべき URL
_BANNER = "https://paypayfleamarket.c.yimg.jp/assets/1.819.2/_next/static/media/banner_down1f.abcd.png"
_NEXT_PHONE = "https://paypayfleamarket.c.yimg.jp/assets/1.819.2/_next/static/media/image_phone_pc.0hjjdv3v.png"
_NEXT_ICON = "https://paypayfleamarket.c.yimg.jp/assets/1.819.2/_next/static/media/icon_goldBadge.0mhksyo6h1g08.svg"
_PRODUCT = "https://auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0205/users/abc/i-img1200x900-1778283550450lr6m6j.jpg"


def test_next_banner_asset_is_branding():
    """/_next/static/media/banner_down... は除外される (本不具合の核心)。"""
    assert _is_branding_image(_BANNER) is True


def test_next_static_media_assets_excluded():
    """_next 配下の他資産 (phone 画像 / アイコン svg) も除外される。"""
    assert _is_branding_image(_NEXT_PHONE) is True
    # svg は別経路でも除外されるが、_next でも確実に弾く
    assert _is_branding_image(_NEXT_ICON) is True


def test_real_product_image_kept():
    """正しい商品画像 (auctions.c.yimg.jp/images...) は除外されない。"""
    assert _is_branding_image(_PRODUCT) is False


def test_filter_keeps_only_product_image():
    """混在リストから商品画像だけが残る (順序保持)。"""
    urls = [_BANNER, _PRODUCT, _NEXT_PHONE, _NEXT_ICON]
    out = _dedupe_ordered(urls)
    assert out == [_PRODUCT]
