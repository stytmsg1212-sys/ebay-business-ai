"""ebay_lister._resolve_credentials の OAuth 自動 refresh 試験 (2026-04-22 追加).

Trading API (VerifyAdd / AddItem) の user_token が期限切れ直前のとき、
ebay_oauth_refresh.get_valid_access_token を経由して自動 refresh されることを確認する。
従来は stale token でそのまま API call して「Auth token is hard expired」エラーで
VerifyAdd が落ちていた重大バグの修正を固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor import ebay_lister  # noqa: E402


class TestResolveCredentialsOAuthRefresh:
    def test_all_explicit_oauth_not_near_expiry_no_refresh(self, monkeypatch):
        """全引数明示 + OAuth 形式 + 期限十分 → refresh 呼ばれない。"""
        called = {'refresh': 0}

        def _fake_near_expiry():
            return False

        def _fake_get_valid(config=None):
            called['refresh'] += 1
            return 'v^1.1#REFRESHED'

        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.is_token_near_expiry', _fake_near_expiry,
        )
        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token', _fake_get_valid,
        )
        creds = ebay_lister._resolve_credentials(
            app_id='A', dev_id='D', cert_id='C',
            user_token='v^1.1#ORIGINAL',
            config=None,
        )
        assert creds['user_token'] == 'v^1.1#ORIGINAL'
        assert called['refresh'] == 0

    def test_all_explicit_oauth_near_expiry_triggers_refresh(self, monkeypatch):
        """全引数明示でも期限寸前なら refresh される。"""
        called = {'refresh': 0}

        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.is_token_near_expiry', lambda: True,
        )

        def _fake_get_valid(config=None):
            called['refresh'] += 1
            return 'v^1.1#FRESH_AUTO'

        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token', _fake_get_valid,
        )
        creds = ebay_lister._resolve_credentials(
            app_id='A', dev_id='D', cert_id='C',
            user_token='v^1.1#STALE',
            config=None,
        )
        assert creds['user_token'] == 'v^1.1#FRESH_AUTO'
        assert called['refresh'] == 1

    def test_non_oauth_token_skips_refresh(self, monkeypatch):
        """Auth'n'Auth 形式 (AgAAAA~) は refresh 対象外。Trading API で直接使う。"""
        called = {'refresh': 0}

        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.is_token_near_expiry',
            lambda: (_ for _ in ()).throw(AssertionError(
                'is_token_near_expiry should not be called for non-OAuth token'
            )),
        )

        def _fake_get_valid(config=None):
            called['refresh'] += 1
            return 'should not be used'

        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token', _fake_get_valid,
        )
        creds = ebay_lister._resolve_credentials(
            app_id='A', dev_id='D', cert_id='C',
            user_token='AgAAAA**LEGACY',
            config=None,
        )
        assert creds['user_token'] == 'AgAAAA**LEGACY'
        assert called['refresh'] == 0

    def test_fallback_from_env_auto_refresh(self, monkeypatch):
        """引数空 → env から fallback 取得時も OAuth 形式なら auto-refresh される。"""
        monkeypatch.setattr(
            'monitor.ebay_lister.get_ebay_credentials',
            lambda cfg: {
                'app_id': 'ENV_APP', 'dev_id': 'ENV_DEV',
                'cert_id': 'ENV_CERT', 'user_token': 'v^1.1#STALE_FROM_ENV',
            },
        )
        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token',
            lambda config=None: 'v^1.1#FRESH_AUTO_ENV',
        )
        creds = ebay_lister._resolve_credentials(
            app_id='', dev_id='', cert_id='', user_token='',
            config=None,
        )
        assert creds['user_token'] == 'v^1.1#FRESH_AUTO_ENV'
        assert creds['app_id'] == 'ENV_APP'

    def test_fallback_from_env_non_oauth_preserved(self, monkeypatch):
        """env fallback で非 OAuth token が返ってきた場合は touch しない。"""
        monkeypatch.setattr(
            'monitor.ebay_lister.get_ebay_credentials',
            lambda cfg: {
                'app_id': 'A', 'dev_id': 'D',
                'cert_id': 'C', 'user_token': 'AgAAAA**LEGACY_ENV',
            },
        )

        def _should_not_be_called(config=None):
            raise AssertionError('get_valid_access_token must not be called for non-OAuth')

        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token',
            _should_not_be_called,
        )
        creds = ebay_lister._resolve_credentials(
            '', '', '', '', None,
        )
        assert creds['user_token'] == 'AgAAAA**LEGACY_ENV'

    def test_refresh_failure_falls_back_to_original(self, monkeypatch):
        """auto-refresh 例外時は元の token を使って API 呼出しに進む (UI 死守)。"""
        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.is_token_near_expiry',
            lambda: True,
        )

        def _boom(config=None):
            raise RuntimeError('network down')

        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token', _boom,
        )
        creds = ebay_lister._resolve_credentials(
            app_id='A', dev_id='D', cert_id='C',
            user_token='v^1.1#ORIGINAL',
            config=None,
        )
        # 落ちずに元の token で返る (上位で 401 catch できる)
        assert creds['user_token'] == 'v^1.1#ORIGINAL'

    def test_refresh_returns_none_falls_back_to_original(self, monkeypatch):
        """refresh が None を返した場合も元 token をそのまま使う。"""
        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.is_token_near_expiry',
            lambda: True,
        )
        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token',
            lambda config=None: None,
        )
        creds = ebay_lister._resolve_credentials(
            'A', 'D', 'C', 'v^1.1#ORIGINAL', None,
        )
        assert creds['user_token'] == 'v^1.1#ORIGINAL'
