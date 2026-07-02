"""W301 AI 店長 Phase1 S2 (2026-07-02): 競合分類エンジン (monitor/rival_classifier.py).

設計書: .company/engineering/docs/2026-06-24-ai-manager-phase1-design.md §5/§8
議事録: .company/engineering/docs/2026-06-22-ai-manager-hearing-minutes.md §3/§13.1

カバレッジ:
  - ハード除外の各条件 (country_not_jp / sold_out / competitor_junk_vs_our_working /
    ddu_blacklist_seller)
  - 国不明は除外されない (保守的)
  - 評価稼ぎ farmer → review (Phase1 安全弁)
  - max_ai_calls_per_run cap 超過 → review + 痕跡 (logger.warning)
  - AI 例外 → review (fail-closed)
  - JSON パース失敗 → review (fail-closed)
  - ANTHROPIC_API_KEY 未設定 → review (fail-closed)
  - 3 分岐の境界 (confidence=0.85 ちょうど / 0.6 ちょうど / same_product=False)
  - Shadow モードで pricing_eligible が絶対に変わらないこと (DB assert)
  - would_be_eligible の記録 (real のみ 1)
  - 全判定が rival_classifications に SKU 列を使わず INSERT されること
"""
from __future__ import annotations

import pytest

from monitor.rival_classifier import (
    AIJudgeResult,
    ClassifyResult,
    classify_batch,
    classify_discovery,
    classify_rival,
    compute_price_ratio,
    compute_title_similarity,
    judge_rival,
    save_rival_classification,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _base_signals(**overrides) -> dict:
    signals = {
        "ebay_item_id": "OUR1",
        "competitor_item_id": "COMP1",
        "our_title": "Sony WH-1000XM5 Wireless Headphones Black",
        "competitor_title": "ソニー WH-1000XM5 ワイヤレスヘッドホン ブラック 美品",
        "our_price_usd": 200.0,
        "competitor_price_usd": 190.0,
        "our_rank": "B",
        "competitor_seller": "jp_seller_1",
        "competitor_country": "JP",
        "is_sold_out": False,
    }
    signals.update(overrides)
    return signals


# ────────────────────────────────────────────────────────────────
# ハード除外
# ────────────────────────────────────────────────────────────────

def test_hard_exclude_country_not_jp():
    signals = _base_signals(competitor_country="US")
    result = classify_discovery(signals)
    assert result.classification == "noise"
    assert result.route == "hard_exclude"
    assert result.exclude_reason == "country_not_jp"
    assert result.needs_ai is False


def test_country_unknown_not_excluded():
    """不明 (None/空) は除外しない = 保守的 (task 指示明記)."""
    signals = _base_signals(competitor_country=None)
    result = classify_discovery(signals)
    assert result.exclude_reason != "country_not_jp"
    # 除外されない = 通常のスコア/AI パイプラインへ進む (今回は高類似度で needs_ai)
    assert result.classification != "noise" or result.route != "hard_exclude"


def test_hard_exclude_sold_out_flag():
    signals = _base_signals(is_sold_out=True)
    result = classify_discovery(signals)
    assert result.classification == "noise"
    assert result.exclude_reason == "sold_out"


def test_hard_exclude_sold_out_title_keyword():
    signals = _base_signals(is_sold_out=None, competitor_title="ソニー WH-1000XM5 売り切れ")
    result = classify_discovery(signals)
    assert result.classification == "noise"
    assert result.exclude_reason == "sold_out"


def test_hard_exclude_competitor_junk_vs_our_working():
    signals = _base_signals(our_rank="B", competitor_title="ソニー WH-1000XM5 ジャンク品")
    result = classify_discovery(signals)
    assert result.classification == "noise"
    assert result.exclude_reason == "competitor_junk_vs_our_working"


def test_our_junk_rank_does_not_trigger_junk_exclusion():
    """自社が As-Is (非動作品扱い) なら、相手が JUNK でも門前払いしない (workable でないため)."""
    signals = _base_signals(our_rank="As-Is", competitor_title="ソニー WH-1000XM5 ジャンク品")
    result = classify_discovery(signals)
    assert result.exclude_reason != "competitor_junk_vs_our_working"


def test_hard_exclude_ddu_blacklist():
    signals = _base_signals(competitor_seller="ddu_seller_x")
    result = classify_discovery(signals, dou_blacklist={"ddu_seller_x"})
    assert result.classification == "noise"
    assert result.exclude_reason == "ddu_blacklist_seller"


def test_ddu_blacklist_no_match_passes_through():
    signals = _base_signals(competitor_seller="jp_seller_1")
    result = classify_discovery(signals, dou_blacklist={"ddu_seller_x"})
    assert result.exclude_reason != "ddu_blacklist_seller"


# ────────────────────────────────────────────────────────────────
# farmer 安全弁
# ────────────────────────────────────────────────────────────────

def test_farmer_safety_valve_routes_to_review_not_noise():
    signals = _base_signals(
        competitor_seller_feedback_score=5,
        competitor_price_usd=50.0,  # our=200 → price_ratio=0.25 <= 0.5
    )
    result = classify_discovery(signals)
    assert result.classification == "review"
    assert result.route == "farmer_safety_valve"
    assert result.needs_ai is False


def test_farmer_not_triggered_when_feedback_score_missing():
    """feedback_score 不明 (Phase2 snapshot 未導入) では farmer 判定しない (安全側スキップ)."""
    signals = _base_signals(
        competitor_seller_feedback_score=None,
        competitor_price_usd=50.0,
    )
    result = classify_discovery(signals)
    assert result.route != "farmer_safety_valve"


def test_farmer_not_triggered_when_price_ratio_normal():
    signals = _base_signals(
        competitor_seller_feedback_score=5,
        competitor_price_usd=190.0,  # price_ratio ≈0.95, farmer 閾値 0.5 超
    )
    result = classify_discovery(signals)
    assert result.route != "farmer_safety_valve"


# ────────────────────────────────────────────────────────────────
# スコア足切り
# ────────────────────────────────────────────────────────────────

def test_score_noise_low_title_similarity():
    signals = _base_signals(
        our_title="Sony WH-1000XM5 Wireless Headphones Black",
        competitor_title="任天堂 Nintendo Switch 有機ELモデル 本体",
    )
    result = classify_discovery(signals)
    assert result.classification == "noise"
    assert result.exclude_reason == "score_low_similarity"


def test_score_noise_price_outlier():
    signals = _base_signals(competitor_price_usd=5.0)  # our=200 → ratio=0.025 (外れ値)
    result = classify_discovery(signals)
    assert result.classification == "noise"
    assert result.exclude_reason == "score_price_outlier"


def test_grey_case_needs_ai():
    signals = _base_signals()
    result = classify_discovery(signals)
    assert result.needs_ai is True
    assert result.route == "pending_ai"


# ────────────────────────────────────────────────────────────────
# 3 分岐 境界値
# ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("confidence,expected", [
    (0.85, "real"),      # ちょうど境界 → real
    (0.849999, "review"),
    (0.6, "review"),      # ちょうど境界 → review
    (0.599999, "noise"),
    (1.0, "real"),
    (0.0, "noise"),
])
def test_confidence_branch_boundaries_same_product_true(monkeypatch, tmp_db, confidence, expected):
    def _fake_judge(signals, model="x"):
        return AIJudgeResult(
            same_product=True, variant_risk="none", condition="USED",
            confidence=confidence, reason="test", ai_model="claude-haiku-4-5-20251001",
            route="ai",
        )
    monkeypatch.setattr("monitor.rival_classifier.judge_rival", _fake_judge)
    signals = _base_signals()
    result = classify_rival(signals, persist=False)
    assert result.classification == expected


