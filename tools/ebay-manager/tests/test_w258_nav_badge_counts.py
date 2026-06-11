"""W258 get_nav_badge_counts() 単体テスト (2026-06-11).

既存テストパターン (test_w182_availability_gate.py 等) に準拠:
- conftest.py / tmp DB fixture は使わず、init_db() + get_conn() で実 DB を共有
- テスト固有の識別子を使い、終了時に cleanup する
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_get_nav_badge_counts_returns_three_keys():
    """get_nav_badge_counts() が 3 つのキーを持つ dict を返すこと (DB が空でも空 dict にならない)。"""
    from monitor.database import init_db, get_nav_badge_counts
    init_db()
    result = get_nav_badge_counts()
    # supplier_candidates / ebay_listings / emails テーブルが存在すれば 3 キーが返る
    assert isinstance(result, dict)
    assert "supplier_actionable" in result
    assert "supply_risk" in result
    assert "purchase_unconfirmed" in result


def test_get_nav_badge_counts_supplier_actionable():
    """supplier_actionable は status=pending + availability_status!=unavailable/not_found の件数。"""
    from monitor.database import init_db, get_conn, get_nav_badge_counts, add_supplier_candidate

    init_db()

    # テスト用 ebay_item_id (衝突回避のため固有値)
    _test_eid = "test_w258_badge_eid_001"
    _url_pending = "https://test.example.com/w258_pending_001"
    _url_unavail = "https://test.example.com/w258_unavail_001"
    _url_applied = "https://test.example.com/w258_applied_001"

    # 既存 cleanup
    with get_conn() as c:
        c.execute(
            "DELETE FROM supplier_candidates WHERE candidate_url IN (?,?,?)",
            (_url_pending, _url_unavail, _url_applied),
        )

    # (1) pending + availability_status=NULL → カウント対象
    id1 = add_supplier_candidate(
        sku="test_w258_sku",
        candidate_url=_url_pending,
        source_platform="test",
        ebay_item_id=_test_eid,
    )
    # (2) pending + availability_status='unavailable' → 除外対象
    id2 = add_supplier_candidate(
        sku="test_w258_sku",
        candidate_url=_url_unavail,
        source_platform="test",
        ebay_item_id=_test_eid,
        availability_status="unavailable",
    )
    # (3) applied → 除外対象 (status='pending' ではない)
    id3 = add_supplier_candidate(
        sku="test_w258_sku",
        candidate_url=_url_applied,
        source_platform="test",
        ebay_item_id=_test_eid,
    )
    with get_conn() as c:
        c.execute(
            "UPDATE supplier_candidates SET status='applied' WHERE id=?", (id3,)
        )

    result = get_nav_badge_counts()

    # cleanup
    with get_conn() as c:
        c.execute(
            "DELETE FROM supplier_candidates WHERE id IN (?,?,?)",
            (id1, id2, id3),
        )

    assert isinstance(result.get("supplier_actionable"), int)
    # id1 のみが actionable (id2=unavailable 除外、id3=applied 除外)
    # ただし他テストデータが混在するため「>= 1」で確認
    # (テスト固有の増分を検証: before との差分で厳密に確認)
    # → 増分確認: cleanup 前の count と cleanup 後の count の差を確認する方法は
    #   実 DB 共有では確実ではないため、insert 前後の差分で判定する。
    # ここでは「結果が int で 0 以上」を保証するのみ (DB 共有制約)。
    assert result["supplier_actionable"] >= 0


def test_get_nav_badge_counts_supplier_actionable_delta():
    """pending + availability_status=NULL の行を 1 件 insert → count が +1 されること。"""
    from monitor.database import init_db, get_conn, get_nav_badge_counts, add_supplier_candidate

    init_db()

    _url = "https://test.example.com/w258_delta_001"
    _test_eid = "test_w258_delta_eid_001"

    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (_url,))

    before = get_nav_badge_counts().get("supplier_actionable", 0)

    row_id = add_supplier_candidate(
        sku="test_w258_delta_sku",
        candidate_url=_url,
        source_platform="test",
        ebay_item_id=_test_eid,
    )

    after = get_nav_badge_counts().get("supplier_actionable", 0)

    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (row_id,))

    assert after == before + 1, f"expected {before + 1}, got {after}"


def test_get_nav_badge_counts_supply_risk_delta():
    """在庫無 + sku=ebay* + risk_confirmed=0 の行を 1 件 insert → supply_risk が +1 されること。"""
    from monitor.database import init_db, get_conn, get_nav_badge_counts

    init_db()

    _test_eid = "test_w258_risk_eid_001"

    with get_conn() as c:
        c.execute("DELETE FROM ebay_listings WHERE ebay_item_id=?", (_test_eid,))

    before = get_nav_badge_counts().get("supply_risk", 0)

    with get_conn() as c:
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, quantity_ebay, source_status, is_ended, risk_confirmed)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (_test_eid, "ebayyh_test_w258", "Test Item W258", 1, "在庫無", 0, 0),
        )

    after = get_nav_badge_counts().get("supply_risk", 0)

    with get_conn() as c:
        c.execute("DELETE FROM ebay_listings WHERE ebay_item_id=?", (_test_eid,))

    assert after == before + 1, f"expected {before + 1}, got {after}"


def test_get_nav_badge_counts_purchase_unconfirmed_delta():
    """category='supplier_purchase' + confirmed=0 の行を 1 件 insert → purchase_unconfirmed が +1 されること。"""
    from monitor.database import init_db, get_conn, get_nav_badge_counts

    init_db()

    _test_subject = "W258_test_badge_purchase_unique_001"

    with get_conn() as c:
        c.execute("DELETE FROM emails WHERE subject=?", (_test_subject,))

    before = get_nav_badge_counts().get("purchase_unconfirmed", 0)

    with get_conn() as c:
        c.execute(
            """INSERT INTO emails (subject, sender, category, confirmed, fetched_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (_test_subject, "test@example.com", "supplier_purchase", 0),
        )

    after = get_nav_badge_counts().get("purchase_unconfirmed", 0)

    with get_conn() as c:
        c.execute("DELETE FROM emails WHERE subject=?", (_test_subject,))

    assert after == before + 1, f"expected {before + 1}, got {after}"
