"""W148 — キーワード新着監視 DB helpers (検索 URL : N 商品 hits 軸)。

設計書: .company/engineering/docs/2026-05-20-W148-alertcrawler-keyword-watch-design.md
DB schema: monitor/database.py v46 (keyword_watches / keyword_watch_hits)

sku-rules 適合: SKU を一切扱わない (検索キーワードと商品 URL のみ)。
claim-then-act dedupe = UNIQUE(watch_id, found_item_url) で物理排除。
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote_plus

from monitor.database import get_conn

logger = logging.getLogger(__name__)

# v2.1 センチネル (各サイトで「常にヒットが出る」watch、DOM変更/ban 検知用)
DEFAULT_SENTINELS = [
    {
        "site": "mercari",
        "keyword": "iPhone",
        "search_url": "https://jp.mercari.com/search?keyword=" + quote_plus("iPhone"),
    },
    {
        "site": "yahoo_auctions",
        "keyword": "iPhone",
        "search_url": "https://auctions.yahoo.co.jp/search/search?p=" + quote_plus("iPhone"),
    },
]


def add_watch(
    *,
    site: str,
    search_url: str,
    keyword: str,
    price_min_jpy: Optional[int] = None,
    price_max_jpy: Optional[int] = None,
    memo: str = "",
    source: str = "manual",
    is_sentinel: bool = False,
) -> tuple[int, bool]:
    """INSERT OR IGNORE で UNIQUE(site, search_url) 重複は静かに skip。
    Returns: (watch_id, inserted_new) — inserted_new=True なら新規 / False なら既存。"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO keyword_watches "
            "(site, search_url, keyword, price_min_jpy, price_max_jpy, memo, "
            " source, is_sentinel) VALUES (?,?,?,?,?,?,?,?)",
            (site, search_url, keyword, price_min_jpy, price_max_jpy, memo or "",
             source, 1 if is_sentinel else 0),
        )
        if cur.rowcount == 1:
            return (cur.lastrowid, True)
        row = conn.execute(
            "SELECT id FROM keyword_watches WHERE site=? AND search_url=?",
            (site, search_url),
        ).fetchone()
        return (row["id"], False)


def list_watches(active_only: bool = True) -> list[dict]:
    """active な watch を古い last_crawled_at 順 (NULL 優先) で返す = 公平 rotation。"""
    sql = (
        "SELECT * FROM keyword_watches "
        + ("WHERE is_active=1 " if active_only else "")
        + "ORDER BY (last_crawled_at IS NULL) DESC, last_crawled_at ASC, id ASC"
    )
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


_UPDATABLE_FIELDS = {"is_active", "price_min_jpy", "price_max_jpy", "memo", "keyword"}


def update_watch(watch_id: int, **fields) -> bool:
    """is_active / price_min_jpy / price_max_jpy / memo / keyword のみ更新可。
    search_url / site は不変 (変更したい場合は delete + add で別 watch 化)。"""
    safe = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS}
    if not safe:
        return False
    set_clause = ", ".join(f"{k}=?" for k in safe.keys())
    set_clause += ", updated_at=CURRENT_TIMESTAMP"
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE keyword_watches SET {set_clause} WHERE id=?",
            (*safe.values(), watch_id),
        )
        return cur.rowcount > 0


def delete_watch(watch_id: int) -> bool:
    with get_conn() as conn:
        conn.execute("DELETE FROM keyword_watch_hits WHERE watch_id=?", (watch_id,))
        cur = conn.execute("DELETE FROM keyword_watches WHERE id=?", (watch_id,))
        return cur.rowcount > 0


def record_hit_claim(
    *,
    watch_id: int,
    found_item_url: str,
    title: str,
    price_jpy: Optional[int],
    image_url: Optional[str],
    in_price_range: bool,
) -> Optional[int]:
    """claim-then-act: INSERT OR IGNORE で UNIQUE(watch_id, found_item_url) 重複は skip。
    rowcount=0 (既知 URL) → None を返す (重複 Discord 防止)。
    rowcount=1 (新規) → lastrowid を返す = caller が Discord 送信して
       mark_hit_notified(hit_id) を呼ぶ責任を持つ。"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO keyword_watch_hits "
            "(watch_id, found_item_url, title, price_jpy, image_url, "
            " in_price_range, detected_at) "
            "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (watch_id, found_item_url, title, price_jpy, image_url,
             1 if in_price_range else 0),
        )
        return cur.lastrowid if cur.rowcount == 1 else None


def mark_hit_notified(hit_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE keyword_watch_hits SET discord_sent=1, notified_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (hit_id,),
        )


def claim_hit_for_resend(hit_id: int) -> bool:
    """Codex 2 周目 HIGH-B: resend pass 二重送信防止の atomic claim.

    UPDATE ... SET discord_sent=1 WHERE id=? AND discord_sent=0 で送信権を獲得.
    rowcount==1 のみ True 返却 = 1 process だけが Discord 送信責任を持つ.

    呼出契約: True 返却したら _send_discord_for_hit を呼び、失敗時は
    release_hit_resend_claim(hit_id) で claim を巻き戻すこと.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE keyword_watch_hits "
            "SET discord_sent=1, notified_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND discord_sent=0",
            (hit_id,),
        )
        return cur.rowcount == 1


