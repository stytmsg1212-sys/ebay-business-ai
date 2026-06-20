"""W284 eBaymag 各国版 自動連携 Phase1 回帰テスト (2026-06-20).

code-reviewer 要求の回帰テスト:
- migration v77/v78 冪等性
- apply_queue 2段集約 (同一 item_id の active job が1本)
- resolve_desired_sites の各区分

relist #5 撤廃の回帰は tests/test_w284_relist_sku_removed.py (D agent 作成) を参照。
"""
from monitor.database import (
    init_db,
    get_conn,
    enqueue_ebaymag_apply,
    get_active_ebaymag_apply_jobs,
)
from monitor.ebaymag_segment import resolve_desired_sites

_TEST_EID = "TEST_W284_ITEM"


def _cleanup() -> None:
    with get_conn() as c:
        c.execute("DELETE FROM ebaymag_apply_queue WHERE ebay_item_id=?", (_TEST_EID,))


def test_init_db_idempotent_v77_v78():
    """init_db 2回連続で schema v78 到達 + 列/テーブル存在 (Q2 冪等性)."""
    init_db()
    init_db()
    with get_conn() as c:
        v = c.execute("PRAGMA user_version").fetchone()[0]
        cols = {r[1] for r in c.execute("PRAGMA table_info(ebay_listings)").fetchall()}
        tbl = c.execute(
            "SELECT name FROM sqlite_master WHERE name='ebaymag_apply_queue'"
        ).fetchone()
    assert v >= 78, f"schema v78 未達 (v={v})"
    assert {"ebaymag_desired_sites_json", "ebaymag_desired_updated_at"} <= cols
    assert tbl is not None


def test_enqueue_idempotent_single_active_job():
    """同一 item_id を2回 enqueue → active job は1本に集約 + reason は後勝ち (Codex-H3)."""
    init_db()  # テストDB分離環境で v78 テーブルを保証
    _cleanup()
    try:
        enqueue_ebaymag_apply(_TEST_EID, "new_listing")
        enqueue_ebaymag_apply(_TEST_EID, "segment_change")
        jobs = [j for j in get_active_ebaymag_apply_jobs()
                if j["ebay_item_id"] == _TEST_EID]
        assert len(jobs) == 1, f"active job が1本でない: {len(jobs)}"
        assert jobs[0]["reason"] == "segment_change", "reason 後勝ちでない"
    finally:
        _cleanup()


def test_resolve_desired_sites_segments():
    """4区分の出品国解決 (全国=7 / 出さない=空 / カスタム=有効コードのみ)."""
    assert sorted(resolve_desired_sites("X", "全国")) == \
        ["AU", "CA", "DE", "ES", "FR", "IT", "UK"]
    assert resolve_desired_sites("X", "出さない") == []
    assert sorted(resolve_desired_sites("X", "カスタム", ["UK", "DE", "BAD"])) == \
        ["DE", "UK"]
