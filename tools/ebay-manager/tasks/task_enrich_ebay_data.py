#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: eBay出品データを自動抽出して sku_conversion_results.json に充実

概要：
  既存の monitor/ebay_client.py の GetItem API レスポンスから
  weight, dimensions, price, condition などのデータを抽出し、
  sku_conversion_results.json に追加する。

ロジック：
  1. sku_conversion_results.json の sourced アイテムを読み込み
  2. 各アイテムの ebay_id から GetItem API で詳細取得（monitor/ebay_client.py 利用）
  3. Weight, Dimensions, Price, Condition などを抽出
  4. Description から includes, warranty を判定
  5. sku_conversion_results.json に書き込み
  6. ログ＆結果レポート出力
"""

import sys
import json
import logging
import re
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime
import xml.etree.ElementTree as ET

import httpx

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
TRADING_API_URL = "https://api.ebay.com/ws/api.dll"
API_VERSION = "967"

# monitor モジュール をインポート
sys.path.insert(0, str(BASE_DIR / 'monitor'))


def _load_ebay_credentials() -> Dict:
    """eBay API credentials を設定から読み込む"""
    config_file = BASE_DIR / 'config' / 'schedule_config.json'
    if not config_file.exists():
        logger.warning("設定ファイルが見つかりません")
        return {}

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        return {
            'app_id': config.get('ebay', {}).get('app_id'),
            'dev_id': config.get('ebay', {}).get('dev_id'),
            'cert_id': config.get('ebay', {}).get('cert_id'),
            'user_token': config.get('ebay', {}).get('user_token'),
        }
    except Exception as e:
        logger.error(f"設定読み込みエラー: {e}")
        return {}


def _build_get_item_xml(item_id: str, user_token: str) -> str:
    """GetItem リクエスト XML を生成"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{user_token}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""


def get_ebay_item_details(
    item_id: str,
    credentials: Dict
) -> Optional[Dict]:
    """
    eBay GetItem API で出品詳細情報を取得

    Args:
        item_id: eBay item ID
        credentials: API credentials (app_id, dev_id, cert_id, user_token)

    Returns:
        {
            'weight_g': float,
            'length_cm': float,
            'width_cm': float,
            'height_cm': float,
            'item_price_usd': float,
            'condition': str,
            'includes': str,
            'warranty': str
        }
    """

    xml_body = _build_get_item_xml(item_id, credentials['user_token'])

    headers = {
        'X-EBAY-API-SITEID': '0',
        'X-EBAY-API-COMPATIBILITY-LEVEL': API_VERSION,
        'X-EBAY-API-CALL-NAME': 'GetItem',
        'X-EBAY-API-APP-NAME': credentials['app_id'],
        'X-EBAY-API-DEV-NAME': credentials['dev_id'],
        'X-EBAY-API-CERT-NAME': credentials['cert_id'],
        'Content-Type': 'text/xml',
    }

    try:
        response = httpx.post(TRADING_API_URL, content=xml_body.encode('utf-8'), headers=headers, timeout=30.0)
        response.raise_for_status()

        # XML をパース
        root = ET.fromstring(response.text)
        ns = {'ns': 'urn:ebay:apis:eBLBaseComponents'}

        # エラーチェック
        ack = root.findtext('ns:Ack', namespaces=ns)
        if ack not in ('Success', 'Warning'):
            errors = root.findall('.//ns:Errors/ns:LongMessage', namespaces=ns)
            msg = '; '.join(e.text for e in errors if e.text) or 'Unknown error'
            logger.warning(f"API Error {item_id}: {msg}")
            return None

        # 基本情報を抽出
        item = root.find('.//ns:Item', ns)
        if item is None:
            logger.warning(f"Item not found in response: {item_id}")
            return None

        # 価格
        price_elem = item.find('ns:CurrentPrice', ns) or item.find('ns:StartPrice', ns)
        price_usd = float(price_elem.text) if price_elem is not None else 0.0

        # 重量（ポンドをグラムに変換）
        # eBay API では寸法情報が提供されない場合が多いため、デフォルト値を使用
        weight_major_elem = item.find('.//ns:ShippingDetails/ns:WeightMajor', ns)
        weight_minor_elem = item.find('.//ns:ShippingDetails/ns:WeightMinor', ns)

        weight_major = float(weight_major_elem.text) if weight_major_elem is not None else 0.0
        weight_minor = float(weight_minor_elem.text) if weight_minor_elem is not None else 0.0
        weight_lbs = weight_major + (weight_minor / 16.0)  # ポンドに変換
        weight_g = weight_lbs * 453.592  # グラムに変換

        # データがない場合はデフォルト値を使用（calculator.py が正常に動作するため）
        if weight_g == 0:
            weight_g = 500.0  # デフォルト: 500g（送料計算用の概算値）

        # 寸法（パッケージ情報から取得）
        length_cm = 0.0
        width_cm = 0.0
        height_cm = 0.0

        # ShippingPackageDetails から寸法を取得（インチ → cm）
        shipping_pkg = item.find('.//ns:ShippingPackageDetails', ns)
        if shipping_pkg is not None:
            length_elem = shipping_pkg.find('ns:DimensionLength', ns)
            width_elem = shipping_pkg.find('ns:DimensionWidth', ns)
            height_elem = shipping_pkg.find('ns:DimensionHeight', ns)

            if length_elem is not None:
                length_cm = float(length_elem.text) * 2.54  # インチ → cm
            if width_elem is not None:
                width_cm = float(width_elem.text) * 2.54
            if height_elem is not None:
                height_cm = float(height_elem.text) * 2.54

        # 商品状態
        condition_elem = item.find('.//ns:Condition/ns:ConditionID', ns)
        condition_id = condition_elem.text if condition_elem is not None else "3000"

        condition_map = {
            '1000': '新品',
            '3000': '中古',
            '4000': '未使用品',
            '5000': 'リサイクル品',
        }
        condition = condition_map.get(condition_id, '中古')

        # Description から includes と warranty を判定
        description_elem = item.find('.//ns:Description', ns)
        description = description_elem.text if description_elem is not None else ""

        includes = _parse_includes(description)
        warranty = _parse_warranty(description)

        logger.info(f"OK {item_id}: weight={weight_g:.0f}g, price=${price_usd}")

        return {
            'weight_g': weight_g,
            'length_cm': length_cm,
            'width_cm': width_cm,
            'height_cm': height_cm,
            'item_price_usd': price_usd,
            'condition': condition,
            'includes': includes,
            'warranty': warranty,
        }

    except Exception as e:
        logger.warning(f"NG {item_id}: {e}")
        return None


