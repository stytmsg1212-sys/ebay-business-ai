#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W10 Photoroom 連携モジュール。

Photoroom は product photography 専用の AI 画像編集サービス。
rembg + 手続き合成では到達できない studio 品質のカバー画像を得る。

提供する関数:
  - remove_background_with_photoroom : /v1/segment (背景除去のみ)
  - compose_cover_with_photoroom      : /v2/edit (背景+影+サイズ調整を全自動)

設計方針:
  - API key は環境変数 PHOTOROOM_API_KEY から取得 (.env 経由)。
  - 失敗時は fail-soft で PhotoroomResult.error に詰めて返す (例外投げない)。
  - レスポンスは PNG バイナリなのでそのまま PIL.Image として保持。
  - Photoroom template を使う場合は template_id 指定、パラメータ方式も両対応。

公式 API ドキュメント:
  - /v2/edit: https://docs.photoroom.com/image-editing-api/image-editing-v2
  - /v1/segment: https://docs.photoroom.com/remove-background-api

参考コスト (2026-04 時点):
  - /v1/segment: $0.02/call
  - /v2/edit: $0.05-0.10/call (パラメータ次第)
"""
from __future__ import annotations

import io
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

import httpx
import numpy as np
from PIL import Image

try:
    from dotenv import load_dotenv
    _ENV = Path(__file__).resolve().parent.parent / ".env"
    if _ENV.exists():
        load_dotenv(_ENV)
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Photoroom API エンドポイント (公式)
PHOTOROOM_EDIT_ENDPOINT = "https://image-api.photoroom.com/v2/edit"
PHOTOROOM_SEGMENT_ENDPOINT = "https://sdk.photoroom.com/v1/segment"

ImageSource = Union[str, bytes, Path, Image.Image]


@dataclass
class PhotoroomResult:
    """Photoroom API 1 回の呼出結果。"""
    success: bool
    image: Optional[Image.Image] = None
    error: Optional[str] = None
    raw_bytes: Optional[bytes] = None
    http_status: Optional[int] = None
    meta: dict = field(default_factory=dict)


def _get_api_key() -> Optional[str]:
    """PHOTOROOM_API_KEY を取得。"""
    return os.environ.get("PHOTOROOM_API_KEY")


def _to_bytes(source: ImageSource) -> bytes:
    """画像入力を送信用 bytes に正規化。HEIC は PNG にも変換。"""
    if isinstance(source, Image.Image):
        buf = io.BytesIO()
        # RGBA は PNG で保存、RGB は JPEG でサイズ節約
        if source.mode == "RGBA":
            source.save(buf, format="PNG", optimize=True)
        else:
            source.save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    if isinstance(source, bytes):
        return source

    if isinstance(source, (str, Path)):
        p = Path(source)
        if not p.exists():
            raise FileNotFoundError(f"ファイル不在: {p}")
        # HEIC は Photoroom が直接受け付けない可能性、PIL 経由で変換
        if p.suffix.lower() in (".heic", ".heif"):
            try:
                import pillow_heif  # type: ignore
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
            with Image.open(p) as opened:
                opened.load()
                img = opened.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=95)
            return buf.getvalue()
        return p.read_bytes()

    raise TypeError(f"未対応の入力型: {type(source)}")


def remove_background_with_photoroom(
    source: ImageSource,
    *,
    timeout: float = 60.0,
) -> PhotoroomResult:
    """Photoroom /v1/segment で背景除去のみ実行。RGBA PNG を返す。"""
    api_key = _get_api_key()
    if not api_key:
        return PhotoroomResult(success=False, error="PHOTOROOM_API_KEY 未設定")

    try:
        image_bytes = _to_bytes(source)
    except Exception as e:  # noqa: BLE001
        return PhotoroomResult(success=False, error=f"入力変換失敗: {e}")

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                PHOTOROOM_SEGMENT_ENDPOINT,
                headers={"x-api-key": api_key},
                files={"image_file": ("image.png", image_bytes, "image/png")},
            )
    except httpx.HTTPError as e:
        return PhotoroomResult(success=False, error=f"HTTP 失敗: {e}")

    if resp.status_code != 200:
        # エラー本文を拾う (JSON なら)
        body = resp.text[:500]
        return PhotoroomResult(
            success=False,
            error=f"Photoroom /segment status={resp.status_code} body={body}",
            http_status=resp.status_code,
        )

    try:
        img = Image.open(io.BytesIO(resp.content))
        img.load()
    except Exception as e:  # noqa: BLE001
        return PhotoroomResult(
            success=False,
            error=f"PNG parse 失敗: {e}",
            http_status=resp.status_code,
            raw_bytes=resp.content,
        )

    return PhotoroomResult(
        success=True,
        image=img,
        raw_bytes=resp.content,
        http_status=resp.status_code,
    )


# 奥行 shading デフォルトプリセット (2026-04-23 確定):
#   background #c0c0c0 + top_darken 14% + vignette 25% を後処理適用。
#   「S3 strong depth」として Agilent/Leica で検証済。
DEPTH_STRONG = {
    "top_darken": 0.14,
    "top_fade": 0.60,
    "vignette_strength": 0.25,
    "vignette_radius": 0.60,
}
DEPTH_MEDIUM = {
    "top_darken": 0.08,
    "top_fade": 0.55,
    "vignette_strength": 0.15,
    "vignette_radius": 0.70,
}
DEPTH_OFF = None


def apply_depth_shading(
    img: Image.Image,
    preset: Optional[dict] = None,
) -> Image.Image:
    """商品写真に奥行感を演出する後処理.

    Photoroom 出力 (平面) に PIL で (a) 上部を暗くする linear gradient +
    (b) 四隅を暗くする radial vignette を合成する。光源の概念を追加して
    studio product photography の立体感を出す。

    Args:
        img: Photoroom /v2/edit 直後の RGB Image (1600x1600 想定).
        preset: DEPTH_STRONG / DEPTH_MEDIUM などの dict。None で無効化.

    Returns:
        同サイズの RGB Image。preset=None なら入力そのまま。
    """
    if preset is None:
        return img

    rgb = img.convert("RGB")
    w, h = rgb.size
    arr = np.array(rgb).astype(np.float32) / 255.0

    # 上部 gradient
    top_darken = preset.get("top_darken", 0.0)
    top_fade = preset.get("top_fade", 0.55)
    if top_darken > 0:
        yy = np.linspace(0, 1, h, dtype=np.float32)
        mask = np.clip((top_fade - yy) / top_fade, 0, 1) * top_darken
        arr *= (1.0 - mask[:, None, None])

    # radial vignette
    vstrength = preset.get("vignette_strength", 0.0)
    vradius = preset.get("vignette_radius", 0.70)
    if vstrength > 0:
        cx, cy = w / 2, h / 2
        max_r = math.sqrt(cx ** 2 + cy ** 2)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        v_mask = np.clip((dist - max_r * vradius) / (max_r * (1 - vradius)), 0, 1) ** 2
        arr *= (1.0 - v_mask[..., None] * vstrength)

    return Image.fromarray((arr * 255).clip(0, 255).astype(np.uint8))


def compose_cover_with_photoroom(
    source: ImageSource,
    *,
    background_color: str = "#c0c0c0",
    shadow_mode: str = "ai.soft",
    lighting_mode: str = "ai.auto",
    padding: float = 0.08,
    output_size: str = "1600x1600",
    position: str = "center",
    template_id: Optional[str] = None,
    extra_params: Optional[dict] = None,
    depth_preset: Optional[dict] = DEPTH_STRONG,
    timeout: float = 120.0,
) -> PhotoroomResult:
    """Photoroom /v2/edit でカバー画像を合成。

    Args:
        source: 商品画像 (背景除去前でも OK、Photoroom 側で自動)。
        background_color: 背景色 hex。`#RRGGBB` 形式。
        shadow_mode: "ai.soft" / "ai.hard" / "ai.floating" / "none"。
        lighting_mode: "ai.auto" / "none"。
        padding: 0.0-1.0、キャンバスに対する余白比率。
        output_size: "WxH" 形式。eBay EPS 向けは "1600x1600" 推奨。
        position: "center" / "bottom" / "top"。商品の配置。
        template_id: Photoroom エディタで保存したテンプレ ID (指定時は他 param より優先)。
        extra_params: 追加の API パラメータを `"key": str(value)` 形式で渡せる。
            例: {"background.prompt": "minimal japanese studio"} で AI 背景生成。

    Returns:
        PhotoroomResult。image は RGB 合成済 PIL.Image。
    """
    api_key = _get_api_key()
    if not api_key:
        return PhotoroomResult(success=False, error="PHOTOROOM_API_KEY 未設定")

    try:
        image_bytes = _to_bytes(source)
    except Exception as e:  # noqa: BLE001
        return PhotoroomResult(success=False, error=f"入力変換失敗: {e}")

    # multipart form 構築
    data: dict[str, Any] = {
        "outputSize": output_size,
        "padding": str(padding),
        "position": position,
    }
    if template_id:
        data["templateId"] = template_id
    else:
        data["background.color"] = background_color
        data["shadow.mode"] = shadow_mode
        if lighting_mode:
            data["lighting.mode"] = lighting_mode
    if extra_params:
        for k, v in extra_params.items():
            data[k] = str(v)

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                PHOTOROOM_EDIT_ENDPOINT,
                headers={"x-api-key": api_key, "Accept": "image/png"},
                files={"imageFile": ("image.png", image_bytes, "image/png")},
                data=data,
            )
    except httpx.HTTPError as e:
        return PhotoroomResult(success=False, error=f"HTTP 失敗: {e}")

    if resp.status_code != 200:
        body = resp.text[:1000]
        logger.warning("Photoroom /edit failed: %d / %s", resp.status_code, body)
        return PhotoroomResult(
            success=False,
            error=f"Photoroom /edit status={resp.status_code} body={body}",
            http_status=resp.status_code,
        )

    try:
        img = Image.open(io.BytesIO(resp.content))
        img.load()
    except Exception as e:  # noqa: BLE001
        return PhotoroomResult(
            success=False,
            error=f"PNG parse 失敗: {e}",
            http_status=resp.status_code,
            raw_bytes=resp.content,
        )

    # 奥行 shading 後処理 (depth_preset が指定されていれば)
    if depth_preset is not None:
        try:
            img = apply_depth_shading(img, depth_preset)
        except Exception as e:  # noqa: BLE001
            logger.warning("depth shading failed (fail-soft): %s", e)

    return PhotoroomResult(
        success=True,
        image=img,
        raw_bytes=resp.content,
        http_status=resp.status_code,
        meta={
            "params": data,
            "size": img.size,
            "depth_preset": depth_preset,
        },
    )


def overlay_logo_card(
    base_image: Image.Image,
    card_image: Image.Image,
    *,
    position: str = "bottom_right",
    card_width_ratio: float = 0.20,
    margin_ratio: float = 0.04,
) -> Image.Image:
    """Photoroom 出力に MonoHonpo カードを後付けオーバーレイする (template 未使用時)。

    将来 Photoroom エディタで MonoHonpo ブランドテンプレートを作成すれば
    この関数は不要になる。暫定的に local composite。

    Args:
        base_image: Photoroom 合成済 PIL.Image (RGB)。
        card_image: ロゴ入り name card (RGBA 透過推奨)。
        position: "bottom_right" / "bottom_left" / "top_right" / "top_left"。
        card_width_ratio: base canvas 幅に対する card 幅比率。
        margin_ratio: 端からの余白比率。

    Returns:
        カード合成済 RGB PIL.Image。
    """
    base = base_image.convert("RGBA")
    bw, bh = base.size
    target_w = int(bw * card_width_ratio)
    # card の元 aspect 維持
    cw, ch = card_image.size
    scale = target_w / cw
    target_h = max(int(ch * scale), 1)

    card_resized = card_image.convert("RGBA").resize((target_w, target_h), Image.LANCZOS)

    margin = int(min(bw, bh) * margin_ratio)
    if position == "bottom_right":
        pos = (bw - target_w - margin, bh - target_h - margin)
    elif position == "bottom_left":
        pos = (margin, bh - target_h - margin)
    elif position == "top_right":
        pos = (bw - target_w - margin, margin)
    elif position == "top_left":
        pos = (margin, margin)
    else:
        raise ValueError(f"不正な position: {position}")

    base.alpha_composite(card_resized, pos)
    return base.convert("RGB")
