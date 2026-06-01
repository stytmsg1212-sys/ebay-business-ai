"""W148 — キーワード新着監視 DB helpers (検索 URL : N 商品 hits 軸)。

設計書: .company/engineering/docs/2026-05-20-W148-alertcrawler-keyword-watch-design.md
DB schema: monitor/database.py v46 (keyword_watches / keyword_watch_hits) +
           v59 (W206: keyword_watches.ebay_item_id)

sku-rules 適合: SKU を一切扱わない (検索キーワードと商品 URL のみ)。
claim-then-act dedupe = UNIQUE(watch_id, found_item_url) で物理排除。

W206 (2026-06-01): keyword_watches.ebay_item_id 列を追加。
  - **任意メタ** (NULL 可): この watch が紐づく自社 eBay 出品の listing ID。
  - **SKU ではない**: listing 識別は ebay_item_id 単位 (sku-rules.md)。
    UI から「相場上限の自社 listing と比較したい watch」に手動で紐付ける用途。
  - relist (Sell Similar) で旧 ItemID が新 ItemID に切替わる時は
    `tasks/task_daily_relist.py::inherit_listing_on_relist` が追従更新する。
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
    ebay_item_id: Optional[str] = None,
) -> tuple[int, bool]:
    """INSERT OR IGNORE で UNIQUE(site, search_url) 重複は静かに skip。
    Returns: (watch_id, inserted_new) — inserted_new=True なら新規 / False なら既存。

    ebay_item_id (W206): この watch を自社の eBay listing と紐付ける任意メタ。
    Discord 通知 embed に「eBay Item ID」「eBay 販売価格」を併記する用途。
    NULL 可。SKU ではなく listing 単位の ID (sku-rules.md)。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO keyword_watches "
            "(site, search_url, keyword, price_min_jpy, price_max_jpy, memo, "
            " source, is_sentinel, ebay_item_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (site, search_url, keyword, price_min_jpy, price_max_jpy, memo or "",
             source, 1 if is_sentinel else 0, ebay_item_id),
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


# search_url は keyword/価格レンジ変更時に UI が再生成して渡せる (W148-fix 2026-06-01)。
# site は不変 (変更したい場合は delete + add で別 watch 化)。
# ebay_item_id は W206 で追加 (任意メタ、None クリアも許可)。
_UPDATABLE_FIELDS = {
    "is_active", "price_min_jpy", "price_max_jpy", "memo", "keyword", "search_url",
    "ebay_item_id",
}


def update_watch(watch_id: int, **fields) -> bool:
    """is_active / price_min_jpy / price_max_jpy / memo / keyword / search_url /
    ebay_item_id (W206、任意メタ・None クリア可) のみ更新可。
    site は不変。search_url は UNIQUE(site, search_url) 制約があるため、別 watch と
    衝突する値への更新は IntegrityError を握りつぶさず呼び出し元に伝播させる
    (Q0: silent skip 禁止。UI 側で error 表示する)。"""
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
    watch info (site/keyword/memo/price_min/price_max/ebay_item_id) を JOIN して
    返却し、Discord embed 生成 (W206 拡張 embed 含む) に必要な情報を一括で渡せる形にする."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT h.id AS hit_id, h.watch_id, h.found_item_url, h.title, "
            "       h.price_jpy, h.image_url, w.site, w.keyword, w.memo, "
            "       w.price_min_jpy, w.price_max_jpy, w.ebay_item_id "
            "FROM keyword_watch_hits h "
            "INNER JOIN keyword_watches w ON w.id = h.watch_id "
            "WHERE h.in_price_range = 1 AND h.discord_sent = 0 "
            "  AND h.detected_at >= datetime('now', ?) "
            "ORDER BY h.detected_at ASC "
            "LIMIT ?",
            (f"-{days} days", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_ebay_meta_for_item_ids(item_ids: list[str]) -> dict[str, dict]:
    """W207 (W206 を拡張): ebay_listings から title + current_price を ebay_item_id IN (...)
    で一括取得 (N+1回避).

    Args:
        item_ids: ebay_item_id のリスト。None / 空文字列は除外する。

    Returns:
        {ebay_item_id: {'title': str | None, 'current_price': float | None}} の dict。
        - row が見つかった listing は title (None 可) と current_price (None 可) を含む。
        - 見つからなかった ebay_item_id は dict に含めない。
        - 空リスト/全 None なら {}。

    W207 用途:
      - title: AI 同一性判定 (claude_evaluator.evaluate_match) に渡す ebay_title.
      - current_price: Discord embed の「eBay 販売価格」表示 (USD only).

    sku-rules.md: listing 識別は ebay_item_id (SKU ではない)。
    """
    valid = [iid for iid in (item_ids or []) if iid]
    if not valid:
        return {}
    placeholders = ",".join("?" for _ in valid)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT ebay_item_id, title, current_price FROM ebay_listings "
            f"WHERE ebay_item_id IN ({placeholders})",
            valid,
        ).fetchall()
    result: dict[str, dict] = {}
    for r in rows:
        result[r["ebay_item_id"]] = {
            "title": r["title"],
            "current_price": float(r["current_price"]) if r["current_price"] is not None else None,
        }
    return result




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
