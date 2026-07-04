#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#43 一部先行実装: INBOX に溜まった業務外メール (楽天系プロモ/レビュー依頼等) の一括掃除.

背景:
  DASHBOARD「REFERENCE・NON-URGENT INBOX」に楽天系の私用メールが蓄積していた。
  さらにそのうち多数が sender/subject='N/A' 固定で保存されていた
  (tasks/task_email_pickup.py の Subject/From ヘッダ取得が大文字小文字を区別する
  完全一致だったため、楽天の一括配信メールが送る小文字ヘッダ ('subject'/'from') を
  拾えなかったのが真因。同 commit でヘッダ取得ロジック自体は修正済み)。

本スクリプトは DB migration ではなくデータクリーンアップの one-shot (db-migration-rules.md
の「本番 DB 直接書込 6 step」に従う):
  1. SELECT dump (snapshot) — 対象候補を JSON にダンプしロールバック可能にする
  2. subject='N/A' の未確認メールを Gmail API で再取得し、正しい subject/sender/date/category
     に UPDATE (バックフィル)
  3. 全未確認メールに is_archivable_noise_email() を適用し、対象を 1 件だけ試行
  4. 残りを一括 UPDATE
  5. SELECT で件数検証
  6. 24h retrospective review は呼出元 (session) で実施 (本スクリプト実行後に記録)

実行方法:
  python scripts/archive_noise_inbox_emails_2026_07_04.py            # 実行 (dry-run 無し、確認しながら段階実行)
  python scripts/archive_noise_inbox_emails_2026_07_04.py --dry-run  # 変更を保存せず件数だけ確認
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SNAPSHOT_DIR = Path(__file__).parent.parent / "data" / "backups"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _backfill_na_rows(dry_run: bool) -> int:
    """subject='N/A' の未確認メールを Gmail API で再取得し, 正しい subject/sender/date/
    category に UPDATE する。取得できない (メール削除済等) 場合はログのみ残し skip (Q0)."""
    import json as _json
    from monitor.database import get_conn
    from tasks.task_email_pickup import get_gmail_service, _header_value, _categorize_email

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT gmail_id FROM emails WHERE subject='N/A' AND COALESCE(confirmed,0)=0"
        ).fetchall()
    gmail_ids = [r[0] for r in rows]
    logger.info(f"N/A backfill 対象: {len(gmail_ids)} 件")
    if not gmail_ids:
        return 0

    config_path = Path(__file__).parent.parent / "config" / "schedule_config.json"
    config = _json.loads(config_path.read_text(encoding="utf-8"))
    service = get_gmail_service(config)
    if service is None:
        logger.error("Gmail service 初期化失敗。N/A backfill を中止 (既存 N/A 行は変更なし)")
        return 0

    fixed = 0
    failed = 0
    for gid in gmail_ids:
        try:
            msg_data = service.users().messages().get(userId="me", id=gid, format="full").execute()
            headers = msg_data["payload"]["headers"]
            subject = _header_value(headers, "Subject")
            sender = _header_value(headers, "From")
            date = _header_value(headers, "Date")
            category = _categorize_email(subject, sender)
        except Exception as e:  # noqa: BLE001 — Gmail 側の 404/削除等は skip し継続 (Q0: ログに残す)
            logger.warning(f"N/A backfill 失敗 (gmail_id={gid}): {e}")
            failed += 1
            continue

        if not dry_run:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE emails SET subject=?, sender=?, date=?, category=? WHERE gmail_id=?",
                    (subject, sender, date, category, gid),
                )
        fixed += 1

    logger.info(f"N/A backfill 完了: 成功 {fixed} 件 / 失敗(skip) {failed} 件")
    return fixed


def _archive_noise(dry_run: bool) -> tuple[int, int]:
    """全未確認メールに is_archivable_noise_email() を適用し, 対象を confirmed=1 にする.

    Returns: (対象件数, 実UPDATE件数)
    """
    from monitor.database import get_conn
    from tasks.task_email_pickup import is_archivable_noise_email

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT gmail_id, subject, sender, category,
                      COALESCE(category_ai, '') as category_ai
               FROM emails WHERE COALESCE(confirmed,0)=0"""
        ).fetchall()

    targets = [
        r["gmail_id"] for r in rows
        if is_archivable_noise_email(r["category"] or "other", r["category_ai"], r["sender"] or "")
    ]
    logger.info(f"未確認 {len(rows)} 件中、業務外ドメイン自動アーカイブ対象 {len(targets)} 件")

    if dry_run or not targets:
        return len(targets), 0

    # Step 1: snapshot (rollback 用)
    snapshot_path = SNAPSHOT_DIR / f"noise_inbox_archive_snapshot_{datetime.now():%Y%m%d_%H%M%S}.json"
    snapshot_path.write_text(json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"snapshot 保存: {snapshot_path} ({len(targets)} 件)")

    # Step 2: 1 件だけ試行
    # db-migration-rules L85: -O 実行時に assert が最適化除去される事故防止のため
    # 安全ゲートは明示 raise で書く (2026-07-04 retrospective review H1 指摘)。
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE emails SET confirmed=1 WHERE gmail_id=? AND COALESCE(confirmed,0)=0",
            (targets[0],),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"1件試行で rowcount={cur.rowcount} (期待値1). "
                f"snapshot={snapshot_path} で rollback 可能。"
            )
    logger.info(f"1件試行 OK: {targets[0]}")

    # Step 3: 残りを一括 UPDATE
    remaining = targets[1:]
    updated = 1
    if remaining:
        placeholders = ",".join("?" * len(remaining))
        with get_conn() as conn:
            cur = conn.execute(
                f"UPDATE emails SET confirmed=1 WHERE gmail_id IN ({placeholders}) "
                f"AND COALESCE(confirmed,0)=0",
                remaining,
            )
            updated += cur.rowcount

    return len(targets), updated


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    with_confirmed_before = _count_unconfirmed()
    logger.info(f"実行前 未確認メール総数: {with_confirmed_before}")

    _backfill_na_rows(dry_run)
    target_count, updated_count = _archive_noise(dry_run)

    with_confirmed_after = _count_unconfirmed()
    logger.info(
        f"完了 (dry_run={dry_run}): 対象候補 {target_count} 件 / 実UPDATE {updated_count} 件 / "
        f"未確認メール {with_confirmed_before} -> {with_confirmed_after}"
    )
    if not dry_run:
        # db-migration-rules L85: assert を安全ゲートに使わない (-O で除去される)
        diff = with_confirmed_before - with_confirmed_after
        if diff != updated_count:
            raise RuntimeError(
                f"件数検証失敗: before-after ({diff}) != updated_count ({updated_count})"
            )
        logger.info("件数検証 OK (rowcount 一致確認)")


def _count_unconfirmed() -> int:
    from monitor.database import get_conn
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM emails WHERE COALESCE(confirmed,0)=0"
        ).fetchone()[0]


if __name__ == "__main__":
    main()
