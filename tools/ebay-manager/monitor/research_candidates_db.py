#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 商品リサーチ自動化 フェーズB MVP: research_candidates CRUD.

設計書: .company/engineering/docs/2026-06-07-product-research-automation-spec.md
ロック: PoC は「1 商品手入力 → フリマ探索 + AI 同一性判定 → 着地」だけ。
       出品 / キーワード監視登録 / 仕入購入は実装しない (§8 P2 / ビルド順 step1-2)。

ルール準拠:
  - listing 識別は独自 PK `rc_id` (P0-1 設計): SKU 規約 (sku-rules.md) は出品済 entity
    の話で、未出品 research_candidate は ebay_item_id を持たない (将来出品時に
    `ebay_listings.ebay_item_id` で別 entity に昇格させる構想)。
    → ここで ebay_item_id 列を作って sentinel `rc:<n>` を挿す案は W185 (UNIQUE
      ebay_item_id ベース) を汚染するので採らない。
  - status は state machine (P0-2): 未定義文字列を黙って受け入れない。`update_status`
    が遷移先を `_VALID_STATUSES` で validate (Q0 silent skip 防止: 不正値は ValueError)。
  - silent skip 防止 (Q0): `needs_review` に落とす時は `needs_review_reason` 必須。
  - K1 Simplicity First: PoC スコープ = new / sourcing / sourced / not_found /
    needs_review の 5 status のみ。listed / approved 系は将来拡張で別 PR。
"""
from __future__ import annotations

import logging
from typing import Optional

from .database import get_conn

logger = logging.getLogger(__name__)

# ---- 状態機械定数 (P0-2) ----------------------------------------------------
# PoC スコープ 5 status。出品関連 (gate_passed / awaiting_identity_approval /
# listed / listed_oos_monitored) は未実装の旨を将来追加するときに INSERT する。
# DB の status 列は CHECK 制約を持たない (後方互換 / 値拡張時の migration コスト
# 削減)。本モジュール経由の更新でだけ validate する = 直接 SQL は assistant 責任。
STATUS_NEW = "new"
STATUS_SOURCING = "sourcing"
STATUS_SOURCED = "sourced"
STATUS_NOT_FOUND = "not_found"
STATUS_NEEDS_REVIEW = "needs_review"

_VALID_STATUSES: set[str] = {
    STATUS_NEW,
    STATUS_SOURCING,
    STATUS_SOURCED,
    STATUS_NOT_FOUND,
    STATUS_NEEDS_REVIEW,
}

# 許容遷移グラフ (PoC スコープ)。spec §8 P0-2 「明示的な状態機械」要件。
#  new       → sourcing / needs_review (人手入力直後の異常終了)
#  sourcing  → sourced / not_found / needs_review (探索結果 3 分岐 = §2-C 表)
#  sourced   → needs_review (後段 review で取り下げ)
#  not_found → sourcing (再探索) / needs_review
#  needs_review → sourcing (修正後再試行) / sourced (人手で利益確定し承認) / not_found
# 値は frozenset で immutable 化 (test の意図しない mutate を防止)。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_NEW: frozenset({STATUS_SOURCING, STATUS_NEEDS_REVIEW}),
    STATUS_SOURCING: frozenset(
        {STATUS_SOURCED, STATUS_NOT_FOUND, STATUS_NEEDS_REVIEW}
    ),
    STATUS_SOURCED: frozenset({STATUS_NEEDS_REVIEW}),
    STATUS_NOT_FOUND: frozenset({STATUS_SOURCING, STATUS_NEEDS_REVIEW}),
    STATUS_NEEDS_REVIEW: frozenset(
        {STATUS_SOURCING, STATUS_SOURCED, STATUS_NOT_FOUND}
    ),
}


def can_transition(old: str, new: str) -> bool:
    """状態機械: old → new 遷移が許容されるか (同値 no-op は False)."""
    return new in _ALLOWED_TRANSITIONS.get(old, frozenset())


# ---- CRUD ------------------------------------------------------------------

def insert_research_candidate(
    title_ja: str,
    *,
    manual_weight_g: Optional[float] = None,
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    terapeak_avg_price_usd: Optional[float] = None,
    status: str = STATUS_NEW,
) -> int:
    """research_candidate を 1 件作成して rc_id を返す.

    探索前の "手入力済" 状態 (status=new)。フリマ探索後に
    `update_research_candidate_result` で found_* / match_* / estimated_profit_usd
    と status (sourced/not_found/needs_review) を埋める。

    Q0: title_ja は必須 (空/None は ValueError)。後段で「無題で needs_review」と
    silent に落とす穴を最初から塞ぐ。
    """
    if not title_ja or not title_ja.strip():
        raise ValueError("title_ja is required (empty not allowed)")
    # Codex 2段指摘#2: insert が任意 valid status を受け付けると、状態機械を迂回して
    # needs_review (reason 必須) や sourced を直接作れてしまう。insert は **必ず初期状態
    # 'new'** のみ。以降の遷移は update_status (遷移検証 + reason 必須) を通す。
    if status != STATUS_NEW:
        raise ValueError(
            f"insert_research_candidate only creates status='new' (got {status!r}); "
            "use update_status for transitions (state-machine integrity / Q0)"
        )

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO research_candidates "
            "(title_ja, manual_weight_g, length_cm, width_cm, height_cm, "
            " terapeak_avg_price_usd, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                title_ja.strip(),
                manual_weight_g,
                length_cm,
                width_cm,
                height_cm,
                terapeak_avg_price_usd,
                status,
            ),
        )
        rc_id = cur.lastrowid
    if rc_id is None:
        # SQLite AUTOINCREMENT で lastrowid=None は理論上ありえないが Q0 防御。
        raise RuntimeError("insert_research_candidate: lastrowid is None")
    return int(rc_id)


def get_research_candidate(rc_id: int) -> Optional[dict]:
    """rc_id で 1 件取得 (見つからなければ None)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM research_candidates WHERE rc_id=?", (rc_id,)
        ).fetchone()
    return dict(row) if row else None


