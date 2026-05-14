#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBay OAuth Access Token 自動更新モジュール.

User Access Token は 2時間で失効するため、Refresh Token (18ヶ月) を使って
自動的に新 Access Token を発行し、.env を更新する。

基本設計:
- `get_valid_access_token()` が API 呼出し直前に呼ばれ、
  残り有効時間 < _REFRESH_THRESHOLD_SEC (既定 600s=10分) なら自動 refresh
- refresh 成功で新 Access Token + expires_at を .env に書き戻し
- 並行 refresh を避けるため threading.Lock で直列化
- Refresh Token 自体が失効した場合 (18ヶ月経過 or revoke) は明示エラー

呼出し側:
- `monitor.ebay_promoted_listings._get_oauth_token` 等から使う
- Trading API (`monitor.credentials.get_ebay_credentials`) も必要なら拡張
"""
from __future__ import annotations

import base64
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr is not None and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

logger = logging.getLogger(__name__)

# eBay OAuth Token エンドポイント (Production)
_TOKEN_ENDPOINT = 'https://api.ebay.com/identity/v1/oauth2/token'

# Access Token の残り有効時間がこの閾値以下なら pre-emptive refresh する
_REFRESH_THRESHOLD_SEC = 600  # 10分

# 本アプリが Consent で取得したスコープ一覧 (refresh 時に送信)
# 将来 scope 追加したら ここも更新すること
_SCOPES = ' '.join([
    'https://api.ebay.com/oauth/api_scope',
    'https://api.ebay.com/oauth/api_scope/sell.inventory',
    'https://api.ebay.com/oauth/api_scope/sell.account',
    'https://api.ebay.com/oauth/api_scope/sell.fulfillment',
    'https://api.ebay.com/oauth/api_scope/sell.finances',
    'https://api.ebay.com/oauth/api_scope/sell.marketing',
])

# .env 書込み競合避けのためのプロセス内ロック
_refresh_lock = threading.Lock()

# プロジェクトルート (= tools/ebay-manager/) の .env を探す
_ENV_PATH = Path(__file__).resolve().parent.parent / '.env'


def _load_env_dict(path: Optional[Path] = None) -> dict:
    """.env を dict として読み込む。存在しなければ空 dict。
    path=None の場合はモジュール変数 _ENV_PATH を動的参照 (テスト差替え対応)。"""
    if path is None:
        path = _ENV_PATH
    if not path.exists():
        return {}
    env: dict = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip()
    return env


def _write_env_values(updates: dict, path: Optional[Path] = None) -> None:
    """.env を保持しつつ key の値だけ更新する。存在しない key は末尾追加。
    path=None の場合はモジュール変数 _ENV_PATH を動的参照 (テスト差替え対応)。"""
    if path is None:
        path = _ENV_PATH
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            out.append(raw)
            continue
        key = stripped.split('=', 1)[0].strip()
        if key in updates:
            out.append(f'{key}={updates[key]}')
            seen.add(key)
        else:
            out.append(raw)
    for k, v in updates.items():
        if k not in seen:
            out.append(f'{k}={v}')
    # 末尾改行保持
    path.write_text('\n'.join(out) + '\n', encoding='utf-8')


def _get_expires_at(env: Optional[dict] = None) -> int:
    """EBAY_USER_TOKEN_EXPIRES_AT (unix epoch sec) を返す。未設定なら 0。"""
    env = env or _load_env_dict()
    raw = env.get('EBAY_USER_TOKEN_EXPIRES_AT') or os.environ.get('EBAY_USER_TOKEN_EXPIRES_AT') or '0'
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def is_token_near_expiry(threshold_sec: int = _REFRESH_THRESHOLD_SEC) -> bool:
    """現 Access Token が閾値以下の有効時間しか残っていないなら True。
    EXPIRES_AT が未設定 (初回) の場合も True を返し、refresh を促す。"""
    expires_at = _get_expires_at()
    if expires_at <= 0:
        return True
    now = int(time.time())
    return (expires_at - now) < threshold_sec


def refresh_access_token(
    config: Optional[dict] = None,
    force: bool = False,
) -> dict:
    """Refresh Token を使って Access Token を更新する。

    Args:
        config: schedule_config.json 相当の dict (credentials fallback 用)
        force: True なら有効期限チェックを skip して必ず refresh

    Returns:
        {
            'success': bool,
            'access_token': str|None,  # 更新成功時の新 token
            'expires_in': int,          # 秒数
            'expires_at': int,          # unix epoch
            'errors': list[str],
        }
    """
    with _refresh_lock:
        env = _load_env_dict()
        # 他スレッドが既に refresh して短時間で連打された場合のデディーラス (skip)
        if not force and not is_token_near_expiry():
            return {
                'success': True,
                'access_token': env.get('EBAY_USER_TOKEN'),
                'expires_in': max(0, _get_expires_at(env) - int(time.time())),
                'expires_at': _get_expires_at(env),
                'errors': [],
                'skipped': True,
            }

        # credentials を .env 優先で取得
        app_id = (env.get('EBAY_APP_ID') or os.environ.get('EBAY_APP_ID') or '').strip()
        cert_id = (env.get('EBAY_CERT_ID') or os.environ.get('EBAY_CERT_ID') or '').strip()
        refresh_token = (
            env.get('EBAY_REFRESH_TOKEN') or os.environ.get('EBAY_REFRESH_TOKEN') or ''
        ).strip()
        if config and isinstance(config, dict):
            ebay_cfg = config.get('ebay') or {}
            app_id = app_id or str(ebay_cfg.get('app_id') or '')
            cert_id = cert_id or str(ebay_cfg.get('cert_id') or '')
            refresh_token = refresh_token or str(ebay_cfg.get('refresh_token') or '')

        missing: list[str] = []
        if not app_id:
            missing.append('EBAY_APP_ID')
        if not cert_id:
            missing.append('EBAY_CERT_ID')
        if not refresh_token:
            missing.append('EBAY_REFRESH_TOKEN')
        if missing:
            return {
                'success': False, 'access_token': None,
                'expires_in': 0, 'expires_at': 0,
                'errors': [f'missing credentials: {", ".join(missing)}'],
            }

        auth_str = f'{app_id}:{cert_id}'
        auth_b64 = base64.b64encode(auth_str.encode()).decode()

        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(
                    _TOKEN_ENDPOINT,
                    headers={
                        'Authorization': f'Basic {auth_b64}',
                        'Content-Type': 'application/x-www-form-urlencoded',
                    },
                    data={
                        'grant_type': 'refresh_token',
                        'refresh_token': refresh_token,
                        'scope': _SCOPES,
                    },
                )
        except httpx.HTTPError as e:
            logger.exception('Refresh token request failed')
            return {
                'success': False, 'access_token': None,
                'expires_in': 0, 'expires_at': 0,
                'errors': [f'http error: {e}'],
            }

        if resp.status_code != 200:
            try:
                body = resp.json()
            except (ValueError, TypeError):
                body = {'raw': resp.text[:500]}
            err_detail = body.get('error_description') or body.get('error') or str(body)[:300]
            logger.error(
                f'Refresh token failed status={resp.status_code}: {err_detail}'
            )
            return {
                'success': False, 'access_token': None,
                'expires_in': 0, 'expires_at': 0,
                'errors': [f'refresh failed ({resp.status_code}): {err_detail}'],
            }

        try:
            data = resp.json()
        except ValueError:
            return {
                'success': False, 'access_token': None,
                'expires_in': 0, 'expires_at': 0,
                'errors': ['refresh response JSON parse error'],
            }

        new_access_token = data.get('access_token') or ''
        expires_in = int(data.get('expires_in') or 0)
        if not new_access_token or expires_in <= 0:
            return {
                'success': False, 'access_token': None,
                'expires_in': expires_in, 'expires_at': 0,
                'errors': ['refresh response missing access_token or expires_in'],
            }

        expires_at = int(time.time()) + expires_in
        # .env と process env を両方更新
        _write_env_values({
            'EBAY_USER_TOKEN': new_access_token,
            'EBAY_USER_TOKEN_EXPIRES_AT': str(expires_at),
        })
        os.environ['EBAY_USER_TOKEN'] = new_access_token
        os.environ['EBAY_USER_TOKEN_EXPIRES_AT'] = str(expires_at)
        logger.info(
            f'Access Token refreshed: expires_in={expires_in}s '
            f'(at={expires_at}), token_len={len(new_access_token)}'
        )
        return {
            'success': True,
            'access_token': new_access_token,
            'expires_in': expires_in,
            'expires_at': expires_at,
            'errors': [],
        }


def get_valid_access_token(
    config: Optional[dict] = None,
    threshold_sec: int = _REFRESH_THRESHOLD_SEC,
) -> Optional[str]:
    """API 呼出し直前に使う便利関数。残り有効時間が閾値以下なら自動 refresh。

    Returns:
        Access Token 文字列 (refresh 失敗時は None)。
    """
    if is_token_near_expiry(threshold_sec=threshold_sec):
        res = refresh_access_token(config=config, force=False)
        if not res['success']:
            logger.warning(
                f'Auto-refresh failed, falling back to current token: {res["errors"]}'
            )
            # fallback: 現 .env の token (期限切れかもしれない)
            env = _load_env_dict()
            return env.get('EBAY_USER_TOKEN') or os.environ.get('EBAY_USER_TOKEN') or None
        return res['access_token']
    # 有効期限十分 → 既存 token をそのまま返す
    env = _load_env_dict()
    return env.get('EBAY_USER_TOKEN') or os.environ.get('EBAY_USER_TOKEN') or None


def record_initial_expires_at(expires_in: int) -> None:
    """初回 Consent 直後に呼ぶヘルパ。expires_in 秒後を EXPIRES_AT として保存。"""
    expires_at = int(time.time()) + int(expires_in or 0)
    _write_env_values({'EBAY_USER_TOKEN_EXPIRES_AT': str(expires_at)})
    os.environ['EBAY_USER_TOKEN_EXPIRES_AT'] = str(expires_at)
    logger.info(f'Initial expires_at recorded: {expires_at}')


if __name__ == '__main__':
    # 手動テスト: python -m monitor.ebay_oauth_refresh [--force]
    logging.basicConfig(level=logging.INFO)
    import json
    force = '--force' in sys.argv
    cfg_path = Path(__file__).resolve().parent.parent / 'config' / 'schedule_config.json'
    cfg = json.loads(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
    now = int(time.time())
    expires_at = _get_expires_at()
    print(f'Current time: {now}')
    print(f'EXPIRES_AT: {expires_at} (remaining: {expires_at - now}s)')
    print(f'Near expiry: {is_token_near_expiry()}')
    print(f'Calling refresh (force={force})...')
    r = refresh_access_token(config=cfg, force=force)
    # access_token は長いので表示制限
    safe = {**r}
    if safe.get('access_token'):
        safe['access_token'] = safe['access_token'][:40] + '...(truncated)'
    print(json.dumps(safe, indent=2, ensure_ascii=False))
