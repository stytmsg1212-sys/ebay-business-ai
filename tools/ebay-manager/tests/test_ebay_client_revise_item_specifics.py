"""#44 G2 (2026-07-04): revise_item_specifics (ReviseItem ItemSpecifics 全置換).

eBay 実 API は叩かない (`_call_trading_api` を mock)。カバー範囲:
  - 禁止 Name (原産国系) の除外 (入力側 / 現行(merge)側 双方)
  - 全置換 (replace_all=True) と merge (replace_all=False) の挙動差
  - Brand 欠落 reject (禁止 Name 除外後の送信対象を判定)
  - 65 字超過値: 低レベル builder は ValueError / 上位関数は success:False に変換
  - read-back (GetItem) verify 不一致で success:False
"""
from __future__ import annotations

import pytest
from unittest.mock import patch


# ── C1 (2026-07-04): _build_get_item_xml が ItemSpecifics を確実に取得する ──

def test_build_get_item_xml_includes_item_specifics_flag():
    """Trading API GetItem で ItemSpecifics を返させるには
    <IncludeItemSpecifics>true</IncludeItemSpecifics> が必須。
    C1 fix 前は IncludeSelector (Shopping API 用・Trading では no-op) しか
    無く応答が空 → merge / read-back verify が silent 消去を招いていた。"""
    from monitor.ebay_client import _build_get_item_xml
    xml = _build_get_item_xml("ITEM_TEST_123")
    assert "<IncludeItemSpecifics>true</IncludeItemSpecifics>" in xml
    # request 全体で ItemID もそのまま入っている (最低限のサニティ)
    assert "<ItemID>ITEM_TEST_123</ItemID>" in xml
    # IncludeSelector は K2 で残置 (削除しない、他 caller の behavioral 影響ゼロ確保)
    assert "<IncludeSelector>Details,ItemSpecifics</IncludeSelector>" in xml
    # ItemSpecifics 以外の既存フィールドも回帰なし (WatchCount 等)
    assert "<IncludeWatchCount>true</IncludeWatchCount>" in xml


# ── 禁止 Name 判定 / フィルタ ──

def test_is_forbidden_specific_name_matches_all_known_variants():
    from monitor.ebay_client import _is_forbidden_specific_name
    for name in (
        "Country of Origin", "country of origin", "  Country of Origin  ",
        "Country/Region of Manufacture", "Country of Manufacture",
        "Manufacturer", "MANUFACTURER",
    ):
        assert _is_forbidden_specific_name(name) is True


def test_is_forbidden_specific_name_allows_normal_names():
    from monitor.ebay_client import _is_forbidden_specific_name
    for name in ("Brand", "Color", "MPN", "Model", "Manufacturer Warranty"):
        assert _is_forbidden_specific_name(name) is False


def test_filter_forbidden_specifics_removes_only_forbidden():
    from monitor.ebay_client import _filter_forbidden_specifics
    specifics = {
        "Brand": "Sony", "Country of Origin": "Japan",
        "Manufacturer": "SKT Corp", "Color": "Black",
    }
    filtered, removed = _filter_forbidden_specifics(specifics)
    assert filtered == {"Brand": "Sony", "Color": "Black"}
    assert sorted(removed) == ["Country of Origin", "Manufacturer"]


# ── XML builder (低レベル) ──

def test_build_item_specifics_nvl_xml_basic_structure():
    from monitor.ebay_client import _build_item_specifics_nvl_xml
    xml = _build_item_specifics_nvl_xml({"Brand": "Sony", "Color": "Black"})
    assert xml.count("<NameValueList>") == 2
    assert "<Name>Brand</Name>" in xml
    assert "<Value>Sony</Value>" in xml
    assert "<Name>Color</Name>" in xml


