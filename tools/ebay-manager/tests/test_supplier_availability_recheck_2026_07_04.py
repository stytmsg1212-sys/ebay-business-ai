"""依頼ボード#45 (2026-07-04): 仕入先候補 availability 定期再チェックの unit test.

scope:
- ヤフオク「落札者なし終了」24h 猶予ロジック (_classify_yahoo_with_grace)
- _classify_candidate の platform dispatch (yahoo auctions vs 他)
- DB 上書きガード (status IN ('pending','accepted') のみ対象、rejected/applied 不可侵)
- get_supplier_candidates_for_availability_recheck の stale 抽出条件
- run_supplier_availability_recheck の end-to-end (mock 判定 → DB 反映)

実 web fetch は行わない (mock)。既存 test_w182_availability_gate.py の作法を踏襲。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitor.yahoo_auction_status import YahooEndStatus  # noqa: E402


# ---------------------------------------------------------------------------
# _classify_yahoo_with_grace: 24h 猶予ロジック
# ---------------------------------------------------------------------------

def test_yahoo_grace_has_winner_is_immediately_unavailable():
    """落札済 (has_winner=True) は即 unavailable (24h 猶予なし)。"""
    from tasks.task_supplier_availability_recheck import _classify_yahoo_with_grace
    est = YahooEndStatus(
        is_ended=True, has_winner=True,
        end_time_utc=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    with patch("tasks.task_supplier_availability_recheck.fetch_yahoo_end_status", return_value=est):
        r = _classify_yahoo_with_grace("https://auctions.yahoo.co.jp/jp/auction/x", "2026-07-04T00:00:00+00:00")
    assert r["conclusive"] is True
    assert r["status"] == "unavailable"


def test_yahoo_grace_no_winner_within_24h_is_grace_not_conclusive():
    """落札者なし終了・終了 1h 後 (24h 未満) は判定保留 (grace)。"""
    from tasks.task_supplier_availability_recheck import _classify_yahoo_with_grace
    est = YahooEndStatus(
        is_ended=True, has_winner=False,
        end_time_utc=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    with patch("tasks.task_supplier_availability_recheck.fetch_yahoo_end_status", return_value=est):
        r = _classify_yahoo_with_grace("https://auctions.yahoo.co.jp/jp/auction/x", "2026-07-04T00:00:00+00:00")
    assert r["conclusive"] is False
    assert r["status"] == "grace"


def test_yahoo_grace_no_winner_after_24h_is_unavailable():
    """落札者なし終了・終了 25h 後 (24h 猶予経過) は unavailable 確定。"""
    from tasks.task_supplier_availability_recheck import _classify_yahoo_with_grace
    est = YahooEndStatus(
        is_ended=True, has_winner=False,
        end_time_utc=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    with patch("tasks.task_supplier_availability_recheck.fetch_yahoo_end_status", return_value=est):
        r = _classify_yahoo_with_grace("https://auctions.yahoo.co.jp/jp/auction/x", "2026-07-04T00:00:00+00:00")
    assert r["conclusive"] is True
    assert r["status"] == "unavailable"


def test_yahoo_grace_open_auction_is_available():
    """進行中 (is_ended=False) は available 確定。"""
    from tasks.task_supplier_availability_recheck import _classify_yahoo_with_grace
    est = YahooEndStatus(is_ended=False, has_winner=None, end_time_utc=None)
    with patch("tasks.task_supplier_availability_recheck.fetch_yahoo_end_status", return_value=est):
        r = _classify_yahoo_with_grace("https://auctions.yahoo.co.jp/jp/auction/x", "2026-07-04T00:00:00+00:00")
    assert r["conclusive"] is True
    assert r["status"] == "available"


def test_yahoo_grace_raw_error_is_not_conclusive():
    """fetch 失敗 (raw_error) は判定保留 (Q0: unavailable に断定しない)。"""
    from tasks.task_supplier_availability_recheck import _classify_yahoo_with_grace
    est = YahooEndStatus(is_ended=False, has_winner=None, end_time_utc=None, raw_error="http_error: Timeout")
    with patch("tasks.task_supplier_availability_recheck.fetch_yahoo_end_status", return_value=est):
        r = _classify_yahoo_with_grace("https://auctions.yahoo.co.jp/jp/auction/x", "2026-07-04T00:00:00+00:00")
    assert r["conclusive"] is False
    assert r["status"] == "unknown"


def test_yahoo_grace_unexpected_exception_is_not_conclusive():
    """fetch_yahoo_end_status が例外送出しても判定保留 (silent crash しない)。"""
    from tasks.task_supplier_availability_recheck import _classify_yahoo_with_grace
    with patch(
        "tasks.task_supplier_availability_recheck.fetch_yahoo_end_status",
        side_effect=RuntimeError("boom"),
    ):
        r = _classify_yahoo_with_grace("https://auctions.yahoo.co.jp/jp/auction/x", "2026-07-04T00:00:00+00:00")
    assert r["conclusive"] is False
    assert r["status"] == "unknown"


# ---------------------------------------------------------------------------
# _classify_candidate: platform dispatch
# ---------------------------------------------------------------------------

def test_classify_candidate_dispatches_yahoo_auctions_to_grace_logic():
    from tasks.task_supplier_availability_recheck import _classify_candidate
    est = YahooEndStatus(is_ended=True, has_winner=True, end_time_utc=datetime.now(timezone.utc))
    with patch("tasks.task_supplier_availability_recheck.fetch_yahoo_end_status", return_value=est):
        r = _classify_candidate("https://auctions.yahoo.co.jp/jp/auction/x", "yahoo_auctions")
    assert r["status"] == "unavailable"
    assert r["conclusive"] is True


def test_classify_candidate_dispatches_non_yahoo_to_w182_gate():
    """mercari 等は既存 check_candidate_availability に委譲する。"""
    from tasks.task_supplier_availability_recheck import _classify_candidate
    with patch(
        "tasks.task_supplier_availability_recheck.check_candidate_availability",
        return_value={"status": "unavailable", "signal": "売り切れました", "checked_at": "2026-07-04T00:00:00+00:00"},
    ):
        r = _classify_candidate("https://jp.mercari.com/item/m123", "mercari")
    assert r["conclusive"] is True
    assert r["status"] == "unavailable"


def test_classify_candidate_unknown_is_not_conclusive():
    from tasks.task_supplier_availability_recheck import _classify_candidate
    with patch(
        "tasks.task_supplier_availability_recheck.check_candidate_availability",
        return_value={"status": "unknown", "signal": "no signal matched", "checked_at": "2026-07-04T00:00:00+00:00"},
    ):
        r = _classify_candidate("https://jp.mercari.com/item/m123", "mercari")
    assert r["conclusive"] is False


def test_classify_candidate_empty_url_is_not_conclusive():
    from tasks.task_supplier_availability_recheck import _classify_candidate
    r = _classify_candidate("", "mercari")
    assert r["conclusive"] is False
    assert r["status"] == "unknown"


# ---------------------------------------------------------------------------
# DB ガード: status IN ('pending','accepted') のみ上書き対象 (user 判断枠は不可侵)
# ---------------------------------------------------------------------------

def _insert_candidate(conn, *, ebay_item_id, candidate_url, status, sku="ebaytest_p1"):
    conn.execute(
        """INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, status)
           VALUES (?,?,?,?)""",
        (sku, ebay_item_id, candidate_url, status),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_reject_supplier_candidate_availability_updates_pending():
    from monitor.database import init_db, get_conn, reject_supplier_candidate_availability
    init_db()
    url = "https://test.example.com/w45_reject_pending"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (url,))
        cid = _insert_candidate(c, ebay_item_id="test_w45_eid_1", candidate_url=url, status="pending")

    ok = reject_supplier_candidate_availability(cid, "unavailable", "sold out signal")
    assert ok is True

    with get_conn() as c:
        row = c.execute(
            "SELECT status, auto_rejected, availability_status FROM supplier_candidates WHERE id=?", (cid,)
        ).fetchone()
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (cid,))
    assert row[0] == "rejected"
    assert row[1] == 1
    assert row[2] == "unavailable"


def test_reject_supplier_candidate_availability_does_not_touch_applied():
    """status='applied' (既に採用済) の候補は上書きしない (rowcount=0)。"""
    from monitor.database import init_db, get_conn, reject_supplier_candidate_availability
    init_db()
    url = "https://test.example.com/w45_reject_applied"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (url,))
        cid = _insert_candidate(c, ebay_item_id="test_w45_eid_2", candidate_url=url, status="applied")

    ok = reject_supplier_candidate_availability(cid, "unavailable", "sold out signal")
    assert ok is False

    with get_conn() as c:
        row = c.execute("SELECT status, auto_rejected FROM supplier_candidates WHERE id=?", (cid,)).fetchone()
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (cid,))
    assert row[0] == "applied"
    assert row[1] in (0, None)


def test_update_supplier_candidate_availability_does_not_touch_rejected():
    """既に status='rejected' (user 不採用済) の候補は availability_* だけの更新も上書きしない。"""
    from monitor.database import init_db, get_conn, update_supplier_candidate_availability
    init_db()
    url = "https://test.example.com/w45_update_rejected"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (url,))
        cid = _insert_candidate(c, ebay_item_id="test_w45_eid_3", candidate_url=url, status="rejected")

    ok = update_supplier_candidate_availability(cid, "available", "bid available")
    assert ok is False

    with get_conn() as c:
        row = c.execute("SELECT availability_status FROM supplier_candidates WHERE id=?", (cid,)).fetchone()
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (cid,))
    assert row[0] is None


def test_update_supplier_candidate_availability_updates_accepted():
    from monitor.database import init_db, get_conn, update_supplier_candidate_availability
    init_db()
    url = "https://test.example.com/w45_update_accepted"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (url,))
        cid = _insert_candidate(c, ebay_item_id="test_w45_eid_4", candidate_url=url, status="accepted")

    ok = update_supplier_candidate_availability(cid, "available", "bid available", "2026-07-04T00:00:00+00:00")
    assert ok is True

    with get_conn() as c:
        row = c.execute(
            "SELECT status, availability_status, availability_checked_at FROM supplier_candidates WHERE id=?",
            (cid,),
        ).fetchone()
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (cid,))
    assert row[0] == "accepted"  # status 自体は変えない
    assert row[1] == "available"
    assert row[2] == "2026-07-04T00:00:00+00:00"


# ---------------------------------------------------------------------------
# get_supplier_candidates_for_availability_recheck: stale 抽出条件
# ---------------------------------------------------------------------------

def test_get_candidates_for_recheck_selects_null_and_stale_excludes_fresh_and_history():
    """MED-1 修正後: stale 判定 / ORDER BY は availability_attempted_at 基準になる。"""
    from monitor.database import init_db, get_conn, get_supplier_candidates_for_availability_recheck
    init_db()
    marker = "w45_stale_test_"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))
        # (1) NULL attempted_at + pending → 対象
        c.execute(
            """INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, status)
               VALUES (?,?,?,?)""",
            (f"{marker}1", "eid_stale_1", "https://test.example.com/w45_null", "pending"),
        )
        # (2) 5日前試行 + accepted (stale=3日) → 対象
        c.execute(
            """INSERT INTO supplier_candidates
               (sku, ebay_item_id, candidate_url, status, availability_attempted_at)
               VALUES (?,?,?,?, datetime('now','-5 days'))""",
            (f"{marker}2", "eid_stale_2", "https://test.example.com/w45_old", "accepted"),
        )
        # (3) 1時間前試行 (fresh) → 対象外
        c.execute(
            """INSERT INTO supplier_candidates
               (sku, ebay_item_id, candidate_url, status, availability_attempted_at)
               VALUES (?,?,?,?, datetime('now','-1 hours'))""",
            (f"{marker}3", "eid_stale_3", "https://test.example.com/w45_fresh", "pending"),
        )
        # (4) rejected (history) + NULL attempted_at → 対象外 (status 対象外)
        c.execute(
            """INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, status)
               VALUES (?,?,?,?)""",
            (f"{marker}4", "eid_stale_4", "https://test.example.com/w45_rejected", "rejected"),
        )

    targets = get_supplier_candidates_for_availability_recheck(stale_days=3, limit=100)
    urls = {t["candidate_url"] for t in targets if t["sku"].startswith(marker)}

    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))

    assert "https://test.example.com/w45_null" in urls
    assert "https://test.example.com/w45_old" in urls
    assert "https://test.example.com/w45_fresh" not in urls
    assert "https://test.example.com/w45_rejected" not in urls


def test_get_candidates_for_recheck_handles_iso_timestamp_boundary():
    """MED-3: attempted_at に ISO 8601 (T + timezone offset) を書き込んでも stale 判定が
    julianday() で正しく効くこと (文字列比較の境界日誤判定回避)。"""
    from monitor.database import init_db, get_conn, get_supplier_candidates_for_availability_recheck
    init_db()
    marker = "w45_iso_boundary_"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))
        # ISO 8601 with 'T' separator + timezone offset = task の record 経路が書く形式
        c.execute(
            """INSERT INTO supplier_candidates
               (sku, ebay_item_id, candidate_url, status, availability_attempted_at)
               VALUES (?,?,?,?, strftime('%Y-%m-%dT%H:%M:%S.000+00:00', 'now','-10 days'))""",
            (f"{marker}iso_old", "eid_iso_1", "https://test.example.com/w45_iso_old", "pending"),
        )
        c.execute(
            """INSERT INTO supplier_candidates
               (sku, ebay_item_id, candidate_url, status, availability_attempted_at)
               VALUES (?,?,?,?, strftime('%Y-%m-%dT%H:%M:%S.000+00:00', 'now','-30 minutes'))""",
            (f"{marker}iso_fresh", "eid_iso_2", "https://test.example.com/w45_iso_fresh", "pending"),
        )

    targets = get_supplier_candidates_for_availability_recheck(stale_days=3, limit=100)
    urls = {t["candidate_url"] for t in targets if t["sku"].startswith(marker)}

    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))

    assert "https://test.example.com/w45_iso_old" in urls
    assert "https://test.example.com/w45_iso_fresh" not in urls


# ---------------------------------------------------------------------------
# run_supplier_availability_recheck: end-to-end (mock 判定)
# ---------------------------------------------------------------------------

def test_run_first_strike_does_not_reject():
    """MED-2: 1 回目の conclusive-unavailable は reject せず availability_pending_reject=1 のみ立てる。"""
    from monitor.database import init_db, get_conn
    init_db()
    url = "https://test.example.com/w45_e2e_first_strike"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (url,))
        c.execute(
            """INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, status)
               VALUES (?,?,?,?)""",
            ("w45_e2e_sku_1", "w45_e2e_eid_1", url, "pending"),
        )
        cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
        return_value={"conclusive": True, "status": "unavailable", "signal": "sold",
                       "checked_at": "2026-07-04T00:00:00+00:00"},
    ):
        from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
        result = run_supplier_availability_recheck({
            "tasks_enabled": {
                "supplier_availability_recheck": {
                    "stale_days": 0, "max_candidates_per_run": 100, "sleep_between_checks_sec": 0,
                }
            }
        })

    with get_conn() as c:
        row = c.execute(
            "SELECT status, auto_rejected, availability_pending_reject, "
            "availability_status, availability_attempted_at "
            "FROM supplier_candidates WHERE id=?", (cid,),
        ).fetchone()
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (cid,))

    assert result["success"] is True
    assert result["first_strike"] == 1
    assert result["rejected"] == 0
    assert row[0] == "pending"  # まだ却下しない
    assert (row[1] or 0) == 0
    assert row[2] == 1  # 1st strike フラグが立った
    # 1st strike では availability_status は 'unavailable' へ書換えない (UI 非表示化を回避)
    assert row[3] != "unavailable"
    assert row[4] is not None  # attempted_at は更新


def test_run_second_strike_rejects_and_clears_pending():
    """MED-2: 1st strike 済みの候補が再度 conclusive-unavailable → 実 reject。"""
    from monitor.database import init_db, get_conn
    init_db()
    url = "https://test.example.com/w45_e2e_second_strike"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (url,))
        c.execute(
            """INSERT INTO supplier_candidates
               (sku, ebay_item_id, candidate_url, status, availability_pending_reject)
               VALUES (?,?,?,?,1)""",
            ("w45_e2e_sku_2b", "w45_e2e_eid_2b", url, "pending"),
        )
        cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
        return_value={"conclusive": True, "status": "unavailable", "signal": "sold again",
                       "checked_at": "2026-07-05T00:00:00+00:00"},
    ):
        from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
        result = run_supplier_availability_recheck({
            "tasks_enabled": {
                "supplier_availability_recheck": {
                    "stale_days": 0, "max_candidates_per_run": 100, "sleep_between_checks_sec": 0,
                }
            }
        })

    with get_conn() as c:
        row = c.execute(
            "SELECT status, auto_rejected, availability_pending_reject, availability_status "
            "FROM supplier_candidates WHERE id=?", (cid,),
        ).fetchone()
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (cid,))

    assert result["success"] is True
    assert result["rejected"] == 1
    assert result["first_strike"] == 0
    assert row[0] == "rejected"
    assert row[1] == 1
    assert row[2] == 0  # 実 reject 後は pending フラグクリア
    assert row[3] == "unavailable"


def test_run_intervening_available_clears_pending_reject():
    """MED-2: 1st strike 済みでも間に conclusive-available が来たら pending_reject=0 に戻る。"""
    from monitor.database import init_db, get_conn
    init_db()
    url = "https://test.example.com/w45_e2e_intervening_avail"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (url,))
        c.execute(
            """INSERT INTO supplier_candidates
               (sku, ebay_item_id, candidate_url, status, availability_pending_reject)
               VALUES (?,?,?,?,1)""",
            ("w45_e2e_sku_avail", "w45_e2e_eid_avail", url, "pending"),
        )
        cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
        return_value={"conclusive": True, "status": "available", "signal": "bid available",
                       "checked_at": "2026-07-05T00:00:00+00:00"},
    ):
        from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
        result = run_supplier_availability_recheck({
            "tasks_enabled": {
                "supplier_availability_recheck": {
                    "stale_days": 0, "max_candidates_per_run": 100, "sleep_between_checks_sec": 0,
                }
            }
        })

    with get_conn() as c:
        row = c.execute(
            "SELECT status, auto_rejected, availability_pending_reject, availability_status "
            "FROM supplier_candidates WHERE id=?", (cid,),
        ).fetchone()
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (cid,))

    assert result["success"] is True
    assert result["rejected"] == 0
    assert row[0] == "pending"
    assert (row[1] or 0) == 0
    assert row[2] == 0  # 1st strike フラグが解除された
    assert row[3] == "available"


def test_run_unknown_advances_attempted_at_avoids_starvation():
    """MED-1: 判定不能 (unknown) でも availability_attempted_at が進み、
    2 回目の run では別の未試行候補が LIMIT の先頭を占める (starvation 防止)。"""
    from monitor.database import init_db, get_conn
    init_db()
    marker = "w45_starvation_"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))
        c.execute(
            """INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, status)
               VALUES (?,?,?,?)""",
            (f"{marker}A", "eid_starve_A", "https://test.example.com/w45_starve_A", "pending"),
        )
        cid_a = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            """INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, status)
               VALUES (?,?,?,?)""",
            (f"{marker}B", "eid_starve_B", "https://test.example.com/w45_starve_B", "pending"),
        )
        cid_b = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck

    # 1 回目: LIMIT=1 で 1 件だけ処理される → 未試行の 2 件のうち id 順で先頭 (=A) が選ばれ、
    # unknown 判定でも attempted_at が進む
    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
        return_value={"conclusive": False, "status": "unknown", "signal": "no signal",
                       "checked_at": "2026-07-04T00:00:00+00:00"},
    ):
        r1 = run_supplier_availability_recheck({
            "tasks_enabled": {
                "supplier_availability_recheck": {
                    "stale_days": 0, "max_candidates_per_run": 1, "sleep_between_checks_sec": 0,
                }
            }
        })
    assert r1["skipped"] == 1

    with get_conn() as c:
        att_a = c.execute("SELECT availability_attempted_at FROM supplier_candidates WHERE id=?", (cid_a,)).fetchone()[0]
        att_b = c.execute("SELECT availability_attempted_at FROM supplier_candidates WHERE id=?", (cid_b,)).fetchone()[0]

    # A は試行済み、B はまだ NULL のはず (starvation なら B に到達できない)
    assert att_a is not None, "A の attempted_at が更新されていない (record_attempt 未実装?)"
    assert att_b is None, "B が想定外に選ばれた"

    # 2 回目: LIMIT=1 で ORDER BY (attempted_at IS NOT NULL, attempted_at ASC) により
    # NULL の B が先に選ばれる = starvation 回避
    from tasks.task_supplier_availability_recheck import _classify_candidate  # re-import to reset patch
    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
        return_value={"conclusive": False, "status": "unknown", "signal": "no signal",
                       "checked_at": "2026-07-04T01:00:00+00:00"},
    ):
        r2 = run_supplier_availability_recheck({
            "tasks_enabled": {
                "supplier_availability_recheck": {
                    "stale_days": 0, "max_candidates_per_run": 1, "sleep_between_checks_sec": 0,
                }
            }
        })
    assert r2["skipped"] == 1

    with get_conn() as c:
        att_b2 = c.execute("SELECT availability_attempted_at FROM supplier_candidates WHERE id=?", (cid_b,)).fetchone()[0]
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))

    assert att_b2 is not None, "2 回目の run で B が選ばれず starvation が再発している"


def test_run_supplier_availability_recheck_leaves_grace_untouched():
    """判定保留 (grace/unknown) は status/availability_status/availability_checked_at を更新しない
    (据え置き)。ただし availability_attempted_at は starvation 防止で更新される。"""
    from monitor.database import init_db, get_conn
    init_db()
    url = "https://test.example.com/w45_e2e_grace"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (url,))
        c.execute(
            """INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, status)
               VALUES (?,?,?,?)""",
            ("w45_e2e_sku_2", "w45_e2e_eid_2", url, "pending"),
        )
        cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
        return_value={"conclusive": False, "status": "grace", "signal": "grace until later",
                       "checked_at": "2026-07-04T00:00:00+00:00"},
    ):
        from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
        result = run_supplier_availability_recheck({
            "tasks_enabled": {
                "supplier_availability_recheck": {
                    "stale_days": 0, "max_candidates_per_run": 100, "sleep_between_checks_sec": 0,
                }
            }
        })

    with get_conn() as c:
        row = c.execute(
            "SELECT status, availability_status, availability_checked_at, availability_attempted_at "
            "FROM supplier_candidates WHERE id=?", (cid,)
        ).fetchone()
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (cid,))

    assert result["success"] is True
    assert result["skipped"] >= 1
    assert row[0] == "pending"
    assert row[1] is None
    assert row[2] is None  # checked_at は据え置き
    assert row[3] is not None  # attempted_at は進む (MED-1)


def test_run_supplier_availability_recheck_no_targets_is_success():
    """max_candidates_per_run=0 (LIMIT 0) で対象ゼロ件を強制し、成功終了することを確認。"""
    from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
    from monitor.database import init_db
    init_db()
    result = run_supplier_availability_recheck({
        "tasks_enabled": {
            "supplier_availability_recheck": {
                "stale_days": 3, "max_candidates_per_run": 0, "sleep_between_checks_sec": 0,
            }
        }
    })
    assert result["success"] is True
    assert result["processed"] == 0


# ---------------------------------------------------------------------------
# MED-2 (code-reviewer 2026-07-04): accepted 反転時のみ通知、pending は通知しない
# ---------------------------------------------------------------------------

def _seed_candidate_for_notify(status: str, url_suffix: str, pending_reject: int = 1) -> int:
    """通知テスト用 seed。pending_reject=1 で 2nd strike スタート (実 reject が走る)。"""
    from monitor.database import init_db, get_conn
    init_db()
    url = f"https://test.example.com/w45_notify_{url_suffix}"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (url,))
        c.execute(
            """INSERT INTO supplier_candidates
               (sku, ebay_item_id, candidate_url, status, candidate_title,
                availability_pending_reject)
               VALUES (?,?,?,?,?,?)""",
            (f"w45_notify_sku_{url_suffix}", f"w45_notify_eid_{url_suffix}",
             url, status, "Test Product Title", pending_reject),
        )
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_run_notifies_when_accepted_flipped_to_rejected():
    """accepted → sold_out 反転時に record_and_maybe_send が action_required/warning で呼ばれる。"""
    from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
    from monitor.database import get_conn
    cid = _seed_candidate_for_notify("accepted", "accepted_flip")

    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
        return_value={"conclusive": True, "status": "unavailable", "signal": "sold",
                       "checked_at": "2026-07-04T00:00:00+00:00"},
    ), patch(
        "notifiers.notification_center.record_and_maybe_send",
        return_value={"notification_id": 1, "discord_sent": True, "gated": False,
                       "deduped": False, "severity_bypassed": False},
    ) as mock_send:
        result = run_supplier_availability_recheck({
            "tasks_enabled": {
                "supplier_availability_recheck": {
                    "stale_days": 0, "max_candidates_per_run": 100,
                    "sleep_between_checks_sec": 0,
                }
            }
        })

    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (cid,))

    assert result["notified_accepted"] == 1
    assert mock_send.called
    call_kwargs = mock_send.call_args.kwargs
    assert call_kwargs["category"] == "action_required"
    assert call_kwargs["severity"] == "warning"
    assert "採用済み" in call_kwargs["title"]
    # dedupe_key に候補 id を含めることで、翌日以降の再走査での再送信を抑止
    assert str(cid) in call_kwargs["dedupe_key"]


def test_run_does_not_notify_when_pending_flipped_to_rejected():
    """pending → sold_out は通知不要 (依頼ボード#39 ノイズ抑止方針)。"""
    from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
    from monitor.database import get_conn
    cid = _seed_candidate_for_notify("pending", "pending_flip")

    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
        return_value={"conclusive": True, "status": "unavailable", "signal": "sold",
                       "checked_at": "2026-07-04T00:00:00+00:00"},
    ), patch(
        "notifiers.notification_center.record_and_maybe_send",
        return_value={"notification_id": 1, "discord_sent": True, "gated": False,
                       "deduped": False, "severity_bypassed": False},
    ) as mock_send:
        result = run_supplier_availability_recheck({
            "tasks_enabled": {
                "supplier_availability_recheck": {
                    "stale_days": 0, "max_candidates_per_run": 100,
                    "sleep_between_checks_sec": 0,
                }
            }
        })

    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (cid,))

    assert result["notified_accepted"] == 0
    assert result["rejected"] >= 1  # DB 却下は行われている
    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# MED-1 (code-reviewer 2026-07-04): 高保留比率で warning + summary flag
