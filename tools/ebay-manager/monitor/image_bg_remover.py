#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W10 Phase B: rembg による背景除去ラッパ。

設計方針:
  - 入力は URL / ファイルパス / bytes / PIL.Image を全て受け付ける。
    呼び出し側は ScrapedProduct.image_urls を渡すだけで動く。
  - Windows asyncio 問題と無関係。rembg は in-process onnxruntime なので
    Streamlit (SelectorEventLoop) 配下でもそのまま動く。
  - rembg session はプロセス単位で 1 回だけ生成、以降再利用 (モデルロード
    コストは 5-10 秒、推論自体は 1-3 秒/画像)。
  - 例外は全て RemovalResult.error に詰めて呼び出し側を壊さない (fail-soft)。
  - EXIF 回転を入口で必ず正規化 (iPhone 画像の横倒し問題防止)。
  - HEIC / HEIF は pillow-heif opener を登録してから Pillow で開く。

rembg モデル配置:
  settings.json の image_processing.rembg_model_dir を U2NET_HOME に設定する。
  初回呼び出し時に rembg がモデルを自動 DL する (約 176MB)。

設定キー参照 (settings.json):
  image_processing.background_removal_engine  — "rembg" のみ対応
  image_processing.rembg_model_dir            — 既定 "models"
  image_processing.rembg_model_name           — 既定 "u2net"
  image_processing.eps_min_dimension_px       — 既定 500 (eBay EPS 拒否閾値)

正源:
  docs/image_processing.md
  W10 設計レビュー (code-architect opus-4.7, 2026-04-23)
