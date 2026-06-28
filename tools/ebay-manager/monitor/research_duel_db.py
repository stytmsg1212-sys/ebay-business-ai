#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""リサーチ対戦アリーナ (W286) データ層 — duel_rounds / duel_ai_picks / duel_user_picks CRUD.

設計書: .company/engineering/docs/2026-06-27-research-duel-arena-system-design-v2.md
スコープ (Phase 1 計測ハーネス):
  - オーナー × AI が同一条件でブラインド・リサーチ → オーナーが AI 5品を 0-100 採点 → 蓄積。
  - 自己改善ループ (few-shot 配線 / ルーブリック昇格 / ROI) は Phase 2 (本モジュール対象外)。

ルール準拠:
  - 状態機械 (research_candidates_db 流儀): `_apply_round_status_in_conn` が遷移検証 +
    compare-and-swap (Q0: 不正遷移は ValueError、並行更新は後勝ちしない)。
  - listing 識別は ebay_item_id (sku-rules)。duel_ai_picks.rc_id は research_candidates 参照、
    profit は複製しない (利益真値は rc_id 側が唯一 / 設計書 Fugu A2)。
  - 採点理由: score < 60 (失点) は user_fb_md 必須 (学習 signal / Q0 / Fugu M2)。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .database import get_conn

logger = logging.getLogger(__name__)

# ---- round 状態機械 ---------------------------------------------------------
STATUS_AI_PENDING = "ai_pending"   # round 作成済、AI リサーチ未
STATUS_AI_DONE = "ai_done"         # AI 5品 + 凍結 snapshot 保存済 (採点待ち)
STATUS_USER_DONE = "user_done"     # オーナーが採点完了 (学習トリガー待ち)
STATUS_COMPLETED = "completed"     # 完了 (深層学習実行済) — 終端
STATUS_INVALIDATED = "invalidated"  # drift 検知等で採点無効化 — 終端

_VALID_ROUND_STATUSES: frozenset[str] = frozenset({
    STATUS_AI_PENDING, STATUS_AI_DONE, STATUS_USER_DONE,
    STATUS_COMPLETED, STATUS_INVALIDATED,
})

# 許容遷移 (前進のみ + 任意時点で invalidated へ)。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_AI_PENDING: frozenset({STATUS_AI_DONE, STATUS_INVALIDATED}),
    STATUS_AI_DONE: frozenset({STATUS_USER_DONE, STATUS_INVALIDATED}),
    STATUS_USER_DONE: frozenset({STATUS_COMPLETED, STATUS_INVALIDATED}),
    STATUS_COMPLETED: frozenset(),
    STATUS_INVALIDATED: frozenset(),
}

_VALID_PATTERNS: frozenset[str] = frozenset({"new", "echo"})

# 失点採点で理由を必須化する閾値 (これ未満は「なぜ低いか」が学習の核)。
_REASON_REQUIRED_BELOW = 60


def can_transition(old: str, new: str) -> bool:
    """round status の遷移可否 (純関数)."""
    return new in _ALLOWED_TRANSITIONS.get(old, frozenset())


# ============================================================================
# round CRUD
# ============================================================================

def create_round(
    *,
    jst_date: str,
    pattern: str,
    category_id: Optional[int] = None,
    category_label: Optional[str] = None,
    snapshot_json: Optional[str] = None,
    prompt_version: Optional[str] = None,
) -> int:
    """1 セル (jst_date × pattern × category) の round を作成し round_id を返す.

    冪等: 同一セルが既存なら既存 round_id を返す (夜間タスク再実行で重複作成しない)。
    UNIQUE(jst_date,pattern,category_id) は category_id NULL を弾けないため、SELECT も
    IFNULL で同値判定する。
    """
    if pattern not in _VALID_PATTERNS:
        raise ValueError(f"pattern must be one of {sorted(_VALID_PATTERNS)}, got {pattern!r}")
    if not jst_date or not jst_date.strip():
        raise ValueError("jst_date is required")
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT round_id FROM duel_rounds WHERE jst_date=? AND pattern=? "
            "AND IFNULL(category_id,-1)=IFNULL(?,-1)",
            (jst_date, pattern, category_id),
        ).fetchone()
        if existing:
            return int(existing[0])
        cur = conn.execute(
            "INSERT INTO duel_rounds (jst_date, pattern, category_id, category_label, "
            "snapshot_json, prompt_version, status) VALUES (?,?,?,?,?,?,?)",
            (jst_date, pattern, category_id, category_label, snapshot_json,
             prompt_version, STATUS_AI_PENDING),
        )
        return int(cur.lastrowid)