def test_build_item_specifics_nvl_xml_list_value_multiple_values():
    from monitor.ebay_client import _build_item_specifics_nvl_xml
    xml = _build_item_specifics_nvl_xml({"Features": ["Waterproof", "Bluetooth"]})
    assert xml.count("<Value>") == 2
    assert "<Value>Waterproof</Value>" in xml
    assert "<Value>Bluetooth</Value>" in xml


def test_build_item_specifics_nvl_xml_skips_empty_and_blank_values():
    from monitor.ebay_client import _build_item_specifics_nvl_xml
    xml = _build_item_specifics_nvl_xml({"Brand": "Sony", "Empty": "", "Blank": "   "})
    assert "<Name>Brand</Name>" in xml
    assert "Empty" not in xml
    assert "Blank" not in xml


def test_build_item_specifics_nvl_xml_over_65_chars_raises_value_error():
    from monitor.ebay_client import _build_item_specifics_nvl_xml
    with pytest.raises(ValueError):
        _build_item_specifics_nvl_xml({"Notes": "x" * 66})


def test_build_item_specifics_nvl_xml_exactly_65_chars_ok():
    from monitor.ebay_client import _build_item_specifics_nvl_xml
    xml = _build_item_specifics_nvl_xml({"Notes": "x" * 65})
    assert "<Value>" + "x" * 65 + "</Value>" in xml


# ── GetItem レスポンス parse (merge / read-back 用) ──

def test_parse_item_specifics_from_get_item_xml_single_and_multi_value():
    from monitor.ebay_client import _parse_item_specifics_from_get_item_xml
    xml = (
        '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<Item><ItemSpecifics>'
        '<NameValueList><Name>Brand</Name><Value>Sony</Value></NameValueList>'
        '<NameValueList><Name>Features</Name>'
        '<Value>Waterproof</Value><Value>Bluetooth</Value></NameValueList>'
        '</ItemSpecifics></Item></GetItemResponse>'
    )
    result = _parse_item_specifics_from_get_item_xml(xml)
    assert result["Brand"] == "Sony"
    assert sorted(result["Features"]) == ["Bluetooth", "Waterproof"]


def test_parse_item_specifics_from_get_item_xml_parse_error_returns_empty():
    from monitor.ebay_client import _parse_item_specifics_from_get_item_xml
    assert _parse_item_specifics_from_get_item_xml("not xml") == {}
    assert _parse_item_specifics_from_get_item_xml("") == {}


# ── revise_item_specifics: 入口 guard (API を呼ばない) ──

def test_revise_item_specifics_empty_item_id_no_api_call():
    import monitor.ebay_client as ec
    with patch.object(ec, "_call_trading_api") as m:
        res = ec.revise_item_specifics(
            "", {"Brand": "Sony"},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
        )
    m.assert_not_called()
    assert res["success"] is False
    assert res["removed_names"] == []


def test_revise_item_specifics_empty_specifics_no_api_call():
    import monitor.ebay_client as ec
    with patch.object(ec, "_call_trading_api") as m:
        res = ec.revise_item_specifics(
            "ITEM1", {},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
        )
    m.assert_not_called()
    assert res["success"] is False


def test_revise_item_specifics_brand_missing_after_filter_rejected_no_api_call():
    """禁止 Name 除外後に Brand が残らない場合、API を呼ばず reject."""
    import monitor.ebay_client as ec
    with patch.object(ec, "_call_trading_api") as m:
        res = ec.revise_item_specifics(
            "ITEM1", {"Manufacturer": "ACME", "Color": "Red"},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
        )
    m.assert_not_called()
    assert res["success"] is False
    assert "Brand" in res["message"]
    # 禁止 Name 除外は Brand チェック前に行われている
    assert "Manufacturer" in res["removed_names"]


# ── revise_item_specifics: replace_all=True (全置換) ──

