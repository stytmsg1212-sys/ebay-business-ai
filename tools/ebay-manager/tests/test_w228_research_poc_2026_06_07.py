#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 商品リサーチ自動化 フェーズB MVP PoC テスト.

設計書: .company/engineering/docs/2026-06-07-product-research-automation-spec.md
ロック: 「1 商品手入力 → フリマ探索 + AI 同一性提示まで (出品しない)」が DoD。

カバレッジ:
  (a) migration v67 冪等性 (init_db x2 でデータ保持 + research_candidates 列実在)
  (b) research_candidates CRUD (insert / get / list / update_result)
  (c) status 状態機械 (許容遷移 / 不正遷移 ValueError / needs_review reason 必須)
  (d) 利益計算: weight 無しで例外でなく needs_review に落ちる (Q0 偽黒字防止)
  (e) フリマ探索エラーで needs_review に分類 (P2: 取得 error vs 0 件)

mock 方針:
  Claude API / Playwright / HTTP は全て monkeypatch。実 API/実検索を呼ばない
  (conftest.py の autouse fixture と同様 hermetic に保つ)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# (a) migration v67 冪等性
# ---------------------------------------------------------------------------

def test_v67_migration_idempotent_keeps_data():
    """init_db を 2 回連続実行しても research_candidates のデータが消えない."""
    from monitor.database import init_db, get_conn

    init_db()
    with get_conn() as c:
        c.execute(
            "INSERT INTO research_candidates "
            "(title_ja, manual_weight_g, terapeak_avg_price_usd, status) "
            "VALUES ('W228-Test SonyWH', 250.0, 180.0, 'new')"
        )
    init_db()  # 2 回目で DROP/DELETE が走ったら 0 件になる = 冪等性違反
    with get_conn() as c:
        rows = c.execute(
            "SELECT title_ja, manual_weight_g, terapeak_avg_price_usd, status "
            "FROM research_candidates WHERE title_ja='W228-Test SonyWH'"
        ).fetchall()
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info(research_candidates)"
        ).fetchall()}

    assert len(rows) == 1, "冪等性違反: init_db 2 回でデータ消失"
    assert tuple(rows[0]) == ("W228-Test SonyWH", 250.0, 180.0, "new")
    # 必須列が揃っていること
    assert {"rc_id", "title_ja", "manual_weight_g", "status",
            "terapeak_avg_price_usd", "found_url", "found_price_jpy",
            "match_score", "match_reason", "estimated_profit_usd",
            "needs_review_reason"} <= cols
    assert ver >= 67, f"user_version not bumped: {ver}"


# ---------------------------------------------------------------------------
# (b) CRUD
# ---------------------------------------------------------------------------

def test_insert_get_list_research_candidate():
    """insert → get で同じ値が引ける. list_research_candidates が status で絞れる."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id1 = rc_db.insert_research_candidate(
        "Audio-Technica ATH-CKS330NC",
        manual_weight_g=120.0,
        terapeak_avg_price_usd=85.0,
    )
    rc_id2 = rc_db.insert_research_candidate(
        "Pioneer DJM-450",
        manual_weight_g=3500.0,
        terapeak_avg_price_usd=520.0,
    )
    assert rc_id1 > 0 and rc_id2 > 0 and rc_id1 != rc_id2

    got1 = rc_db.get_research_candidate(rc_id1)
    assert got1 is not None
    assert got1["title_ja"] == "Audio-Technica ATH-CKS330NC"
    assert got1["manual_weight_g"] == 120.0
    assert got1["terapeak_avg_price_usd"] == 85.0
    assert got1["status"] == rc_db.STATUS_NEW

    # list で全件取得
    all_rows = rc_db.list_research_candidates()
    titles = {r["title_ja"] for r in all_rows}
    assert {"Audio-Technica ATH-CKS330NC", "Pioneer DJM-450"} <= titles

    # status 絞り込み
    news = rc_db.list_research_candidates(status=rc_db.STATUS_NEW)
    assert all(r["status"] == rc_db.STATUS_NEW for r in news)


def test_insert_rejects_empty_title():
    """Q0: title_ja 空は silent に保存させない (ValueError)."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    with pytest.raises(ValueError):
        rc_db.insert_research_candidate("")
    with pytest.raises(ValueError):
        rc_db.insert_research_candidate("   ")