"""
from __future__ import annotations

import io
import ipaddress
import logging
import os
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union
from urllib.parse import urlparse

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# HEIC / HEIF opener を Pillow に登録 (仕入先が iPhone 由来画像の場合に必要)。
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except ImportError:
    logger.warning("pillow-heif 未インストール。HEIC 画像は未対応。")

ImageSource = Union[str, bytes, Path, Image.Image]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# プロセス単位の session cache。key = (engine, model_name)
# Streamlit worker + scheduler thread の同時 new_session を防ぐため Lock で保護。
_SESSION_CACHE: dict[tuple[str, str], Any] = {}
_SESSION_LOCK = threading.Lock()

# SSRF 防御用: http/https 以外の URL スキーム拒否、private/link-local 拒否。
_ALLOWED_URL_SCHEMES: tuple[str, ...] = ("http", "https")


@dataclass
class RemovalResult:
    """背景除去 1 件の結果。success=False 時は image=None, error に理由。"""
    success: bool
    image: Optional[Image.Image] = None
    error: Optional[str] = None
    source_kind: str = ""  # "url" / "path" / "bytes" / "pil"
    original_size: Optional[tuple[int, int]] = None
    output_size: Optional[tuple[int, int]] = None
    meta: dict = field(default_factory=dict)


def _coerce_settings(settings: Optional[dict]) -> dict:
    """image_processing ブロックのみ抜き出す。無ければ defaults。"""
    defaults = {
        "background_removal_engine": "rembg",
        "rembg_model_dir": "models",
        # isnet-general-use は u2net より新しく、境界品質が大幅に良い。
        # さらに alpha_matting を有効にすると pymatting が後処理で境界精度を上げる。
        "rembg_model_name": "isnet-general-use",
        "rembg_alpha_matting": True,
        "rembg_alpha_matting_foreground_threshold": 240,
        "rembg_alpha_matting_background_threshold": 20,
        "rembg_alpha_matting_erode_size": 10,
        "eps_min_dimension_px": 500,
    }
    if not settings:
        return defaults
    ip = settings.get("image_processing") or {}
    merged = dict(defaults)
    for k in defaults:
        if k in ip and ip[k] is not None:
            merged[k] = ip[k]
    return merged


def _resolve_model_dir(model_dir_setting: str) -> Path:
    """settings の model_dir を絶対パスに解決。"""
    p = Path(model_dir_setting)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_session(engine: str, model_name: str, model_dir: Path) -> Any:
    """rembg session を取得 (double-checked locking でスレッド安全)。"""
    key = (engine, model_name)
    cached = _SESSION_CACHE.get(key)
    if cached is not None:
        return cached

    if engine != "rembg":
        raise ValueError(f"未対応の background_removal_engine: {engine}")

    with _SESSION_LOCK:
        # Lock 取得後に再チェック (別スレッドが先に生成済みかもしれない)
        cached = _SESSION_CACHE.get(key)
        if cached is not None:
            return cached

        os.environ["U2NET_HOME"] = str(model_dir)
        from rembg import new_session  # 遅延 import (起動時間短縮)

        logger.info("rembg session 生成: engine=%s model=%s dir=%s", engine, model_name, model_dir)
        session = new_session(model_name)
        _SESSION_CACHE[key] = session
        return session


def _validate_url_for_ssrf(url: str) -> Optional[str]:
    """URL が SSRF 脅威に該当しないか検査。問題あればエラー文字列を返す。

    防御内容:
      - スキーム allowlist (http/https のみ)
      - hostname の無い URL 拒否
      - private / loopback / link-local / reserved IP 拒否
      - hostname が IP でなくても DNS 解決結果が private なら拒否

    注意: DNS を引くので若干遅延あるが、信頼境界外の URL を扱う以上は必須。
    """
    try:
        parsed = urlparse(url)
    except Exception as e:  # noqa: BLE001  urlparse は基本失敗しないが保険
        return f"URL 解析失敗: {e}"

    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        return f"許可されていない URL スキーム: {parsed.scheme!r}"

    host = parsed.hostname
    if not host:
        return "URL に hostname がない"

    # hostname が直接 IP の場合
    try:
        ip_obj = ipaddress.ip_address(host)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved:
            return f"内部アドレスは拒否: {host}"
        return None
    except ValueError:
        pass  # IP ではなく DNS 名

    # DNS 解決 → 戻り値全てを検査
    try:
        addrs = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return f"DNS 解決失敗: {e}"

    for entry in addrs:
        sockaddr = entry[4]
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved:
            return f"内部アドレスに解決される hostname: {host} -> {ip_str}"

    return None


def _load_image(source: ImageSource) -> tuple[Image.Image, str]:
    """入力を PIL.Image に正規化。戻り値は (image, source_kind)。"""
    if isinstance(source, Image.Image):
        return source.copy(), "pil"

    if isinstance(source, bytes):
        return Image.open(io.BytesIO(source)), "bytes"

    if isinstance(source, Path) or (isinstance(source, str) and not source.startswith(("http://", "https://"))):
        p = Path(source)
        if not p.exists():
            raise FileNotFoundError(f"画像ファイルなし: {p}")
        return Image.open(p), "path"

    # URL
    if isinstance(source, str):
        ssrf_err = _validate_url_for_ssrf(source)
        if ssrf_err:
            raise ValueError(f"URL SSRF 検査失敗: {ssrf_err}")

        import httpx  # 既存依存 (requirements.txt)

        resp = httpx.get(source, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)), "url"

    raise TypeError(f"想定外の入力型: {type(source)}")


def _prepare_image(img: Image.Image) -> Image.Image:
    """EXIF 回転を適用し、alpha を持つ形式に揃える。"""
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img


def _validate_min_dimension(img: Image.Image, min_px: int) -> Optional[str]:
    """eBay EPS 拒否サイズをチェック。問題あればエラー文字列、なければ None。"""
    w, h = img.size
    if min(w, h) < min_px:
        return f"画像サイズ {w}x{h} は eBay EPS 最小 {min_px}px 未満"
    return None


def remove_background(
    source: ImageSource,
    *,
    settings: Optional[dict] = None,
) -> RemovalResult:
    """1 枚の画像から背景を除去する。例外は投げず RemovalResult.error で返す。

    Args:
        source: URL / ファイルパス / bytes / PIL.Image。
        settings: 全 settings dict (image_processing ブロックだけ参照)。

    Returns:
        RemovalResult。success=True なら image は RGBA の PIL.Image。
    """
    cfg = _coerce_settings(settings)

    try:
        img, source_kind = _load_image(source)
    except Exception as e:  # noqa: BLE001  fail-soft 方針
        logger.warning("画像読込失敗: %s / %s", type(e).__name__, e)
        return RemovalResult(success=False, error=f"読込失敗: {e}", source_kind="")

    original_size = img.size

    try:
        img = _prepare_image(img)
    except Exception as e:  # noqa: BLE001
        logger.warning("画像前処理失敗: %s", e)
        return RemovalResult(
            success=False, error=f"前処理失敗: {e}",
            source_kind=source_kind, original_size=original_size,
        )

    min_dim_err = _validate_min_dimension(img, cfg["eps_min_dimension_px"])
    if min_dim_err:
        logger.info("最小寸法違反: %s", min_dim_err)
        return RemovalResult(
            success=False, error=min_dim_err,
            source_kind=source_kind, original_size=original_size,
        )

    try:
        model_dir = _resolve_model_dir(cfg["rembg_model_dir"])
        session = _get_session(cfg["background_removal_engine"], cfg["rembg_model_name"], model_dir)
    except Exception as e:  # noqa: BLE001
        logger.exception("rembg session 生成失敗")
        return RemovalResult(
            success=False, error=f"session 生成失敗: {e}",
            source_kind=source_kind, original_size=original_size,
        )

    try:
        from rembg import remove  # 遅延 import

        remove_kwargs: dict[str, Any] = {"session": session}
        if cfg.get("rembg_alpha_matting"):
            # alpha matting 有効化: pymatting が境界のふわっと感を緻密に再計算する。
            # 速度は 2-5 倍遅くなるが品質が大幅向上。studio photo 相当の境界になる。
            remove_kwargs["alpha_matting"] = True
            remove_kwargs["alpha_matting_foreground_threshold"] = int(
                cfg.get("rembg_alpha_matting_foreground_threshold", 240)
            )
            remove_kwargs["alpha_matting_background_threshold"] = int(
                cfg.get("rembg_alpha_matting_background_threshold", 20)
            )
            remove_kwargs["alpha_matting_erode_size"] = int(
                cfg.get("rembg_alpha_matting_erode_size", 10)
            )
        output = remove(img, **remove_kwargs)
    except Exception as e:  # noqa: BLE001
        logger.exception("rembg 推論失敗")
        return RemovalResult(
            success=False, error=f"推論失敗: {e}",
            source_kind=source_kind, original_size=original_size,
        )

    return RemovalResult(
        success=True,
        image=output,
        source_kind=source_kind,
        original_size=original_size,
        output_size=output.size,
        meta={
            "engine": cfg["background_removal_engine"],
            "model": cfg["rembg_model_name"],
        },
    )


def remove_background_batch(
    sources: list[ImageSource],
    *,
    settings: Optional[dict] = None,
    on_progress: Optional[Callable[[int, int, RemovalResult], None]] = None,
) -> list[RemovalResult]:
    """複数画像を順次処理する。各画像完了時に on_progress(index, total, result) を呼ぶ。

    同期処理。Streamlit からは st.progress(i/n) でループ内更新する想定。
    """
    results: list[RemovalResult] = []
    total = len(sources)
    for i, src in enumerate(sources):
        r = remove_background(src, settings=settings)
        results.append(r)
        if on_progress is not None:
            try:
                on_progress(i + 1, total, r)
            except Exception:  # noqa: BLE001
                logger.exception("on_progress callback 失敗 (無視して継続)")
    return results


def clear_session_cache() -> None:
    """テスト用: session cache をリセットする。"""
    _SESSION_CACHE.clear()
