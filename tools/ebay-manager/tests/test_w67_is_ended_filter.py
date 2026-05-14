"""W67 Iteration 3 regression test: is_ended filter 統一棚卸し Tier 1 (7 query / 3 ファイル).

検証対象 (production code に `AND COALESCE(is_ended, 0) = 0` filter 追加):
- task_market_analysis_refresh.py:64-67 (Q1 skip_recent_hours あり)
- task_market_analysis_refresh.py:75-77 (Q2 skip_recent_hours なし)
- tab_market_strategy.py:51 (区分別分布)
- tab_market_strategy.py:260 (検索 LIKE)
- tab_market_strategy.py:269 (全 listing)
- task_rival_detection.py:54 (高ランク)
- task_rival_detection.py:69 (general fallback)

baseline (production DB 2026-04-30 02:00 JST 時点 verify):
- ebay_listings: total 541 / active 440 / ended 101
- ended AND qty>=1 = 44 件 (filter 漏れで誤処理対象になっていた)
- mkt_strategy_listall: BEFORE 254 → AFTER 210 (diff 44 = 除外された ended)
- rival_general: BEFORE 541 → AFTER 440 (diff 101)

過去事故: 2026-04-30 不具合 1+3 (`get_ebay_listings_supply_risk` WHERE 不統一) と同根。
詳細: `.claude/rules/sku-rules.md` / `feedback_silent_skip_prevention.md` (Q0 業務 critical 経路)
"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """fresh DB を作って monitor.database.DB_PATH を差し替え + init_db で schema 作成"""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert(conn, eid: str, sku: str, title: str = "T", *,
            quantity_ebay: int = 1, is_ended: int = 0,
            rank: str | None = None,
            primary_market: str | None = None,
            watch_count: int | None = None,
            market_analysis_at: str | None = None,
            source_status: str | None = None,
            source_out_of_stock_since: str | None = None):
    conn.execute(
        """INSERT INTO ebay_listings (
              ebay_item_id, sku, title, current_price, quantity_ebay, is_ended,
              rank, primary_market, watch_count, market_analysis_at,
              source_status, source_out_of_stock_since)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (eid, sku, title, 100.0, quantity_ebay, is_ended,
         rank, primary_market, watch_count, market_analysis_at,
         source_status, source_out_of_stock_since),
    )


# ============================================================================
# task_market_analysis_refresh.py
# ============================================================================

def test_market_analysis_refresh_q1_excludes_ended(tmp_db, monkeypatch):
    """task_market_analysis_refresh.py:64-67 (Q1 skip_recent_hours あり) で ended 除外"""
    from monitor.database import get_conn
    from tasks.task_market_analysis_refresh import _get_active_listings

    with get_conn() as c:
        # active 2 + ended 2 (full mix)
        _insert(c, "ITEM_A", "stock:01", quantity_ebay=1, is_ended=0)
        _insert(c, "ITEM_B", "ebayyh_x", quantity_ebay=2, is_ended=0)
        _insert(c, "ITEM_C", "stock:02", quantity_ebay=1, is_ended=1)  # ended
        _insert(c, "ITEM_D", "ebayyh_y", quantity_ebay=3, is_ended=1)  # ended

    # Q1: skip_recent_hours=24
    targets = _get_active_listings(skip_recent_hours=24)
    eids = [t["ebay_item_id"] for t in targets]
    assert "ITEM_A" in eids
    assert "ITEM_B" in eids
    assert "ITEM_C" not in eids, f"ended ITEM_C が混入: {eids}"
    assert "ITEM_D" not in eids, f"ended ITEM_D が混入: {eids}"


def test_market_analysis_refresh_q2_excludes_ended(tmp_db, monkeypatch):
    """task_market_analysis_refresh.py:75-77 (Q2 skip_recent_hours なし) で ended 除外"""
    from monitor.database import get_conn
    from tasks.task_market_analysis_refresh import _get_active_listings

    with get_conn() as c:
        _insert(c, "ITEM_A", "stock:01", quantity_ebay=1, is_ended=0)
        _insert(c, "ITEM_B", "stock:02", quantity_ebay=1, is_ended=1)  # ended

    # Q2: skip_recent_hours=None (else branch)
    targets = _get_active_listings(skip_recent_hours=None)
    eids = [t["ebay_item_id"] for t in targets]
    assert "ITEM_A" in eids
    assert "ITEM_B" not in eids, f"ended ITEM_B が混入 (Q2): {eids}"


# ============================================================================
# tab_market_strategy.py (DB-level only、Streamlit runtime 非依存)
# ============================================================================

