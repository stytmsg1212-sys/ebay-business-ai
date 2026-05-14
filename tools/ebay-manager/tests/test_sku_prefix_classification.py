"""SKU prefix classification (有在庫 / 無在庫 判定) 回帰テスト

2026-04-30 SKU 一意性誤推論事故の再発防止。

詳細:
- .claude/rules/sku-rules.md (横断 rule)
- feedback_sku_misuse_repeat_offense.md (事故記録)
- tools/ebay-manager/CLAUDE.md SKU 規約セクション

ルール:
- SKU 用途は「有/無在庫判定」「無在庫の URL 変換」の 2 つだけ
- listing 一意キーには絶対使うな (`ebay_item_id` を使用)
"""
import pytest


def is_in_stock(sku: str) -> bool:
    """SKU が有在庫商品 (`stock**` で始まる) か判定。

    user 公認の判定ロジック (2026-04-30):
    `stock` で始まる → 有在庫、`ebay` で始まる → 無在庫。
    """
    return sku.startswith("stock")


def is_supplier_sourced(sku: str) -> bool:
    """SKU が無在庫商品 (`ebay**_*****`) か判定。"""
    return sku.startswith("ebay")


@pytest.mark.parametrize("sku", [
    "stock:01",
    "stock1",
    "stock",
    "stock01",
    "stock: 1",
    "stock: 01",
    "stock:128",
])
def test_in_stock_prefix_variants(sku: str) -> None:
    """有在庫 prefix の表記揺れを許容する (prefix 判定のみで正規化不要)"""
    assert is_in_stock(sku) is True
    assert is_supplier_sourced(sku) is False


@pytest.mark.parametrize("sku", [
    "ebayyh_p1221413657",
    "ebayme_m32400850054",
    "ebayPF_z587339852",
    "ebayrm_f8da0",
    "ebayh_xyz",
    "ebayMS_abc",
    "ebayRT_123",
    "ebayRB_456",
    "ebayYS_789",
    "ebayAM_dp1",
    "ebayBS_5850",
    "ebayFA_Hphvn",
])
def test_supplier_sourced_prefix(sku: str) -> None:
    """無在庫 prefix を判定 (大文字小文字の中間文字を含む全パターン)"""
    assert is_supplier_sourced(sku) is True
    assert is_in_stock(sku) is False


@pytest.mark.parametrize("sku", ["", "STOCK01", "Stock:01", "STOCK", "EBAYme_xxx"])
def test_case_sensitivity_documented(sku: str) -> None:
    """大文字 STOCK / EBAY は現状判定対象外 (仕様明示)。

    将来表記揺れが拡大したらルール変更必要。今は prefix 完全一致のみ。
    """
    assert is_in_stock(sku) is False
    assert is_supplier_sourced(sku) is False


def test_disjoint_classification() -> None:
    """有在庫 / 無在庫 は排他 (同一 SKU が両方該当しない)"""
    samples = [
        "stock:01",
        "ebayyh_p1221413657",
        "garbage",
        "",
        "stockabc",
        "ebayBS_1318",
    ]
    for s in samples:
        assert not (is_in_stock(s) and is_supplier_sourced(s)), (
            f"重複分類: {s} が有在庫 AND 無在庫 と判定された"
        )


def test_unclassified_returns_false_for_both() -> None:
    """有在庫でも無在庫でもない SKU は両方 False"""
    unclassified = ["", "garbage", "ABCDEF", "123456"]
    for s in unclassified:
        assert is_in_stock(s) is False
        assert is_supplier_sourced(s) is False
