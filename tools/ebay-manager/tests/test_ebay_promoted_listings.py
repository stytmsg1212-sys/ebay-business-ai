"""monitor.ebay_promoted_listings の unit test.

実 Marketing API を叩かずに httpx + get_valid_access_token を mock 化してテストする。
キャンペーン一覧取得 / 作成 / listing 追加 / enroll_new_listing 統合を検証。
"""
from __future__ import annotations

from unittest import mock

import pytest

from monitor import ebay_promoted_listings as mod


@pytest.fixture
def token_mock(monkeypatch):
    """get_valid_access_token を常に成功で返す mock (lazy import 経路を考慮)。"""
    # ebay_promoted_listings._get_oauth_token は関数内で lazy import するので
    # 元モジュールの関数を置き換える
    monkeypatch.setattr(
        'monitor.ebay_oauth_refresh.get_valid_access_token',
        lambda *a, **kw: 'v^1.1#VALID_TOKEN',
    )
    # Legacy fallback 経路も短絡
    monkeypatch.setattr(
        'monitor.ebay_promoted_listings.get_ebay_credentials',
        lambda cfg: {'user_token': 'v^1.1#VALID_TOKEN',
                     'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C'},
    )
    return 'v^1.1#VALID_TOKEN'


def _mk_resp(status_code: int, json_body):
    class FakeResp:
        def __init__(self):
            self.status_code = status_code
            self.text = str(json_body)
        def json(self):
            return json_body
    return FakeResp()


class TestGetOauthToken:
    def test_token_prefix_validated(self, monkeypatch):
        """OAuth token は v^ で始まる"""
        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token',
            lambda *a, **kw: 'v^1.1#OK',
        )
        token = mod._get_oauth_token({})
        assert token == 'v^1.1#OK'

    def test_non_oauth_token_rejected(self, monkeypatch):
        """Auth'n'Auth 形式 (AgAAAA~) は明示的に拒否"""
        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token',
            lambda *a, **kw: 'AgAAAA**',
        )
        # Legacy fallback 経路も同じ値を返すようにして確実に拒否判定に入る
        monkeypatch.setattr(
            'monitor.ebay_promoted_listings.get_ebay_credentials',
            lambda cfg: {'user_token': 'AgAAAA**'},
        )
        token = mod._get_oauth_token({})
        assert token is None

    def test_refresh_failure_fallback(self, monkeypatch):
        """get_valid_access_token が例外を出しても legacy 取得で救う"""
        def _boom(*a, **kw):
            raise RuntimeError('boom')
        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token', _boom,
        )
        monkeypatch.setattr(
            'monitor.ebay_promoted_listings.get_ebay_credentials',
            lambda cfg: {'user_token': 'v^1.1#LEGACY'},
        )
        token = mod._get_oauth_token({})
        assert token == 'v^1.1#LEGACY'


class TestListCampaigns:
    def test_success(self, token_mock):
        fake = _mk_resp(200, {
            'campaigns': [
                {'campaignId': '100', 'campaignName': 'Camp A',
                 'campaignStatus': 'RUNNING'},
                {'campaignId': '200', 'campaignName': 'Camp B',
                 'campaignStatus': 'RUNNING'},
            ],
            'total': 2,
        })
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            r = mod.list_campaigns(config={})
        assert r['success']
        assert len(r['campaigns']) == 2
        assert r['campaigns'][0]['campaignId'] == '100'

    def test_non_200_error(self, token_mock):
        fake = _mk_resp(403, {
            'errors': [{'errorId': 1100, 'message': 'Access denied'}],
        })
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            r = mod.list_campaigns(config={})
        assert not r['success']
        assert any('1100' in e for e in r['errors'])

    def test_token_missing(self, monkeypatch):
        """token 取得失敗時は空結果で返す"""
        # _get_oauth_token は ebay_oauth_refresh.get_valid_access_token を lazy import
        # するので、元モジュール側でパッチする必要がある
        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token',
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            'monitor.ebay_promoted_listings.get_ebay_credentials',
            lambda cfg: {'user_token': ''},
        )
        r = mod.list_campaigns(config={})
        assert not r['success']


class TestCreateCampaign:
    def test_success_via_data(self, token_mock):
        fake = _mk_resp(201, {'campaignId': '999'})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            r = mod.create_campaign('My Camp', bid_percentage=2.0, config={})
        assert r['success']
        assert r['campaign_id'] == '999'

    def test_failure(self, token_mock):
        fake = _mk_resp(400, {
            'errors': [{'errorId': 3, 'message': 'Invalid body'}],
        })
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            r = mod.create_campaign('Bad', bid_percentage=99.0, config={})
        assert not r['success']
        assert r['campaign_id'] is None


