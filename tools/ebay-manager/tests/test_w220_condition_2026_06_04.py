"""W220 slice3 condition/description eBay 反映の unit test (2026-06-04).

eBay API (ReviseFixedPriceItem/ReviseItem) は叩かず、XML 組立・rank マップ・
空ガードのみ検証 (実 API 経路は実機 Q1 で別途確認済)。account-direct な
ConditionID 反映の regression 保護。
"""
from monitor.ebay_client import (
    _build_revise_item_condition_xml,
    revise_item_condition,
)


def test_rank_to_condition_id_map():
    """CLAUDE.md 8段階 → eBay ConditionID の対応を固定."""
    from tabs.tab_product_management import _RANK_TO_CONDITION_ID as M
    assert M == {
        "N": "1000", "S": "1500",
        "A": "3000", "B": "3000", "C": "3000", "D": "3000", "PO": "3000",
        "As-Is": "7000",
    }


def test_condition_xml_id_and_optional_description():
    x = _build_revise_item_condition_xml("123456789012", "7000", "As-Is reason")
    assert "ReviseFixedPriceItemRequest" in x
    assert "<ConditionID>7000</ConditionID>" in x
    assert "<ConditionDescription>As-Is reason</ConditionDescription>" in x
    # ConditionDescription 未指定なら送らない (既存 CD を eBay 側で維持)
    x2 = _build_revise_item_condition_xml("123", "3000")
    assert "<ConditionID>3000</ConditionID>" in x2
    assert "ConditionDescription" not in x2


def test_condition_xml_escapes_special_chars():
    x = _build_revise_item_condition_xml("1", "3000", "a<b>&c")
    assert "&lt;b&gt;" in x and "&amp;c" in x


def test_revise_condition_rejects_empty_id():
    """ConditionID 空は API を叩かず success=False (Q0: silent 反映しない)."""
    r = revise_item_condition("1", "", "app", "dev", "cert", "tok")
    assert r["success"] is False and "empty" in r["message"]


def test_asis_condition_xml_includes_description():
    """As-Is(7000) revise XML に ConditionDescription が含まれる (Defect 防止)."""
    x = _build_revise_item_condition_xml("123", "7000", "As-Is — no AC adapter")
    assert "<ConditionID>7000</ConditionID>" in x
    assert "<ConditionDescription>As-Is — no AC adapter</ConditionDescription>" in x


def test_apply_content_asis_without_cd_blocks(monkeypatch):
    """As-Is で Condition 理由(CD)欠落時は revise を打たず success/cond_ok=False
    (HIGH-1 / Q0: silent push 禁止、Defect 防止)."""
    import monitor.ebay_client as ec
    import tabs.tab_product_management as tpm

    monkeypatch.setattr(
        tpm, "get_ebay_credentials",
        lambda c=None: {"app_id": "a", "dev_id": "d",
                        "cert_id": "c", "user_token": "t"},
    )
    called = {"cond": False}

    def _spy(*a, **k):
        called["cond"] = True
        return {"success": True, "condition_id": "7000"}

    monkeypatch.setattr(ec, "revise_item_condition", _spy)

    editing = {"rank": "As-Is", "condition_description": "",
               "listing_description": None}
    res = tpm._apply_listing_content_to_ebay("123", editing, {})
    assert res["changed"] is True
    assert res["success"] is False
    assert res["cond_ok"] is False
    assert called["cond"] is False  # As-Is + CD 欠落 → revise を打たない
