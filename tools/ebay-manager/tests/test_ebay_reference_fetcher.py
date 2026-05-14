#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ebay_reference_fetcher の単体テスト。

- extract_item_id の各 URL パターン網羅
- ReferenceListing dataclass
- XML parse (モックレスポンス)

実 API は呼ばない。認証失敗時の ReferenceListing.fetch_error を検証する。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

# tools/ebay-manager/ を sys.path に追加
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.ebay_reference_fetcher import (  # noqa: E402
    ReferenceListing,
    _parse_get_item_response,
    extract_item_id,
    fetch_reference_listing,
)


# =========================================================================
# extract_item_id
# =========================================================================

class TestExtractItemId:
    def test_plain_url(self):
        assert extract_item_id('https://www.ebay.com/itm/358463512773') == '358463512773'

    def test_url_with_title_slug(self):
        assert extract_item_id(
            'https://www.ebay.com/itm/Sony-WH-1000XM5/358463512773?hash=xxx'
        ) == '358463512773'

    def test_url_with_hash_query(self):
        assert extract_item_id(
            'https://www.ebay.com/itm/358463512773?hash=item123'
        ) == '358463512773'

    def test_url_with_fragment(self):
        assert extract_item_id(
            'https://www.ebay.com/itm/358463512773#details'
        ) == '358463512773'

    def test_raw_item_id_12digit(self):
        assert extract_item_id('358463512773') == '358463512773'

    def test_raw_item_id_9digit(self):
        assert extract_item_id('123456789') == '123456789'

    def test_raw_item_id_13digit(self):
        assert extract_item_id('1234567890123') == '1234567890123'

    def test_raw_item_id_with_whitespace(self):
        assert extract_item_id('  358463512773  ') == '358463512773'

    def test_not_ebay_url(self):
        assert extract_item_id('https://example.com/foo/bar') is None

    def test_empty(self):
        assert extract_item_id('') is None

    def test_none(self):
        assert extract_item_id(None) is None  # type: ignore[arg-type]

    def test_too_short(self):
        assert extract_item_id('12345678') is None  # 8桁は不適合

    def test_too_long(self):
        assert extract_item_id('12345678901234') is None  # 14桁は不適合

    def test_non_string(self):
        assert extract_item_id(358463512773) is None  # type: ignore[arg-type]

    def test_mixed_text(self):
        assert extract_item_id('商品: 358463512773') is None  # 生ID判定は strict


# =========================================================================
# ReferenceListing dataclass
# =========================================================================

class TestReferenceListingDataclass:
    def test_default_initialization(self):
        r = ReferenceListing(item_id='358463512773')
        assert r.item_id == '358463512773'
        assert r.category_id is None
        assert r.item_specifics_keys == []
        assert r.fetch_error is None

    def test_full_initialization(self):
        r = ReferenceListing(
            item_id='358463512773',
            category_id='3',
            category_name='Consumer Electronics > Headphones',
            condition_id='3000',
            condition_display_name='Used',
            item_specifics_keys=['Brand', 'Model', 'Type'],
            title_sample='Sony WH-1000XM5 Wireless Headphones',
        )
        assert r.category_id == '3'
        assert len(r.item_specifics_keys) == 3

    def test_error_state(self):
        r = ReferenceListing(
            item_id='',
            fetch_error='invalid_ebay_url_or_id',
        )
        assert r.fetch_error == 'invalid_ebay_url_or_id'

    def test_item_specifics_keys_independent(self):
        # default_factory 検証
        r1 = ReferenceListing(item_id='111111111')
        r2 = ReferenceListing(item_id='222222222')
        r1.item_specifics_keys.append('Brand')
        assert r2.item_specifics_keys == []


# =========================================================================
# _parse_get_item_response — モック XML から抽出
# =========================================================================

_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Timestamp>2026-04-20T00:00:00.000Z</Timestamp>
  <Ack>Success</Ack>
  <Version>967</Version>
  <Item>
    <ItemID>358463512773</ItemID>
    <Title>Sony WH-1000XM5 Wireless Headphones Black</Title>
    <PrimaryCategory>
      <CategoryID>112529</CategoryID>
      <CategoryName>Consumer Electronics:Portable Audio &amp; Headphones:Headphones</CategoryName>
    </PrimaryCategory>
    <ConditionID>3000</ConditionID>
    <ConditionDisplayName>Used</ConditionDisplayName>
    <ItemSpecifics>
      <NameValueList>
        <Name>Brand</Name>
        <Value>Sony</Value>
      </NameValueList>
      <NameValueList>
        <Name>Model</Name>
        <Value>WH-1000XM5</Value>
      </NameValueList>
      <NameValueList>
        <Name>Type</Name>
        <Value>Over-Ear</Value>
      </NameValueList>
      <NameValueList>
        <Name>Color</Name>
        <Value>Black</Value>
      </NameValueList>
    </ItemSpecifics>
  </Item>
</GetItemResponse>"""


_FAILURE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Timestamp>2026-04-20T00:00:00.000Z</Timestamp>
  <Ack>Failure</Ack>
  <Errors>
    <ShortMessage>Invalid item</ShortMessage>
    <LongMessage>The item you requested was not found.</LongMessage>
    <ErrorCode>17</ErrorCode>
  </Errors>
</GetItemResponse>"""


_MINIMAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Item>
    <ItemID>358463512773</ItemID>
  </Item>
</GetItemResponse>"""


class TestParseGetItemResponse:
    def test_parse_success(self):
        result = _parse_get_item_response(_SUCCESS_XML, '358463512773')
        assert result.fetch_error is None
        assert result.item_id == '358463512773'
        assert result.category_id == '112529'
        assert 'Headphones' in (result.category_name or '')
        assert result.condition_id == '3000'
        assert result.condition_display_name == 'Used'
        assert result.title_sample == 'Sony WH-1000XM5 Wireless Headphones Black'
        assert result.item_specifics_keys == ['Brand', 'Model', 'Type', 'Color']

    def test_parse_failure_ack(self):
        result = _parse_get_item_response(_FAILURE_XML, '999999999999')
        assert result.fetch_error is not None
        assert 'ack_Failure' in result.fetch_error
        assert 'not found' in result.fetch_error

    def test_parse_minimal(self):
        # 最小 XML: Item はあるが詳細情報がない
        result = _parse_get_item_response(_MINIMAL_XML, '358463512773')
        assert result.fetch_error is None
        assert result.item_id == '358463512773'
        assert result.category_id is None
        assert result.item_specifics_keys == []
        assert result.title_sample is None

    def test_parse_malformed_xml(self):
        # fetch_reference_listing 経由で ET.ParseError をハンドリング
        import xml.etree.ElementTree as ET
        try:
            _parse_get_item_response('<broken', '358463512773')
            assert False, 'should have raised ParseError'
        except ET.ParseError:
            pass  # 期待動作


# =========================================================================
# fetch_reference_listing — 認証欠如 / invalid URL などのガードパス
# =========================================================================

class TestFetchReferenceListingGuards:
    def test_invalid_url_returns_error(self):
        result = fetch_reference_listing('https://example.com/not-ebay')
        assert result.item_id == ''
        assert result.fetch_error is not None
        assert 'invalid_ebay_url_or_id' in result.fetch_error

    def test_empty_input(self):
        result = fetch_reference_listing('')
        assert result.fetch_error is not None
        assert 'invalid_ebay_url_or_id' in result.fetch_error

    def test_missing_credentials(self):
        """env / config に認証情報がない場合、API を呼ばずに credentials_missing を返す。"""
        fake_creds = {'app_id': '', 'dev_id': '', 'cert_id': '', 'user_token': ''}
        with mock.patch(
            'monitor.ebay_reference_fetcher.get_ebay_credentials',
            return_value=fake_creds,
        ):
            result = fetch_reference_listing('358463512773')
        assert result.item_id == '358463512773'
        assert result.fetch_error is not None
        assert 'ebay_credentials_missing' in result.fetch_error

    def test_api_success_mocked(self):
        """_call_trading_api をモックして GetItem 成功パスを検証。"""
        fake_creds = {
            'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C', 'user_token': 'T',
        }
        fake_api_result = {
            'success': True,
            'ack': 'Success',
            'raw': _SUCCESS_XML,
        }
        with mock.patch(
            'monitor.ebay_reference_fetcher.get_ebay_credentials',
            return_value=fake_creds,
        ), mock.patch(
            'monitor.ebay_reference_fetcher._call_trading_api',
            return_value=fake_api_result,
        ):
            result = fetch_reference_listing(
                'https://www.ebay.com/itm/358463512773'
            )
        assert result.fetch_error is None
        assert result.item_id == '358463512773'
        assert result.category_id == '112529'
        assert 'Brand' in result.item_specifics_keys

    def test_api_failure_mocked(self):
        """_call_trading_api が success=False を返した場合のハンドリング。"""
        fake_creds = {
            'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C', 'user_token': 'T',
        }
        fake_api_result = {
            'success': False,
            'message': 'API エラー: Invalid auth token',
        }
        with mock.patch(
            'monitor.ebay_reference_fetcher.get_ebay_credentials',
            return_value=fake_creds,
        ), mock.patch(
            'monitor.ebay_reference_fetcher._call_trading_api',
            return_value=fake_api_result,
        ):
            result = fetch_reference_listing('358463512773')
        assert result.fetch_error is not None
        assert 'api_failed' in result.fetch_error

    def test_api_empty_raw_mocked(self):
        fake_creds = {
            'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C', 'user_token': 'T',
        }
        fake_api_result = {'success': True, 'raw': None}
        with mock.patch(
            'monitor.ebay_reference_fetcher.get_ebay_credentials',
            return_value=fake_creds,
        ), mock.patch(
            'monitor.ebay_reference_fetcher._call_trading_api',
            return_value=fake_api_result,
        ):
            result = fetch_reference_listing('358463512773')
        assert result.fetch_error == 'empty_api_response'

    def test_api_malformed_xml_mocked(self):
        fake_creds = {
            'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C', 'user_token': 'T',
        }
        fake_api_result = {'success': True, 'raw': '<not-valid-xml'}
        with mock.patch(
            'monitor.ebay_reference_fetcher.get_ebay_credentials',
            return_value=fake_creds,
        ), mock.patch(
            'monitor.ebay_reference_fetcher._call_trading_api',
            return_value=fake_api_result,
        ):
            result = fetch_reference_listing('358463512773')
        assert result.fetch_error is not None
        assert 'xml_parse_error' in result.fetch_error
