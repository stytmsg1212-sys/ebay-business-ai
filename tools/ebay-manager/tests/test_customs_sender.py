#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W14 customs_gmail_sender unit tests."""
from __future__ import annotations

import json
import pytest

from monitor.customs_gmail_sender import (
    CustomsSendBlocked, _atomic_claim_for_sending, _hash_recipients,
    _validate_recipients, send_customs_reply,
)


@pytest.fixture(autouse=True)
def _init_customs_schema(_isolate_monitor_db):
    """conftest が DB_PATH を tmp に隔離した後に customs schema を作成 (W187).

    2026-05-25 の conftest autouse 隔離で空 tmp DB に差し替わり、
    customs_requests / customs_send_audit を触る test が 'no such table' で
    fail していた回帰の修正。_isolate_monitor_db を要求して順序保証。init_db 冪等。
    """
    from monitor.database import init_db
    init_db()


# ─────────────────────────────
# allow-list validation (H-1)
# ─────────────────────────────

def test_validate_recipients_fedex_accepts_paperwork():
    _validate_recipients("fedex", ["paperwork@fedex.com"], [])


def test_validate_recipients_fedex_accepts_osv_cc():
    _validate_recipients(
        "fedex", ["paperwork@fedex.com"],
        ["jayson.lumbang.osv@fedex.com", "5259134@fedex.com"],
    )


def test_validate_recipients_fedex_rejects_external():
    with pytest.raises(CustomsSendBlocked, match="allow-list"):
        _validate_recipients(
            "fedex", ["paperwork@fedex.com"], ["attacker@evil.com"]
        )


def test_validate_recipients_empty_to_rejected():
    with pytest.raises(CustomsSendBlocked, match="empty TO"):
        _validate_recipients("fedex", [], ["paperwork@fedex.com"])


def test_validate_recipients_case_insensitive():
    _validate_recipients("fedex", ["PAPERWORK@FEDEX.COM"], [])


def test_validate_recipients_ups_importbrokerage():
    _validate_recipients("ups", ["importbrokerage@ups.com"], [])


def test_validate_recipients_dhl_domain_ok():
    _validate_recipients("dhl", ["customs@dhl.com"], [])


# ─────────────────────────────
# atomic claim (H-4)
# ─────────────────────────────

def _insert_test_request(status: str = "drafted") -> int:
    from monitor.database import get_conn
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO customs_requests (gmail_id, carrier, status, draft_body,
                                              draft_subject, draft_recipients)
               VALUES (?, 'fedex', ?, ?, ?, ?)""",
            (f"test_sender_{status}_{pytest.__version__}", status,
             "test body", "test subject",
             json.dumps({"to": ["paperwork@fedex.com"], "cc": []})),
        )
        return int(cur.lastrowid)


def _cleanup(req_id: int) -> None:
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute("DELETE FROM customs_send_audit WHERE customs_request_id = ?",
                  (req_id,))
        c.execute("DELETE FROM customs_requests WHERE id = ?", (req_id,))


def test_atomic_claim_only_works_on_drafted():
    req_id = _insert_test_request(status="drafted")
    try:
        claimed = _atomic_claim_for_sending(req_id)
        assert claimed is not None
        assert claimed["status"] == "sending"
        # 2回目は既に sending なので None
        claimed2 = _atomic_claim_for_sending(req_id)
        assert claimed2 is None
    finally:
        _cleanup(req_id)


def test_atomic_claim_rejects_sent_state():
    req_id = _insert_test_request(status="sent")
    try:
        assert _atomic_claim_for_sending(req_id) is None
    finally:
        _cleanup(req_id)


# ─────────────────────────────
# kill switch (追加推奨 3)
# ─────────────────────────────

def test_send_reply_blocked_by_kill_switch():
    req_id = _insert_test_request(status="drafted")
    try:
        with pytest.raises(CustomsSendBlocked, match="kill switch"):
            send_customs_reply(
                req_id, gmail_service=None,
                config={"customs_automation": {"send_enabled": False}},
            )
        # status は drafted のまま (atomic_claim 前の kill switch なので未変更)
        from monitor.database import get_conn
        with get_conn() as c:
            row = c.execute(
                "SELECT status FROM customs_requests WHERE id = ?", (req_id,),
            ).fetchone()
            assert row["status"] == "drafted"
    finally:
        _cleanup(req_id)


# ─────────────────────────────
# recipients_hash stability
# ─────────────────────────────

def test_hash_recipients_deterministic_regardless_of_order():
    h1 = _hash_recipients(["a@fedex.com", "b@fedex.com"], ["c@fedex.com"])
    h2 = _hash_recipients(["b@fedex.com", "a@fedex.com"], ["C@FEDEX.COM"])
    assert h1 == h2  # order + case invariant
