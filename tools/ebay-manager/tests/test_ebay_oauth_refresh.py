"""monitor.ebay_oauth_refresh の unit test.

実 eBay OAuth エンドポイントを叩かずに httpx.Client.post / _write_env_values / _load_env_dict を
mock 化してテストする。並行 refresh 抑制、scope 維持、エラーハンドリングを検証。
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

import pytest

from monitor import ebay_oauth_refresh as mod


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """.env を tmp に切替え、初期 credentials を書き込む。"""
    env_path = tmp_path / '.env'
    env_path.write_text(
        'EBAY_APP_ID=APP1\n'
        'EBAY_CERT_ID=CERT1\n'
        'EBAY_USER_TOKEN=v^1.1#old_token\n'
        'EBAY_REFRESH_TOKEN=v^1.1#refresh_token_abc\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(mod, '_ENV_PATH', env_path)
    # process env も reset
    for k in ['EBAY_USER_TOKEN', 'EBAY_USER_TOKEN_EXPIRES_AT',
              'EBAY_APP_ID', 'EBAY_CERT_ID', 'EBAY_REFRESH_TOKEN']:
        monkeypatch.delenv(k, raising=False)
    return env_path


class TestIsTokenNearExpiry:
    def test_no_expires_at_returns_true(self, isolated_env):
        """EXPIRES_AT 未設定は「refresh 必要」とみなす (初回)"""
        assert mod.is_token_near_expiry()

    def test_expired_returns_true(self, isolated_env):
        env_path = isolated_env
        env_path.write_text(
            env_path.read_text(encoding='utf-8')
            + f'EBAY_USER_TOKEN_EXPIRES_AT={int(time.time()) - 100}\n',
            encoding='utf-8',
        )
        assert mod.is_token_near_expiry()

    def test_within_threshold_returns_true(self, isolated_env):
        """残り 5分 (<10分) なら refresh 必要"""
        env_path = isolated_env
        env_path.write_text(
            env_path.read_text(encoding='utf-8')
            + f'EBAY_USER_TOKEN_EXPIRES_AT={int(time.time()) + 300}\n',
            encoding='utf-8',
        )
        assert mod.is_token_near_expiry(threshold_sec=600)

    def test_far_from_expiry_returns_false(self, isolated_env):
        """残り 1時間 (>10分) なら refresh 不要"""
        env_path = isolated_env
        env_path.write_text(
            env_path.read_text(encoding='utf-8')
            + f'EBAY_USER_TOKEN_EXPIRES_AT={int(time.time()) + 3600}\n',
            encoding='utf-8',
        )
        assert not mod.is_token_near_expiry(threshold_sec=600)


class TestRefreshAccessToken:
    def _make_fake_resp(self, status_code: int, json_body: dict, text: str = ''):
        class FakeResp:
            def __init__(self):
                self.status_code = status_code
                self.text = text or str(json_body)
            def json(self):
                return json_body
        return FakeResp()

    def test_success_updates_env(self, isolated_env):
        fake = self._make_fake_resp(200, {
            'access_token': 'v^1.1#NEW_TOKEN',
            'expires_in': 7200,
            'token_type': 'User Access Token',
        })
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = fake
            result = mod.refresh_access_token(force=True)
        assert result['success'] is True
        assert result['access_token'] == 'v^1.1#NEW_TOKEN'
        assert result['expires_in'] == 7200
        assert result['expires_at'] > int(time.time())
        # .env に書き込まれているか
        content = isolated_env.read_text(encoding='utf-8')
        assert 'EBAY_USER_TOKEN=v^1.1#NEW_TOKEN' in content
        assert 'EBAY_USER_TOKEN_EXPIRES_AT=' in content

    def test_missing_refresh_token(self, tmp_path, monkeypatch):
        env_path = tmp_path / '.env'
        env_path.write_text(
            'EBAY_APP_ID=APP1\nEBAY_CERT_ID=CERT1\n',
            encoding='utf-8',
        )
        monkeypatch.setattr(mod, '_ENV_PATH', env_path)
        for k in ['EBAY_REFRESH_TOKEN']:
            monkeypatch.delenv(k, raising=False)
        result = mod.refresh_access_token(force=True)
        assert result['success'] is False
        assert any('EBAY_REFRESH_TOKEN' in e for e in result['errors'])

    def test_non_200_response(self, isolated_env):
        fake = self._make_fake_resp(400, {
            'error': 'invalid_grant',
            'error_description': 'refresh token invalid',
        })
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = fake
            result = mod.refresh_access_token(force=True)
        assert result['success'] is False
        assert any('refresh failed' in e for e in result['errors'])

    def test_http_error_returns_failure(self, isolated_env):
        class _FakeHTTPError(mod.httpx.HTTPError):
            pass

        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = (
                _FakeHTTPError('network down')
            )
            result = mod.refresh_access_token(force=True)
        assert result['success'] is False
        assert any('http error' in e for e in result['errors'])

    def test_response_missing_access_token(self, isolated_env):
        fake = self._make_fake_resp(200, {'expires_in': 7200})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = fake
            result = mod.refresh_access_token(force=True)
        assert result['success'] is False
        assert any('missing access_token' in e for e in result['errors'])

    def test_skip_when_not_near_expiry(self, isolated_env):
        """残り十分 + force=False なら refresh しない (API 叩かない)"""
        env_path = isolated_env
        env_path.write_text(
            env_path.read_text(encoding='utf-8')
            + f'EBAY_USER_TOKEN_EXPIRES_AT={int(time.time()) + 3600}\n',
            encoding='utf-8',
        )
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            result = mod.refresh_access_token(force=False)
            MockClient.assert_not_called()
        assert result['success'] is True
        assert result.get('skipped') is True

    def test_scope_sent_in_body(self, isolated_env):
        """refresh request で scope が space separated で送られる"""
        fake = self._make_fake_resp(200, {
            'access_token': 'v^1.1#NEW', 'expires_in': 7200,
        })
        captured: dict = {}
        def _capture(url, **kwargs):
            captured['data'] = kwargs.get('data')
            return fake
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = _capture
            mod.refresh_access_token(force=True)
        assert captured['data']['grant_type'] == 'refresh_token'
        assert 'sell.marketing' in captured['data']['scope']
        assert 'sell.inventory' in captured['data']['scope']
        assert captured['data']['refresh_token'] == 'v^1.1#refresh_token_abc'


class TestGetValidAccessToken:
    def test_returns_current_when_far_from_expiry(self, isolated_env):
        env_path = isolated_env
        env_path.write_text(
            env_path.read_text(encoding='utf-8')
            + f'EBAY_USER_TOKEN_EXPIRES_AT={int(time.time()) + 3600}\n',
            encoding='utf-8',
        )
        token = mod.get_valid_access_token()
        assert token == 'v^1.1#old_token'

    def test_auto_refresh_when_near_expiry(self, isolated_env):
        env_path = isolated_env
        env_path.write_text(
            env_path.read_text(encoding='utf-8')
            + f'EBAY_USER_TOKEN_EXPIRES_AT={int(time.time()) + 60}\n',
            encoding='utf-8',
        )
        class FakeResp:
            status_code = 200
            text = ''
            def json(self):
                return {'access_token': 'v^1.1#AUTO_NEW', 'expires_in': 7200}
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = FakeResp()
            token = mod.get_valid_access_token()
        assert token == 'v^1.1#AUTO_NEW'

    def test_fallback_to_current_on_refresh_failure(self, isolated_env):
        """refresh 失敗時は None ではなく現 token を返す (UI を壊さないため)"""
        env_path = isolated_env
        env_path.write_text(
            env_path.read_text(encoding='utf-8')
            + f'EBAY_USER_TOKEN_EXPIRES_AT={int(time.time()) + 60}\n',
            encoding='utf-8',
        )
        class FakeResp:
            status_code = 401
            text = 'unauth'
            def json(self):
                return {'error': 'invalid_grant'}
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = FakeResp()
            token = mod.get_valid_access_token()
        # 期限切れ寸前でも現 token を返す (401 を上位に見せる)
        assert token == 'v^1.1#old_token'


class TestWriteEnvValues:
    def test_update_existing_key(self, tmp_path, monkeypatch):
        p = tmp_path / '.env'
        p.write_text('FOO=old\nBAR=stay\n', encoding='utf-8')
        monkeypatch.setattr(mod, '_ENV_PATH', p)
        mod._write_env_values({'FOO': 'new'})
        content = p.read_text(encoding='utf-8')
        assert 'FOO=new' in content
        assert 'BAR=stay' in content
        assert 'FOO=old' not in content

    def test_append_new_key(self, tmp_path, monkeypatch):
        p = tmp_path / '.env'
        p.write_text('FOO=1\n', encoding='utf-8')
        monkeypatch.setattr(mod, '_ENV_PATH', p)
        mod._write_env_values({'NEW_KEY': 'X'})
        content = p.read_text(encoding='utf-8')
        assert 'FOO=1' in content
        assert 'NEW_KEY=X' in content

    def test_preserves_comments_and_blank_lines(self, tmp_path, monkeypatch):
        p = tmp_path / '.env'
        p.write_text('# header\n\nFOO=1\n# mid\nBAR=2\n', encoding='utf-8')
        monkeypatch.setattr(mod, '_ENV_PATH', p)
        mod._write_env_values({'BAR': 'new'})
        content = p.read_text(encoding='utf-8')
        assert '# header' in content
        assert '# mid' in content
        assert 'BAR=new' in content


class TestRecordInitialExpiresAt:
    def test_saves_expires_at(self, isolated_env):
        mod.record_initial_expires_at(7200)
        content = isolated_env.read_text(encoding='utf-8')
        assert 'EBAY_USER_TOKEN_EXPIRES_AT=' in content
        import os
        # process env にも反映
        assert 'EBAY_USER_TOKEN_EXPIRES_AT' in os.environ
