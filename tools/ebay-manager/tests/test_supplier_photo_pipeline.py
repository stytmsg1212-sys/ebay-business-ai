"""W115 H-2/H-4: supplier 専用 photo pipeline 単体テスト.

対象:
  - fetch_supplier_image_url: og:image meta tag 抽出 (Mercari/Yahoo/PayPay 共通)
  - _upload_eps_and_revise: EPS upload + ReviseItem 連携、H-4 orphan handling

streamlit 依存の UI render 関数 (render_supplier_photo_apply_section) は integration
test 領域 (Q1 DoD Phase 2 で Streamlit + Playwright で検証、本 unit test 範囲外).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# =====================================================================
# fetch_supplier_image_url (H-2)
# =====================================================================

def _make_html(og_image_url: str) -> str:
    return f'''<html><head>
<meta property="og:image" content="{og_image_url}">
<title>test</title>
</head></html>'''


@patch("httpx.Client")
def test_fetch_supplier_image_url_extracts_og_image(mock_client_cls):
    from tabs._supplier_photo_pipeline import fetch_supplier_image_url

    mock_resp = MagicMock()
    mock_resp.text = _make_html("https://static.mercdn.net/item/m12345/photo.jpg")
    mock_resp.raise_for_status = MagicMock()

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_ctx.get = MagicMock(return_value=mock_resp)
    mock_client_cls.return_value = mock_ctx

    url = fetch_supplier_image_url("https://jp.mercari.com/item/m12345")
    assert url == "https://static.mercdn.net/item/m12345/photo.jpg"


@patch("httpx.Client")
def test_fetch_supplier_image_url_returns_none_when_no_og_image(mock_client_cls):
    from tabs._supplier_photo_pipeline import fetch_supplier_image_url

    mock_resp = MagicMock()
    mock_resp.text = "<html><head><title>no og</title></head></html>"
    mock_resp.raise_for_status = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_ctx.get = MagicMock(return_value=mock_resp)
    mock_client_cls.return_value = mock_ctx

    url = fetch_supplier_image_url("https://example.com/no-og")
    assert url is None


def test_fetch_supplier_image_url_invalid_url_returns_none():
    from tabs._supplier_photo_pipeline import fetch_supplier_image_url
    assert fetch_supplier_image_url("") is None
    assert fetch_supplier_image_url("not-a-url") is None


@patch("httpx.Client")
def test_fetch_supplier_image_url_handles_protocol_relative(mock_client_cls):
    from tabs._supplier_photo_pipeline import fetch_supplier_image_url

    mock_resp = MagicMock()
    mock_resp.text = _make_html("//static.mercdn.net/photo.jpg")  # //... 形式
    mock_resp.raise_for_status = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_ctx.get = MagicMock(return_value=mock_resp)
    mock_client_cls.return_value = mock_ctx

    url = fetch_supplier_image_url("https://jp.mercari.com/item/m12345")
    assert url == "https://static.mercdn.net/photo.jpg"


# =====================================================================
# _upload_eps_and_revise (H-4 orphan handling)
# =====================================================================

@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    'app_id': 'app', 'dev_id': 'dev', 'cert_id': 'cert', 'user_token': 'tok',
})
def test_upload_eps_and_revise_success_path(mock_creds, mock_ok, mock_revise, mock_upload):
    from tabs._supplier_photo_pipeline import _upload_eps_and_revise
    from monitor.ebay_eps_uploader import EpsUploadResult

    mock_upload.return_value = [EpsUploadResult(
        success=True, eps_url="https://i.ebayimg.com/abc.jpg", error=None,
    )]
    mock_revise.return_value = {'success': True, 'message': 'OK', 'picture_urls': []}

    result = _upload_eps_and_revise(
        candidate_id=42, ebay_item_id="123456789012",
        hero_local_path="data/hero_candidates/sup_42/hero_W001.png",
    )
    assert result['success'] is True
    assert result['eps_url'] == "https://i.ebayimg.com/abc.jpg"
    mock_upload.assert_called_once()
    mock_revise.assert_called_once()


@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    'app_id': 'app', 'dev_id': 'dev', 'cert_id': 'cert', 'user_token': 'tok',
})
def test_upload_eps_and_revise_eps_failure_no_revise_called(
    mock_creds, mock_ok, mock_revise, mock_upload,
):
    """EPS upload 失敗 → ReviseItem 呼ばない (orphan 防止)."""
    from tabs._supplier_photo_pipeline import _upload_eps_and_revise
    from monitor.ebay_eps_uploader import EpsUploadResult

    mock_upload.return_value = [EpsUploadResult(
        success=False, eps_url=None, error="Photoroom file too large",
    )]

    result = _upload_eps_and_revise(
        candidate_id=42, ebay_item_id="123456789012",
        hero_local_path="data/sup_42/hero.png",
    )
    assert result['success'] is False
    assert "EPS upload failed" in result['message']
    assert result['eps_url'] is None
    mock_revise.assert_not_called()  # H-4: revise を呼ばない (二次的 orphan 回避)


@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    'app_id': 'app', 'dev_id': 'dev', 'cert_id': 'cert', 'user_token': 'tok',
})
def test_upload_eps_and_revise_revise_failure_surfaces_eps_url(
    mock_creds, mock_ok, mock_revise, mock_upload,
):
    """H-4 主シナリオ: EPS 成功 + ReviseItem 失敗 → eps_url を返却 (cache hit 再試行用)."""
    from tabs._supplier_photo_pipeline import _upload_eps_and_revise
    from monitor.ebay_eps_uploader import EpsUploadResult

    mock_upload.return_value = [EpsUploadResult(
        success=True, eps_url="https://i.ebayimg.com/orphan.jpg", error=None,
    )]
    mock_revise.return_value = {
        'success': False, 'message': 'Picture URL invalid', 'picture_urls': [],
    }

    result = _upload_eps_and_revise(
        candidate_id=42, ebay_item_id="123456789012",
        hero_local_path="data/sup_42/hero.png",
    )
    assert result['success'] is False
    assert result['eps_url'] == "https://i.ebayimg.com/orphan.jpg"
    assert "EPS upload OK" in result['message']
    assert "ReviseItem 失敗" in result['message']
    assert "cache hit" in result['message']  # 再試行ガイド有り


def test_upload_eps_and_revise_no_credentials_returns_failure():
    from tabs._supplier_photo_pipeline import _upload_eps_and_revise
    with patch("monitor.credentials.ebay_credentials_ok", return_value=False):
        result = _upload_eps_and_revise(
            candidate_id=42, ebay_item_id="123456789012",
            hero_local_path="data/sup_42/hero.png",
        )
    assert result['success'] is False
    assert "credentials" in result['message']
    assert result['eps_url'] is None
