#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W10 Phase C: カバー画像合成用の PIL 描画プリミティブ。

提供する関数:
  - render_gradient_background  : Studio seamless paper 風の縦グラデーション
  - render_logo_card            : 白い名刺カード (ロゴを中央配置, 角丸, 細ボーダー)
  - render_floor_shadow         : 床に落ちる楕円ぼかし影 (studio product photo 風)
  - fit_into_zone               : 商品画像をゾーンに aspect 保持リサイズ
  - paste_with_floor_shadow     : 床影を先に置いてから対象をペーストするヘルパ

設計方針:
  - 全関数は純粋 (入力 PIL.Image を mutate しない)。コピーして返す。
  - drop shadow ではなく floor shadow を採用 (studio product photo 風味)。
    床影の表現: alpha マスクを縦方向に圧縮 → 下方向にオフセット → ぼかし → 暗色合成。
  - カラー指定は RGB tuple (設定 JSON 互換)。alpha は別引数。
  - 依存は Pillow のみ、rembg / onnxruntime は引かない。
    → Phase D 以降の image_composer と独立してテスト可能。

正源: docs/image_processing.md / W10 設計レビュー (2026-04-23)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class ShadowConfig:
    """Modern studio photo 風の二層シャドウ設定。

    現代的な商品写真は "contact shadow" (鋭く濃い近接影) と
    "ambient shadow" (広く淡い全体影) の 2 層で構成される。
    片方だけでは古い 90s PowerPoint 感が出るため必ず両方合成する。
    """
    # Contact shadow: 物体の根元 (地面との接点) を鋭く濃く
    contact_blur: int = 5
    contact_offset_y: int = 6
    contact_opacity: float = 0.55
    contact_squish: float = 0.10

    # Ambient shadow: 全体をふんわり広く
    ambient_blur: int = 55
    ambient_offset_y: int = 22
    ambient_opacity: float = 0.18
    ambient_squish: float = 0.28

    # 共通: 影の色 (純黒より warm gray の方が馴染む)
    shadow_color: RGB = (25, 24, 22)


def render_gradient_background(
    size: tuple[int, int],
    top_color: RGB,
    bottom_color: RGB,
    *,
    direction: str = "vertical",
) -> Image.Image:
    """上下 (or 左右) に色が推移するグラデーション背景を生成。

    Args:
        size: (width, height) px。
        top_color: 開始色 (RGB)。direction=vertical なら最上端の色。
        bottom_color: 終了色。direction=vertical なら最下端の色。
        direction: "vertical" / "horizontal"。

    Returns:
        RGB モードの PIL.Image。
    """
    if direction not in ("vertical", "horizontal"):
        raise ValueError(f"direction は 'vertical' or 'horizontal': {direction!r}")

    w, h = size
    if w <= 0 or h <= 0:
        raise ValueError(f"size は正の値: {size}")

    # 1 次元ストリップを作ってから resize で引き伸ばす。
    # 2.56M 回の Python ループを避けるための最適化 (1600x1600 で数秒→数ms)。
    if direction == "vertical":
        strip = Image.new("RGB", (1, h))
        sp = strip.load()
        length = h
        for i in range(h):
            t = i / max(length - 1, 1)
            sp[0, i] = (
                int(top_color[0] * (1 - t) + bottom_color[0] * t),
                int(top_color[1] * (1 - t) + bottom_color[1] * t),
                int(top_color[2] * (1 - t) + bottom_color[2] * t),
            )
        return strip.resize((w, h), Image.NEAREST)

    # horizontal
    strip = Image.new("RGB", (w, 1))
    sp = strip.load()
    length = w
    for i in range(w):
        t = i / max(length - 1, 1)
        sp[i, 0] = (
            int(top_color[0] * (1 - t) + bottom_color[0] * t),
            int(top_color[1] * (1 - t) + bottom_color[1] * t),
            int(top_color[2] * (1 - t) + bottom_color[2] * t),
        )
    return strip.resize((w, h), Image.NEAREST)


