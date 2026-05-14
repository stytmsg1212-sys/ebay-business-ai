#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBay Promoted Listings Standard (Marketing API v1) wrapper.

既存 Trading API とは別体系の REST API を叩く。認証は OAuth user token (`v^1.1#...`)
を Bearer ヘッダで渡す。環境変数 EBAY_USER_TOKEN が OAuth 形式である前提。

主機能:
  - `list_campaigns(state)`: 既存キャンペーン一覧取得
  - `ensure_default_campaign(config)`: 設定に campaign_id があればそれを返す、
    無ければ作成して settings.json を更新
  - `add_listing_to_campaign(campaign_id, ebay_item_id, bid_percentage)`: 個別 listing を Ad として追加

PRD 位置付け: W9 Phase 5 の Add 成功後、自動で 2% Promoted Listings Standard に登録する。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import httpx

if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr is not None and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from monitor.credentials import get_ebay_credentials

logger = logging.getLogger(__name__)

# eBay Marketing API エンドポイント (production)
_API_BASE = 'https://api.ebay.com/sell/marketing/v1'
_DEFAULT_MARKETPLACE = 'EBAY_US'
_DEFAULT_BID_PERCENTAGE = 2.0


def _get_oauth_token(config: Optional[dict] = None) -> Optional[str]:
    """OAuth user token を取得する。残り有効時間 < 10分なら自動で refresh する。
    OAuth 形式 (`v^1.1#...`) であれば Trading API / REST API の両方に使用可能。"""
    # 2026-04-21: ebay_oauth_refresh.get_valid_access_token で自動 refresh を経由
    try:
        from monitor.ebay_oauth_refresh import get_valid_access_token
        token = get_valid_access_token(config=config)
    except Exception as e:  # noqa: BLE001
        logger.exception(f'get_valid_access_token raised: {e}')
        # fallback: 直接 credentials から取得 (refresh なし)
        creds = get_ebay_credentials(config or {})
        token = creds.get('user_token') or ''
    if not token or not token.startswith('v^'):
        logger.warning(
            "EBAY_USER_TOKEN is not OAuth format (must start with 'v^1.1#'). "
            "Promoted Listings API will not work."
        )
        return None
    return token


def _api_call(
    method: str,
    path: str,
    json_body: Optional[dict] = None,
    config: Optional[dict] = None,
    timeout: float = 30.0,
) -> dict:
    """REST 呼出しヘルパ。戻り値: {success, status_code, data, errors}"""
    token = _get_oauth_token(config)
    if not token:
        return {
            'success': False,
            'status_code': 0,
            'errors': ['OAuth user token 未設定 (EBAY_USER_TOKEN が OAuth 形式である必要)'],
            'data': None,
        }
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-EBAY-C-MARKETPLACE-ID': _DEFAULT_MARKETPLACE,
    }
    url = f'{_API_BASE}{path}'
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as e:
        logger.exception('Marketing API request failed')
        return {
            'success': False, 'status_code': 0,
            'errors': [f'HTTP error: {e}'], 'data': None,
        }
    data: Optional[dict] = None
    try:
        data = resp.json() if resp.text else None
    except (ValueError, json.JSONDecodeError):
        data = None

    if 200 <= resp.status_code < 300:
        return {
            'success': True, 'status_code': resp.status_code,
            'errors': [], 'data': data,
        }
    # eBay REST のエラー形式: { "errors": [ {"errorId", "message", ...} ] }
    err_msgs: list[str] = []
    if isinstance(data, dict) and 'errors' in data:
        for e in data['errors'] or []:
            err_msgs.append(f"{e.get('errorId', '?')}: {e.get('message', '')}")
    if not err_msgs:
        err_msgs.append(f'HTTP {resp.status_code}: {resp.text[:500]}')
    return {
        'success': False, 'status_code': resp.status_code,
        'errors': err_msgs, 'data': data,
    }


