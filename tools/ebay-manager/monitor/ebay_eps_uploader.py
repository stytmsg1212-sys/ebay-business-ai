#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBay Picture Services (EPS) アップローダ.

Trading API `UploadSiteHostedPictures` を使ってローカル画像を eBay の
公式画像ストレージにアップロードし、publicly accessible な URL を返す。
取得した URL は Trading API `AddFixedPriceItem` の `PictureDetails` に
そのまま使える。

特徴:
- multipart/form-data 形式 (通常の Trading API は XML body のみ)
- XML envelope + binary attachment の 2 part 構成
- 保持期間: アップロード後 90 日 (`PictureUploadPolicy=Add` の場合)
- 並行 upload 対応 (ThreadPoolExecutor 想定)
- OAuth auto-refresh 済み token を自動使用 (2026-04-23 統合)

DB キャッシュで同じ画像の重複アップロードを防止.
"""
from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
from monitor.ebay_client import API_VERSION, TRADING_API_URL, _resolve_active_token

logger = logging.getLogger(__name__)

# 保持期間ポリシー. "Add" = 90日、"HostPictureOnly" = 365日
_UPLOAD_POLICY = "HostPictureOnly"

# 1 回の multipart request 上限. eBay は 12MB まで許容するが余裕を持って 10MB.
_MAX_FILE_BYTES = 10 * 1024 * 1024

# 対応する画像フォーマット
_SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}


@dataclass
class EpsUploadResult:
    success: bool
    eps_url: Optional[str] = None
    error: Optional[str] = None
    file_hash: Optional[str] = None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_xml_envelope(user_token: str, picture_name: str) -> str:
    """UploadSiteHostedPictures 用の XML ヘッダ部."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{user_token}</eBayAuthToken>
  </RequesterCredentials>
  <PictureName>{picture_name}</PictureName>
  <PictureSet>Supersize</PictureSet>
  <PictureUploadPolicy>{_UPLOAD_POLICY}</PictureUploadPolicy>
  <ExtensionInDays>30</ExtensionInDays>