def render_radial_gradient_background(
    size: tuple[int, int],
    center_color: RGB,
    edge_color: RGB,
    *,
    vignette_strength: float = 0.3,
) -> Image.Image:
    """中央→周辺に推移する放射状グラデーション。studio vignette 風。

    vignette_strength: 0.0-1.0。低いほどほぼ center_color のみ、
    高いほど周辺が edge_color に深く沈む。
    """
    import numpy as np

    w, h = size
    if w <= 0 or h <= 0:
        raise ValueError(f"size は正の値: {size}")

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2.0, h / 2.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    max_dist = np.sqrt(cx ** 2 + cy ** 2)
    t = np.clip(dist / max_dist, 0.0, 1.0) * vignette_strength
    # t=0 → center, t=1 → edge

    r = center_color[0] * (1 - t) + edge_color[0] * t
    g = center_color[1] * (1 - t) + edge_color[1] * t
    b = center_color[2] * (1 - t) + edge_color[2] * t
    arr = np.stack([r, g, b], axis=-1).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def _apply_perspective_tilt(img: Image.Image, tilt_strength: float) -> Image.Image:
    """カードに軽い 3D 傾きを与える。tilt_strength=0.0 で無変換。

    カードが「上奥に少し倒れた」感じ: 上辺をやや内側に縮める。
    tilt_strength=0.05 なら 5% 分 (1600px で ~80px) 上辺を内側に。
    """
    if tilt_strength <= 0.0:
        return img

    w, h = img.size
    dx = int(w * tilt_strength)
    dy = int(h * tilt_strength * 0.3)  # わずかに上方へ短縮

    # 4 点 perspective: (src) top-left, top-right, bottom-right, bottom-left
    # → (dst) 上辺を dx だけ内側に入れる = 奥行き感
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    dst = [(dx, dy), (w - dx, dy), (w, h), (0, h)]

    coeffs = _find_perspective_coeffs(dst, src)
    tilted = img.transform(
        (w, h), Image.PERSPECTIVE, coeffs,
        resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0),
    )
    return tilted


def _find_perspective_coeffs(dst_corners, src_corners):
    """PIL PERSPECTIVE 用の 8 係数を numpy で解く。

    PIL は dst 側から src 側への逆変換係数を要求する。
    """
    import numpy as np

    matrix = []
    for (sx, sy), (dx, dy) in zip(src_corners, dst_corners):
        matrix.append([sx, sy, 1, 0, 0, 0, -dx * sx, -dx * sy])
        matrix.append([0, 0, 0, sx, sy, 1, -dy * sx, -dy * sy])
    A = np.array(matrix, dtype=np.float64)
    B = np.array([d for pair in dst_corners for d in pair], dtype=np.float64)
    res = np.linalg.solve(A, B)
    return res.tolist()