def test_same_product_false_is_noise_regardless_of_confidence(monkeypatch, tmp_db):
    def _fake_judge(signals, model="x"):
        return AIJudgeResult(
            same_product=False, variant_risk="none", condition="USED",
            confidence=0.99, reason="different item", ai_model="claude-haiku-4-5-20251001",
            route="ai",
        )
    monkeypatch.setattr("monitor.rival_classifier.judge_rival", _fake_judge)
    signals = _base_signals()
    result = classify_rival(signals, persist=False)
    assert result.classification == "noise"


# ────────────────────────────────────────────────────────────────
# fail-closed
# ────────────────────────────────────────────────────────────────

def test_ai_key_missing_fails_closed_to_review(monkeypatch):
    # 2026-07-02: _get_client は monitor.credentials import (.env 読込副作用) で
    # 鍵を復元し得るため、先に import してキャッシュ化してから delenv する
    # (= 「.env にも鍵が無い」状況の正しいシミュレーション)
    import monitor.credentials  # noqa: F401
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = judge_rival(_base_signals())
    assert result.error is not None
    assert result.route == "ai_key_missing"


def test_ai_exception_fails_closed_to_review(monkeypatch, tmp_db):
    # log_anthropic_response(success=False) が DB へ書くため tmp_db で本番 DB 汚染を防ぐ。
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")

    class _RaisingClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("simulated network failure")

    monkeypatch.setattr("monitor.rival_classifier._get_client", lambda: _RaisingClient())
    result = judge_rival(_base_signals())
    assert result.error is not None
    assert result.route == "ai_error"


