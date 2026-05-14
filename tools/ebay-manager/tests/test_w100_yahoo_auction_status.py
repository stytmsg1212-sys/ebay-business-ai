"""W100 Phase 2: yahoo_auction_status helper の単体テスト.

検証対象:
- 終了済 + 落札あり (has_winner=True、24h 猶予不要)
- 終了済 + 落札なし (has_winner=False、24h 猶予対象)
- 進行中 (is_ended=False、24h 猶予対象外)
- HTTP エラー / JSON 取得失敗 (raw_error あり、判定不能)
- end_time の JST → UTC 変換正確性
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from monitor.yahoo_auction_status import (
    YahooEndStatus, fetch_yahoo_end_status,
    _extract_yahoo_item, _parse_end_time,
)


def _build_html(item_dict: dict) -> str:
    """__NEXT_DATA__ 含む HTML を組み立てる (test fixture).

    実機の HTML は <script>{...}</script> が 1 行 (改行なし) で出力されるので、
    fixture も改行なしの 1 行にしないと正規表現がマッチしない.
    """
    import json as _json
    next_data = {
        "props": {
            "pageProps": {
                "initialState": {
                    "item": {
                        "detail": {
                            "item": item_dict
                        }
                    }
                }
            }
        }
    }
    payload = _json.dumps(next_data, ensure_ascii=False)
    return (
        f'<html><head><script id="__NEXT_DATA__" type="application/json">'
        f'{payload}</script></head><body></body></html>'
    )


# ─────────────────────────────────
# _parse_end_time
# ─────────────────────────────────

def test_parse_end_time_jst_to_utc():
    """ISO 8601 JST → UTC 変換 (9 時間引く)"""
    result = _parse_end_time("2025-09-17T22:07:18+09:00")
    assert result is not None
    assert result == datetime(2025, 9, 17, 13, 7, 18, tzinfo=timezone.utc)


def test_parse_end_time_no_offset_assumed_jst():
    """offset なし = JST 仮定"""
    result = _parse_end_time("2025-09-17T22:07:18")
    assert result is not None
    # JST 22:07 = UTC 13:07
    assert result == datetime(2025, 9, 17, 13, 7, 18, tzinfo=timezone.utc)


def test_parse_end_time_invalid():
    assert _parse_end_time(None) is None
    assert _parse_end_time("") is None
    assert _parse_end_time("invalid") is None
    assert _parse_end_time(12345) is None  # not str


# ─────────────────────────────────
# _extract_yahoo_item
# ─────────────────────────────────

def test_extract_yahoo_item_success():
    html = _build_html({"auctionId": "abc123", "status": "closed", "bids": 3})
    result = _extract_yahoo_item(html)
    assert result == {"auctionId": "abc123", "status": "closed", "bids": 3}


def test_extract_yahoo_item_no_next_data():
    assert _extract_yahoo_item("<html>no script</html>") is None


def test_extract_yahoo_item_invalid_json():
    html = '<script id="__NEXT_DATA__" type="application/json">{not valid}</script>'
    assert _extract_yahoo_item(html) is None


# ─────────────────────────────────
# fetch_yahoo_end_status (HTTP mock)
# ─────────────────────────────────

@patch("monitor.yahoo_auction_status.httpx.get")
def test_fetch_ended_with_winner(mock_get):
    """終了済 + 落札あり (has_winner=True)"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = _build_html({
        "status": "closed", "bids": 3, "biddersNum": 2,
        "endTime": "2025-09-17T22:07:18+09:00"
    })
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    r = fetch_yahoo_end_status("https://page.auctions.yahoo.co.jp/jp/auction/test")
    assert r.is_ended is True
    assert r.has_winner is True
    assert r.end_time_utc == datetime(2025, 9, 17, 13, 7, 18, tzinfo=timezone.utc)
    assert r.raw_error is None


@patch("monitor.yahoo_auction_status.httpx.get")
def test_fetch_ended_no_winner(mock_get):
    """終了済 + 落札なし (has_winner=False、24h 猶予対象)"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = _build_html({
        "status": "closed", "bids": 0, "biddersNum": 0,
        "endTime": "2025-10-01T20:00:00+09:00"
    })
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    r = fetch_yahoo_end_status("https://page.auctions.yahoo.co.jp/jp/auction/test")
    assert r.is_ended is True
    assert r.has_winner is False  # ← 24h 猶予対象
    assert r.end_time_utc == datetime(2025, 10, 1, 11, 0, 0, tzinfo=timezone.utc)


@patch("monitor.yahoo_auction_status.httpx.get")
def test_fetch_in_progress(mock_get):
    """進行中 (is_ended=False、24h 猶予対象外)"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = _build_html({
        "status": "open", "bids": 0,
        "endTime": "2025-12-31T23:59:59+09:00"
    })
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    r = fetch_yahoo_end_status("https://page.auctions.yahoo.co.jp/jp/auction/test")
    assert r.is_ended is False
    assert r.has_winner is None  # 進行中は判定無関係


@patch("monitor.yahoo_auction_status.httpx.get")
def test_fetch_http_error(mock_get):
    """HTTP エラー → raw_error 付き判定不能"""
    import httpx
    mock_get.side_effect = httpx.ConnectError("connection refused")

    r = fetch_yahoo_end_status("https://page.auctions.yahoo.co.jp/jp/auction/test")
    assert r.is_ended is False
    assert r.has_winner is None
    assert r.end_time_utc is None
    assert r.raw_error is not None
    assert "ConnectError" in r.raw_error


@patch("monitor.yahoo_auction_status.httpx.get")
def test_fetch_no_next_data(mock_get):
    """JSON 取得失敗 → raw_error 付き"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html>NO __NEXT_DATA__</html>"
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    r = fetch_yahoo_end_status("https://page.auctions.yahoo.co.jp/jp/auction/test")
    assert r.raw_error == "next_data_not_found"


@patch("monitor.yahoo_auction_status.httpx.get")
def test_fetch_only_bidders_num(mock_get):
    """bids=0 だが biddersNum>0 (補助フィールド) → has_winner=True"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = _build_html({
        "status": "closed", "bids": 0, "biddersNum": 1,
        "endTime": "2025-09-17T22:07:18+09:00"
    })
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    r = fetch_yahoo_end_status("https://page.auctions.yahoo.co.jp/jp/auction/test")
    assert r.is_ended is True
    assert r.has_winner is True
