"""依頼ボード #48: 無在庫ハンドリングポリシー是正 one-shot の回帰テスト.

対象: scripts/fix_handling_policy_board48.py の抽出ロジック (find_candidates)
と安全ゲート (ABORT_THRESHOLD)。money-direct のため実 eBay API 呼出を伴う
経路 (_enrich_with_live_snapshot / _execute) は pytest 対象外 (K1: 純関数の
みユニットテスト、実機検証は main レビュー通過後に別途実施)。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from monitor.database import get_conn, init_db

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "fix_handling_policy_board48.py"
)


@pytest.fixture()
def board48():
    spec = importlib.util.spec_from_file_location(
        "fix_handling_policy_board48", _SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# settings.json 実値と切り離した最小 cfg (test 決定性優先)。
_CFG = {
    "ebay_business_policies": {
        "shipping_weight_mapping_in_stock": {
            "0-500": "IN_0_500",
            "500-1000": "IN_500_1000",
            "1000-2000": "IN_1000_2000",
        },
        "shipping_weight_mapping_no_stock": {
            "0-500": "NO_0_500",
            "500-1000": "NO_500_1000",
            "1000-2000": "NO_1000_2000",
        },
    }
}


def _insert_listing(
    conn, ebay_item_id, sku, shipping_profile_id, weight_g=800.0,
    is_ended=0, title="Test Item",
):
    conn.execute(
        "INSERT INTO ebay_listings "
        "(ebay_item_id, sku, title, weight_g, shipping_profile_id, is_ended) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ebay_item_id, sku, title, weight_g, shipping_profile_id, is_ended),
    )


def test_find_candidates_identifies_no_stock_sku_with_in_stock_policy(board48):
    """無在庫 SKU + 在庫あり(1日)ポリシー = 抽出対象."""
    init_db()
    with get_conn() as conn:
        _insert_listing(conn, "1001", "ebayyh_p001", "IN_500_1000", weight_g=800.0)
        conn.commit()
        result = board48.find_candidates(conn, _CFG)

    assert len(result) == 1
    row = result[0]
    assert row["ebay_item_id"] == "1001"
    assert row["current_policy_id"] == "IN_500_1000"
    assert row["correct_policy_id"] == "NO_500_1000"


def test_find_candidates_excludes_stock_sku(board48):
    """有在庫 SKU (stock*) は在庫あり(1日)ポリシーで正常 = 抽出対象外."""
    init_db()
    with get_conn() as conn:
        _insert_listing(conn, "1002", "stock01", "IN_500_1000", weight_g=800.0)
        conn.commit()
        result = board48.find_candidates(conn, _CFG)

    assert result == []


def test_find_candidates_excludes_already_correct_no_stock_policy(board48):
    """無在庫 SKU で既に在庫なし(7日)ポリシー = 是正不要、抽出対象外."""
    init_db()
    with get_conn() as conn:
        _insert_listing(conn, "1003", "ebayme_m001", "NO_500_1000", weight_g=800.0)
        conn.commit()
        result = board48.find_candidates(conn, _CFG)

    assert result == []


def test_find_candidates_excludes_ended_listing(board48):
    """is_ended=1 (終了済) は対象外 (revise 対象にならないため)."""
    init_db()
    with get_conn() as conn:
        _insert_listing(
            conn, "1004", "ebayyh_p002", "IN_500_1000", weight_g=800.0, is_ended=1,
        )
        conn.commit()
        result = board48.find_candidates(conn, _CFG)

    assert result == []


def test_find_candidates_weight_selects_correct_no_stock_band(board48):
    """weight_g に応じて正解 no_stock ポリシーの重量帯が変わること."""
    init_db()
    with get_conn() as conn:
        _insert_listing(conn, "1005", "ebayyh_p003", "IN_0_500", weight_g=100.0)
        _insert_listing(conn, "1006", "ebayyh_p004", "IN_1000_2000", weight_g=1500.0)
        conn.commit()
        result = board48.find_candidates(conn, _CFG)

    by_id = {r["ebay_item_id"]: r for r in result}
    assert by_id["1005"]["correct_policy_id"] == "NO_0_500"
    assert by_id["1006"]["correct_policy_id"] == "NO_1000_2000"


def test_find_candidates_abort_threshold(board48):
    """抽出件数が ABORT_THRESHOLD (6) 以上なら RuntimeError で中止する."""
    init_db()
    with get_conn() as conn:
        for i in range(board48.ABORT_THRESHOLD):
            _insert_listing(
                conn, f"200{i}", f"ebayyh_p{i}", "IN_500_1000", weight_g=800.0,
            )
        conn.commit()
        with pytest.raises(RuntimeError, match="中止"):
            board48.find_candidates(conn, _CFG)


def test_find_candidates_below_threshold_does_not_abort(board48):
    """ABORT_THRESHOLD 未満 (想定 5 件含む) なら正常に返す (中止しない)."""
    init_db()
    with get_conn() as conn:
        for i in range(board48.EXPECTED_COUNT):
            _insert_listing(
                conn, f"300{i}", f"ebayyh_q{i}", "IN_500_1000", weight_g=800.0,
            )
        conn.commit()
        result = board48.find_candidates(conn, _CFG)

    assert len(result) == board48.EXPECTED_COUNT


def test_find_candidates_raises_valueerror_when_mapping_missing(board48):
    """settings.json に shipping_weight_mapping_in_stock が無ければ ValueError."""
    init_db()
    with get_conn() as conn:
        with pytest.raises(ValueError):
            board48.find_candidates(conn, {"ebay_business_policies": {}})


# ---------------------------------------------------------------------------
# HIGH-1 (T3 レビュー 2026-07-04): compute_cost_deltas 純関数の回帰テスト
# ---------------------------------------------------------------------------

def _row(**overrides):
    base = {
        "ebay_item_id": "X1",
        "correct_policy_id": "NO_500_1000",
        "ship_cost_usd": 40.0,
        "ship_additional_usd": 0.0,
        "ship_override_present": False,
    }
    base.update(overrides)
    return base


def _bp(cost=30.0, additional=0.0, error=None, intl=False, rt_id=None):
    return {
        "cost": cost,
        "additional": additional,
        "error": error,
        "intl_uses_rate_table": intl,
        "rate_table_id": rt_id,
        "service_code": "US_ExpeditedSppedPAK",
    }


def test_compute_cost_deltas_decrease(board48):
    """現行 $40 → 是正後 BP default $30 = 差額 -$10 (減額)."""
    rows = board48.compute_cost_deltas(
        [_row(ship_cost_usd=40.0)], {"NO_500_1000": _bp(cost=30.0)},
    )
    assert len(rows) == 1
    assert rows[0]["current_ship_cost_usd"] == 40.0
    assert rows[0]["predicted_ship_cost_usd"] == 30.0
    assert rows[0]["delta_usd"] == -10.0
    assert rows[0]["override_will_be_lost"] is False


def test_compute_cost_deltas_increase_flags_warning(board48):
    """現行 $20 → 是正後 BP default $30 = 差額 +$10 (増額) → warning に増額メッセ."""
    rows = board48.compute_cost_deltas(
        [_row(ship_cost_usd=20.0)], {"NO_500_1000": _bp(cost=30.0)},
    )
    assert rows[0]["delta_usd"] == 10.0
    assert any("増額" in w for w in rows[0]["warnings"]), rows[0]["warnings"]


def test_compute_cost_deltas_override_present_flags_ddp_loss(board48):
    """ship_override_present=True → DDP override 消失リスク警告."""
    rows = board48.compute_cost_deltas(
        [_row(ship_cost_usd=100.0, ship_override_present=True)],
        {"NO_500_1000": _bp(cost=30.0)},
    )
    assert rows[0]["override_will_be_lost"] is True
    assert any("DDP" in w or "override" in w.lower() for w in rows[0]["warnings"]), (
        rows[0]["warnings"]
    )


def test_compute_cost_deltas_bp_fetch_error_flags_and_returns_none_delta(board48):
    """destination BP default 取得失敗 → predicted=None、delta=None、warning."""
    rows = board48.compute_cost_deltas(
        [_row()], {"NO_500_1000": _bp(cost=None, error="Account API 通信失敗: timeout")},
    )
    assert rows[0]["predicted_ship_cost_usd"] is None
    assert rows[0]["delta_usd"] is None
    assert any("取得失敗" in w or "予測不能" in w for w in rows[0]["warnings"])


def test_compute_cost_deltas_current_cost_none_flags_unknown(board48):
    """現行送料 (GetItem 由来) が None → 変動不明を warning."""
    rows = board48.compute_cost_deltas(
        [_row(ship_cost_usd=None)], {"NO_500_1000": _bp(cost=30.0)},
    )
    assert rows[0]["current_ship_cost_usd"] is None
    assert rows[0]["delta_usd"] is None
    assert any("不明" in w for w in rows[0]["warnings"])


def test_compute_cost_deltas_international_rate_table_note(board48):
    """国際 rate table 経路の note を warning に含める (買い手国依存)."""
    rows = board48.compute_cost_deltas(
        [_row()], {"NO_500_1000": _bp(cost=30.0, intl=True, rt_id="5284241010")},
    )
    assert any("rate table" in w.lower() for w in rows[0]["warnings"])


def test_compute_cost_deltas_multiple_rows_are_independent(board48):
    """複数 row 入力: 各 row の delta / warnings 判定が独立."""
    rows = board48.compute_cost_deltas(
        [
            _row(ebay_item_id="A", ship_cost_usd=40.0),
            _row(ebay_item_id="B", ship_cost_usd=20.0),
            _row(ebay_item_id="C", ship_cost_usd=40.0, ship_override_present=True),
        ],
        {"NO_500_1000": _bp(cost=30.0)},
    )
    a, b, c = rows
    assert a["delta_usd"] == -10.0 and not a["override_will_be_lost"]
    assert b["delta_usd"] == 10.0 and any("増額" in w for w in b["warnings"])
    assert c["override_will_be_lost"] is True
