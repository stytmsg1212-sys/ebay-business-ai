"""eBaymag 出品国プラン区分 (ebaymag_segment) の再計算 (W242, 2026-06-09).

区分 (出品国プラン v2, user 確定):
  - 全国   : brand∈{PLOTTER,MAXELL,GOOGLE} OR (rank='S' AND primary_market∈{global_only,mixed_global})
  - 優先国 : 上記外で、実買い手国(Terapeak countries_breakdown)が eBaymag サイトにマップする非US国を持つ
  - 出さない: それ以外 (非US実績なし=eBaymag非対象=US本体のみ)

daily_relist は ebaymag_segment='出さない' のみ relist する (eBaymag各国版リンク破壊回避)。
本関数を market_analysis_refresh の末尾から呼ぶことで、新規取込/relist された listing も
定期的に再分類され、ebaymag_segment が NULL のまま放置されない (HIGH-2 対策)。
"""
import json
import re

from .database import get_conn

_C2S = {
    "GB": "UK", "IE": "UK", "DE": "DE", "AT": "DE", "CH": "DE", "NL": "DE",
    "BE": "DE", "LU": "DE", "NO": "DE", "SE": "DE", "DK": "DE", "FI": "DE",
    "PL": "DE", "CZ": "DE", "HU": "DE", "SK": "DE", "SI": "DE", "HR": "DE",
    "FR": "FR", "IT": "IT", "ES": "ES", "PT": "ES", "AU": "AU", "NZ": "AU", "CA": "CA",
}
_ZENKOKU_BRANDS = {"PLOTTER", "MAXELL", "GOOGLE"}
_INTL = {"global_only", "mixed_global"}


def _brand1(title: str) -> str:
    m = re.match(r"([A-Za-z][A-Za-z0-9'&-]+)", (title or "").strip())
    return (m.group(1) if m else "").upper()


def recompute_ebaymag_segments() -> dict:
    """全 active listing の ebaymag_segment を再計算して UPDATE.

    Returns: {"全国": n, "優先国": n, "出さない": n, "total": n}
    """
    with get_conn() as conn:
        # 各 listing の最新 countries_breakdown (後勝ち=最新 scraped_at)
        cb: dict[str, str] = {}
        for r in conn.execute(
            "SELECT ebay_item_id, countries_breakdown FROM market_analysis "
            "WHERE countries_breakdown IS NOT NULL AND ebay_item_id IS NOT NULL "
            "ORDER BY scraped_at"
        ).fetchall():
            cb[r["ebay_item_id"]] = r["countries_breakdown"]

        rows = conn.execute(
            "SELECT ebay_item_id, title, rank, primary_market FROM ebay_listings "
            "WHERE COALESCE(is_ended,0)=0"
        ).fetchall()

        counts = {"全国": 0, "優先国": 0, "出さない": 0}
        updates = []
        for r in rows:
            eid, title, pm = r["ebay_item_id"], r["title"] or "", r["primary_market"]
            if _brand1(title) in _ZENKOKU_BRANDS or (r["rank"] == "S" and pm in _INTL):
                seg = "全国"
            else:
                sites = set()
                raw = cb.get(eid)
                if raw:
                    try:
                        for e in json.loads(raw):
                            code = e.get("code")
                            if code and code != "US" and _C2S.get(code) and (e.get("count", 0) > 0):
                                sites.add(_C2S[code])
                    except (json.JSONDecodeError, TypeError):
                        pass
                seg = "優先国" if sites else "出さない"
            counts[seg] += 1
            updates.append((seg, eid))

        conn.executemany(
            "UPDATE ebay_listings SET ebaymag_segment=? WHERE ebay_item_id=?", updates
        )
    counts["total"] = len(updates)
    return counts
