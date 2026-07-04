#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#44 Wave2 (2026-07-04): US 本体 DB↔eBay 整合性 日次突合.

先行監査 (data/tmp/coo_scan.py, read-only手動実行) が 511件中498件で
Country of Origin / Manufacturer 等の禁止 Item Specifics 残存を検出した。
本タスクは同種の突合を **毎日自動 (02:15 JST)** で行い、以下を検出する:

  1. title 不一致 (DB ebay_listings.title vs eBay Item/Title)
  2. condition 不一致 (DB rank から期待される ConditionID vs eBay Item/ConditionID)
  3. ConditionDescription に前商品痕跡の疑い (CD 内 "Rank X" 表記が DB rank と矛盾)
  4. ItemSpecifics に禁止 Name (原産国/Manufacturer 系) 残存
     (`monitor.ebay_client._is_forbidden_specific_name` を再利用、#44 の
     4層防御と同一の禁止 Name 定義に揃える — 別定義を持つと drift する)
  5. 画像 0 枚

対象選定 (1 run 上限 50 件, money-direct ではないが API コスト抑制):
  - supplier_candidates.status='applied' の直近 7 日分 (乗り換え直後の stale
    をピンポイント検出)
  - + ランダム 20 件/日 (残プールの継続監視)

eBay へは GetItem のみ (書込み一切なし)。DB は SELECT のみ。
listing 識別は ebay_item_id (SKU 不使用、sku-rules.md 準拠)。
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Any, Optional

# pythonw.exe gotcha guard
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (ValueError, OSError):
        pass

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOTAL_PER_RUN = 50
DEFAULT_RANDOM_N_PER_RUN = 20
DEFAULT_RECENT_APPLIED_DAYS = 7

# ConditionDescription 内の「前商品の Rank 表記が残っている疑い」検出用。
# 例: "Rank As-Is — heavy wear" が DB rank='A' の listing に残っていれば不整合。
# 長い/より具体的な選択肢 (As-Is, PO) を先に置く (Python re の alternation は
# 左から順に試すため、先に "A" 単体が "As-Is" の頭にマッチしてしまうのを防ぐ)。
_RANK_TAG_RE = re.compile(r"\bRank\s+(As-Is|PO|N|S|A|B|C|D)\b", re.IGNORECASE)


def _expected_condition_id(rank: Optional[str]) -> Optional[str]:
    """DB の rank コードから期待される eBay ConditionID を引く.

    monitor.rank_classifier._RANK_TABLE (rank→(label, jp_hint, condition_id) の
    単一ソース) を再利用する。別に独自マップを持つと将来 rank_classifier 側の
    改訂に追従できず drift するため (cascade-update.md)。
    """
    if not rank:
        return None
    try:
        from monitor.rank_classifier import _RANK_TABLE
    except ImportError:
        return None
    entry = _RANK_TABLE.get(str(rank).strip())
    return entry[2] if entry else None


def _is_forbidden_specific_name(name: str) -> bool:
    """原産国/Manufacturer 系の禁止 Name か判定.

    monitor.ebay_client._is_forbidden_specific_name (#44 の書込み時 4層防御と
    同一の禁止 Name 定義) を再利用する。監査タスクが独自の禁止リストを持つと
    書込み側の定義とズレるリスクがあるため単一ソースに揃える。import 失敗時
    (ebay_client 側の変更中など) は検出不能として False を返す
    (Q0: raise で監査全体を止めない、他 issue 種別の検出は継続)。
    """
    try:
        from monitor.ebay_client import _is_forbidden_specific_name as _impl
    except ImportError:
        return False
    return _impl(name)


