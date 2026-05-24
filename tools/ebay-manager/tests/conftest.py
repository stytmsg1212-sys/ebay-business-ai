"""pytest 共通 fixture.

2026-05-25 追加: 全テストで `monitor.database.DB_PATH` を tmp_path 配下に隔離.
本番 `data/monitor.db` への汚染防止 (5/05〜5/24 で `simulated task crash` 64 件
偽 failed が `task_execution_log` に蓄積されていた事故対応).

詳細: `.claude/rules/db-migration-rules.md` (本番 DB 直接書込原則禁止) /
      `.claude/rules/silent-skip-prevention.md` Q0 (健康 alert noise = silent skip 検知能力低下).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_monitor_db(monkeypatch, tmp_path):
    """`monitor.database.DB_PATH` を test 専用 tmp dir に強制差し替え.

    `get_conn()` は呼び出し時に module 変数 `DB_PATH` を `str()` で評価するため、
    monkeypatch で module attr を上書きすれば既存テストの import を変えずに
    本番 DB を遮断できる. test 内で `init_db()` を呼ぶケースに備え dir は事前作成.
    """
    test_db = tmp_path / "monitor.db"
    test_db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("monitor.database.DB_PATH", test_db)
