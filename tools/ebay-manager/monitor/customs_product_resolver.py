#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W14 通関対応自動化: tracking ↔ 売却商品リンク解決

入力: FedEx/DHL/UPS メールから得られた
  - tracking_number
  - recipient_name (Consignee)
  - ship_date

出力: 該当する sales_history / ebay_listings レコード

解決戦略 (優先順位):
  1. Gmail の "sold notification" メール (ebay@ebay.com) を ship_date 周辺で検索し、
     recipient_name / tracking_number とマッチする item_id を抽出
  2. sales_history.sold_at + buyer_country で絞り込み
  3. ebay_listings.title を fuzzy match

注意: sales_history テーブルに tracking_number カラムが無いため、Gmail API 必須.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ResolvedProduct:
    ebay_item_id: Optional[str]
    sku: Optional[str]
    title: Optional[str]
    source: str                      # 'gmail_sold' / 'sales_history' / 'fuzzy' / 'unresolved'
    confidence: str                  # 'high' / 'medium' / 'low'
    raw_match_info: dict = None


def resolve_product(
    *,
    tracking_number: str,
    recipient_name: Optional[str] = None,
    ship_date: Optional[str] = None,
    gmail_service=None,
) -> ResolvedProduct:
    """tracking + recipient + ship_date から商品情報を解決.

    Args:
        tracking_number: 必須. FedEx/DHL/UPS の tracking 番号
        recipient_name: 任意. "Coral Kiefer" 等
        ship_date: 任意. ISO 形式 (YYYY-MM-DD)
        gmail_service: 任意. None なら内部で構築

    Returns:
        ResolvedProduct. unresolved の場合も値返却 (caller が handling)
    """
    # 1. Gmail から sold 通知メール検索 (最強の手がかり)
    if gmail_service is None:
        from tasks.task_email_pickup import get_gmail_service
        import json, io
        from pathlib import Path
        cfg_path = (Path(__file__).resolve().parent.parent
                    / "config" / "schedule_config.json")
        cfg = {}
        if cfg_path.exists():
            try:
                with io.open(cfg_path, encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError) as e:
                logger.warning(f"schedule_config read: {e}")
        gmail_service = get_gmail_service(cfg)

    if gmail_service is not None:
        hit = _search_sold_email(
            gmail_service,
            tracking_number=tracking_number,
            recipient_name=recipient_name,
            ship_date=ship_date,
        )
        if hit:
            return hit

    # 2. sales_history fallback (ship_date 近辺の米国向け売上)
    if ship_date:
        fallback = _sales_history_match(ship_date, recipient_name)
        if fallback:
            return fallback

    # 3. unresolved
    return ResolvedProduct(
        ebay_item_id=None, sku=None, title=None,
        source="unresolved", confidence="low",
        raw_match_info={"tracking_number": tracking_number},
    )