def test_update_research_candidate_result_partial():
    """update_research_candidate_result が部分 update を許容する."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("Test Item", manual_weight_g=500.0)
    # 部分: found_url のみ
    ok = rc_db.update_research_candidate_result(
        rc_id, found_url="https://jp.mercari.com/item/m12345"
    )
    assert ok is True
    got = rc_db.get_research_candidate(rc_id)
    assert got["found_url"] == "https://jp.mercari.com/item/m12345"
    assert got["match_score"] is None  # 触っていない列は不変


# ---------------------------------------------------------------------------
# (c) status 状態機械
# ---------------------------------------------------------------------------

def test_status_transitions_allowed_and_forbidden():
    """new → sourcing は OK. new → sourced は禁止 (中間スキップ silent 防止)."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("State Machine Test")

    # 許容: new → sourcing
    assert rc_db.update_status(rc_id, rc_db.STATUS_SOURCING) is True
    assert rc_db.get_research_candidate(rc_id)["status"] == rc_db.STATUS_SOURCING

    # 許容: sourcing → sourced
    assert rc_db.update_status(rc_id, rc_db.STATUS_SOURCED) is True

    # 禁止: sourced → not_found (一度 sourced したら needs_review 経由のみ)
    with pytest.raises(ValueError, match="transition not allowed"):
        rc_db.update_status(rc_id, rc_db.STATUS_NOT_FOUND)

    # 不正 status 値そのもの
    with pytest.raises(ValueError, match="invalid new_status"):
        rc_db.update_status(rc_id, "garbage_status")


