#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W14ext (Gmail draft + reply thread) regression tests."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


# ─────────────────────────────
# _build_mime_message (reply 仕様)
# ─────────────────────────────

def test_mime_message_no_reply_metadata_keeps_subject():
    from monitor.customs_gmail_sender import _build_mime_message
    msg = _build_mime_message(
        subject="TRK#123 - Customs Info",
        body_text="Hello",
        to_list=["paperwork@fedex.com"], cc_list=[],
        attachments=[],
    )
    assert msg["Subject"] == "TRK#123 - Customs Info"
    assert "In-Reply-To" not in msg


def test_mime_message_with_in_reply_to_adds_re_prefix():
    from monitor.customs_gmail_sender import _build_mime_message
    msg = _build_mime_message(
        subject="FedEx / TRK#999",
        body_text="Reply body",
        to_list=["paperwork@fedex.com"], cc_list=[],
        attachments=[],
        in_reply_to="<orig-msg-id@fedex.com>",
    )
    assert msg["Subject"] == "Re: FedEx / TRK#999"
    assert msg["In-Reply-To"] == "<orig-msg-id@fedex.com>"
    assert msg["References"] == "<orig-msg-id@fedex.com>"


def test_mime_message_existing_re_prefix_not_doubled():
    from monitor.customs_gmail_sender import _build_mime_message
    msg = _build_mime_message(
        subject="Re: previous reply",
        body_text="x",
        to_list=["paperwork@fedex.com"], cc_list=[],
        attachments=[],
        in_reply_to="<id@x.com>",
    )
    assert msg["Subject"] == "Re: previous reply"


def test_mime_message_quoted_original_appended():
    from monitor.customs_gmail_sender import _build_mime_message
    msg = _build_mime_message(
        subject="x",
        body_text="My reply",
        to_list=["paperwork@fedex.com"], cc_list=[],
        attachments=[],
        in_reply_to="<id@x.com>",
        quoted_original="Original line 1\nOriginal line 2",
    )
    body_part = msg.get_payload()[0]
    body = body_part.get_payload(decode=True).decode("utf-8")
    assert "My reply" in body
    assert "> Original line 1" in body
    assert "> Original line 2" in body


def test_mime_message_references_explicit_overrides():
    from monitor.customs_gmail_sender import _build_mime_message
    msg = _build_mime_message(
        subject="x", body_text="x",
        to_list=["a@fedex.com"], cc_list=[],
        attachments=[],
        in_reply_to="<m@x.com>",
        references="<old@x.com> <m@x.com>",
    )
    assert msg["References"] == "<old@x.com> <m@x.com>"


# ─────────────────────────────
# DB helpers for tests
# ─────────────────────────────