def test_ai_json_parse_failure_fails_closed_to_review(monkeypatch, tmp_db):
    # log_anthropic_response(success=True) が DB へ書くため tmp_db で本番 DB 汚染を防ぐ。
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")

    class _TextBlock:
        type = "text"
        text = "this is not json at all, sorry"

    class _FakeMsg:
        content = [_TextBlock()]
        usage = type("U", (), {"input_tokens": 10, "output_tokens": 5,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0})()

    class _FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                return _FakeMsg()

    monkeypatch.setattr("monitor.rival_classifier._get_client", lambda: _FakeClient())
    result = judge_rival(_base_signals())
    assert result.error is not None
    assert result.route == "ai_parse_error"


def test_ai_missing_same_product_field_fails_closed(monkeypatch, tmp_db):
    """same_product/confidence が JSON に無い/型不正 → fail-closed (review)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")

    class _TextBlock:
        type = "text"
        text = '{"variant_risk": "none", "reason": "no same_product key"}'

    class _FakeMsg:
        content = [_TextBlock()]
        usage = type("U", (), {"input_tokens": 10, "output_tokens": 5,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0})()

    class _FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                return _FakeMsg()

    monkeypatch.setattr("monitor.rival_classifier._get_client", lambda: _FakeClient())
    result = judge_rival(_base_signals())
    assert result.error is not None
    assert result.route == "ai_parse_error"


def test_classify_rival_integration_ai_error_routes_to_review(monkeypatch, tmp_db):
    def _fake_judge(signals, model="x"):
        return AIJudgeResult(error="boom", reason="AI 呼出エラー: boom",
                              ai_model="claude-haiku-4-5-20251001", route="ai_error")
    monkeypatch.setattr("monitor.rival_classifier.judge_rival", _fake_judge)
    signals = _base_signals()
    result = classify_rival(signals, persist=False)
    assert result.classification == "review"
    assert result.route == "ai_error"
    assert result.same_product is None
    assert result.confidence is None


# ────────────────────────────────────────────────────────────────
# max_ai_calls_per_run cap
# ────────────────────────────────────────────────────────────────

def test_ai_cap_exceeded_routes_to_review_with_log(monkeypatch, tmp_db, caplog):
    call_count = {"n": 0}

    def _fake_judge(signals, model="x"):
        call_count["n"] += 1
        return AIJudgeResult(same_product=True, variant_risk="none", condition="USED",
                              confidence=0.9, reason="ok",
                              ai_model="claude-haiku-4-5-20251001", route="ai")

    monkeypatch.setattr("monitor.rival_classifier.judge_rival", _fake_judge)

    discoveries = [
        _base_signals(ebay_item_id="OUR1", competitor_item_id=f"COMP{i}")
        for i in range(5)
    ]
    import logging
    with caplog.at_level(logging.WARNING, logger="monitor.rival_classifier"):
        results = classify_batch(discoveries, thresholds={"max_ai_calls_per_run": 2}, persist=False)

    assert call_count["n"] == 2, "AI 呼出は cap (2) までしか行われないこと"
    cap_exceeded = [r for r in results if r.route == "ai_cap_exceeded"]
    assert len(cap_exceeded) == 3
    for r in cap_exceeded:
        assert r.classification == "review"
    assert any("AI cap 超過" in rec.message for rec in caplog.records), (
        "cap 超過は Q0 (silent skip 禁止) により logger.warning で痕跡を残すこと"
    )


# ────────────────────────────────────────────────────────────────
# Shadow / would_be_eligible / DB 永続化
# ────────────────────────────────────────────────────────────────

def test_shadow_mode_never_changes_pricing_eligible(monkeypatch, tmp_db):
    """real 判定でも本モジュールは competitor_products に一切書き込まない
    (pricing_eligible が絶対に変わらないこと、設計書 L63 逐語)。"""
    from monitor.database import get_conn

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible) VALUES (?, ?, ?)",
            ("OUR1", "COMP1", 0),
        )

    def _fake_judge(signals, model="x"):
        return AIJudgeResult(same_product=True, variant_risk="none", condition="USED",
                              confidence=0.95, reason="real match",
                              ai_model="claude-haiku-4-5-20251001", route="ai")

    monkeypatch.setattr("monitor.rival_classifier.judge_rival", _fake_judge)
    signals = _base_signals(ebay_item_id="OUR1", competitor_item_id="COMP1")
    result = classify_rival(signals, discovery_id=1, shadow_mode=True, persist=True)
    assert result.classification == "real"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT pricing_eligible FROM competitor_products WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert row[0] == 0, "rival_classifier が competitor_products.pricing_eligible を変更した (禁止)"


def test_would_be_eligible_recorded_for_real_only(monkeypatch, tmp_db):
    from monitor.database import get_conn

    def _fake_judge_real(signals, model="x"):
        return AIJudgeResult(same_product=True, confidence=0.9, variant_risk="none",
                              condition="USED", reason="real",
                              ai_model="claude-haiku-4-5-20251001", route="ai")

    monkeypatch.setattr("monitor.rival_classifier.judge_rival", _fake_judge_real)
    signals_real = _base_signals(ebay_item_id="OUR1", competitor_item_id="COMP_REAL")
    classify_rival(signals_real, discovery_id=10, shadow_mode=True, persist=True)

    signals_noise = _base_signals(
        ebay_item_id="OUR1", competitor_item_id="COMP_NOISE", competitor_country="US",
    )
    classify_rival(signals_noise, discovery_id=11, shadow_mode=True, persist=True)

    with get_conn() as conn:
        real_row = conn.execute(
            "SELECT classification, would_be_eligible, shadow_mode FROM rival_classifications "
            "WHERE competitor_item_id='COMP_REAL'"
        ).fetchone()
        noise_row = conn.execute(
            "SELECT classification, would_be_eligible, shadow_mode FROM rival_classifications "
            "WHERE competitor_item_id='COMP_NOISE'"
        ).fetchone()

    assert real_row[0] == "real" and real_row[1] == 1 and real_row[2] == 1
    assert noise_row[0] == "noise" and noise_row[1] == 0 and noise_row[2] == 1


def test_save_rival_classification_no_sku_column_used(tmp_db):
    """sku-rules.md: rival_classifications は ebay_item_id/competitor_item_id で識別."""
    from monitor.database import get_conn
    with get_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(rival_classifications)").fetchall()}
    assert "sku" not in cols


def test_save_rival_classification_all_fields_persisted(tmp_db):
    result = ClassifyResult(
        classification="real", route="ai", exclude_reason=None,
        title_similarity=0.9, price_ratio=0.95, same_product=True,
        variant_risk="none", ai_condition="USED", confidence=0.9,
        reason="test reason", ai_model="claude-haiku-4-5-20251001",
    )
    row_id = save_rival_classification(
        result, discovery_id=99, ebay_item_id="OUR9", competitor_item_id="COMP9",
        shadow_mode=True,
    )
    assert row_id > 0
    from monitor.database import get_conn
    with get_conn() as conn:
        row = dict(conn.execute(
            "SELECT * FROM rival_classifications WHERE id=?", (row_id,)
        ).fetchone())
    assert row["ebay_item_id"] == "OUR9"
    assert row["competitor_item_id"] == "COMP9"
    assert row["classification"] == "real"
    assert row["same_product"] == 1
    assert row["would_be_eligible"] == 1
    assert row["shadow_mode"] == 1
    assert row["ai_model"] == "claude-haiku-4-5-20251001"


# ────────────────────────────────────────────────────────────────
# ヘルパー関数の単体テスト
# ────────────────────────────────────────────────────────────────

def test_compute_title_similarity_none_when_missing():
    assert compute_title_similarity(None, "foo") is None
    assert compute_title_similarity("foo", None) is None


def test_compute_title_similarity_model_token_boost():
    sim = compute_title_similarity(
        "Sony WH-1000XM5 Black", "全く違う言葉の羅列 WH-1000XM5"
    )
    assert sim is not None and sim >= 0.9


def test_compute_price_ratio_none_when_our_price_zero_or_missing():
    assert compute_price_ratio(0, 10) is None
    assert compute_price_ratio(None, 10) is None
    assert compute_price_ratio(100, None) is None
    assert compute_price_ratio(100, 50) == 0.5
