#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W14 通関対応自動化: eBay listing から商品画像を取得してローカル保存.

code-reviewer HIGH-9 対応: scrape 依存の脆弱性に対して
  1. eBay Trading API (GetItem) を第一優先 (安定)
  2. HTML scrape は fallback のみ
  3. 失敗時は status='drafted_no_photo' で user が手動添付可

保存先: `.company/daily-operations/customs-attachments/<tracking_number>/`
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_ATTACHMENTS_ROOT = (
    _BASE_DIR.parent.parent / ".company" / "daily-operations" / "customs-attachments"
)


def fetch_product_photos(
    ebay_item_id: str, *,
    tracking_number: str,
    max_photos: int = 5,
    prefer_api: bool = True,
) -> list[Path]:
    """eBay item id から商品画像 N 枚をローカル保存. 保存パス list を返す.

    Args:
        ebay_item_id: 12 桁の eBay Item ID
        tracking_number: 保存フォルダ名に使用
        max_photos: 取得枚数上限 (default 5)
        prefer_api: True なら GetItem API を第一優先、False なら scrape 第一優先

    Returns:
        list[Path]: 保存した画像ファイルパス. 失敗時は空 list.
    """
    # H-F 対応: eBay Item ID は従来 12 桁、2024 年以降新規は 13 桁も存在
    if not (ebay_item_id and ebay_item_id.isdigit()
            and len(ebay_item_id) in (12, 13)):
        logger.warning(f"invalid ebay_item_id: {ebay_item_id}")
        return []

    out_dir = _ATTACHMENTS_ROOT / tracking_number / "photos"
    out_dir.mkdir(parents=True, exist_ok=True)

    urls: list[str] = []
    if prefer_api:
        urls = _api_image_urls(ebay_item_id)
        if not urls:
            urls = _scrape_image_urls(ebay_item_id)
    else:
        urls = _scrape_image_urls(ebay_item_id)
        if not urls:
            urls = _api_image_urls(ebay_item_id)

    saved: list[Path] = []
    import httpx
    with httpx.Client(
        timeout=httpx.Timeout(20.0, connect=5.0),
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
    ) as client:
        for i, url in enumerate(urls[:max_photos], start=1):
            try:
                r = client.get(url)
                r.raise_for_status()
            except (httpx.HTTPError, OSError) as e:
                logger.warning(f"image {i} DL failed: {e}")
                continue
            # ファイル名: {ebay_item_id}_{N:02d}.jpg
            path = out_dir / f"{ebay_item_id}_{i:02d}.jpg"
            path.write_bytes(r.content)
            saved.append(path)
    logger.info(f"saved {len(saved)} photos -> {out_dir}")
    return saved


def _scrape_image_urls(ebay_item_id: str) -> list[str]:
    """eBay 公開ページから画像 URL を抽出. scrape fallback."""
    import httpx
    url = f"https://www.ebay.com/itm/{ebay_item_id}"
    try:
        r = httpx.get(
            url, timeout=15, follow_redirects=True,
            headers={
                "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        r.raise_for_status()
    except (httpx.HTTPError, OSError) as e:
        logger.warning(f"scrape failed: {e}")
        return []
    html = r.text
    # s-l{N} のサイズ指定部分を s-l1600 (最高解像度) に統一して dedupe
    raw = re.findall(
        r"https://i\.ebayimg\.com/[^\s\"']+?\.(?:jpg|jpeg|png|webp)", html
    )
    seen: set[str] = set()
    uniq: list[str] = []
    for u in raw:
        normalized = re.sub(r"s-l\d+", "s-l1600", u)
        if normalized not in seen:
            seen.add(normalized)
            uniq.append(normalized)
    return uniq


def _api_image_urls(ebay_item_id: str) -> list[str]:
    """eBay Trading API GetItem で PictureURL を取得.

    認証情報が無い / API 失敗時は空 list を返して caller が scrape に fallback.
    """
    import json
    import io
    try:
        from monitor.credentials import get_ebay_credentials
        from monitor.ebay_client import _call_trading_api
    except ImportError as e:
        logger.debug(f"ebay_client not available: {e}")
        return []
    cfg_path = _BASE_DIR / "config" / "schedule_config.json"
    if not cfg_path.exists():
        return []
    try:
        with io.open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return []
    try:
        creds = get_ebay_credentials(cfg)
    except Exception as e:  # noqa: BLE001 credentials モジュールが例外投げ得る
        logger.debug(f"ebay creds unavailable: {e}")
        return []
    xml_req = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{creds.get("user_token", "")}</eBayAuthToken></RequesterCredentials>
  <ItemID>{ebay_item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>
"""
    try:
        resp = _call_trading_api("GetItem", xml_req, **creds)
    except Exception as e:  # noqa: BLE001 API 例外多様
        logger.debug(f"GetItem call failed: {e}")
        return []
    body = (resp or {}).get("body", "") or ""
    return re.findall(r"<PictureURL>(https?://[^<]+)</PictureURL>", body)


__all__ = ["fetch_product_photos"]