def _search_sold_email(
    service, *,
    tracking_number: str,
    recipient_name: Optional[str],
    ship_date: Optional[str],
) -> Optional[ResolvedProduct]:
    """sold notification メールから ebay_item_id を抽出."""
    # tracking は sold notification 本文に含まれない (発送は売却後) ので検索条件に入れない.
    # 代わりに from:ebay + recipient_name で絞り込む.
    queries = ["from:ebay"]
    if recipient_name:
        queries.append(f'"{recipient_name}"')
    if ship_date:
        # sold は ship_date より 1-30 日前 (発送遅延 + 出品即売もあるので広めに)
        try:
            d = datetime.fromisoformat(ship_date)
            start = (d - timedelta(days=30)).strftime("%Y/%m/%d")
            end = (d + timedelta(days=3)).strftime("%Y/%m/%d")
            queries.append(f"after:{start} before:{end}")
        except ValueError:
            pass
    q = " ".join(queries)

    try:
        resp = service.users().messages().list(userId="me", q=q, maxResults=10).execute()
    except Exception as e:  # noqa: BLE001 Gmail API 多様な例外
        logger.warning(f"gmail sold search failed: {e}")
        return None

    for m in resp.get("messages", []):
        try:
            full = service.users().messages().get(
                userId="me", id=m["id"], format="full"
            ).execute()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"get msg {m['id']} failed: {e}")
            continue
        hdrs = {h["name"]: h["value"]
                for h in full.get("payload", {}).get("headers", [])}
        from_ = hdrs.get("From", "")
        if "ebay" not in from_.lower():
            continue
        subj = hdrs.get("Subject", "")
        body = _extract_body(full.get("payload", {}))

        # title 抽出: subject の "... が売れました" 前
        title = None
        m2 = re.search(r"^(.+?)(?:\s*-\s*(?:日本|from|.*?))?\s*が売れました", subj)
        if m2:
            title = m2.group(1).strip()
        else:
            # sold 通知でなければ skip
            continue

        # eBay item id は subject/body に無いケースが多い (2026-04 時点の eBay 通知)
        # → title から DB 逆引きで ebay_item_id を取得
        lookup = _lookup_listing_by_title(title)
        if lookup is None:
            logger.debug(f"title lookup miss for: {title[:60]}")
            continue
        return ResolvedProduct(
            ebay_item_id=lookup["ebay_item_id"], sku=lookup["sku"],
            title=lookup["title"],
            source="gmail_sold", confidence="high",
            raw_match_info={
                "gmail_id": m["id"], "subject": subj,
                "matched_via": "title_lookup",
            },
        )
    return None


def _lookup_listing_by_title(title: str) -> Optional[dict]:
    """sold 通知メールの title から ebay_listings / sales_history を逆引き.

    challenge: eBay の sold 通知 subject は日本語翻訳されるが、DB の title は
    英語原文. ASCII トークン抽出でブランド/型番一致を探す.

    Strategy:
      1. 完全一致 (LOWER)
      2. ASCII token 抽出: 'BOOX', 'Leaf2' 等の英語/数字トークン (3文字以上)
         → ebay_listings を AND 条件で LIKE 検索
      3. sales_history でも同様検索
      4. 複数ヒット = 曖昧 → None (誤認防止)
    """
    if not title:
        return None
    from monitor.database import get_conn
    with get_conn() as c:
        # 1. 完全一致
        r = c.execute(
            "SELECT ebay_item_id, sku, title FROM ebay_listings "
            "WHERE LOWER(title) = LOWER(?) LIMIT 1", (title,),
        ).fetchone()
        if r:
            return dict(r)
        r = c.execute(
            "SELECT ebay_item_id, sku, title FROM sales_history "
            "WHERE LOWER(title) = LOWER(?) LIMIT 1", (title,),
        ).fetchone()
        if r:
            return dict(r)

        # 2. ASCII トークン抽出 → AND 条件 LIKE
        tokens = _extract_ascii_tokens(title)
        if len(tokens) >= 2:
            result = _search_by_tokens(c, "ebay_listings", tokens)
            if result:
                return result
            result = _search_by_tokens(c, "sales_history", tokens)
            if result:
                return result
    return None


def _extract_ascii_tokens(title: str) -> list[str]:
    """日本語混じり title から ASCII トークン (3文字以上) を抽出.

    Example: "Onyx Boox leaf2 ホワイト 7 インチeインクリーダー" →
      ["Onyx", "Boox", "leaf2"]  (インクリーダーや小文字 'e' は除外)
    """
    import re as _re
    # 英数字トークン (3+ chars or 数字入り 2+ chars)
    raw = _re.findall(r"[A-Za-z][A-Za-z0-9]{2,}|[A-Za-z]{2,}\d+", title)
    # 一般用語除外
    stopwords = {"used", "new", "for", "from", "the", "and", "with"}
    tokens = [t for t in raw if t.lower() not in stopwords]
    # 重複除去、元順序維持
    seen: set[str] = set()
    uniq: list[str] = []
    for t in tokens:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return uniq[:5]  # 上位 5 トークンまで


def _search_by_tokens(conn, table: str, tokens: list[str]) -> Optional[dict]:
    """table の title 列に全 tokens が含まれるレコードを AND 検索. 1 件のみなら返す."""
    conditions = " AND ".join(["title LIKE ?" for _ in tokens])
    binds = [f"%{t}%" for t in tokens]
    q = f"SELECT ebay_item_id, sku, title FROM {table} WHERE {conditions} LIMIT 3"
    rows = conn.execute(q, binds).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    return None


