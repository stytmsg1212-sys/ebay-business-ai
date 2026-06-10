"""eBaymag 商品別 出品国レコメンド v2 (2026-06-09 user 方針 + 3者レビュー反映).

全国(全7サイト)の条件:
  - ブランド PLOTTER / Maxell / Google は全件全国 (user 確定: 消費者ブランド=先行指標)
  - それ以外は「人気度ランクS かつ 国際需要あり(global_only/mixed_global)」のみ全国
    (ランクS でも US_only/unknown は海外実績なし → 全国から外す。Pioneer 含む = user 委任)

その他:
  - 実買い手国(Terapeak countries_breakdown)を eBaymag サイトにマップし非US上位1-2
  - 非US実績なし → 出さない(US中心)

読み取り専用。CSV 出力のみ。
"""
import csv
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB = ROOT / "data" / "monitor.db"
OUT = ROOT / "data" / "ebaymag_country_plan_v2_2026_06_09.csv"

SITE = {"UK", "DE", "FR", "IT", "ES", "AU", "CA"}
C2S = {
    "GB": "UK", "IE": "UK",
    "DE": "DE", "AT": "DE", "CH": "DE", "NL": "DE", "BE": "DE", "LU": "DE",
    "NO": "DE", "SE": "DE", "DK": "DE", "FI": "DE", "PL": "DE", "CZ": "DE",
    "HU": "DE", "SK": "DE", "SI": "DE", "HR": "DE",
    "FR": "FR", "IT": "IT", "ES": "ES", "PT": "ES",
    "AU": "AU", "NZ": "AU", "CA": "CA",
}
ZENKOKU_BRANDS = {"PLOTTER", "MAXELL", "GOOGLE"}
INTL_MARKETS = {"global_only", "mixed_global"}


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
    cb_map = {}
    for r in c.execute(
        "SELECT ebay_item_id, countries_breakdown FROM market_analysis "
        "WHERE countries_breakdown IS NOT NULL ORDER BY scraped_at"
    ).fetchall():
        cb_map[r["ebay_item_id"]] = r["countries_breakdown"]

    out, stat, site_cnt = [], defaultdict(int), defaultdict(int)
    for r in rows:
        eid, title, pm = r["ebay_item_id"], r["title"] or "", r["primary_market"]
        b = brand1(title)
        if b in ZENKOKU_BRANDS:
            reason = f"brand:{b}(全件全国)"
            zen = True
        elif r["rank"] == "S" and pm in INTL_MARKETS:
            reason = f"rankS+{pm}(国際需要あり)"
            zen = True
        else:
            zen = False
        if zen:
            out.append([title, eid, r["current_price"], r["rank"], pm, "全国",
                        "UK,DE,FR,IT,ES,AU,CA", reason]); stat["全国"] += 1; continue
        # その他: 買い手国→サイト
        sc = defaultdict(int)
        raw = cb_map.get(eid)
        if raw:
            try:
                for e in json.loads(raw):
                    code, n = e.get("code"), e.get("count", 0)
                    if code == "US" or not code:
                        continue
                    s = C2S.get(code)
                    if s:
                        sc[s] += n
            except (json.JSONDecodeError, TypeError):
                pass
        top = sorted(sc.items(), key=lambda x: -x[1])[:2]
        if top:
            out.append([title, eid, r["current_price"], r["rank"], pm, "優先国",
                        ",".join(s for s, _ in top),
                        " ".join(f"{s}:{n}" for s, n in top)])
            stat["優先国"] += 1
            for s, _ in top:
                site_cnt[s] += 1
        else:
            out.append([title, eid, r["current_price"], r["rank"], pm,
                        "出さない", "", "非US実績なし"]); stat["出さない"] += 1

    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["商品名", "item_id", "価格USD", "人気度rank", "primary_market",
                    "区分", "出品国", "根拠"])
        w.writerows(out)
    print(f"出力: {OUT.name} ({len(out)}件)")
    print("=== v2 区分内訳 ===")
    for k, v in stat.items():
        print(f"  {k}: {v}")
    # 全国の内訳 (ブランド vs ランクS国際)
    zen_rows = [o for o in out if o[5] == "全国"]
    from collections import Counter
    print("=== 全国の primary_market 内訳 ===")
    for k, v in Counter(o[4] for o in zen_rows).most_common():
        print(f"  {k}: {v}")
    print("=== 優先サイト別 ===")
    for s, n in sorted(site_cnt.items(), key=lambda x: -x[1]):
        print(f"  {s}: {n}")
    conn.close()


if __name__ == "__main__":
    main()