def render_logo_card(
    logo: Image.Image,
    card_size: tuple[int, int],
    *,
    fill_color: RGB = (252, 252, 250),
    border_color: RGB = (220, 218, 212),
    border_width: int = 1,
    corner_radius: int = 8,
    logo_padding_ratio: float = 0.15,
    tilt_strength: float = 0.035,  # 3D 傾き。0.0 でフラット
    thickness_px: int = 4,          # 下辺の「厚み」シミュレーション
    rim_strength: float = 0.18,     # 0〜1、傾いてる側のエッジ暗色化
) -> Image.Image:
    """白背景の名刺カードを作り、ロゴを中央に配置する。

    Args:
        logo: 透過 PNG 推奨の RGBA/RGB PIL.Image。
        card_size: (width, height) px。
        fill_color: カード本体色 (RGB)。
        border_color: 枠線色 (RGB)。
        border_width: 枠線の太さ。0 ならボーダーなし。
        corner_radius: 角丸 (px)。0 で矩形。
        logo_padding_ratio: カードサイズに対するロゴ周囲 padding 比率 (0-0.4)。

    Returns:
        RGBA PIL.Image (card_size)。
    """
    if not (0.0 <= logo_padding_ratio < 0.5):
        raise ValueError(f"logo_padding_ratio は 0 <= p < 0.5: {logo_padding_ratio}")

    w, h = card_size
    if w <= 0 or h <= 0:
        raise ValueError(f"card_size は正の値: {card_size}")

    # まずカード本体 (RGBA、角丸、枠線)
    card = Image.new("RGBA", card_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    if corner_radius > 0:
        draw.rounded_rectangle(
            [(0, 0), (w - 1, h - 1)],
            radius=corner_radius,
            fill=fill_color + (255,),
        )
        if border_width > 0:
            draw.rounded_rectangle(
                [(0, 0), (w - 1, h - 1)],
                radius=corner_radius,
                outline=border_color + (255,),
                width=border_width,
            )
    else:
        draw.rectangle([(0, 0), (w - 1, h - 1)], fill=fill_color + (255,))
        if border_width > 0:
            draw.rectangle(
                [(0, 0), (w - 1, h - 1)],
                outline=border_color + (255,),
                width=border_width,
            )

    # ロゴのリサイズ (card に対して padding を確保)
    pad = int(min(w, h) * logo_padding_ratio)
    max_logo_w = max(w - 2 * pad, 1)
    max_logo_h = max(h - 2 * pad, 1)

    logo_rgba = logo.convert("RGBA")
    lw, lh = logo_rgba.size
    scale = min(max_logo_w / lw, max_logo_h / lh)
    if scale < 1.0:
        new_size = (max(int(lw * scale), 1), max(int(lh * scale), 1))
        logo_rgba = logo_rgba.resize(new_size, Image.LANCZOS)

    # 中央配置
    lw2, lh2 = logo_rgba.size
    pos = ((w - lw2) // 2, (h - lh2) // 2)
    card.alpha_composite(logo_rgba, pos)

    # 上辺側の rim shadow: カードが奥に傾いているので上辺が遠く = 少し暗い
    if rim_strength > 0.0:
        rim_height = max(int(h * 0.12), 2)
        rim_overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        rim_draw = ImageDraw.Draw(rim_overlay)
        for i in range(rim_height):
            alpha_val = int(rim_strength * 255 * (1 - i / rim_height))
            rim_draw.line([(0, i), (w - 1, i)], fill=(0, 0, 0, alpha_val))
        # カード内のみ適用 (角丸外には影響させない): card の alpha を mask に
        card_mask = card.getchannel("A")
        rim_clipped = Image.composite(
            rim_overlay,
            Image.new("RGBA", (w, h), (0, 0, 0, 0)),
            card_mask,
        )
        card.alpha_composite(rim_clipped)

    # 下辺の厚み: 数 px グラデーションで紙の厚さを演出
    if thickness_px > 0:
        th = min(thickness_px, max(h // 8, 2))
        thickness_overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        td = ImageDraw.Draw(thickness_overlay)
        for i in range(th):
            alpha_val = int(50 * (i + 1) / th)
            td.line([(0, h - 1 - i), (w - 1, h - 1 - i)], fill=(0, 0, 0, alpha_val))
        card_mask = card.getchannel("A")
        thickness_clipped = Image.composite(
            thickness_overlay,
            Image.new("RGBA", (w, h), (0, 0, 0, 0)),
            card_mask,
        )
        card.alpha_composite(thickness_clipped)

    # 3D 傾き適用 (最後に)
    if tilt_strength > 0.0:
        card = _apply_perspective_tilt(card, tilt_strength)

    return card


def rotate_with_alpha(img: Image.Image, angle_deg: float) -> Image.Image:
    """RGBA 画像を任意角度で回転する (bbox 拡張付き、透明パディング)。"""
    if angle_deg == 0:
        return img
    return img.rotate(angle_deg, resample=Image.BICUBIC, expand=True)


def _render_single_shadow_layer(
    subject: Image.Image,
    *,
    blur: int,
    offset_y: int,
    opacity: float,
    squish: float,
    shadow_color: RGB,
    lateral_pad: int,
) -> Image.Image:
    """1 枚の影 layer を生成。lateral_pad だけ左右に余白を広げる (blur 用)。"""
    sw, sh = subject.size
    extra_h = blur * 2 + offset_y + 4
    extra_w = lateral_pad * 2
    layer_w = sw + extra_w
    layer_h = sh + extra_h
    layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))

    alpha = subject.getchannel("A")
    squished_h = max(int(sh * squish), 2)
    squished = alpha.resize((sw, squished_h), Image.LANCZOS)

    shadow_plate = Image.new("RGBA", (sw, squished_h), shadow_color + (0,))
    shadow_plate.putalpha(squished)

    shadow_top = sh - squished_h // 2 + offset_y
    layer.alpha_composite(shadow_plate, (lateral_pad, shadow_top))

    if blur > 0:
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))

    if opacity < 1.0:
        la = layer.getchannel("A")
        la = la.point(lambda v: int(v * opacity))
        layer.putalpha(la)

    return layer


