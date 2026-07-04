#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
参考 eBay Listing から Category / Item Specifics Keys / ConditionID を取得するモジュール (W9 Phase 2)

W9 個別新規出品機能の補助役として、ユーザーが提供した参考 eBay URL / ItemID から
GetItem API 経由で「コピー可能」な構造情報を抽出する。

取得するもの (コピー可):
  - PrimaryCategory.CategoryID / CategoryName
  - ConditionID / ConditionDisplayName (参考のみ、最終値は rank_classifier で上書き想定)
  - ItemSpecifics の Name 一覧 (Keys のみ。値は Claude が仕入先情報から埋める)

取得しないもの (VeRO / 著作権):
  - Description 本文
  - Pictures
  - Price

Title は参考のみ UI に表示し「コピー禁止」と明示する前提で fetch する。
"""
from __future__ import annotations

import logging
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

# pythonw gotcha ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (ValueError, OSError):
        pass

from monitor.credentials import get_ebay_credentials, ebay_credentials_ok
from monitor.ebay_client import (
    _build_get_item_xml,
    _call_trading_api,
    _is_forbidden_specific_name,
)

logger = logging.getLogger(__name__)

# eBay Trading API の XML namespace
_NS = 'urn:ebay:apis:eBLBaseComponents'


# =========================================================================
# dataclass
# =========================================================================

@dataclass
class ReferenceListing:
    """参考 eBay Listing から抽出した構造情報。"""
    item_id: str
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    condition_id: Optional[str] = None
    condition_display_name: Optional[str] = None
    item_specifics_keys: list[str] = field(default_factory=list)
    title_sample: Optional[str] = None       # 参考用 (SEO 分析) — UI に「コピー禁止」表示
    fetch_error: Optional[str] = None


# =========================================================================
# ItemID 抽出
# =========================================================================

# /itm/358463512773 または /itm/Foo-Bar/358463512773 の両方に対応。
# 末尾の ?hash=... や /?... は末尾捕捉しない(キャプチャ後に任意)。
_ITEM_ID_URL_PATTERN = re.compile(r'/itm/(?:[^/\s?#]+/)?(\d{9,13})(?:[/?#]|$)')
_ITEM_ID_RAW_PATTERN = re.compile(r'^\d{9,13}$')


def extract_item_id(url_or_id: str) -> Optional[str]:
    """URL または生 ItemID から eBay ItemID を抽出する。

    対応パターン:
      - https://www.ebay.com/itm/358463512773
      - https://www.ebay.com/itm/358463512773?hash=item123
      - https://www.ebay.com/itm/Sony-WH-1000XM5/358463512773?hash=xxx
      - 358463512773 (9-13桁の数字単独)

    Args:
        url_or_id: eBay URL または ItemID 文字列

    Returns:
        抽出された ItemID (12桁前後の数字文字列)。抽出失敗時は None。
    """
    if not url_or_id or not isinstance(url_or_id, str):
        return None

    s = url_or_id.strip()
    if not s:
        return None

    # 生 ItemID (9〜13桁の数字単独)
    if _ITEM_ID_RAW_PATTERN.match(s):
        return s

    # URL パターン
    m = _ITEM_ID_URL_PATTERN.search(s)
    if m:
        return m.group(1)

    return None


# =========================================================================
# XML namespace ヘルパ
# =========================================================================

def _ns(tag: str) -> str:
    """ElementTree の namespace 付きタグに変換。"""
    return f'{{{_NS}}}{tag}'


# =========================================================================
# 公開 API
# =========================================================================

def fetch_reference_listing(url_or_id: str, config: Optional[dict] = None) -> ReferenceListing:
    """参考 eBay URL/ItemID から GetItem API で構造情報を取得する。

    失敗時も例外を投げず、ReferenceListing.fetch_error にメッセージを格納して返す。

    Args:
        url_or_id: eBay URL または ItemID 文字列
        config: schedule_config.json の辞書 (env にフォールバック時の後方互換)

    Returns:
        ReferenceListing (部分取得可、fetch_error に失敗理由)
    """
    item_id = extract_item_id(url_or_id)
    if not item_id:
        return ReferenceListing(
            item_id='',
            fetch_error=f'invalid_ebay_url_or_id: {url_or_id!r}',
        )

    # 認証情報取得
    try:
        creds = get_ebay_credentials(config)
    except Exception as e:  # noqa: BLE001
        return ReferenceListing(
            item_id=item_id,
            fetch_error=f'credentials_load_failed: {e}',
        )

    if not ebay_credentials_ok(creds):
        missing = [k for k, v in creds.items() if not v]
        return ReferenceListing(
            item_id=item_id,
            fetch_error=f'ebay_credentials_missing: {missing}',
        )

    # GetItem API 呼出し
    try:
        xml_body = _build_get_item_xml(item_id)
        api_result = _call_trading_api(
            call_name='GetItem',
            xml_body=xml_body,
            app_id=creds['app_id'],
            dev_id=creds['dev_id'],
            cert_id=creds['cert_id'],
            user_token=creds['user_token'],
        )
    except Exception as e:  # noqa: BLE001
        return ReferenceListing(
            item_id=item_id,
            fetch_error=f'get_item_api_error: {e}',
        )

    if not api_result.get('success'):
        return ReferenceListing(
            item_id=item_id,
            fetch_error=f'api_failed: {api_result.get("message", "unknown")}',
        )

    raw_xml = api_result.get('raw')
    if not raw_xml:
        return ReferenceListing(
            item_id=item_id,
            fetch_error='empty_api_response',
        )

    # XML parse
    try:
        return _parse_get_item_response(raw_xml, item_id)
    except ET.ParseError as e:
        return ReferenceListing(
            item_id=item_id,
            fetch_error=f'xml_parse_error: {e}',
        )
    except Exception as e:  # noqa: BLE001
        return ReferenceListing(
            item_id=item_id,
            fetch_error=f'parse_unexpected_error: {e}',
        )


# =========================================================================
# XML レスポンス parse
# =========================================================================

def _parse_get_item_response(raw_xml: str, item_id: str) -> ReferenceListing:
    """GetItem API レスポンス XML から参考情報を抽出する。"""
    root = ET.fromstring(raw_xml)

    # Ack チェック (Success/Warning で続行)
    ack_el = root.find(_ns('Ack'))
    ack = ack_el.text if ack_el is not None else None
    if ack not in ('Success', 'Warning'):
        errors = root.findall(f'.//{_ns("Errors")}/{_ns("LongMessage")}')
        msg = '; '.join(e.text for e in errors if e.text is not None) or 'Unknown error'
        return ReferenceListing(
            item_id=item_id,
            fetch_error=f'ack_{ack}: {msg}',
        )

    item_el = root.find(_ns('Item'))
    if item_el is None:
        return ReferenceListing(
            item_id=item_id,
            fetch_error='no_item_in_response',
        )

    result = ReferenceListing(item_id=item_id)

    # PrimaryCategory
    primary_cat = item_el.find(_ns('PrimaryCategory'))
    if primary_cat is not None:
        cat_id_el = primary_cat.find(_ns('CategoryID'))
        cat_name_el = primary_cat.find(_ns('CategoryName'))
        if cat_id_el is not None and cat_id_el.text:
            result.category_id = cat_id_el.text.strip()
        if cat_name_el is not None and cat_name_el.text:
            result.category_name = cat_name_el.text.strip()

    # Condition
    cond_id_el = item_el.find(_ns('ConditionID'))
    cond_name_el = item_el.find(_ns('ConditionDisplayName'))
    if cond_id_el is not None and cond_id_el.text:
        result.condition_id = cond_id_el.text.strip()
    if cond_name_el is not None and cond_name_el.text:
        result.condition_display_name = cond_name_el.text.strip()

    # Title (参考のみ)
    title_el = item_el.find(_ns('Title'))
    if title_el is not None and title_el.text:
        result.title_sample = title_el.text.strip()[:300]

    # ItemSpecifics Keys
    # #44 (2026-07-04) 原産国混入チェーン封鎖 (3点封鎖の3、源流フィルタ):
    # 参考 listing (他セラー) の ItemSpecifics Keys を無除外で抽出すると、
    # Country of Origin / Country/Region of Manufacture / Manufacturer が
    # listing_generator.py の「Keys 完全一致で必須埋込」指示に乗って AddItem
    # XML まで伝播する (CLAUDE.md「Country of Origin / Manufacturer の layer
    # 分離」違反)。G2 の禁止 Name 集合 (revise_item_specifics と同一) を共有
    # import し、抽出時点 (源流) でも除外する多層防御。
    keys: list[str] = []
    item_specifics = item_el.find(_ns('ItemSpecifics'))
    if item_specifics is not None:
        for nvl in item_specifics.findall(_ns('NameValueList')):
            name_el = nvl.find(_ns('Name'))
            if name_el is not None and name_el.text:
                name = name_el.text.strip()
                if not name:
                    continue
                if _is_forbidden_specific_name(name):
                    logger.warning(
                        "fetch_reference_listing: 禁止 Name '%s' を item_specifics_keys "
                        "から除外 (原産国/Manufacturer 系、CLAUDE.md 規約)", name,
                    )
                    continue
                if name not in keys:
                    keys.append(name)
    result.item_specifics_keys = keys

    return result


if __name__ == '__main__':
    # 手動テスト例:
    #   python -m monitor.ebay_reference_fetcher https://www.ebay.com/itm/358463512773
    import json
    logging.basicConfig(level=logging.INFO)

    test_input = sys.argv[1] if len(sys.argv) > 1 else '358463512773'
    result = fetch_reference_listing(test_input)
    print(json.dumps({
        'item_id': result.item_id,
        'category_id': result.category_id,
        'category_name': result.category_name,
        'condition_id': result.condition_id,
        'condition_display_name': result.condition_display_name,
        'item_specifics_keys': result.item_specifics_keys,
        'title_sample': result.title_sample,
        'fetch_error': result.fetch_error,
    }, ensure_ascii=False, indent=2))
