#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W223 step3: 仕入先候補 AI 評価台帳 (supplier_candidate_evaluations) のテスト.

検証:
  - migration v64 でテーブルが増える + 冪等
  - record_candidate_evaluation / get_recent_candidate_evaluation (窓内/窓外/upsert)
  - realtime run_supplier_candidate_search:
      * 既評価 (同 eid+正規化URL, 30日内, 同title) は AI 呼出をスキップし過去判定を再利用
      * 新規候補は却下含め台帳に記録される
      * API エラー評価は台帳に記録しない (一時失敗を 30 日固定しない)
      * title 変更時は再評価 (reuse しない)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ─── DB 層 ───

def test_migration_v64_table_and_idempotent():
    from monitor.database import init_db, get_conn
    init_db()
    with get_conn() as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert "supplier_candidate_evaluations" in tables
    assert ver >= 64
    # 冪等: 記録後に init_db 再実行してもデータ保持
    from monitor.database import record_candidate_evaluation, get_recent_candidate_evaluation
    record_candidate_evaluation("eid_v64", "example.com/x", match_score=77)
    init_db()
    assert get_recent_candidate_evaluation("eid_v64", "example.com/x") is not None


def test_record_and_get_recent_within_window():
    from monitor.database import (
        init_db, record_candidate_evaluation, get_recent_candidate_evaluation,
    )
    init_db()
    record_candidate_evaluation(
        "100", "mercari.com/item/a", source_platform="mercari",
        candidate_title="T", candidate_price_jpy=5000, match_score=82,
        match_reasoning="ok", eval_model="claude-sonnet-4-6",
    )
    got = get_recent_candidate_evaluation("100", "mercari.com/item/a", within_days=30)
    assert got is not None
    assert got["match_score"] == 82
    assert got["candidate_title"] == "T"


def test_get_recent_outside_window_returns_none():
    from monitor.database import init_db, get_conn, get_recent_candidate_evaluation
    init_db()
    # 40 日前の評価を直接 INSERT (窓外)
    with get_conn() as c:
        c.execute(
            "INSERT INTO supplier_candidate_evaluations "
            "(ebay_item_id, candidate_url, match_score, evaluated_at) "
            "VALUES (?,?,?, datetime('now','-40 days'))",
            ("200", "mercari.com/item/old", 90),
        )
    assert get_recent_candidate_evaluation("200", "mercari.com/item/old", within_days=30) is None


def test_record_upsert_updates_score():
    from monitor.database import (
        init_db, record_candidate_evaluation, get_recent_candidate_evaluation,
    )
    init_db()
    record_candidate_evaluation("300", "u/1", match_score=10)
    record_candidate_evaluation("300", "u/1", match_score=88, match_reasoning="re")
    got = get_recent_candidate_evaluation("300", "u/1")
    assert got["match_score"] == 88
    assert got["match_reasoning"] == "re"
    # UNIQUE(eid,url) なので 1 行のみ
    from monitor.database import get_conn
    with get_conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM supplier_candidate_evaluations "
            "WHERE ebay_item_id='300' AND candidate_url='u/1'").fetchone()[0]
    assert n == 1


def test_record_requires_ids():
    from monitor.database import init_db, record_candidate_evaluation
    init_db()
    with pytest.raises(ValueError):
        record_candidate_evaluation("", "u/1", match_score=1)
    with pytest.raises(ValueError):
        record_candidate_evaluation("eid", "", match_score=1)


# ─── realtime 経路 ───

class _Hit:
    def __init__(self, url, title="Sony X", price=5000):
        self.source_platform = "mercari"
        self.url = url
        self.price_jpy = price
        self.title = title
        self.image_url = None