# ---------------------------------------------------------------------------

def test_run_flags_high_unknown_ratio_when_over_threshold(caplog):
    """processed>=20 かつ skipped/processed>0.8 → high_unknown_ratio=True + warning log."""
    from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
    from monitor.database import init_db, get_conn
    init_db()

    marker = "w45_high_ratio_"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))
        for i in range(25):
            c.execute(
                """INSERT INTO supplier_candidates
                   (sku, ebay_item_id, candidate_url, status)
                   VALUES (?,?,?,?)""",
                (f"{marker}{i}", f"eid_high_ratio_{i}",
                 f"https://test.example.com/w45_high_ratio_{i}", "pending"),
            )

    # 全件を「保留 (unknown)」判定させる
    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
        return_value={"conclusive": False, "status": "unknown", "signal": "no signal",
                       "checked_at": "2026-07-04T00:00:00+00:00"},
    ):
        with caplog.at_level(logging.WARNING, logger="tasks.task_supplier_availability_recheck"):
            result = run_supplier_availability_recheck({
                "tasks_enabled": {
                    "supplier_availability_recheck": {
                        "stale_days": 0, "max_candidates_per_run": 100,
                        "sleep_between_checks_sec": 0,
                    }
                }
            })

    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))

    assert result["processed"] >= 20
    assert result["skipped"] >= 20
    assert result["high_unknown_ratio"] is True
    assert "高保留比率" in result["message"] or "[WARN" in result["message"]
    # サイト構造変化検知の warning log が出ていること
    assert any(
        "判定保留比率が高い" in rec.getMessage()
        for rec in caplog.records
    )


