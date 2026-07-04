# -*- coding: utf-8 -*-
"""W284 Phase 2 code-reviewer HIGH 修正の回帰テスト (2026-06-20).

H1: failed job が backoff(next_attempt_at)を持ち即再試行されない + 上限で needs_manual。
H3: discover 成功直後に fetch 失敗しても空 states を ebaymag_products に永続化しない。

conftest.py の DB 隔離を利用 (本番 DB 非使用)。
"""
from __future__ import annotations

import json


def _seed(eid: str, title: str, desired: list) -> int:
    from monitor.database import init_db, get_conn, enqueue_ebaymag_apply
    init_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ebay_listings (ebay_item_id, title, sku, "
            "ebaymag_segment, ebaymag_desired_sites_json, ebaymag_desired_updated_at) "
            "VALUES (?, ?, 'stock:01', '全国', ?, CURRENT_TIMESTAMP)",
            (eid, title, json.dumps(desired)),
        )
    enqueue_ebaymag_apply(eid, "new_listing")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM ebaymag_apply_queue WHERE ebay_item_id=? AND status='pending'",
            (eid,),
        ).fetchone()
    return row["id"]


def _job(job_id: int) -> dict:
    from monitor.database import get_conn
    with get_conn() as conn:
        return dict(conn.execute(
            "SELECT * FROM ebaymag_apply_queue WHERE id=?", (job_id,)).fetchone())


def _fake(ok: bool, *, error: str = "", site_states=None, product_id=None):
    from monitor.ebaymag_driver import EbaymagResult
    r = EbaymagResult()
    r.ok = ok
    r.error = error
    r.site_states = site_states if site_states is not None else {}
    r.product_id = product_id
    return r


def test_failed_job_gets_backoff_not_immediate_retry(monkeypatch):
    """H1: fetch失敗で failed → next_attempt_at(未来)が付き、即再取得されない。"""
    from tasks.task_ebaymag_apply_queue import _process_job
    from monitor.database import upsert_ebaymag_product, get_active_ebaymag_apply_jobs
    jid = _seed("EID_F1", "Test Widget", ["UK", "DE"])
    upsert_ebaymag_product("EID_F1", product_id="P1", site_states={"UK": True})
    monkeypatch.setattr("monitor.ebaymag_driver.fetch_site_states",
                        lambda pid, expected_itm: _fake(False, error="fetch boom"))
    res = _process_job(_job(jid), {})
    assert res["result"] == "failed"
    row = _job(jid)
    assert row["status"] == "failed"
    assert row["next_attempt_at"] is not None, "failed に backoff(next_attempt_at)が無い"
    active = [j for j in get_active_ebaymag_apply_jobs() if j["ebay_item_id"] == "EID_F1"]
    assert active == [], "failed job が backoff 中なのに即再取得された (無限リトライ)"


def test_failed_reaches_max_then_needs_manual(monkeypatch):
    """H1: failed が上限到達で needs_manual + Discord 通知。"""
    from tasks.task_ebaymag_apply_queue import _process_job, _FAILED_MAX_ATTEMPTS
    from monitor.database import upsert_ebaymag_product, get_conn
    jid = _seed("EID_F2", "Test Widget2", ["UK"])
    upsert_ebaymag_product("EID_F2", product_id="P2", site_states={"UK": True})
    with get_conn() as c:
        c.execute("UPDATE ebaymag_apply_queue SET attempts=? WHERE id=?",
                  (_FAILED_MAX_ATTEMPTS - 1, jid))
    monkeypatch.setattr("monitor.ebaymag_driver.fetch_site_states",
                        lambda pid, expected_itm: _fake(False, error="boom"))
    notified: list = []
    monkeypatch.setattr("tasks.task_ebaymag_apply_queue._discord_notify",
                        lambda cfg, msg, **_kw: notified.append(msg))
    _process_job(_job(jid), {})
    assert _job(jid)["status"] == "needs_manual"
    assert any("手動対応" in m for m in notified), "上限到達で Discord 通知が出ていない"


def test_discover_ok_then_fetch_fail_does_not_persist_empty_states(monkeypatch):
    """H3: discover成功→fetch失敗時、ebaymag_products.site_states が {} で残らない。"""
    from tasks.task_ebaymag_apply_queue import _process_job
    from monitor.database import get_conn
    jid = _seed("EID_H3", "Widget H3", ["UK"])
    monkeypatch.setattr("monitor.ebaymag_driver.discover_product_id",
                        lambda q, itm: _fake(True, product_id="PD3"))
    monkeypatch.setattr("monitor.ebaymag_driver.fetch_site_states",
                        lambda pid, expected_itm: _fake(False, error="fetch fail"))
    _process_job(_job(jid), {})
    with get_conn() as c:
        row = c.execute(
            "SELECT site_states_json FROM ebaymag_products WHERE ebay_item_id='EID_H3'"
        ).fetchone()
    assert row is not None, "discover で product_id が登録されていない"
    assert row["site_states_json"] is None, "discover 直後に空 states が永続化された (H3 退行)"
