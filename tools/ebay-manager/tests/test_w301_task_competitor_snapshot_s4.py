"""W301 AI 店長 Phase1 S4 (2026-07-02): tasks/task_competitor_snapshot.py.

設計書: .company/engineering/docs/2026-06-24-ai-manager-phase1-design.md §5/§6

カバレッジ:
  - kill switch (enabled=false) → success=True + skip 痕跡
  - 対象 0 件 → success=True + skip 痕跡
  - eBay 認証情報未設定 → success=False + 痕跡
  - 対象 = pricing_eligible=1 の active 競合 と rival_classifications real/review
    競合の和集合であること (get_snapshot_targets 経由)
  - GetItem 成功分は competitor_snapshots へ INSERT (蓄積のみ、他テーブル書込なし)
  - 失敗 item は skip でなく failed カウントに計上 (Q0)
  - 1 run の API コール上限 (max_calls_per_run) を超えた分は処理されない
    (= 翌回へ持ち越し、remaining に件数が出る)
"""
from __future__ import annotations

import pytest

from monitor.database import get_conn


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _seed_eligible_competitor(competitor_item_id: str, our_item_id: str = "OUR1") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO competitor_products
               (our_item_id, our_sku, competitor_item_id, competitor_seller,
                seller_location, is_active, pricing_eligible)
               VALUES (?, 'stock01', ?, 'jp_seller_1', 'Japan', 1, 1)""",
            (our_item_id, competitor_item_id),
        )


def _seed_real_classification(competitor_item_id: str, ebay_item_id: str = "OUR2") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO rival_classifications
               (ebay_item_id, competitor_item_id, classification, route,
                shadow_mode, would_be_eligible)
               VALUES (?, ?, 'real', 'ai', 1, 1)""",
            (ebay_item_id, competitor_item_id),
        )


_FAKE_CREDS = {
    "app_id": "fake_app", "dev_id": "fake_dev",
    "cert_id": "fake_cert", "user_token": "fake_token",
}


# ────────────────────────────────────────────────────────────────
# kill switch / 0 件
# ────────────────────────────────────────────────────────────────

def test_kill_switch_disabled_skips(tmp_db):
    from tasks.task_competitor_snapshot import run_competitor_snapshot
    config = {"tasks_enabled": {"competitor_snapshot": {"enabled": False}}}
    result = run_competitor_snapshot(config)
    assert result["success"] is True
    assert "enabled=false" in result["message"]


def test_zero_targets_skips(tmp_db):
    from tasks.task_competitor_snapshot import run_competitor_snapshot
    result = run_competitor_snapshot({})
    assert result["success"] is True
    assert "0 targets" in result["message"]


def test_missing_credentials_fails(tmp_db, monkeypatch):
    _seed_eligible_competitor("COMP1")
    monkeypatch.setattr(
        "tasks.task_competitor_snapshot.get_ebay_credentials",
        lambda config=None: {"app_id": "", "dev_id": "", "cert_id": "", "user_token": ""},
    )

    from tasks.task_competitor_snapshot import run_competitor_snapshot
    result = run_competitor_snapshot({})
    assert result["success"] is False
    assert "認証情報" in result["message"]


# ────────────────────────────────────────────────────────────────
# 対象抽出 (pricing_eligible=1 + rival_classifications real/review 和集合)
# ────────────────────────────────────────────────────────────────

