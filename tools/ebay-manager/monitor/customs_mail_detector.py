#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W14 通関対応自動化: Gmail から FedEx/DHL/UPS の通関要求メールを検知

code-reviewer HIGH-1 対応:
  - 受信メールの SPF/DKIM passed を Authentication-Results ヘッダで検証
  - 検証失敗 (フィッシング疑惑) は status='manual' に降格

検知対象:
  - from:@fedex.com / @dhl.com / @ups.com
  - subject に "customs" / "clearance" / "AWB" / "TRK#" / "通関"
  - since:<N days ago>
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# 受付対象の送信者ドメイン (HIGH-1 allow-list)
TRUSTED_SENDER_DOMAINS = {
    "fedex": {"fedex.com"},
    "dhl": {"dhl.com", "dhl.de", "myscg.dhl.com"},
    "ups": {"ups.com"},
}

# 通関要求を示唆するキーワード (subject 優先)
CUSTOMS_KEYWORDS = {
    "customs", "clearance", "awb", "trk#", "tracking",
    "tariff", "hts", "invoice", "通関", "税関",
    "information request", "shipment information",
    "commercial invoice", "tsca",
}


@dataclass
class DetectedMail:
    gmail_id: str
    carrier: str                           # 'fedex' / 'dhl' / 'ups'
    sender: str
    sender_domain: str
    subject: str
    received_at: str                       # ISO-8601
    body_plain: str
    body_html: str
    spf_dkim_ok: bool                      # HIGH-1: 認証検証結果
    authentication_results: str = ""       # raw header
    attachments_meta: list[dict] = field(default_factory=list)  # [{filename, mimeType, attachmentId, size}]
    # W14 v19 (2026-04-25) 追加: reply スレッド対応
    gmail_thread_id: str = ""              # Gmail の threadId (会話まとめ用)
    rfc822_message_id: str = ""            # 元メールの Message-ID ヘッダ (In-Reply-To 用)
    extra: dict = field(default_factory=dict)


def detect_customs_mails(
    gmail_service, *,
    days: int = 7,
    max_per_carrier: int = 30,
) -> list[DetectedMail]:
    """Gmail から通関要求メールを検知.

    各 carrier ごとに Gmail 検索を実行し、DetectedMail list を返す.
    status='manual' に降格すべきフィッシング疑惑は spf_dkim_ok=False で示す.
    """
    results: list[DetectedMail] = []
    seen_ids: set[str] = set()

    for carrier, domains in TRUSTED_SENDER_DOMAINS.items():
        # 各 carrier domain × keyword で OR 検索
        from_clause = " OR ".join(f"from:{d}" for d in domains)
        kw_clause = " OR ".join(f'"{k}"' for k in [
            "customs", "clearance", "AWB", "TRK#", "invoice",
        ])
        query = f"({from_clause}) ({kw_clause}) newer_than:{days}d"
        try:
            resp = gmail_service.users().messages().list(
                userId="me", q=query, maxResults=max_per_carrier,
            ).execute()
        except Exception as e:  # noqa: BLE001 Gmail API 例外多様
            logger.warning(f"detect carrier={carrier} query failed: {e}")
            continue

        for m in resp.get("messages", []) or []:
            gid = m.get("id")
            if not gid or gid in seen_ids:
                continue
            seen_ids.add(gid)
            try:
                full = gmail_service.users().messages().get(
                    userId="me", id=gid, format="full",
                ).execute()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"get msg {gid} failed: {e}")
                continue
            det = _parse_gmail_message(full, carrier)
            if det is None:
                continue
            results.append(det)
    return results


def _parse_gmail_message(msg: dict, carrier: str) -> Optional[DetectedMail]:
    payload = msg.get("payload") or {}
    headers_raw = payload.get("headers") or []
    hdrs = {h["name"].lower(): h["value"]
            for h in headers_raw if isinstance(h, dict)}
    gmail_id = msg.get("id", "")
    sender_raw = hdrs.get("from", "")
    subject = hdrs.get("subject", "")
    date_hdr = hdrs.get("date", "")
    auth_results = hdrs.get("authentication-results", "")

    # 送信者ドメイン抽出
    m = re.search(r"<([^>]+)>", sender_raw)
    sender_addr = m.group(1) if m else sender_raw.strip()
    sender_domain = sender_addr.split("@")[-1].lower() if "@" in sender_addr else ""

    # HIGH-1: trusted domain でなければ skip (フィッシング対策)
    trusted = any(
        sender_domain == d or sender_domain.endswith("." + d)
        for d in TRUSTED_SENDER_DOMAINS.get(carrier, set())
    )
    if not trusted:
        logger.debug(
            f"skip {gmail_id}: sender domain '{sender_domain}' not trusted for {carrier}"
        )
        return None

    # HIGH-1: SPF/DKIM pass 検証
    auth_lower = auth_results.lower()
    spf_ok = "spf=pass" in auth_lower
    dkim_ok = "dkim=pass" in auth_lower
    spf_dkim_ok = spf_ok and dkim_ok
    if not spf_dkim_ok:
        logger.warning(
            f"SPF/DKIM failed for {gmail_id} (from={sender_domain}): "
            f"spf={spf_ok} dkim={dkim_ok}"
        )

    # 本文抽出 (plain + html)
    body_plain, body_html = _extract_bodies(payload)

    # 添付メタ抽出
    attachments_meta = _extract_attachments_meta(payload)

    # W14 v19: Gmail threadId + RFC822 Message-ID 抽出 (reply スレッド対応)
    thread_id = msg.get("threadId") or ""
    rfc822_msg_id = hdrs.get("message-id", "").strip()

    return DetectedMail(
        gmail_id=gmail_id,
        carrier=carrier,
        sender=sender_addr,
        sender_domain=sender_domain,
        subject=subject,
        received_at=date_hdr,
        body_plain=body_plain,
        body_html=body_html,
        spf_dkim_ok=spf_dkim_ok,
        authentication_results=auth_results[:500],
        attachments_meta=attachments_meta,
        gmail_thread_id=thread_id,
        rfc822_message_id=rfc822_msg_id,
    )


def _extract_bodies(payload: dict) -> tuple[str, str]:
    """mimeType ごとに plain / html を再帰抽出."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        if "parts" in part:
            for p in part["parts"]:
                walk(p)
            return
        data = (part.get("body") or {}).get("data")
        if not data:
            return
        try:
            text = base64.urlsafe_b64decode(data).decode(
                "utf-8", errors="replace"
            )
        except (ValueError, UnicodeDecodeError):
            return
        if mime == "text/plain":
            plain_parts.append(text)
        elif mime == "text/html":
            html_parts.append(text)

    walk(payload)
    return "\n\n".join(plain_parts), "\n\n".join(html_parts)


def _extract_attachments_meta(payload: dict) -> list[dict]:
    """添付ファイルのメタ情報を抽出 (本体 DL は parser 側)."""
    out: list[dict] = []

    def walk(part: dict):
        filename = part.get("filename")
        body = part.get("body") or {}
        if filename and body.get("attachmentId"):
            out.append({
                "filename": filename,
                "mimeType": part.get("mimeType", ""),
                "attachmentId": body.get("attachmentId"),
                "size": int(body.get("size") or 0),
            })
        for p in part.get("parts") or []:
            walk(p)

    walk(payload)
    return out


__all__ = ["DetectedMail", "detect_customs_mails", "TRUSTED_SENDER_DOMAINS"]
