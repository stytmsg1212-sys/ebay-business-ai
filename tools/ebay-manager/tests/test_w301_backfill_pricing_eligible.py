"""W301 AI 店長 Phase1 S1 (2026-07-02): backfill_pricing_eligible_w301.py の
dry-run / apply 検証 (tmp DB のみ、本番 data/monitor.db には触れない)。

要件 (指示書): 既存の active 採用ライバル (is_active=1) のみ pricing_eligible=1
へ backfill、非 active・新規は 0 のまま。
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "backfill_pricing_eligible_w301.py"


def _load_backfill_module():
    spec = importlib.util.spec_from_file_location(
        "backfill_pricing_eligible_w301", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def tmp_db_with_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    monkeypatch.syspath_prepend(str(ROOT))
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    with db_mod.get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "is_active) VALUES ('OUR1','COMP1',1)"
        )
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "is_active) VALUES ('OUR2','COMP2',1)"
        )
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "is_active) VALUES ('OUR3','COMP3',0)"
        )
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "is_active, pricing_eligible) VALUES ('OUR4','COMP4',1,1)"
        )
    return db_path


def test_dry_run_does_not_write(tmp_db_with_rows):
    mod = _load_backfill_module()
    conn = sqlite3.connect(tmp_db_with_rows)
    conn.row_factory = sqlite3.Row
    targets = mod._fetch_targets(conn)
    conn.close()
    assert {t["competitor_item_id"] for t in targets} == {"COMP1", "COMP2"}

    sys.argv = ["backfill_pricing_eligible_w301.py", "--db", str(tmp_db_with_rows)]
    mod.main()

    with sqlite3.connect(tmp_db_with_rows) as c:
        rows = {
            r[0]: r[1]
            for r in c.execute(
                "SELECT competitor_item_id, pricing_eligible FROM competitor_products"
            )
        }
    # dry-run = 未書込
    assert rows["COMP1"] == 0
    assert rows["COMP2"] == 0
    assert rows["COMP3"] == 0
    assert rows["COMP4"] == 1


def test_apply_backfills_only_active_and_not_already_eligible(tmp_db_with_rows):
    mod = _load_backfill_module()
    sys.argv = [
        "backfill_pricing_eligible_w301.py",
        "--db", str(tmp_db_with_rows),
        "--apply",
    ]
    mod.main()

    with sqlite3.connect(tmp_db_with_rows) as c:
        rows = {
            r[0]: (r[1], r[2])
            for r in c.execute(
                "SELECT competitor_item_id, is_active, pricing_eligible "
                "FROM competitor_products"
            )
        }
    assert rows["COMP1"] == (1, 1), "active 採用は backfill されるべき"
    assert rows["COMP2"] == (1, 1), "active 採用は backfill されるべき"
    assert rows["COMP3"] == (0, 0), "非 active は 0 のまま (Shadow 対象)"
    assert rows["COMP4"] == (1, 1), "既に eligible=1 は変化なし"


def test_apply_is_idempotent(tmp_db_with_rows):
    mod = _load_backfill_module()
    sys.argv = [
        "backfill_pricing_eligible_w301.py",
        "--db", str(tmp_db_with_rows),
        "--apply",
    ]
    mod.main()
    mod.main()  # 2 回目 (冪等: 対象が 0 件になり無害)

    with sqlite3.connect(tmp_db_with_rows) as c:
        eligible_count = c.execute(
            "SELECT COUNT(*) FROM competitor_products WHERE pricing_eligible=1"
        ).fetchone()[0]
        residual = c.execute(
            "SELECT COUNT(*) FROM competitor_products "
            "WHERE is_active=1 AND COALESCE(pricing_eligible,0)=0"
        ).fetchone()[0]
    assert eligible_count == 3  # COMP1, COMP2, COMP4
    assert residual == 0


def test_snapshot_backup_written(tmp_db_with_rows, tmp_path):
    mod = _load_backfill_module()
    sys.argv = ["backfill_pricing_eligible_w301.py", "--db", str(tmp_db_with_rows)]
    mod.main()
    backups = list(tmp_db_with_rows.parent.glob("backup_pricing_eligible_w301_*.json"))
    assert len(backups) >= 1, "snapshot backup が作成されていない"
