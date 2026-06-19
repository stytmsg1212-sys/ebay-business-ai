"""監査ログ shipping_rate_batch_log (§8 / Codex MEDIUM)。

money-direct トレーサビリティ: 毎月の old→new を全件残す。
run summary と per-rate 明細を 2 テーブルに分離 (PDF hash / FX 観測期間 / 燃料 effective date を summary に)。

⚠️ init_db は触らない (K2)。CREATE TABLE IF NOT EXISTS は本質的に冪等のため、
バッチ起動時にこのモジュールが自前で table を保証する (app/scheduler 起動には無影響)。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from monitor.database import get_conn

logger = logging.getLogger(__name__)


def ensure_tables() -> None:
    """ログテーブルを冪等作成。"""
    conn = get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shipping_rate_batch_run (
                run_id TEXT PRIMARY KEY,
                run_at TEXT NOT NULL,
                mode TEXT NOT NULL,                 -- dry_run | auto
                outcome TEXT NOT NULL,              -- ok | held | partial_applied | aborted
                fx_used INTEGER,
                fx_period TEXT,
                fedex_fuel REAL,
                dhl_fuel REAL,
                fuel_source TEXT,
                base_rates_source TEXT,             -- pdf | cache
                summary TEXT                        -- JSON: warnings/guard fires/notes
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shipping_rate_batch_detail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                table_id TEXT NOT NULL,
                band TEXT,
                zone INTEGER,
                rate_id TEXT,
                old_usd INTEGER,
                new_usd INTEGER,
                action TEXT NOT NULL,               -- dryrun | applied | held | skipped | rolled_back | rollback_failed
                note TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def insert_run(run_id: str, mode: str, outcome: str, inputs: dict, summary: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO shipping_rate_batch_run
               (run_id, run_at, mode, outcome, fx_used, fx_period, fedex_fuel, dhl_fuel,
                fuel_source, base_rates_source, summary)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, datetime.now().isoformat(timespec="seconds"), mode, outcome,
                inputs.get("fx"), json.dumps(inputs.get("fx_period")),
                inputs.get("fedex_fuel"), inputs.get("dhl_fuel"),
                inputs.get("fuel_source"), inputs.get("base_rates_source"),
                json.dumps(summary, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def insert_details(run_id: str, rows: list[dict]) -> None:
    """rows: [{table_id, band, zone, rate_id, old_usd, new_usd, action, note}, ...]。"""
    if not rows:
        return
    conn = get_conn()
    try:
        conn.executemany(
            """INSERT INTO shipping_rate_batch_detail
               (run_id, table_id, band, zone, rate_id, old_usd, new_usd, action, note)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (run_id, r.get("table_id"), r.get("band"), r.get("zone"), r.get("rate_id"),
                 r.get("old_usd"), r.get("new_usd"), r.get("action"), r.get("note"))
                for r in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()
