"""W308 (2026-07-02): 自社セラーの自己マッチ遮断 — pytest.

背景: `listing_rival_discoveries` に自社ストアの出品が競合として 77 件混入
  (competitor_item_id 全件が自社 ebay_listings.ebay_item_id と一致)。
  既存の task_rival_detection.py `seller == my_seller` 除外は
  config['ebay']['seller_id'] が本番未設定のため機能していなかった (根本原因)。

カバレッジ:
  - monitor/rival_classifier.classify_discovery: self_item_ids 一致 → noise /
    hard_exclude / exclude_reason='self_listing' / needs_ai=False (AI 未呼出)
  - self_item_ids 不一致は従来通りの分岐に影響しない (regression)
  - classify_rival / classify_batch: self_item_ids 透過 + 永続化
  - tasks/task_rival_classify.run_rival_classify: self_excluded 集計 +
    listing_rival_discoveries.status='dismissed' への遷移 + AI 未呼出
  - tasks/task_rival_detection.run_rival_per_listing_detection_one: self_item_ids
    一致で record_rival_discovery を呼ばず skipped_self_listing++
  - scripts/dismiss_self_discoveries_w308.py: dry-run (DB 書込ゼロ) / --apply
    (status='dismissed' へ一括更新、既に dismissed の行は対象外)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from monitor.database import get_conn
from monitor.rival_classifier import (
    AIJudgeResult,
    classify_batch,
    classify_discovery,
    classify_rival,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    return db_mod


def _insert_listing(conn, ebay_item_id: str, sku: str = "stock:01",
                     title: str = "Test", **extra):
    cols = ["ebay_item_id", "sku", "title", "current_price"]
    vals = [ebay_item_id, sku, title, 100.0]
    for k, v in extra.items():
        cols.append(k)
        vals.append(v)
    placeholders = ",".join("?" * len(vals))
    conn.execute(
        f"INSERT INTO ebay_listings ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )


# ────────────────────────────────────────────────────────────────
# classify_discovery: 純ロジック
# ────────────────────────────────────────────────────────────────

def test_classify_discovery_self_item_id_excludes_without_ai():
    signals = {
        "ebay_item_id": "OUR1",
        "competitor_item_id": "OUR2",  # 自社の別 listing が競合として混入
        "our_title": "Sony WH-1000XM5 Wireless Headphones Black",
        "competitor_title": "Sony WH-1000XM5 Wireless Headphones Black",
        "our_price_usd": 200.0,
        "competitor_price_usd": 195.0,
    }
    result = classify_discovery(signals, self_item_ids=frozenset({"OUR2", "OUR3"}))
    assert result.classification == "noise"
    assert result.route == "hard_exclude"
    assert result.exclude_reason == "self_listing"
    assert result.needs_ai is False


def test_classify_discovery_self_check_precedes_country_check():
    """自己マッチは国判定より前に確定する (country_not_jp に化けない)。"""
    signals = {
        "ebay_item_id": "OUR1",
        "competitor_item_id": "OUR2",
        "our_title": "Sony WH-1000XM5",
        "competitor_title": "Sony WH-1000XM5",
        "competitor_country": "US",  # 国判定なら別 exclude_reason になるはず
    }
    result = classify_discovery(signals, self_item_ids=frozenset({"OUR2"}))
    assert result.exclude_reason == "self_listing"


def test_classify_discovery_non_self_unaffected_by_empty_default():
    """self_item_ids 未指定 (デフォルト frozenset()) は既存の分岐に影響しない。"""
    signals = {
        "ebay_item_id": "OUR1",
        "competitor_item_id": "COMP1",
        "our_title": "Sony WH-1000XM5 Wireless Headphones Black",
        "competitor_title": "ソニー WH-1000XM5 ワイヤレスヘッドホン ブラック 美品",
        "our_price_usd": 200.0,
        "competitor_price_usd": 190.0,
    }
    result = classify_discovery(signals)  # self_item_ids 省略
    assert result.exclude_reason != "self_listing"
    assert result.needs_ai is True  # グレー (通常通り AI 判定へ)


def test_classify_discovery_self_item_ids_does_not_falsely_exclude_others():
    """self_item_ids に無関係な item_id が混じっていても誤って除外しない。"""
    signals = {
        "ebay_item_id": "OUR1",
        "competitor_item_id": "COMP1",
        "our_title": "Sony WH-1000XM5 Wireless Headphones Black",
        "competitor_title": "ソニー WH-1000XM5 ワイヤレスヘッドホン ブラック 美品",
        "our_price_usd": 200.0,
        "competitor_price_usd": 190.0,
    }
    result = classify_discovery(signals, self_item_ids=frozenset({"OUR2", "OUR3"}))
    assert result.exclude_reason != "self_listing"


# ────────────────────────────────────────────────────────────────
# classify_rival / classify_batch: 透過 + 永続化
# ────────────────────────────────────────────────────────────────

def test_classify_rival_self_listing_persists_and_skips_ai(tmp_db, monkeypatch):
    def _fail_if_called(*_a, **_kw):
        raise AssertionError("judge_rival は self_listing では呼ばれてはならない")

    import monitor.rival_classifier as rc
    monkeypatch.setattr(rc, "judge_rival", _fail_if_called)

    signals = {
        "ebay_item_id": "OUR1",
        "competitor_item_id": "OUR2",
        "our_title": "Sony WH-1000XM5",
        "competitor_title": "Sony WH-1000XM5",
    }
    result = classify_rival(
        signals, self_item_ids=frozenset({"OUR2"}),
        discovery_id=None, persist=True, shadow_mode=True,
    )
    assert result.classification == "noise"
    assert result.exclude_reason == "self_listing"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT classification, exclude_reason, would_be_eligible, shadow_mode "
            "FROM rival_classifications WHERE ebay_item_id=? AND competitor_item_id=?",
            ("OUR1", "OUR2"),
        ).fetchone()
    assert row is not None
    assert row["classification"] == "noise"
    assert row["exclude_reason"] == "self_listing"
    assert row["would_be_eligible"] == 0


def test_classify_batch_threads_self_item_ids(tmp_db):
    discoveries = [
        {
            "discovery_id": 1, "ebay_item_id": "OUR1", "competitor_item_id": "OUR2",
            "our_title": "Sony WH-1000XM5", "competitor_title": "Sony WH-1000XM5",
        },
        {
            "discovery_id": 2, "ebay_item_id": "OUR1", "competitor_item_id": "COMP1",
            "our_title": "Sony WH-1000XM5", "competitor_title": "全く別の商品タイトル XYZ",
        },
    ]
    results = classify_batch(
        discoveries, self_item_ids=frozenset({"OUR2"}), persist=True,
    )
    assert results[0].exclude_reason == "self_listing"
    assert results[1].exclude_reason != "self_listing"


# ────────────────────────────────────────────────────────────────
# tasks/task_rival_classify.run_rival_classify: 統合
# ────────────────────────────────────────────────────────────────

def test_run_rival_classify_self_excluded_dismissed_no_ai(tmp_db, monkeypatch):
    """self match の discovery は AI を呼ばず status='dismissed' + self_excluded 集計。"""
    with get_conn() as conn:
        _insert_listing(conn, "OUR1", title="Sony WH-1000XM5")
        _insert_listing(conn, "OUR2", title="Sony WH-1000XM5 (別 listing)")
        cur = conn.execute(
            """INSERT INTO listing_rival_discoveries
               (ebay_item_id, competitor_seller, competitor_item_id,
                competitor_title, competitor_price_usd, search_keyword, status)
               VALUES ('OUR1', 'mono_honpo_japan', 'OUR2',
                       'Sony WH-1000XM5 (別 listing)', 190.0, 'sony headphones', 'new')"""
        )
        discovery_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("self_listing で judge_rival が呼ばれた (AI コスト浪費)")

    import monitor.rival_classifier as rc
    monkeypatch.setattr(rc, "judge_rival", _fail_if_called)

    from tasks.task_rival_classify import run_rival_classify
    result = run_rival_classify({})

    assert result["success"] is True
    assert result["processed"] == 1
    assert result["noise"] == 1
    assert result["self_excluded"] == 1
    assert result["ai_calls_used"] == 0

    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM listing_rival_discoveries WHERE id=?", (discovery_id,)
        ).fetchone()
    assert row[0] == "dismissed"


def test_run_rival_classify_non_self_still_needs_ai(tmp_db, monkeypatch):
    """W308 追加後も、自己マッチでない grey ケースは従来通り AI 判定へ進む (regression)。"""
    with get_conn() as conn:
        _insert_listing(conn, "OUR1", title="Sony WH-1000XM5 Wireless Headphones Black")
        conn.execute(
            """INSERT INTO listing_rival_discoveries
               (ebay_item_id, competitor_seller, competitor_item_id,
                competitor_title, competitor_price_usd, search_keyword, status)
               VALUES ('OUR1', 'jp_seller_1', 'COMP1',
                       'ソニー WH-1000XM5 ワイヤレスヘッドホン ブラック 美品', 190.0,
                       'sony headphones', 'new')"""
        )

    calls = []

    def _fake_judge_rival(signals, model="unused"):
        calls.append(signals)
        return AIJudgeResult(
            same_product=True, variant_risk="none", condition="USED",
            confidence=0.95, reason="同一商品", ai_model="claude-haiku-4-5-20251001",
            route="ai",
        )

    import monitor.rival_classifier as rc
    monkeypatch.setattr(rc, "judge_rival", _fake_judge_rival)

    from tasks.task_rival_classify import run_rival_classify
    result = run_rival_classify({})

    assert result["real"] == 1
    assert result["self_excluded"] == 0
    assert len(calls) == 1


# ────────────────────────────────────────────────────────────────
# tasks/task_rival_detection.run_rival_per_listing_detection_one: 発見側
# ────────────────────────────────────────────────────────────────

@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_run_one_skips_self_item_id(mock_browse_cls, tmp_db):
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_watched",
            rival_watch_enabled=1,
            rival_search_keywords="maxell cassette tape",
        )
    mock_client = MagicMock()
    mock_client.search_items.return_value = [
        # 自社の別 listing が別セラー名 (config 未設定を模す) で紛れ込むケース
        {"seller": "mono_honpo_japan", "item_id": "v1|999999|0",
         "title": "自社出品と同一商品", "price_usd": 5.0},
        {"seller": "other_jp_seller", "item_id": "v1|111111|0",
         "title": "本物のライバル", "price_usd": 6.0},
    ]
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x"}}  # seller_id 未設定 (本番想定)
    res = run_rival_per_listing_detection_one(
        "eid_watched", cfg, sleep_between=0.0,
        self_item_ids=frozenset({"999999"}),
    )
    assert res["skipped_self_listing"] == 1
    assert res["new_discoveries"] == 1
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM listing_rival_discoveries WHERE competitor_item_id=?",
            ("999999",),
        ).fetchone()
    assert row[0] == 0


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_run_one_self_item_ids_none_default_unaffected(mock_browse_cls, tmp_db):
    """self_item_ids 省略 (None) は既存呼出側/テスト互換で従来通り動作する。"""
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_watched2",
            rival_watch_enabled=1,
            rival_search_keywords="maxell cassette tape",
        )
    mock_client = MagicMock()
    mock_client.search_items.return_value = [
        {"seller": "comp", "item_id": "v1|123|0", "title": "T", "price_usd": 1.0},
    ]
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x"}}
    res = run_rival_per_listing_detection_one("eid_watched2", cfg, sleep_between=0.0)
    assert res["skipped_self_listing"] == 0
    assert res["new_discoveries"] == 1


# ────────────────────────────────────────────────────────────────
# scripts/dismiss_self_discoveries_w308.py: one-shot
# ────────────────────────────────────────────────────────────────

def _seed_self_and_normal_discoveries(conn):
    _insert_listing(conn, "OUR1", title="Sony WH-1000XM5")
    _insert_listing(conn, "OUR2", title="Sony WH-1000XM5 (別 listing)")
    # 自己マッチ (未 dismiss)
    conn.execute(
        """INSERT INTO listing_rival_discoveries
           (ebay_item_id, competitor_seller, competitor_item_id,
            competitor_title, status) VALUES
           ('OUR1', 'mono_honpo_japan', 'OUR2', 'Sony WH-1000XM5 (別 listing)', 'new')"""
    )
    # 自己マッチだが既に dismissed 済み (対象外になるはず)
    conn.execute(
        """INSERT INTO listing_rival_discoveries
           (ebay_item_id, competitor_seller, competitor_item_id,
            competitor_title, status) VALUES
           ('OUR2', 'mono_honpo_japan', 'OUR1', 'Sony WH-1000XM5', 'dismissed')"""
    )
    # 正常な競合 (対象外)
    conn.execute(
        """INSERT INTO listing_rival_discoveries
           (ebay_item_id, competitor_seller, competitor_item_id,
            competitor_title, status) VALUES
           ('OUR1', 'jp_seller_1', 'COMP1', '本物のライバル', 'new')"""
    )


def test_dismiss_script_dry_run_no_writes(tmp_db):
    with get_conn() as conn:
        _seed_self_and_normal_discoveries(conn)

    from scripts.dismiss_self_discoveries_w308 import _run
    summary = _run(apply=False, limit=0)

    assert summary["target_count"] == 1  # 未 dismiss の自己マッチ 1 件のみ
    assert summary["apply"] is False

    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM listing_rival_discoveries WHERE competitor_item_id='OUR2'"
        ).fetchone()
    assert row[0] == "new"  # dry-run は書込ゼロ


def test_dismiss_script_apply_marks_dismissed(tmp_db):
    with get_conn() as conn:
        _seed_self_and_normal_discoveries(conn)

    from scripts.dismiss_self_discoveries_w308 import _run
    summary = _run(apply=True, limit=0)

    assert summary["dismissed_count"] == 1
    assert summary["failed_ids"] == []

    with get_conn() as conn:
        self_row = conn.execute(
            "SELECT status FROM listing_rival_discoveries WHERE competitor_item_id='OUR2'"
        ).fetchone()
        normal_row = conn.execute(
            "SELECT status FROM listing_rival_discoveries WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert self_row[0] == "dismissed"
    assert normal_row[0] == "new"  # 正常な競合は不変
