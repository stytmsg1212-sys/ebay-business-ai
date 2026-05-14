#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W10 Phase C: image_composer の単体テスト。"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from monitor.image_composer import (
    ComposeResult,
    _build_shadow_config,
    _coerce_settings,
    _rgb_tuple,
    compose_cover_image,
)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


def _product_rgba(size=(800, 800)) -> Image.Image:
    """RGBA 商品画像のフェイク (円 + 少し透過余白)。"""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    draw.ellipse([(50, 50), (size[0] - 50, size[1] - 50)], fill=(100, 160, 220, 255))
    return img


def _tiny_logo() -> Image.Image:
    """テスト用の簡単なロゴ (1px circle)。"""
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    draw.ellipse([(10, 10), (90, 90)], fill=(30, 30, 30, 255))
    return img


class TestCoerceSettings:
    def test_empty_returns_defaults(self):
        cfg = _coerce_settings(None)
        assert cfg["canvas_size"] == [1600, 1600]
        assert cfg["layout"]["product_ratio"] == 0.70
        assert cfg["shadow"]["enabled"] is True

    def test_user_overrides_merged_into_defaults(self):
        cfg = _coerce_settings({"image_processing": {"canvas_size": [800, 800]}})
        assert cfg["canvas_size"] == [800, 800]
        # 他の key は default 維持
        assert cfg["layout"]["card_ratio"] == 0.22

    def test_dict_merge_preserves_defaults(self):
        """layout dict の一部のみ上書きしても他 key は残る。"""
        cfg = _coerce_settings({
            "image_processing": {"layout": {"product_ratio": 0.6}},
        })
        assert cfg["layout"]["product_ratio"] == 0.6
        assert cfg["layout"]["card_ratio"] == 0.22
        assert cfg["layout"]["margin_ratio"] == 0.08

    def test_no_image_processing_returns_defaults(self):
        cfg = _coerce_settings({"unrelated": "key"})
        assert cfg["canvas_size"] == [1600, 1600]


class TestRgbTuple:
    def test_list_converted(self):
        assert _rgb_tuple([100, 150, 200]) == (100, 150, 200)

    def test_tuple_converted(self):
        assert _rgb_tuple((10, 20, 30)) == (10, 20, 30)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _rgb_tuple("not-a-color")
        with pytest.raises(ValueError):
            _rgb_tuple([100, 200])  # 短すぎ


class TestBuildShadowConfig:
    def test_enabled_legacy_keys_mapped_to_ambient(self):
        cfg = _build_shadow_config({
            "enabled": True, "blur_radius": 30, "offset_y": 20, "opacity": 0.5,
        })
        assert cfg is not None
        # 旧キー → ambient 層にマップされている
        assert cfg.ambient_blur >= 30
        assert cfg.ambient_offset_y == 20

    def test_disabled_returns_none(self):
        cfg = _build_shadow_config({"enabled": False})
        assert cfg is None

    def test_missing_enabled_defaults_to_true(self):
        cfg = _build_shadow_config({})
        assert cfg is not None

    def test_new_keys_applied(self):
        cfg = _build_shadow_config({
            "enabled": True,
            "contact_blur": 8, "contact_opacity": 0.6,
            "ambient_blur": 60, "ambient_opacity": 0.2,
        })
        assert cfg.contact_blur == 8
        assert cfg.contact_opacity == 0.6
        assert cfg.ambient_blur == 60
        assert cfg.ambient_opacity == 0.2


class TestComposeCoverImage:
    def test_success_with_fake_product(self, tmp_path):
        """小さい canvas で合成完走する。"""
        product = _product_rgba((400, 400))
        logo = _tiny_logo()
        settings = {
            "image_processing": {
                "canvas_size": [800, 800],
                "shadow": {"enabled": False},
            },
        }
        r = compose_cover_image(product, settings=settings, logo_image=logo)
        assert r.success, r.error
        assert r.image is not None
        assert r.image.mode == "RGB"
        assert r.image.size == (800, 800)
        assert r.canvas_size == (800, 800)

    def test_fail_when_logo_not_found(self, tmp_path):
        product = _product_rgba((400, 400))
        # logo_path が存在しないファイル
        settings = {
            "image_processing": {
                "canvas_size": [200, 200],
                "logo_path": str(tmp_path / "nope.png"),
            },
        }
        r = compose_cover_image(product, settings=settings)
        assert not r.success
        assert "ロゴ" in (r.error or "")

    def test_invalid_canvas_size_fails(self):
        product = _product_rgba((400, 400))
        logo = _tiny_logo()
        r = compose_cover_image(
            product,
            settings={"image_processing": {"canvas_size": [-1, 100]}},
            logo_image=logo,
        )
        assert not r.success
        assert "canvas_size" in (r.error or "")

    def test_invalid_layout_ratio_fails(self):
        product = _product_rgba((400, 400))
        logo = _tiny_logo()
        r = compose_cover_image(
            product,
            settings={
                "image_processing": {
                    "canvas_size": [500, 500],
                    "layout": {"product_ratio": 1.5, "card_ratio": 0.2, "margin_ratio": 0.1},
                }
            },
            logo_image=logo,
        )
        assert not r.success
        assert "layout" in (r.error or "")

    def test_shadow_config_respected(self):
        product = _product_rgba((300, 300))
        logo = _tiny_logo()
        r_with = compose_cover_image(
            product,
            settings={"image_processing": {"canvas_size": [600, 600], "shadow": {"enabled": True}}},
            logo_image=logo,
        )
        r_without = compose_cover_image(
            product,
            settings={"image_processing": {"canvas_size": [600, 600], "shadow": {"enabled": False}}},
            logo_image=logo,
        )
        assert r_with.success and r_without.success
        assert r_with.meta["shadow"] is True
        assert r_without.meta["shadow"] is False

    def test_loads_logo_from_settings_path_when_exists(self):
        """デフォルトのロゴパスが動作する (assets/ 配下)。"""
        if not (ASSETS_DIR / "monohonpo_logo_transparent.png").exists():
            pytest.skip("assets/monohonpo_logo_transparent.png が無い環境")
        product = _product_rgba((400, 400))
        # settings 省略 → defaults = assets/monohonpo_logo_transparent.png
        r = compose_cover_image(product)
        assert r.success, r.error

    def test_rgb_product_accepted(self):
        """RGB 入力 (背景除去未実施) でもクラッシュしない。"""
        product_rgb = Image.new("RGB", (400, 400), (200, 100, 100))
        logo = _tiny_logo()
        r = compose_cover_image(
            product_rgb,
            settings={"image_processing": {"canvas_size": [600, 600]}},
            logo_image=logo,
        )
        assert r.success, r.error
