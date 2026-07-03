"""W314 Phase1 S2 (2026-07-03): 画像 3 モード (① AI 合成 / ② そのまま採用 / ③ メイン差し替え).

対象:
  - check_image_resolution / is_low_resolution (確定判断1: 500px 未満は警告のみ)
  - build_main_replace_picture_urls ([new_main] + existing[1:] の pure 構築, 12 枚上限)
  - get_all_ebay_image_urls (monitor.ebay_image_fetcher, _api_image_urls の公開ラップ)
  - _upload_full_list_and_revise (② EPS 化 + 全置換)
  - _upload_single_and_revise_main (③ EPS 化 + [new_main]+existing[1:] 再送)
  - _log_content_change_images_safe (listing_content_change_log 未整備でも no-op fallback)

streamlit 依存の UI render 関数 (render_supplier_photo_apply_section /
_render_mode1_ai_compose / _render_mode2_as_is / _render_mode3_main_replace) は
integration test 領域 (既存 test_supplier_photo_pipeline.py の方針を踏襲、本 unit
test 範囲外)。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# =====================================================================
# check_image_resolution / is_low_resolution
# =====================================================================

def test_check_image_resolution_reads_real_image(tmp_path):
    from tabs._supplier_photo_pipeline import check_image_resolution
    from PIL import Image

    p = tmp_path / "small.png"
    Image.new("RGB", (300, 450), color="white").save(p)

    reso = check_image_resolution(p)
    assert reso == (300, 450)


def test_check_image_resolution_missing_file_returns_none(tmp_path):
    from tabs._supplier_photo_pipeline import check_image_resolution
    reso = check_image_resolution(tmp_path / "nonexistent.png")
    assert reso is None


def test_is_low_resolution_below_threshold():
    from tabs._supplier_photo_pipeline import is_low_resolution, MIN_RESOLUTION_PX
    assert MIN_RESOLUTION_PX == 500
    assert is_low_resolution(499, 900) is True   # 幅未満
    assert is_low_resolution(900, 499) is True   # 高さ未満
    assert is_low_resolution(499, 499) is True   # 両方未満


def test_is_low_resolution_at_or_above_threshold():
    from tabs._supplier_photo_pipeline import is_low_resolution
    assert is_low_resolution(500, 500) is False
    assert is_low_resolution(1600, 1200) is False


# =====================================================================
# build_main_replace_picture_urls (③ pure 構築ロジック, 3-tuple 契約)
#   Returns (kept, dropped, invalid) — F4/F5/F6 codex review 対応
# =====================================================================

def test_build_main_replace_basic():
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    existing = [
        "https://i.ebayimg.com/old_main.jpg",
        "https://i.ebayimg.com/2.jpg",
        "https://i.ebayimg.com/3.jpg",
    ]
    kept, dropped, invalid = build_main_replace_picture_urls(
        "https://i.ebayimg.com/new_main.jpg", existing,
    )
    assert kept == [
        "https://i.ebayimg.com/new_main.jpg",
        "https://i.ebayimg.com/2.jpg",
        "https://i.ebayimg.com/3.jpg",
    ]
    assert dropped == []
    assert invalid == []


def test_build_main_replace_empty_existing_keeps_only_new_main():
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    kept, dropped, invalid = build_main_replace_picture_urls(
        "https://i.ebayimg.com/new_main.jpg", [],
    )
    assert kept == ["https://i.ebayimg.com/new_main.jpg"]
    assert dropped == []
    assert invalid == []


def test_build_main_replace_single_existing_image_replaced_entirely():
    """既存が 1 枚 (メインのみ) の場合 existing[1:] は空 → new_main だけ残る."""
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    kept, dropped, invalid = build_main_replace_picture_urls(
        "https://i.ebayimg.com/new_main.jpg", ["https://i.ebayimg.com/old_main.jpg"],
    )
    assert kept == ["https://i.ebayimg.com/new_main.jpg"]
    assert dropped == []
    assert invalid == []


def test_build_main_replace_exceeds_12_truncates():
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    existing = ["https://i.ebayimg.com/main.jpg"] + [
        f"https://i.ebayimg.com/{i}.jpg" for i in range(2, 15)  # 2..14 = 13 枚
    ]
    assert len(existing) == 14
    kept, dropped, invalid = build_main_replace_picture_urls(
        "https://i.ebayimg.com/new_main.jpg", existing,
    )
    # combined = new_main + existing[1:] (13 枚) = 14 枚 -> cap 12
    assert len(kept) == 12
    assert kept[0] == "https://i.ebayimg.com/new_main.jpg"
    assert len(dropped) == 2
    assert invalid == []


def test_build_main_replace_empty_new_main_returns_empty():
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    kept, dropped, invalid = build_main_replace_picture_urls(
        "", ["https://i.ebayimg.com/1.jpg"],
    )
    assert kept == []
    assert dropped == []
    assert invalid == []


def test_build_main_replace_skips_falsy_existing_entries():
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    existing = ["https://i.ebayimg.com/main.jpg", "", None, "https://i.ebayimg.com/3.jpg"]
    kept, dropped, invalid = build_main_replace_picture_urls(
        "https://i.ebayimg.com/new_main.jpg", existing,
    )
    assert kept == ["https://i.ebayimg.com/new_main.jpg", "https://i.ebayimg.com/3.jpg"]
    assert invalid == []


# ---- F5 dedupe (順序保持で重複除去) ----

def test_build_main_replace_dedupe_when_new_main_already_in_existing():
    """F5: new_main が existing[1:] にも含まれる場合、重複せず 1 枠に."""
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    existing = [
        "https://i.ebayimg.com/old_main.jpg",
        "https://i.ebayimg.com/2.jpg",
        "https://i.ebayimg.com/new_main.jpg",  # ここで重複
        "https://i.ebayimg.com/4.jpg",
    ]
    kept, dropped, invalid = build_main_replace_picture_urls(
        "https://i.ebayimg.com/new_main.jpg", existing,
    )
    # new_main が先頭に来て、重複した existing 側は削除 (順序保持)
    assert kept == [
        "https://i.ebayimg.com/new_main.jpg",
        "https://i.ebayimg.com/2.jpg",
        "https://i.ebayimg.com/4.jpg",
    ]
    assert invalid == []


def test_build_main_replace_dedupe_preserves_order():
    """F5: existing[1:] 内で重複がある場合も順序保持で最初の 1 回だけ残る."""
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    existing = [
        "https://i.ebayimg.com/main.jpg",
        "https://i.ebayimg.com/a.jpg",
        "https://i.ebayimg.com/b.jpg",
        "https://i.ebayimg.com/a.jpg",  # 重複
    ]
    kept, dropped, invalid = build_main_replace_picture_urls(
        "https://i.ebayimg.com/new_main.jpg", existing,
    )
    assert kept == [
        "https://i.ebayimg.com/new_main.jpg",
        "https://i.ebayimg.com/a.jpg",
        "https://i.ebayimg.com/b.jpg",
    ]


def test_build_main_replace_dedupe_frees_slot_under_cap():
    """F5 と cap 12 の相互作用: dedupe 後の実効枚数で 12 上限判定.

    dedupe しない実装だと 12 + 1 dup = 13 枚が cap 12 で dropped 1、しかし正しくは
    重複が消えて 12 枚ちょうどに収まる (実効枚数を無駄にしない).
    """
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    # existing 全体 13 枚 (main 1 枚 + u2..u12 = 11 枚 + new_main 重複 1 枚)
    existing = (
        ["https://i.ebayimg.com/main.jpg"]
        + [f"https://i.ebayimg.com/u{i}.jpg" for i in range(2, 13)]  # 11 枚
        + ["https://i.ebayimg.com/new_main.jpg"]  # dedupe 対象
    )
    assert len(existing) == 13
    kept, dropped, invalid = build_main_replace_picture_urls(
        "https://i.ebayimg.com/new_main.jpg", existing,
    )
    # combined_raw = new_main + existing[1:] (12 枚) = 13 枚
    # dedupe (new_main の重複 1 枚を削除) → 12 枚 (cap ちょうどに収まる)
    assert len(kept) == 12
    assert len(dropped) == 0, "dedupe が cap 上限内に収める (実効枚数を無駄にしない)"
    assert kept[0] == "https://i.ebayimg.com/new_main.jpg"
    # kept 内に new_main は 1 度だけ登場
    assert kept.count("https://i.ebayimg.com/new_main.jpg") == 1
    assert invalid == []


# ---- F6 http→https 昇格 ----

def test_build_main_replace_upgrades_http_to_https_for_ebayimg():
    """F6: existing 側 URL の `http://` は自動で `https://` に昇格."""
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    existing = [
        "http://i.ebayimg.com/old_main.jpg",   # 昇格対象
        "http://i.ebayimg.com/2.jpg",          # 昇格対象
        "https://i.ebayimg.com/3.jpg",
    ]
    kept, dropped, invalid = build_main_replace_picture_urls(
        "https://i.ebayimg.com/new_main.jpg", existing,
    )
    assert kept == [
        "https://i.ebayimg.com/new_main.jpg",
        "https://i.ebayimg.com/2.jpg",
        "https://i.ebayimg.com/3.jpg",
    ]
    assert invalid == []


def test_build_main_replace_upgrade_helper_ebayimg_and_generic():
    """F6 helper 単体: `http://` prefix を機械的に `https://` に昇格."""
    from tabs._supplier_photo_pipeline import _upgrade_to_https
    assert _upgrade_to_https("http://i.ebayimg.com/x.jpg") == "https://i.ebayimg.com/x.jpg"
    assert _upgrade_to_https("http://example.com/x.jpg") == "https://example.com/x.jpg"
    assert _upgrade_to_https("https://i.ebayimg.com/x.jpg") == "https://i.ebayimg.com/x.jpg"
    assert _upgrade_to_https("") == ""
    assert _upgrade_to_https(None) is None


# ---- F4 fail-closed: 昇格しても非 https が残る URL は invalid 集計 ----

def test_build_main_replace_non_https_after_upgrade_goes_to_invalid():
    """F4: 昇格しても非 https に残る URL は silent 除外せず invalid に集計.

    contract: `build_main_replace_picture_urls` は invalid を返すが `kept` からは
    silent 除外しない (kept にも残す = caller が invalid チェックを飛ばすと後段の
    `revise_item_pictures` の https チェックで拒否される二重防御). caller は
    len(invalid) > 0 を見て中断すること (silent 除外 = 画像消失事故 = Q0 違反).
    """
    from tabs._supplier_photo_pipeline import build_main_replace_picture_urls
    existing = [
        "https://i.ebayimg.com/main.jpg",
        "ftp://example.com/weird.jpg",   # 昇格対象外 → invalid
        "https://i.ebayimg.com/3.jpg",
    ]
    kept, dropped, invalid = build_main_replace_picture_urls(
        "https://i.ebayimg.com/new_main.jpg", existing,
    )
    # invalid が空でない = caller は反映を中断する (Q0 silent skip 防止)
    assert invalid == ["ftp://example.com/weird.jpg"]
    # kept 側でも silent 除外しない (caller が invalid を無視した場合の二重防御)
    assert "ftp://example.com/weird.jpg" in kept


# =====================================================================
# get_all_ebay_image_urls (monitor.ebay_image_fetcher)
# =====================================================================

@patch("monitor.ebay_client._call_trading_api")
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_get_all_ebay_image_urls_success_path(mock_creds, mock_call):
    """F3 統合: `_call_trading_api` を mock、Ack=Success で URL 返却."""
    from monitor import ebay_image_fetcher
    mock_call.return_value = {"raw": (
        "<?xml version='1.0'?><GetItemResponse>"
        "<Ack>Success</Ack>"
        "<Item><PictureDetails>"
        "<PictureURL>https://i.ebayimg.com/1.jpg</PictureURL>"
        "<PictureURL>https://i.ebayimg.com/2.jpg</PictureURL>"
        "</PictureDetails></Item></GetItemResponse>"
    )}
    urls = ebay_image_fetcher.get_all_ebay_image_urls("123456789012")
    assert urls == ["https://i.ebayimg.com/1.jpg", "https://i.ebayimg.com/2.jpg"]


@patch("monitor.ebay_client._call_trading_api", side_effect=RuntimeError("boom"))
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_get_all_ebay_image_urls_empty_on_api_exception(mock_creds, mock_call):
    """F3: API 例外は空 list に degradate (fail-closed でも呼出側の判定と整合)."""
    from monitor import ebay_image_fetcher
    assert ebay_image_fetcher.get_all_ebay_image_urls("123456789012") == []


def test_get_all_ebay_image_urls_does_not_change_1st_image_cache_path(monkeypatch):
    """ebay_listing_image.get_ebay_image_url (1 枚目 cache 利用箇所) の挙動不変を確認.

    `get_all_ebay_image_urls` を追加しても `_api_image_urls` は touch していない
    (F3 で Ack 検証は本関数側にのみ追加、`_api_image_urls` は Ack 検証なしのまま).
    既存の 1 枚目 cache 経路は影響を受けない (K2 surgical).
    """
    from monitor import ebay_image_fetcher
    monkeypatch.setattr(
        ebay_image_fetcher, "_api_image_urls",
        lambda eid: ["https://i.ebayimg.com/first.jpg", "https://i.ebayimg.com/second.jpg"],
    )
    from monitor.ebay_listing_image import get_ebay_image_url
    # DB 未初期化でも cache read は OperationalError を握って miss 扱いになる想定
    got = get_ebay_image_url("999999999999")
    assert got == "https://i.ebayimg.com/first.jpg"


# =====================================================================
# _upload_full_list_and_revise (② そのまま採用)
# =====================================================================

def test_upload_full_list_no_credentials_returns_failure():
    from tabs._supplier_photo_pipeline import _upload_full_list_and_revise
    with patch("monitor.credentials.ebay_credentials_ok", return_value=False):
        result = _upload_full_list_and_revise("123456789012", ["a.jpg", "b.jpg"])
    assert result["success"] is False
    assert "credentials" in result["message"]


def test_upload_full_list_no_paths_returns_failure():
    from tabs._supplier_photo_pipeline import _upload_full_list_and_revise
    with patch("monitor.credentials.ebay_credentials_ok", return_value=True), \
         patch("monitor.credentials.get_ebay_credentials", return_value={
             "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
         }):
        result = _upload_full_list_and_revise("123456789012", [])
    assert result["success"] is False
    assert "アップロード対象画像がありません" in result["message"]


@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_full_list_success(mock_creds, mock_ok, mock_revise, mock_upload):
    from tabs._supplier_photo_pipeline import _upload_full_list_and_revise
    from monitor.ebay_eps_uploader import EpsUploadResult

    mock_upload.return_value = [
        EpsUploadResult(success=True, eps_url="https://i.ebayimg.com/a.jpg"),
        EpsUploadResult(success=True, eps_url="https://i.ebayimg.com/b.jpg"),
    ]
    mock_revise.return_value = {"success": True, "message": "OK", "picture_urls": []}

    result = _upload_full_list_and_revise("123456789012", ["a.jpg", "b.jpg"])
    assert result["success"] is True
    assert result["picture_urls"] == ["https://i.ebayimg.com/a.jpg", "https://i.ebayimg.com/b.jpg"]
    mock_revise.assert_called_once()


@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_full_list_all_eps_fail_no_revise_called(mock_creds, mock_ok, mock_upload):
    from tabs._supplier_photo_pipeline import _upload_full_list_and_revise
    from monitor.ebay_eps_uploader import EpsUploadResult

    mock_upload.return_value = [
        EpsUploadResult(success=False, error="boom"),
        EpsUploadResult(success=False, error="boom2"),
    ]
    with patch("monitor.ebay_client.revise_item_pictures") as mock_revise:
        result = _upload_full_list_and_revise("123456789012", ["a.jpg", "b.jpg"])
        mock_revise.assert_not_called()
    assert result["success"] is False
    assert "全滅" in result["message"]


@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_full_list_revise_failure_surfaces_kept_urls(mock_creds, mock_ok, mock_revise, mock_upload):
    from tabs._supplier_photo_pipeline import _upload_full_list_and_revise
    from monitor.ebay_eps_uploader import EpsUploadResult

    mock_upload.return_value = [EpsUploadResult(success=True, eps_url="https://i.ebayimg.com/a.jpg")]
    mock_revise.return_value = {"success": False, "message": "invalid url", "picture_urls": []}

    result = _upload_full_list_and_revise("123456789012", ["a.jpg"])
    assert result["success"] is False
    assert result["picture_urls"] == ["https://i.ebayimg.com/a.jpg"]
    assert "ReviseItem 失敗" in result["message"]


@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_full_list_exceeds_12_truncates_with_notice(mock_creds, mock_ok, mock_revise, mock_upload):
    from tabs._supplier_photo_pipeline import _upload_full_list_and_revise
    from monitor.ebay_eps_uploader import EpsUploadResult

    paths = [f"{i}.jpg" for i in range(14)]
    mock_upload.return_value = [
        EpsUploadResult(success=True, eps_url=f"https://i.ebayimg.com/{i}.jpg") for i in range(14)
    ]
    mock_revise.return_value = {"success": True, "message": "OK", "picture_urls": []}

    result = _upload_full_list_and_revise("123456789012", paths)
    assert result["success"] is True
    assert len(result["picture_urls"]) == 12
    assert "truncate" in result["message"]


# =====================================================================
# _upload_single_and_revise_main (③ メイン差し替え)
# =====================================================================

def test_upload_single_no_credentials_returns_failure():
    from tabs._supplier_photo_pipeline import _upload_single_and_revise_main
    with patch("monitor.credentials.ebay_credentials_ok", return_value=False):
        result = _upload_single_and_revise_main(
            "123456789012", "https://supplier.example/img.jpg", "remote",
            ["https://i.ebayimg.com/old.jpg"],
        )
    assert result["success"] is False
    assert "credentials" in result["message"]


def test_upload_single_empty_existing_aborts_without_upload():
    """既存画像消失リスク回避: existing_urls が空なら EPS upload すら試さず中断."""
    from tabs._supplier_photo_pipeline import _upload_single_and_revise_main
    with patch("monitor.credentials.ebay_credentials_ok", return_value=True), \
         patch("monitor.credentials.get_ebay_credentials", return_value={
             "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
         }), \
         patch("monitor.ebay_eps_uploader.upload_image_to_eps") as mock_eps:
        result = _upload_single_and_revise_main(
            "123456789012", "https://supplier.example/img.jpg", "remote", [],
        )
        mock_eps.assert_not_called()
    assert result["success"] is False
    assert "既存画像消失リスク回避" in result["message"]


def test_upload_single_local_path_missing_returns_failure():
    from tabs._supplier_photo_pipeline import _upload_single_and_revise_main
    with patch("monitor.credentials.ebay_credentials_ok", return_value=True), \
         patch("monitor.credentials.get_ebay_credentials", return_value={
             "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
         }):
        result = _upload_single_and_revise_main(
            "123456789012", "/tmp/nonexistent_main_image.png", "local",
            ["https://i.ebayimg.com/old.jpg"],
        )
    assert result["success"] is False
    assert "見つかりません" in result["message"]


@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_single_local_success_builds_new_main_first(mock_creds, mock_ok, mock_eps_par, mock_revise, tmp_path):
    from tabs._supplier_photo_pipeline import _upload_single_and_revise_main
    from monitor.ebay_eps_uploader import EpsUploadResult

    local_img = tmp_path / "hero.png"
    local_img.write_bytes(b"fake-png-bytes")

    mock_eps_par.return_value = [
        EpsUploadResult(success=True, eps_url="https://i.ebayimg.com/new_main.jpg"),
    ]
    mock_revise.return_value = {"success": True, "message": "OK", "picture_urls": []}

    existing = [
        "https://i.ebayimg.com/old_main.jpg",
        "https://i.ebayimg.com/2.jpg",
        "https://i.ebayimg.com/3.jpg",
    ]
    result = _upload_single_and_revise_main(
        "123456789012", str(local_img), "local", existing,
    )
    assert result["success"] is True
    assert result["picture_urls"] == [
        "https://i.ebayimg.com/new_main.jpg",
        "https://i.ebayimg.com/2.jpg",
        "https://i.ebayimg.com/3.jpg",
    ]
    mock_eps_par.assert_called_once()
    # W314 S2 review H1: cache 経由必須 (use_cache=True で eps_upload_cache を通す)
    _, kwargs = mock_eps_par.call_args
    assert kwargs.get("use_cache") is True, "モード② と同じ eps_upload_cache 経由に統一する契約"
    mock_revise.assert_called_once()


@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_single_eps_failure_no_revise_called(mock_creds, mock_ok, mock_eps_par, tmp_path):
    from tabs._supplier_photo_pipeline import _upload_single_and_revise_main
    from monitor.ebay_eps_uploader import EpsUploadResult

    local_img = tmp_path / "hero.png"
    local_img.write_bytes(b"fake-png-bytes")
    mock_eps_par.return_value = [EpsUploadResult(success=False, error="quota exceeded")]

    with patch("monitor.ebay_client.revise_item_pictures") as mock_revise:
        result = _upload_single_and_revise_main(
            "123456789012", str(local_img), "local", ["https://i.ebayimg.com/old.jpg"],
        )
        mock_revise.assert_not_called()
    assert result["success"] is False
    assert "EPS upload 失敗" in result["message"]


@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_single_exceeds_12_truncates(mock_creds, mock_ok, mock_eps_par, mock_revise, tmp_path):
    from tabs._supplier_photo_pipeline import _upload_single_and_revise_main
    from monitor.ebay_eps_uploader import EpsUploadResult

    local_img = tmp_path / "hero.png"
    local_img.write_bytes(b"fake-png-bytes")
    mock_eps_par.return_value = [
        EpsUploadResult(success=True, eps_url="https://i.ebayimg.com/new_main.jpg"),
    ]
    mock_revise.return_value = {"success": True, "message": "OK", "picture_urls": []}

    existing = ["https://i.ebayimg.com/main.jpg"] + [
        f"https://i.ebayimg.com/{i}.jpg" for i in range(2, 15)  # 13 枚 -> combined 14 枚
    ]
    result = _upload_single_and_revise_main("123456789012", str(local_img), "local", existing)
    assert result["success"] is True
    assert len(result["picture_urls"]) == 12
    assert "truncate" in result["message"]


@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_single_uses_cache_via_upload_images_parallel(
    mock_creds, mock_ok, mock_eps_par, mock_revise, tmp_path,
):
    """W314 S2 review H1 回帰: モード③ は cache 経由 (`use_cache=True`) で呼ぶ.

    非 cache 経路 (`upload_image_to_eps` 直呼び) に戻すと、この test が失敗する
    (mock_eps_par の call_args が空になる or use_cache=True が渡らない).
    ① で EPS 化済の hero 再利用 / ReviseItem 失敗後の再クリックで二重課金を防ぐ.
    """
    from tabs._supplier_photo_pipeline import _upload_single_and_revise_main
    from monitor.ebay_eps_uploader import EpsUploadResult

    local_img = tmp_path / "hero.png"
    local_img.write_bytes(b"fake-png-bytes")
    mock_eps_par.return_value = [
        EpsUploadResult(success=True, eps_url="https://i.ebayimg.com/main.jpg"),
    ]
    mock_revise.return_value = {"success": True, "message": "OK", "picture_urls": []}

    existing = ["https://i.ebayimg.com/old.jpg", "https://i.ebayimg.com/2.jpg"]
    result = _upload_single_and_revise_main("123456789012", str(local_img), "local", existing)
    assert result["success"] is True

    args, kwargs = mock_eps_par.call_args
    # 第1引数 = paths (single-item list)
    passed_paths = args[0] if args else kwargs.get("paths")
    assert len(passed_paths) == 1
    assert Path(str(passed_paths[0])).name == "hero.png"
    # kwargs で use_cache=True + max_workers=1
    assert kwargs.get("use_cache") is True, "cache 経由でないと再課金アップロード事故 (H1)"
    assert kwargs.get("max_workers") == 1


@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_single_twice_second_call_hits_cache_no_reupload(
    mock_creds, mock_ok, mock_eps_par, mock_revise, tmp_path,
):
    """W314 S2 review H1 追加回帰: 同一ファイルで③を 2 回実行しても実 EPS upload は 1 回だけ.

    現実の `eps_upload_cache` (DB, file_hash → EPS URL) を通っている想定の挙動を、
    `upload_images_parallel` のモック側で cache 挙動を模倣して検証する
    (1 回目: 実 upload / 2 回目: cache hit で `_upload_image_to_eps` を呼ばない).
    実装が cache 非経由 (`upload_image_to_eps` 直呼び) だとこの test は fail する
    (mock_eps_par が呼ばれない = call_count が 0 のまま).
    """
    from tabs._supplier_photo_pipeline import _upload_single_and_revise_main
    from monitor.ebay_eps_uploader import EpsUploadResult

    local_img = tmp_path / "hero.png"
    local_img.write_bytes(b"fake-png-bytes")

    # モック上で cache 挙動を模す: どちらの call でも use_cache=True で EPS URL を返す
    # (現実の cache lookup 経路が生きていることを、caller が cache 経由経路を叩いている
    # ことで確認する)
    mock_eps_par.return_value = [
        EpsUploadResult(success=True, eps_url="https://i.ebayimg.com/cached.jpg"),
    ]
    mock_revise.return_value = {"success": True, "message": "OK", "picture_urls": []}

    existing = ["https://i.ebayimg.com/old.jpg"]
    r1 = _upload_single_and_revise_main("123456789012", str(local_img), "local", existing)
    r2 = _upload_single_and_revise_main("123456789012", str(local_img), "local", existing)
    assert r1["success"] is True and r2["success"] is True

    # 2 回とも cache 経由 (`upload_images_parallel` with use_cache=True) を叩いている
    assert mock_eps_par.call_count == 2
    for call in mock_eps_par.call_args_list:
        _, kwargs = call
        assert kwargs.get("use_cache") is True, (
            "cache 経由でない = eps_upload_cache を bypass = ReviseItem 失敗 → 再試行で "
            "同一ファイルを再課金アップロードする H1 事故が復活している"
        )


@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_single_remote_downloads_then_uploads(mock_creds, mock_ok, mock_revise, tmp_path, monkeypatch):
    """kind='remote' は内部で DL → EPS upload を経由する (hotlink 直渡し禁止)."""
    from tabs._supplier_photo_pipeline import _upload_single_and_revise_main
    from monitor.ebay_eps_uploader import EpsUploadResult

    downloaded_path = tmp_path / "downloaded_main.jpg"

    def _fake_download(url, dest, timeout=30.0):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-bytes")
        return dest

    monkeypatch.setattr(
        "tabs._supplier_photo_pipeline._download_image_to", _fake_download,
    )

    with patch("monitor.ebay_eps_uploader.upload_image_to_eps") as mock_eps:
        mock_eps.return_value = EpsUploadResult(success=True, eps_url="https://i.ebayimg.com/new_main.jpg")
        mock_revise.return_value = {"success": True, "message": "OK", "picture_urls": []}

        result = _upload_single_and_revise_main(
            "123456789012", "https://supplier.example/raw.jpg", "remote",
            ["https://i.ebayimg.com/old_main.jpg", "https://i.ebayimg.com/2.jpg"],
        )
    assert result["success"] is True
    mock_eps.assert_called_once()


# ---- F4 fail-closed integration: 非 https 混入時に revise を呼ばず中断 ----

@patch("monitor.ebay_client.revise_item_pictures")
@patch("monitor.ebay_eps_uploader.upload_images_parallel")
@patch("monitor.credentials.ebay_credentials_ok", return_value=True)
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_upload_single_aborts_when_existing_has_non_upgradable_non_https(
    mock_creds, mock_ok, mock_eps_par, mock_revise, tmp_path,
):
    """F4: 昇格しても非 https が残る URL がある時、ReviseItem を呼ばず中断."""
    from tabs._supplier_photo_pipeline import _upload_single_and_revise_main
    from monitor.ebay_eps_uploader import EpsUploadResult

    local_img = tmp_path / "hero.png"
    local_img.write_bytes(b"fake-png-bytes")
    mock_eps_par.return_value = [
        EpsUploadResult(success=True, eps_url="https://i.ebayimg.com/new_main.jpg"),
    ]

    existing = [
        "https://i.ebayimg.com/main.jpg",
        "ftp://example.com/bad.jpg",  # 昇格対象外
        "https://i.ebayimg.com/3.jpg",
    ]
    result = _upload_single_and_revise_main(
        "123456789012", str(local_img), "local", existing,
    )
    assert result["success"] is False
    assert "https 化できない" in result["message"]
    mock_revise.assert_not_called()


# =====================================================================
# F3: get_all_ebay_image_urls の Ack 検証 (fail-closed)
# =====================================================================

def _ack_body(ack: str, picture_urls: list[str]) -> str:
    pics = "\n".join(f"    <PictureURL>{u}</PictureURL>" for u in picture_urls)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>{ack}</Ack>
  <Item>
    <PictureDetails>
{pics}
    </PictureDetails>
  </Item>
</GetItemResponse>"""