def list_campaigns(
    state: str = 'RUNNING',
    config: Optional[dict] = None,
) -> dict:
    """PLS キャンペーン一覧を取得。

    Returns:
        {'success': bool, 'campaigns': [...], 'errors': [...]}
    """
    res = _api_call('GET', f'/ad_campaign?campaign_status={state}&limit=200', config=config)
    if not res['success']:
        return {'success': False, 'campaigns': [], 'errors': res['errors']}
    data = res['data'] or {}
    return {
        'success': True,
        'campaigns': data.get('campaigns') or [],
        'errors': [],
    }


def create_campaign(
    name: str,
    bid_percentage: float = _DEFAULT_BID_PERCENTAGE,
    config: Optional[dict] = None,
) -> dict:
    """Promoted Listings Standard (COST_PER_SALE) キャンペーンを作成する。

    Returns:
        {'success': bool, 'campaign_id': str|None, 'errors': [...]}
    """
    # 最小構成: manual な INVENTORY_PARTITION なしで、listing を Ad として個別追加する運用
    # campaignCriterion を省略すると INDIVIDUAL な PLS キャンペーンとして扱われる
    body = {
        'name': name,
        'marketplaceId': _DEFAULT_MARKETPLACE,
        'fundingStrategy': {
            'fundingModel': 'COST_PER_SALE',
            'bidPercentage': f'{bid_percentage:.1f}',
        },
        # startDate を省略するとすぐに RUNNING になる仕様
    }
    res = _api_call('POST', '/ad_campaign', json_body=body, config=config)
    if not res['success']:
        return {
            'success': False, 'campaign_id': None, 'errors': res['errors'],
        }
    # 成功時はレスポンスヘッダ Location: /ad_campaign/{id} にIDが入る
    # data が {"campaignId": "..."} で返ることもあり
    data = res['data'] or {}
    campaign_id = data.get('campaignId') or ''
    if not campaign_id:
        # fallback: 作成直後の listings を取得して探す
        listed = list_campaigns(state='RUNNING', config=config)
        for c in listed.get('campaigns') or []:
            if c.get('campaignName') == name:
                campaign_id = c.get('campaignId') or ''
                break
    return {
        'success': bool(campaign_id),
        'campaign_id': campaign_id,
        'errors': [] if campaign_id else ['campaign_id を応答から取得できず'],
    }


def add_listing_to_campaign(
    campaign_id: str,
    ebay_item_id: str,
    bid_percentage: float = _DEFAULT_BID_PERCENTAGE,
    config: Optional[dict] = None,
) -> dict:
    """既存キャンペーンに listing を Ad として追加する。

    Returns:
        {'success': bool, 'ad_id': str|None, 'errors': [...]}
    """
    if not campaign_id:
        return {
            'success': False, 'ad_id': None,
            'errors': ['campaign_id 未設定'],
        }
    if not ebay_item_id:
        return {
            'success': False, 'ad_id': None,
            'errors': ['ebay_item_id 未設定'],
        }
    body = {
        'listingId': str(ebay_item_id),
        'bidPercentage': f'{bid_percentage:.1f}',
    }
    res = _api_call(
        'POST', f'/ad_campaign/{campaign_id}/ad',
        json_body=body, config=config,
    )
    if not res['success']:
        return {
            'success': False, 'ad_id': None, 'errors': res['errors'],
        }
    data = res['data'] or {}
    return {
        'success': True,
        'ad_id': data.get('adId') or '',
        'errors': [],
    }


