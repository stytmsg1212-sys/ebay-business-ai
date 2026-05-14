#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRK#870480400096 (Netsuken NV-25) の Steel-Aluminum-Copper Derivatives WORKSHEET 自動記入.

flat PDF (フォームフィールドなし) なので reportlab で overlay PDF 作成 →
pypdf でマージして flat な記入済 PDF を生成.

実行:
    python -m scripts.fill_steel_alu_copper_worksheet_870480400096
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfReader, PdfWriter  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

# 元 PDF と出力先
SRC = Path(
    "C:/Users/gucch/projects/claude/.company/daily-operations/"
    "customs-attachments/870480400096/Steel_-_Aluminum_-_Copper__WORKSHEET__1_.pdf"
)
DST = Path(
    "C:/Users/gucch/projects/claude/.company/daily-operations/"
    "customs-attachments/870480400096/"
    "Steel-Aluminum-Copper-WORKSHEET_870480400096_FILLED.pdf"
)

PAGE_W, PAGE_H = 612, 792  # US Letter

# pdfplumber は top 座標を返す (上から下に増える)
# PDF 座標は bottom 原点 (下から上に増える) → pdf_y = PAGE_H - top - height_offset
# テキストはベースライン基準なので top + ~9 程度を引いて配置

def to_pdf_y(top: float, baseline_offset: float = 9.0) -> float:
    """pdfplumber の top → reportlab の y (ベースライン)"""
    return PAGE_H - top - baseline_offset


def main():
    print(f"input : {SRC}")
    print(f"output: {DST}")

    # overlay 構築
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setFont("Helvetica", 10)

    # ── Header (y=89) ──
    # AWB / Part number / Description
    c.drawString(67, to_pdf_y(89), "870480400096")
    c.drawString(250, to_pdf_y(89), "NV-25 (Netsuken)")
    c.drawString(451, to_pdf_y(89), "Sushi Rice Warmer")

    # ── Q1-Q3 (Aluminum/Copper/Steel) ──
    # 該当する Yes 側にチェック X
    # Q1 Aluminum: Yes blank x=317.8-350.8 / y=114
    c.drawString(330, to_pdf_y(114), "X")
    # Q2 Copper: Yes blank x=305.8-338.8 / y=138
    c.drawString(317, to_pdf_y(138), "X")
    # Q3 Steel: Yes blank x=293.5-326.5 / y=163
    c.drawString(305, to_pdf_y(163), "X")

    # ── Q4 Aluminum (y=238, 260, 282) ──
    # Full weight 8.5kg, Al derivative ~0.8kg
    c.drawString(160, to_pdf_y(238), "8.5")
    c.drawString(370, to_pdf_y(238), "0.8")
    # Value $798, Value of Al ~$80
    c.drawString(127, to_pdf_y(260), "USD 798.00")
    c.drawString(346, to_pdf_y(260), "USD 80.00")
    # Country of Smelt / Secondary / Cast
    c.drawString(113, to_pdf_y(282), "Japan")
    c.drawString(284, to_pdf_y(282), "Japan")
    c.drawString(401, to_pdf_y(282), "Japan")

    # ── Q5 Copper (y=438, 460) ──
    c.drawString(160, to_pdf_y(438), "8.5")
    c.drawString(358, to_pdf_y(438), "0.05")
    c.drawString(127, to_pdf_y(460), "USD 798.00")
    c.drawString(343, to_pdf_y(460), "USD 5.00")

    # ── Q6 Steel (y=524, 546, 568) ──
    c.drawString(160, to_pdf_y(524), "8.5")
    c.drawString(347, to_pdf_y(524), "4.0")
    c.drawString(127, to_pdf_y(546), "USD 798.00")
    c.drawString(333, to_pdf_y(546), "USD 400.00")
    # Steel Melt/Pour country
    c.drawString(142, to_pdf_y(568), "Japan")

    # ── Manufacturer info (y=631-747) ──
    c.drawString(107, to_pdf_y(631), "Netsuken Co., Ltd.")
    c.drawString(69, to_pdf_y(650), "3-19-9 Motoasakusa, Taito-ku, Tokyo 111-0041, Japan")
    c.drawString(89, to_pdf_y(670), "TOYOTASUMI")
    c.drawString(53, to_pdf_y(689), "Seller (eBay International Shipper)")
    c.drawString(60, to_pdf_y(708), "styt.msg1212@gmail.com")
    c.drawString(74, to_pdf_y(728), "TOYOTASUMI")
    c.drawString(57, to_pdf_y(747), "2026-04-25")

    c.save()

    # overlay と元 PDF をマージ
    overlay_reader = PdfReader(io.BytesIO(buf.getvalue()))
    src_reader = PdfReader(str(SRC))

    writer = PdfWriter()
    src_page = src_reader.pages[0]
    src_page.merge_page(overlay_reader.pages[0])
    writer.add_page(src_page)

    with open(DST, "wb") as f:
        writer.write(f)
    print(f"\nDONE: {DST.stat().st_size} bytes")


if __name__ == "__main__":
    main()