def release_hit_resend_claim(hit_id: int) -> None:
    """claim_hit_for_resend で claim 後の Discord 送信失敗時に呼ぶ.
    次回 crawl の resend pass で再 retry させるため discord_sent=0 に戻す.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE keyword_watch_hits "
            "SET discord_sent=0, notified_at=NULL "
            "WHERE id=?",
            (hit_id,),
        )


def get_unnotified_in_range_hits(days: int = 7, limit: int = 200) -> list[dict]:
    """Codex HIGH-3 (b): webhook 5xx 等で discord_sent=0 のまま残った in-range hit を
    crawl 末尾で resend するための一覧取得.

    7 日以内に detect された in-range で discord_sent=0 のものを最大 limit 件返す.
    watch info (site/keyword/memo/price_min/price_max) を JOIN して返却し、
    Discord embed 生成に必要な情報を一括で渡せる形にする."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT h.id AS hit_id, h.watch_id, h.found_item_url, h.title, "
            "       h.price_jpy, h.image_url, w.site, w.keyword, w.memo, "
            "       w.price_min_jpy, w.price_max_jpy "
            "FROM keyword_watch_hits h "
            "INNER JOIN keyword_watches w ON w.id = h.watch_id "
            "WHERE h.in_price_range = 1 AND h.discord_sent = 0 "
            "  AND h.detected_at >= datetime('now', ?) "
            "ORDER BY h.detected_at ASC "
            "LIMIT ?",
            (f"-{days} days", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_hits(watch_id: int, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM keyword_watch_hits WHERE watch_id=? "
            "ORDER BY detected_at DESC LIMIT ?",
            (watch_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_watch_stats() -> dict:
    """UI ヘッダ用統計。"""
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM keyword_watches").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM keyword_watches WHERE is_active=1"
        ).fetchone()[0]
        sentinel_active = conn.execute(
            "SELECT COUNT(*) FROM keyword_watches WHERE is_active=1 AND is_sentinel=1"
        ).fetchone()[0]
        h24 = conn.execute(
            "SELECT COUNT(*) FROM keyword_watch_hits "
            "WHERE detected_at >= datetime('now', '-24 hours')"
        ).fetchone()[0]
        d7 = conn.execute(
            "SELECT COUNT(*) FROM keyword_watch_hits "
            "WHERE detected_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
        last_crawl = conn.execute(
            "SELECT MAX(last_crawled_at) FROM keyword_watches"
        ).fetchone()[0]
        unnotified = conn.execute(
            "SELECT COUNT(*) FROM keyword_watch_hits "
            "WHERE discord_sent=0 AND in_price_range=1"
        ).fetchone()[0]
    return {
        "total": total,
        "active": active,
        "sentinel_active": sentinel_active,
        "hits_24h": h24,
        "hits_7d": d7,
        "last_crawl_at": last_crawl,
        "unnotified_in_range": unnotified,
    }


def update_watch_last_crawled(watch_id: int, error: Optional[str] = None) -> None:
    """last_crawled_at = now(UTC), last_error = error (None ならクリア)。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE keyword_watches SET last_crawled_at=datetime('now'), "
            "last_error=? WHERE id=?",
            (error, watch_id),
        )


def init_default_sentinels() -> int:
    """各サイトに DOM 健康センチネル watch を 1 件ずつ登録 (既登録は skip)。
    Returns: 新規登録件数。UI「センチネル初期化」ボタンが呼ぶ。
    price_min/max=None = 通知対象でない (健康確認用)。"""
    inserted = 0
    for s in DEFAULT_SENTINELS:
        _, new = add_watch(
            site=s["site"],
            search_url=s["search_url"],
            keyword=s["keyword"],
            memo="DOM/bot ban 検知用センチネル (自動登録、削除非推奨)",
            source="sentinel",
            is_sentinel=True,
        )
        if new:
            inserted += 1
    if inserted:
        logger.info(f"W148 init_default_sentinels: {inserted} 件新規登録")
    return inserted


def list_active_sentinels() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM keyword_watches WHERE is_active=1 AND is_sentinel=1"
        ).fetchall()
    return [dict(r) for r in rows]
