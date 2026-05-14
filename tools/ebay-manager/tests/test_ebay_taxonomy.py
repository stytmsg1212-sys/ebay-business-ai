"""monitor.ebay_taxonomy の単体試験.

実 eBay Taxonomy API を叩かずに httpx.Client.request を mock してテストする。
category_tree_id 取得 / category_suggestions パース / キャッシュ挙動 / エラーフォールバック。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor import ebay_taxonomy as mod  # noqa: E402


def _mk_resp(status_code: int, json_body):
    class FakeResp:
        def __init__(self):
            self.status_code = status_code
            self.text = json.dumps(json_body) if json_body is not None else ''
        def json(self):
            return json_body
    return FakeResp()


@pytest.fixture
def token_mock(monkeypatch):
    monkeypatch.setattr(
        'monitor.ebay_oauth_refresh.get_valid_access_token',
        lambda *a, **kw: 'v^1.1#VALID',
    )
    return 'v^1.1#VALID'


@pytest.fixture
def tmp_cache(monkeypatch, tmp_path):
    cache_file = tmp_path / 'taxonomy_cache.json'
    monkeypatch.setattr(mod, '_TREE_ID_CACHE_FILE', cache_file)
    monkeypatch.setattr(mod, '_CACHE_DIR', tmp_path)
    return cache_file


class TestGetDefaultCategoryTreeId:
    def test_fresh_fetch_caches_tree_id(self, token_mock, tmp_cache):
        fake = _mk_resp(200, {
            'categoryTreeId': '0',
            'categoryTreeVersion': '133',
        })
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            tid = mod.get_default_category_tree_id()
        assert tid == '0'
        # キャッシュが書き出されている
        assert tmp_cache.exists()
        cached = json.loads(tmp_cache.read_text(encoding='utf-8'))
        assert cached['tree_id'] == '0'

    def test_cache_hit_within_ttl(self, token_mock, tmp_cache):
        tmp_cache.write_text(json.dumps({
            'marketplace': 'EBAY_US',
            'tree_id': 'CACHED_999',
            'fetched_at': int(time.time()),
        }), encoding='utf-8')
        # API が呼ばれていないことを確認
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            tid = mod.get_default_category_tree_id()
            MockClient.assert_not_called()
        assert tid == 'CACHED_999'

    def test_cache_expired_refetches(self, token_mock, tmp_cache):
        expired_at = int(time.time()) - (mod._TREE_ID_CACHE_TTL_SEC + 10)
        tmp_cache.write_text(json.dumps({
            'marketplace': 'EBAY_US',
            'tree_id': 'OLD',
            'fetched_at': expired_at,
        }), encoding='utf-8')
        fake = _mk_resp(200, {'categoryTreeId': 'NEW_0'})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            tid = mod.get_default_category_tree_id()
        assert tid == 'NEW_0'

    def test_api_failure_returns_none(self, token_mock, tmp_cache):
        fake = _mk_resp(500, {'errors': [{'errorId': 1, 'message': 'Internal'}]})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            tid = mod.get_default_category_tree_id(use_cache=False)
        assert tid is None

    def test_no_token_returns_none(self, tmp_cache, monkeypatch):
        monkeypatch.setattr(
            'monitor.ebay_oauth_refresh.get_valid_access_token',
            lambda *a, **kw: None,
        )
        tid = mod.get_default_category_tree_id(use_cache=False)
        assert tid is None


class TestGetCategorySuggestions:
    def test_parses_multiple_suggestions(self, token_mock, monkeypatch):
        # tree_id 取得を短絡
        monkeypatch.setattr(mod, 'get_default_category_tree_id', lambda **kw: '0')

        fake_suggestions = {
            'categorySuggestions': [
                {
                    'category': {
                        'categoryId': '14990',
                        'categoryName': 'Home Speakers & Subwoofers',
                    },
                    'categoryTreeNodeAncestors': [
                        {'categoryId': '14969', 'categoryName': 'Home Audio'},
                        {'categoryId': '32852', 'categoryName': 'TV, Video & Home Audio'},
                        {'categoryId': '293', 'categoryName': 'Consumer Electronics'},
                    ],
                    'categoryTreeNodeLevel': 4,
                },
                {
                    'category': {
                        'categoryId': '111694',
                        'categoryName': 'Audio Docks & Mini Speakers',
                    },
                    'categoryTreeNodeAncestors': [],
                    'categoryTreeNodeLevel': 3,
                },
            ],
        }
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = \
                _mk_resp(200, fake_suggestions)
            res = mod.get_category_suggestions('OPSODIS 1 speaker', config={})
        assert len(res) == 2
        assert res[0]['category_id'] == '14990'
        assert res[0]['category_name'] == 'Home Speakers & Subwoofers'
        assert res[0]['is_leaf'] is True
        assert '293' in res[0]['ancestors']
        assert res[1]['category_id'] == '111694'

    def test_empty_query_returns_empty(self, token_mock):
        assert mod.get_category_suggestions('', config={}) == []
        assert mod.get_category_suggestions('   ', config={}) == []

    def test_missing_tree_id_returns_empty(self, token_mock, monkeypatch):
        monkeypatch.setattr(mod, 'get_default_category_tree_id', lambda **kw: None)
        res = mod.get_category_suggestions('anything', config={})
        assert res == []

    def test_api_failure_returns_empty(self, token_mock, monkeypatch):
        monkeypatch.setattr(mod, 'get_default_category_tree_id', lambda **kw: '0')
        fake = _mk_resp(500, {'errors': [{'errorId': 9, 'message': 'oops'}]})
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = fake
            res = mod.get_category_suggestions('X', config={})
        assert res == []

    def test_filters_empty_category_ids(self, token_mock, monkeypatch):
        monkeypatch.setattr(mod, 'get_default_category_tree_id', lambda **kw: '0')
        fake_body = {
            'categorySuggestions': [
                {'category': {'categoryId': '', 'categoryName': 'empty'},
                 'categoryTreeNodeAncestors': []},
                {'category': {'categoryId': '14990', 'categoryName': 'Real'},
                 'categoryTreeNodeAncestors': []},
            ],
        }
        with mock.patch.object(mod.httpx, 'Client') as MockClient:
            MockClient.return_value.__enter__.return_value.request.return_value = \
                _mk_resp(200, fake_body)
            res = mod.get_category_suggestions('X', config={})
        assert len(res) == 1
        assert res[0]['category_id'] == '14990'


class TestPickBestCategory:
    def test_first_suggestion_returned_by_default(self, token_mock, monkeypatch):
        monkeypatch.setattr(mod, 'get_category_suggestions', lambda q, **kw: [
            {'category_id': '14990', 'category_name': 'Home Speakers',
             'ancestors_names': ['Home Audio', 'Consumer Electronics'],
             'ancestors': ['14969', '293'], 'is_leaf': True,
             'category_tree_node_level': 4},
            {'category_id': '47091', 'category_name': 'Pro Speakers',
             'ancestors_names': ['Pro Audio Equipment', 'Musical Instruments'],
             'ancestors': ['180014', '619'], 'is_leaf': True,
             'category_tree_node_level': 3},
        ])
        res = mod.pick_best_category('anything')
        assert res['category_id'] == '14990'

    def test_preferred_root_prioritized(self, token_mock, monkeypatch):
        monkeypatch.setattr(mod, 'get_category_suggestions', lambda q, **kw: [
            {'category_id': '14990', 'category_name': 'Home Speakers',
             'ancestors_names': ['Home Audio', 'Consumer Electronics'],
             'ancestors': ['14969', '293'], 'is_leaf': True,
             'category_tree_node_level': 4},
            {'category_id': '47091', 'category_name': 'Pro Speakers',
             'ancestors_names': ['Pro Audio Equipment', 'Musical Instruments & Gear'],
             'ancestors': ['180014', '619'], 'is_leaf': True,
             'category_tree_node_level': 3},
        ])
        res = mod.pick_best_category(
            'anything', preferred_root='Musical Instruments & Gear',
        )
        assert res['category_id'] == '47091'

    def test_no_suggestions_returns_none(self, token_mock, monkeypatch):
        monkeypatch.setattr(mod, 'get_category_suggestions', lambda q, **kw: [])
        assert mod.pick_best_category('anything') is None