def get_round(round_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM duel_rounds WHERE round_id=?", (round_id,)
        ).fetchone()
    return dict(row) if row else None


def get_round_by_cell(
    jst_date: str, pattern: str, category_id: Optional[int] = None
) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM duel_rounds WHERE jst_date=? AND pattern=? "
            "AND IFNULL(category_id,-1)=IFNULL(?,-1)",
            (jst_date, pattern, category_id),
        ).fetchone()
    return dict(row) if row else None


def list_rounds(limit: int = 60) -> list[dict]:
    """新しい日付順に round を返す (バックナンバー用)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM duel_rounds ORDER BY jst_date DESC, round_id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================================
# status 遷移 (CAS + 検証、research_candidates_db 流儀)
# ============================================================================

def _apply_round_status_in_conn(conn, round_id: int, new_status: str) -> bool:
    """渡された conn 上で round status 遷移 (検証 + compare-and-swap).

    不正遷移 / 不正値は ValueError (Q0)。並行更新は rowcount!=1 で後勝ちしない。
    completed への遷移時は completed_at を打刻。
    """
    if new_status not in _VALID_ROUND_STATUSES:
        raise ValueError(f"invalid new_status: {new_status!r}")
    row = conn.execute(
        "SELECT status FROM duel_rounds WHERE round_id=?", (round_id,)
    ).fetchone()
    if not row:
        return False
    old_status = row[0]
    if old_status == new_status:
        return False  # 同値 no-op
    if not can_transition(old_status, new_status):
        raise ValueError(f"transition not allowed: {old_status} -> {new_status}")
    if new_status == STATUS_COMPLETED:
        cur = conn.execute(
            "UPDATE duel_rounds SET status=?, completed_at=CURRENT_TIMESTAMP "
            "WHERE round_id=? AND status=?",
            (new_status, round_id, old_status),
        )
    else:
        cur = conn.execute(
            "UPDATE duel_rounds SET status=? WHERE round_id=? AND status=?",
            (new_status, round_id, old_status),
        )
    return cur.rowcount == 1


def update_round_status(round_id: int, new_status: str) -> bool:
    """round status 遷移. 不正遷移は ValueError (Q0 silent skip 禁止)."""
    with get_conn() as conn:
        return _apply_round_status_in_conn(conn, round_id, new_status)


def invalidate_round(round_id: int, reason: str) -> bool:
    """drift 検知等で round を採点無効化 (snapshot 母集合が翌朝と乖離した時等)."""
    if not reason or not reason.strip():
        raise ValueError("invalidate reason is required (Q0)")
    with get_conn() as conn:
        ok = _apply_round_status_in_conn(conn, round_id, STATUS_INVALIDATED)
        if ok:
            logger.warning(
                "[research_duel_db] round_id=%s invalidated: %s", round_id, reason.strip()
            )
        return ok


# ============================================================================
# picks (AI / user)
# ============================================================================

def save_ai_picks(round_id: int, picks: list[dict]) -> None:
    """AI の 5 品を保存 (rc_id 参照 + rank + 表示スナップショット title)。

    picks: [{"rc_id": int|None, "rank": int, "title_ja": str}, ...]
    冪等: 同 round の既存 ai_picks を置換。status が ai_pending なら ai_done へ自動前進。
    profit は保存しない (rc_id 側が真値 / Fugu A2)。
    """
    with get_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM duel_rounds WHERE round_id=?", (round_id,)
        ).fetchone():
            raise ValueError(f"save_ai_picks: round_id={round_id} not found")
        conn.execute("DELETE FROM duel_ai_picks WHERE round_id=?", (round_id,))
        for p in picks:
            conn.execute(
                "INSERT INTO duel_ai_picks (round_id, rc_id, rank, title_ja) "
                "VALUES (?,?,?,?)",
                (round_id, p.get("rc_id"), p.get("rank"), p.get("title_ja")),
            )
        # ai_pending からのみ自動前進 (それ以降の再保存は status を動かさない)。
        st = conn.execute(
            "SELECT status FROM duel_rounds WHERE round_id=?", (round_id,)
        ).fetchone()
        if st and st[0] == STATUS_AI_PENDING:
            _apply_round_status_in_conn(conn, round_id, STATUS_AI_DONE)


def save_user_picks(round_id: int, picks: list[dict]) -> None:
    """オーナーの 1〜5 品を保存。

    picks: [{"rank", "title_ja", "ebay_url", "supplier_url", "profit_jpy_user", "why_md"}, ...]
    冪等: 同 round の既存 user_picks を置換。1〜5 品 (時間が無ければ少なくてよい)。
    """
    if not picks:
        raise ValueError("save_user_picks: 最低 1 品必要")
    if len(picks) > 5:
        raise ValueError("save_user_picks: 最大 5 品")
    with get_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM duel_rounds WHERE round_id=?", (round_id,)
        ).fetchone():
            raise ValueError(f"save_user_picks: round_id={round_id} not found")
        conn.execute("DELETE FROM duel_user_picks WHERE round_id=?", (round_id,))
        for p in picks:
            conn.execute(
                "INSERT INTO duel_user_picks (round_id, rank, title_ja, ebay_url, "
                "supplier_url, profit_jpy_user, why_md) VALUES (?,?,?,?,?,?,?)",
                (round_id, p.get("rank"), p.get("title_ja"), p.get("ebay_url"),
                 p.get("supplier_url"), p.get("profit_jpy_user"), p.get("why_md")),
            )


def score_ai_pick(
    pick_id: int,
    *,
    user_score: int,
    user_fb_md: Optional[str] = None,
    reject_tags: Optional[list[str]] = None,
) -> bool:
    """AI の 1 品を 0-100 で採点する。

    Q0 / Fugu M2: score < 60 (失点) は user_fb_md (採点理由) 必須 = 学習 signal。
    出品不可 = 0 点 (理由必須)。返り値 False = pick_id 不在。
    """
    score = int(user_score)
    if not (0 <= score <= 100):
        raise ValueError(f"user_score must be 0-100, got {score}")
    if score < _REASON_REQUIRED_BELOW and not (user_fb_md and user_fb_md.strip()):
        raise ValueError(
            f"user_fb_md is required when score < {_REASON_REQUIRED_BELOW} "
            "(失点理由 = 学習 signal / Q0)"
        )
    tags_json = json.dumps(reject_tags, ensure_ascii=False) if reject_tags else None
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE duel_ai_picks SET user_score=?, user_fb_md=?, reject_tags_json=?, "
            "scored_at=CURRENT_TIMESTAMP WHERE id=?",
            (score, (user_fb_md.strip() if user_fb_md else None), tags_json, pick_id),
        )
        return cur.rowcount == 1


def get_round_picks(round_id: int) -> dict:
    """round の AI picks と user picks を rank 順で返す."""
    with get_conn() as conn:
        ai = conn.execute(
            "SELECT * FROM duel_ai_picks WHERE round_id=? ORDER BY rank", (round_id,)
        ).fetchall()
        user = conn.execute(
            "SELECT * FROM duel_user_picks WHERE round_id=? ORDER BY rank", (round_id,)
        ).fetchall()
    return {"ai": [dict(r) for r in ai], "user": [dict(r) for r in user]}


# ============================================================================
# スコア集計 (平均 / 致命傷率 / 中央値)
# ============================================================================

def compute_and_save_round_scores(round_id: int) -> dict:
    """AI 5品の採点から平均・致命傷率(0点)・中央値を算出し duel_rounds に保存して返す.

    採点済 (user_score IS NOT NULL) のみ対象。出品不可=0 点は分母に含む (平均の重し)。
    中央値は 0 点を除く「採点された品」の選定眼を見るため scored(>0) ベース。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_score FROM duel_ai_picks WHERE round_id=? "
            "AND user_score IS NOT NULL",
            (round_id,),
        ).fetchall()
    scores = [int(r[0]) for r in rows]
    if not scores:
        return {"count": 0, "avg": None, "zero_rate": None, "median": None}
    avg = round(sum(scores) / len(scores), 2)
    zero_rate = round(sum(1 for s in scores if s == 0) / len(scores), 4)
    nonzero = sorted(s for s in scores if s > 0)
    median = nonzero[(len(nonzero) - 1) // 2] if nonzero else 0
    with get_conn() as conn:
        conn.execute(
            "UPDATE duel_rounds SET ai_score_avg=?, zero_rate=?, score_median=? "
            "WHERE round_id=?",
            (avg, zero_rate, median, round_id),
        )
    return {"count": len(scores), "avg": avg, "zero_rate": zero_rate, "median": median}
