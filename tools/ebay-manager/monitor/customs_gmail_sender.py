#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
W14 通関対応自動化: MONO Deck から Gmail 送信

code-reviewer HIGH-1/H-4 対応:
  - send 前に CARRIER_DOMAINS allow-list で recipients 二重検証
  - atomic status 遷移 (drafted → sending → sent / failed)
  - 二重送信防止 (gmail_sent_id UNIQUE + 楽観的ロック)
  - audit log (customs_send_audit、immutable) を INSERT

追加推奨 3: kill switch (config/schedule_config.json の
  customs_automation.send_enabled) を必ず check
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import mimetypes
import re
import sqlite3
from dataclasses import dataclass
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent


# CARRIER_DOMAINS allow-list (H-1 対応、customs_draft_generator と整合)
_ALLOWED_DOMAINS_FIXED = {
    "fedex": {"paperwork@fedex.com"},
    "dhl": set(),       # TO は detected sender から動的決定、ドメイン allow-list のみ
    "ups": {"importbrokerage@ups.com"},
}
_ALLOWED_DOMAIN_SUFFIX = {
    "fedex": ("@fedex.com",),
    "dhl": ("@dhl.com", "@dhl.de"),
    "ups": ("@ups.com",),
}


class CustomsSendBlocked(RuntimeError):
    """送信をブロックすべきエラー (kill switch / allow-list / atomic 状態異常等)."""


class CustomsSendFailed(RuntimeError):
    """Gmail API 呼び出し失敗."""


@dataclass
class SendResult:
    success: bool
    gmail_sent_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class DraftResult:
    success: bool
    draft_gmail_id: Optional[str] = None
    action: str = ""              # 'created' or 'updated'
    error: Optional[str] = None


# ─────────────────────────────────────────────
# entry point
# ─────────────────────────────────────────────