def audit_one(db_row: dict, snapshot: Any) -> list[dict]:
    """1 listing の DB↔eBay 突合 (純関数、ネットワーク非依存).

    Args:
        db_row: {"ebay_item_id", "title", "rank"} を含む dict (select_audit_targets
            の返り値要素、またはテスト用の任意 dict)。
        snapshot: monitor.ebay_listing_snapshot.ListingSnapshot (または互換 duck
            type)。snapshot.ok=False の場合は fetch_error のみ返す。

    Returns:
        issue dict のリスト ({"kind": str, "detail": str})。空 = 問題なし。
    """
    issues: list[dict] = []

    if snapshot is None or not getattr(snapshot, "ok", False):
        issues.append({
            "kind": "fetch_error",
            "detail": (getattr(snapshot, "error", None) if snapshot else None)
                      or "GetItem 失敗 (詳細不明)",
        })
        return issues

    # 1. title 不一致
    db_title = (db_row.get("title") or "").strip()
    snap_title = (getattr(snapshot, "title", None) or "").strip()
    if db_title and snap_title and db_title != snap_title:
        issues.append({
            "kind": "title_mismatch",
            "detail": f"db={db_title!r} ebay={snap_title!r}",
        })

    # 2. condition 不一致 (rank バケット単位、8段階中 A/B/C/D/PO は同一 3000 に
    #    集約されるため厳密な rank 一致ではなく condition_id バケット一致で判定)
    db_rank = (db_row.get("rank") or "").strip()
    expected_cid = _expected_condition_id(db_rank)
    snap_cid = getattr(snapshot, "condition_id", None)
    if expected_cid and snap_cid and snap_cid != expected_cid:
        issues.append({
            "kind": "condition_mismatch",
            "detail": (
                f"db_rank={db_rank}(期待 condition_id={expected_cid}) "
                f"ebay_condition_id={snap_cid}"
            ),
        })

    # 3. ConditionDescription 前商品痕跡の疑い
    cd = getattr(snapshot, "condition_description", None) or ""
    m = _RANK_TAG_RE.search(cd)
    if m and db_rank and m.group(1).upper() != db_rank.upper():
        issues.append({
            "kind": "condition_description_stale_rank",
            "detail": (
                f"ConditionDescription に 'Rank {m.group(1)}' 表記だが "
                f"db_rank={db_rank} (前商品の記述が残存している疑い): {cd[:80]!r}"
            ),
        })

    # 4. 禁止 ItemSpecifics (原産国/Manufacturer 系) 残存
    specifics = getattr(snapshot, "item_specifics", None) or {}
    prohibited_hits = sorted(
        name for name in specifics if _is_forbidden_specific_name(name)
    )
    if prohibited_hits:
        parts = []
        for name in prohibited_hits:
            vals = specifics[name]
            v = vals[0] if len(vals) == 1 else ", ".join(vals)
            parts.append(f"{name}={v}")
        issues.append({
            "kind": "prohibited_item_specifics",
            "detail": "; ".join(parts),
        })

    # 5. 画像 0 枚
    pic_count = getattr(snapshot, "picture_count", None)
    if pic_count is not None and pic_count == 0:
        issues.append({"kind": "no_images", "detail": "PictureURL 0件"})

    return issues


def select_audit_targets(
    *,
    max_total: int = DEFAULT_MAX_TOTAL_PER_RUN,
    random_n: int = DEFAULT_RANDOM_N_PER_RUN,
    recent_days: int = DEFAULT_RECENT_APPLIED_DAYS,
) -> list[dict]:
    """監査対象を選定する (DB SELECT のみ).

    1. supplier_candidates.status='applied' の直近 recent_days 日分
       (乗り換え直後の stale をピンポイント検出、対応する active listing のみ)。
    2. 残り枠を active listing からランダム抽出 (random_n 上限)。
    3. 合計 max_total 件を超えない。

    listing 識別・JOIN キーは ebay_item_id (SKU 不使用、sku-rules.md 準拠)。
    user_action_at は SQL CURRENT_TIMESTAMP 由来 = UTC 保存
    (sqlite-timezone.md)。`datetime('now', '-N days')` の相対比較で TZ 安全に扱う。

    Returns:
        [{"ebay_item_id": str, "title": str, "rank": str, "source": "recent_applied"|"random"}]
    """
    import sqlite3

    from monitor.database import get_conn

    max_total = max(0, int(max_total))
    if max_total == 0:
        return []

    targets: list[dict] = []
    seen_ids: set[str] = set()

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row

        recent_rows = conn.execute(
            """
            SELECT DISTINCT l.ebay_item_id AS ebay_item_id,
                   l.title AS title, l.rank AS rank,
                   MAX(sc.user_action_at) AS latest_action
              FROM supplier_candidates sc
              JOIN ebay_listings l ON l.ebay_item_id = sc.ebay_item_id
             WHERE sc.status = 'applied'
               AND sc.user_action_at >= datetime('now', ?)
               AND COALESCE(l.is_ended, 0) = 0
             GROUP BY l.ebay_item_id
             ORDER BY latest_action DESC
             LIMIT ?
            """,
            (f"-{int(recent_days)} days", max_total),
        ).fetchall()
        for r in recent_rows:
            eid = str(r["ebay_item_id"])
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            targets.append({
                "ebay_item_id": eid, "title": r["title"], "rank": r["rank"],
                "source": "recent_applied",
            })

        remaining_slots = max_total - len(targets)
        want_random = min(int(random_n), max(0, remaining_slots))
        if want_random > 0:
            if seen_ids:
                placeholders = ",".join("?" for _ in seen_ids)
                q = (
                    "SELECT ebay_item_id, title, rank FROM ebay_listings "
                    f"WHERE COALESCE(is_ended, 0) = 0 "
                    f"AND ebay_item_id NOT IN ({placeholders}) "
                    "ORDER BY RANDOM() LIMIT ?"
                )
                params: tuple = (*seen_ids, want_random)
            else:
                q = (
                    "SELECT ebay_item_id, title, rank FROM ebay_listings "
                    "WHERE COALESCE(is_ended, 0) = 0 "
                    "ORDER BY RANDOM() LIMIT ?"
                )
                params = (want_random,)
            for r in conn.execute(q, params).fetchall():
                eid = str(r["ebay_item_id"])
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                targets.append({
                    "ebay_item_id": eid, "title": r["title"], "rank": r["rank"],
                    "source": "random",
                })

    return targets[:max_total]


