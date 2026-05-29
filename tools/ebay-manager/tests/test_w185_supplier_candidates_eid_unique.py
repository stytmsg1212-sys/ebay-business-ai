"""W185 回帰テスト (2026-05-29 Opus 4.8 総チェック H3): supplier_candidates の
UNIQUE(sku, candidate_url) → UNIQUE(ebay_item_id, candidate_url) 張り替え + ebay_item_id
NOT NULL 化 + one-shot migration の安全性を保証する.

背景: sku は listing 一意キーに使えない (sku-rules.md). 同一 listing が別 sku を持つと
旧 UNIQUE(sku, candidate_url) は dedup を取り違える. listing 識別は ebay_item_id.

不変条件 (本テストが守る):
1. fresh DB の supplier_candidates が UNIQUE(ebay_item_id, candidate_url) + ebay_item_id NOT NULL.
2. init_db 2 回でデータ保持 (冪等性, Q2) かつ v56 gate が誤って bump しない (旧 DB) / する (新 DB).
3. 同一 (ebay_item_id, candidate_url) は INSERT OR IGNORE で 1 行に dedup.
4. sku が異なっても (ebay_item_id, candidate_url) が同じなら dedup される (W185 の核心).
5. add_supplier_candidate は ebay_item_id None/空 で ValueError (Q0 silent NULL 挿入防止).
6. get_supplier_candidates(ebay_item_id=...) で listing 単位に絞れる.
7. one-shot migration: 旧スキーマ DB を新 UNIQUE へ張り替え, status 優先 dedup, backup 保持, 冪等.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# 旧スキーマ (UNIQUE(sku, candidate_url), ebay_item_id nullable) — migration 入力の再現用.
_OLD_SCHEMA = """
CREATE TABLE supplier_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    ebay_item_id TEXT,
    source_platform TEXT,
    candidate_url TEXT NOT NULL,
    candidate_price_jpy INTEGER,
    candidate_title TEXT,
    match_score INTEGER,
    match_reasoning TEXT,
    profit_jpy REAL,
    profitable INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    user_action_at TIMESTAMP,
    discovered_via TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    junk_likely_untested INTEGER DEFAULT 0,
    alt_listing_possible INTEGER DEFAULT 0,
    alt_listing_note TEXT,
    auto_rejected INTEGER DEFAULT 0,
    eval_model TEXT,
    availability_status TEXT,
    availability_checked_at TIMESTAMP,
    availability_signal TEXT,
    UNIQUE(sku, candidate_url)
);
CREATE INDEX idx_supplier_candidates_sku ON supplier_candidates(sku);
CREATE INDEX idx_supplier_candidates_status
    ON supplier_candidates(status, match_score DESC);