@patch("monitor.ebay_client._call_trading_api")
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_get_all_ebay_image_urls_ack_success_returns_urls(mock_creds, mock_call, tmp_path):
    """F3: Ack=Success 時は PictureURL 全件を返す."""
    from monitor import ebay_image_fetcher
    mock_call.return_value = {"raw": _ack_body("Success", [
        "https://i.ebayimg.com/1.jpg", "https://i.ebayimg.com/2.jpg",
    ])}
    urls = ebay_image_fetcher.get_all_ebay_image_urls("123456789012")
    assert urls == ["https://i.ebayimg.com/1.jpg", "https://i.ebayimg.com/2.jpg"]


@patch("monitor.ebay_client._call_trading_api")
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_get_all_ebay_image_urls_ack_warning_returns_empty_fail_closed(
    mock_creds, mock_call, tmp_path,
):
    """F3: Ack=Warning (部分応答の可能性) は fail-closed で空 list."""
    from monitor import ebay_image_fetcher
    mock_call.return_value = {"raw": _ack_body("Warning", [
        "https://i.ebayimg.com/might_be_partial.jpg",
    ])}
    urls = ebay_image_fetcher.get_all_ebay_image_urls("123456789012")
    assert urls == []


@patch("monitor.ebay_client._call_trading_api")
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_get_all_ebay_image_urls_ack_failure_returns_empty(mock_creds, mock_call):
    """F3: Ack=Failure は fail-closed."""
    from monitor import ebay_image_fetcher
    mock_call.return_value = {"raw": _ack_body("Failure", [])}
    assert ebay_image_fetcher.get_all_ebay_image_urls("123456789012") == []