def list_research_candidates(
    status: Optional[str] = None, limit: int = 200
) -> list[dict]:
    """status で絞り込んで新しい順に返す. status=None で全件 (limit 上限)."""
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(f"invalid status filter: {status!r}")
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM research_candidates WHERE status=? "
                "ORDER BY created_at DESC, rc_id DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM research_candidates "
                "ORDER BY created_at DESC, rc_id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    return [dict(r) for r in rows]


def _apply_status_in_conn(
    conn,
    rc_id: int,
    new_status: str,
    needs_review_reason: Optional[str],
) -> bool:
    """渡された conn 上で status 遷移を実行 (検証 + compare-and-swap).

    Codex 2段指摘#3/#4 反映:
      - #3 原子性: 呼出側が結果列 UPDATE と同一 conn (= 同一 transaction) で本関数を
        呼ぶことで、遷移検証で raise した時に結果列 UPDATE も rollback される。
      - #4 race: `WHERE rc_id=? AND status=?` (読んだ old_status を条件) + rowcount==1
        で compare-and-swap。並行で status が変わっていたら後勝ち更新せず False。
    不正遷移 / 不正値 / reason 欠落は ValueError (Q0 silent skip 禁止)。
    """
    if new_status not in _VALID_STATUSES:
        raise ValueError(f"invalid new_status: {new_status!r}")
    if new_status == STATUS_NEEDS_REVIEW and not (
        needs_review_reason and needs_review_reason.strip()
    ):
        raise ValueError(
            "needs_review_reason is required when transitioning to 'needs_review' "
            "(Q0 silent skip 防止)"
        )
    row = conn.execute(
        "SELECT status FROM research_candidates WHERE rc_id=?", (rc_id,)
    ).fetchone()
    if not row:
        return False
    old_status = row[0]
    if old_status == new_status:
        return False  # 同値 no-op
    if not can_transition(old_status, new_status):
        raise ValueError(
            f"transition not allowed: {old_status} -> {new_status}"
        )
    if needs_review_reason is not None:
        cur = conn.execute(
            "UPDATE research_candidates SET status=?, needs_review_reason=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE rc_id=? AND status=?",
            (new_status, needs_review_reason.strip(), rc_id, old_status),
        )
    else:
        cur = conn.execute(
            "UPDATE research_candidates SET status=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE rc_id=? AND status=?",
            (new_status, rc_id, old_status),
        )
    # rowcount != 1 = 並行で status が変わった (compare-and-swap 失敗)。後勝ちしない。
    return cur.rowcount == 1


def update_status(
    rc_id: int,
    new_status: str,
    *,
    needs_review_reason: Optional[str] = None,
) -> bool:
    """status 遷移. 不正遷移は ValueError (Q0 silent skip 禁止).

    needs_review に落とす場合は `needs_review_reason` を **必須**。
    付帯情報なしの needs_review = どこで詰まったか追跡不能 = silent skip と等価。
    """
    with get_conn() as conn:
        return _apply_status_in_conn(conn, rc_id, new_status, needs_review_reason)


def update_research_candidate_result(
    rc_id: int,
    *,
    found_url: Optional[str] = None,
    found_price_jpy: Optional[int] = None,
    found_condition_ja: Optional[str] = None,
    match_score: Optional[int] = None,
    match_reason: Optional[str] = None,
    estimated_profit_usd: Optional[float] = None,
    new_status: Optional[str] = None,
    needs_review_reason: Optional[str] = None,
) -> bool:
    """探索結果を一括書き込み + 必要なら status 遷移.

    `new_status` を渡せば `update_status` 経由で遷移検証付きで書く。
    status 遷移を伴わない部分 update も許容 (例: 追加 review メモ反映)。
    """
    if rc_id is None:
        raise ValueError("rc_id is required")

    sets: list[str] = []
    params: list = []
    if found_url is not None:
        sets.append("found_url=?")
        params.append(found_url)
    if found_price_jpy is not None:
        sets.append("found_price_jpy=?")
        params.append(int(found_price_jpy))
    if found_condition_ja is not None:
        sets.append("found_condition_ja=?")
        params.append(found_condition_ja)
    if match_score is not None:
        sets.append("match_score=?")
        params.append(int(match_score))
    if match_reason is not None:
        sets.append("match_reason=?")
        params.append(match_reason)
    if estimated_profit_usd is not None:
        sets.append("estimated_profit_usd=?")
        params.append(float(estimated_profit_usd))

    if not sets and new_status is None:
        # 何も書かないなら no-op (caller のロジックバグ可能性を Q0 で表面化)。
        logger.warning(
            "update_research_candidate_result: no fields to update "
            f"(rc_id={rc_id})"
        )
        return False

    # Codex 2段指摘#3: 結果列 UPDATE と status 遷移を **同一 conn (同一 transaction)** で
    # 実行。遷移検証で raise したら結果列 UPDATE も rollback され、found_url/profit だけ
    # 先に commit される半端な状態を作らない (金銭直結の原子性)。
    with get_conn() as conn:
        if sets:
            sets.append("updated_at=CURRENT_TIMESTAMP")
            params.append(rc_id)
            conn.execute(
                f"UPDATE research_candidates SET {', '.join(sets)} "
                "WHERE rc_id=?",
                params,
            )
        if new_status is not None:
            # 不正遷移 / reason 欠落 = ValueError → with を抜けて rollback。
            _apply_status_in_conn(
                conn, rc_id, new_status, needs_review_reason
            )

    return True
