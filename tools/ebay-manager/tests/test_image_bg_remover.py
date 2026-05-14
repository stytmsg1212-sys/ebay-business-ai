#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W10 Phase B: image_bg_remover の単体テスト。

設計:
  - rembg モデル初回 DL に時間がかかるため、重い推論テストは
    `--run-slow` opt-in で走らせる (pytest conftest で定義済の想定なら流用、
    無ければ skipif で環境変数 RUN_SLOW_TESTS=1 を見る)。
  - 軽量ユニット (設定解決、入力型判定、エラーパス、最小寸法検査) は
    常時実行で 415 + α の regression を守る。
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from PIL import Image

from monitor.image_bg_remover import (
    RemovalResult,
    _coerce_settings,
    _load_image,
    _prepare_image,
    _resolve_model_dir,
    _validate_min_dimension,
    _validate_url_for_ssrf,
    clear_session_cache,
    remove_background,
    remove_background_batch,
)

FIXTURES = Path(__file__).parent / "fixtures" / "design_samples"
SLOW_TESTS_ENABLED = os.environ.get("RUN_SLOW_TESTS") == "1"


def _make_test_png(w: int = 600, h: int = 600, color=(255, 0, 0)) -> bytes:
    """テスト用 PNG の bytes を生成。"""
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestCoerceSettings:
    def test_empty_returns_defaults(self):
        cfg = _coerce_settings(None)
        assert cfg["background_removal_engine"] == "rembg"
        assert cfg["rembg_model_name"] == "isnet-general-use"
        assert cfg["eps_min_dimension_px"] == 500
        assert cfg["rembg_alpha_matting"] is True

    def test_user_settings_override_defaults(self):
        cfg = _coerce_settings({"image_processing": {"eps_min_dimension_px": 400}})
        assert cfg["eps_min_dimension_px"] == 400
        assert cfg["rembg_model_name"] == "isnet-general-use"  # 他は default 維持

    def test_no_image_processing_block_returns_defaults(self):
        cfg = _coerce_settings({"unrelated": "key"})
        assert cfg["rembg_model_dir"] == "models"

    def test_null_values_treated_as_missing(self):
        cfg = _coerce_settings({"image_processing": {"rembg_model_name": None}})
        assert cfg["rembg_model_name"] == "isnet-general-use"


class TestResolveModelDir:
    def test_relative_path_becomes_absolute(self, tmp_path, monkeypatch):
        import monitor.image_bg_remover as m

        monkeypatch.setattr(m, "_PROJECT_ROOT", tmp_path)
        resolved = _resolve_model_dir("models")
        assert resolved.is_absolute()
        assert resolved.name == "models"
        assert resolved.exists()

    def test_absolute_path_kept(self, tmp_path):
        target = tmp_path / "custom_models"
        resolved = _resolve_model_dir(str(target))
        assert resolved == target
        assert resolved.exists()