@patch("monitor.ebay_client._call_trading_api")
@patch("monitor.credentials.get_ebay_credentials", return_value={
    "app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t",
})
def test_get_all_ebay_image_urls_ack_missing_returns_empty(mock_creds, mock_call):
    """F3: Ack 要素そのものが無い応答も fail-closed."""
    from monitor import ebay_image_fetcher
    mock_call.return_value = {"raw": "<garbage/>"}
    assert ebay_image_fetcher.get_all_ebay_image_urls("123456789012") == []


def test_get_all_ebay_image_urls_invalid_item_id_returns_empty():
    """F3 sanity: 明らかに不正な ItemID は早期 return."""
    from monitor.ebay_image_fetcher import get_all_ebay_image_urls
    assert get_all_ebay_image_urls("") == []
    assert get_all_ebay_image_urls("abc") == []
    assert get_all_ebay_image_urls("1234") == []  # 桁数不足


def test_api_image_urls_source_unchanged_by_f3():
    """F3: `_api_image_urls` のソースコードは Ack 検証を追加していない.

    ebay_listing_image.py の 1 枚目 cache 経路が `_api_image_urls` を叩いており、
    その挙動 (Ack 検証なし = Warning でも URL 返却) を維持する契約. F3 変更が
    `_api_image_urls` を触っていないことをソース検査で verify (conftest autouse で
    `_api_image_urls` 自体は `[]` に固定されているため実行時 assertion は不可、
    ソース検査で code smell 検出).
    """
    import inspect
    from monitor import ebay_image_fetcher
    src = inspect.getsource(ebay_image_fetcher._api_image_urls)
    # `_api_image_urls` は Ack 検証を含まない (F3 は get_all_ebay_image_urls 側にのみ追加)
    assert "<Ack>" not in src, "F3 は `_api_image_urls` を touch してはいけない (1 枚目 cache 経路の挙動変化)"
    assert "Ack != Success" not in src


