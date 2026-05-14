#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eBay API 認証情報ローダ。

優先順位:
  1. 環境変数 (.env ロード済を想定)
  2. schedule_config.json の ebay セクション（後方互換）
  3. None (キーが一切無ければ空文字)

.env に以下を設定:
  EBAY_APP_ID=...
  EBAY_DEV_ID=...
  EBAY_CERT_ID=...
  EBAY_USER_TOKEN=...

移行方針: schedule_config.json の ebay 値は段階的に空にし、最終的に .env 専用にする。
Git に平文コミットされるリスクを排除するのが目的。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    # monitor/ 配下から2つ上の .env を想定（tools/ebay-manager/.env）
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    # python-dotenv が無い環境でも環境変数が直接セットされていれば動く
    pass


_ENV_KEY_MAP = {
    "app_id": "EBAY_APP_ID",
    "dev_id": "EBAY_DEV_ID",
    "cert_id": "EBAY_CERT_ID",
    "user_token": "EBAY_USER_TOKEN",
}


def get_ebay_credentials(config: Optional[dict] = None) -> dict:
    """eBay 認証4値を取得。env→config の順に解決。

    Returns: {app_id, dev_id, cert_id, user_token} のdict。
             未設定項目は空文字列 ""（呼び出し側で all() チェックする想定）
    """
    config_ebay = (config or {}).get("ebay") or {}
    result = {}
    for key, env_name in _ENV_KEY_MAP.items():
        result[key] = os.environ.get(env_name) or config_ebay.get(key) or ""
    return result


def ebay_credentials_ok(creds: dict) -> bool:
    return all(creds.get(k) for k in _ENV_KEY_MAP.keys())