def render_floor_shadow(
    subject: Image.Image,
    *,
    shadow_config: ShadowConfig = ShadowConfig(),
) -> Image.Image:
    """モダン二層シャドウ (contact + ambient) を 1 枚の RGBA layer として返す。

    戻り値のサイズは subject より**左右・下方に余白が張り出した**大きさ。
    paste_with_floor_shadow は戻り値の `lateral_pad` オフセットを考慮して貼る。
    """
    if subject.mode != "RGBA":
        raise ValueError("subject は RGBA モードである必要がある")

    cfg = shadow_config
    # ambient の blur 分だけ左右に余白確保 (ぼかしが被写体幅を超えるため)
    lateral_pad = max(cfg.ambient_blur, cfg.contact_blur) * 2

    ambient = _render_single_shadow_layer(
        subject,
        blur=cfg.ambient_blur,
        offset_y=cfg.ambient_offset_y,
        opacity=cfg.ambient_opacity,
        squish=cfg.ambient_squish,
        shadow_color=cfg.shadow_color,
        lateral_pad=lateral_pad,
    )

    contact = _render_single_shadow_layer(
        subject,
        blur=cfg.contact_blur,
        offset_y=cfg.contact_offset_y,
        opacity=cfg.contact_opacity,
        squish=cfg.contact_squish,
        shadow_color=cfg.shadow_color,
        lateral_pad=lateral_pad,
    )

    # Ambient が下、Contact が上
    combined = Image.new("RGBA", ambient.size, (0, 0, 0, 0))
    combined.alpha_composite(ambient)
    combined.alpha_composite(contact)
    # lateral_pad を保存してペースト側で参照する。
    combined.info["lateral_pad"] = lateral_pad
    return combined


def fit_into_zone(
    img: Image.Image,
    zone_size: tuple[int, int],
    *,
    align: str = "center",
) -> Image.Image:
    """画像を zone にアスペクト比保持でフィットさせ、zone サイズの RGBA を返す。

    入力が zone より大きければ縮小、小さければ拡大 (LANCZOS)。
    align は zone 内の配置: "center" / "bottom" / "top" / "left" / "right"。
    zone 外の余白は透明。
    """
    if align not in ("center", "bottom", "top", "left", "right"):
        raise ValueError(f"align invalid: {align}")

    zw, zh = zone_size
    if zw <= 0 or zh <= 0:
        raise ValueError(f"zone_size は正の値: {zone_size}")

    src = img.convert("RGBA")
    iw, ih = src.size
    scale = min(zw / iw, zh / ih)
    new_w, new_h = max(int(iw * scale), 1), max(int(ih * scale), 1)
    resized = src.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGBA", zone_size, (0, 0, 0, 0))
    if align == "center":
        pos = ((zw - new_w) // 2, (zh - new_h) // 2)
    elif align == "bottom":
        pos = ((zw - new_w) // 2, zh - new_h)
    elif align == "top":
        pos = ((zw - new_w) // 2, 0)
    elif align == "left":
        pos = (0, (zh - new_h) // 2)
    else:  # right
        pos = (zw - new_w, (zh - new_h) // 2)
    canvas.alpha_composite(resized, pos)
    return canvas


def paste_with_floor_shadow(
    canvas: Image.Image,
    subject: Image.Image,
    position: tuple[int, int],
    *,
    shadow_config: Optional[ShadowConfig] = None,
) -> Image.Image:
    """subject を canvas の指定位置に貼り付ける。modern 2-layer 影は自動で敷く。

    Args:
        canvas: 貼り付け先 (RGBA)。破壊しない (コピーして返す)。
        subject: RGBA 被写体。
        position: (x, y) subject の左上を canvas のどこに置くか。
        shadow_config: None なら影なし。

    Returns:
        コピー後に貼り付け済みの新しい RGBA Image。
    """
    if canvas.mode != "RGBA":
        canvas = canvas.convert("RGBA")
    if subject.mode != "RGBA":
        subject = subject.convert("RGBA")

    out = canvas.copy()
    x, y = position

    if shadow_config is not None:
        shadow_layer = render_floor_shadow(subject, shadow_config=shadow_config)
        lateral_pad = shadow_layer.info.get("lateral_pad", 0)
        # shadow_layer は lateral_pad 分左右に余白がある → x から引いて貼る
        out.alpha_composite(shadow_layer, (x - lateral_pad, y))

    out.alpha_composite(subject, (x, y))
    return out