def test_targets_union_of_eligible_and_classified(tmp_db, monkeypatch):
    _seed_eligible_competitor("COMP1", our_item_id="OUR1")
    _seed_real_classification("COMP2", ebay_item_id="OUR2")

    monkeypatch.setattr(
        "tasks.task_competitor_snapshot.get_ebay_credentials",
        lambda config=None: dict(_FAKE_CREDS),
    )

    captured_item_ids = []

    def _fake_batch(item_ids, app_id, dev_id, cert_id, user_token, max_calls=None):
        captured_item_ids.extend(item_ids)
        return (
            {iid: {
                "quantity_sold": 5, "quantity_total": 10, "quantity_available": 5,
                "seller_feedback_score": 1200, "seller_positive_pct": 99.5,
                "seller_country": "JP", "price_usd": 100.0, "shipping_usd": 10.0,
            } for iid in item_ids},
            len(item_ids),
        )

    import monitor.ebay_client as ebay_client_mod
    monkeypatch.setattr(ebay_client_mod, "get_competitor_snapshot_batch", _fake_batch)

    from tasks.task_competitor_snapshot import run_competitor_snapshot
    result = run_competitor_snapshot({})

    assert result["success"] is True
    assert result["targets"] == 2
    assert result["captured"] == 2
    assert result["failed"] == 0
    assert set(captured_item_ids) == {"COMP1", "COMP2"}

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT competitor_item_id, our_item_id, quantity_sold, seller_country "
            "FROM competitor_snapshots ORDER BY competitor_item_id"
        ).fetchall()
    assert [dict(r)["competitor_item_id"] for r in rows] == ["COMP1", "COMP2"]
    row_map = {dict(r)["competitor_item_id"]: dict(r) for r in rows}
    assert row_map["COMP1"]["our_item_id"] == "OUR1"
    assert row_map["COMP2"]["our_item_id"] == "OUR2"
    assert row_map["COMP1"]["quantity_sold"] == 5
    assert row_map["COMP1"]["seller_country"] == "JP"

    # 蓄積のみ: competitor_products / rival_classifications への書込はしない
    with get_conn() as conn:
        cp_row = conn.execute(
            "SELECT pricing_eligible FROM competitor_products WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert cp_row[0] == 1, "既存 pricing_eligible を書き換えていないこと"


def test_failed_items_counted_not_silently_skipped(tmp_db, monkeypatch):
    _seed_eligible_competitor("COMP1")
    _seed_eligible_competitor("COMP2", our_item_id="OUR1")

    monkeypatch.setattr(
        "tasks.task_competitor_snapshot.get_ebay_credentials",
        lambda config=None: dict(_FAKE_CREDS),
    )

    def _fake_batch(item_ids, app_id, dev_id, cert_id, user_token, max_calls=None):
        # COMP1 のみ成功、COMP2 は取得失敗 (results に含まれない)
        return ({"COMP1": {
            "quantity_sold": 1, "quantity_total": 2, "quantity_available": 1,
            "seller_feedback_score": 10, "seller_positive_pct": 90.0,
            "seller_country": "JP", "price_usd": 50.0, "shipping_usd": 5.0,
        }}, len(item_ids))

    import monitor.ebay_client as ebay_client_mod
    monkeypatch.setattr(ebay_client_mod, "get_competitor_snapshot_batch", _fake_batch)

    from tasks.task_competitor_snapshot import run_competitor_snapshot
    result = run_competitor_snapshot({})

    assert result["success"] is True
    assert result["captured"] == 1
    assert result["failed"] == 1  # Q0: silent skip でなく件数計上


# ────────────────────────────────────────────────────────────────
# cap 超過 → 翌回持ち越し (remaining)
# ────────────────────────────────────────────────────────────────

def test_cap_exceeded_carries_over_remaining(tmp_db, monkeypatch):
    for i in range(5):
        _seed_eligible_competitor(f"COMP{i}", our_item_id="OUR1")

    monkeypatch.setattr(
        "tasks.task_competitor_snapshot.get_ebay_credentials",
        lambda config=None: dict(_FAKE_CREDS),
    )

    captured_item_ids = []

    def _fake_batch(item_ids, app_id, dev_id, cert_id, user_token, max_calls=None):
        captured_item_ids.extend(item_ids)
        return (
            {iid: {
                "quantity_sold": 1, "quantity_total": 2, "quantity_available": 1,
                "seller_feedback_score": 10, "seller_positive_pct": 90.0,
                "seller_country": "JP", "price_usd": 50.0, "shipping_usd": 5.0,
            } for iid in item_ids},
            len(item_ids),
        )

    import monitor.ebay_client as ebay_client_mod
    monkeypatch.setattr(ebay_client_mod, "get_competitor_snapshot_batch", _fake_batch)

    from tasks.task_competitor_snapshot import run_competitor_snapshot
    config = {"tasks_enabled": {"competitor_snapshot": {"max_calls_per_run": 2}}}
    result = run_competitor_snapshot(config)

    assert result["success"] is True
    assert result["targets"] == 5
    assert result["captured"] == 2
    assert result["remaining"] == 3
    assert len(captured_item_ids) == 2