def test_run_clamps_negative_max_candidates_to_zero():
    """LOW-4 (Codex): max_candidates_per_run=-1 (LIMIT -1 = 無制限) を 0 に clamp。"""
    from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
    from monitor.database import init_db, get_conn
    init_db()
    marker = "w45_clamp_neg_"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))
        for i in range(3):
            c.execute(
                """INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, status)
                   VALUES (?,?,?,?)""",
                (f"{marker}{i}", f"eid_clamp_{i}",
                 f"https://test.example.com/w45_clamp_neg_{i}", "pending"),
            )

    # 判定関数が呼ばれてしまうと clamp が失敗している証拠
    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
    ) as mock_classify:
        result = run_supplier_availability_recheck({
            "tasks_enabled": {
                "supplier_availability_recheck": {
                    "stale_days": 0, "max_candidates_per_run": -1,
                    "sleep_between_checks_sec": 0,
                }
            }
        })

    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))

    assert result["success"] is True
    assert result["processed"] == 0
    mock_classify.assert_not_called()


def test_run_does_not_flag_high_unknown_ratio_when_processed_too_small():
    """processed<20 (少数サンプル) では ratio が高くても flag しない (false positive 防止)。"""
    from tasks.task_supplier_availability_recheck import run_supplier_availability_recheck
    from monitor.database import init_db, get_conn
    init_db()

    marker = "w45_small_sample_"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))
        for i in range(5):  # 5 件のみ (min=20 未満)
            c.execute(
                """INSERT INTO supplier_candidates
                   (sku, ebay_item_id, candidate_url, status)
                   VALUES (?,?,?,?)""",
                (f"{marker}{i}", f"eid_small_{i}",
                 f"https://test.example.com/w45_small_sample_{i}", "pending"),
            )

    with patch(
        "tasks.task_supplier_availability_recheck._classify_candidate",
        return_value={"conclusive": False, "status": "unknown", "signal": "no signal",
                       "checked_at": "2026-07-04T00:00:00+00:00"},
    ):
        result = run_supplier_availability_recheck({
            "tasks_enabled": {
                "supplier_availability_recheck": {
                    "stale_days": 0, "max_candidates_per_run": 100,
                    "sleep_between_checks_sec": 0,
                }
            }
        })

    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE sku LIKE ?", (f"{marker}%",))

    assert result["high_unknown_ratio"] is False