PRAGMA user_version = 55;
"""


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migrate_v56", _SCRIPTS / "migrate_supplier_candidates_v56.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _unique_first_col(conn: sqlite3.Connection) -> str | None:
    for idx in conn.execute(
        "PRAGMA index_list(supplier_candidates)"
    ).fetchall():
        name, origin = idx[1], idx[3]
        if str(name).startswith("sqlite_autoindex") and origin == "u":
            cols = [
                r[2]
                for r in conn.execute(f"PRAGMA index_info({name})").fetchall()
            ]
            if cols:
                return cols[0]
    return None


# ---------------------------------------------------------------------------
# fresh DB スキーマ + 冪等性
# ---------------------------------------------------------------------------

def test_fresh_db_has_eid_unique_and_notnull(tmp_path, monkeypatch):
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fresh.db")
    db.init_db()
    with db.get_conn() as c:
        assert _unique_first_col(c) == "ebay_item_id", \
            "UNIQUE が ebay_item_id ベースでない"
        info = {r[1]: r for r in c.execute(
            "PRAGMA table_info(supplier_candidates)"
        ).fetchall()}
        # PRAGMA table_info: (cid, name, type, notnull, dflt, pk). notnull=1.
        assert info["ebay_item_id"][3] == 1, "ebay_item_id が NOT NULL でない"
        assert c.execute("PRAGMA user_version").fetchone()[0] >= 56, \
            "fresh DB で v56 まで bump されていない"


def test_init_db_idempotent_keeps_rows(tmp_path, monkeypatch):
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "idem.db")
    db.init_db()
    db.add_supplier_candidate(
        sku="ebayyh_p1", candidate_url="https://x/1",
        source_platform="yahoo", ebay_item_id="E1",
    )
    db.init_db()  # 2 回目: データ保持 + v56 gate が壊さない
    with db.get_conn() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM supplier_candidates"
        ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# dedup (INSERT OR IGNORE) の挙動
# ---------------------------------------------------------------------------

def test_same_eid_url_dedups(tmp_path, monkeypatch):
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dedup.db")
    db.init_db()
    first = db.add_supplier_candidate(
        sku="ebayyh_p1", candidate_url="https://x/1",
        source_platform="yahoo", ebay_item_id="E1",
    )
    dup = db.add_supplier_candidate(
        sku="ebayyh_p1", candidate_url="https://x/1",
        source_platform="yahoo", ebay_item_id="E1",
    )
    assert first is not None and dup is None, "同一 (eid,url) が dedup されない"


def test_diff_sku_same_eid_url_dedups(tmp_path, monkeypatch):
    """W185 核心: sku が違っても (ebay_item_id, candidate_url) が同じなら dedup."""
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "diffsku.db")
    db.init_db()
    a = db.add_supplier_candidate(
        sku="ebayyh_p1", candidate_url="https://x/1",
        source_platform="yahoo", ebay_item_id="E1",
    )
    b = db.add_supplier_candidate(
        sku="ebayme_m2", candidate_url="https://x/1",  # 別 sku, 同 eid+url
        source_platform="mercari", ebay_item_id="E1",
    )
    assert a is not None and b is None, \
        "別 sku だが同一 (eid,url) が dedup されない (旧 UNIQUE(sku,...) 残存の疑い)"
    with db.get_conn() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM supplier_candidates WHERE ebay_item_id='E1'"
        ).fetchone()[0] == 1


def test_diff_eid_same_url_kept_separate(tmp_path, monkeypatch):
    """別 listing (eid) なら同一 url でも別行として残る."""
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "diffeid.db")
    db.init_db()
    db.add_supplier_candidate(
        sku="ebayyh_p1", candidate_url="https://x/1",
        source_platform="yahoo", ebay_item_id="E1",
    )
    db.add_supplier_candidate(
        sku="ebayyh_p2", candidate_url="https://x/1",
        source_platform="yahoo", ebay_item_id="E2",
    )
    with db.get_conn() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM supplier_candidates"
        ).fetchone()[0] == 2


# ---------------------------------------------------------------------------
# add / get の API 契約
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, ""])
def test_add_requires_ebay_item_id(tmp_path, monkeypatch, bad):
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "guard.db")
    db.init_db()
    with pytest.raises(ValueError):
        db.add_supplier_candidate(
            sku="ebayyh_p1", candidate_url="https://x/1",
            source_platform="yahoo", ebay_item_id=bad,
        )


def test_get_by_ebay_item_id(tmp_path, monkeypatch):
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "geteid.db")
    db.init_db()
    db.add_supplier_candidate(
        sku="ebayyh_p1", candidate_url="https://x/1",
        source_platform="yahoo", ebay_item_id="E1",
    )
    db.add_supplier_candidate(
        sku="ebayyh_p2", candidate_url="https://x/2",
        source_platform="yahoo", ebay_item_id="E2",
    )
    rows = db.get_supplier_candidates(ebay_item_id="E1")
    assert len(rows) == 1 and rows[0]["ebay_item_id"] == "E1"


# ---------------------------------------------------------------------------
# one-shot migration
# ---------------------------------------------------------------------------

def _seed_old_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.executescript(_OLD_SCHEMA)
    rows = [
        # (id, sku, eid, url, status) — E1/U1 が衝突 (別 sku, applied vs rejected)
        (1, "ebayA_x", "E1", "https://u/1", "rejected"),
        (2, "ebayB_y", "E1", "https://u/1", "applied"),
        # 非衝突
        (3, "ebayC_z", "E2", "https://u/2", "pending"),
    ]
    for rid, sku, eid, url, st in rows:
        conn.execute(
            "INSERT INTO supplier_candidates (id, sku, ebay_item_id, "
            "candidate_url, status) VALUES (?,?,?,?,?)",
            (rid, sku, eid, url, st),
        )
    conn.commit()
    conn.close()


def test_one_shot_migration_dedups_and_backs_up(tmp_path, monkeypatch):
    db_path = tmp_path / "migrate.db"
    _seed_old_db(db_path)
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", db_path)

    mig = _load_migration()
    monkeypatch.setattr(sys, "argv", ["migrate", "--apply"])
    assert mig.main() == 0

    with db.get_conn() as c:
        # 新 UNIQUE + user_version
        assert _unique_first_col(c) == "ebay_item_id"
        assert c.execute("PRAGMA user_version").fetchone()[0] == 56
        # dedup: 3 行 → 2 行
        assert c.execute(
            "SELECT COUNT(*) FROM supplier_candidates"
        ).fetchone()[0] == 2
        # E1/U1 は applied 優先で id=2 が残る
        row = c.execute(
            "SELECT id, status FROM supplier_candidates "
            "WHERE ebay_item_id='E1'"
        ).fetchone()
        assert (row[0], row[1]) == (2, "applied"), \
            f"applied 優先 dedup が効いていない: {tuple(row)}"
        # backup table 保持 (元 3 行)
        assert c.execute(
            "SELECT COUNT(*) FROM supplier_candidates_old_w185"
        ).fetchone()[0] == 3


def test_one_shot_migration_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "migrate_idem.db"
    _seed_old_db(db_path)
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", db_path)

    mig = _load_migration()
    monkeypatch.setattr(sys, "argv", ["migrate", "--apply"])
    assert mig.main() == 0
    after1 = None
    with db.get_conn() as c:
        after1 = c.execute(
            "SELECT COUNT(*) FROM supplier_candidates"
        ).fetchone()[0]
    # 2 回目: backup 既存 + 新 UNIQUE 済 = no-op skip (exit 0, 行数不変)
    assert mig.main() == 0
    with db.get_conn() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM supplier_candidates"
        ).fetchone()[0] == after1


def test_one_shot_dry_run_no_write(tmp_path, monkeypatch):
    db_path = tmp_path / "migrate_dry.db"
    _seed_old_db(db_path)
    import monitor.database as db
    monkeypatch.setattr(db, "DB_PATH", db_path)

    mig = _load_migration()
    monkeypatch.setattr(sys, "argv", ["migrate"])  # --apply 無し
    assert mig.main() == 0
    with db.get_conn() as c:
        # 書込なし: 旧 UNIQUE のまま, backup 未作成, 3 行維持
        assert _unique_first_col(c) == "sku"
        assert c.execute("PRAGMA user_version").fetchone()[0] == 55
        assert c.execute(
            "SELECT COUNT(*) FROM supplier_candidates"
        ).fetchone()[0] == 3
        assert c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='supplier_candidates_old_w185'"
        ).fetchone() is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
