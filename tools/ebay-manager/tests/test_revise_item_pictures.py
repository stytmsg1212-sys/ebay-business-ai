"""W115 H-1: ReviseItem PictureDetails 関数の単体テスト.

検証観点:
  T1. XML が PictureURL 配列を含んで組立てられる (escape あり)
  T2. picture_urls 空リスト → success=False (Q0: silent skip 防止)
  T3. 12 件超 → success=False (eBay silent drop 防止)
  T4. http:// URL → success=False (HTTPS 強制、eBay 仕様)
  T5. Trading API Success → success=True + 件数正報告
  T6. Trading API Ack=Failure → success=False + LongMessage 抽出
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.ebay_client import (  # noqa: E402
    _build_revise_item_pictures_xml,
    revise_item_pictures,
)


_DUMMY_CREDS = dict(
    app_id="app", dev_id="dev", cert_id="cert", user_token="USERTOKEN",
)


# T1
def test_xml_contains_all_picture_urls_with_escape():
    urls = [
        "https://i.ebayimg.com/foo&bar.jpg",  # & は XML escape 対象
        "https://i.ebayimg.com/normal.jpg",
    ]
    xml = _build_revise_item_pictures_xml("123456789012", urls)
    assert "<ItemID>123456789012</ItemID>" in xml
    assert "<PictureURL>https://i.ebayimg.com/foo&amp;bar.jpg</PictureURL>" in xml
    assert "<PictureURL>https://i.ebayimg.com/normal.jpg</PictureURL>" in xml
    assert xml.count("<PictureURL>") == 2


# T2
def test_empty_picture_urls_returns_failure():
    result = revise_item_pictures("123456789012", [], **_DUMMY_CREDS)
    assert result["success"] is False
    assert "empty" in result["message"]


# T3
def test_picture_urls_over_12_returns_failure():
    urls = [f"https://i.ebayimg.com/p{i}.jpg" for i in range(13)]
    result = revise_item_pictures("123456789012", urls, **_DUMMY_CREDS)
    assert result["success"] is False
    assert "12" in result["message"]
    assert "13" in result["message"]


# T4
def test_http_url_rejected():
    urls = ["http://insecure.example.com/p.jpg"]
    result = revise_item_pictures("123456789012", urls, **_DUMMY_CREDS)
    assert result["success"] is False
    assert "https" in result["message"]


# T5
@patch("monitor.ebay_client.httpx.post")
@patch("monitor.ebay_client._resolve_active_token", return_value="USERTOKEN")
def test_trading_api_success_returns_success(mock_token, mock_post):
    mock_resp = MagicMock()
    mock_resp.text = """<?xml version="1.0"?>
<ReviseItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
</ReviseItemResponse>"""
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    urls = [
        "https://i.ebayimg.com/p1.jpg",
        "https://i.ebayimg.com/p2.jpg",
    ]
    result = revise_item_pictures("123456789012", urls, **_DUMMY_CREDS)
    assert result["success"] is True
    assert "2 件" in result["message"]
    assert result["picture_urls"] == urls


# T6
@patch("monitor.ebay_client.httpx.post")
@patch("monitor.ebay_client._resolve_active_token", return_value="USERTOKEN")
def test_trading_api_failure_extracts_long_message(mock_token, mock_post):
    mock_resp = MagicMock()
    mock_resp.text = """<?xml version="1.0"?>
<ReviseItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors>
    <LongMessage>Picture URL is invalid format.</LongMessage>
  </Errors>
</ReviseItemResponse>"""
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    urls = ["https://i.ebayimg.com/p1.jpg"]
    result = revise_item_pictures("123456789012", urls, **_DUMMY_CREDS)
    assert result["success"] is False
    assert "Picture URL is invalid format" in result["message"]
