"""W322 夕方 refresh (19:30): AI店長「今夜の価格対応候補」digest 抽出.

設計書: .company/engineering/docs/2026-07-04-daily-workflow-design.md §4/§6

責務:
  competitor_snapshots に蓄積された定点観測 (毎日 05:30 + 19:30 の 2 回) から、
  同一 competitor_item_id の最新 2 件を比較し「競合が売れた / 在庫を増やした /
  値下げした」の 3 シグナルを検出する。Discord digest (tasks/task_evening_refresh.py)
  と 午後の作業タブ (tabs/tab_today_tasks.py) の両方が **本モジュールの抽出クエリを
  共有** することで、通知内容と画面表示の不一致を防ぐ (K1: 抽出ロジックを 1 箇所に)。

Phase1 は観測のみで自動値付けは行わない (pricing_eligible は一切変更しない)。
SKU 規約: 本モジュールは SKU を一切参照しない (listing 識別は competitor_item_id /
  our_item_id = ebay_item_id のみ、sku-rules.md 準拠)。
"""
from __future__ import annotations

from typing import Optional

from monitor.database import get_conn

# competitor_item_id 単位で最新 2 件を比較するクエリ (ROW_NUMBER で自己 JOIN)。
# captured_at は秒精度 (CURRENT_TIMESTAMP) のため同一秒内の複数 INSERT でタイが
# 発生し得る (05:30 実行が長引き複数 item を同秒で処理する場合等)。id (AUTOINCREMENT)
# を副次キーにして常に挿入順 = 新しい順を保証する。
#
# W322 追補 (2026-07-05, レビュー MED-1): recency 窓を「当日 (JST) に snapshot が
# 存在する競合のみ」に限定する。窓なしだと、snapshot が更新されない (= 05:30 の
# GetItem cap に外れた等で数日前の観測 2 件しか無い) 競合の古い変化が毎晩再掲
# されるため。captured_at は SQL `CURRENT_TIMESTAMP` DEFAULT = UTC 保存
# (sqlite-timezone.md、Python bind 系ではない例外扱いの逆側 = UTC で正しい) の
# ため `DATE(captured_at, '+9 hours') = DATE('now', '+9 hours')` で JST 今日へ shift。
# 「最新スナップショットが当日である」= cur.rn=1 の captured_at が JST 今日と一致
# を条件にする。prev は「前回」なので当日でなくてよい (むしろ前日 05:30 との比較が
# 主目的)。
_CANDIDATES_SQL = """
WITH ranked AS (
    SELECT competitor_item_id, our_item_id, quantity_sold, quantity_available,
           price_usd, captured_at,
           ROW_NUMBER() OVER (
               PARTITION BY competitor_item_id ORDER BY captured_at DESC, id DESC
           ) AS rn
    FROM competitor_snapshots
)
SELECT cur.competitor_item_id  AS competitor_item_id,
       cur.our_item_id         AS our_item_id,
       cur.quantity_sold       AS sold_now,
       prev.quantity_sold      AS sold_prev,
       cur.quantity_available  AS avail_now,
       prev.quantity_available AS avail_prev,
       cur.price_usd           AS price_now,
       prev.price_usd          AS price_prev,
       cur.captured_at         AS captured_at
FROM ranked cur
JOIN ranked prev
  ON prev.competitor_item_id = cur.competitor_item_id AND prev.rn = 2
WHERE cur.rn = 1
  AND DATE(cur.captured_at, '+9 hours') = DATE('now', '+9 hours')
"""


def _fmt_price(v: Optional[float]) -> str:
    return f"${v:.2f}" if v is not None else "?"


def _build_line(cand: dict) -> str:
    """1 行形式の共有テキスト (Discord content / タブ表示の両方で使う)."""
    title = cand.get("our_title") or cand.get("competitor_item_id") or "?"
    eid = cand.get("our_item_id") or ""
    suffix = f" ({eid[-4:]})" if eid else ""
    parts = []
    if cand.get("price_drop"):
        parts.append(f"値下げ {_fmt_price(cand['price_prev'])}→{_fmt_price(cand['price_now'])}")
    if cand.get("sold_delta"):
        parts.append(f"{cand['sold_delta']}個 売れた")
    if cand.get("avail_delta"):
        parts.append(f"在庫 +{cand['avail_delta']}")
    return f"• {title}{suffix} — " + " / ".join(parts)


def get_evening_price_candidates(limit: int = 10) -> list[dict]:
    """本日の「今夜の価格対応候補」候補一覧を返す (直近 2 スナップショット比較).

    シグナル判定 (いずれか true で候補入り):
      - price_drop: price_usd が前回より下がった (値下げ)
      - sold_delta > 0: quantity_sold が前回より増えた (売れた)
      - avail_delta > 0: quantity_available が前回より増えた (在庫を増やした)

    競合 snapshot が 1 件しかない (比較対象なし) 場合は対象外 (Q0: 誤検知回避).
    並び順: 値下げ額の大きい順 → sold_delta 降順 (対応優先度が高い順).
    """
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(_CANDIDATES_SQL).fetchall()]

    candidates: list[dict] = []
    for r in rows:
        price_now, price_prev = r.get("price_now"), r.get("price_prev")
        sold_now, sold_prev = r.get("sold_now"), r.get("sold_prev")
        avail_now, avail_prev = r.get("avail_now"), r.get("avail_prev")

        price_drop = (
            round(price_prev - price_now, 2)
            if price_now is not None and price_prev is not None and price_now < price_prev
            else 0
        )
        sold_delta = (
            sold_now - sold_prev
            if sold_now is not None and sold_prev is not None and sold_now > sold_prev
            else 0
        )
        avail_delta = (
            avail_now - avail_prev
            if avail_now is not None and avail_prev is not None and avail_now > avail_prev
            else 0
        )
        if not (price_drop or sold_delta or avail_delta):
            continue

        cand = {
            "competitor_item_id": r["competitor_item_id"],
            "our_item_id": r.get("our_item_id"),
            "price_now": price_now, "price_prev": price_prev, "price_drop": price_drop,
            "sold_delta": sold_delta, "avail_delta": avail_delta,
        }
        candidates.append(cand)

    if not candidates:
        return []

    # our_item_id → title 解決 (ebay_listings, 1 クエリで一括)
    our_item_ids = sorted({c["our_item_id"] for c in candidates if c.get("our_item_id")})
    titles: dict[str, str] = {}
    if our_item_ids:
        placeholders = ",".join("?" for _ in our_item_ids)
        with get_conn() as conn:
            title_rows = conn.execute(
                f"SELECT ebay_item_id, title FROM ebay_listings "
                f"WHERE ebay_item_id IN ({placeholders})",
                our_item_ids,
            ).fetchall()
        titles = {row["ebay_item_id"]: row["title"] for row in title_rows}

    for c in candidates:
        c["our_title"] = titles.get(c.get("our_item_id") or "")

    candidates.sort(key=lambda c: (c["price_drop"], c["sold_delta"]), reverse=True)
    candidates = candidates[:limit]

    for c in candidates:
        c["line"] = _build_line(c)
    return candidates


def format_digest_body(candidates: list[dict]) -> str:
    """Discord content / タブ表示で共有する本文テキスト."""
    if not candidates:
        return "本日は対応候補なし"
    return "\n".join(c["line"] for c in candidates)
