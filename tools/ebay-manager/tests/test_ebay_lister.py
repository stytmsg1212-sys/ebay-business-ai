#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ebay_lister の単体テスト (W9 Phase 4)

カバー範囲:
  - _build_add_fixed_price_item_xml: 必須フィールド / CDATA / ScheduleTime /
    ItemSpecifics 繰返 / PictureURL 24枚上限 / Verify vs Add ルート切替
  - _parse_add_item_response: Success / Warning / Failure 各レスポンス
  - verify_add_fixed_price_item / add_fixed_price_item_draft: _call_trading_api
    を mock.patch で置換し、実 eBay API 呼出しゼロで挙動検証
  - ScheduleTime が now + 21日 ± 1秒 の範囲内
  - 認証情報欠損時のエラー return

実 API は絶対に叩かない。全ての `_call_trading_api` を mock で置換する。
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.ebay_lister import (  # noqa: E402
    _MAX_PICTURES,
    _build_add_fixed_price_item_xml,
    _build_item_specifics_xml,
    _build_pictures_xml,
    _build_schedule_time,
    _parse_add_item_response,
    _wrap_cdata,
    add_fixed_price_item_draft,
    build_draft_params_from_phase3,
    verify_add_fixed_price_item,
)


# =========================================================================
# fixture
# =========================================================================

def _minimal_params(**overrides) -> dict:
    """最小限の draft_params (テスト用)。"""
    base = {
        'sku': 'W9-TEST-001',
        'ebay_title': 'Sony WH-1000XM5 Wireless Headphones Black',
        'ebay_description': '<div class="mh-wrap"><h1>Sony WH-1000XM5</h1><style>.x{color:red;}</style></div>',
        'ebay_category_id': '112529',
        'ebay_condition_id': '3000',
        'rank_code': 'A',
        'item_specifics': {
            'Brand': 'Sony',
            'Model': 'WH-1000XM5',
            'Type': 'Over-Ear',
            'Color': 'Black',
        },
        'listing_price_usd': 249.99,
        'image_urls': ['https://example.com/1.jpg', 'https://example.com/2.jpg'],
        'payment_policy_id': '359244671023',
        'return_policy_id': '359243687023',
        'shipping_policy_id': '377279091023',
        'country': 'JP',
        'currency': 'USD',
        'location': 'Tokyo, Japan',
        'postal_code': '100-0001',
        'dispatch_time_max': 3,
        'listing_duration': 'GTC',
        'scheduled_days_offset': 21,
    }
    base.update(overrides)
    return base


# Success レスポンス (ItemID 返却あり、Fees 付き、Warning 1件)
_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AddFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Timestamp>2026-04-20T00:00:00.000Z</Timestamp>
  <Ack>Success</Ack>
  <Version>1371</Version>
  <Build>E1371_CORE_API</Build>
  <ItemID>358463512773</ItemID>
  <StartTime>2026-05-11T13:00:00.000Z</StartTime>
  <Fees>
    <Fee>
      <Name>InsertionFee</Name>
      <Fee currencyID="USD">0.0</Fee>
    </Fee>
    <Fee>
      <Name>ListingFee</Name>
      <Fee currencyID="USD">0.35</Fee>
    </Fee>
  </Fees>
</AddFixedPriceItemResponse>"""

# Warning レスポンス (出品は成立、Warnings あり)
_WARNING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AddFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Warning</Ack>
  <ItemID>358463512774</ItemID>
  <Errors>
    <ShortMessage>Image warning</ShortMessage>
    <LongMessage>One or more of your images failed to upload.</LongMessage>
    <ErrorCode>37</ErrorCode>
    <SeverityCode>Warning</SeverityCode>
  </Errors>
  <Fees>
    <Fee>
      <Name>InsertionFee</Name>
      <Fee currencyID="USD">0.0</Fee>
    </Fee>
  </Fees>
</AddFixedPriceItemResponse>"""

# Failure レスポンス (出品失敗、Errors あり)
_FAILURE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AddFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors>
    <ShortMessage>Invalid category</ShortMessage>
    <LongMessage>The category ID you supplied is invalid.</LongMessage>
    <ErrorCode>87</ErrorCode>
    <SeverityCode>Error</SeverityCode>
  </Errors>
  <Errors>
    <ShortMessage>Missing shipping profile</ShortMessage>
    <LongMessage>Shipping profile is required.</LongMessage>
    <ErrorCode>88</ErrorCode>
    <SeverityCode>Error</SeverityCode>
  </Errors>