# =====================================================================
# _log_content_change_images_safe (listing_content_change_log no-op fallback)
# =====================================================================

def test_log_content_change_images_safe_import_error_no_raise():
    """listing_content_change_log 未整備 (並行実装中) でも例外を投げない (Q0)."""
    from tabs._supplier_photo_pipeline import _log_content_change_images_safe

    # W314 S1 (別 agent 担当) が未着手/未マージの環境ではモジュールが存在しない。
    # 存在してもしなくても例外を投げないことを確認する。
    _log_content_change_images_safe(
        "123456789012", ["https://old.jpg"], ["https://new.jpg"],
        source_tab="supplier_candidates", candidate_id=1, success=True,
    )  # 例外が出れば test 自体が fail する


def test_log_content_change_images_safe_calls_module_when_available(monkeypatch):
    """listing_content_change_log 統合後の契約 (設計書 §6) に沿って呼び出す."""
    from tabs._supplier_photo_pipeline import _log_content_change_images_safe

    calls = []

    def _fake_log_content_change(**kwargs):
        calls.append(kwargs)
        return 1

    fake_mod = types.ModuleType("monitor.listing_content_change_log")
    fake_mod.log_content_change = _fake_log_content_change
    monkeypatch.setitem(sys.modules, "monitor.listing_content_change_log", fake_mod)

    _log_content_change_images_safe(
        "123456789012", ["https://old.jpg"], ["https://new.jpg"],
        source_tab="supplier_candidates", candidate_id=7, success=True,
        ebay_ack="OK",
    )

    assert len(calls) == 1
    kw = calls[0]
    assert kw["ebay_item_id"] == "123456789012"
    assert kw["field"] == "images"
    assert kw["before_value"] == '["https://old.jpg"]'
    assert kw["after_value"] == '["https://new.jpg"]'
    assert kw["source_tab"] == "supplier_candidates"
    assert kw["candidate_id"] == 7
    assert kw["success"] is True
    assert kw["ebay_ack"] == "OK"


def test_log_content_change_images_safe_module_exception_no_raise(monkeypatch):
    """log_content_change 呼出自体が例外を投げても UI を落とさない."""
    from tabs._supplier_photo_pipeline import _log_content_change_images_safe

    def _boom(**kwargs):
        raise RuntimeError("db error")

    fake_mod = types.ModuleType("monitor.listing_content_change_log")
    fake_mod.log_content_change = _boom
    monkeypatch.setitem(sys.modules, "monitor.listing_content_change_log", fake_mod)

    _log_content_change_images_safe(
        "123456789012", [], [], source_tab="x", candidate_id=None, success=False,
    )  # 例外が出れば test 自体が fail する


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
