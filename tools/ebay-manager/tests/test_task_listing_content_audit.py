"""#44 Wave2 (2026-07-04): tasks/task_listing_content_audit.py の単体テスト.

audit_one は純関数 (ネットワーク非依存)。select_audit_targets は tmp DB
(conftest._isolate_monitor_db で隔離済) に対する SELECT のみ。
run_listing_content_audit は GetItem/Discord を monkeypatch で mock する
(eBay 実 API は叩かない)。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _snap(**kwargs):
    """ListingSnapshot 相当の SimpleNamespace (audit_one は duck-typing で読む)."""
    base = {
        "ok": True, "error": None, "title": None, "condition_id": None,
        "condition_description": None, "item_specifics": {}, "picture_count": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


# ── audit_one (純関数) ──────────────────────────────────────────────


def test_audit_one_fetch_error():
    from tasks.task_listing_content_audit import audit_one
    snap = _snap(ok=False, error="通信エラー: boom")
    issues = audit_one({"ebay_item_id": "1", "title": "X", "rank": "A"}, snap)
    assert len(issues) == 1
    assert issues[0]["kind"] == "fetch_error"
    assert "boom" in issues[0]["detail"]


def test_audit_one_no_issues_when_all_match():
    from tasks.task_listing_content_audit import audit_one
    snap = _snap(
        title="Sony WH-1000XM5", condition_id="3000",
        condition_description="Tested and fully working (2026-07).",
        item_specifics={"Brand": ["Sony"]}, picture_count=3,
    )
    db_row = {"ebay_item_id": "1", "title": "Sony WH-1000XM5", "rank": "A"}
    assert audit_one(db_row, snap) == []


def test_audit_one_title_mismatch():
    from tasks.task_listing_content_audit import audit_one
    snap = _snap(title="Totally Different Product", condition_id="3000",
                 picture_count=1)
    db_row = {"ebay_item_id": "1", "title": "Sony WH-1000XM5", "rank": "A"}
    issues = audit_one(db_row, snap)
    kinds = [i["kind"] for i in issues]
    assert "title_mismatch" in kinds


def test_audit_one_condition_mismatch():
    from tasks.task_listing_content_audit import audit_one
    # db rank='N' expects condition_id 1000, snapshot に 3000 が返っている
    snap = _snap(condition_id="3000", picture_count=1)
    db_row = {"ebay_item_id": "1", "title": "", "rank": "N"}
    issues = audit_one(db_row, snap)
    hits = [i for i in issues if i["kind"] == "condition_mismatch"]
    assert len(hits) == 1
    assert "1000" in hits[0]["detail"] and "3000" in hits[0]["detail"]


def test_audit_one_condition_bucket_same_rank_group_no_mismatch():
    """A/B/C/D/PO は同一 condition_id=3000 バケットなので、rank が違っても
    condition_id が一致していれば mismatch と判定しない (8段階中のバケット化仕様)."""
    from tasks.task_listing_content_audit import audit_one
    snap = _snap(condition_id="3000", picture_count=1)
    db_row = {"ebay_item_id": "1", "title": "", "rank": "C"}
    issues = audit_one(db_row, snap)
    assert all(i["kind"] != "condition_mismatch" for i in issues)


def test_audit_one_condition_description_stale_rank():
    from tasks.task_listing_content_audit import audit_one
    snap = _snap(
        condition_id="7000",
        condition_description="Rank As-Is — heavy wear, no testing possible",
        picture_count=1,
    )
    db_row = {"ebay_item_id": "1", "title": "", "rank": "A"}
    issues = audit_one(db_row, snap)
    hits = [i for i in issues if i["kind"] == "condition_description_stale_rank"]
    assert len(hits) == 1
    assert "As-Is" in hits[0]["detail"]


def test_audit_one_condition_description_matching_rank_no_flag():
    from tasks.task_listing_content_audit import audit_one
    snap = _snap(
        condition_id="3000",
        condition_description="Rank A — tested and working",
        picture_count=1,
    )
    db_row = {"ebay_item_id": "1", "title": "", "rank": "A"}
    issues = audit_one(db_row, snap)
    assert all(i["kind"] != "condition_description_stale_rank" for i in issues)


def test_audit_one_prohibited_item_specifics():
    from tasks.task_listing_content_audit import audit_one
    snap = _snap(
        condition_id="3000",
        item_specifics={"Brand": ["Sony"], "Country of Origin": ["Japan"]},
        picture_count=1,
    )
    db_row = {"ebay_item_id": "1", "title": "", "rank": "A"}
    issues = audit_one(db_row, snap)
    hits = [i for i in issues if i["kind"] == "prohibited_item_specifics"]
    assert len(hits) == 1
    assert "Country of Origin" in hits[0]["detail"]
    assert "Brand" not in hits[0]["detail"]


def test_audit_one_prohibited_item_specifics_case_insensitive():
    """monitor.ebay_client._is_forbidden_specific_name は大文字小文字を無視する
    単一ソース定義 — 監査側もそれをそのまま再利用していることを確認."""
    from tasks.task_listing_content_audit import audit_one
    snap = _snap(
        condition_id="3000",
        item_specifics={"country of origin": ["China"]},
        picture_count=1,
    )
    db_row = {"ebay_item_id": "1", "title": "", "rank": "A"}
    issues = audit_one(db_row, snap)
    assert any(i["kind"] == "prohibited_item_specifics" for i in issues)


def test_audit_one_no_images():
    from tasks.task_listing_content_audit import audit_one
    snap = _snap(condition_id="3000", picture_count=0)
    db_row = {"ebay_item_id": "1", "title": "", "rank": "A"}
    issues = audit_one(db_row, snap)
    assert any(i["kind"] == "no_images" for i in issues)


def test_audit_one_multiple_issues_all_reported():
    from tasks.task_listing_content_audit import audit_one
    snap = _snap(
        title="Different Title", condition_id="7000",
        item_specifics={"Manufacturer": ["Acme Corp"]},
        picture_count=0,
    )
    db_row = {"ebay_item_id": "1", "title": "Original Title", "rank": "N"}
    issues = audit_one(db_row, snap)
    kinds = {i["kind"] for i in issues}
    assert {"title_mismatch", "condition_mismatch",
            "prohibited_item_specifics", "no_images"} <= kinds


# ── select_audit_targets (DB SELECT のみ、tmp DB 隔離済) ──────────────


def _seed_listing(conn, eid, title="T", rank="A", is_ended=0):
    conn.execute(
        "INSERT INTO ebay_listings (ebay_item_id, sku, title, rank, is_ended) "
        "VALUES (?, ?, ?, ?, ?)",
        (eid, f"stock:{eid}", title, rank, is_ended),
    )


def _seed_applied_candidate(conn, eid, days_ago=1):
    conn.execute(
        "INSERT INTO supplier_candidates "
        "(sku, ebay_item_id, candidate_url, status, user_action_at) "
        "VALUES (?, ?, ?, 'applied', datetime('now', ?))",
        (f"ebayyh_p{eid}", eid, f"https://example.com/{eid}", f"-{days_ago} days"),
    )


def test_select_audit_targets_recent_applied_picked_up():
    from monitor.database import get_conn, init_db
    from tasks.task_listing_content_audit import select_audit_targets

    init_db()
    with get_conn() as conn:
        _seed_listing(conn, "100")
        _seed_applied_candidate(conn, "100", days_ago=1)

    targets = select_audit_targets(max_total=50, random_n=0, recent_days=7)
    assert len(targets) == 1
    assert targets[0]["ebay_item_id"] == "100"
    assert targets[0]["source"] == "recent_applied"


def test_select_audit_targets_excludes_old_applied():
    from monitor.database import get_conn, init_db
    from tasks.task_listing_content_audit import select_audit_targets

    init_db()
    with get_conn() as conn:
        _seed_listing(conn, "200")
        _seed_applied_candidate(conn, "200", days_ago=30)  # 直近7日の外

    targets = select_audit_targets(max_total=50, random_n=0, recent_days=7)
    assert targets == []


def test_select_audit_targets_excludes_ended_listing():
    from monitor.database import get_conn, init_db
    from tasks.task_listing_content_audit import select_audit_targets

    init_db()
    with get_conn() as conn:
        _seed_listing(conn, "300", is_ended=1)
        _seed_applied_candidate(conn, "300", days_ago=1)

    targets = select_audit_targets(max_total=50, random_n=0, recent_days=7)
    assert targets == []


def test_select_audit_targets_random_fills_remaining_slots():
    from monitor.database import get_conn, init_db
    from tasks.task_listing_content_audit import select_audit_targets

    init_db()
    with get_conn() as conn:
        for i in range(5):
            _seed_listing(conn, str(400 + i))

    targets = select_audit_targets(max_total=50, random_n=3, recent_days=7)
    assert len(targets) == 3
    assert all(t["source"] == "random" for t in targets)


def test_select_audit_targets_dedup_recent_and_random():
    """recent_applied で既に選ばれた listing はランダム枠から重複選出されない."""
    from monitor.database import get_conn, init_db
    from tasks.task_listing_content_audit import select_audit_targets

    init_db()
    with get_conn() as conn:
        for i in range(3):
            _seed_listing(conn, str(500 + i))
        _seed_applied_candidate(conn, "500", days_ago=1)

    targets = select_audit_targets(max_total=50, random_n=10, recent_days=7)
    ids = [t["ebay_item_id"] for t in targets]
    assert len(ids) == len(set(ids))  # 重複なし
    assert "500" in ids
    assert len(targets) == 3  # 500(recent) + 501,502(random) = 3 (プール上限)


def test_select_audit_targets_caps_at_max_total():
    """上限50件/日: recent_applied だけで50件超あっても max_total で切り詰める."""
    from monitor.database import get_conn, init_db
    from tasks.task_listing_content_audit import select_audit_targets

    init_db()
    with get_conn() as conn:
        for i in range(70):
            eid = str(600 + i)
            _seed_listing(conn, eid)
            _seed_applied_candidate(conn, eid, days_ago=1)

    targets = select_audit_targets(max_total=50, random_n=20, recent_days=7)
    assert len(targets) == 50


def test_select_audit_targets_zero_max_total():
    from monitor.database import init_db
    from tasks.task_listing_content_audit import select_audit_targets

    init_db()
    assert select_audit_targets(max_total=0) == []


# ── run_listing_content_audit (kill switch / 通知 / cap 統合) ──────────


def test_run_listing_content_audit_kill_switch():
    from tasks.task_listing_content_audit import run_listing_content_audit
    result = run_listing_content_audit(
        {"tasks_enabled": {"listing_content_audit": {"enabled": False}}}
    )
    assert result["success"] is True
    assert "enabled=false" in result["message"]


def test_run_listing_content_audit_no_targets():
    from monitor.database import init_db
    from tasks.task_listing_content_audit import run_listing_content_audit
    init_db()
    result = run_listing_content_audit({})
    assert result["success"] is True
    assert result["targets"] == 0


def test_run_listing_content_audit_missing_credentials():
    from monitor.database import get_conn, init_db
    from tasks.task_listing_content_audit import run_listing_content_audit
    init_db()
    with get_conn() as conn:
        _seed_listing(conn, "700")
        _seed_applied_candidate(conn, "700", days_ago=1)

    with patch(
        "monitor.credentials.get_ebay_credentials",
        return_value={"app_id": "", "dev_id": "", "cert_id": "", "user_token": ""},
    ):
        result = run_listing_content_audit({})
    assert result["success"] is False
    assert "認証情報" in result["message"]


def test_run_listing_content_audit_end_to_end_issue_detected_and_notified():
    """GetItem/Discord を mock し、issue 検出時に severity='error' で通知されることを確認."""
    from monitor.database import get_conn, init_db
    from tasks.task_listing_content_audit import run_listing_content_audit
    init_db()
    with get_conn() as conn:
        _seed_listing(conn, "800", title="Original Title", rank="A")
        _seed_applied_candidate(conn, "800", days_ago=1)

    bad_snap = _snap(
        title="Different Title!", condition_id="3000",
        item_specifics={"Country of Origin": ["Japan"]}, picture_count=2,
    )

    notify_calls = []

    def _fake_notify(category, severity, title, body, **kwargs):
        notify_calls.append((category, severity, title, body))
        return {"notification_id": 1, "discord_sent": True}

    with patch(
        "monitor.credentials.get_ebay_credentials",
        return_value={"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "T"},
    ), patch(
        "monitor.ebay_listing_snapshot.fetch_listing_snapshot",
        return_value=bad_snap,
    ), patch(
        "notifiers.notification_center.record_and_maybe_send",
        side_effect=_fake_notify,
    ):
        result = run_listing_content_audit({})

    assert result["success"] is True
    assert result["targets"] == 1
    assert result["checked"] == 1
    assert result["issues_found"] == 1
    assert len(notify_calls) == 1
    category, severity, title, body = notify_calls[0]
    assert category == "system"
    assert severity == "error"
    assert "800" in body


def test_run_listing_content_audit_no_issues_notifies_info():
    from monitor.database import get_conn, init_db
    from tasks.task_listing_content_audit import run_listing_content_audit
    init_db()
    with get_conn() as conn:
        _seed_listing(conn, "900", title="Match", rank="A")
        _seed_applied_candidate(conn, "900", days_ago=1)

    good_snap = _snap(
        title="Match", condition_id="3000", item_specifics={"Brand": ["X"]},
        picture_count=1,
    )

    notify_calls = []

    def _fake_notify(category, severity, title, body, **kwargs):
        notify_calls.append((category, severity))
        return {"notification_id": 1, "discord_sent": False}

    with patch(
        "monitor.credentials.get_ebay_credentials",
        return_value={"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "T"},
    ), patch(
        "monitor.ebay_listing_snapshot.fetch_listing_snapshot",
        return_value=good_snap,
    ), patch(
        "notifiers.notification_center.record_and_maybe_send",
        side_effect=_fake_notify,
    ):
        result = run_listing_content_audit({})

    assert result["issues_found"] == 0
    assert notify_calls == [("system", "info")]


def test_run_listing_content_audit_max_50_cap_enforced_end_to_end():
    """上限50件/日: 70件対象があっても GetItem 呼出は 50 回に抑えられる."""
    from monitor.database import get_conn, init_db
    from tasks.task_listing_content_audit import run_listing_content_audit
    init_db()
    with get_conn() as conn:
        for i in range(70):
            eid = str(1000 + i)
            _seed_listing(conn, eid)
            _seed_applied_candidate(conn, eid, days_ago=1)

    call_count = {"n": 0}

    def _fake_fetch(*args, **kwargs):
        call_count["n"] += 1
        return _snap(title="X", condition_id="3000", picture_count=1)

    with patch(
        "monitor.credentials.get_ebay_credentials",
        return_value={"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "T"},
    ), patch(
        "monitor.ebay_listing_snapshot.fetch_listing_snapshot",
        side_effect=_fake_fetch,
    ), patch(
        "notifiers.notification_center.record_and_maybe_send",
        return_value={"notification_id": 1, "discord_sent": False},
    ):
        result = run_listing_content_audit({})

    assert call_count["n"] == 50
    assert result["targets"] == 50
    assert result["checked"] == 50