def _extract_body(payload: dict) -> str:
    """メール本文 (plain+html 合体). 可能なら plain 優先."""
    if "body" in payload and payload["body"].get("data"):
        try:
            return base64.urlsafe_b64decode(
                payload["body"]["data"]
            ).decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            pass
    out = []
    for p in payload.get("parts", []) or []:
        out.append(_extract_body(p))
    text = "\n".join(t for t in out if t)
    # HTML タグ除去
    return re.sub(r"<[^>]+>", " ", text)


# eBay ID 抽出 regex. # prefix 優先 (確実)、fallback に word-boundary 付き 12-13 桁
# 日付文字列 (20260420230954 等) との誤認防止のため word boundary 厳格.
_EBAY_ID_HASH_RE = re.compile(r"#(\d{12,13})\b")
_EBAY_ID_BARE_RE = re.compile(r"(?<![\d])(\d{12,13})(?![\d])")


def _extract_ebay_item_id(text: str) -> Optional[str]:
    """#358178581550 形式の 12-13 桁 eBay item id を抽出.

    優先順位:
      1. # prefix + 12-13 桁 (確実)
      2. word boundary で囲まれた 12-13 桁 (日付/タイムスタンプ誤認防止)

    H-F 対応: 2024 年以降 13 桁 ID 混在.
    """
    t = text or ""
    m = _EBAY_ID_HASH_RE.search(t)
    if m:
        return m.group(1)
    m = _EBAY_ID_BARE_RE.search(t)
    return m.group(1) if m else None


def _lookup_sku_by_item_id(item_id: str) -> Optional[str]:
    from monitor.database import get_conn
    with get_conn() as c:
        r = c.execute(
            "SELECT sku FROM ebay_listings WHERE ebay_item_id = ? LIMIT 1",
            (item_id,),
        ).fetchone()
        if r and r[0]:
            return r[0]
        r2 = c.execute(
            "SELECT sku FROM sales_history WHERE ebay_item_id = ? LIMIT 1",
            (item_id,),
        ).fetchone()
        return r2[0] if r2 else None


def _sales_history_match(
    ship_date: str, recipient_name: Optional[str]
) -> Optional[ResolvedProduct]:
    """sales_history から ship_date 近辺の USA 向けを fallback 検索.

    code-reviewer H-D 対応:
      - tracking 番号で直接照合できないため候補が複数 (同日複数発送の通常ケース)
        の場合は必ず None を返し、誤商品情報で通関申告するリスクを回避.
      - 1 件に絞れても confidence="low" 固定 (task_customs_check 側で manual 降格).
    """
    try:
        d = datetime.fromisoformat(ship_date)
    except ValueError:
        return None
    start = (d - timedelta(days=7)).strftime("%Y-%m-%d")
    end = (d + timedelta(days=2)).strftime("%Y-%m-%d")
    from monitor.database import get_conn
    with get_conn() as c:
        rows = c.execute(
            """SELECT ebay_item_id, sku, title, sold_at, buyer_country
               FROM sales_history
               WHERE sold_at BETWEEN ? AND ?
                 AND (buyer_country = 'US' OR buyer_country = 'United States')
               ORDER BY sold_at DESC LIMIT 5""",
            (start, end),
        ).fetchall()
    if not rows:
        return None
    # H-D: 候補複数は曖昧すぎるので unresolved に倒す (虚偽通関申告リスク回避)
    if len(rows) > 1:
        logger.warning(
            f"sales_history fallback: {len(rows)} candidates for ship_date={ship_date}, "
            f"returning None to force manual review"
        )
        return None
    r = rows[0]
    # 1 件でも tracking 番号非照合なので confidence は常に low
    return ResolvedProduct(
        ebay_item_id=r["ebay_item_id"], sku=r["sku"], title=r["title"],
        source="sales_history", confidence="low",
        raw_match_info={"candidates": 1, "picked_sold_at": r["sold_at"]},
    )


__all__ = ["ResolvedProduct", "resolve_product"]
