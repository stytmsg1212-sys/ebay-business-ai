#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBay Taxonomy API wrapper.

Claude の訓練データに基づく category_id 推定は、eBay カテゴリツリーの
四半期更新で deprecated / 無効になることがある (ユーザー実例:
「The category is not valid, select another category」エラー多発)。

本モジュールは Taxonomy API v1 を叩いて **実際に eBay が受け付ける leaf カテゴリ**
を取得する。

主 API:
  - GET /commerce/taxonomy/v1/get_default_category_tree_id
  - GET /commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions?q=...

認証: OAuth user token (`v^1.1#...`) が必要 (Bearer ヘッダ)。
`monitor.ebay_oauth_refresh.get_valid_access_token()` で自動更新経由。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (ValueError, OSError):
        pass

logger = logging.getLogger(__name__)

_API_BASE = 'https://api.ebay.com/commerce/taxonomy/v1'
_DEFAULT_MARKETPLACE = 'EBAY_US'

# tree_id は marketplace 毎に固定 (EBAY_US = 0)、四半期〜年単位で変わる
# キャッシュファイル (7日有効)
_CACHE_DIR = Path(__file__).resolve().parent.parent / 'data'
_TREE_ID_CACHE_FILE = _CACHE_DIR / 'ebay_taxonomy_tree_id.json'
_TREE_ID_CACHE_TTL_SEC = 7 * 24 * 3600  # 7日


def _get_oauth_token(config: Optional[dict] = None) -> Optional[str]:
    """OAuth user token (v^1.1#) を auto-refresh 経由で取得する。"""
    try:
        from monitor.ebay_oauth_refresh import get_valid_access_token
        token = get_valid_access_token(config=config)
    except Exception as e:  # noqa: BLE001
        logger.warning(f'get_valid_access_token failed: {e}')
        token = None
    if not token or not token.startswith('v^'):
        logger.warning(
            'Taxonomy API は OAuth user token (v^1.1#) が必要。'
            'EBAY_USER_TOKEN を確認してください。'
        )
        return None
    return token


def _call_api(
    method: str, path: str,
    params: Optional[dict] = None,
    config: Optional[dict] = None,
    timeout: float = 15.0,
) -> dict:
    """Taxonomy API 呼出しヘルパ。戻り: {success, data, errors}"""
    token = _get_oauth_token(config)
    if not token:
        return {
            'success': False, 'data': None,
            'errors': ['OAuth token unavailable'],
        }
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'X-EBAY-C-MARKETPLACE-ID': _DEFAULT_MARKETPLACE,
    }
    url = f'{_API_BASE}{path}'
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, headers=headers, params=params)
    except httpx.HTTPError as e:
        logger.warning(f'Taxonomy API HTTP error: {e!r}')
        return {'success': False, 'data': None, 'errors': [f'http_error: {e}']}

    try:
        data = resp.json() if resp.text else None
    except (ValueError, json.JSONDecodeError):
        data = None

    if 200 <= resp.status_code < 300:
        return {'success': True, 'data': data, 'errors': []}

    errs = []
    if isinstance(data, dict) and 'errors' in data:
        for e in data.get('errors') or []:
            errs.append(f"{e.get('errorId', '?')}: {e.get('message', '')}")
    if not errs:
        errs.append(f'HTTP {resp.status_code}: {resp.text[:300]}')
    return {'success': False, 'data': data, 'errors': errs}


def get_default_category_tree_id(
    config: Optional[dict] = None,
    marketplace: str = _DEFAULT_MARKETPLACE,
    use_cache: bool = True,
) -> Optional[str]:
    """Marketplace ID に対応する category_tree_id を取得する。

    EBAY_US は通常 "0"。rarely-changing なので 7日キャッシュ。
    """
    if use_cache and _TREE_ID_CACHE_FILE.exists():
        try:
            cached = json.loads(_TREE_ID_CACHE_FILE.read_text(encoding='utf-8'))
            if (
                cached.get('marketplace') == marketplace
                and cached.get('fetched_at', 0) + _TREE_ID_CACHE_TTL_SEC > time.time()
            ):
                return str(cached.get('tree_id') or '')
        except (json.JSONDecodeError, OSError):
            pass

    res = _call_api(
        'GET', '/get_default_category_tree_id',
        params={'marketplace_id': marketplace},
        config=config,
    )
    if not res['success']:
        logger.warning(f"get_default_category_tree_id failed: {res['errors']}")
        return None
    data = res['data'] or {}
    tree_id = str(data.get('categoryTreeId') or '')
    if tree_id:
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _TREE_ID_CACHE_FILE.write_text(
                json.dumps({
                    'marketplace': marketplace,
                    'tree_id': tree_id,
                    'version': data.get('categoryTreeVersion'),
                    'fetched_at': int(time.time()),
                }, ensure_ascii=False),
                encoding='utf-8',
            )
        except OSError as e:
            logger.debug(f'tree_id cache write failed: {e}')
    return tree_id or None


