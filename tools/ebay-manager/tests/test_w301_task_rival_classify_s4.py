"""W301 AI 店長 Phase1 S4 (2026-07-02): tasks/task_rival_classify.py.

設計書: .company/engineering/docs/2026-06-24-ai-manager-phase1-design.md §5/§6/§8

カバレッジ:
  - kill switch (enabled=false) → success=True + skip 痕跡
  - discoveries 0 件 → success=True + skip 痕跡
  - real 判定 (AI same_product=True, confidence>=0.85) →
      listing_rival_discoveries.status='monitoring_added' +
      competitor_products upsert (pricing_eligible は default 0 のまま = Shadow 安全)
  - noise 判定 (スコア足切り: タイトル類似度極小) →
      listing_rival_discoveries.status='dismissed'
  - review 判定 (AI confidence 0.6-0.85) → status は 'new' のまま (再分類対象)
  - AI 例外 (ai_key_missing) → review へ fail-closed + Discord issue 通知が
      呼ばれること (webhook 未設定時は痕跡 warning のみで例外にならない)
  - rival_classifications に SKU 列を使わず INSERT されること (既存 S2 テストで
      カバー済のためここでは再検証しない、統合経路のみ確認)
"""
from __future__ import annotations

import pytest

from monitor.database import get_conn
from monitor.rival_classifier import AIJudgeResult


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _seed_listing_and_discovery(
    *,
    ebay_item_id: str = "OUR1",
    competitor_item_id: str = "COMP1",
    our_title: str = "Sony WH-1000XM5 Wireless Headphones Black",
    competitor_title: str = "ソニー WH-1000XM5 ワイヤレスヘッドホン ブラック 美品",
    our_price: float = 200.0,
    competitor_price: float = 190.0,
) -> int:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, current_price, ebay_condition_id, condition_rank)
               VALUES (?, 'stock01', ?, ?, '3000', 'B')""",
            (ebay_item_id, our_title, our_price),
        )
        cur = conn.execute(
            """INSERT INTO listing_rival_discoveries
               (ebay_item_id, competitor_seller, competitor_item_id,
                competitor_title, competitor_price_usd, search_keyword, status)
               VALUES (?, 'jp_seller_1', ?, ?, ?, 'sony headphones', 'new')""",
            (ebay_item_id, competitor_item_id, competitor_title, competitor_price),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _get_discovery_status(discovery_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM listing_rival_discoveries WHERE id=?", (discovery_id,)
        ).fetchone()
    return row[0]


# ────────────────────────────────────────────────────────────────
# kill switch / 0 件
# ────────────────────────────────────────────────────────────────

def test_kill_switch_disabled_skips(tmp_db):
    from tasks.task_rival_classify import run_rival_classify
    config = {"tasks_enabled": {"rival_classify": {"enabled": False}}}
    result = run_rival_classify(config)
    assert result["success"] is True
    assert "enabled=false" in result["message"]
    assert result["processed"] == 0


def test_zero_discoveries_skips(tmp_db):
    from tasks.task_rival_classify import run_rival_classify
    result = run_rival_classify({})
    assert result["success"] is True
    assert "0 discoveries" in result["message"]


# ────────────────────────────────────────────────────────────────
# real 判定 → status 遷移 + competitor_products upsert (Shadow 安全)
# ────────────────────────────────────────────────────────────────

def test_real_classification_updates_status_and_upserts_competitor(tmp_db, monkeypatch):
    discovery_id = _seed_listing_and_discovery()

    def _fake_judge_rival(signals, model="unused"):
        return AIJudgeResult(
            same_product=True, variant_risk="none", condition="USED",
            confidence=0.95, reason="同一商品", ai_model="claude-haiku-4-5-20251001",
            route="ai",
        )

    import monitor.rival_classifier as rc
    monkeypatch.setattr(rc, "judge_rival", _fake_judge_rival)

    from tasks.task_rival_classify import run_rival_classify
    result = run_rival_classify({})

    assert result["success"] is True
    assert result["processed"] == 1
    assert result["real"] == 1
    assert result["noise"] == 0
    assert result["review"] == 0

    assert _get_discovery_status(discovery_id) == "monitoring_added"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_active, pricing_eligible, our_item_id FROM competitor_products "
            "WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert row is not None, "competitor_products に upsert されていない"
    assert row[0] == 1, "is_active=1 のはず"
    assert (row[1] or 0) == 0, "Shadow 中は pricing_eligible が絶対 0 のはず"
    assert row[2] == "OUR1"

    with get_conn() as conn:
        rc_row = conn.execute(
            "SELECT classification, shadow_mode, would_be_eligible FROM "
            "rival_classifications WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert rc_row[0] == "real"
    assert rc_row[1] == 1  # shadow_mode 固定
    assert rc_row[2] == 1  # would_be_eligible


# ────────────────────────────────────────────────────────────────
# noise 判定 (スコア足切り: タイトル無関係) → status='dismissed'
# ────────────────────────────────────────────────────────────────

def test_noise_classification_dismisses_discovery(tmp_db):
    discovery_id = _seed_listing_and_discovery(
        our_title="Sony WH-1000XM5 Wireless Headphones",
        competitor_title="全く関係ない掃除機のパーツセット",
    )

    from tasks.task_rival_classify import run_rival_classify
    result = run_rival_classify({})

    assert result["success"] is True
    assert result["noise"] == 1
    assert result["real"] == 0
    assert _get_discovery_status(discovery_id) == "dismissed"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT classification, route FROM rival_classifications "
            "WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert row[0] == "noise"
    assert row[1] == "score"

    # noise は competitor_products に一切書かれない
    with get_conn() as conn:
        comp = conn.execute(
            "SELECT 1 FROM competitor_products WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert comp is None


# ────────────────────────────────────────────────────────────────
# review 判定 (AI confidence 中間) → status は 'new' のまま (再分類対象)
# ────────────────────────────────────────────────────────────────

def test_review_classification_keeps_status_new(tmp_db, monkeypatch):
    discovery_id = _seed_listing_and_discovery()

    def _fake_judge_rival(signals, model="unused"):
        return AIJudgeResult(
            same_product=True, variant_risk="unknown", condition="USED",
            confidence=0.7, reason="やや不確か", ai_model="claude-haiku-4-5-20251001",
            route="ai",
        )

    import monitor.rival_classifier as rc
    monkeypatch.setattr(rc, "judge_rival", _fake_judge_rival)

    from tasks.task_rival_classify import run_rival_classify
    result = run_rival_classify({})

    assert result["review"] == 1
    assert _get_discovery_status(discovery_id) == "new", (
        "review は既存 triage UI へ、次回 run でも再分類対象になる仕様 (設計書 §6)"
    )

    with get_conn() as conn:
        comp = conn.execute(
            "SELECT 1 FROM competitor_products WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert comp is None


# ────────────────────────────────────────────────────────────────
# AI 例外系 (ANTHROPIC_API_KEY 未設定) → review + issue 集計 (Q0)
# ────────────────────────────────────────────────────────────────

def test_ai_key_missing_falls_back_to_review_and_reports_issue(tmp_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _seed_listing_and_discovery()

    from tasks.task_rival_classify import run_rival_classify
    # webhook 未設定 → Discord 送信はスキップされるが例外にはならないこと
    result = run_rival_classify({"discord": {}})

    assert result["success"] is True
    assert result["review"] == 1
    assert result["issues"] == 1

    with get_conn() as conn:
        row = conn.execute(
            "SELECT route FROM rival_classifications WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert row[0] == "ai_key_missing"


# ────────────────────────────────────────────────────────────────
# max_ai_calls_per_run cap 超過 → review + cap 痕跡
# ────────────────────────────────────────────────────────────────

def test_ai_cap_exceeded_falls_back_to_review(tmp_db, monkeypatch):
    # our_rank/competitor_title を微妙にして needs_ai=True になる状況を作る
    # (タイトル類似度が noise/real どちらの閾値にも入らない中間帯)。
    _seed_listing_and_discovery(
        our_title="Sony WH-1000XM5 Wireless Headphones Black Bluetooth",
        competitor_title="WH-1000XM5 ソニー ワイヤレス ヘッドホン",
        our_price=200.0,
        competitor_price=150.0,
    )

    from tasks.task_rival_classify import run_rival_classify
    config = {"tasks_enabled": {"rival_classify": {"max_ai_calls_per_run": 0}}}
    result = run_rival_classify(config)

    assert result["success"] is True
    assert result["ai_calls_used"] == 0
    assert result["review"] == 1
    assert result["issues"] == 1

    with get_conn() as conn:
        row = conn.execute(
            "SELECT route FROM rival_classifications WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert row[0] == "ai_cap_exceeded"