def _notify(findings: list[dict], result: dict) -> None:
    """検出結果を choke point (record_and_maybe_send) 経由で必ず記録する.

    Q0: 不整合の有無に関わらず必ず notification_log へ記録。
    severity='error' は category gate に関わらず Discord 必達
    (notifiers.notification_center._ALWAYS_SEND_SEVERITIES) — 設計通り。
    """
    from notifiers.notification_center import record_and_maybe_send

    has_issues = bool(findings)
    severity = "error" if has_issues else "info"
    title = (
        f"[出品内容監査] 不整合 {len(findings)} 件検出"
        if has_issues else "[出品内容監査] 不整合なし"
    )
    lines = [
        f"targets={result.get('targets')} checked={result.get('checked')} "
        f"fetch_errors={result.get('fetch_errors')}",
    ]
    for f in findings[:10]:
        kinds = ", ".join(i["kind"] for i in f["issues"])
        lines.append(f"  - {f['ebay_item_id']} ({f['title']}) [{f['source']}]: {kinds}")
    if len(findings) > 10:
        lines.append(f"  ...他 {len(findings) - 10} 件")
    body = "\n".join(lines)

    try:
        record_and_maybe_send("system", severity, title, body, link_target="system")
    except Exception as e:  # noqa: BLE001 — 通知失敗で run 全体を落とさない
        logger.warning(f"[listing_content_audit] 通知記録失敗: {e}")


def run_listing_content_audit(config: Optional[dict] = None) -> dict:
    """cron 経路. daily_scheduler.py から呼ばれる (毎日 02:15 JST)."""
    cfg = config or {}
    task_cfg = (cfg.get('tasks_enabled') or {}).get('listing_content_audit') or {}
    result: dict = {
        "success": False, "targets": 0, "checked": 0, "issues_found": 0,
        "fetch_errors": 0, "message": "",
    }

    # ── kill switch (Q0: skip も痕跡) ──
    if not task_cfg.get('enabled', True):
        result["success"] = True
        result["message"] = "listing_content_audit: enabled=false → skip"
        logger.info(f"[listing_content_audit] {result['message']}")
        return result

    max_total = int(task_cfg.get('max_total_per_run', DEFAULT_MAX_TOTAL_PER_RUN))
    random_n = int(task_cfg.get('random_n_per_run', DEFAULT_RANDOM_N_PER_RUN))
    recent_days = int(
        task_cfg.get('recent_applied_days', DEFAULT_RECENT_APPLIED_DAYS)
    )

    try:
        targets = select_audit_targets(
            max_total=max_total, random_n=random_n, recent_days=recent_days,
        )
    except Exception as e:  # noqa: BLE001
        result["message"] = f"対象選定失敗: {type(e).__name__}: {e}"
        logger.error(f"[listing_content_audit] {result['message']}")
        return result

    result["targets"] = len(targets)
    if not targets:
        result["success"] = True
        result["message"] = (
            "対象 0 件 (直近 applied 候補なし かつ active listing なし)"
        )
        logger.info(f"[listing_content_audit] {result['message']}")
        return result

    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    creds = get_ebay_credentials(cfg)
    if not ebay_credentials_ok(creds):
        result["message"] = "eBay 認証情報未設定"
        logger.error(f"[listing_content_audit] {result['message']}")
        return result

    from monitor.ebay_listing_snapshot import fetch_listing_snapshot

    findings: list[dict] = []
    fetch_errors = 0
    checked = 0
    for t in targets:
        eid = t["ebay_item_id"]
        try:
            snap = fetch_listing_snapshot(
                eid, creds.get("app_id", ""), creds.get("dev_id", ""),
                creds.get("cert_id", ""), creds.get("user_token", ""),
            )
        except Exception as e:  # noqa: BLE001 — Q0: 個別失敗で run 全体を止めない
            fetch_errors += 1
            checked += 1
            logger.warning(
                f"[listing_content_audit] GetItem 例外 {eid}: "
                f"{type(e).__name__}: {e}"
            )
            continue
        checked += 1
        if not snap.ok:
            fetch_errors += 1
        issues = audit_one(t, snap)
        if issues:
            findings.append({
                "ebay_item_id": eid, "title": (t.get("title") or "")[:40],
                "source": t.get("source"), "issues": issues,
            })

    result["checked"] = checked
    result["fetch_errors"] = fetch_errors
    result["issues_found"] = len(findings)
    result["success"] = True
    result["message"] = (
        f"targets={len(targets)} checked={checked} "
        f"issues_found={len(findings)} fetch_errors={fetch_errors}"
    )
    logger.info(f"[listing_content_audit] {result['message']}")

    _notify(findings, result)
    return result


if __name__ == "__main__":
    import json

    from daily_scheduler import load_config  # type: ignore
    _cfg = load_config()
    _r = run_listing_content_audit(_cfg)
    print(json.dumps(_r, indent=2, ensure_ascii=False))