def get_category_suggestions(
    query: str,
    config: Optional[dict] = None,
    marketplace: str = _DEFAULT_MARKETPLACE,
    tree_id: Optional[str] = None,
    max_results: int = 5,
) -> list[dict]:
    """商品タイトル等のクエリから eBay 推奨 leaf カテゴリを取得する。

    戻り値: [
        {
            'category_id': '14990',
            'category_name': 'Home Speakers & Subwoofers',
            'is_leaf': True,
            'ancestors': ['293', '3270', ...],  # root → parent の順
            'ancestors_names': ['Consumer Electronics', ...],
        },
        ...
    ]

    本 API の戻りは "categorySuggestions" というフィールドに dict のリストで
    返る。各要素には `category.categoryId`, `category.categoryName`,
    `categoryTreeNodeAncestors`, `categoryTreeNodeLevel` 等が入る。
    """
    q = (query or '').strip()
    if not q:
        return []

    if tree_id is None:
        tree_id = get_default_category_tree_id(config=config, marketplace=marketplace)
    if not tree_id:
        logger.warning('category_tree_id unavailable, cannot call get_category_suggestions')
        return []

    res = _call_api(
        'GET',
        f'/category_tree/{tree_id}/get_category_suggestions',
        params={'q': q[:300]},
        config=config,
    )
    if not res['success']:
        logger.warning(f"get_category_suggestions failed: {res['errors']}")
        return []

    data = res['data'] or {}
    raw = data.get('categorySuggestions') or []
    out: list[dict] = []
    for item in raw[:max_results]:
        if not isinstance(item, dict):
            continue
        cat = item.get('category') or {}
        ancestors_raw = item.get('categoryTreeNodeAncestors') or []
        out.append({
            'category_id': str(cat.get('categoryId') or '').strip(),
            'category_name': str(cat.get('categoryName') or '').strip(),
            # Taxonomy API の suggestions は全て leaf を返す仕様
            'is_leaf': True,
            'ancestors': [
                str((a or {}).get('categoryId') or '').strip()
                for a in ancestors_raw
            ],
            'ancestors_names': [
                str((a or {}).get('categoryName') or '').strip()
                for a in ancestors_raw
            ],
            # score / level も保存しておく (fine-tuning 候補順で使える)
            'category_tree_node_level': item.get('categoryTreeNodeLevel'),
        })
    # 空 category_id は捨てる
    return [s for s in out if s['category_id']]


def pick_best_category(
    query: str,
    config: Optional[dict] = None,
    preferred_root: Optional[str] = None,
) -> Optional[dict]:
    """suggestions の中から最適候補 1 件を選ぶ。

    preferred_root (例: "Consumer Electronics") が指定されると、その配下の
    suggestion を優先。指定なしなら先頭 (eBay 推奨スコア順) を返す。
    """
    suggestions = get_category_suggestions(query, config=config)
    if not suggestions:
        return None
    if preferred_root:
        for s in suggestions:
            if preferred_root in s.get('ancestors_names') or preferred_root == s.get('category_name'):
                return s
    return suggestions[0]


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    import sys as _sys
    cfg_path = Path(__file__).resolve().parent.parent / 'config' / 'schedule_config.json'
    cfg = json.loads(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
    if len(_sys.argv) < 2:
        print('Usage: python -m monitor.ebay_taxonomy <query>')
        _sys.exit(1)
    q = ' '.join(_sys.argv[1:])
    tree_id = get_default_category_tree_id(config=cfg)
    print(f'tree_id = {tree_id}')
    suggestions = get_category_suggestions(q, config=cfg)
    for s in suggestions:
        print(json.dumps(s, ensure_ascii=False, indent=2))