def test_needs_review_requires_reason():
    """needs_review に落とすには reason 必須 (Q0 silent skip 防止)."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("Reason Required")
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)

    # 理由なし → 拒否
    with pytest.raises(ValueError, match="needs_review_reason is required"):
        rc_db.update_status(rc_id, rc_db.STATUS_NEEDS_REVIEW)
    # 空文字も拒否
    with pytest.raises(ValueError, match="needs_review_reason is required"):
        rc_db.update_status(
            rc_id, rc_db.STATUS_NEEDS_REVIEW, needs_review_reason="   "
        )

    # 理由ありなら OK
    assert rc_db.update_status(
        rc_id,
        rc_db.STATUS_NEEDS_REVIEW,
        needs_review_reason="weight 未入力で利益計算不能",
    ) is True
    got = rc_db.get_research_candidate(rc_id)
    assert got["needs_review_reason"] == "weight 未入力で利益計算不能"


# ---------------------------------------------------------------------------
# (d) 利益計算: weight 無しで needs_review に落ちる
# ---------------------------------------------------------------------------

def test_estimate_profit_without_weight_returns_needs_review_reason():
    """P1-1: weight 欠落で例外でなく (None, reason) を返す (0 clip 偽黒字防止)."""
    from monitor.research_poc import estimate_profit_usd_for_research

    # weight=None
    profit, reason = estimate_profit_usd_for_research(
        terapeak_avg_price_usd=150.0,
        purchase_yen=8000,
        manual_weight_g=None,
    )
    assert profit is None
    assert reason is not None and "manual_weight_g" in reason

    # weight=0 (0 clip 防止確認)
    profit, reason = estimate_profit_usd_for_research(
        terapeak_avg_price_usd=150.0,
        purchase_yen=8000,
        manual_weight_g=0,
    )
    assert profit is None
    assert reason is not None and "0 clip" in reason


def test_estimate_profit_without_terapeak_or_purchase():
    """terapeak 平均 or 仕入金額が欠けても needs_review reason を返す."""
    from monitor.research_poc import estimate_profit_usd_for_research

    p, r = estimate_profit_usd_for_research(
        terapeak_avg_price_usd=None,
        purchase_yen=8000,
        manual_weight_g=250.0,
    )
    assert p is None and r is not None and "terapeak" in r.lower()

    p, r = estimate_profit_usd_for_research(
        terapeak_avg_price_usd=150.0,
        purchase_yen=0,
        manual_weight_g=250.0,
    )
    assert p is None and r is not None and "purchase_yen" in r


def test_evaluate_product_weight_missing_lands_needs_review(monkeypatch):
    """E2E: weight 無しの evaluate_product が status=needs_review で着地する.

    フリマ探索 & claude_evaluator は mock (実 API/実検索を呼ばない)。
    """
    from monitor.database import init_db
    from monitor import research_poc

    init_db()

    # フリマ探索 mock: 各 platform 1 件返す
    def _fake_search(platform, keyword, max_results=5):
        return [
            research_poc.FreemarketHit(
                source_platform=platform,
                url=f"https://example.com/{platform}/item/123",
                title=f"{keyword} (mocked {platform})",
                price_jpy=6800,
                image_url=None,
            )
        ]

    monkeypatch.setattr(research_poc, "_search_freemarket", _fake_search)

    # claude_evaluator mock (match_score=85 で AI 同一性 OK = AI エラー無し)
    from monitor import claude_evaluator as ce

    def _fake_evaluate_match(**kwargs):
        return ce.EvaluationResult(match_score=85, reasoning="型番一致 (mock)")

    monkeypatch.setattr(ce, "evaluate_match", _fake_evaluate_match)
    # research_poc 内 import 経路の差し替え (cached import 対策)
    monkeypatch.setattr(
        "monitor.claude_evaluator.evaluate_match", _fake_evaluate_match
    )

    result = research_poc.evaluate_product(
        title_ja="Sony WH-1000XM5",
        manual_weight_g=None,  # ← ここが test の核心
        terapeak_avg_price_usd=320.0,
    )

    assert result["status"] == "needs_review"
    assert result["needs_review_reason"] is not None
    assert "manual_weight_g" in result["needs_review_reason"]
    # AI 同一性自体は評価できているので match_score は 85 のまま保存
    assert result["match_score"] == 85


# ---------------------------------------------------------------------------
# (e) フリマ探索エラーで needs_review
# ---------------------------------------------------------------------------

def test_evaluate_product_search_error_lands_needs_review(monkeypatch):
    """P2: フリマ探索が 1 platform でも例外 → status=needs_review + reason."""
    from monitor.database import init_db
    from monitor import research_poc

    init_db()

    # mercari は成功、yahoo は例外、paypay は成功
    def _fake_search(platform, keyword, max_results=5):
        if platform == "yahoo_auctions":
            raise RuntimeError("Playwright timeout (simulated)")
        return [
            research_poc.FreemarketHit(
                source_platform=platform,
                url=f"https://example.com/{platform}/item/X",
                title=f"{keyword} ({platform})",
                price_jpy=5000,
                image_url=None,
            )
        ]

    monkeypatch.setattr(research_poc, "_search_freemarket", _fake_search)

    # claude_evaluator は呼ばれないはずだが安全のため mock
    from monitor import claude_evaluator as ce
    monkeypatch.setattr(
        ce, "evaluate_match",
        lambda **kw: ce.EvaluationResult(match_score=0, reasoning="should not be called"),
    )

    result = research_poc.evaluate_product(
        title_ja="Pioneer DJM-450",
        manual_weight_g=4000.0,
        terapeak_avg_price_usd=550.0,
    )

    assert result["status"] == "needs_review"
    assert result["needs_review_reason"] is not None
    assert "yahoo_auctions" in result["needs_review_reason"]
    assert "取得エラー" in result["needs_review_reason"]
    # search_errors 配列にもエラー platform 名が入る
    assert any("yahoo_auctions" in e for e in result["search_errors"])


def test_evaluate_product_zero_hits_lands_not_found(monkeypatch):
    """0 件 (実在しない) は取得エラーと別状態 (not_found) で記録."""
    from monitor.database import init_db
    from monitor import research_poc

    init_db()
    # 全 platform で空リスト (取得は成功)
    monkeypatch.setattr(
        research_poc, "_search_freemarket", lambda platform, kw, max_results=5: []
    )

    result = research_poc.evaluate_product(
        title_ja="ありえない超レア品 999",
        manual_weight_g=300.0,
        terapeak_avg_price_usd=120.0,
    )

    assert result["status"] == "not_found"
    assert result["match_score"] is None
    assert result["found_url"] is None
    # not_found に reason は不要 (取得エラーと違って業務判断)
    assert result["needs_review_reason"] is None


def test_evaluate_product_happy_path(monkeypatch):
    """E2E happy path: 全部成功 → status=sourced + profit_usd 計算済み.

    Important: weight=250g を渡し、terapeak $200 vs 仕入 ¥5000 で利益が出る前提。
    calculator の実 settings.json を読むので、breakeven が異常値でないことだけ確認。
    """
    from monitor.database import init_db
    from monitor import research_poc

    init_db()

    monkeypatch.setattr(
        research_poc, "_search_freemarket",
        lambda platform, kw, max_results=5: [
            research_poc.FreemarketHit(
                source_platform=platform,
                url=f"https://example.com/{platform}/item/H",
                title=f"{kw} ({platform})",
                price_jpy=5000,
                image_url=None,
            )
        ],
    )

    from monitor import claude_evaluator as ce
    monkeypatch.setattr(
        ce, "evaluate_match",
        lambda **kw: ce.EvaluationResult(
            match_score=92, reasoning="型番完全一致 + 色一致 (mock)"
        ),
    )

    result = research_poc.evaluate_product(
        title_ja="Audio-Technica ATH-CKS330NC",
        manual_weight_g=250.0,
        terapeak_avg_price_usd=200.0,
    )

    assert result["status"] == "sourced"
    assert result["needs_review_reason"] is None
    assert result["match_score"] == 92
    assert result["match_reason"].startswith("型番完全一致")
    # profit は計算済み (float)。具体値は settings.json/送料テーブル依存なので
    # 「数値が入っていること」のみ確認 (実値テストは別 cassette テストで)。
    assert result["estimated_profit_usd"] is not None
    assert isinstance(result["estimated_profit_usd"], float)
    assert result["found_url"].startswith("https://example.com/")
    assert result["found_price_jpy"] == 5000
    assert result["source_platform"] in research_poc.DEFAULT_PLATFORMS

    # DB にも全 field が保存されていること
    from monitor import research_candidates_db as rc_db
    got = rc_db.get_research_candidate(result["rc_id"])
    assert got["status"] == "sourced"
    assert got["match_score"] == 92
    assert got["found_price_jpy"] == 5000
    assert got["estimated_profit_usd"] == result["estimated_profit_usd"]


def test_insert_rejects_non_new_status():
    """Codex#2: insert は 'new' のみ。他 status 直接挿入で状態機械を迂回させない."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    for bad in (rc_db.STATUS_SOURCED, rc_db.STATUS_NOT_FOUND,
                rc_db.STATUS_NEEDS_REVIEW, "garbage"):
        with pytest.raises(ValueError):
            rc_db.insert_research_candidate("X", status=bad)
    # 'new' は OK
    assert rc_db.insert_research_candidate("OK", status=rc_db.STATUS_NEW) > 0