</AddFixedPriceItemResponse>"""


# =========================================================================
# _build_schedule_time
# =========================================================================

class TestBuildScheduleTime:
    def test_default_21_days(self):
        before = datetime.now(timezone.utc)
        s = _build_schedule_time()
        after = datetime.now(timezone.utc)
        # ISO 8601 with .000Z
        assert re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$', s)
        dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.000Z').replace(tzinfo=timezone.utc)
        # now + 21日 ± 1秒 範囲内
        assert (before + timedelta(days=21) - timedelta(seconds=1)) <= dt <= (after + timedelta(days=21) + timedelta(seconds=1))

    def test_custom_days(self):
        s = _build_schedule_time(days_offset=7)
        dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.000Z').replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert (now + timedelta(days=7) - timedelta(seconds=2)) <= dt <= (now + timedelta(days=7) + timedelta(seconds=2))

    def test_invalid_days_falls_back_to_21(self):
        s1 = _build_schedule_time(days_offset=0)
        s2 = _build_schedule_time(days_offset=-5)
        s3 = _build_schedule_time(days_offset=None)  # type: ignore[arg-type]
        now = datetime.now(timezone.utc)
        for s in (s1, s2, s3):
            dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.000Z').replace(tzinfo=timezone.utc)
            assert abs((dt - (now + timedelta(days=21))).total_seconds()) < 3

    def test_above_21_clipped(self):
        # eBay 仕様では ScheduleTime は最大 21日。30 指定しても 21 にクリップ
        s = _build_schedule_time(days_offset=30)
        dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.000Z').replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert abs((dt - (now + timedelta(days=21))).total_seconds()) < 3


# =========================================================================
# _wrap_cdata
# =========================================================================

class TestWrapCdata:
    def test_basic(self):
        assert _wrap_cdata('<div>hello</div>') == '<![CDATA[<div>hello</div>]]>'

    def test_empty(self):
        assert _wrap_cdata('') == '<![CDATA[]]>'

    def test_none(self):
        assert _wrap_cdata(None) == '<![CDATA[]]>'  # type: ignore[arg-type]

    def test_cdata_terminator_escape(self):
        # HTML 内に `]]>` が出現した場合は分割回避
        html = 'foo ]]> bar'
        wrapped = _wrap_cdata(html)
        # `]]>` がそのまま連続では出現しないこと
        assert ']]]]><![CDATA[>' in wrapped
        # Sanity: 2つの CDATA セクションに分かれている
        assert wrapped.count('<![CDATA[') == 2
        assert wrapped.count(']]>') == 2


# =========================================================================
# _build_item_specifics_xml
# =========================================================================

class TestBuildItemSpecificsXml:
    def test_basic(self):
        xml = _build_item_specifics_xml({'Brand': 'Sony', 'Model': 'WH-1000XM5'})
        assert '<ItemSpecifics>' in xml
        assert '<Name>Brand</Name>' in xml
        assert '<Value>Sony</Value>' in xml
        assert '<Name>Model</Name>' in xml
        assert '<Value>WH-1000XM5</Value>' in xml

    def test_empty(self):
        assert _build_item_specifics_xml({}) == ''
        assert _build_item_specifics_xml(None) == ''  # type: ignore[arg-type]

    def test_list_value_multi(self):
        xml = _build_item_specifics_xml({'Features': ['Wireless', 'Noise Cancelling']})
        assert xml.count('<Value>Wireless</Value>') == 1
        assert xml.count('<Value>Noise Cancelling</Value>') == 1
        assert xml.count('<Name>Features</Name>') == 1

    def test_escape(self):
        xml = _build_item_specifics_xml({'Desc': 'A & B <test>'})
        assert 'A &amp; B &lt;test&gt;' in xml

    def test_skip_empty_values(self):
        xml = _build_item_specifics_xml({'Brand': 'Sony', 'Empty': '', 'None': None})
        assert '<Name>Brand</Name>' in xml
        assert '<Name>Empty</Name>' not in xml
        assert '<Name>None</Name>' not in xml

    # -----------------------------------------------------------------
    # Regression tests from user-reported bugs (2026-04-22)
    # ユーザー実例: VerifyAdd が以下2件で失敗
    #   1. "Seller Notes's value of '...' is too long. Max 65 chars"
    #   2. "Connectivity is missing" (Claude が "Unknown" を返すと reject)
    # -----------------------------------------------------------------
    def test_seller_notes_truncated_to_65_chars(self):
        """Seller Notes 78字 → 65字に truncate (ユーザー報告バグ)"""
        long_value = (
            "Fully serviced; tested working with multiple discs. "
            "Minor light wear on chassis."
        )  # 79 chars
        assert len(long_value) > 65
        xml = _build_item_specifics_xml({'Seller Notes': long_value})
        # 65 字以内の値のみ XML に出る
        import re
        m = re.search(r'<Value>([^<]*)</Value>', xml)
        assert m is not None
        assert len(m.group(1)) <= 65

    def test_unknown_placeholder_value_excluded(self):
        """"Unknown" 等の placeholder は item_specifics から除外 (ユーザー報告バグ)"""
        xml = _build_item_specifics_xml({
            'Brand': 'Pioneer',
            'Connectivity': 'Unknown',   # placeholder → 除外
            'Model': 'LD-S9',
            'Country/Region of Manufacture': 'N/A',  # placeholder → 除外
            'Color': 'Black',
        })
        assert '<Name>Brand</Name>' in xml
        assert '<Name>Model</Name>' in xml
        assert '<Name>Color</Name>' in xml
        # Unknown / N/A フィールド自体が出ていないこと
        assert '<Name>Connectivity</Name>' not in xml
        assert '<Name>Country/Region of Manufacture</Name>' not in xml
        assert '<Value>Unknown</Value>' not in xml
        assert '<Value>N/A</Value>' not in xml

    def test_placeholder_variants_all_excluded(self):
        """複数の placeholder バリエーションを網羅的にテスト"""
        xml = _build_item_specifics_xml({
            'F1': 'Unknown',
            'F2': 'N/A',
            'F3': '-',
            'F4': 'None',
            'F5': 'Not specified',
            'F6': 'not applicable',
            'F7': '不明',
            'F8': 'Sony',  # 正常値
        })
        for name in ['F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7']:
            assert f'<Name>{name}</Name>' not in xml, (
                f'placeholder field {name} should be excluded'
            )
        assert '<Name>F8</Name>' in xml
        assert '<Value>Sony</Value>' in xml

    def test_list_value_placeholder_filtered(self):
        """list value 内の placeholder は個別に除外される"""
        xml = _build_item_specifics_xml({
            'Features': ['Bluetooth', 'Unknown', 'Wireless', 'N/A'],
        })
        assert '<Value>Bluetooth</Value>' in xml
        assert '<Value>Wireless</Value>' in xml
        assert '<Value>Unknown</Value>' not in xml
        assert '<Value>N/A</Value>' not in xml

    def test_all_placeholder_field_omits_itemspecifics_block(self):
        """全 field が placeholder → ItemSpecifics ブロック自体を出力しない"""
        xml = _build_item_specifics_xml({
            'F1': 'Unknown',
            'F2': 'N/A',
        })
        assert xml == '', '全部 placeholder なら空文字を返す (eBay reject 防止)'

    def test_value_exactly_65_chars_kept(self):
        """境界値: ちょうど 65字 は truncate されない"""
        val_65 = 'A' * 65
        xml = _build_item_specifics_xml({'Field': val_65})
        assert f'<Value>{val_65}</Value>' in xml

    def test_value_66_chars_truncated(self):
        """境界値: 66字は 65 字で切られる"""
        val_66 = 'A' * 66
        xml = _build_item_specifics_xml({'Field': val_66})
        import re
        m = re.search(r'<Value>([^<]*)</Value>', xml)
        assert m is not None
        assert len(m.group(1)) == 65

    def test_whitespace_trimmed_before_length_check(self):
        """trailing/leading whitespace は trim されてから長さ判定"""
        val = '  ' + ('X' * 65) + '  '  # 前後空白 + 65 chars
        xml = _build_item_specifics_xml({'Field': val})
        assert f'<Value>{"X" * 65}</Value>' in xml

    # -----------------------------------------------------------------
    # #44 (2026-07-04) 原産国混入チェーン封鎖 1/3: AddItem XML builder が
    # 禁止 Name (原産国/Manufacturer 系) を「値が正当でも」除外すること。
    # (旧 test_unknown_placeholder_value_excluded は value=N/A による placeholder
    #  除外の副産物で Name 自体を狙ったテストでは無かったため、本テストで固定する)
    # -----------------------------------------------------------------
    def test_country_of_origin_excluded_even_with_valid_value(self):
        xml = _build_item_specifics_xml({
            'Brand': 'Sony', 'Country of Origin': 'Japan',
        })
        assert '<Name>Brand</Name>' in xml
        assert '<Name>Country of Origin</Name>' not in xml
        assert '<Value>Japan</Value>' not in xml

    def test_all_forbidden_manufacturer_name_variants_excluded(self):
        xml = _build_item_specifics_xml({
            'Brand': 'Sony',
            'Country of Origin': 'Japan',
            'Country/Region of Manufacture': 'China',
            'Country of Manufacture': 'Vietnam',
            'Manufacturer': 'Sony Corp',
        })
        assert '<Name>Brand</Name>' in xml
        for forbidden in (
            'Country of Origin', 'Country/Region of Manufacture',
            'Country of Manufacture', 'Manufacturer',
        ):
            assert f'<Name>{forbidden}</Name>' not in xml

    def test_forbidden_name_case_insensitive(self):
        xml = _build_item_specifics_xml({
            'Brand': 'Sony', 'MANUFACTURER': 'Sony Corp', 'country of origin': 'Japan',
        })
        assert '<Name>MANUFACTURER</Name>' not in xml
        assert '<Name>country of origin</Name>' not in xml

    def test_all_forbidden_names_omits_itemspecifics_block(self):
        """全 field が禁止 Name → ItemSpecifics ブロック自体を出力しない."""
        xml = _build_item_specifics_xml({
            'Manufacturer': 'Sony Corp', 'Country of Origin': 'Japan',
        })
        assert xml == ''

    def test_forbidden_name_exclusion_logs_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger='monitor.ebay_lister'):
            _build_item_specifics_xml({'Brand': 'Sony', 'Manufacturer': 'Sony Corp'})
        assert any('Manufacturer' in r.message for r in caplog.records), (
            "禁止 Name 除外は Q0 (silent skip 禁止) のため warning ログが必要"
        )


# =========================================================================
# _build_pictures_xml
# =========================================================================

class TestBuildPicturesXml:
    def test_basic(self):
        xml = _build_pictures_xml(['https://a.jpg', 'https://b.jpg'])
        assert '<PictureDetails>' in xml
        assert '<PictureURL>https://a.jpg</PictureURL>' in xml
        assert '<PictureURL>https://b.jpg</PictureURL>' in xml

    def test_empty(self):
        assert _build_pictures_xml([]) == ''
        assert _build_pictures_xml(None) == ''  # type: ignore[arg-type]

    def test_over_24_clipped(self):
        urls = [f'https://e.com/{i}.jpg' for i in range(30)]
        xml = _build_pictures_xml(urls)
        count = xml.count('<PictureURL>')
        assert count == _MAX_PICTURES == 24

    def test_url_escape(self):
        xml = _build_pictures_xml(['https://a.com/?x=1&y=2'])
        # & は escape されて &amp; になる
        assert '&amp;y=2' in xml


# =========================================================================
# _build_add_fixed_price_item_xml
# =========================================================================

class TestBuildAddFixedPriceItemXml:
    def test_valid_xml_structure(self):
        xml = _build_add_fixed_price_item_xml(_minimal_params(), verify=False)
        # Well-formed XML (parse error 出ない) — USER_TOKEN を適当な値に置換してから
        xml_for_parse = xml.replace('{USER_TOKEN}', 'dummy_token')
        root = ET.fromstring(xml_for_parse)
        assert root.tag.endswith('AddFixedPriceItemRequest')

    def test_verify_root_tag(self):
        xml = _build_add_fixed_price_item_xml(_minimal_params(), verify=True)
        xml_for_parse = xml.replace('{USER_TOKEN}', 'dummy_token')
        root = ET.fromstring(xml_for_parse)
        assert root.tag.endswith('VerifyAddFixedPriceItemRequest')

    def test_required_fields_present(self):
        xml = _build_add_fixed_price_item_xml(_minimal_params(), verify=False)
        required = [
            '<Title>',
            '<Description>',
            '<PrimaryCategory>',
            '<CategoryID>112529</CategoryID>',
            '<ConditionID>3000</ConditionID>',
            '<SKU>W9-TEST-001</SKU>',
            '<StartPrice currencyID="USD">249.99</StartPrice>',
            '<Quantity>1</Quantity>',
            '<ListingDuration>GTC</ListingDuration>',
            '<ListingType>FixedPriceItem</ListingType>',
            '<Country>JP</Country>',
            '<Currency>USD</Currency>',
            '<Location>Tokyo, Japan</Location>',
            '<PostalCode>100-0001</PostalCode>',
            '<DispatchTimeMax>3</DispatchTimeMax>',
            '<ScheduleTime>',
            '<SellerProfiles>',
            '<PaymentProfileID>359244671023</PaymentProfileID>',
            '<ReturnProfileID>359243687023</ReturnProfileID>',
            '<ShippingProfileID>377279091023</ShippingProfileID>',
            '<ItemSpecifics>',
            '<PictureDetails>',
            '<WarningLevel>High</WarningLevel>',
        ]
        for r in required:
            assert r in xml, f'missing required field: {r}'

    def test_description_wrapped_in_cdata(self):
        xml = _build_add_fixed_price_item_xml(_minimal_params(), verify=False)
        # CDATA で Description HTML を包んでいること
        m = re.search(r'<Description><!\[CDATA\[(.*?)\]\]></Description>', xml, re.DOTALL)
        assert m is not None
        html_inside = m.group(1)
        # HTML がそのまま残っていること (escape されていない)
        assert '<div class="mh-wrap">' in html_inside
        assert '<h1>Sony WH-1000XM5</h1>' in html_inside
        assert '<style>.x{color:red;}</style>' in html_inside

    def test_schedule_time_format_and_range(self):
        before = datetime.now(timezone.utc)
        xml = _build_add_fixed_price_item_xml(_minimal_params(), verify=False)
        after = datetime.now(timezone.utc)
        m = re.search(r'<ScheduleTime>([^<]+)</ScheduleTime>', xml)
        assert m is not None
        s = m.group(1)
        assert re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$', s)
        dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.000Z').replace(tzinfo=timezone.utc)
        assert (before + timedelta(days=21) - timedelta(seconds=1)) <= dt <= (after + timedelta(days=21) + timedelta(seconds=1))

    def test_item_specifics_all_keys_present(self):
        xml = _build_add_fixed_price_item_xml(_minimal_params(), verify=False)
        assert '<Name>Brand</Name>' in xml
        assert '<Value>Sony</Value>' in xml
        assert '<Name>Model</Name>' in xml
        assert '<Value>WH-1000XM5</Value>' in xml
        assert '<Name>Type</Name>' in xml
        assert '<Value>Over-Ear</Value>' in xml
        assert '<Name>Color</Name>' in xml
        assert '<Value>Black</Value>' in xml

    def test_picture_url_limit_24(self):
        urls = [f'https://e.com/img{i}.jpg' for i in range(30)]
        xml = _build_add_fixed_price_item_xml(_minimal_params(image_urls=urls), verify=False)
        count = xml.count('<PictureURL>')
        assert count == 24

    def test_empty_images_no_picture_details(self):
        xml = _build_add_fixed_price_item_xml(_minimal_params(image_urls=[]), verify=False)
        assert '<PictureDetails>' not in xml

    def test_title_xml_escaped(self):
        # Title に "&" "<" ">" などの XML 特殊文字が含まれる場合 escape されること
        # (`"` は要素内容では escape 不要なので検証対象外 — XML 1.0 仕様)
        params = _minimal_params(ebay_title='Sony WH-1000XM5 <Special> & New')
        xml = _build_add_fixed_price_item_xml(params, verify=False)
        assert '&amp; New' in xml
        assert '&lt;Special&gt;' in xml
        # Well-formed XML として parse できること
        xml_for_parse = xml.replace('{USER_TOKEN}', 'dummy')
        ET.fromstring(xml_for_parse)

    def test_sku_omitted_when_empty(self):
        params = _minimal_params(sku='')
        xml = _build_add_fixed_price_item_xml(params, verify=False)
        # 空 SKU の場合は SKU タグ自体を出力しない
        assert '<SKU>' not in xml

    def test_user_token_placeholder_preserved(self):
        # USER_TOKEN は _call_trading_api が置換するため、プレースホルダで残すこと
        xml = _build_add_fixed_price_item_xml(_minimal_params(), verify=False)
        assert '{USER_TOKEN}' in xml


# =========================================================================
# _parse_add_item_response
# =========================================================================

class TestParseAddItemResponse:
    def test_parse_success(self):
        result = _parse_add_item_response(_SUCCESS_XML)
        assert result['success'] is True
        assert result['ack'] == 'Success'
        assert result['ebay_item_id'] == '358463512773'
        assert len(result['fees']) == 2
        assert result['fees'][0]['name'] == 'InsertionFee'
        assert result['fees'][0]['currency'] == 'USD'
        assert result['errors'] == []
        assert result['warnings'] == []
        assert result['parse_error'] is None

    def test_parse_warning(self):
        result = _parse_add_item_response(_WARNING_XML)
        assert result['success'] is True  # Warning でも success=True
        assert result['ack'] == 'Warning'
        assert result['ebay_item_id'] == '358463512774'
        assert len(result['warnings']) == 1
        assert 'images failed to upload' in result['warnings'][0]
        assert result['errors'] == []

    def test_parse_failure(self):
        result = _parse_add_item_response(_FAILURE_XML)
        assert result['success'] is False
        assert result['ack'] == 'Failure'
        assert result['ebay_item_id'] is None
        assert len(result['errors']) == 2
        assert 'category ID you supplied is invalid' in result['errors'][0]
        assert 'Shipping profile is required' in result['errors'][1]
        assert result['warnings'] == []

    def test_parse_empty(self):
        result = _parse_add_item_response('')
        assert result['success'] is False
        assert result['parse_error'] == 'empty_response_xml'

    def test_parse_malformed(self):
        result = _parse_add_item_response('<broken')
        assert result['success'] is False
        assert result['parse_error'] is not None
        assert 'xml_parse_error' in result['parse_error']


# =========================================================================
# verify_add_fixed_price_item (mock)
# =========================================================================

class TestVerifyAddFixedPriceItem:
    def test_missing_credentials(self):
        fake_creds = {'app_id': '', 'dev_id': '', 'cert_id': '', 'user_token': ''}
        with mock.patch(
            'monitor.ebay_lister.get_ebay_credentials',
            return_value=fake_creds,
        ):
            result = verify_add_fixed_price_item(_minimal_params())
        assert result['success'] is False
        assert any('ebay_credentials_missing' in e for e in result['errors'])

    def test_mocked_success(self):
        fake_api = {
            'success': True,
            'ack': 'Success',
            'raw': _SUCCESS_XML,
        }
        with mock.patch(
            'monitor.ebay_lister._call_trading_api',
            return_value=fake_api,
        ) as mocked:
            result = verify_add_fixed_price_item(
                _minimal_params(),
                app_id='A', dev_id='D', cert_id='C', user_token='T',
            )
        assert result['success'] is True
        assert result['ack'] == 'Success'
        assert len(result['fees']) == 2
        assert result['errors'] == []
        assert result['raw_xml'] == _SUCCESS_XML
        # call_name が VerifyAddFixedPriceItem で呼ばれていること
        mocked.assert_called_once()
        call_kwargs = mocked.call_args.kwargs
        assert call_kwargs.get('call_name') == 'VerifyAddFixedPriceItem'
        # XML body に {USER_TOKEN} プレースホルダ (実 token は _call_trading_api が置換)
        assert '{USER_TOKEN}' in call_kwargs.get('xml_body', '')

    def test_mocked_failure(self):
        fake_api = {
            'success': False,
            'message': 'API エラー: Category invalid',
            'raw': _FAILURE_XML,
        }
        with mock.patch(
            'monitor.ebay_lister._call_trading_api',
            return_value=fake_api,
        ):
            result = verify_add_fixed_price_item(
                _minimal_params(),
                app_id='A', dev_id='D', cert_id='C', user_token='T',
            )
        assert result['success'] is False
        assert result['ack'] == 'Failure'
        assert len(result['errors']) >= 2

    def test_mocked_network_failure(self):
        # _call_trading_api が raw=None を返す (通信段階失敗)
        fake_api = {'success': False, 'message': '通信エラー: timeout', 'raw': None}
        with mock.patch(
            'monitor.ebay_lister._call_trading_api',
            return_value=fake_api,
        ):
            result = verify_add_fixed_price_item(
                _minimal_params(),
                app_id='A', dev_id='D', cert_id='C', user_token='T',
            )
        assert result['success'] is False
        assert any('通信エラー' in e for e in result['errors'])
        assert result['raw_xml'] == ''

    def test_api_call_exception_caught(self):
        # _call_trading_api が例外を投げても dict で return し UI を壊さない
        with mock.patch(
            'monitor.ebay_lister._call_trading_api',
            side_effect=RuntimeError('boom'),
        ):
            result = verify_add_fixed_price_item(
                _minimal_params(),
                app_id='A', dev_id='D', cert_id='C', user_token='T',
            )
        assert result['success'] is False
        assert any('api_call_error' in e for e in result['errors'])


# =========================================================================
# add_fixed_price_item_draft (mock)
# =========================================================================

class TestAddFixedPriceItemDraft:
    def test_missing_credentials(self):
        fake_creds = {'app_id': '', 'dev_id': '', 'cert_id': '', 'user_token': ''}
        with mock.patch(
            'monitor.ebay_lister.get_ebay_credentials',
            return_value=fake_creds,
        ):
            result = add_fixed_price_item_draft(_minimal_params())
        assert result['success'] is False
        assert result['ebay_item_id'] is None
        assert any('ebay_credentials_missing' in e for e in result['errors'])

    def test_mocked_success_returns_item_id(self):
        fake_api = {'success': True, 'ack': 'Success', 'raw': _SUCCESS_XML}
        with mock.patch(
            'monitor.ebay_lister._call_trading_api',
            return_value=fake_api,
        ) as mocked:
            result = add_fixed_price_item_draft(
                _minimal_params(),
                app_id='A', dev_id='D', cert_id='C', user_token='T',
            )
        assert result['success'] is True
        assert result['ebay_item_id'] == '358463512773'
        assert len(result['fees']) == 2
        # scheduled_time が now + 21日 ± 3秒 の範囲内
        assert re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$', result['scheduled_time'])
        dt = datetime.strptime(result['scheduled_time'], '%Y-%m-%dT%H:%M:%S.000Z').replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert abs((dt - (now + timedelta(days=21))).total_seconds()) < 3
        # call_name が AddFixedPriceItem
        mocked.assert_called_once()
        assert mocked.call_args.kwargs.get('call_name') == 'AddFixedPriceItem'

    def test_mocked_warning_still_success(self):
        fake_api = {'success': True, 'ack': 'Warning', 'raw': _WARNING_XML}
        with mock.patch(
            'monitor.ebay_lister._call_trading_api',
            return_value=fake_api,
        ):
            result = add_fixed_price_item_draft(
                _minimal_params(),
                app_id='A', dev_id='D', cert_id='C', user_token='T',
            )
        assert result['success'] is True
        assert result['ebay_item_id'] == '358463512774'
        assert len(result['warnings']) == 1

    def test_mocked_failure_no_item_id(self):
        fake_api = {'success': False, 'message': 'API エラー', 'raw': _FAILURE_XML}
        with mock.patch(
            'monitor.ebay_lister._call_trading_api',
            return_value=fake_api,
        ):
            result = add_fixed_price_item_draft(
                _minimal_params(),
                app_id='A', dev_id='D', cert_id='C', user_token='T',
            )
        assert result['success'] is False
        assert result['ebay_item_id'] is None
        assert len(result['errors']) >= 2


# =========================================================================
# build_draft_params_from_phase3
# =========================================================================

class _FakeListing:
    """GeneratedListing 風のダミーオブジェクト。"""
    ebay_title = 'Sony WH-1000XM5 Wireless Headphones'
    ebay_description = '<div>html</div>'
    ebay_category_id = '112529'
    ebay_category_name = 'Headphones'
    item_specifics = {'Brand': 'Sony', 'Model': 'WH-1000XM5'}


class _FakeRank:
    rank_code = 'A'
    ebay_condition_id = '3000'


class _FakeReference:
    category_id = '999'


class TestBuildDraftParamsFromPhase3:
    def _config(self) -> dict:
        return {
            'ebay_business_policies': {
                'payment_policy_id': 'PPP',
                'return_policy_id': 'RRR',
            },
            'w9_listing_defaults': {
                'location': 'Osaka, Japan',
                'postal_code': '540-0001',
                'dispatch_time_max': 2,
            },
            'w9_draft_mode': {
                'scheduled_days_offset': 7,
            },
        }

    def test_basic(self):
        params = build_draft_params_from_phase3(
            product=None,
            reference=None,
            rank=_FakeRank(),
            listing=_FakeListing(),
            shipping_policy_id='SSS',
            sku='TEST-001',
            listing_price_usd=199.99,
            image_urls=['https://a.jpg'],
            config=self._config(),
        )
        assert params['sku'] == 'TEST-001'
        assert params['ebay_title'] == 'Sony WH-1000XM5 Wireless Headphones'
        assert params['ebay_category_id'] == '112529'
        assert params['listing_price_usd'] == 199.99
        assert params['payment_policy_id'] == 'PPP'
        assert params['return_policy_id'] == 'RRR'
        assert params['shipping_policy_id'] == 'SSS'
        assert params['location'] == 'Osaka, Japan'
        assert params['postal_code'] == '540-0001'
        assert params['dispatch_time_max'] == 2
        assert params['scheduled_days_offset'] == 7
        assert params['rank_code'] == 'A'
        assert params['ebay_condition_id'] == '3000'

    def test_defaults_without_config(self):
        params = build_draft_params_from_phase3(
            product=None,
            reference=None,
            rank=_FakeRank(),
            listing=_FakeListing(),
            shipping_policy_id='SSS',
            sku='',
            listing_price_usd=100.0,
            image_urls=[],
            config=None,
        )
        # settings.json が未提供 → 既定値 (Tokyo, 100-0001, 3日, 21日) が採用される
        assert params['location'] == 'Tokyo, Japan'
        assert params['postal_code'] == '100-0001'
        assert params['dispatch_time_max'] == 3
        assert params['scheduled_days_offset'] == 21

    def test_reference_category_fallback(self):
        # listing.ebay_category_id が空なら reference.category_id を採用
        class _ListingNoCategory(_FakeListing):
            ebay_category_id = None

        params = build_draft_params_from_phase3(
            product=None,
            reference=_FakeReference(),
            rank=_FakeRank(),
            listing=_ListingNoCategory(),
            shipping_policy_id='SSS',
            sku='X',
            listing_price_usd=10.0,
            image_urls=[],
            config=self._config(),
        )
        assert params['ebay_category_id'] == '999'

    def test_image_urls_clipped_to_max(self):
        urls = [f'https://e.com/{i}.jpg' for i in range(30)]
        params = build_draft_params_from_phase3(
            product=None,
            reference=None,
            rank=_FakeRank(),
            listing=_FakeListing(),
            shipping_policy_id='SSS',
            sku='X',
            listing_price_usd=1.0,
            image_urls=urls,
            config=None,
        )
        assert len(params['image_urls']) == _MAX_PICTURES == 24

    def test_listing_as_dict(self):
        # dataclass でなく dict でも動くこと (Phase 5 UI から dict を渡す想定にも対応)
        params = build_draft_params_from_phase3(
            product=None,
            reference=None,
            rank={'rank_code': 'B', 'ebay_condition_id': '3000'},
            listing={
                'ebay_title': 'test',
                'ebay_description': '<b>x</b>',
                'ebay_category_id': '1',
                'item_specifics': {'Brand': 'Sony'},
            },
            shipping_policy_id='SSS',
            sku='X',
            listing_price_usd=1.0,
            image_urls=[],
            config=None,
        )
        assert params['ebay_title'] == 'test'
        assert params['rank_code'] == 'B'
        assert params['item_specifics'] == {'Brand': 'Sony'}


class TestShippingServiceCostOverrideList2026_05_01:
    """eBay 公式 BP cost override 機構 ShippingServiceCostOverrideList のテスト.

    修正経緯:
      - 4/21 実装: <ShippingDetails> 直接指定 → BP と同居で silently ignored
      - 4/26 fix: ShippingType=Flat 追加 → necessary だが not sufficient
      - 5/1 first attempt: SellerShippingProfile 完全 omit → 動作するが eBay 非標準 (revert)
      - 5/1 final: ShippingServiceCostOverrideList = eBay 公式正攻法 (本実装)

    Reference:
      https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/ShippingServiceCostOverrideListType.html
    """

    def test_override_list_structure(self):
        """正しい公式 XML 構造で出力される."""
        from monitor.ebay_lister import _build_shipping_service_cost_override_list_xml
        xml = _build_shipping_service_cost_override_list_xml(cost=20.0, additional=20.0)
        assert '<ShippingServiceCostOverrideList>' in xml
        assert '<ShippingServiceCostOverride>' in xml
        assert '<ShippingServiceType>Domestic</ShippingServiceType>' in xml, (
            'BP の Domestic service を override する仕様、type 必須'
        )
        assert '<ShippingServicePriority>1</ShippingServicePriority>' in xml, (
            'BP 内の sortOrderId と一致させる必須要素 (default 1)'
        )

    def test_override_with_price_20_percent(self):
        """price $100 → cost $20.00 / additional $20.00 が正しく XML に出る."""
        from monitor.ebay_lister import _build_shipping_service_cost_override_list_xml
        xml = _build_shipping_service_cost_override_list_xml(cost=20.0, additional=20.0)
        assert '<ShippingServiceCost currencyID="USD">20.00</ShippingServiceCost>' in xml
        assert '<ShippingServiceAdditionalCost currencyID="USD">20.00</ShippingServiceAdditionalCost>' in xml

    def test_override_none_returns_empty(self):
        """両方 None なら空文字 (BP の cost を完全踏襲、override しない)."""
        from monitor.ebay_lister import _build_shipping_service_cost_override_list_xml
        xml = _build_shipping_service_cost_override_list_xml(cost=None, additional=None)
        assert xml == ''

    def test_priority_customizable(self):
        """priority param で BP 内の sortOrderId に合わせられる."""
        from monitor.ebay_lister import _build_shipping_service_cost_override_list_xml
        xml = _build_shipping_service_cost_override_list_xml(
            cost=20.0, additional=20.0, priority=2,
        )
        assert '<ShippingServicePriority>2</ShippingServicePriority>' in xml


class TestShippingProfileAndOverrideCoexist2026_05_01:
    """eBay 公式仕様: SellerShippingProfile + ShippingServiceCostOverrideList が共存.

    BP は service 定義 (Domestic / International / 業者 etc.) を保持し、
    override list は cost のみ listing 単位で上書きする. 両方が XML 内に出る.
    """

    def test_both_seller_shipping_profile_and_override_present(self):
        """override 指定時、両方 (BP + override) が XML に存在."""
        params = _minimal_params(
            shipping_cost_usd_override=20.00,
            shipping_additional_cost_usd=20.00,
        )
        xml = _build_add_fixed_price_item_xml(params, verify=False)
        # Business Policy (BP) は維持
        assert '<SellerShippingProfile>' in xml, (
            'BP profile は eBay 公式仕様で必須 (SellerShippingProfile omit は非標準)'
        )
        assert f'<ShippingProfileID>{params["shipping_policy_id"]}</ShippingProfileID>' in xml
        # 公式 override 機構が同時に存在
        assert '<ShippingServiceCostOverrideList>' in xml, (
            '公式 override 機構が cost を listing 単位で上書きする'
        )
        assert '<ShippingServiceCost currencyID="USD">20.00</ShippingServiceCost>' in xml

    def test_no_override_keeps_only_seller_shipping_profile(self):
        """override 不在時、ShippingServiceCostOverrideList は出ない (BP cost を完全踏襲)."""
        params = _minimal_params(
            shipping_cost_usd_override=None,
            shipping_additional_cost_usd=None,
        )
        xml = _build_add_fixed_price_item_xml(params, verify=False)
        assert '<SellerShippingProfile>' in xml
        assert '<ShippingServiceCostOverrideList>' not in xml

    def test_legacy_shipping_details_block_not_emitted(self):
        """旧 <ShippingDetails> direct injection は完全廃止 (4/26 fix の遺物)."""
        params = _minimal_params(
            shipping_cost_usd_override=20.00,
            shipping_additional_cost_usd=20.00,
        )
        xml = _build_add_fixed_price_item_xml(params, verify=False)
        assert '<ShippingDetails>\n' not in xml, (
            '旧 ShippingDetails block 残存 = 5/1 first attempt の revert 漏れ'
        )
        assert '<ShippingType>Flat</ShippingType>' not in xml, (
            'ShippingType=Flat も旧経路の遺物、ShippingServiceCostOverride では不要'
        )


class _RichRank:
    """RankClassification 風 (rank_jp / reasoning も含む) のダミー."""
    rank_code = 'A'
    rank_label = 'Excellent'
    rank_jp = 'Tested · Minor Wear'  # 英語だが意味重複 — 出品文に出さないこと
    reasoning = 'UI で手動指定'  # 日本語 — 出品文に絶対漏出させない
    quick_notes = ''  # RankClassification dataclass には実際この field はない
    ebay_condition_id = '3000'


class TestConditionDescriptionFormat2026_05_01:
    """個別出品 (AddItem) ConditionDescription の書式ガード.

    出典:
      - 2026-05-01 fix: 日本語混入 + 生硬な区切り bug (`Rank A — Excellent | ...`)
      - 2026-07-04 fix: user 追加報告 358754421540 で「quick_notes 直接連結による
        AI 自由文混入」経路が残存と判明 → `resolve_condition_description_for_rank`
        (UI パネル #44 と同じ state 層 helper) に一本化した。
        新書式: A/B/C/D/PO は「Rank X — Label. <状態文>.」の 65 字以内定型。
        As-Is のみ「As-Is — <reason>」を維持 (商品固有理由必須)。quick_notes は
        AddItem 経路でも CD には転記しない (description 本文の Quick Notes 用)。
    """

    def _config(self) -> dict:
        return {
            'ebay_business_policies': {'payment_policy_id': 'P', 'return_policy_id': 'R'},
            'w9_listing_defaults': {},
            'w9_draft_mode': {'scheduled_days_offset': 21},
        }

    def test_no_japanese_reasoning_leak(self):
        """`reasoning="UI で手動指定"` が ConditionDescription に絶対出ないこと."""
        params = build_draft_params_from_phase3(
            product=None, reference=None, rank=_RichRank(), listing=_FakeListing(),
            shipping_policy_id='SSS', sku='X',
            listing_price_usd=100.0, image_urls=[], config=self._config(),
        )
        cd = params['condition_description']
        assert 'UI で手動指定' not in cd, (
            f'Japanese reasoning leaked into ConditionDescription: {cd!r}'
        )
        assert '手動指定' not in cd
        # 日本語文字 (ひらがな・カタカナ・漢字) を含まない
        for ch in cd:
            assert ord(ch) < 0x3000 or ord(ch) > 0x9FFF, (
                f'Japanese char {ch!r} (U+{ord(ch):04X}) found in {cd!r}'
            )

    def test_rank_jp_not_included(self):
        """rank_jp は rank_label と意味重複のため ConditionDescription から除外."""
        params = build_draft_params_from_phase3(
            product=None, reference=None, rank=_RichRank(), listing=_FakeListing(),
            shipping_policy_id='SSS', sku='X',
            listing_price_usd=100.0, image_urls=[], config=self._config(),
        )
        cd = params['condition_description']
        # rank_jp の値 ("Tested · Minor Wear") は出ないこと
        assert 'Minor Wear' not in cd, f'rank_jp leaked: {cd!r}'
        assert '·' not in cd  # middle dot

    def test_new_template_format_2026_07_04(self):
        """2026-07-04 新書式: A ランクは resolve_condition_description_for_rank の
        テンプレ (`Rank A — Excellent. Tested, fully working. Minor wear.`) に一本化."""
        params = build_draft_params_from_phase3(
            product=None, reference=None, rank=_RichRank(), listing=_FakeListing(),
            shipping_policy_id='SSS', sku='X',
            listing_price_usd=100.0, image_urls=[], config=self._config(),
        )
        cd = params['condition_description']
        assert cd == 'Rank A — Excellent. Tested, fully working. Minor wear.', (
            f'expected new 2026-07-04 template, got {cd!r}'
        )
        assert ' | ' not in cd, f"old ' | ' separator still present: {cd!r}"
        assert cd.endswith('.'), f"missing trailing period: {cd!r}"
        assert len(cd) <= 65, f'over 65 chars: len={len(cd)} cd={cd!r}'

    def test_quick_notes_not_appended_to_cd_2026_07_04(self):
        """2026-07-04 fix (358754421540 事例): quick_notes は CD に転記しない
        (description 本文の Quick Notes 欄で使う)。AddItem 経路でも UI パネル (#44)
        と同じく resolve_condition_description_for_rank の定型に一本化する
        = quick_notes 自由文の CD 混入経路を根絶する回帰テスト."""
        class _RankWithNotes(_RichRank):
            quick_notes = 'Tested and confirmed working (Bluetooth OK / Battery OK)'
        params = build_draft_params_from_phase3(
            product=None, reference=None, rank=_RankWithNotes(), listing=_FakeListing(),
            shipping_policy_id='SSS', sku='X',
            listing_price_usd=100.0, image_urls=[], config=self._config(),
        )
        cd = params['condition_description']
        # 新書式のテンプレそのまま (quick_notes は連結されない)
        assert cd == 'Rank A — Excellent. Tested, fully working. Minor wear.'
        assert 'Bluetooth OK' not in cd, (
            f'quick_notes leaked into CD (2026-07-04 root-cause path): {cd!r}'
        )

    def test_new_template_all_used_ranks_within_65_chars(self):
        """N (1000, CD 非対応で空文字) 以外の 6 ランクが全て 65 字以内 + 「Rank X — 」で
        始まること (書式変更の網羅回帰)."""
        expected = {
            'S':  ('New (Opened)',    'Rank S — New (Opened). Unused, no visible wear.'),
            'A':  ('Excellent',       'Rank A — Excellent. Tested, fully working. Minor wear.'),
            'B':  ('Good',            'Rank B — Good. Tested, fully working. Visible wear.'),
            'C':  ('Fair',            'Rank C — Fair. Tested, fully working. Heavy wear.'),
            'D':  ('Issues',          'Rank D — Issues. Tested; works within limits.'),
            'PO': ('Power-On Only',   'Rank PO — Power-On Only. Full function not verified.'),
        }
        for code, (label, expected_cd) in expected.items():
            class _R:
                pass
            _R.rank_code = code
            _R.rank_label = label
            _R.rank_jp = ''
            _R.reasoning = ''
            _R.quick_notes = 'IGNORED — must not appear in CD'
            _R.ebay_condition_id = '1500' if code == 'S' else '3000'
            params = build_draft_params_from_phase3(
                product=None, reference=None, rank=_R, listing=_FakeListing(),
                shipping_policy_id='SSS', sku='X',
                listing_price_usd=100.0, image_urls=[], config=self._config(),
            )
            cd = params['condition_description']
            assert cd == expected_cd, f'rank={code}: got {cd!r}'
            assert len(cd) <= 65, f'rank={code}: over 65 chars ({len(cd)}): {cd!r}'
            assert cd.startswith(f'Rank {code} — ')
            assert 'IGNORED' not in cd  # quick_notes は CD に混入しない

    def test_new_template_rank_n_returns_empty_cd(self):
        """N (ConditionID 1000) は eBay 仕様上 CD 非対応 = 空文字を返す
        (resolve_condition_description_for_rank の "N" 例外分岐と整合)."""
        class _RN:
            rank_code = 'N'
            rank_label = 'New'
            rank_jp = ''
            reasoning = ''
            quick_notes = ''
            ebay_condition_id = '1000'
        params = build_draft_params_from_phase3(
            product=None, reference=None, rank=_RN(), listing=_FakeListing(),
            shipping_policy_id='SSS', sku='X',
            listing_price_usd=100.0, image_urls=[], config=self._config(),
        )
        assert params['condition_description'] == ''

    def test_as_is_rank_no_rank_prefix_duplication(self):
        """rank_code='As-Is' rank_label='As-Is' で `Rank As-Is — As-Is` にならない (CLAUDE.md L243).

        実 _RANK_TABLE['As-Is'] = ('As-Is', ...) で rank_code == rank_label の特殊ケース.
        旧 fix では `Rank As-Is — As-Is.` と冗長化していた.
        """
        class _AsIsRank:
            rank_code = 'As-Is'
            rank_label = 'As-Is'
            rank_jp = 'Not Tested · No Warranty'
            reasoning = '動作未確認'
            quick_notes = 'No AC adapter for testing'
            ebay_condition_id = '7000'
        params = build_draft_params_from_phase3(
            product=None, reference=None, rank=_AsIsRank(), listing=_FakeListing(),
            shipping_policy_id='SSS', sku='X',
            listing_price_usd=100.0, image_urls=[], config=self._config(),
        )
        cd = params['condition_description']
        assert 'Rank As-Is' not in cd, f'redundant Rank prefix on As-Is: {cd!r}'
        assert cd.startswith('As-Is —'), f'expected `As-Is — <reason>` format: {cd!r}'
        assert 'No AC adapter' in cd, f'reason missing: {cd!r}'

    def test_as_is_condition_description_max_65_chars(self):
        """CLAUDE.md L246: As-Is は 65 字以内必須. 長文 quick_notes でも clip される."""
        class _AsIsLong:
            rank_code = 'As-Is'
            rank_label = 'As-Is'
            rank_jp = ''
            reasoning = ''
            quick_notes = 'Very long reason ' * 10  # ~170 字
            ebay_condition_id = '7000'
        params = build_draft_params_from_phase3(
            product=None, reference=None, rank=_AsIsLong(), listing=_FakeListing(),
            shipping_policy_id='SSS', sku='X',
            listing_price_usd=100.0, image_urls=[], config=self._config(),
        )
        cd = params['condition_description']
        assert len(cd) <= 65, f'over 65 chars: len={len(cd)} cd={cd!r}'

    def test_as_is_without_quick_notes_uses_placeholder(self):
        """As-Is で quick_notes 空 = silent fall-through を防ぐため明示的 placeholder.

        Q0 silent skip prevention: `As-Is.` だけで eBay に届くと VerifyAdd warning
        だが通る → buyer 紛争で defect 確定リスク. placeholder で reason 不在を可観測化.
        """
        class _AsIsBare:
            rank_code = 'As-Is'
            rank_label = 'As-Is'
            rank_jp = ''
            reasoning = ''
            quick_notes = ''  # 空
            ebay_condition_id = '7000'
        params = build_draft_params_from_phase3(
            product=None, reference=None, rank=_AsIsBare(), listing=_FakeListing(),
            shipping_policy_id='SSS', sku='X',
            listing_price_usd=100.0, image_urls=[], config=self._config(),
        )
        cd = params['condition_description']
        assert cd.startswith('As-Is —'), f'expected `As-Is — <placeholder>`: {cd!r}'
        # `As-Is.` だけにならず、reason 部分が必ず存在する
        assert cd != 'As-Is.', 'silent fall-through: As-Is reason missing'
        assert len(cd) > len('As-Is — '), f'placeholder text empty: {cd!r}'

    def test_manual_override_reasoning_string_is_english(self):
        """tab_individual_listing.py L1312: reasoning='manual override' (英語固定).

        旧 'UI で手動指定' (日本語) が ConditionDescription に reasoning fallback
        経由で漏出していた問題の static check. tab module を import して定数を確認.
        """
        import inspect
        from tabs import tab_individual_listing
        src = inspect.getsource(tab_individual_listing)
        # 旧日本語が source code に残っていないこと
        assert 'reasoning="UI で手動指定"' not in src, (
            'old Japanese reasoning literal still in source'
        )
        # 新英語版が存在すること
        assert 'reasoning="manual override"' in src, (
            'expected English reasoning="manual override" not found'
        )



class TestActiveImmediatePublication2026_05_01:
    """2026-05-01 fix: scheduled_days_offset=0 で Active 即時公開 (ScheduleTime 要素省略).

    旧: 必ず now+21日 の Scheduled、user が shipping policy 反映を eBay 上で確認できない.
    新: 0 sentinel で ScheduleTime XML 要素を省略 → eBay は即 Active 公開.
    """

    def test_schedule_time_element_omitted_when_offset_zero(self):
        """scheduled_days_offset=0 → XML から <ScheduleTime> 要素ごと省略."""
        params = _minimal_params(scheduled_days_offset=0)
        # XML ビルダは _fixed_schedule_time が無ければ scheduled_days_offset を見る
        xml = _build_add_fixed_price_item_xml(params, verify=False)
        assert '<ScheduleTime>' not in xml, (
            f'ScheduleTime element should be omitted for active immediate, '
            f'got XML containing it: {xml[:500]}'
        )

    def test_schedule_time_present_when_offset_positive(self):
        """既存挙動: scheduled_days_offset=21 → <ScheduleTime> 要素あり (回帰防止)."""
        params = _minimal_params(scheduled_days_offset=21)
        xml = _build_add_fixed_price_item_xml(params, verify=False)
        assert '<ScheduleTime>' in xml, 'regression: 21日指定で ScheduleTime が出ない'
        m = re.search(r'<ScheduleTime>([^<]+)</ScheduleTime>', xml)
        assert m, 'ScheduleTime tag malformed'
        # ISO 8601 UTC 形式 (末尾 Z)
        assert m.group(1).endswith('Z')

    def test_fixed_schedule_time_empty_string_respected(self):
        """`_fixed_schedule_time=''` (sentinel) でも ScheduleTime 要素省略."""
        params = _minimal_params(scheduled_days_offset=21)
        params['_fixed_schedule_time'] = ''  # 明示的 sentinel
        xml = _build_add_fixed_price_item_xml(params, verify=False)
        assert '<ScheduleTime>' not in xml, (
            f'_fixed_schedule_time="" should omit element, got: {xml[:500]}'
        )

    def test_build_draft_params_respects_offset_zero(self):
        """build_draft_params_from_phase3 が settings の 0 を尊重 (旧 `or` で 21 復活する bug 防止)."""
        cfg = {
            'ebay_business_policies': {'payment_policy_id': 'P', 'return_policy_id': 'R'},
            'w9_listing_defaults': {},
            'w9_draft_mode': {'scheduled_days_offset': 0},
        }
        params = build_draft_params_from_phase3(
            product=None, reference=None, rank=_FakeRank(), listing=_FakeListing(),
            shipping_policy_id='SSS', sku='X',
            listing_price_usd=100.0, image_urls=[], config=cfg,
        )
        assert params['scheduled_days_offset'] == 0, (
            f"settings の 0 が `or _DEFAULT (21)` で消えている: {params['scheduled_days_offset']}"
        )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