def create_customs_draft(
    customs_request_id: int, *,
    gmail_service=None, config: Optional[dict] = None,
) -> "DraftResult":
    """指定 customs_requests 行の draft を Gmail に保存 (送信はしない).

    user の「送信準備」操作で呼ばれる. 同 request に既存 draft があれば update.
    threadId / In-Reply-To 設定で proper reply スレッドに保存される.

    Flow:
      1. kill switch check
      2. atomic 'drafted' → 'drafted_in_gmail' 状態遷移は **後続で別途**.
         draft 作成自体は何度でも安全 (既存 draft_gmail_id があれば update)
      3. recipient allow-list 検証
      4. Gmail drafts.create()  または drafts.update() (既存 draft_gmail_id 時)
      5. status='drafted_in_gmail' + draft_gmail_id 保存
    """
    cfg = config or _load_config()
    # kill switch は送信のみブロック対象 (draft は OK でも安全側にしておく)
    if not (cfg.get("customs_automation") or {}).get("send_enabled", True):
        raise CustomsSendBlocked("kill switch active (send_enabled=False)")

    # 観点 8 対応 + H-X1/X2/X3 修正 (2026-04-25):
    # 専用列 draft_lock_at で atomic claim. drafted_at は触らない (本番 INSERT パスと
    # 衝突回避). lock は最終的に必ず解除される (try/finally + _finalize_sent でクリア).
    from monitor.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM customs_requests WHERE id = ?",
            (customs_request_id,),
        ).fetchone()
        if row is None:
            raise CustomsSendBlocked(f"request {customs_request_id} not found")
        req = dict(row)
        prior_status = req["status"]
        if prior_status not in ("drafted", "drafted_no_photo", "drafted_in_gmail"):
            raise CustomsSendBlocked(
                f"request status='{prior_status}' is not draftable"
            )
        # draft_lock_at に対する 30 秒 lock (専用列、新規 INSERT パスとは無関係)
        cur = conn.execute(
            "UPDATE customs_requests SET draft_lock_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status=? AND "
            "(draft_lock_at IS NULL OR "
            " draft_lock_at < datetime('now', '-30 seconds'))",
            (customs_request_id, prior_status),
        )
        if cur.rowcount == 0:
            raise CustomsSendBlocked(
                "concurrent draft attempt — another tab/process is creating "
                "a draft. Wait 30s and retry."
            )

    # recipient 検証
    _recips = json.loads(req.get("draft_recipients") or "{}")
    to_list = _recips.get("to") or []
    cc_list = _recips.get("cc") or []
    _validate_recipients(req["carrier"], to_list, cc_list)

    if gmail_service is None:
        gmail_service = _get_gmail_service(cfg)
    if gmail_service is None:
        raise CustomsSendFailed("Gmail service unavailable")

    # 元メール情報 (reply スレッド対応)
    in_reply_to = (req.get("original_message_id") or "").strip() or None
    thread_id = (req.get("gmail_thread_id") or "").strip() or None
    quoted_original = _fetch_original_body_for_quote(
        gmail_service, req.get("gmail_id"),
    )

    msg = _build_mime_message(
        subject=req.get("draft_subject") or "",
        body_text=req.get("draft_body") or "",
        to_list=to_list, cc_list=cc_list,
        attachments=_resolve_attachment_paths(req),
        in_reply_to=in_reply_to,
        references=in_reply_to,  # 元 References 取得は省略、Message-ID のみで十分
        quoted_original=quoted_original,
    )
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    draft_body = {"message": {"raw": raw}}
    if thread_id:
        draft_body["message"]["threadId"] = thread_id

    existing_draft_id = (req.get("draft_gmail_id") or "").strip()
    # H-3 対応: action を try 前に初期化 (UnboundLocalError 防止)
    action = "create"
    new_draft_id: Optional[str] = None
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        HttpError = Exception  # type: ignore

    # H-X3 対応: try/finally で例外時 lock を必ず解除
    try:
        try:
            if existing_draft_id:
                action = "update"
                resp = gmail_service.users().drafts().update(
                    userId="me", id=existing_draft_id, body=draft_body,
                ).execute()
                new_draft_id = resp.get("id") or existing_draft_id
                action = "updated"
            else:
                resp = gmail_service.users().drafts().create(
                    userId="me", body=draft_body,
                ).execute()
                new_draft_id = resp.get("id")
                action = "created"
        except (HttpError, OSError, TimeoutError) as e:
            raise CustomsSendFailed(
                f"drafts.{action}: {type(e).__name__}: {e}"
            ) from e

        if not new_draft_id:
            raise CustomsSendFailed("Gmail did not return a draft id")

        # 成功確定 + lock 解除
        with get_conn() as conn:
            conn.execute(
                "UPDATE customs_requests SET status='drafted_in_gmail', "
                "draft_gmail_id=?, draft_lock_at=NULL "
                "WHERE id=?",
                (new_draft_id, customs_request_id),
            )

        logger.info(
            f"customs draft {action}: req_id={customs_request_id} "
            f"draft_gmail_id={new_draft_id} thread_id={thread_id}"
        )
        return DraftResult(
            success=True, draft_gmail_id=new_draft_id, action=action,
        )
    except Exception:
        # 例外時に lock を解除して次回 retry を即可能化 (H-X3)
        try:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE customs_requests SET draft_lock_at=NULL WHERE id=?",
                    (customs_request_id,),
                )
        except sqlite3.Error as _re:
            logger.warning(f"failed to clear draft_lock_at on error: {_re}")
        raise


def _fetch_original_body_for_quote(
    gmail_service, gmail_id: str, max_chars: int = 1500,
) -> Optional[str]:
    """元メールの plain body を引用用に取得. 失敗は None で続行."""
    if not gmail_id or gmail_service is None:
        return None
    try:
        full = gmail_service.users().messages().get(
            userId="me", id=gmail_id, format="full",
        ).execute()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"original mail fetch for quote failed: {e}")
        return None
    payload = full.get("payload") or {}
    plain = []

    def walk(p):
        mime = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if mime == "text/plain" and data:
            try:
                plain.append(
                    base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                )
            except (ValueError, UnicodeDecodeError):
                pass
        for sub in p.get("parts") or []:
            walk(sub)

    walk(payload)
    body = "\n\n".join(plain).strip()
    if not body:
        return None
    return body[:max_chars]


