#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gmail OAuth 再認可スクリプト.

Gmail の refresh token が revoke / expire された場合にユーザーが手動実行する。
ローカル web server を立ち上げてブラウザで Google 認証を完了させ、
`config/gmail_token.json` を再生成する。

使い方:
    python -m scripts.gmail_reauth

    または: python scripts/gmail_reauth.py

前提:
    - `config/credentials.json` (Google OAuth client secret) が配置済み
    - Gmail API が Google Cloud Console で有効化済み
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    cred_file = project_root / 'config' / 'credentials.json'
    # 2026-04-24 W14 CRITICAL C-1: Gmail token を OneDrive 同期外に保存
    sys.path.insert(0, str(project_root))
    from monitor.secure_paths import get_gmail_token_path
    token_file = get_gmail_token_path()
    schedule_cfg_file = project_root / 'config' / 'schedule_config.json'

    if not cred_file.exists():
        print(f'ERROR: {cred_file} が見つかりません。'
              f'Google Cloud Console から OAuth client secret をダウンロードして '
              f'配置してください。', file=sys.stderr)
        return 1

    # scopes を config から読む (schedule_config.json)
    scopes = ['https://www.googleapis.com/auth/gmail.readonly']
    if schedule_cfg_file.exists():
        try:
            cfg = json.loads(schedule_cfg_file.read_text(encoding='utf-8'))
            scopes = cfg.get('gmail', {}).get('scopes', scopes)
        except (json.JSONDecodeError, OSError) as e:
            print(f'WARN: schedule_config.json 読込失敗、デフォルト scope 使用: {e}')

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print('ERROR: google-auth-oauthlib が未インストールです。'
              'pip install google-auth-oauthlib', file=sys.stderr)
        return 1

    # 旧 token があればバックアップしてから削除
    if token_file.exists():
        backup = token_file.with_suffix('.json.bak')
        try:
            backup.write_bytes(token_file.read_bytes())
            print(f'旧 token をバックアップ: {backup}')
        except OSError as e:
            print(f'WARN: backup 失敗 (続行): {e}')
        token_file.unlink()
        print(f'旧 token を削除: {token_file}')

    print(f'Gmail OAuth 再認可を開始します...')
    print(f'credentials: {cred_file}')
    print(f'scopes: {scopes}')
    print('ブラウザが自動で開きます。Google アカウントでログイン+同意してください。')

    flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), scopes)
    creds = flow.run_local_server(port=0)

    token_file.parent.mkdir(parents=True, exist_ok=True)
    with open(token_file, 'w', encoding='utf-8') as f:
        f.write(creds.to_json())
    print(f'新 token を保存: {token_file}')
    print('完了。次回の scheduler (02:30 など) で正常にメール取得が再開します。')
    print('即時で取得したい場合: python -m tasks.task_email_pickup')
    return 0


if __name__ == '__main__':
    sys.exit(main())
