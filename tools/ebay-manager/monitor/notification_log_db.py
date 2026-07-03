#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知ログ (依頼ボード #39 Phase A S1) データ層 — notification_log CRUD.

design 出典: 依頼ボード #39「monodeckへの通知サマリ機能追加」。設計書は無く、本
prompt (S1 実装依頼) が仕様。migration v89 (database.py init_db) で作成される
notification_log テーブルへの薄い CRUD レイヤ。

scope (S1):
  - insert / 取得 (未読フィルタ・category フィルタ) / 既読化 (個別/category/全件)
  - 未読件数 (全体・category 別)
  - dedupe (直近 N 時間の同一 dedupe_key 検知)

ルール準拠:
  - category は discord_notifier.WEBHOOK_CATEGORY_ENV (依頼ボード#22 タクソノミー)
    と同一 whitelist + 'default' (通知系カテゴリ taxonomy を新規発明せず既存流用、K1)。
  - created_at は UTC 保存 (SQL default (datetime('now')))。相対範囲比較のみ使用
    (sqlite-timezone.md 準拠、絶対日付換算不要)。
  - Q0: category/severity の不正値は ValueError (silent な型崩れ防止)。
  - K1: 最小実装。コネクション管理は get_conn に委譲 (他 *_db.py モジュールと同一流儀)。
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from .database import get_conn

logger = logging.getLogger(__name__)

# ---- whitelist ----------------------------------------------------------------
# discord_notifier.WEBHOOK_CATEGORY_ENV (依頼ボード#22) と同一タクソノミー。
# カテゴリを増やす場合は discord_notifier 側と cascade 更新 (cascade-update.md)。
NOTIFICATION_CATEGORIES = frozenset({
    "inventory", "order", "rival", "keyword", "research", "pricing",
    "system", "action_required", "default",
})

NOTIFICATION_SEVERITIES = frozenset({"info", "warning", "error", "critical"})


def _validate_category(category: str) -> None:
    if category not in NOTIFICATION_CATEGORIES:
        raise ValueError(
            f"不正な category: {category!r} (許可: {sorted(NOTIFICATION_CATEGORIES)})"
        )


def _validate_severity(severity: str) -> None:
    if severity not in NOTIFICATION_SEVERITIES:
        raise ValueError(
            f"不正な severity: {severity!r} (許可: {sorted(NOTIFICATION_SEVERITIES)})"
        )


# ---- insert ---------------------------------------------------------------------


def insert_notification(
    category: str,
    severity: str,
    title: str,
    body: Optional[str] = None,
    *,
    link_target: Optional[str] = None,
    link_ref: Optional[str] = None,
    discord_sent: bool = False,
    dedupe_key: Optional[str] = None,
) -> int:
    """通知ログ 1 件を INSERT し、新規行の id を返す。

    category/severity は whitelist validation (不正値は ValueError、Q0 準拠)。
    title は必須 (空文字/空白のみは ValueError)。
    """
    _validate_category(category)
    _validate_severity(severity)
    if not title or not title.strip():
        raise ValueError("title は必須 (空文字不可)")

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO notification_log
                (category, severity, title, body, link_target, link_ref,
                 discord_sent, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                category, severity, title, body, link_target, link_ref,
                int(bool(discord_sent)), dedupe_key,
            ),
        )
        new_id = cur.lastrowid
    return int(new_id)


# ---- 取得 -------------------------------------------------------------------


def get_notifications(
    *,
    unread_only: bool = False,
    category: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """通知ログを新しい順 (created_at DESC, id DESC) に取得。"""
    if category is not None:
        _validate_category(category)

    where: list[str] = []
    params: list = []
    if unread_only:
        where.append("read_at IS NULL")
    if category is not None:
        where.append("category = ?")
        params.append(category)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    sql = (
        f"SELECT * FROM notification_log {where_sql} "
        f"ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    params.append(int(limit))

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ---- 既読化 -------------------------------------------------------------------


def mark_read(ids: Sequence[int]) -> int:
    """指定 id 群を既読化 (read_at=now)。更新件数を返す。空 list は 0 で no-op。"""
    id_list = list(ids)
    if not id_list:
        return 0
    placeholders = ",".join("?" for _ in id_list)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE notification_log SET read_at = datetime('now') "
            f"WHERE id IN ({placeholders}) AND read_at IS NULL",
            id_list,
        )
        return cur.rowcount


def mark_category_read(category: str) -> int:
    """category の未読を全て既読化。更新件数を返す。"""
    _validate_category(category)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notification_log SET read_at = datetime('now') "
            "WHERE category = ? AND read_at IS NULL",
            (category,),
        )
        return cur.rowcount


def mark_all_read() -> int:
    """全未読を既読化。更新件数を返す。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notification_log SET read_at = datetime('now') WHERE read_at IS NULL"
        )
        return cur.rowcount


# ---- 未読集計 -------------------------------------------------------------------


def get_unread_count() -> int:
    """未読件数 (全体)。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM notification_log WHERE read_at IS NULL"
        ).fetchone()
    return int(row[0])


def get_unread_count_by_category() -> dict:
    """category → 未読件数 の dict。未読 0 件の category はキーを持たない。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS c FROM notification_log "
            "WHERE read_at IS NULL GROUP BY category"
        ).fetchall()
    return {r["category"]: int(r["c"]) for r in rows}


# ---- dedupe -------------------------------------------------------------------


def has_recent_dedupe(dedupe_key: str, hours: int = 24) -> bool:
    """直近 hours 時間以内に同一 dedupe_key の行が存在するか。

    created_at は UTC 保存 (notification_log.created_at の SQL default
    `datetime('now')`、sqlite-timezone.md 準拠) のため、相対範囲
    `datetime('now', '-N hours')` で比較する (絶対日付換算不要)。
    """
    if not dedupe_key:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM notification_log "
            "WHERE dedupe_key = ? AND created_at >= datetime('now', ?) LIMIT 1",
            (dedupe_key, f"-{int(hours)} hours"),
        ).fetchone()
    return row is not None
