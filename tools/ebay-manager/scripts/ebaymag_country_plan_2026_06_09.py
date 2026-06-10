"""eBaymag 商品別 出品国レコメンド生成 (2026-06-09 user 方針).

方針:
  ① 人気度ランクS / PLOTTER / Pioneer / Maxell → 全国 (eBaymag 7サイト)
  ② その他 → 実買い手国 (Terapeak countries_breakdown) を eBaymag サイトに
     マッピングして非US上位1-2サイトを優先国とする。非US実績なしは US中心扱い。

読み取り専用。CSV 出力のみ (eBaymag 操作はしない)。
"""
import csv
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB = ROOT / "data" / "monitor.db"
OUT = ROOT / "data" / "ebaymag_country_plan_2026_06_09.csv"

# eBaymag 対象サイト (UK/DE/FR/IT/ES/AU/CA) と買い手国→サイト対応
SITE = {"UK", "DE", "FR", "IT", "ES", "AU", "CA"}
C2S = {
    "GB": "UK", "IE": "UK",
    "DE": "DE", "AT": "DE", "CH": "DE", "NL": "DE", "BE": "DE", "LU": "DE",
    "NO": "DE", "SE": "DE", "DK": "DE", "FI": "DE", "PL": "DE", "CZ": "DE",
    "HU": "DE", "SK": "DE", "SI": "DE", "HR": "DE",
    "FR": "FR",
    "IT": "IT",
    "ES": "ES", "PT": "ES",
    "AU": "AU", "NZ": "AU",
    "CA": "CA",
}
ZENKOKU_BRANDS = {"PLOTTER", "PIONEER", "MAXELL"}


def brand1(t: str) -> str:
    m = re.match(r"([A-Za-z][A-Za-z0-9'&-]+)", (t or "").strip())
    return (m.group(1) if m else "").upper()


def main():
    conn = sqlite3.connect(str(DB)); conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute(
        "SELECT ebay_item_id, title, rank, primary_market, current_price "
        "FROM ebay_listings WHERE COALESCE(is_ended,0)=0 ORDER BY current_price DESC"
    ).fetchall()

    # 各 listing の最新 countries_breakdown
    cb_map = {}
    for r in c.execute(
        "SELECT ebay_item_id, countries_breakdown, scraped_at FROM market_analysis "
        "WHERE countries_breakdown IS NOT NULL ORDER BY scraped_at"
    ).fetchall():
        cb_map[r["ebay_item_id"]] = r["countries_breakdown"]  # 後勝ち=最新

    out = []
    stat = defaultdict(int)
    site_priority_count = defaultdict(int)
    for r in rows:
        eid = r["ebay_item_id"]; title = r["title"] or ""
        is_zenkoku = (r["rank"] == "S") or (brand1(title) in ZENKOKU_BRANDS)
        if is_zenkoku:
            reason = "rankS" if r["rank"] == "S" else f"brand:{brand1(title)}"
            out.append([title, eid, r["current_price"], r["rank"], r["primary_market"],
                        "全国", "UK,DE,FR,IT,ES,AU,CA", reason])
            stat["全国"] += 1
            continue
        # その他: 買い手国→サイト集計
        site_count = defaultdict(int)
        raw = cb_map.get(eid)
        if raw:
            try:
                for e in json.loads(raw):
                    code = e.get("code"); n = e.get("count", 0)
                    if code == "US" or not code:
                        continue
                    site = C2S.get(code)
                    if site:
                        site_count[site] += n
            except (json.JSONDecodeError, TypeError):
                pass
        top = sorted(site_count.items(), key=lambda x: -x[1])[:2]
        if top:
            prio = ",".join(s for s, _ in top)
            evid = " ".join(f"{s}:{n}" for s, n in top)
            out.append([title, eid, r["current_price"], r["rank"], r["primary_market"],
                        "優先国", prio, evid])
            stat["優先国あり"] += 1
            for s, _ in top:
                site_priority_count[s] += 1
        else:
            out.append([title, eid, r["current_price"], r["rank"], r["primary_market"],
                        "US中心(国際実績なし)", "", "non-US sold 0 or 非対応国のみ"])
            stat["US中心"] += 1

    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["商品名", "item_id", "価格USD", "人気度rank", "primary_market",
                    "区分", "出品国(eBaymagサイト)", "根拠"])
        w.writerows(out)

    print(f"出力: {OUT.name} ({len(out)} 件)")
    print("=== 区分内訳 ===")
    for k, v in stat.items():
        print(f"  {k}: {v}")
    print("=== その他の優先サイト別 (上位国として選ばれた回数) ===")
    for s, n in sorted(site_priority_count.items(), key=lambda x: -x[1]):
        print(f"  {s}: {n}")
    conn.close()


if __name__ == "__main__":
    main()