def test_revise_item_specifics_replace_all_true_success_sends_input_only():
    """replace_all=True は現行取得 (merge 用 GetItem) を呼ばず、
    ReviseItem → read-back GetItem の 2 回のみ _call_trading_api を呼ぶ."""
    import monitor.ebay_client as ec
    revise_resp = {"success": True, "ack": "Success", "raw": "<x/>"}
    verify_xml = (
        '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<Item><ItemSpecifics>'
        '<NameValueList><Name>Brand</Name><Value>Sony</Value></NameValueList>'
        '<NameValueList><Name>Color</Name><Value>Black</Value></NameValueList>'
        '</ItemSpecifics></Item></GetItemResponse>'
    )
    verify_resp = {"success": True, "ack": "Success", "raw": verify_xml}
    with patch.object(ec, "_call_trading_api",
                       side_effect=[revise_resp, verify_resp]) as m:
        res = ec.revise_item_specifics(
            "ITEM1", {"Brand": "Sony", "Color": "Black"},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
            replace_all=True,
        )
    assert m.call_count == 2
    assert m.call_args_list[0][0][0] == "ReviseItem"
    assert m.call_args_list[1][0][0] == "GetItem"
    assert res["success"] is True
    assert res["sent_specifics"] == {"Brand": "Sony", "Color": "Black"}
    assert res["removed_names"] == []


def test_revise_item_specifics_replace_all_true_forbidden_name_removed_from_input():
    import monitor.ebay_client as ec
    revise_resp = {"success": True, "ack": "Success", "raw": "<x/>"}
    verify_xml = (
        '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<Item><ItemSpecifics>'
        '<NameValueList><Name>Brand</Name><Value>Sony</Value></NameValueList>'
        '</ItemSpecifics></Item></GetItemResponse>'
    )
    verify_resp = {"success": True, "ack": "Success", "raw": verify_xml}
    with patch.object(ec, "_call_trading_api",
                       side_effect=[revise_resp, verify_resp]) as m:
        res = ec.revise_item_specifics(
            "ITEM1", {"Brand": "Sony", "Country of Origin": "Japan"},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
            replace_all=True,
        )
    # ReviseItem に送った XML に Country of Origin が含まれていないこと
    sent_xml = m.call_args_list[0][0][1]
    assert "Country of Origin" not in sent_xml
    assert res["success"] is True
    assert res["removed_names"] == ["Country of Origin"]
    assert "Country of Origin" not in res["sent_specifics"]


# ── revise_item_specifics: replace_all=False (merge) ──

def test_revise_item_specifics_replace_all_false_merges_with_current_and_filters_forbidden():
    """現行 (GetItem merge) に禁止 Name (Manufacturer) が含まれていても
    最終送信からは除外される."""
    import monitor.ebay_client as ec
    current_xml = (
        '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<Item><ItemSpecifics>'
        '<NameValueList><Name>Brand</Name><Value>Sony</Value></NameValueList>'
        '<NameValueList><Name>Manufacturer</Name><Value>ACME</Value></NameValueList>'
        '<NameValueList><Name>Model</Name><Value>XYZ</Value></NameValueList>'
        '</ItemSpecifics></Item></GetItemResponse>'
    )
    merge_resp = {"success": True, "ack": "Success", "raw": current_xml}
    revise_resp = {"success": True, "ack": "Success", "raw": "<x/>"}
    verify_xml = (
        '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<Item><ItemSpecifics>'
        '<NameValueList><Name>Brand</Name><Value>Sony</Value></NameValueList>'
        '<NameValueList><Name>Model</Name><Value>XYZ</Value></NameValueList>'
        '<NameValueList><Name>Color</Name><Value>Black</Value></NameValueList>'
        '</ItemSpecifics></Item></GetItemResponse>'
    )
    verify_resp = {"success": True, "ack": "Success", "raw": verify_xml}
    with patch.object(
        ec, "_call_trading_api",
        side_effect=[merge_resp, revise_resp, verify_resp],
    ) as m:
        res = ec.revise_item_specifics(
            "ITEM1", {"Color": "Black"},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
            replace_all=False,
        )
    assert m.call_count == 3
    assert m.call_args_list[0][0][0] == "GetItem"    # merge 取得
    assert m.call_args_list[1][0][0] == "ReviseItem"
    assert m.call_args_list[2][0][0] == "GetItem"    # read-back verify
    assert res["success"] is True
    assert res["sent_specifics"] == {"Brand": "Sony", "Model": "XYZ", "Color": "Black"}
    assert res["removed_names"] == ["Manufacturer"]