</UploadSiteHostedPicturesRequest>"""


def upload_image_to_eps(
    image_path: Path,
    *,
    config: Optional[dict] = None,
    timeout: float = 60.0,
) -> EpsUploadResult:
    """ローカル画像を EPS にアップロードして URL を返す (キャッシュなし).

    Args:
        image_path: アップロード対象のローカルファイルパス.
        config: settings.json 相当 (credentials fallback 用).
        timeout: HTTP timeout 秒.

    Returns:
        EpsUploadResult (success, eps_url, file_hash, error).
    """
    if not isinstance(image_path, Path):
        image_path = Path(image_path)
    if not image_path.exists():
        return EpsUploadResult(success=False, error=f"ファイル不在: {image_path}")
    if image_path.suffix.lower() not in _SUPPORTED_EXT:
        return EpsUploadResult(
            success=False, error=f"未対応拡張子: {image_path.suffix}",
        )
    size = image_path.stat().st_size
    if size > _MAX_FILE_BYTES:
        return EpsUploadResult(
            success=False,
            error=f"サイズ超過: {size/1024/1024:.1f} MB > 10 MB",
        )
    if size == 0:
        return EpsUploadResult(success=False, error="空ファイル")

    creds = get_ebay_credentials(config)
    if not ebay_credentials_ok(creds):
        return EpsUploadResult(success=False, error="eBay 認証情報未設定")

    file_hash = _file_sha256(image_path)
    user_token = _resolve_active_token(creds["user_token"])
    picture_name = image_path.stem[:80]  # eBay 上限 80 文字

    xml_envelope = _build_xml_envelope(user_token, picture_name)
    mime, _ = mimetypes.guess_type(image_path.name)
    mime = mime or "image/png"

    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures",
        "X-EBAY-API-APP-NAME": creds["app_id"],
        "X-EBAY-API-DEV-NAME": creds["dev_id"],
        "X-EBAY-API-CERT-NAME": creds["cert_id"],
    }

    # eBay 側の仕様: Content-Disposition で name=XML Payload / name=image のみ
    image_bytes = image_path.read_bytes()
    files = {
        "XML Payload": (None, xml_envelope, "text/xml"),
        "dummy": (image_path.name, image_bytes, mime),
    }

    try:
        resp = httpx.post(TRADING_API_URL, headers=headers, files=files, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        return EpsUploadResult(
            success=False, error=f"HTTP 失敗: {e}", file_hash=file_hash,
        )

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return EpsUploadResult(
            success=False, error=f"XML parse 失敗: {e}", file_hash=file_hash,
        )

    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack not in ("Success", "Warning"):
        # エラー詳細
        err_msg = root.findtext("ns:Errors/ns:LongMessage", namespaces=ns) or "unknown"
        return EpsUploadResult(
            success=False, error=f"eBay error: {err_msg}", file_hash=file_hash,
        )

    # SiteHostedPictureDetails/FullURL を取得
    full_url = root.findtext(
        "ns:SiteHostedPictureDetails/ns:FullURL", namespaces=ns,
    )
    if not full_url:
        return EpsUploadResult(
            success=False, error="FullURL が返らない", file_hash=file_hash,
        )

    logger.info(f"EPS upload success: {image_path.name} -> {full_url}")
    return EpsUploadResult(success=True, eps_url=full_url, file_hash=file_hash)


def upload_images_parallel(
    paths: list[Path],
    *,
    config: Optional[dict] = None,
    max_workers: int = 3,
    use_cache: bool = True,
) -> list[EpsUploadResult]:
    """複数画像を並列アップロード. use_cache=True なら DB キャッシュ確認."""
    results: dict[int, EpsUploadResult] = {}

    if use_cache:
        # キャッシュ hit した分はスキップして先に results に格納
        cache_map = {p: _lookup_cached_url(p) for p in paths}
    else:
        cache_map = {p: None for p in paths}

    to_upload: list[tuple[int, Path]] = []
    for i, p in enumerate(paths):
        cached = cache_map.get(p)
        if cached:
            results[i] = EpsUploadResult(success=True, eps_url=cached)
            logger.debug(f"EPS cache hit: {p.name} -> {cached}")
        else:
            to_upload.append((i, p))

    if to_upload:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            fut_map = {
                pool.submit(upload_image_to_eps, p, config=config): i
                for i, p in to_upload
            }
            for fut in as_completed(fut_map):
                idx = fut_map[fut]
                try:
                    r = fut.result()
                except Exception as e:  # noqa: BLE001
                    r = EpsUploadResult(success=False, error=str(e))
                results[idx] = r
                if use_cache and r.success and r.eps_url and r.file_hash:
                    _save_to_cache(paths[idx], r.file_hash, r.eps_url)

    # 元の順序で返す
    return [results[i] for i in range(len(paths))]


# ────────── DB cache 補助 (本番 Phase D 用) ──────────

def _lookup_cached_url(path: Path) -> Optional[str]:
    """file hash でキャッシュ lookup. なければ None."""
    try:
        from monitor.database import get_conn
        h = _file_sha256(path)
        with get_conn() as c:
            c.row_factory = None
            r = c.execute(
                "SELECT eps_url FROM eps_upload_cache WHERE file_hash = ?",
                (h,),
            ).fetchone()
            return r[0] if r else None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"eps cache lookup skip: {e}")
        return None


def _save_to_cache(path: Path, file_hash: str, eps_url: str) -> None:
    """アップロード成功を DB cache に保存."""
    try:
        from monitor.database import get_conn
        with get_conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO eps_upload_cache
                   (file_hash, local_path, eps_url, uploaded_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
                (file_hash, str(path), eps_url),
            )
            c.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"eps cache save skip: {e}")
