#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W10 Phase C: カバー画像 (1 枚目) 合成オーケストレータ。

入力 1 枚の商品画像 (rembg 背景除去済 RGBA が理想) を元に、
MonoHonpo ロゴカードを並べた Studio product photo 風のカバー画像を生成する。

合成レイアウト (MVP 固定, Pioneer サンプルベース):
    ┌──────────────────────────────────────────┐
    │  ##### gradient bg (#ECEC → #D8D8) #####  │
    │                                           │
    │   +------------+        +-----------+     │
    │   |            |        | [MONO card]|    │
    │   |            |        +-----------+     │
    │   |  product   |          (shadow)        │
    │   |            |                          │
    │   |            |                          │
    │   +------------+                          │
    │      (shadow)                             │
    └──────────────────────────────────────────┘

商品・カード両方に床影を付け、合成感を抑える。

Phase C-2 で Claude Opus 4.7 に商品アスペクトから layout JSON を
生成させる adaptive 版に拡張予定。本モジュールは "固定テンプレ"。

正源: docs/image_processing.md / W10 設計レビュー (2026-04-23)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image

from monitor.image_renderer import (
    RGB,
    ShadowConfig,
    fit_into_zone,
    paste_with_floor_shadow,
    render_gradient_background,
    render_logo_card,
    render_radial_gradient_background,
    rotate_with_alpha,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ComposeResult:
    """カバー合成 1 件の結果。"""
    success: bool
    image: Optional[Image.Image] = None
    error: Optional[str] = None
    canvas_size: Optional[tuple[int, int]] = None
    meta: dict = field(default_factory=dict)


def _coerce_settings(settings: Optional[dict]) -> dict:
    """image_processing ブロックから合成に必要な値を取り出し、defaults で埋める。"""
    defaults = {
        "canvas_size": [1600, 1600],
        "layout": {"product_ratio": 0.70, "card_ratio": 0.22, "margin_ratio": 0.08},
        "background": {
            "top_color": [236, 236, 236],
            "bottom_color": [216, 216, 216],
            "gradient_direction": "vertical",
        },
        "card": {
            "aspect_ratio": 1.6,
            "corner_radius": 8,
            "fill_color": [252, 252, 250],
            "border_color": [220, 218, 212],
            "border_width": 1,
        },
        "shadow": {
            "enabled": True,
            "blur_radius": 22,
            "offset_y": 18,
            "opacity": 0.35,
        },
        "logo_path": "assets/monohonpo_logo_transparent.png",
    }

    if not settings:
        return defaults

    ip = settings.get("image_processing") or {}
    merged = dict(defaults)
    for key in defaults:
        if key in ip and ip[key] is not None:
            # dict 型はマージ、それ以外は上書き
            if isinstance(defaults[key], dict) and isinstance(ip[key], dict):
                merged[key] = {**defaults[key], **ip[key]}
            else:
                merged[key] = ip[key]
    return merged


def _rgb_tuple(c) -> RGB:
    """JSON list or tuple を RGB tuple に正規化。"""
    if isinstance(c, (list, tuple)) and len(c) >= 3:
        return (int(c[0]), int(c[1]), int(c[2]))
    raise ValueError(f"invalid RGB value: {c!r}")


def _resolve_logo_path(path_setting: str) -> Path:
    """settings のロゴパスを絶対パスに解決。"""
    p = Path(path_setting)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p


def _load_logo(logo: Optional[Image.Image], cfg: dict) -> Optional[Image.Image]:
    """引数 logo が None なら settings のパスから読み込む。読み込めなければ None。

    Pillow の Image.open は lazy load でファイルハンドルを保持し続けるので、
    コンテキストマネージャで読み込んだ後 copy() してメモリ常駐化する。
    """
    if logo is not None:
        return logo
    path = _resolve_logo_path(cfg["logo_path"])
    if not path.exists():
        logger.warning("ロゴファイルが存在しない: %s", path)
        return None
    try:
        with Image.open(path) as opened:
            opened.load()
            return opened.copy()
    except Exception as e:  # noqa: BLE001
        logger.warning("ロゴ読込失敗: %s / %s", path, e)
        return None


def _build_shadow_config(shadow_cfg: dict) -> Optional[ShadowConfig]:
    """settings の shadow dict から ShadowConfig を組み立てる。enabled=False なら None。

    旧設定 (blur_radius/offset_y/opacity 単層) も受け入れ、contact+ambient に
    自動マッピングする後方互換性を維持する。
    """
    if not shadow_cfg.get("enabled", True):
        return None

    # 旧設定キーのみの場合: blur_radius/offset_y/opacity を ambient 層にマップ、
    # contact 層はその派生で生成 (blur/offset/opacity を控えめに)
    if "blur_radius" in shadow_cfg and "ambient_blur" not in shadow_cfg:
        br = int(shadow_cfg.get("blur_radius", 22))
        oy = int(shadow_cfg.get("offset_y", 18))
        op = float(shadow_cfg.get("opacity", 0.35))
        return ShadowConfig(
            ambient_blur=max(br * 2, 30),
            ambient_offset_y=oy,
            ambient_opacity=op * 0.7,
            contact_blur=max(br // 4, 3),
            contact_offset_y=max(oy // 3, 3),
            contact_opacity=min(op * 1.5, 0.7),
        )

    # 新設定キー
    kwargs: dict = {}
    for key in (
        "contact_blur", "contact_offset_y", "contact_opacity", "contact_squish",
        "ambient_blur", "ambient_offset_y", "ambient_opacity", "ambient_squish",
    ):
        if key in shadow_cfg:
            val = shadow_cfg[key]
            if key.endswith("_opacity") or key.endswith("_squish"):
                kwargs[key] = float(val)
            else:
                kwargs[key] = int(val)
    if "shadow_color" in shadow_cfg:
        sc = shadow_cfg["shadow_color"]
        if isinstance(sc, (list, tuple)) and len(sc) >= 3:
            kwargs["shadow_color"] = (int(sc[0]), int(sc[1]), int(sc[2]))
    return ShadowConfig(**kwargs)


def compose_cover_image(
    product_image: Image.Image,
    *,
    settings: Optional[dict] = None,
    logo_image: Optional[Image.Image] = None,
) -> ComposeResult:
    """商品画像 + MonoHonpo カードのカバー合成を行う (固定テンプレート MVP)。

    Args:
        product_image: RGBA 推奨の商品画像。背景除去済が理想。
                       RGB でも動作するが商品と背景の境界に白枠が出る可能性あり。
        settings: 全 settings dict。Noneなら defaults。
        logo_image: ロゴ画像。None なら settings.logo_path から読み込み。

    Returns:
        ComposeResult。success=False 時は error に理由。
    """
    cfg = _coerce_settings(settings)

    try:
        canvas_w, canvas_h = (int(cfg["canvas_size"][0]), int(cfg["canvas_size"][1]))
    except Exception as e:  # noqa: BLE001
        return ComposeResult(success=False, error=f"canvas_size 不正: {e}")

    if canvas_w <= 0 or canvas_h <= 0:
        return ComposeResult(success=False, error=f"canvas_size は正値: {canvas_w}x{canvas_h}")

    try:
        layout = cfg["layout"]
        product_ratio = float(layout["product_ratio"])
        card_ratio = float(layout["card_ratio"])
        margin_ratio = float(layout["margin_ratio"])
    except Exception as e:  # noqa: BLE001
        return ComposeResult(success=False, error=f"layout 不正: {e}")

    if not (0 < product_ratio < 1 and 0 < card_ratio < 1 and 0 <= margin_ratio < 1):
        return ComposeResult(
            success=False,
            error=f"layout 比率は 0-1 範囲: product={product_ratio} card={card_ratio} margin={margin_ratio}",
        )

    if product_ratio + card_ratio + margin_ratio > 1.01:
        return ComposeResult(
            success=False,
            error=f"product+card+margin > 1.0: {product_ratio}+{card_ratio}+{margin_ratio}",
        )

    # 1. 背景グラデーション
    try:
        bg_cfg = cfg["background"]
        bg = render_gradient_background(
            (canvas_w, canvas_h),
            _rgb_tuple(bg_cfg["top_color"]),
            _rgb_tuple(bg_cfg["bottom_color"]),
            direction=bg_cfg.get("gradient_direction", "vertical"),
        ).convert("RGBA")
    except Exception as e:  # noqa: BLE001
        return ComposeResult(success=False, error=f"背景生成失敗: {e}")

    shadow_cfg = _build_shadow_config(cfg["shadow"])

    # 2. 商品配置ゾーン
    #    商品を左側に配置。縦は下側寄せ (床立ち感)。
    product_zone_w = int(canvas_w * product_ratio)
    product_zone_h = int(canvas_h * (1 - margin_ratio))
    margin_x = int(canvas_w * margin_ratio / 2)
    # 上下 margin は対称に、下は床影分を少し確保
    product_y = int(canvas_h * margin_ratio / 2)
    try:
        product_fit = fit_into_zone(
            product_image, (product_zone_w, product_zone_h), align="bottom",
        )
    except Exception as e:  # noqa: BLE001
        return ComposeResult(success=False, error=f"商品フィット失敗: {e}")

    bg = paste_with_floor_shadow(
        bg, product_fit, (margin_x, product_y), shadow_config=shadow_cfg,
    )

    # 3. カード配置 (右上寄り、商品より"後ろ" な感じ = 下端がキャンバス中央よりやや下)
    card_w = int(canvas_w * card_ratio)
    try:
        card_aspect = float(cfg["card"]["aspect_ratio"])
    except Exception:  # noqa: BLE001
        card_aspect = 1.6
    card_h = max(int(card_w / max(card_aspect, 0.1)), 1)

    logo = _load_logo(logo_image, cfg)
    if logo is None:
        return ComposeResult(
            success=False,
            error=f"ロゴが読み込めない (settings.logo_path={cfg['logo_path']})",
        )

    try:
        card = render_logo_card(
            logo,
            (card_w, card_h),
            fill_color=_rgb_tuple(cfg["card"]["fill_color"]),
            border_color=_rgb_tuple(cfg["card"]["border_color"]),
            border_width=int(cfg["card"].get("border_width", 1)),
            corner_radius=int(cfg["card"].get("corner_radius", 8)),
        )
    except Exception as e:  # noqa: BLE001
        return ComposeResult(success=False, error=f"カード生成失敗: {e}")

    # カード位置: 右寄り。商品と同じ"床面"に立つイメージで、
    # カード下端が商品下端より少し上 (= 画面手前にいる product より奥にいる card)。
    # 商品は下寄せされて product_y + product_zone_h が床面。
    floor_y = product_y + product_zone_h
    # カード下端を床面より少し上に置いて "奥行き" を演出
    card_bottom_y = floor_y - int(canvas_h * 0.05)
    card_y = max(card_bottom_y - card_h, 0)
    card_x = canvas_w - margin_x - card_w

    bg = paste_with_floor_shadow(
        bg, card, (card_x, card_y), shadow_config=shadow_cfg,
    )

    # 4. 最終 RGB 化 (EPS/JPEG 互換)
    final = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
    final.paste(bg, (0, 0), mask=bg.getchannel("A"))

    return ComposeResult(
        success=True,
        image=final,
        canvas_size=(canvas_w, canvas_h),
        meta={
            "product_zone": (product_zone_w, product_zone_h),
            "card_zone": (card_w, card_h),
            "product_pos": (margin_x, product_y),
            "card_pos": (card_x, card_y),
            "shadow": shadow_cfg is not None,
        },
    )


# ═══════════════════════════════════════════════════════════
# Claude Design-driven composition (Phase C-2)
# ═══════════════════════════════════════════════════════════

def _shadow_config_from_dict(d: dict) -> ShadowConfig:
    """Claude design の shadow dict を ShadowConfig に変換。"""
    return ShadowConfig(
        contact_blur=int(d.get("contact_blur", 5)),
        contact_offset_y=int(d.get("contact_offset_y", 6)),
        contact_opacity=float(d.get("contact_opacity", 0.55)),
        ambient_blur=int(d.get("ambient_blur", 55)),
        ambient_offset_y=int(d.get("ambient_offset_y", 22)),
        ambient_opacity=float(d.get("ambient_opacity", 0.18)),
    )


def compose_cover_image_with_design(
    product_image: Image.Image,
    design: dict,
    *,
    canvas_size: tuple[int, int] = (1600, 1600),
    logo_image: Optional[Image.Image] = None,
    logo_path: Optional[str] = None,
) -> ComposeResult:
    """Claude Opus 4.7 が生成した design JSON に従ってカバー画像を合成する。

    design は image_composer_claude.design_cover_with_claude() の戻り値
    .design を渡す前提。フィールドは検証済で不正値は無い想定。
    """
    canvas_w, canvas_h = canvas_size

    # ─── 背景 ───
    bg_spec = design.get("background") or {}
    top_c = tuple(bg_spec.get("top_color", (236, 236, 236)))
    bot_c = tuple(bg_spec.get("bottom_color", (216, 216, 216)))
    bg_type = bg_spec.get("type", "linear_gradient")

    try:
        if bg_type == "radial_gradient":
            vignette = float(bg_spec.get("vignette_strength", 0.25))
            bg = render_radial_gradient_background(
                canvas_size, top_c, bot_c, vignette_strength=vignette,
            )
        else:
            bg = render_gradient_background(canvas_size, top_c, bot_c, direction="vertical")
    except Exception as e:  # noqa: BLE001
        return ComposeResult(success=False, error=f"背景生成失敗: {e}")
    bg = bg.convert("RGBA")

    # ─── Product ───
    product_spec = design.get("product") or {}
    p_bbox = product_spec.get("bbox_ratio") or [0.06, 0.28, 0.60, 0.65]
    p_x = int(p_bbox[0] * canvas_w)
    p_y = int(p_bbox[1] * canvas_h)
    p_w = max(int(p_bbox[2] * canvas_w), 1)
    p_h = max(int(p_bbox[3] * canvas_h), 1)
    p_align = product_spec.get("align", "bottom")

    try:
        product_fit = fit_into_zone(product_image, (p_w, p_h), align=p_align)
    except Exception as e:  # noqa: BLE001
        return ComposeResult(success=False, error=f"商品フィット失敗: {e}")

    p_shadow = _shadow_config_from_dict(design.get("product_shadow") or {})
    bg = paste_with_floor_shadow(bg, product_fit, (p_x, p_y), shadow_config=p_shadow)

    # ─── Card ───
    logo = logo_image
    if logo is None and logo_path:
        try:
            with Image.open(logo_path) as opened:
                opened.load()
                logo = opened.copy()
        except Exception as e:  # noqa: BLE001
            return ComposeResult(success=False, error=f"ロゴ読込失敗: {e}")
    if logo is None:
        # デフォルトパスで再試行
        default_path = _PROJECT_ROOT / "assets" / "monohonpo_logo_transparent.png"
        if default_path.exists():
            with Image.open(default_path) as opened:
                opened.load()
                logo = opened.copy()
        else:
            return ComposeResult(success=False, error="ロゴが指定されず、デフォルトも不在")

    card_spec = design.get("card") or {}
    c_bbox = card_spec.get("bbox_ratio") or [0.72, 0.48, 0.22, 0.14]
    c_x = int(c_bbox[0] * canvas_w)
    c_y = int(c_bbox[1] * canvas_h)
    c_w = max(int(c_bbox[2] * canvas_w), 1)
    c_h = max(int(c_bbox[3] * canvas_h), 1)
    tilt = float(card_spec.get("tilt_strength", 0.035))
    rotation = float(card_spec.get("rotation_deg", 0))

    try:
        card = render_logo_card(logo, (c_w, c_h), tilt_strength=tilt)
        if rotation != 0:
            card = rotate_with_alpha(card, rotation)
            # 回転で画像サイズが拡張されるので中央補正
            new_w, new_h = card.size
            c_x -= (new_w - c_w) // 2
            c_y -= (new_h - c_h) // 2
    except Exception as e:  # noqa: BLE001
        return ComposeResult(success=False, error=f"カード生成失敗: {e}")

    c_shadow = _shadow_config_from_dict(design.get("card_shadow") or {})
    bg = paste_with_floor_shadow(bg, card, (c_x, c_y), shadow_config=c_shadow)

    # ─── RGB 化 ───
    final = Image.new("RGB", canvas_size, (0, 0, 0))
    final.paste(bg, (0, 0), mask=bg.getchannel("A"))

    return ComposeResult(
        success=True,
        image=final,
        canvas_size=canvas_size,
        meta={
            "style": design.get("style_name"),
            "reasoning": design.get("reasoning"),
            "analysis": design.get("analysis"),
            "product_bbox_px": (p_x, p_y, p_w, p_h),
            "card_bbox_px": (c_x, c_y, c_w, c_h),
            "card_rotation": rotation,
            "bg_type": bg_type,
        },
    )