class TestLoadImage:
    def test_load_from_bytes(self):
        png = _make_test_png()
        img, kind = _load_image(png)
        assert isinstance(img, Image.Image)
        assert kind == "bytes"

    def test_load_from_pil_image_copies(self):
        original = Image.new("RGB", (100, 100), (0, 255, 0))
        img, kind = _load_image(original)
        assert kind == "pil"
        assert img is not original  # コピーされている
        assert img.size == original.size

    def test_load_from_file_path(self, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(_make_test_png())
        img, kind = _load_image(str(f))
        assert kind == "path"
        assert img.size == (600, 600)

    def test_load_from_path_object(self, tmp_path):
        f = tmp_path / "b.png"
        f.write_bytes(_make_test_png(w=50, h=50))
        img, kind = _load_image(f)
        assert kind == "path"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _load_image(str(tmp_path / "missing.png"))

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            _load_image(12345)  # type: ignore[arg-type]


class TestPrepareImage:
    def test_rgb_retained(self):
        img = Image.new("RGB", (100, 100), (128, 128, 128))
        out = _prepare_image(img)
        assert out.mode in ("RGB", "RGBA")

    def test_rgba_retained(self):
        img = Image.new("RGBA", (100, 100), (128, 128, 128, 200))
        out = _prepare_image(img)
        assert out.mode == "RGBA"

    def test_palette_converted(self):
        img = Image.new("P", (100, 100))
        out = _prepare_image(img)
        assert out.mode in ("RGB", "RGBA")


class TestValidateUrlForSsrf:
    def test_https_public_hostname_ok(self):
        # 実ネットワーク依存だが eBay/メルカリは必ず解決可能
        # DNS が落ちていると skip したい場合は後で環境チェック追加
        import socket
        try:
            socket.getaddrinfo("static.mercdn.net", None)
        except socket.gaierror:
            pytest.skip("DNS 解決不可能な環境")
        assert _validate_url_for_ssrf("https://static.mercdn.net/item/a.jpg") is None

    def test_reject_file_scheme(self):
        err = _validate_url_for_ssrf("file:///etc/passwd")
        assert err is not None
        assert "スキーム" in err

    def test_reject_ftp_scheme(self):
        err = _validate_url_for_ssrf("ftp://example.com/x.jpg")
        assert err is not None

    def test_reject_loopback_ip(self):
        err = _validate_url_for_ssrf("http://127.0.0.1/x.jpg")
        assert err is not None
        assert "内部" in err

    def test_reject_private_ip(self):
        err = _validate_url_for_ssrf("http://192.168.1.1/x.jpg")
        assert err is not None

    def test_reject_link_local_aws_metadata(self):
        # AWS/GCP metadata service の代表例
        err = _validate_url_for_ssrf("http://169.254.169.254/latest/meta-data/")
        assert err is not None

    def test_reject_localhost_hostname(self):
        err = _validate_url_for_ssrf("http://localhost:8502/")
        assert err is not None

    def test_reject_url_without_hostname(self):
        err = _validate_url_for_ssrf("http:///no-host")
        assert err is not None

    def test_ssrf_blocks_load_image(self):
        """_load_image 内で SSRF が発動し ValueError になる。"""
        with pytest.raises(ValueError, match="SSRF"):
            _load_image("http://127.0.0.1:8502/x.png")


class TestValidateMinDimension:
    def test_ok_when_above_min(self):
        img = Image.new("RGB", (600, 600))
        assert _validate_min_dimension(img, 500) is None

    def test_fail_when_short_side_below(self):
        img = Image.new("RGB", (800, 400))
        err = _validate_min_dimension(img, 500)
        assert err is not None
        assert "500px" in err

    def test_equal_to_min_is_ok(self):
        img = Image.new("RGB", (500, 500))
        assert _validate_min_dimension(img, 500) is None


class TestRemoveBackgroundErrorPaths:
    """重い推論を回さずエラーパスのみ検証するテスト群。"""

    def test_below_min_dimension_returns_failure(self):
        tiny = _make_test_png(w=400, h=400)
        r = remove_background(tiny, settings={"image_processing": {"eps_min_dimension_px": 500}})
        assert not r.success
        assert r.image is None
        assert "500px" in (r.error or "")
        assert r.original_size == (400, 400)

    def test_missing_file_returns_failure(self, tmp_path):
        r = remove_background(str(tmp_path / "nope.png"))
        assert not r.success
        assert "読込失敗" in (r.error or "")

    def test_invalid_source_type_returns_failure(self):
        # ImageSource 型外は読込段階で失敗
        r = remove_background(object())  # type: ignore[arg-type]
        assert not r.success
        assert r.error


class TestBatch:
    def test_batch_reports_progress(self):
        # 最小寸法で失敗するケースで progress callback の呼び出しを確認
        sources = [_make_test_png(w=400, h=400) for _ in range(3)]
        calls: list[tuple[int, int]] = []

        def cb(i, total, result):
            calls.append((i, total))

        results = remove_background_batch(
            sources,
            settings={"image_processing": {"eps_min_dimension_px": 500}},
            on_progress=cb,
        )
        assert len(results) == 3
        assert all(not r.success for r in results)
        assert calls == [(1, 3), (2, 3), (3, 3)]

    def test_batch_continues_after_failures(self):
        # 1 件は成功しなくてもリスト全長が帰る
        sources = [_make_test_png(w=400, h=400), _make_test_png(w=400, h=400)]
        results = remove_background_batch(
            sources, settings={"image_processing": {"eps_min_dimension_px": 500}},
        )
        assert len(results) == 2


# ===== 重い推論テスト (opt-in) =====

@pytest.mark.skipif(
    not SLOW_TESTS_ENABLED,
    reason="rembg 推論テストは RUN_SLOW_TESTS=1 で有効化",
)
class TestRemoveBackgroundInference:
    """実 rembg 推論を走らせるテスト。モデル DL 済み環境専用。"""

    @pytest.fixture(autouse=True)
    def _reset_session(self):
        # テスト間で session を cache しつつ、fixture 側では問題ないようにする
        yield
        # 何もしない (session 再利用)

    def test_real_removal_png_produces_rgba(self):
        sample = FIXTURES / "sample_04_cable_corner.png"
        if not sample.exists():
            pytest.skip(f"fixture なし: {sample}")
        r = remove_background(sample)
        assert r.success, r.error
        assert r.image is not None
        assert r.image.mode == "RGBA"

    def test_real_removal_with_pil_input(self):
        sample = FIXTURES / "sample_04_cable_corner.png"
        if not sample.exists():
            pytest.skip(f"fixture なし: {sample}")
        img = Image.open(sample)
        r = remove_background(img)
        assert r.success, r.error
        assert r.source_kind == "pil"


def teardown_module(module):
    """全テスト後に session cache をクリア (リーク防止)。"""
    clear_session_cache()
