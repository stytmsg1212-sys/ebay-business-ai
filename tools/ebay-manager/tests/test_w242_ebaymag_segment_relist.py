"""W242 (2026-06-09): daily_relist の eBaymag 区分除外 + relist 継承 回帰テスト.

検証:
  1. relist (inherit_listing_on_relist) で ebaymag_segment が新 ItemID に継承される
     (継承漏れ = relist プール永久枯渇 = code-reviewer HIGH-1)。
  2. _select_relist_targets が '出さない' のみ選出し、全国/優先国/NULL を除外する。
"""
import pytest

from monitor.database import init_db, get_conn
from tasks.task_daily_relist import inherit_listing_on_relist, _select_relist_targets

_IDS = ("W242_OLD", "W242_NEW", "W242_OK", "W242_ZEN", "W242_YU", "W242_NULL")


def _cleanup():
    with get_conn() as c:
        ph = ",".join("?" * len(_IDS))
        c.execute(f"DELETE FROM ebay_listings WHERE ebay_item_id IN ({ph})", _IDS)


def _seed(conn, item_id, segment):
    conn.execute(
        "INSERT INTO ebay_listings (ebay_item_id, sku, title, quantity_ebay, "
        "watch_count, rank, is_ended, ebaymag_segment) "
        "VALUES (?, 'stock01', 't', 1, 0, 'E', 0, ?)",
        (item_id, segment),
    )


@pytest.fixture(autouse=True)
def _around():
    init_db()
    _cleanup()
    yield
    _cleanup()


def test_relist_inherits_ebaymag_segment():
    """HIGH-1: relist 後の新 ItemID に ebaymag_segment='出さない' が継承される。"""
    with get_conn() as conn:
        _seed(conn, "W242_OLD", "出さない")
    inherit_listing_on_relist(
        old_item_id="W242_OLD", new_item_id="W242_NEW",
        sku="stock01", title="t", current_price=10.0,
    )
    with get_conn() as conn:
        seg = conn.execute(
            "SELECT ebaymag_segment FROM ebay_listings WHERE ebay_item_id='W242_NEW'"
        ).fetchone()[0]
    assert seg == "出さない", f"継承漏れ: 新 ItemID segment={seg!r} (NULL なら枯渇バグ)"


def test_select_excludes_non_dasanai_segment():
    """eBaymag 対象 (全国/優先国/NULL) は relist 対象外、'出さない' のみ選出。"""
    with get_conn() as conn:
        for iid, seg in [("W242_OK", "出さない"), ("W242_ZEN", "全国"),
                         ("W242_YU", "優先国"), ("W242_NULL", None)]:
            _seed(conn, iid, seg)
    ids = {t["ebay_item_id"] for t in _select_relist_targets(limit=200, cooldown_days=10)}
    assert "W242_OK" in ids, "出さない が選出されない (relist 機能停止)"
    assert ids.isdisjoint({"W242_ZEN", "W242_YU", "W242_NULL"}), \
        "eBaymag 対象/未分類が relist 対象に混入 (各国版リンク破壊リスク)"