def _insert_drafted_request(status: str = "drafted") -> int:
    from monitor.database import get_conn
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO customs_requests
               (gmail_id, carrier, status, draft_subject, draft_body,
                draft_recipients, original_message_id, gmail_thread_id)
               VALUES (?, 'fedex', ?, ?, ?, ?, ?, ?)""",
            (f"test_w14ext_{status}_{id(object())}", status,
             "Test Subject", "Test body",
             json.dumps({"to": ["paperwork@fedex.com"], "cc": []}),
             "<orig@fedex.com>", "thread_xyz"),
        )
        return int(cur.lastrowid)


def _cleanup_request(req_id: int) -> None:
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "DELETE FROM customs_send_audit WHERE customs_request_id=?", (req_id,)
        )
        c.execute("DELETE FROM customs_requests WHERE id=?", (req_id,))


# ─────────────────────────────
# create_customs_draft
# ─────────────────────────────

def test_create_draft_kill_switch_blocks():
    from monitor.customs_gmail_sender import (
        CustomsSendBlocked, create_customs_draft,
    )
    req_id = _insert_drafted_request("drafted")
    try:
        with pytest.raises(CustomsSendBlocked, match="kill switch"):
            create_customs_draft(
                req_id, gmail_service=None,
                config={"customs_automation": {"send_enabled": False}},
            )
    finally:
        _cleanup_request(req_id)


def test_create_draft_invalid_status_blocks():
    from monitor.customs_gmail_sender import (
        CustomsSendBlocked, create_customs_draft,
    )
    req_id = _insert_drafted_request("manual")
    try:
        with pytest.raises(CustomsSendBlocked, match="not draftable"):
            create_customs_draft(
                req_id, gmail_service=MagicMock(),
                config={"customs_automation": {"send_enabled": True}},
            )
    finally:
        _cleanup_request(req_id)


def test_create_draft_calls_drafts_create_and_updates_db():
    from monitor.customs_gmail_sender import create_customs_draft
    from monitor.database import get_conn
    req_id = _insert_drafted_request("drafted")
    try:
        svc = MagicMock()
        svc.users().drafts().create().execute.return_value = {
            "id": "draft_xyz_123"
        }
        svc.users().messages().get().execute.return_value = {
            "payload": {"mimeType": "text/plain", "body": {}}
        }
        result = create_customs_draft(
            req_id, gmail_service=svc,
            config={"customs_automation": {"send_enabled": True}},
        )
        assert result.success is True
        assert result.draft_gmail_id == "draft_xyz_123"
        assert result.action == "created"
        with get_conn() as c:
            row = c.execute(
                "SELECT status, draft_gmail_id FROM customs_requests WHERE id=?",
                (req_id,),
            ).fetchone()
            assert row["status"] == "drafted_in_gmail"
            assert row["draft_gmail_id"] == "draft_xyz_123"
    finally:
        _cleanup_request(req_id)


def test_create_draft_uses_update_when_existing_draft_id():
    from monitor.customs_gmail_sender import create_customs_draft
    from monitor.database import get_conn
    req_id = _insert_drafted_request("drafted_in_gmail")
    with get_conn() as c:
        c.execute(
            "UPDATE customs_requests SET draft_gmail_id=? WHERE id=?",
            ("existing_draft_456", req_id),
        )
    try:
        svc = MagicMock()
        svc.users().drafts().update().execute.return_value = {
            "id": "existing_draft_456"
        }
        svc.users().messages().get().execute.return_value = {
            "payload": {"mimeType": "text/plain", "body": {}}
        }
        result = create_customs_draft(
            req_id, gmail_service=svc,
            config={"customs_automation": {"send_enabled": True}},
        )
        assert result.action == "updated"
        svc.users().drafts().update.assert_called()
    finally:
        _cleanup_request(req_id)


def test_create_draft_invalid_recipient_blocks():
    from monitor.customs_gmail_sender import (
        CustomsSendBlocked, create_customs_draft,
    )
    from monitor.database import get_conn
    req_id = _insert_drafted_request("drafted")
    with get_conn() as c:
        c.execute(
            "UPDATE customs_requests SET draft_recipients=? WHERE id=?",
            (json.dumps({"to": ["attacker@evil.com"], "cc": []}), req_id),
        )
    try:
        with pytest.raises(CustomsSendBlocked, match="allow-list"):
            create_customs_draft(
                req_id, gmail_service=MagicMock(),
                config={"customs_automation": {"send_enabled": True}},
            )
    finally:
        _cleanup_request(req_id)


def test_atomic_claim_accepts_drafted_in_gmail():
    """W14ext で drafted_in_gmail も atomic_claim 対象."""
    from monitor.customs_gmail_sender import _atomic_claim_for_sending
    req_id = _insert_drafted_request("drafted_in_gmail")
    try:
        claimed = _atomic_claim_for_sending(req_id)
        assert claimed is not None
        assert claimed["status"] == "sending"
    finally:
        _cleanup_request(req_id)


# ─────────────────────────────
# H-X1: 新規検出直後 (drafted_at=NOW) に「送信準備」が通ること
# ─────────────────────────────

def test_create_draft_immediately_after_detection_works():
    """task_customs_check は drafted_at=NOW で INSERT する.
    create_customs_draft の lock は draft_lock_at 専用列を使うので
    drafted_at に依存せず即時実行可能.
    """
    from monitor.customs_gmail_sender import create_customs_draft
    from monitor.database import get_conn
    req_id = _insert_drafted_request("drafted")
    # task_customs_check と同じ条件: drafted_at を CURRENT_TIMESTAMP に
    with get_conn() as c:
        c.execute(
            "UPDATE customs_requests SET drafted_at=CURRENT_TIMESTAMP WHERE id=?",
            (req_id,),
        )
    try:
        svc = MagicMock()
        svc.users().drafts().create().execute.return_value = {"id": "draft_immediate"}
        svc.users().messages().get().execute.return_value = {
            "payload": {"mimeType": "text/plain", "body": {}}
        }
        # drafted_at が直近でも create が通ること
        result = create_customs_draft(
            req_id, gmail_service=svc,
            config={"customs_automation": {"send_enabled": True}},
        )
        assert result.success is True
    finally:
        _cleanup_request(req_id)


# ─────────────────────────────
# H-X3: 例外時の draft_lock_at 解除 (即 retry 可能)
# ─────────────────────────────

def test_create_draft_releases_lock_on_exception():
    """drafts.create() で例外発生時、draft_lock_at が NULL に戻ること."""
    from monitor.customs_gmail_sender import (
        CustomsSendFailed, create_customs_draft,
    )
    from monitor.database import get_conn
    req_id = _insert_drafted_request("drafted")
    try:
        svc = MagicMock()
        # drafts.create で例外発生
        svc.users().drafts().create().execute.side_effect = OSError("network")
        svc.users().messages().get().execute.return_value = {
            "payload": {"mimeType": "text/plain", "body": {}}
        }
        with pytest.raises(CustomsSendFailed):
            create_customs_draft(
                req_id, gmail_service=svc,
                config={"customs_automation": {"send_enabled": True}},
            )
        # 例外後、draft_lock_at が NULL になっていること
        with get_conn() as c:
            row = c.execute(
                "SELECT draft_lock_at FROM customs_requests WHERE id=?",
                (req_id,),
            ).fetchone()
            assert row["draft_lock_at"] is None, \
                "draft_lock_at should be cleared on exception"
    finally:
        _cleanup_request(req_id)


def test_create_draft_clears_lock_on_success():
    """成功時、draft_lock_at が NULL に戻ること (次回更新が即実行可)."""
    from monitor.customs_gmail_sender import create_customs_draft
    from monitor.database import get_conn
    req_id = _insert_drafted_request("drafted")
    try:
        svc = MagicMock()
        svc.users().drafts().create().execute.return_value = {"id": "draft_ok"}
        svc.users().messages().get().execute.return_value = {
            "payload": {"mimeType": "text/plain", "body": {}}
        }
        create_customs_draft(
            req_id, gmail_service=svc,
            config={"customs_automation": {"send_enabled": True}},
        )
        with get_conn() as c:
            row = c.execute(
                "SELECT draft_lock_at, draft_gmail_id, status FROM customs_requests WHERE id=?",
                (req_id,),
            ).fetchone()
            assert row["draft_lock_at"] is None
            assert row["draft_gmail_id"] == "draft_ok"
            assert row["status"] == "drafted_in_gmail"
    finally:
        _cleanup_request(req_id)
