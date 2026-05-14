#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRK#870904145187 (ONYX BOOX Leaf2) の 2 通の reply ドラフトを準備する one-shot script.

W14ext を活用して Gmail 下書きまで作成する.

対象:
  #5 (Kanako/FedEx Japan, 日本語) → 日本語短報を Kanako 個人宛に返信
  #7 (Jayson/FedEx US OSV, 英語)  → 英文 customs info を paperwork@ + Jayson 宛に返信
  #6 (Kanako 重複)                → manual に降格
  #8, #9 (paperwork@ 自動リマインダ) → manual のまま (#7 で paperwork@ に届く)

実行:
    python -m scripts.prepare_drafts_870904145187 [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn  # noqa: E402


# ─────────────────────────────────────────────
# 日本語短報 (Kanako 様向け)
# ─────────────────────────────────────────────

JA_SUBJECT = "Re: FedEx / アメリカ向け貨物につきまして TRK#870904145187"
JA_BODY = """\
安原様

お世話になっております。TOYOTASUMI です。
ご連絡ありがとうございました。

TRK#870904145187 の通関情報について、本日 paperwork@fedex.com 宛に
英文で提出いたしました（CC: 5259134@fedex.com / jayson.lumbang.osv@fedex.com）。

提出内容の概要:
- 商品: ONYX BOOX Leaf2 White 7" 電子書籍リーダー (中古)
- 製造元: SKT 株式会社 (〒583-0017 大阪府藤井寺市藤ケ丘 4-1-37, TEL: 072-989-2911)
- 最終用途: 個人の電子書籍リーダーとしての使用
- 素材: プラスチック/ガラス/電子部品/Li-ion 電池、アルミ・鉄使用なし
- HTS 参考コード: 8543.70.9200 (米税関 Ruling NY N215220)
- 商品写真 5 枚を添付

ご確認のほど、よろしくお願いいたします。

TOYOTASUMI
"""

# ─────────────────────────────────────────────
# 英文 customs info (Jayson + paperwork@ 向け)
# ─────────────────────────────────────────────

EN_SUBJECT = "Re: FedEx AWB 870904145187 - Customs Clearance Information"
EN_BODY = """\
Dear FedEx Team,

Thank you for your email regarding the above shipment.
Please find the requested manufacturer information below in order to
proceed with customs clearance.

Tracking Number: 870904145187

Description of Goods:
ONYX BOOX Leaf2 White - a 7-inch portable E-Ink electronic book reader
with physical page-turn buttons. It consists of a plastic housing
(ABS/polycarbonate), a glass E-Ink display surface, a printed circuit
board with a processor and memory, and a built-in rechargeable
lithium-ion battery. No aluminum or steel parts requiring country of
smelt/cast or melt/pour.

End Use:
Personal e-book reading device for the consignee's own use.

Manufacturer Information:
SKT Co., Ltd.
4-1-37 Fujigaoka, Fujiidera-shi, Osaka 583-0017, Japan
Tel: +81-72-989-2911

Suggested HTSUS Classification (for reference):
8543.70.9200 - "Electrical machines and apparatus with translation or
dictionary functions" (the classification used by U.S. CBP for similar
e-readers, e.g., CBP Ruling NY N215220). Please verify with your
customs broker.

Five reference product photographs are attached.

The shipper is a retailer and is not the manufacturer.

Please let me know if any additional information or documentation
is needed.

Best regards,
TOYOTASUMI
(Japanese eBay Seller)
"""


def update_request(req_id: int, *,
                   subject: str, body: str,
                   to_list: list[str], cc_list: list[str],
                   status: str = "drafted",
                   clear_draft_gmail_id: bool = True,
                   clear_lock: bool = True,
                   dry_run: bool = False) -> None:
    """customs_requests を新ドラフト用にリセット.

    既に Gmail draft が存在する場合は draft_gmail_id をクリアして
    強制 drafts.create() 経路に. drafted_in_gmail の Gmail 上の旧 draft は
    user が手動で削除可能.
    """
    set_clauses = [
        "draft_subject=?", "draft_body=?", "draft_recipients=?",
        "status=?",
    ]
    params: list = [
        subject, body,
        json.dumps({"to": to_list, "cc": cc_list}, ensure_ascii=False),
        status,
    ]
    if clear_draft_gmail_id:
        set_clauses.append("draft_gmail_id=NULL")
    if clear_lock:
        set_clauses.append("draft_lock_at=NULL")

    sql = (
        f"UPDATE customs_requests SET {', '.join(set_clauses)} "
        f"WHERE id=?"
    )
    params.append(req_id)
    if dry_run:
        print(f"[DRY] UPDATE id={req_id} TO={to_list} CC={cc_list} status={status}")
        print(f"      subject: {subject}")
        return
    with get_conn() as conn:
        conn.execute(sql, params)
    print(f"[UPDATE] id={req_id} TO={to_list} status={status}")


def mark_manual(req_id: int, reason: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"[DRY] mark_manual id={req_id} ({reason})")
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE customs_requests SET status='manual', "
            "error_msg=COALESCE(error_msg,'') || ? WHERE id=? "
            "AND status NOT IN ('sent','sending')",
            (f" [auto-marked manual: {reason}]", req_id),
        )
    print(f"[MANUAL] id={req_id} ({reason})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-create-drafts", action="store_true",
        help="DB UPDATE のみで Gmail drafts.create() 呼び出しは skip",
    )
    args = parser.parse_args()

    dry = args.dry_run

    # 1. #5 (Kanako 4/24 04:53) を日本語短報に書き換え
    update_request(
        req_id=5,
        subject=JA_SUBJECT,
        body=JA_BODY,
        to_list=["kanako.yasuhara@fedex.com"],
        cc_list=[],
        dry_run=dry,
    )

    # 2. #7 (Jayson) を英文 customs info に書き換え
    #    + product 情報セット
    update_request(
        req_id=7,
        subject=EN_SUBJECT,
        body=EN_BODY,
        to_list=["paperwork@fedex.com"],
        cc_list=["jayson.lumbang.osv@fedex.com", "5259134@fedex.com"],
        dry_run=dry,
    )
    if not dry:
        with get_conn() as c:
            c.execute(
                "UPDATE customs_requests SET recipient=?, ebay_item_id=?, "
                "product_title=?, deadline=? WHERE id=?",
                ("CORAL KIEFER", "357618434395",
                 "ONYX BOOX Leaf2 White 7-inch E Ink Reader with "
                 "Page-Turn Buttons Used fro Japan",
                 "2026-04-27", 7),
            )

    # 3. #6 を manual (Kanako 重複)
    mark_manual(6, "duplicate of #5 (same Kanako thread)", dry_run=dry)

    # 4. #8, #9 は既に manual のまま (paperwork@ 自動リマインダで #7 が代替)
    print("[NOTE] #8, #9 (automated paperwork@ reminders) remain manual; "
          "#7 reply will reach paperwork@ on the same thread.")

    if args.skip_create_drafts or dry:
        print("\n--- Gmail drafts.create() を skip ---")
        return

    # 5. Gmail drafts 作成
    from monitor.customs_gmail_sender import (
        create_customs_draft,
        CustomsSendBlocked, CustomsSendFailed,
    )
    import io
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    with io.open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    for rid in (5, 7):
        try:
            r = create_customs_draft(rid, config=cfg)
            print(f"[DRAFT] id={rid} -> {r.action} draft_id={r.draft_gmail_id[:24]}...")
        except CustomsSendBlocked as e:
            print(f"[BLOCKED] id={rid}: {e}")
        except CustomsSendFailed as e:
            print(f"[FAILED] id={rid}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] id={rid}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
