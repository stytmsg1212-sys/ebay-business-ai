#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W14 customs_mail_detector + customs_parser unit tests."""
from __future__ import annotations

import pytest

from monitor.customs_mail_detector import (
    DetectedMail, TRUSTED_SENDER_DOMAINS, _parse_gmail_message,
)
from monitor.customs_parser import (
    ParsedRequest, parse_mail,
    _detect_language, _extract_deadline, _extract_recipient,
    _extract_request_items, _extract_ship_date, _extract_tracking,
    _strip_html,
)


# ─────────────────────────────
# tracking 抽出
# ─────────────────────────────

def test_extract_tracking_fedex_trk():
    text = "Please refer to TRK#870904145187 in your reply."
    assert _extract_tracking(text, "fedex") == "870904145187"


def test_extract_tracking_fedex_awb():
    text = "Subject: FedEx AWB 870904145187"
    assert _extract_tracking(text, "fedex") == "870904145187"


def test_extract_tracking_ups_1z():
    text = "Your UPS tracking 1ZA12345678901234X is scheduled."
    assert _extract_tracking(text, "ups") == "1ZA12345678901234X"


def test_extract_tracking_not_found():
    assert _extract_tracking("no number here", "fedex") is None


# ─────────────────────────────
# recipient / deadline / ship_date
# ─────────────────────────────

def test_extract_recipient_consignee():
    text = "Consignee: CORAL KIEFER (USA)"
    assert _extract_recipient(text) == "CORAL KIEFER"


def test_extract_recipient_japanese():
    text = "宛先 : CORAL KIEFER 様 ( アメリカ向け )"
    assert _extract_recipient(text) == "CORAL KIEFER"


def test_extract_ship_date_japanese():
    text = "発送日: 04月22日"
    got = _extract_ship_date(text)
    assert got and got.endswith("-04-22")


def test_extract_deadline_japanese():
    text = "＜貨物の保管期限＞\n04月27日"
    got = _extract_deadline(text)
    assert got and got.endswith("-04-27")


def test_extract_deadline_iso():
    text = "Please respond by deadline: 2026-04-30"
    assert _extract_deadline(text) == "2026-04-30"


# ─────────────────────────────
# request_items / language
# ─────────────────────────────

def test_extract_request_items_numbered():
    text = """
    Please provide:
    1. Better description of the item
    2. Manufacturer Name and Address
    3. End use
    4. Composition
    """
    items = _extract_request_items(text)
    assert len(items) >= 4
    joined = " | ".join(items).lower()
    assert "description" in joined
    assert "manufacturer" in joined


def test_extract_request_items_bullet():
    text = "- Composition\n- Country of origin\n- End use"
    items = _extract_request_items(text)
    assert "Composition" in " ".join(items)


def test_detect_language_japanese():
    assert _detect_language("通関のため貨物の詳細の確認が必要です") == "ja"


def test_detect_language_english():
    assert _detect_language("Please provide detailed description of goods.") == "en"


def test_detect_language_mixed():
    text = "通関 for customs clearance is required."
    assert _detect_language(text) == "mixed"


# ─────────────────────────────
# strip_html
# ─────────────────────────────

def test_strip_html_removes_scripts():
    html = (
        "<html><body><script>alert('x')</script>"
        "<p>Hello <b>World</b></p></body></html>"
    )
    out = _strip_html(html)
    assert "script" not in out.lower() or "alert" not in out
    assert "Hello" in out
    assert "World" in out


# ─────────────────────────────
# mail_detector: SPF/DKIM validation
# ─────────────────────────────

def _build_gmail_msg(
    *, from_header: str, subject: str = "Test",
    auth_results: str = "", body_plain: str = "",
) -> dict:
    """Gmail API full-format メッセージの最小モック."""
    import base64
    body_b64 = base64.urlsafe_b64encode(body_plain.encode()).decode()
    return {
        "id": "test_msg_id_1",
        "payload": {
            "headers": [
                {"name": "From", "value": from_header},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Fri, 24 Apr 2026 10:00:00 +0900"},
                {"name": "Authentication-Results", "value": auth_results},
            ],
            "mimeType": "text/plain",
            "body": {"data": body_b64},
        },
    }


def test_parse_gmail_spf_dkim_pass():
    msg = _build_gmail_msg(
        from_header="FedEx <noreply@fedex.com>",
        auth_results="mx.google.com; spf=pass (google.com: ...) dkim=pass header.i=@fedex.com",
    )
    det = _parse_gmail_message(msg, "fedex")
    assert det is not None
    assert det.spf_dkim_ok is True
    assert det.sender_domain == "fedex.com"


def test_parse_gmail_spf_fail_still_returned_but_flagged():
    msg = _build_gmail_msg(
        from_header="FedEx <noreply@fedex.com>",
        auth_results="mx.google.com; spf=softfail dkim=fail",
    )
    det = _parse_gmail_message(msg, "fedex")
    assert det is not None
    assert det.spf_dkim_ok is False


def test_parse_gmail_untrusted_domain_rejected():
    msg = _build_gmail_msg(
        from_header="Fake FedEx <attacker@evil.com>",
        auth_results="spf=pass dkim=pass",
    )
    det = _parse_gmail_message(msg, "fedex")
    assert det is None   # trusted domain に含まれないので reject


def test_trusted_sender_domains_covers_3_carriers():
    for carrier in ("fedex", "dhl", "ups"):
        assert carrier in TRUSTED_SENDER_DOMAINS
        assert len(TRUSTED_SENDER_DOMAINS[carrier]) >= 1
