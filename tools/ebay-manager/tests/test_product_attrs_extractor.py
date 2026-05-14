"""monitor.product_attrs_extractor の単体試験.

Anthropic API は mock 化、JSON parse / サニタイズ / 範囲チェックのみ検証。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor import product_attrs_extractor as mod  # noqa: E402


def _make_claude_response(payload: dict) -> MagicMock:
    """Claude messages.create の戻り値を模す。"""
    block = MagicMock()
    block.type = 'text'
    block.text = json.dumps(payload, ensure_ascii=False)
    msg = MagicMock()
    msg.content = [block]
    msg.usage = MagicMock(input_tokens=0, output_tokens=0,
                          cache_creation_input_tokens=0,
                          cache_read_input_tokens=0)
    return msg


class TestExtractProductAttrs:
    def test_short_description_skipped(self):
        """30 字未満は API を呼ばず空結果"""
        result = mod.extract_product_attrs('短い')
        assert result['weight_g'] is None
        assert result['includes_list'] == []

    def test_empty_none_skipped(self):
        assert mod.extract_product_attrs('')['weight_g'] is None
        assert mod.extract_product_attrs(None)['includes_list'] == []

    def test_no_api_key_returns_empty(self, monkeypatch):
        """ANTHROPIC_API_KEY 未設定時は空結果"""
        monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
        result = mod.extract_product_attrs('x' * 50)
        assert result['weight_g'] is None
        assert result['includes_list'] == []

    def test_full_extraction_success(self, monkeypatch):
        """典型的な成功ケース"""
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _make_claude_response({
            'weight_g': 1500,
            'length_mm': 300, 'width_mm': 200, 'depth_mm': 100,
            'includes_list': ['リモコン', '取扱説明書'],
        })
        with patch.object(mod, '_get_client', return_value=fake_client):
            with patch.object(mod, 'log_anthropic_response', create=True,
                              return_value=None):
                with patch('monitor.api_logger.log_anthropic_response',
                           return_value=None):
                    result = mod.extract_product_attrs(
                        '本体重量約 1.5kg、サイズ 30×20×10cm。リモコン、取扱説明書付属。',
                    )
        assert result['weight_g'] == 1500
        assert result['length_mm'] == 300
        assert result['width_mm'] == 200
        assert result['depth_mm'] == 100
        assert result['includes_list'] == ['リモコン', '取扱説明書']

    def test_weight_out_of_range_clamped_to_none(self, monkeypatch):
        """weight_g が範囲外 (100kg 超) なら None"""
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _make_claude_response({
            'weight_g': 99999999,  # 異常値
            'length_mm': None, 'width_mm': None, 'depth_mm': None,
            'includes_list': [],
        })
        with patch.object(mod, '_get_client', return_value=fake_client):
            with patch('monitor.api_logger.log_anthropic_response',
                       return_value=None):
                result = mod.extract_product_attrs('x' * 100)
        assert result['weight_g'] is None  # clamp → None

    def test_dimension_out_of_range_clamped(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _make_claude_response({
            'weight_g': 1000,
            'length_mm': 99999,  # > 5000mm (5m) 範囲外
            'width_mm': 200, 'depth_mm': 100,
            'includes_list': [],
        })
        with patch.object(mod, '_get_client', return_value=fake_client):
            with patch('monitor.api_logger.log_anthropic_response',
                       return_value=None):
                result = mod.extract_product_attrs('x' * 100)
        assert result['length_mm'] is None  # clamp
        assert result['width_mm'] == 200
        assert result['depth_mm'] == 100

    def test_includes_list_truncated_and_sanitized(self, monkeypatch):
        """長すぎる item は 60 字で truncate、配列は 20 個まで"""
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _make_claude_response({
            'weight_g': None,
            'length_mm': None, 'width_mm': None, 'depth_mm': None,
            'includes_list': (
                ['a' * 100]  # 100 chars → 60 chars
                + [f'item{i}' for i in range(30)]  # 30 items → 20 max
            ),
        })
        with patch.object(mod, '_get_client', return_value=fake_client):
            with patch('monitor.api_logger.log_anthropic_response',
                       return_value=None):
                result = mod.extract_product_attrs('x' * 100)
        assert len(result['includes_list']) <= 20
        assert all(len(x) <= 60 for x in result['includes_list'])

    def test_json_parse_failure_returns_empty(self, monkeypatch):
        """Claude が無効な JSON を返した場合"""
        fake_client = MagicMock()
        block = MagicMock()
        block.type = 'text'
        block.text = 'not a json at all'
        msg = MagicMock()
        msg.content = [block]
        msg.usage = MagicMock(input_tokens=0, output_tokens=0,
                              cache_creation_input_tokens=0,
                              cache_read_input_tokens=0)
        fake_client.messages.create.return_value = msg
        with patch.object(mod, '_get_client', return_value=fake_client):
            with patch('monitor.api_logger.log_anthropic_response',
                       return_value=None):
                result = mod.extract_product_attrs('x' * 100)
        assert result['weight_g'] is None
        assert result['includes_list'] == []

    def test_api_exception_returns_empty(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = Exception('network down')
        with patch.object(mod, '_get_client', return_value=fake_client):
            result = mod.extract_product_attrs('x' * 100)
        assert result['weight_g'] is None
        assert result['includes_list'] == []

    def test_null_values_preserved(self, monkeypatch):
        """全て null の正常応答もサポート"""
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _make_claude_response({
            'weight_g': None,
            'length_mm': None, 'width_mm': None, 'depth_mm': None,
            'includes_list': [],
        })
        with patch.object(mod, '_get_client', return_value=fake_client):
            with patch('monitor.api_logger.log_anthropic_response',
                       return_value=None):
                result = mod.extract_product_attrs('スピーカーです。' * 10)
        assert all(v is None for k, v in result.items()
                   if k != 'includes_list')
        assert result['includes_list'] == []


class TestExtractJson:
    def test_fenced(self):
        assert mod._extract_json('```json\n{"a":1}\n```') == '{"a":1}'

    def test_greedy(self):
        assert mod._extract_json('prefix {"a":1} suffix') == '{"a":1}'

    def test_none(self):
        assert mod._extract_json('') is None
        assert mod._extract_json('no json here') is None