def _parse_includes(description: str) -> str:
    """
    Description から付属品情報を抽出

    Returns: 「本体のみ」「付属品完備」「ケースあり」等
    """

    if not description:
        return ""

    description_lower = description.lower()

    # キーワード判定
    if 'body only' in description_lower or '本体のみ' in description:
        return '本体のみ'
    elif 'complete' in description_lower or 'complete set' in description_lower or '完備' in description:
        return '付属品完備'
    elif 'with box' in description_lower or '箱あり' in description:
        return 'ボックス付き'
    elif 'case' in description_lower or 'ケース' in description:
        return 'ケース付き'
    elif 'no box' in description_lower or 'なし' in description:
        return 'ボックスなし'

    return ""


def _parse_warranty(description: str) -> str:
    """
    Description から保証情報を抽出

    Returns: 「ノークレーム」「保証あり」「1年保証」等
    """

    if not description:
        return ""

    description_lower = description.lower()

    # キーワード判定
    if 'no warranty' in description_lower or 'ノークレーム' in description or 'no claim' in description_lower:
        return 'ノークレーム'
    elif 'warranty' in description_lower or '保証' in description or '保障' in description:
        # 期間を抽出してみる
        if '30' in description or '1 month' in description_lower:
            return '30日保証'
        elif '90' in description or '3 month' in description_lower:
            return '90日保証'
        elif '1 year' in description_lower or '12 month' in description_lower:
            return '1年保証'
        else:
            return '保証あり'

    return ""


def enrich_sku_conversion_results(limit: int = None) -> Dict:
    """
    sku_conversion_results.json にeBay出品データを追加

    Args:
        limit: テスト用。指定した件数のみ処理（None=全件）

    Returns:
        {
            'success': bool,
            'total_items': int,
            'enriched_items': int,
            'failed_items': int,
            'message': str
        }
    """

    logger.info("【開始】eBay出品データの充実化")

    # 資格情報を読み込み
    credentials = _load_ebay_credentials()
    if not all(credentials.values()):
        logger.error("eBay API credentials が設定されていません")
        return {
            'success': False,
            'error': 'Missing eBay API credentials'
        }

    # sku_conversion_results.json を読み込み
    sku_file = BASE_DIR / 'data' / 'sku_conversion_results.json'
    if not sku_file.exists():
        logger.error(f"ファイルが見つかりません: {sku_file}")
        return {
            'success': False,
            'error': 'sku_conversion_results.json not found'
        }

    try:
        with open(sku_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"ファイル読み込みエラー: {e}")
        return {
            'success': False,
            'error': str(e)
        }

    sourced = data.get('sourced', [])
    total = len(sourced)

    if limit:
        sourced = sourced[:limit]
        logger.info(f"テストモード: 最初の {limit} 件のみ処理")

    enriched_count = 0
    failed_count = 0

    # 各商品について処理
    for idx, item in enumerate(sourced, 1):
        ebay_id = item.get('ebay_id')

        if not ebay_id:
            logger.warning(f"[{idx}/{len(sourced)}] ebay_id が見つかりません")
            failed_count += 1
            continue

        # 既にデータが充実していればスキップ
        if item.get('weight_g') and item.get('item_price_usd'):
            logger.info(f"[{idx}/{len(sourced)}] {ebay_id}: スキップ（既に充実）")
            enriched_count += 1
            continue

        # API で詳細情報を取得
        details = get_ebay_item_details(ebay_id, credentials)

        if details:
            # マージ
            item.update(details)
            enriched_count += 1
        else:
            failed_count += 1

    # ファイルに保存
    try:
        with open(sku_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info(f"✅ ファイルを保存: {sku_file}")
    except Exception as e:
        logger.error(f"ファイル保存エラー: {e}")
        return {
            'success': False,
            'error': str(e)
        }

    # 結果レポート
    logger.info(f"【完了】eBay出品データの充実化")
    logger.info(f"  総処理数: {len(sourced)}件")
    logger.info(f"  成功: {enriched_count}件")
    logger.info(f"  失敗: {failed_count}件")

    return {
        'success': True,
        'total_items': total,
        'enriched_items': enriched_count,
        'failed_items': failed_count,
        'message': f'Enriched {enriched_count}/{total} items'
    }


if __name__ == '__main__':
    import sys

    # テストモード
    limit = None
    if len(sys.argv) > 1:
        if sys.argv[1] == '--limit':
            limit = int(sys.argv[2])

    result = enrich_sku_conversion_results(limit=limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
