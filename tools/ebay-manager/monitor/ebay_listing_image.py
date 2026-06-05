#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBay 商品画像 URL の取得 + DB cache (W223 step1, 2026-06-05).

背景:
  仕入先候補の AI 評価 (monitor.claude_evaluator.evaluate_match) は
  「eBay 出品中の商品」と「候補商品」を画像対画像で比べることで精度が出る。
  ところが eBay 側の商品画像 URL は ebay_listings に保持されておらず、
  GetItem (Trading API) 経由でしか取れない (ebay_image_fetcher.py 前例)。
  realtime / sweep の両評価経路は ebay_image_url を渡せておらず
  (列が無く常に None)、画像対「タイトル文字」の非対称比較に退化していた。

責務:
  - get_ebay_image_url(ebay_item_id) で eBay 代表画像 1 枚の URL を返す。
  - DB cache (ebay_listings.ebay_image_url / ebay_image_fetched_at, migration v63)
    を 30 日窓で再利用し、miss 時のみ GetItem を 1 回叩いて結果を cache。
  - 取得不能 (creds 無し / API 失敗 / 列未追加) は None を返して fail-open。
    呼び出し側は None でも従来どおりテキスト評価を継続できる
    (Q0: silent skip ではなく「画像なし評価」への正当な degrade)。

listing 識別キーは ebay_item_id (.claude/rules/sku-rules.md 準拠)。
時刻は CURRENT_TIMESTAMP (UTC) 保存・SQL 側 datetime('now', '-N days') 比較で
TZ ずれを回避 (.claude/rules/sqlite-timezone.md option A/C)。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

# eBay 画像 cache の鮮度窓 (日)。これより古い / 未取得なら GetItem で再取得。
DEFAULT_MAX_AGE_DAYS = 30


def _read_cached(ebay_item_id: str, max_age_days: int) -> Optional[str]:
    """DB cache から鮮度内の ebay_image_url を返す。無ければ None。

    列未追加 (旧 DB で migration v63 未適用) は OperationalError を握って None
    (= cache miss 扱い) にし、呼び出し側を壊さない。
    """
    from monitor.database import get_conn
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT ebay_image_url FROM ebay_listings "
                "WHERE ebay_item_id = ? "
                "AND ebay_image_url IS NOT NULL AND ebay_image_url != '' "
                "AND ebay_image_fetched_at IS NOT NULL "
                "AND ebay_image_fetched_at >= datetime('now', ?)",
                (ebay_item_id, f"-{int(max_age_days)} days"),
            ).fetchone()
    except sqlite3.OperationalError as e:
        logger.debug(f"[ebay_image] cache read skipped (schema?): {e}")
        return None
    return row[0] if row else None


def _store_cache(ebay_item_id: str, image_url: str) -> None:
    """取得した ebay_image_url を DB に書き戻す (fetched_at = 現在 UTC)。

    列未追加時は OperationalError を握って no-op (cache できないだけで評価は進む)。
    """
    from monitor.database import get_conn
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE ebay_listings SET ebay_image_url = ?, "
                "ebay_image_fetched_at = CURRENT_TIMESTAMP "
                "WHERE ebay_item_id = ?",
                (image_url, ebay_item_id),
            )
    except sqlite3.OperationalError as e:
        logger.debug(f"[ebay_image] cache store skipped (schema?): {e}")


def get_ebay_image_url(
    ebay_item_id: str,
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> Optional[str]:
    """eBay 代表画像 1 枚の URL を返す (DB cache → miss 時 GetItem)。

    Args:
        ebay_item_id: eBay listing 一意 ID (listing 識別 canonical key)。
        max_age_days: cache 鮮度窓 (日)。これより古ければ GetItem で再取得。

    Returns:
        画像 URL 文字列。取得不能時は None (fail-open、テキスト評価継続)。
    """
    if not ebay_item_id:
        return None
    eid = str(ebay_item_id)

    cached = _read_cached(eid, max_age_days)
    if cached:
        return cached

    # cache miss → GetItem (Trading API) で PictureURL を取得し 1 枚目を採用。
    try:
        from monitor.ebay_image_fetcher import _api_image_urls
    except ImportError as e:
        logger.debug(f"[ebay_image] ebay_image_fetcher 不在: {e}")
        return None
    try:
        urls = _api_image_urls(eid)
    except Exception as e:  # noqa: BLE001 API/network 例外多様、fail-open
        logger.debug(f"[ebay_image] GetItem 失敗 eid={eid}: {e}")
        return None
    if not urls:
        return None

    image_url = urls[0]
    _store_cache(eid, image_url)
    return image_url


__all__ = ["get_ebay_image_url"]
