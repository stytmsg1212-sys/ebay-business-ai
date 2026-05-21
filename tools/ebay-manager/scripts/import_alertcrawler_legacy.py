"""W148 — AlertCrawler legacy data.db 取込スクリプト (one-shot)。

設計書: .company/engineering/docs/2026-05-20-W148-alertcrawler-keyword-watch-design.md (v2.2)
db-migration-rules.md: bulk INSERT は migration 内ではなく one-shot script 化する。

入力: AlertCrawler の data.db (450 件)
出力: data/alertcrawler_legacy_export.json — UI 側で選別して add_watch()

⚠️ md-files-can-be-wrong R-1: 設計書には「SJIS デコード」とあるが、
実データは UTF-8 bytes encoded だった (2026-05-21 確認、第一行 dataC を実機 decode)。
text_factory=bytes で取り出し、UTF-8 decode を第一選択 / SJIS は fallback。

用法:
  python scripts/import_alertcrawler_legacy.py
  python scripts/import_alertcrawler_legacy.py --src "C:\\Users\\gucch\\Desktop\\work\\EBAY\\EBAY\\AlertCrawler\\data.db"
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

DEFAULT_SRC = Path(r"C:\Users\gucch\Desktop\work\EBAY\EBAY\AlertCrawler\data.db")
DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent / "data" / "alertcrawler_legacy_export.json"
)

logger = logging.getLogger(__name__)


def _decode(b: Optional[bytes]) -> str:
    """UTF-8 を第一選択、SJIS (cp932) を fallback。"""
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return b.decode("cp932")
        except UnicodeDecodeError:
            return b.decode("utf-8", errors="replace")


def _detect_site(url: str) -> Optional[str]:
    """URL host から site を判定 (yahoo_auctions / mercari のみ採用、他は除外)。"""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return None
    if "auctions.yahoo.co.jp" in host:
        return "yahoo_auctions"
    if "jp.mercari.com" in host:
        return "mercari"
    return None  # 他サイトは AlertCrawler に混在しているが本機能では未対応


def _parse_price_range(
    url: str,
    site: str,
    dataC: str,
) -> tuple[Optional[int], Optional[int]]:
    """価格レンジを取得。
    優先順位:
      1. URL query (yahoo: min/max, mercari: price_min/price_max) — 真実源
      2. dataC regex (補助情報、URL 欠落時の fallback)
    """
    try:
        q = parse_qs(urlparse(url).query)
    except Exception:
        q = {}

    pmin: Optional[int] = None
    pmax: Optional[int] = None

    if site == "yahoo_auctions":
        # ヤフオク URL: `min=&max=60000`
        mn = q.get("min", [""])[0].strip()
        mx = q.get("max", [""])[0].strip()
        if mn.isdigit():
            pmin = int(mn)
        if mx.isdigit():
            pmax = int(mx)
    elif site == "mercari":
        # メルカリ URL: `price_min=N&price_max=M`
        mn = q.get("price_min", [""])[0].strip()
        mx = q.get("price_max", [""])[0].strip()
        if mn.isdigit():
            pmin = int(mn)
        if mx.isdigit():
            pmax = int(mx)

    # dataC fallback: 「有:¥XX 無:¥YY 最安$ZZ」のような旧 AlertCrawler 形式
    # 設計書は「【価格】安:¥X 高:¥Y」想定だったが、実データは異形式。
    # URL 取得済ならスキップ (URL が真実源)。
    if pmin is None and pmax is None and dataC:
        # 「¥」または `\` で始まる JPY 値を抽出 (AlertCrawler 表記は `\58000`)
        nums = [int(n) for n in re.findall(r"[¥\\](\d{3,})", dataC)]
        if len(nums) >= 2:
            # 慣例: 1 つ目を「有 (送料込)」/ 2 つ目を「無 (送料抜)」と解釈する場合、
            # 価格レンジには小さい方を min、大きい方を max として安全側へ寄せる。
            pmin = min(nums[:2])
            pmax = max(nums[:2])

    return (pmin, pmax)


def _extract_keyword_from_url(url: str, site: str) -> str:
    """URL query から検索キーワードを抽出 (dataB が空の時の fallback)。"""
    try:
        q = parse_qs(urlparse(url).query)
    except Exception:
        return ""
    if site == "yahoo_auctions":
        return q.get("p", [""])[0]
    if site == "mercari":
        return q.get("keyword", [""])[0]
    return ""


def export_legacy_db(src: Path, out: Path) -> dict:
    """data.db を読んで JSON にダンプ。"""
    if not src.exists():
        raise FileNotFoundError(f"AlertCrawler data.db not found: {src}")
    conn = sqlite3.connect(str(src))
    conn.text_factory = bytes
    rows = conn.execute(
        "SELECT id, URL, item1, item2, item3, times, dataA, dataB, dataC, dataD, dataE "
        "FROM dataBase ORDER BY id"
    ).fetchall()
    conn.close()

    exported: list[dict] = []
    skipped: list[dict] = []
    for r in rows:
        rid = r[0]
        url = _decode(r[1])
        dataB = _decode(r[7])
        dataC = _decode(r[8])
        times = _decode(r[5])

        site = _detect_site(url)
        if not site:
            skipped.append({"id": rid, "reason": "unsupported site", "url": url[:100]})
            continue

        keyword = dataB.strip() or _extract_keyword_from_url(url, site)
        if not keyword:
            skipped.append({"id": rid, "reason": "no keyword", "url": url[:100]})
            continue

        pmin, pmax = _parse_price_range(url, site, dataC)
        exported.append({
            "legacy_id": rid,
            "site": site,
            "search_url": url,
            "keyword": keyword.strip(),
            "price_min_jpy": pmin,
            "price_max_jpy": pmax,
            "dataC_raw": dataC,
            "legacy_added_at": times,
            "selected": False,  # UI 側のチェックボックス初期値
        })

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({
            "source": str(src),
            "total_rows": len(rows),
            "exported": exported,
            "skipped": skipped,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result = {
        "total_rows": len(rows),
        "exported_count": len(exported),
        "skipped_count": len(skipped),
        "output_path": str(out),
    }
    logger.info(f"W148 import done: {result}")
    return result


def main():
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="W148 AlertCrawler legacy import")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC,
                        help=f"AlertCrawler data.db path (default: {DEFAULT_SRC})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"output JSON (default: {DEFAULT_OUT})")
    args = parser.parse_args()
    result = export_legacy_db(args.src, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
