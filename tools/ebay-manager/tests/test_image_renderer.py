#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W10 Phase C: image_renderer の単体テスト。"""
from __future__ import annotations

import pytest
from PIL import Image

from monitor.image_renderer import (
    ShadowConfig,
    fit_into_zone,
    paste_with_floor_shadow,
    render_floor_shadow,
    render_gradient_background,
    render_logo_card,
)


def _opaque_logo(size: int = 200) -> Image.Image:
    """テスト用の不透明な "ロゴ" (黒円) を RGBA で返す。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    draw.ellipse([(20, 20), (size - 20, size - 20)], fill=(20, 20, 20, 255))
    return img


def _opaque_subject(size=(100, 200)) -> Image.Image:
    """床影生成用の単純な RGBA 被写体。"""
    img = Image.new("RGBA", size, (200, 40, 40, 255))
    return img


class TestRenderGradientBackground:
    def test_vertical_gradient_top_and_bottom_match(self):
        bg = render_gradient_background((50, 100), (255, 0, 0), (0, 0, 255))
        assert bg.mode == "RGB"
        assert bg.size == (50, 100)
        assert bg.getpixel((0, 0)) == (255, 0, 0)
        # 最下端は bottom_color (誤差 ±2)
        r, g, b = bg.getpixel((0, 99))
        assert r < 5 and b > 250

    def test_horizontal_gradient(self):
        bg = render_gradient_background(
            (100, 50), (0, 255, 0), (0, 0, 255), direction="horizontal",
        )
        assert bg.getpixel((0, 0))[:2] == (0, 255)
        right = bg.getpixel((99, 25))
        assert right[2] > 250

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError, match="direction"):
            render_gradient_background((50, 50), (0, 0, 0), (1, 1, 1), direction="diagonal")

    def test_zero_size_raises(self):
        with pytest.raises(ValueError, match="size"):
            render_gradient_background((0, 10), (0, 0, 0), (1, 1, 1))


class TestRenderLogoCard:
    def test_size_matches_card_size(self):
        card = render_logo_card(_opaque_logo(), (160, 100))
        assert card.size == (160, 100)
        assert card.mode == "RGBA"

    def test_center_opaque_logo_visible(self):
        """ロゴ中央がカードに描画されている (不透明かつ黒っぽい)。"""
        card = render_logo_card(_opaque_logo(), (200, 200), logo_padding_ratio=0.1)
        r, g, b, a = card.getpixel((100, 100))
        assert a == 255
        assert r < 80 and g < 80 and b < 80, f"中央画素は黒系想定、got {(r, g, b)}"

    def test_corner_is_transparent_when_rounded(self):
        """角丸の四隅は透明 (RGBA)。"""
        card = render_logo_card(_opaque_logo(), (160, 100), corner_radius=20)
        # (0, 0) は角丸のカット領域 -> alpha=0
        assert card.getpixel((0, 0))[3] < 50

    def test_no_border_when_width_zero(self):
        card = render_logo_card(_opaque_logo(), (160, 100), border_width=0)
        # (1, 1) は borderless なら fill_color に近いはず
        assert card.getpixel((80, 50))[3] == 255  # 中心不透明

    def test_invalid_padding_raises(self):
        with pytest.raises(ValueError, match="logo_padding_ratio"):
            render_logo_card(_opaque_logo(), (100, 100), logo_padding_ratio=0.6)

    def test_zero_size_raises(self):
        with pytest.raises(ValueError, match="card_size"):
            render_logo_card(_opaque_logo(), (0, 100))


class TestRenderFloorShadow:
    def test_returns_layer_larger_than_subject(self):
        sub = _opaque_subject((100, 200))
        cfg = ShadowConfig(contact_blur=10, ambient_blur=30, contact_offset_y=5, ambient_offset_y=20)
        layer = render_floor_shadow(sub, shadow_config=cfg)
        # Ambient のぼかしで左右に余白、下方にも余白
        assert layer.size[0] > 100
        assert layer.size[1] > 200

    def test_rgb_subject_raises(self):
        rgb = Image.new("RGB", (100, 100), (255, 0, 0))
        with pytest.raises(ValueError, match="RGBA"):
            render_floor_shadow(rgb)

    def test_zero_opacity_both_layers_transparent(self):
        sub = _opaque_subject((50, 50))
        cfg = ShadowConfig(contact_opacity=0.0, ambient_opacity=0.0)
        layer = render_floor_shadow(sub, shadow_config=cfg)
        alpha = layer.getchannel("A")
        assert max(alpha.getextrema()) == 0

    def test_high_opacity_produces_visible_shadow(self):
        sub = _opaque_subject((50, 80))
        cfg = ShadowConfig(contact_opacity=1.0, ambient_opacity=1.0,
                           contact_blur=3, ambient_blur=8)
        layer = render_floor_shadow(sub, shadow_config=cfg)
        alpha_max = max(layer.getchannel("A").getextrema())
        assert alpha_max > 100

    def test_layer_has_lateral_pad_metadata(self):
        sub = _opaque_subject((80, 80))
        layer = render_floor_shadow(sub)
        assert "lateral_pad" in layer.info
        assert layer.info["lateral_pad"] > 0


class TestFitIntoZone:
    def test_aspect_preserved_when_downsizing(self):
        src = Image.new("RGBA", (1000, 500), (0, 255, 0, 255))
        fitted = fit_into_zone(src, (200, 200))
        assert fitted.size == (200, 200)
        # 中央の横ライン上に緑画素が存在、上下に透明余白があるはず
        assert fitted.getpixel((100, 100))[3] > 0  # 中央不透明
        assert fitted.getpixel((100, 5))[3] == 0   # 上余白透明
        assert fitted.getpixel((100, 195))[3] == 0  # 下余白透明

    def test_bottom_align(self):
        src = Image.new("RGBA", (100, 50), (255, 0, 0, 255))
        fitted = fit_into_zone(src, (200, 200), align="bottom")
        # 下辺に画素、上辺は透明
        assert fitted.getpixel((100, 199))[3] > 0
        assert fitted.getpixel((100, 10))[3] == 0

    def test_upscale_small_image(self):
        src = Image.new("RGBA", (50, 50), (0, 0, 255, 255))
        fitted = fit_into_zone(src, (200, 200))
        assert fitted.size == (200, 200)

    def test_invalid_align_raises(self):
        src = Image.new("RGBA", (100, 100), (128, 128, 128, 255))
        with pytest.raises(ValueError, match="align"):
            fit_into_zone(src, (200, 200), align="diagonal")

    def test_zero_zone_raises(self):
        src = Image.new("RGBA", (100, 100), (128, 128, 128, 255))
        with pytest.raises(ValueError, match="zone_size"):
            fit_into_zone(src, (0, 100))


class TestPasteWithFloorShadow:
    def test_paste_returns_new_image(self):
        canvas = Image.new("RGBA", (400, 400), (200, 200, 200, 255))
        sub = _opaque_subject((100, 100))
        result = paste_with_floor_shadow(canvas, sub, (50, 50))
        # コピーされている
        assert result is not canvas
        assert result.size == canvas.size
        # 貼付位置は不透明
        assert result.getpixel((100, 100))[3] == 255

    def test_no_shadow_when_config_none(self):
        canvas = Image.new("RGBA", (400, 400), (255, 255, 255, 255))
        sub = _opaque_subject((100, 100))
        result = paste_with_floor_shadow(canvas, sub, (50, 50), shadow_config=None)
        # 被写体より下方 (150 < y < 180) は影が無いので元の白のまま
        r, g, b, a = result.getpixel((100, 170))
        assert (r, g, b) == (255, 255, 255)

    def test_rgb_canvas_converted(self):
        rgb_canvas = Image.new("RGB", (200, 200), (128, 128, 128))
        sub = _opaque_subject((50, 50))
        result = paste_with_floor_shadow(rgb_canvas, sub, (10, 10))
        assert result.mode == "RGBA"