def _setup_realtime(monkeypatch, t, listing, hits, eval_calls):
    monkeypatch.setattr(t, "get_ebay_listing_by_item_id", lambda eid: listing)
    monkeypatch.setattr(t, "load_settings", lambda: {})
    monkeypatch.setattr(
        t, "check_candidate_availability",
        lambda url, **_kw: {"status": "available", "signal": "mock",
                            "checked_at": "2026-06-05T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        t, "search_candidates_on_platform",
        lambda plat, kw, max_results=5: hits if plat == "mercari" else [],
    )

    def _fake_eval(h, ebay_title, ebay_image_url=None, sku=None, ebay_item_id=None, **_kw):
        eval_calls.append(h.url)
        return t.ScoredCandidate(hit=h, match_score=70, match_reasoning="freshly judged")
    monkeypatch.setattr(t, "evaluate_candidate_with_claude", _fake_eval)
    monkeypatch.setattr(t, "_estimate_profit_for_candidate", lambda **kw: 5000.0)
    monkeypatch.setattr(
        t, "check_supplier_candidate_profitable",
        lambda profit_with_refund, purchase_yen: (True, {}),
    )
    saved = []
    monkeypatch.setattr(t, "add_supplier_candidate", lambda **kw: saved.append(kw) or 1)
    return saved


def test_realtime_reuses_prior_evaluation_skips_ai(monkeypatch):
    from monitor.database import init_db, record_candidate_evaluation
    from tasks import task_supplier_candidate_search as t
    init_db()
    listing = {"sku": "ebayme_x", "title": "Sony X", "current_price": 100.0,
               "ebay_item_id": "900", "source_url": None}
    url = "https://jp.mercari.com/item/m111"
    norm = t._normalize_url(url)
    # 事前に台帳へ過去 AI 判定 (score=85, 同 title)
    record_candidate_evaluation("900", norm, candidate_title="cand title",
                                match_score=85, match_reasoning="prior judged")
    hit = _Hit(url, title="cand title")
    eval_calls = []
    saved = _setup_realtime(monkeypatch, t, listing, [hit], eval_calls)

    r = t.run_supplier_candidate_search(
        ebay_item_id="900", sku="ebayme_x", config={},
        platforms=["mercari"], discovered_via="test",
    )
    assert r["reused_eval"] == 1
    assert eval_calls == [], "既評価なので AI 呼出されない"
    assert len(saved) == 1
    assert saved[0]["match_score"] == 85, "過去 score を再利用"
    assert saved[0]["match_reasoning"] == "prior judged"


def test_realtime_new_candidate_recorded_to_ledger(monkeypatch):
    from monitor.database import init_db, get_recent_candidate_evaluation
    from tasks import task_supplier_candidate_search as t
    init_db()
    listing = {"sku": "ebayme_y", "title": "Sony Y", "current_price": 100.0,
               "ebay_item_id": "901", "source_url": None}
    url = "https://jp.mercari.com/item/m222"
    hit = _Hit(url, title="new cand")
    eval_calls = []
    _setup_realtime(monkeypatch, t, listing, [hit], eval_calls)

    r = t.run_supplier_candidate_search(
        ebay_item_id="901", sku="ebayme_y", config={},
        platforms=["mercari"], discovered_via="test",
    )
    assert eval_calls == [url], "新規候補は AI 評価される"
    assert r["reused_eval"] == 0
    # 台帳に記録された
    led = get_recent_candidate_evaluation("901", t._normalize_url(url))
    assert led is not None and led["match_score"] == 70


def test_realtime_error_eval_not_recorded(monkeypatch):
    from monitor.database import init_db, get_recent_candidate_evaluation
    from tasks import task_supplier_candidate_search as t
    init_db()
    listing = {"sku": "ebayme_z", "title": "Sony Z", "current_price": 100.0,
               "ebay_item_id": "902", "source_url": None}
    url = "https://jp.mercari.com/item/m333"
    hit = _Hit(url, title="err cand")

    def _err_eval(h, ebay_title, ebay_image_url=None, sku=None, ebay_item_id=None, **_kw):
        return t.ScoredCandidate(hit=h, match_score=0, match_reasoning="API error",
                                 eval_error=True)
    monkeypatch.setattr(t, "get_ebay_listing_by_item_id", lambda eid: listing)
    monkeypatch.setattr(t, "load_settings", lambda: {})
    monkeypatch.setattr(t, "check_candidate_availability",
                        lambda url, **_kw: {"status": "available", "signal": "m"})
    monkeypatch.setattr(t, "search_candidates_on_platform",
                        lambda plat, kw, max_results=5: [hit] if plat == "mercari" else [])
    monkeypatch.setattr(t, "evaluate_candidate_with_claude", _err_eval)
    monkeypatch.setattr(t, "_estimate_profit_for_candidate", lambda **kw: 5000.0)
    monkeypatch.setattr(t, "check_supplier_candidate_profitable",
                        lambda profit_with_refund, purchase_yen: (True, {}))
    monkeypatch.setattr(t, "add_supplier_candidate", lambda **kw: 1)

    t.run_supplier_candidate_search(
        ebay_item_id="902", sku="ebayme_z", config={},
        platforms=["mercari"], discovered_via="test",
    )
    assert get_recent_candidate_evaluation("902", t._normalize_url(url)) is None, (
        "API エラー評価は台帳に記録しない (再評価させる)"
    )


def test_realtime_title_change_triggers_reeval(monkeypatch):
    from monitor.database import init_db, record_candidate_evaluation
    from tasks import task_supplier_candidate_search as t
    init_db()
    listing = {"sku": "ebayme_w", "title": "Sony W", "current_price": 100.0,
               "ebay_item_id": "903", "source_url": None}
    url = "https://jp.mercari.com/item/m444"
    norm = t._normalize_url(url)
    record_candidate_evaluation("903", norm, candidate_title="OLD title",
                                match_score=85)
    hit = _Hit(url, title="NEW title")  # title 変更
    eval_calls = []
    _setup_realtime(monkeypatch, t, listing, [hit], eval_calls)

    r = t.run_supplier_candidate_search(
        ebay_item_id="903", sku="ebayme_w", config={},
        platforms=["mercari"], discovered_via="test",
    )
    assert r["reused_eval"] == 0, "title が変われば再評価"
    assert eval_calls == [url]


# ─── batch (sweep) 経路 ───

def test_batch_sweep_all_reused_skips_batch_submit(monkeypatch):
    """全候補が既評価なら evaluate_batch を呼ばず reused を persist、success=True。"""
    from monitor.database import init_db, record_candidate_evaluation
    from tasks import task_supplier_sweep as s
    import monitor.supplier_batch_evaluator as sbe
    init_db()

    listing = {"sku": "ebayme_b", "title": "Item B", "current_price": 100.0,
               "ebay_item_id": "950", "source_url": None}
    url = "https://jp.mercari.com/item/m950"
    norm = s._normalize_url(url)
    record_candidate_evaluation("950", norm, candidate_title="cand B", match_score=88,
                                match_reasoning="prior")
    hit = _Hit(url, title="cand B")

    monkeypatch.setattr(s, "_fetch_sweep_targets", lambda *a, **k: [("950", "ebayme_b")])
    monkeypatch.setattr(s, "get_ebay_listing_by_item_id", lambda eid: listing)
    monkeypatch.setattr(s, "get_ebay_image_url", lambda eid: None)
    monkeypatch.setattr(s, "search_candidates_on_platform",
                        lambda plat, kw, max_results=5: [hit] if plat == "mercari" else [])
    monkeypatch.setattr(s, "check_candidate_availability",
                        lambda url, **_k: {"status": "available", "signal": "m",
                                           "checked_at": "2026-06-05T00:00:00+00:00"})
    monkeypatch.setattr(s, "_estimate_profit_for_candidate", lambda **k: 5000.0)
    monkeypatch.setattr(s, "check_supplier_candidate_profitable",
                        lambda profit_with_refund, purchase_yen: (True, {}))
    monkeypatch.setattr(s, "load_settings", lambda: {})
    saved = []
    monkeypatch.setattr(s, "add_supplier_candidate", lambda **kw: saved.append(kw) or 1)

    def _boom(*a, **k):
        raise AssertionError("全件 reused なのに evaluate_batch を呼んだ")
    monkeypatch.setattr(sbe, "evaluate_batch", _boom)

    cfg = {"tasks_enabled": {"supplier_sweep": {"use_batch_api": True,
                                                "min_batch_size": 1}}}
    r = s.run_supplier_sweep_batch(cfg)

    assert r["reused_eval"] == 1
    assert r["success"] is True
    assert len(saved) == 1
    assert saved[0]["match_score"] == 88, "過去 score 再利用"
    assert saved[0]["eval_model"].endswith("-reused"), "reused ラベルで区別"


def test_batch_sweep_new_candidate_recorded(monkeypatch):
    """batch 経路で新規評価された候補が却下含め台帳に記録される。"""
    from monitor.database import init_db, get_recent_candidate_evaluation
    from tasks import task_supplier_sweep as s
    import monitor.supplier_batch_evaluator as sbe
    from monitor.claude_evaluator import EvaluationResult
    init_db()

    listing = {"sku": "ebayme_c", "title": "Item C", "current_price": 100.0,
               "ebay_item_id": "951", "source_url": None}
    url = "https://jp.mercari.com/item/m951"
    hit = _Hit(url, title="cand C")

    monkeypatch.setattr(s, "_fetch_sweep_targets", lambda *a, **k: [("951", "ebayme_c")])
    monkeypatch.setattr(s, "get_ebay_listing_by_item_id", lambda eid: listing)
    monkeypatch.setattr(s, "get_ebay_image_url", lambda eid: None)
    monkeypatch.setattr(s, "search_candidates_on_platform",
                        lambda plat, kw, max_results=5: [hit] if plat == "mercari" else [])
    monkeypatch.setattr(s, "check_candidate_availability",
                        lambda url, **_k: {"status": "available", "signal": "m",
                                           "checked_at": "2026-06-05T00:00:00+00:00"})
    monkeypatch.setattr(s, "_estimate_profit_for_candidate", lambda **k: 5000.0)
    monkeypatch.setattr(s, "check_supplier_candidate_profitable",
                        lambda profit_with_refund, purchase_yen: (True, {}))
    monkeypatch.setattr(s, "load_settings", lambda: {})
    monkeypatch.setattr(s, "add_supplier_candidate", lambda **kw: 1)

    def _fake_batch(items, **k):
        # 1 件投入 → 却下スコア(reject)でも台帳に記録される検証のため低 score
        res = {it.custom_id: EvaluationResult(match_score=15, reasoning="rejected")
               for it in items}
        from monitor.supplier_batch_evaluator import BatchResult
        return BatchResult(batch_id="b1", results=res, submitted=len(items))
    monkeypatch.setattr(sbe, "evaluate_batch", _fake_batch)

    cfg = {"tasks_enabled": {"supplier_sweep": {"use_batch_api": True,
                                                "min_batch_size": 1}}}
    s.run_supplier_sweep_batch(cfg)

    led = get_recent_candidate_evaluation("951", s._normalize_url(url))
    assert led is not None and led["match_score"] == 15, "却下評価も台帳記録 (再評価skip用)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