def test_evaluate_product_hits_without_price_lands_needs_review(monkeypatch):
    """Codex#1: ヒットはあるが全件 価格 None → not_found でなく needs_review."""
    from monitor.database import init_db
    from monitor import research_poc

    init_db()
    # 価格 None のヒットだけ返す (取得不完全)
    monkeypatch.setattr(
        research_poc, "_search_freemarket",
        lambda platform, kw, max_results=5: [
            research_poc.FreemarketHit(
                source_platform=platform,
                url=f"https://example.com/{platform}/np",
                title=f"{kw}", price_jpy=None, image_url=None,
            )
        ],
    )
    result = research_poc.evaluate_product(
        title_ja="Price Parse Fail",
        manual_weight_g=300.0,
        terapeak_avg_price_usd=120.0,
    )
    assert result["status"] == "needs_review"
    assert result["needs_review_reason"] is not None
    assert "価格" in result["needs_review_reason"]
    assert result["hits_count_total"] >= 1  # ヒット自体はあった


def test_evaluate_product_ai_error_lands_needs_review(monkeypatch):
    """claude_evaluator が error を返す (API key 未設定等) → needs_review."""
    from monitor.database import init_db
    from monitor import research_poc

    init_db()
    monkeypatch.setattr(
        research_poc, "_search_freemarket",
        lambda platform, kw, max_results=5: [
            research_poc.FreemarketHit(
                source_platform=platform,
                url=f"https://example.com/{platform}/item/A",
                title=f"{kw}",
                price_jpy=5000,
                image_url=None,
            )
        ],
    )

    from monitor import claude_evaluator as ce
    monkeypatch.setattr(
        ce, "evaluate_match",
        lambda **kw: ce.EvaluationResult(
            match_score=0,
            reasoning="API error",
            error="ANTHROPIC_API_KEY not set",
        ),
    )

    result = research_poc.evaluate_product(
        title_ja="Test AI Error",
        manual_weight_g=300.0,
        terapeak_avg_price_usd=180.0,
    )

    assert result["status"] == "needs_review"
    assert result["needs_review_reason"] is not None
    assert "claude_evaluator" in result["needs_review_reason"]