def test_revise_item_specifics_replace_all_false_merge_getitem_fails_no_revise_call():
    import monitor.ebay_client as ec
    with patch.object(
        ec, "_call_trading_api",
        return_value={"success": False, "message": "通信エラー", "raw": None},
    ) as m:
        res = ec.revise_item_specifics(
            "ITEM1", {"Color": "Black"},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
            replace_all=False,
        )
    assert m.call_count == 1  # merge 用 GetItem のみ、ReviseItem は呼ばれない
    assert res["success"] is False
    assert res["removed_names"] == []


# ── Ack=Warning に SeverityCode=Error 混入 ──

def test_revise_item_specifics_warning_with_fatal_error_downgraded():
    import monitor.ebay_client as ec
    raw = (
        '<ReviseItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<Ack>Warning</Ack>'
        '<Errors><SeverityCode>Error</SeverityCode>'
        '<ErrorCode>21916918</ErrorCode>'
        '<LongMessage>Invalid item specific</LongMessage></Errors>'
        '</ReviseItemResponse>'
    )
    revise_resp = {"success": True, "ack": "Warning", "raw": raw}
    with patch.object(ec, "_call_trading_api", return_value=revise_resp) as m:
        res = ec.revise_item_specifics(
            "ITEM1", {"Brand": "Sony"},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
        )
    # ReviseItem 1回のみで打ち切り (read-back verify まで進まない)
    assert m.call_count == 1
    assert res["success"] is False
    assert "21916918" in res["message"]


# ── read-back verify 不一致 ──

def test_revise_item_specifics_read_back_mismatch_success_false():
    import monitor.ebay_client as ec
    revise_resp = {"success": True, "ack": "Success", "raw": "<x/>"}
    # eBay 側に反映された値が送信値と異なる (verify 不一致)
    verify_xml = (
        '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<Item><ItemSpecifics>'
        '<NameValueList><Name>Brand</Name><Value>DIFFERENT</Value></NameValueList>'
        '</ItemSpecifics></Item></GetItemResponse>'
    )
    verify_resp = {"success": True, "ack": "Success", "raw": verify_xml}
    with patch.object(ec, "_call_trading_api",
                       side_effect=[revise_resp, verify_resp]):
        res = ec.revise_item_specifics(
            "ITEM1", {"Brand": "Sony"},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
        )
    assert res["success"] is False
    assert "Brand" in res["message"]


def test_revise_item_specifics_verify_getitem_communication_failure():
    import monitor.ebay_client as ec
    revise_resp = {"success": True, "ack": "Success", "raw": "<x/>"}
    fail_resp = {"success": False, "message": "通信エラー", "raw": None}
    with patch.object(ec, "_call_trading_api",
                       side_effect=[revise_resp, fail_resp]):
        res = ec.revise_item_specifics(
            "ITEM1", {"Brand": "Sony"},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
        )
    assert res["success"] is False
    assert "verify" in res["message"]


# ── 65 字超過は上位関数で例外を伝播させず success:False に変換 ──

def test_revise_item_specifics_over_65_chars_graceful_failure_not_raised():
    import monitor.ebay_client as ec
    with patch.object(ec, "_call_trading_api") as m:
        res = ec.revise_item_specifics(
            "ITEM1", {"Brand": "Sony", "Notes": "x" * 66},
            app_id="A", dev_id="D", cert_id="C", user_token="v^T",
        )
    m.assert_not_called()  # XML builder が ValueError → API 未到達
    assert res["success"] is False
    assert "65" in res["message"] or "字" in res["message"]