class TestAddListingToCampaign:
    def test_success(self, token_mock):
        fake = _mk_resp(201, {'adId': 'AD_7777'})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            r = mod.add_listing_to_campaign(
                campaign_id='100',
                ebay_item_id='358467369123',
                bid_percentage=2.0,
                config={},
            )
        assert r['success']
        assert r['ad_id'] == 'AD_7777'

    def test_missing_campaign_id(self, token_mock):
        r = mod.add_listing_to_campaign(
            campaign_id='', ebay_item_id='123', config={},
        )
        assert not r['success']
        assert any('campaign_id' in e for e in r['errors'])

    def test_missing_item_id(self, token_mock):
        r = mod.add_listing_to_campaign(
            campaign_id='100', ebay_item_id='', config={},
        )
        assert not r['success']
        assert any('ebay_item_id' in e for e in r['errors'])

    def test_bid_percentage_in_body(self, token_mock):
        captured: dict = {}
        def _capture(method, url, **kwargs):
            captured['body'] = kwargs.get('json')
            return _mk_resp(201, {'adId': 'x'})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.side_effect = _capture
            mod.add_listing_to_campaign('100', '999', bid_percentage=2.5, config={})
        assert captured['body']['bidPercentage'] == '2.5'
        assert captured['body']['listingId'] == '999'


class TestEnsureDefaultCampaign:
    def test_returns_existing_from_config(self, token_mock):
        """config に campaign_id が既に設定済みなら API を叩かずに返す"""
        config = {'ebay_promoted_listings': {'campaign_id': '777'}}
        r = mod.ensure_default_campaign(config)
        assert r['success']
        assert r['campaign_id'] == '777'
        assert not r['created']

    def test_searches_existing_campaigns_by_name(self, token_mock):
        config = {'ebay_promoted_listings': {}}
        fake_list = _mk_resp(200, {
            'campaigns': [
                {'campaignId': 'C1', 'campaignName': 'OTHER'},
                {'campaignId': 'C2', 'campaignName': 'MonoHonpo General Promoted 2%'},
            ],
        })
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake_list
            r = mod.ensure_default_campaign(config)
        assert r['success']
        assert r['campaign_id'] == 'C2'
        assert not r['created']

    def test_creates_new_campaign_when_absent(self, token_mock):
        config = {'ebay_promoted_listings': {}}
        call_seq = []
        def _seq(method, url, **kwargs):
            call_seq.append((method, url))
            if 'campaign_status=RUNNING' in url and method == 'GET':
                return _mk_resp(200, {'campaigns': []})
            # POST /ad_campaign
            return _mk_resp(201, {'campaignId': 'BRAND_NEW_999'})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.side_effect = _seq
            r = mod.ensure_default_campaign(config)
        assert r['success']
        assert r['campaign_id'] == 'BRAND_NEW_999'
        assert r['created']


class TestEnrollNewListing:
    def test_full_success(self, token_mock):
        config = {'ebay_promoted_listings': {
            'enabled': True, 'auto_enroll_new_listings': True,
            'campaign_id': '100', 'bid_percentage': 2.0,
        }}
        fake = _mk_resp(201, {'adId': 'AD_X'})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            r = mod.enroll_new_listing('358467369123', config)
        assert r['success']
        assert r['ad_id'] == 'AD_X'
        assert not r['skipped']

    def test_disabled_skips(self, token_mock):
        config = {'ebay_promoted_listings': {'enabled': False}}
        r = mod.enroll_new_listing('123', config)
        assert r['success']
        assert r['skipped']

    def test_auto_enroll_off_skips(self, token_mock):
        config = {'ebay_promoted_listings': {
            'enabled': True, 'auto_enroll_new_listings': False,
        }}
        r = mod.enroll_new_listing('123', config)
        assert r['success']
        assert r['skipped']

    def test_campaign_ensure_failure(self, token_mock):
        config = {'ebay_promoted_listings': {
            'enabled': True, 'auto_enroll_new_listings': True,
            'campaign_id': '',  # 無いので search → 作成が走る
        }}
        def _fail(method, url, **kwargs):
            # list は空で返すが create で失敗
            if method == 'GET':
                return _mk_resp(200, {'campaigns': []})
            return _mk_resp(400, {'errors': [{'errorId': 1, 'message': 'bad'}]})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.side_effect = _fail
            r = mod.enroll_new_listing('123', config)
        assert not r['success']
        assert 'campaign 確保失敗' in r['message']

    def test_add_listing_failure_preserves_campaign_id(self, token_mock):
        config = {'ebay_promoted_listings': {
            'enabled': True, 'auto_enroll_new_listings': True,
            'campaign_id': '100', 'bid_percentage': 2.0,
        }}
        fake = _mk_resp(400, {'errors': [{'errorId': 9, 'message': 'dup'}]})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            r = mod.enroll_new_listing('123', config)
        assert not r['success']
        assert r['campaign_id'] == '100'
        assert r['ad_id'] is None
        assert not r['skipped']


class TestApiCall:
    def test_marketplace_header_attached(self, token_mock):
        captured: dict = {}
        def _capture(method, url, **kwargs):
            captured['headers'] = kwargs.get('headers') or {}
            return _mk_resp(200, {})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.side_effect = _capture
            mod._api_call('GET', '/test', config={})
        assert captured['headers'].get('X-EBAY-C-MARKETPLACE-ID') == 'EBAY_US'
        assert captured['headers'].get('Authorization', '').startswith('Bearer ')
