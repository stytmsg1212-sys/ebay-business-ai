"""ヤフオク終了状態 + 終了時刻取得 helper (W100 Phase 2 / 2026-05-06).

inventory_check が「ヤフオク URL の listing で在庫切れ」を検知時、
本 helper で「落札者なし終了」を判定し、yahoo_grace_until = end_time + 24h を
セットする (再出品の慣行を待つため).

実装方針:
  - httpx で HTML 取得 (Playwright 不要、軽量)
  - HTML 内 __NEXT_DATA__ JSON から item dict 抽出
  - status='closed' + bids=0 → 落札者なし終了 (24h 猶予対象)
  - status='closed' + bids>0 → 落札済 (24h 猶予不要、即リサーチ)
  - status='open' → 進行中 (在庫切れ検知が想定外、24h 猶予不要)
  - JSON 取れない場合は raw_error で None 返し → caller が「即リサーチ」 fallback

判定 ロジック:
  - 入札数: `bids` フィールド (Yahoo __NEXT_DATA__)
  - 終了時刻: `endTime` フィールド (ISO 8601 with JST offset)
  - 状態: `status` フィールド ('open' / 'closed')
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class YahooEndStatus:
    """ヤフオク商品ページの終了状態.

    has_winner:
      - True: 入札者あり (落札済 or 落札確定間近)
      - False: 落札者なし終了 (再出品慣行該当 → 24h 猶予対象)
      - None: 判定不能 (HTML 取得失敗 / JSON 構造変動)

    end_time_utc:
      - 終了時刻 (UTC, aware datetime)
      - None なら判定不能

    is_ended:
      - True: status='closed' を確認
      - False: status='open' or 判定不能
    """
    is_ended: bool
    has_winner: Optional[bool]
    end_time_utc: Optional[datetime]
    raw_error: Optional[str] = None


def _extract_yahoo_item(html: str) -> Optional[dict]:
    """Yahoo Auctions の __NEXT_DATA__ から item dict を取り出す.

    既存 `monitor.supplier_scraper._extract_yahoo_next_data` と同じ JSON path.
    成功: dict / 失敗: None
    """
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(\{.*?\})</script>',
        html, re.DOTALL,
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return (
            (data.get('props') or {})
            .get('pageProps', {})
            .get('initialState', {})
            .get('item', {})
            .get('detail', {})
            .get('item')
        )
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def _parse_end_time(raw: Optional[str]) -> Optional[datetime]:
    """ヤフオク endTime ('2025-09-17T22:07:18+09:00') を UTC aware datetime に変換."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            # offset なし = JST と仮定 (ヤフオクは常に +09:00 だが防御)
            return dt.replace(tzinfo=timezone(offset=__import__("datetime").timedelta(hours=9))).astimezone(timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def fetch_yahoo_end_status(url: str, timeout_sec: int = 15) -> YahooEndStatus:
    """ヤフオク商品ページから終了状態 + 終了時刻を取得.

    Args:
        url: https://page.auctions.yahoo.co.jp/jp/auction/<auction_id> 形式
        timeout_sec: HTTP timeout

    Returns:
        YahooEndStatus (raw_error 付与時は判定不能)
    """
    headers = {"User-Agent": _USER_AGENT}
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout_sec, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"yahoo end_status HTTP error ({url}): {e}")
        return YahooEndStatus(
            is_ended=False, has_winner=None, end_time_utc=None,
            raw_error=f"http_error: {type(e).__name__}: {e}"
        )

    item = _extract_yahoo_item(resp.text)
    if not item:
        return YahooEndStatus(
            is_ended=False, has_winner=None, end_time_utc=None,
            raw_error="next_data_not_found"
        )

    status_raw = item.get("status")
    is_ended = (status_raw == "closed")

    # 落札者判定: bids > 0 なら入札あり = 落札者あり (= 再出品慣行外)
    bids_raw = item.get("bids")
    try:
        bids = int(bids_raw) if bids_raw is not None else 0
    except (ValueError, TypeError):
        bids = 0
    # 補助: biddersNum も同等の意味
    bidders_raw = item.get("biddersNum")
    try:
        bidders = int(bidders_raw) if bidders_raw is not None else 0
    except (ValueError, TypeError):
        bidders = 0

    if is_ended:
        has_winner = (bids > 0 or bidders > 0)
    else:
        # 進行中の場合は落札判定無関係 (in-progress, 終了待ち)
        has_winner = None

    end_time = _parse_end_time(item.get("endTime"))

    return YahooEndStatus(
        is_ended=is_ended,
        has_winner=has_winner,
        end_time_utc=end_time,
        raw_error=None,
    )
