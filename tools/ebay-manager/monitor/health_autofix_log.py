"""health-check auto-fix の監査ログ helper (v57 テーブル).

健康チェック (task_scheduler_health_check) が検知した異常を task_health_autofix が
自動対処する際の、全試行の痕跡記録 + ループガード (当日試行回数追跡) + Tier3 DB 書込
提案の保管を担う。

設計方針:
- shadow DB_PATH を持たず monitor.database.get_conn を使う (conftest 隔離互換、
  busy_timeout/WAL 設定を共有。MEDIUM-1 アンチパターン回避)。
- attempt_date は JST 日付 (datetime.now() は Windows ローカル = JST naive)。
  ループガードは「JST 当日」単位で集計する (sqlite-timezone.md 例外カラム)。
- finding_hash は同一異常を安定識別するキー。SKU を含めない (sku-rules: listing
  識別は ebay_item_id。target_task_key / ebay_item_id / 正規化 evidence から導出)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Optional

from monitor.database import get_conn

logger = logging.getLogger(__name__)

# 自動対処の status 語彙 (Q0: 全経路に痕跡)。
#   attempted   … 対処に着手 (Tier1 再実行を試みた等)
#   resolved    … 再実行/対処で finding が解消 (Tier1 成功)
#   applied     … コード修正をローカル commit 適用 (Tier2)
#   gate_failed … pytest/reviewer/scope/diff の gate で弾かれ適用せず (Tier2)
#   aborted     … 処理中断 (claude 異常終了 / budget 超過 / scope 外)
#   escalated   … 自動対処不能で user 判断へ回付
#   proposed    … DB 書込提案を保存 (Tier3、実行はしていない)
#   skipped     … killswitch / ループガードで対処自体を見送り (試行回数に数えない)
_VALID_STATUS = frozenset({
    "attempted", "resolved", "applied", "gate_failed",
    "aborted", "escalated", "proposed", "skipped",
})


def make_finding_hash(
    kind: str,
    target_task_key: Optional[str] = None,
    evidence: Any = None,
) -> str:
    """finding の安定ハッシュ (同一異常の識別 = ループガード/dedupe キー).

    同じ異常が複数 cron で再検知されても同一 hash になるよう、揺れる値
    (時刻・件数の微差) は evidence に含めない運用を呼び出し側で守る。
    """
    norm_evidence = ""
    if evidence is not None:
        try:
            # dict/list は key ソートで安定化。文字列はそのまま。
            norm_evidence = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            norm_evidence = str(evidence)
    raw = f"{kind}|{target_task_key or ''}|{norm_evidence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def record_attempt(
    finding_hash: str,
    tier: str,
    kind: str,
    action: str,
    status: str,
    *,
    target_task_key: Optional[str] = None,
    commit_hash: Optional[str] = None,
    gate_report: Optional[dict] = None,
    cost_usd: float = 0.0,
    detail: str = "",
) -> int:
    """自動対処 1 件を autofix_attempt_log に記録し log id を返す.

    status は _VALID_STATUS のいずれか。範囲外は ValueError (Q0: 曖昧な
    成功偽装を型で弾く)。
    """
    if status not in _VALID_STATUS:
        raise ValueError(
            f"invalid status {status!r} (valid: {sorted(_VALID_STATUS)})"
        )
    attempt_date = datetime.now().strftime("%Y-%m-%d")  # JST naive
    gate_json = (
        json.dumps(gate_report, ensure_ascii=False) if gate_report else None
    )
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO autofix_attempt_log
              (attempt_date, finding_hash, tier, kind, target_task_key,
               action, status, commit_hash, gate_report, cost_usd, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (attempt_date, finding_hash, tier, kind, target_task_key,
             action, status, commit_hash, gate_json, cost_usd,
             (detail or "")[:2000]),
        )
        return cur.lastrowid


def count_attempts_today(finding_hash: str) -> int:
    """JST 当日、この finding に対する実対処の回数 (ループガード用).

    status='skipped' (killswitch/ガードで見送ったもの) は数えない。
    実際に再実行/修正/提案を試みた回数のみを返す。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM autofix_attempt_log
            WHERE attempt_date = ? AND finding_hash = ?
              AND status != 'skipped'
            """,
            (today, finding_hash),
        ).fetchone()
    return int(row[0]) if row else 0


def record_db_proposal(
    finding_hash: str,
    kind: str,
    proposed_sql: str,
    *,
    diagnosis_sql: Optional[str] = None,
    diagnosis_result: Any = None,
    affected_rows_est: Optional[int] = None,
) -> int:
    """Tier3 の DB 書込提案を保存し proposal id を返す (実行はしない).

    既に同一 finding の pending 提案があれば新規作成せず既存 id を返す
    (cron 毎の重複提案を防ぐ)。
    """
    existing = _open_proposal_id(finding_hash)
    if existing is not None:
        return existing
    result_json = None
    if diagnosis_result is not None:
        try:
            result_json = json.dumps(diagnosis_result, ensure_ascii=False)
        except (TypeError, ValueError):
            result_json = str(diagnosis_result)
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO autofix_db_proposal
              (finding_hash, kind, diagnosis_sql, diagnosis_result,
               proposed_sql, affected_rows_est, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (finding_hash, kind, diagnosis_sql, result_json,
             proposed_sql, affected_rows_est),
        )
        return cur.lastrowid


def _open_proposal_id(finding_hash: str) -> Optional[int]:
    """同一 finding の pending 提案 id (無ければ None)."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id FROM autofix_db_proposal
            WHERE finding_hash = ? AND status = 'pending'
            ORDER BY id DESC LIMIT 1
            """,
            (finding_hash,),
        ).fetchone()
    return int(row[0]) if row else None


def get_pending_proposals() -> list[dict]:
    """承認待ちの DB 書込提案を一覧 (MonoDeck UI 表示用)."""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, finding_hash, kind, diagnosis_sql, diagnosis_result,
                   proposed_sql, affected_rows_est, status, created_at
            FROM autofix_db_proposal
            WHERE status = 'pending'
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]