def ensure_default_campaign(
    config: dict,
    campaign_name: str = 'MonoHonpo General Promoted 2%',
    bid_percentage: float = _DEFAULT_BID_PERCENTAGE,
) -> dict:
    """config 内の settings から campaign_id を取得、無ければ作成する。

    Returns:
        {'success': bool, 'campaign_id': str|None, 'created': bool, 'errors': [...]}
    """
    pl_cfg = (config.get('ebay_promoted_listings') or {}) if isinstance(config, dict) else {}
    existing_id = (pl_cfg.get('campaign_id') or '').strip()
    if existing_id:
        return {
            'success': True, 'campaign_id': existing_id,
            'created': False, 'errors': [],
        }

    # 既存の同名キャンペーンを検索 (重複作成防止)
    listed = list_campaigns(state='RUNNING', config=config)
    if listed['success']:
        for c in listed['campaigns']:
            if c.get('campaignName') == campaign_name:
                return {
                    'success': True,
                    'campaign_id': c.get('campaignId') or '',
                    'created': False,
                    'errors': [],
                }

    # 新規作成
    res = create_campaign(campaign_name, bid_percentage=bid_percentage, config=config)
    if not res['success']:
        return {
            'success': False, 'campaign_id': None,
            'created': False, 'errors': res['errors'],
        }
    return {
        'success': True,
        'campaign_id': res['campaign_id'],
        'created': True,
        'errors': [],
    }


def enroll_new_listing(
    ebay_item_id: str,
    config: dict,
) -> dict:
    """Add 成功後に呼ぶ便利関数: campaign 確保 → listing 追加 まで一気に実行。

    settings.json の `ebay_promoted_listings.enabled=False` なら no-op。

    Returns:
        {'success': bool, 'campaign_id': str|None, 'ad_id': str|None,
         'skipped': bool, 'message': str, 'errors': [...]}
    """
    pl_cfg = (config.get('ebay_promoted_listings') or {}) if isinstance(config, dict) else {}
    if not pl_cfg.get('enabled', True):
        return {
            'success': True, 'campaign_id': None, 'ad_id': None,
            'skipped': True,
            'message': 'Promoted Listings 無効化設定のため skip',
            'errors': [],
        }
    if not pl_cfg.get('auto_enroll_new_listings', True):
        return {
            'success': True, 'campaign_id': None, 'ad_id': None,
            'skipped': True,
            'message': 'auto_enroll_new_listings=False のため skip',
            'errors': [],
        }
    campaign_name = pl_cfg.get('campaign_name') or 'MonoHonpo General Promoted 2%'
    bid = float(pl_cfg.get('bid_percentage') or _DEFAULT_BID_PERCENTAGE)

    ensure = ensure_default_campaign(
        config, campaign_name=campaign_name, bid_percentage=bid,
    )
    if not ensure['success']:
        return {
            'success': False, 'campaign_id': None, 'ad_id': None,
            'skipped': False,
            'message': 'campaign 確保失敗',
            'errors': ensure['errors'],
        }
    cid = ensure['campaign_id']
    add = add_listing_to_campaign(cid, ebay_item_id, bid_percentage=bid, config=config)
    if not add['success']:
        return {
            'success': False, 'campaign_id': cid, 'ad_id': None,
            'skipped': False,
            'message': f'campaign_id={cid} への listing 追加失敗',
            'errors': add['errors'],
        }
    return {
        'success': True,
        'campaign_id': cid,
        'ad_id': add['ad_id'],
        'skipped': False,
        'message': (
            f'Promoted Listings Standard {bid}% に登録成功 '
            f'(campaign_id={cid}, ad_id={add["ad_id"] or "?"})'
        ),
        'errors': [],
    }


if __name__ == '__main__':
    # 手動テスト: python -m monitor.ebay_promoted_listings <ItemID>
    logging.basicConfig(level=logging.INFO)
    cfg_path = Path(__file__).resolve().parent.parent / 'config' / 'schedule_config.json'
    cfg = json.loads(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
    if len(sys.argv) < 2:
        print('Usage: python -m monitor.ebay_promoted_listings <ItemID>')
        print('       python -m monitor.ebay_promoted_listings --list')
        sys.exit(1)
    if sys.argv[1] == '--list':
        r = list_campaigns(config=cfg)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        r = enroll_new_listing(sys.argv[1], cfg)
        print(json.dumps(r, indent=2, ensure_ascii=False))