def send_customs_reply(
    customs_request_id: int, *,
    gmail_service=None, config: Optional[dict] = None,
) -> SendResult:
    """指定 customs_requests 行を実送信.

    new flow (W14ext): drafted_in_gmail 状態 + draft_gmail_id があれば
    `drafts().send(draft_id)` を使用 (Gmail 上の draft → Sent フォルダに移動).
    なければ従来通り messages().send() で MIME 直接送信 (後方互換).

    Flow:
      1. kill switch check
      2. DB で atomic 'drafted'/'drafted_in_gmail' → 'sending' 遷移
      3. recipient allow-list 検証
      4. drafts().send (draft_id あり) or messages().send (なし)
      5. status='sent' + audit log INSERT
      6. 失敗時 status='failed' + audit log INSERT
    """
    # 1. kill switch
    cfg = config or _load_config()
    if not (cfg.get("customs_automation") or {}).get("send_enabled", True):
        raise CustomsSendBlocked("kill switch active (send_enabled=False)")

    # 2. atomic 遷移 & row 取得
    req = _atomic_claim_for_sending(customs_request_id)
    if req is None:
        raise CustomsSendBlocked(
            f"request {customs_request_id} not in 'drafted'/'drafted_in_gmail' state "
            "(possibly already sent, failed, or not found)"
        )

    # H-A 対応: except 内で to_list/cc_list を audit に残せるよう早期宣言
    to_list: list[str] = []
    cc_list: list[str] = []
    try:
        # 3. recipients 検証 (H-1)
        _recips = json.loads(req.get("draft_recipients") or "{}")
        to_list = _recips.get("to") or []
        cc_list = _recips.get("cc") or []
        _validate_recipients(req["carrier"], to_list, cc_list)

        # 4. Gmail API send
        if gmail_service is None:
            gmail_service = _get_gmail_service(cfg)
        if gmail_service is None:
            raise CustomsSendFailed("Gmail service unavailable")

        draft_id = (req.get("draft_gmail_id") or "").strip()
        try:
            if draft_id:
                # drafts().send で Gmail 上の draft をそのまま送信 (Sent に移動)
                resp = gmail_service.users().drafts().send(
                    userId="me", body={"id": draft_id},
                ).execute()
            else:
                # 後方互換: draft 未作成の場合は MIME 直接送信
                in_reply_to = (req.get("original_message_id") or "").strip() or None
                thread_id = (req.get("gmail_thread_id") or "").strip() or None
                quoted = _fetch_original_body_for_quote(
                    gmail_service, req.get("gmail_id"),
                )
                msg = _build_mime_message(
                    subject=req.get("draft_subject") or "",
                    body_text=req.get("draft_body") or "",
                    to_list=to_list, cc_list=cc_list,
                    attachments=_resolve_attachment_paths(req),
                    in_reply_to=in_reply_to,
                    references=in_reply_to,
                    quoted_original=quoted,
                )
                raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
                send_body = {"raw": raw}
                if thread_id:
                    send_body["threadId"] = thread_id
                resp = gmail_service.users().messages().send(
                    userId="me", body=send_body,
                ).execute()
        except Exception as e:  # noqa: BLE001 googleapiclient HttpError 多様
            # H-E 対応: ambiguous failure (timeout 等) は error_msg に明示
            etype = type(e).__name__
            is_ambiguous = any(
                s in etype.lower() for s in ("timeout", "connection", "socket")
            )
            marker = (
                "[AMBIGUOUS — check Gmail Sent folder before resending] "
                if is_ambiguous else ""
            )
            raise CustomsSendFailed(f"{marker}{etype}: {e}") from e
        gmail_sent_id = resp.get("id")

        # 5. success 確定
        _finalize_sent(customs_request_id, gmail_sent_id, req, to_list, cc_list)
        logger.info(
            f"customs reply sent: req_id={customs_request_id} "
            f"gmail_sent_id={gmail_sent_id}"
        )
        return SendResult(success=True, gmail_sent_id=gmail_sent_id)

    except (CustomsSendBlocked, CustomsSendFailed) as e:
        # H-A 対応: to_list/cc_list を audit に残す (forensic 用途)
        _finalize_failed(customs_request_id, str(e), req, to_list, cc_list)
        return SendResult(success=False, error=str(e))
    except Exception as e:  # noqa: BLE001 想定外 も DB 反映して raise
        _finalize_failed(
            customs_request_id,
            f"unexpected: {type(e).__name__}: {e}",
            req, to_list, cc_list,
        )
        raise


# ─────────────────────────────────────────────
# DB atomic 遷移
# ─────────────────────────────────────────────

