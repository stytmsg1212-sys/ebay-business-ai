#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRK#870480400096 (Netsuken NV-25 Steel-Aluminum-Copper case) の Gmail 下書き準備.

Kelly Medalla 指示通り subject は tracking number のみ.
TO: paperwork@fedex.com / CC: kelly.medalla.osv@fedex.com
Attachment: 記入済 WORKSHEET PDF (前段で生成済)
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn  # noqa: E402

CUST_REQ_ID = 186  # customs_requests.id

WORKSHEET_FILLED = (
    "C:/Users/gucch/projects/claude/.company/daily-operations/"
    "customs-attachments/870480400096/"
    "Steel-Aluminum-Copper-WORKSHEET_870480400096_FILLED.pdf"
)

SUBJECT = "870480400096"   # Kelly 指示「tracking number only on the subject line」

BODY = """\
Dear FedEx Logistics Team,

Thank you for your inquiry regarding the above shipment.
Please find attached the completed Steel-Aluminum-Copper Derivatives
worksheet for tracking number 870480400096.

Shipment Summary:
  Tracking Number: 870480400096
  Shipper: TOYOTASUMI (Japan)
  Consignee: Narongkorn Butsaboon (Chicago IL, USA)
  Ship Date: April 9, 2026
  Item: Netsuken NV-25 Electric Sushi Rice Warmer (2.5-Sho, 100V/47W)
  HS Code: 8516.60.4000
  Country of Origin: Japan
  Total Value: USD 798.00 / Net Weight: 8.5 kg

Material Composition (per Section 232 derivatives reporting):
  - Steel:    Yes (body + outer pot, ~50% by weight) — Melt/Pour country: Japan
  - Aluminum: Yes (inner pot, ~10% by weight)        — Smelt/Cast country: Japan
  - Copper:   Yes (minimal, in 100V power cord and 47W heating wire,
                  <1% by weight, de minimis range)   — Country: Japan

Manufacturer Information:
  Netsuken Co., Ltd. (株式会社 熱研)
  3-19-9 Motoasakusa, Taito-ku, Tokyo 111-0041, Japan
  Established 1956, all manufacturing facilities in Japan.

The shipper is a retailer and is not the manufacturer.

Please let me know if any additional information or documentation
is needed.

Best regards,
TOYOTASUMI
(Japanese eBay Seller)
"""


def update_request(*, dry_run: bool = False) -> None:
    """customs_requests #186 を Gmail draft 化に必要な状態に更新."""
    set_clauses = [
        "draft_subject=?", "draft_body=?", "draft_recipients=?",
        "recipient=?", "ship_date=?", "deadline=?",
        "ebay_item_id=?", "product_title=?",
        "attached_attachments=?",
        "status='drafted'",
        "draft_gmail_id=NULL", "draft_lock_at=NULL",
    ]
    params = [
        SUBJECT, BODY,
        json.dumps({
            "to": ["paperwork@fedex.com"],
            "cc": ["kelly.medalla.osv@fedex.com"],
        }, ensure_ascii=False),
        "Narongkorn Butsaboon",
        "2026-04-09",
        "2026-04-27",
        None,  # ebay_item_id 未解決 (sales_history に該当 sale なし)
        "Netsuken NV-25 Electric Sushi Rice Warmer 2.5-Sho 100V 47W",
        json.dumps([WORKSHEET_FILLED], ensure_ascii=False),
        CUST_REQ_ID,
    ]
    sql = f"UPDATE customs_requests SET {', '.join(set_clauses)} WHERE id=?"
    if dry_run:
        print(f"[DRY] UPDATE {CUST_REQ_ID}\n  TO=paperwork@fedex.com\n  "
              f"CC=kelly.medalla.osv@fedex.com\n  Subject={SUBJECT}\n  "
              f"Attached: WORKSHEET filled PDF")
        return
    with get_conn() as conn:
        conn.execute(sql, params)
    print(f"[UPDATE] id={CUST_REQ_ID} status=drafted")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not Path(WORKSHEET_FILLED).exists():
        print(f"ERROR: WORKSHEET filled PDF not found: {WORKSHEET_FILLED}",
              file=sys.stderr)
        print("先に: python -m scripts.fill_steel_alu_copper_worksheet_870480400096",
              file=sys.stderr)
        sys.exit(1)

    update_request(dry_run=args.dry_run)
    if args.dry_run:
        print("--- skip drafts.create() ---")
        return

    # Gmail drafts 作成
    from monitor.customs_gmail_sender import (
        create_customs_draft,
        CustomsSendBlocked, CustomsSendFailed,
    )
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    with io.open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    try:
        r = create_customs_draft(CUST_REQ_ID, config=cfg)
        print(f"[DRAFT] {r.action} draft_id={r.draft_gmail_id[:24]}...")
    except CustomsSendBlocked as e:
        print(f"[BLOCKED] {e}")
        sys.exit(2)
    except CustomsSendFailed as e:
        print(f"[FAILED] {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
