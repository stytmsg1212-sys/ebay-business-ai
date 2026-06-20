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
# _C2S の値域から導出 (SITE_MAP の 7 カ国と対応、ハードコード重複なし)
_ALL_SITES: list[str] = sorted(set(_C2S.values()))  # ['AU','CA','DE','ES','FR','IT','UK']

_ZENKOKU_BRANDS = {"PLOTTER", "MAXELL", "GOOGLE"}
_INTL = {"global_only", "mixed_global"}


def _brand1(title: str) -> str:
    m = re.match(r"([A-Za-z][A-Za-z0-9'&-]+)", (title or "").strip())
    return (m.group(1) if m else "").upper()


def _sites_from_countries_breakdown(raw: str | None) -> list[str]:
    """countries_breakdown JSON 文字列から非 US 実績サイトコードリストを返す.

    Args:
        raw: market_analysis.countries_breakdown の JSON 文字列。None / 空は [] を返す。

    Returns:
        実績のある eBaymag サイトコードのリスト (例: ["UK", "DE"])。重複なし・順序不定。
    """
    if not raw:
        return []
    sites: set[str] = set()
    try:
        for e in json.loads(raw):
            code = e.get("code")
            if code and code != "US" and _C2S.get(code) and (e.get("count", 0) > 0):
                sites.add(_C2S[code])
    except (json.JSONDecodeError, TypeError):
        pass
    return list(sites)


def resolve_priority_sites(ebay_item_id: str) -> list[str]:
    """単一 listing の優先国サイトコードリストを返す (W284 Phase1).

    market_analysis.countries_breakdown (Terapeak 買い手国別 sold) を読み、
    _C2S マップで国コード → eBaymag サイトコードに変換した非 US 実績国を返す。
    識別キーは ebay_item_id (SKU 禁止、sku-rules.md)。

    Args:
        ebay_item_id: 対象 listing の ebay_item_id。

    Returns:
        実績のあるサイトコードのリスト (例: ["UK", "DE"])。
        実績なし / データなし の場合は [] を返す。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT countries_breakdown FROM market_analysis "
            "WHERE ebay_item_id=? AND countries_breakdown IS NOT NULL "
            "ORDER BY scraped_at DESC LIMIT 1",
            (ebay_item_id,),
        ).fetchone()
    if row is None:
        return []
    return _sites_from_countries_breakdown(row["countries_breakdown"])


def resolve_desired_sites(
    ebay_item_id: str,
    segment: str,
    custom_sites: list[str] | None = None,
) -> list[str]:
    """区分から希望出品サイトコードリストを解決する (W284 Phase1).

    Args:
        ebay_item_id: 対象 listing の ebay_item_id。優先国区分の実績照会に使用。
        segment: "全国" / "優先国" / "カスタム" / "出さない"。
        custom_sites: segment="カスタム" 時の user 指定サイトコードリスト。
                      _ALL_SITES に含まれない無効コードは除外して正規化する。

    Returns:
        希望出品サイトコードのリスト。
        全国 → _ALL_SITES (7 カ国全部)
        優先国 → resolve_priority_sites(ebay_item_id)
        カスタム → custom_sites のうち有効コードのみ (custom_sites=None は [])
        出さない → []
    """
    if segment == "全国":
        return list(_ALL_SITES)
    if segment == "優先国":
        return resolve_priority_sites(ebay_item_id)
    if segment == "カスタム":
        if not custom_sites:
            return []
        valid = set(_ALL_SITES)
        return [s for s in custom_sites if s in valid]
    # "出さない" およびその他未知値
    return []


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
                # _sites_from_countries_breakdown を使って single source 化
                sites = _sites_from_countries_breakdown(cb.get(eid))
                seg = "優先国" if sites else "出さない"
            counts[seg] += 1
            updates.append((seg, eid))

        conn.executemany(
            "UPDATE ebay_listings SET ebaymag_segment=? WHERE ebay_item_id=?", updates
        )
    counts["total"] = len(updates)
    return counts