def _atomic_claim_for_sending(req_id: int) -> Optional[dict]:
    """'drafted' / 'drafted_no_photo' / 'drafted_in_gmail' → 'sending' atomic 遷移.

    W14ext (2026-04-25):
      - drafted_in_gmail も対象に追加
      - H-4 修正: sent_at=CURRENT_TIMESTAMP の早期セットを削除
        (失敗時に sent_at が claim 時刻で残り audit ログ不整合になる事故回避)
        sent_at は _finalize_sent でのみセットする.
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE customs_requests SET status='sending' "
            "WHERE id=? AND status IN "
            "('drafted','drafted_no_photo','drafted_in_gmail')",
            (req_id,),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM customs_requests WHERE id = ?", (req_id,)
        ).fetchone()
    return dict(row) if row else None


def _finalize_sent(
    req_id: int, gmail_sent_id: str, req: dict,
    to_list: list[str], cc_list: list[str],
) -> None:
    from monitor.database import get_conn
    recipients_hash = _hash_recipients(to_list, cc_list)
    body_hash = hashlib.sha256(
        (req.get("draft_body") or "").encode("utf-8")
    ).hexdigest()[:32]
    attachments_paths = _resolve_attachment_paths(req)
    att_hash = _hash_attachments(attachments_paths)
    with get_conn() as conn:
        try:
            # H-1 対応: drafts.send 後、Gmail 上の draft は自動削除されるので
            # DB の draft_gmail_id を NULL にクリア (404 リスク回避).
            # 併せて draft_lock_at もクリア (H-X1/X3).
            conn.execute(
                "UPDATE customs_requests SET status='sent', "
                "gmail_sent_id=?, sent_at=CURRENT_TIMESTAMP, "
                "draft_gmail_id=NULL, draft_lock_at=NULL "
                "WHERE id=?",
                (gmail_sent_id, req_id),
            )
            conn.execute(
                """INSERT INTO customs_send_audit
                   (customs_request_id, gmail_sent_id, recipients_hash,
                    body_hash, attachments_hash, result)
                   VALUES (?, ?, ?, ?, ?, 'success')""",
                (req_id, gmail_sent_id, recipients_hash, body_hash, att_hash),
            )
        except sqlite3.IntegrityError as e:
            # H-B 対応: gmail_sent_id UNIQUE 違反 = 実送信は成功済みだが DB 重複.
            # Gmail API 側は既にメール配信済み (再送してはいけない).
            # status='sent' にして error_msg で「重複検知」を記録、audit も failed で残す.
            logger.error(
                f"DOUBLE SEND DETECTED (API sent OK but DB unique violation): "
                f"req_id={req_id} msg_id={gmail_sent_id}: {e}"
            )
            conn.execute(
                "UPDATE customs_requests SET status='sent', "
                "error_msg='duplicate gmail_sent_id — manual review required' "
                "WHERE id=? AND status='sending'",
                (req_id,),
            )
            conn.execute(
                """INSERT INTO customs_send_audit
                   (customs_request_id, recipients_hash, body_hash,
                    attachments_hash, result, error_msg)
                   VALUES (?, ?, ?, ?, 'failed', ?)""",
                (req_id, recipients_hash, body_hash, att_hash,
                 f"IntegrityError: gmail_sent_id={gmail_sent_id} already exists"),
            )
            # raise しない (実送信は成功している)


def _finalize_failed(
    req_id: int, error_msg: str, req: Optional[dict],
    to_list: Optional[list[str]], cc_list: Optional[list[str]],
) -> None:
    from monitor.database import get_conn
    body_hash = ""
    recipients_hash = ""
    if req:
        body_hash = hashlib.sha256(
            (req.get("draft_body") or "").encode("utf-8")
        ).hexdigest()[:32]
    if to_list is not None or cc_list is not None:
        recipients_hash = _hash_recipients(to_list or [], cc_list or [])
    with get_conn() as conn:
        conn.execute(
            "UPDATE customs_requests SET status='failed', error_msg=? "
            "WHERE id=? AND status='sending'",
            (error_msg[:500], req_id),
        )
        conn.execute(
            """INSERT INTO customs_send_audit
               (customs_request_id, recipients_hash, body_hash, result, error_msg)
               VALUES (?, ?, ?, 'failed', ?)""",
            (req_id, recipients_hash, body_hash, error_msg[:500]),
        )


# ─────────────────────────────────────────────
# recipient allow-list (H-1)
# ─────────────────────────────────────────────

def _validate_recipients(
    carrier: str, to_list: list[str], cc_list: list[str],
) -> None:
    allowed_fixed = _ALLOWED_DOMAINS_FIXED.get(carrier, set())
    allowed_suffix = _ALLOWED_DOMAIN_SUFFIX.get(carrier, ())
    if not to_list:
        raise CustomsSendBlocked("empty TO list")
    for addr in list(to_list) + list(cc_list):
        a = addr.strip().lower()
        if a in {x.lower() for x in allowed_fixed}:
            continue
        if any(a.endswith(s.lower()) for s in allowed_suffix):
            continue
        raise CustomsSendBlocked(
            f"recipient '{addr}' not in allow-list for carrier={carrier}"
        )


# ─────────────────────────────────────────────
# MIME 構築
# ─────────────────────────────────────────────

def _build_mime_message(
    *, subject: str, body_text: str,
    to_list: list[str], cc_list: list[str],
    attachments: list[Path],
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    quoted_original: Optional[str] = None,
) -> MIMEMultipart:
    """Reply 仕様 MIME 構築.

    Args:
        in_reply_to: 元メールの RFC822 Message-ID (e.g., "<abc@fedex.com>").
                     設定されると Subject に "Re: " prefix も自動追加.
        references: 元の References ヘッダ (元 References + Message-ID 連結が望ましい).
        quoted_original: 元メール本文 (各行 "> " prefix で本文末尾に追加).
    """
    msg = MIMEMultipart()
    # Subject に "Re: " prefix (既に Re: 始まりなら追加しない)
    if in_reply_to and subject and not re.match(r"^\s*re:\s*", subject, re.IGNORECASE):
        subject = f"Re: {subject}"
    msg["Subject"] = subject
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    # Reply スレッドヘッダ (RFC 5322 / 2822)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    # 本文 (元メール引用付き)
    full_body = body_text or ""
    if quoted_original:
        quoted = "\n".join(
            f"> {line}" for line in quoted_original.splitlines()
        )
        full_body = f"{full_body}\n\n{quoted}"
    msg.attach(MIMEText(full_body, "plain", "utf-8"))
    # 添付
    for p in attachments:
        try:
            data = p.read_bytes()
        except OSError as e:
            logger.warning(f"attachment {p.name} read failed: {e}")
            continue
        ctype, _ = mimetypes.guess_type(str(p))
        if ctype is None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        part = MIMEBase(maintype, subtype)
        part.set_payload(data)
        from email.encoders import encode_base64
        encode_base64(part)
        part.add_header(
            "Content-Disposition", f'attachment; filename="{p.name}"'
        )
        msg.attach(part)
    return msg


def _resolve_attachment_paths(req: dict) -> list[Path]:
    paths: list[Path] = []
    for key in ("attached_photos", "attached_attachments"):
        raw = req.get(key)
        if not raw:
            continue
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(items, list):
            continue
        for p in items:
            try:
                path = Path(str(p))
                if path.exists() and path.is_file():
                    paths.append(path)
            except (TypeError, OSError):
                continue
    return paths


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _hash_recipients(to_list: list[str], cc_list: list[str]) -> str:
    sorted_all = sorted([a.lower() for a in to_list] + [a.lower() for a in cc_list])
    return hashlib.sha256("|".join(sorted_all).encode("utf-8")).hexdigest()[:32]


def _hash_attachments(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        try:
            h.update(p.name.encode("utf-8"))
            h.update(str(p.stat().st_size).encode("ascii"))
        except OSError:
            h.update(b"MISSING")
    return h.hexdigest()[:32]


def _load_config() -> dict:
    cfg_path = _BASE_DIR / "config" / "schedule_config.json"
    if not cfg_path.exists():
        return {}
    try:
        with io.open(cfg_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _get_gmail_service(config: dict):
    try:
        from tasks.task_email_pickup import get_gmail_service
        return get_gmail_service(config)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Gmail service init failed: {e}")
        return None


__all__ = [
    "send_customs_reply", "create_customs_draft",
    "SendResult", "DraftResult",
    "CustomsSendBlocked", "CustomsSendFailed",
]