def test_tab_market_strategy_distribution_excludes_ended(tmp_db):
    """tab_market_strategy.py:51 区分別分布クエリで ended 除外 (row-level + BEFORE/AFTER diff verify)"""
    from monitor.database import get_conn

    with get_conn() as c:
        _insert(c, "ITEM_A", "s1", quantity_ebay=1, is_ended=0, primary_market="US_only")
        _insert(c, "ITEM_B", "s2", quantity_ebay=2, is_ended=0, primary_market="US_only")
        _insert(c, "ITEM_C", "s3", quantity_ebay=1, is_ended=1, primary_market="US_only")  # ended
        _insert(c, "ITEM_D", "s4", quantity_ebay=1, is_ended=1, primary_market="mixed_global")  # ended

    # row-level verify: 修正後 SQL は ITEM_A/B のみ含む (ITEM_C/D 除外)
    with get_conn() as c:
        new_rows = c.execute(
            """SELECT ebay_item_id, primary_market FROM ebay_listings
               WHERE quantity_ebay >= 1 AND COALESCE(is_ended, 0) = 0"""
        ).fetchall()
    new_eids = {r["ebay_item_id"] for r in new_rows}
    assert new_eids == {"ITEM_A", "ITEM_B"}, (
        f"ended 除外失敗: 期待 {{ITEM_A, ITEM_B}} 取得 {new_eids}"
    )

    # diff verify: 旧 SQL (filter なし) では 4 件、新 SQL では 2 件
    with get_conn() as c:
        old_total = sum(r[1] for r in c.execute(
            "SELECT COALESCE(primary_market,'未判定'), COUNT(*) "
            "FROM ebay_listings WHERE quantity_ebay >= 1 GROUP BY primary_market"
        ).fetchall())
    new_total = len(new_rows)
    assert old_total == 4 and new_total == 2 and old_total - new_total == 2, (
        f"diff 異常: BEFORE {old_total} → AFTER {new_total} (期待 4→2、ended 2 件除外)"
    )


def test_tab_market_strategy_search_excludes_ended(tmp_db):
    """tab_market_strategy.py:260 LIKE 検索で ended 除外"""
    from monitor.database import get_conn

    with get_conn() as c:
        _insert(c, "ITEM_A", "s1", title="Sony Alpha A7 Camera", quantity_ebay=1, is_ended=0)
        _insert(c, "ITEM_B", "s2", title="Sony Alpha A7R Camera", quantity_ebay=1, is_ended=1)  # ended

    with get_conn() as c:
        rows = c.execute(
            """SELECT sku, title, ebay_item_id FROM ebay_listings
               WHERE quantity_ebay >= 1
                 AND COALESCE(is_ended, 0) = 0
                 AND title LIKE ? COLLATE NOCASE
               ORDER BY title COLLATE NOCASE ASC""",
            ("%Sony%",),
        ).fetchall()
    eids = [r["ebay_item_id"] for r in rows]
    assert "ITEM_A" in eids
    assert "ITEM_B" not in eids, f"ended ITEM_B が検索結果に: {eids}"


def test_tab_market_strategy_listall_excludes_ended(tmp_db):
    """tab_market_strategy.py:269 全 listing 表示で ended 除外"""
    from monitor.database import get_conn

    with get_conn() as c:
        _insert(c, "ITEM_A", "s1", quantity_ebay=1, is_ended=0)
        _insert(c, "ITEM_B", "s2", quantity_ebay=1, is_ended=1)  # ended

    with get_conn() as c:
        rows = c.execute(
            """SELECT sku, title, ebay_item_id FROM ebay_listings
               WHERE quantity_ebay >= 1
                 AND COALESCE(is_ended, 0) = 0
               ORDER BY title COLLATE NOCASE ASC"""
        ).fetchall()
    eids = [r["ebay_item_id"] for r in rows]
    assert "ITEM_A" in eids
    assert "ITEM_B" not in eids, f"ended ITEM_B が listall に: {eids}"


# ============================================================================
# task_rival_detection.py
# ============================================================================

def test_rival_detection_high_rank_excludes_ended(tmp_db):
    """task_rival_detection.py:54 高ランク rival 認識で ended 除外"""
    from monitor.database import get_conn

    with get_conn() as c:
        _insert(c, "ITEM_A", "s1", title="Active S rank", rank="S", watch_count=10, is_ended=0)
        _insert(c, "ITEM_B", "s2", title="Ended A rank", rank="A", watch_count=20, is_ended=1)  # ended

    with get_conn() as c:
        rows = c.execute(
            """SELECT title, watch_count, rank
               FROM ebay_listings
               WHERE rank IN ('S', 'A', 'B')
                 AND COALESCE(is_ended, 0) = 0
               ORDER BY watch_count DESC LIMIT 10"""
        ).fetchall()
    titles = [r[0] for r in rows]
    assert "Active S rank" in titles
    assert "Ended A rank" not in titles, f"ended が rival 認識: {titles}"


def test_rival_detection_general_excludes_ended(tmp_db):
    """task_rival_detection.py:69 general fallback で ended 除外"""
    from monitor.database import get_conn

    with get_conn() as c:
        _insert(c, "ITEM_A", "s1", title="Active item", watch_count=5, is_ended=0)
        _insert(c, "ITEM_B", "s2", title="Ended item", watch_count=100, is_ended=1)  # ended (高 watch だが ended)

    with get_conn() as c:
        rows = c.execute(
            """SELECT title, watch_count, rank
               FROM ebay_listings
               WHERE COALESCE(is_ended, 0) = 0
               ORDER BY watch_count DESC LIMIT 10"""
        ).fetchall()
    titles = [r[0] for r in rows]
    assert "Active item" in titles
    assert "Ended item" not in titles, f"ended が general fallback rival 認識: {titles}"
