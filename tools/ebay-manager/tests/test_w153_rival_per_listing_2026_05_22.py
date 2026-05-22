"""W153 (2026-05-22): 商品別ライバル検出 — pytest.

設計書: .company/engineering/docs/2026-05-22-W153-rival-per-listing-detection-design.md (v2.1)

CRITICAL path tests covering v2.1 HIGH fixes:
- H-A: anchor (rival_watch_started_at) preservation against late initial_registration
- H-B: drift recovery (schema_ver 独立)
- H-C: add_or_reactivate_competitor (3 action: added/reactivated/conflict)
- H-D: errors>0 → success=False (no fake success)
- H-E: 0 listings weekly reminder
- H-F: Haiku output filter (apology/numbering/word-count)
- H-G: bad item_id counter
- H-H: max_requests_per_run early break + 429 backoff
- v2.1 HIGH-3: counter decrement BEFORE call (failed retry includes)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


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


# ============================================================
# Section 1: migration v50 冪等性 + drift recovery (Q2 / H-B)
# ============================================================

def test_v50_idempotent_init_db_twice_retains_data(tmp_db):
    """Q2: init_db() 2 回連続 + データ投入 → 列 + 値が消えない."""
    from monitor.database import get_conn, set_rival_watch_enabled
    with get_conn() as conn:
        _insert_listing(conn, "test123")
    set_rival_watch_enabled("test123", True)
    tmp_db.init_db()  # 2 回目
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rival_watch_enabled, rival_watch_started_at "
            "FROM ebay_listings WHERE ebay_item_id = ?", ("test123",),
        ).fetchone()
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert row["rival_watch_enabled"] == 1
    assert row["rival_watch_started_at"] is not None
    assert ver >= 50


def test_v50_self_heal_when_table_missing(tmp_db):
    """drift recovery: listing_rival_discoveries DROP → init_db 再実行で復活."""
    from monitor.database import get_conn
    with get_conn() as conn:
        conn.execute("DROP TABLE listing_rival_discoveries")
        conn.execute("PRAGMA user_version = 49")  # rollback version too
    tmp_db.init_db()
    with get_conn() as conn:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='listing_rival_discoveries'"
        ).fetchone()
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert has is not None
    assert ver >= 50


def test_v50_drift_recovery_at_version_50(tmp_db):
    """H-B: user_version=50 でも列・table 欠損なら drift recovery 発動."""
    from monitor.database import get_conn
    with get_conn() as conn:
        # column drop simulation: SQLite では DROP COLUMN 不可なので
        # table 全 drop + ver=50 のまま放置で drift シミュレート
        conn.execute("DROP TABLE listing_rival_discoveries")
        # user_version は 50 のまま (drift state)
    tmp_db.init_db()
    with get_conn() as conn:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='listing_rival_discoveries'"
        ).fetchone()
    assert has is not None, "drift recovery should re-create the table"


# ============================================================
# Section 2: DB helpers (H-A / H-C / MED-6)
# ============================================================

def test_set_rival_watch_enabled_on_sets_started_at(tmp_db):
    """H-A: ON 時に rival_watch_started_at = NOW set."""
    from monitor.database import get_conn, set_rival_watch_enabled
    with get_conn() as conn:
        _insert_listing(conn, "eid1")
    ok = set_rival_watch_enabled("eid1", True)
    assert ok is True
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rival_watch_enabled, rival_watch_started_at "
            "FROM ebay_listings WHERE ebay_item_id = ?", ("eid1",),
        ).fetchone()
    assert row["rival_watch_enabled"] == 1
    assert row["rival_watch_started_at"] is not None


def test_set_rival_watch_enabled_off_preserves_started_at(tmp_db):
    """H-A: OFF 時に rival_watch_started_at は維持 (NULL に戻さない)."""
    from monitor.database import get_conn, set_rival_watch_enabled
    with get_conn() as conn:
        _insert_listing(conn, "eid2")
    set_rival_watch_enabled("eid2", True)
    with get_conn() as conn:
        before = conn.execute(
            "SELECT rival_watch_started_at FROM ebay_listings "
            "WHERE ebay_item_id = ?", ("eid2",),
        ).fetchone()["rival_watch_started_at"]
    set_rival_watch_enabled("eid2", False)
    with get_conn() as conn:
        after = conn.execute(
            "SELECT rival_watch_enabled, rival_watch_started_at "
            "FROM ebay_listings WHERE ebay_item_id = ?", ("eid2",),
        ).fetchone()
    assert after["rival_watch_enabled"] == 0
    assert after["rival_watch_started_at"] == before  # 維持


def test_set_rival_watch_enabled_re_on_preserves_original_anchor(tmp_db):
    """v2.1 設計判断: OFF→再 ON で COALESCE が元 anchor を保持."""
    from monitor.database import get_conn, set_rival_watch_enabled
    with get_conn() as conn:
        _insert_listing(conn, "eid3")
    set_rival_watch_enabled("eid3", True)
    with get_conn() as conn:
        anchor1 = conn.execute(
            "SELECT rival_watch_started_at FROM ebay_listings "
            "WHERE ebay_item_id = ?", ("eid3",),
        ).fetchone()["rival_watch_started_at"]
    set_rival_watch_enabled("eid3", False)
    set_rival_watch_enabled("eid3", True)  # 再 ON
    with get_conn() as conn:
        anchor2 = conn.execute(
            "SELECT rival_watch_started_at FROM ebay_listings "
            "WHERE ebay_item_id = ?", ("eid3",),
        ).fetchone()["rival_watch_started_at"]
    assert anchor1 == anchor2  # 再 ON で巻き戻さない


def test_set_rival_watch_enabled_returns_false_on_missing(tmp_db):
    from monitor.database import set_rival_watch_enabled
    assert set_rival_watch_enabled("nonexistent", True) is False


def test_set_rival_search_keywords_normalizes_blank_lines(tmp_db):
    from monitor.database import get_conn, set_rival_search_keywords
    with get_conn() as conn:
        _insert_listing(conn, "eid_kw")
    set_rival_search_keywords("eid_kw", "  a\n\nb\n  c  \n", mark_generated=False)
    with get_conn() as conn:
        kw = conn.execute(
            "SELECT rival_search_keywords FROM ebay_listings "
            "WHERE ebay_item_id = ?", ("eid_kw",),
        ).fetchone()["rival_search_keywords"]
    assert kw == "a\nb\nc"


def test_set_rival_search_keywords_mark_generated_sets_timestamp(tmp_db):
    from monitor.database import get_conn, set_rival_search_keywords
    with get_conn() as conn:
        _insert_listing(conn, "eid_kw2")
    set_rival_search_keywords("eid_kw2", "a b c", mark_generated=True)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rival_search_keywords_generated_at FROM ebay_listings "
            "WHERE ebay_item_id = ?", ("eid_kw2",),
        ).fetchone()
    assert row["rival_search_keywords_generated_at"] is not None


def test_record_rival_discovery_new_returns_id(tmp_db):
    from monitor.database import get_conn, record_rival_discovery
    with get_conn() as conn:
        _insert_listing(conn, "eid_d1")
    rid = record_rival_discovery(
        ebay_item_id="eid_d1",
        competitor_seller="seller_a",
        competitor_item_id="111",
        competitor_title="Test item",
        competitor_price_usd=99.99,
        search_keyword="kw",
    )
    assert rid is not None
    assert rid > 0


def test_record_rival_discovery_existing_returns_none_and_updates_last_seen(tmp_db):
    """UNIQUE(eid, seller, item_id) で 2 回目は None 返却 + last_seen 更新."""
    from monitor.database import get_conn, record_rival_discovery
    with get_conn() as conn:
        _insert_listing(conn, "eid_d2")
    r1 = record_rival_discovery(
        ebay_item_id="eid_d2", competitor_seller="s",
        competitor_item_id="2", competitor_price_usd=10.0,
    )
    r2 = record_rival_discovery(
        ebay_item_id="eid_d2", competitor_seller="s",
        competitor_item_id="2", competitor_price_usd=20.0,
    )
    assert r1 is not None
    assert r2 is None  # dedupe
    with get_conn() as conn:
        row = conn.execute(
            "SELECT competitor_price_usd FROM listing_rival_discoveries "
            "WHERE ebay_item_id = ?", ("eid_d2",),
        ).fetchone()
    assert row["competitor_price_usd"] == 20.0  # 更新


def test_get_rival_discoveries_since_filter(tmp_db):
    """since より前の discovery は返さない. SQLite CURRENT_TIMESTAMP は UTC (sqlite-timezone.md).
    本テストの since も UTC で渡す.
    """
    from datetime import datetime, timedelta, timezone
    from monitor.database import get_conn, record_rival_discovery, get_rival_discoveries
    with get_conn() as conn:
        _insert_listing(conn, "eid_s")
    record_rival_discovery(
        ebay_item_id="eid_s", competitor_seller="s",
        competitor_item_id="1",
    )
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    # since = 1 hour future → 0 件
    future = (now_utc + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    rows = get_rival_discoveries("eid_s", status='new', since=future)
    assert len(rows) == 0
    # since = 1 hour past → 1 件
    past = (now_utc - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    rows = get_rival_discoveries("eid_s", status='new', since=past)
    assert len(rows) == 1


def test_update_rival_discovery_status_validates_value(tmp_db):
    from monitor.database import update_rival_discovery_status
    with pytest.raises(ValueError):
        update_rival_discovery_status(1, "invalid_status")


def test_add_or_reactivate_added_returns_added_action(tmp_db):
    """H-C: 新規 INSERT で action='added'."""
    from monitor.database import add_or_reactivate_competitor
    new_id, action = add_or_reactivate_competitor(
        our_item_id="our_1", our_sku="stock:01",
        competitor_seller="comp_seller", competitor_item_id="comp_1",
    )
    assert new_id > 0
    assert action == 'added'


def test_add_or_reactivate_same_listing_reactivates(tmp_db):
    """H-C: 同 our_item_id で is_active=0 → 1 復活 + action='reactivated'.
    v2.1 MED-6: our_sku も更新される.
    """
    from monitor.database import get_conn, add_or_reactivate_competitor
    add_or_reactivate_competitor(
        our_item_id="our_2", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_2",
    )
    with get_conn() as conn:
        conn.execute(
            "UPDATE competitor_products SET is_active = 0 "
            "WHERE competitor_item_id = ?", ("comp_2",),
        )
    # 再追加 (different our_sku)
    rid, action = add_or_reactivate_competitor(
        our_item_id="our_2", our_sku="stock:02",
        competitor_seller="s", competitor_item_id="comp_2",
    )
    assert action == 'reactivated'
    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_active, our_sku FROM competitor_products "
            "WHERE id = ?", (rid,),
        ).fetchone()
    assert row["is_active"] == 1
    assert row["our_sku"] == "stock:02"  # MED-6: 更新確認


def test_add_or_reactivate_other_listing_returns_conflict(tmp_db):
    """H-C: 別 our_item_id で既登録 → action='conflict'."""
    from monitor.database import add_or_reactivate_competitor
    add_or_reactivate_competitor(
        our_item_id="our_3", our_sku="",
        competitor_seller="s", competitor_item_id="comp_3",
    )
    rid, action = add_or_reactivate_competitor(
        our_item_id="our_4_different", our_sku="",
        competitor_seller="s", competitor_item_id="comp_3",
    )
    assert action == 'conflict'


# ============================================================
# Section 3: rival_keyword_generator (H-F)
# ============================================================

@patch("monitor.rival_keyword_generator.anthropic.Anthropic")
def test_generate_keywords_returns_3_to_5(mock_anthropic_cls, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from monitor.rival_keyword_generator import generate_keywords
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=(
        "Ohuhu PEN 320 FINE\n"
        "Ohuhu marker pen 320pcs\n"
        "Ohuhu illustration marker fine"
    ))]
    mock_anthropic_cls.return_value.messages.create.return_value = mock_msg
    result = generate_keywords(title="Ohuhu Fine Tip Pen 320 colors")
    assert 3 <= len(result) <= 5
    assert all(3 <= len(c.split()) <= 6 for c in result)


@patch("monitor.rival_keyword_generator.anthropic.Anthropic")
def test_haiku_apology_filter(mock_anthropic_cls, monkeypatch):
    """H-F: 'I cannot' / 'sorry' / numbered → reject. <3 valid で raise."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from monitor.rival_keyword_generator import generate_keywords
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=(
        "I cannot answer this question\n"
        "Sorry I don't know\n"
        "1. numbered line is rejected\n"
        "Ohuhu PEN 320 FINE\n"
        "Ohuhu marker pen kit"
    ))]
    mock_anthropic_cls.return_value.messages.create.return_value = mock_msg
    with pytest.raises(ValueError, match="only 2 valid"):
        generate_keywords(title="Ohuhu Fine Tip Pen 320 colors")


@patch("monitor.rival_keyword_generator.anthropic.Anthropic")
def test_generate_keywords_filters_wrong_word_count(mock_anthropic_cls, monkeypatch):
    """1 word / 7 words はreject (3-6 enforce)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from monitor.rival_keyword_generator import generate_keywords
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=(
        "Single\n"  # 1 word, reject
        "This is a sentence with seven words total\n"  # 8 words, reject
        "Ohuhu PEN 320 FINE\n"  # 4 words OK
        "Ohuhu marker kit\n"  # 3 words OK
        "another good candidate here\n"  # 4 words OK
    ))]
    mock_anthropic_cls.return_value.messages.create.return_value = mock_msg
    result = generate_keywords(title="x")
    assert len(result) == 3  # only the 3 valid


def test_generate_keywords_raises_on_missing_api_key(monkeypatch):
    monkeypatch.delenv("EBAY_ANTHROPIC_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from monitor.rival_keyword_generator import generate_keywords
    with pytest.raises(RuntimeError, match="API key not set"):
        generate_keywords(title="x")


@patch("monitor.rival_keyword_generator.anthropic.Anthropic")
def test_generate_keywords_uses_ebay_anthropic_key_first(
    mock_anthropic_cls, monkeypatch,
):
    """H-F: EBAY_ANTHROPIC_KEY 優先 → ANTHROPIC_API_KEY fallback."""
    monkeypatch.setenv("EBAY_ANTHROPIC_KEY", "key1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key2")
    from monitor.rival_keyword_generator import generate_keywords
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="a b c\nd e f\ng h i")]
    mock_anthropic_cls.return_value.messages.create.return_value = mock_msg
    generate_keywords(title="x")
    mock_anthropic_cls.assert_called_with(api_key="key1")


# ============================================================
# Section 4: detection task (H-D / H-E / H-G / H-H / v2.1 HIGH-3)
# ============================================================

def test_run_one_keyword_empty_increments_errors(tmp_db):
    """H-D: 空 keyword で errors++ + success=False."""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(conn, "eid_emp", rival_watch_enabled=1)
    res = run_rival_per_listing_detection_one(
        "eid_emp", {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}},
    )
    assert res["errors"] >= 1
    assert res["success"] is False


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_run_one_inserts_discoveries(mock_browse_cls, tmp_db):
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_ins",
            rival_watch_enabled=1,
            rival_search_keywords="test kw",
        )
    mock_client = MagicMock()
    mock_client.search_items.return_value = [
        {"seller": "comp_a", "item_id": "v1|111|0",
         "title": "Test", "price_usd": 10.0},
        {"seller": "comp_b", "item_id": "v1|222|0",
         "title": "Test2", "price_usd": 20.0},
    ]
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    res = run_rival_per_listing_detection_one("eid_ins", cfg, sleep_between=0.0)
    assert res["success"] is True
    assert res["new_discoveries"] == 2
    assert res["errors"] == 0


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_run_one_skips_own_seller(mock_browse_cls, tmp_db):
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_self",
            rival_watch_enabled=1,
            rival_search_keywords="kw",
        )
    mock_client = MagicMock()
    mock_client.search_items.return_value = [
        {"seller": "me", "item_id": "v1|111|0", "title": "T", "price_usd": 1},
        {"seller": "other", "item_id": "v1|222|0", "title": "T2", "price_usd": 2},
    ]
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    res = run_rival_per_listing_detection_one("eid_self", cfg, sleep_between=0.0)
    assert res["new_discoveries"] == 1  # me 除外


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_skipped_bad_item_id_counter(mock_browse_cls, tmp_db, caplog):
    """H-G: item_id 空の Browse result → skipped_bad_item_id +=1 + WARNING."""
    import logging
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_bad",
            rival_watch_enabled=1,
            rival_search_keywords="kw",
        )
    mock_client = MagicMock()
    mock_client.search_items.return_value = [
        {"seller": "comp", "item_id": "", "title": "Bad", "price_usd": 1},
        {"seller": "comp2", "item_id": "v1|333|0", "title": "Good", "price_usd": 2},
    ]
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    with caplog.at_level(logging.WARNING):
        res = run_rival_per_listing_detection_one(
            "eid_bad", cfg, sleep_between=0.0,
        )
    assert res["skipped_bad_item_id"] == 1
    assert res["new_discoveries"] == 1
    assert any("without competitor_item_id" in r.message for r in caplog.records)


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_requests_counter_decrements_before_call(mock_browse_cls, tmp_db):
    """v2.1 HIGH-3: counter は試行 *前* に decrement (failed retry も含む)."""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_cnt",
            rival_watch_enabled=1,
            rival_search_keywords="k1\nk2",
        )
    mock_client = MagicMock()
    # 1st call raises 5xx → 3 retries → all 5xx → fail
    # 2nd call succeeds
    import httpx
    err = httpx.RequestError("500 Server Error")
    mock_client.search_items.side_effect = [err, err, err, [
        {"seller": "c", "item_id": "v1|1|0", "title": "T", "price_usd": 1}
    ]]
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    # Patch sleep to speed up test
    with patch("tasks.task_rival_detection.time.sleep"):
        res = run_rival_per_listing_detection_one(
            "eid_cnt", cfg, sleep_between=0.0,
        )
    # 3 retries for k1 (all 5xx) + 1 call for k2 = 4 attempts
    assert res["requests_used"] == 4
    assert res["errors"] == 1  # k1 fully failed


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_max_requests_per_run_early_break(mock_browse_cls, tmp_db):
    """H-H: max_requests_per_run で early break."""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_detection
    with get_conn() as conn:
        # 3 listings × 5 keywords each = 15 calls potential
        for i in range(3):
            _insert_listing(
                conn, f"eid_q{i}",
                rival_watch_enabled=1,
                rival_search_keywords="k1\nk2\nk3\nk4\nk5",
            )
    mock_client = MagicMock()
    mock_client.search_items.return_value = []
    mock_browse_cls.return_value = mock_client
    cfg = {
        "ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"},
        "tasks_enabled": {"rival_detection": {
            "max_listings_per_run": 30,
            "max_requests_per_run": 5,
        }},
    }
    with patch("tasks.task_rival_detection.time.sleep"):
        res = run_rival_detection(cfg)
    # 5 calls 消費後 early break
    assert res["requests_used"] == 5


def test_run_rival_detection_zero_listings_returns_success_with_message(tmp_db):
    """0 listings (rival_watch_enabled=1 が 0 件) → success=True で reminder ガード経路."""
    from tasks.task_rival_detection import run_rival_detection
    cfg = {"discord": {"webhook_url": ""}}  # webhook なしで reminder skip
    res = run_rival_detection(cfg)
    assert res["success"] is True
    assert "0 listings" in res["message"]


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_errors_gt_zero_sets_success_false(mock_browse_cls, tmp_db):
    """H-D: errors>0 で全体 success=False (空 keyword listing 経由)."""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_detection
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_ok",
            rival_watch_enabled=1,
            rival_search_keywords="kw",
        )
        _insert_listing(
            conn, "eid_empty",
            rival_watch_enabled=1,
            rival_search_keywords=None,  # 空 → errors++
        )
    mock_client = MagicMock()
    mock_client.search_items.return_value = []
    mock_browse_cls.return_value = mock_client
    cfg = {
        "ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"},
        "discord": {"webhook_url": ""},  # webhook なしで notify skip
        "tasks_enabled": {"rival_detection": {
            "max_listings_per_run": 30,
            "max_requests_per_run": 150,
        }},
    }
    with patch("tasks.task_rival_detection.time.sleep"):
        res = run_rival_detection(cfg)
    assert res["errors"] >= 1
    assert res["success"] is False  # H-D


# ============================================================
# code-reviewer regression tests (HIGH-1 / HIGH-3 / HIGH-4 fix verify)
# ============================================================

@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_run_one_retries_on_http_502(mock_browse_cls, tmp_db):
    """code-reviewer HIGH-1: httpx.HTTPStatusError 502 が retry 経路に乗る.

    旧実装の `msg[:3].startswith("5")` は `"Server error '502 ...'"` で False に
    なって retry されず即 errors++ break (silent gap). status_code 直接判定で根治.
    """
    import httpx
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid502",
            rival_watch_enabled=1,
            rival_search_keywords="k1",
        )
    mock_client = MagicMock()
    req = httpx.Request("GET", "https://example.com")
    resp = httpx.Response(502, request=req)
    err = httpx.HTTPStatusError(
        "Server error '502 Bad Gateway'", request=req, response=resp,
    )
    mock_client.search_items.side_effect = [err, err, err]
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    with patch("tasks.task_rival_detection.time.sleep"):
        res = run_rival_per_listing_detection_one(
            "eid502", cfg, sleep_between=0.0,
        )
    assert res["requests_used"] == 3, "全 3 retry が走るべき (HIGH-1 retry path verify)"
    assert res["errors"] == 1, "最終的に retry exhausted で error 1"


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_run_one_does_not_retry_on_http_401(mock_browse_cls, tmp_db):
    """code-reviewer HIGH-1: 4xx (non-transient) は retry しない."""
    import httpx
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid401",
            rival_watch_enabled=1,
            rival_search_keywords="k1",
        )
    mock_client = MagicMock()
    req = httpx.Request("GET", "https://example.com")
    resp = httpx.Response(401, request=req)
    err = httpx.HTTPStatusError(
        "Client error '401 Unauthorized'", request=req, response=resp,
    )
    mock_client.search_items.side_effect = [err]
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    with patch("tasks.task_rival_detection.time.sleep"):
        res = run_rival_per_listing_detection_one(
            "eid401", cfg, sleep_between=0.0,
        )
    # 401 は non-transient = 1 回で break
    assert res["requests_used"] == 1
    assert res["errors"] == 1


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_max_requests_budget_consumed_across_listings(mock_browse_cls, tmp_db):
    """code-reviewer HIGH-3: 複数 listing 跨ぎで max_requests budget が cumulative.

    3 listings × 各 1 keyword、max_requests=2 → listing #1 + #2 で 2 calls 消費、
    listing #3 は一切 search_items 呼ばれない.
    """
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_detection
    with get_conn() as conn:
        for i in range(3):
            _insert_listing(
                conn, f"eid_x{i}",
                rival_watch_enabled=1,
                rival_search_keywords="kw_only",
            )
    mock_client = MagicMock()
    mock_client.search_items.return_value = []
    mock_browse_cls.return_value = mock_client
    cfg = {
        "ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"},
        "discord": {"webhook_url": ""},
        "tasks_enabled": {"rival_detection": {
            "max_listings_per_run": 30,
            "max_requests_per_run": 2,
        }},
    }
    with patch("tasks.task_rival_detection.time.sleep"):
        res = run_rival_detection(cfg)
    # 2 calls 消費後 listing #3 は skip
    assert res["requests_used"] == 2
    # search_items は 2 回呼ばれた (listing #3 は 0 回)
    assert mock_client.search_items.call_count == 2


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_no_sleep_after_max_requests_break(mock_browse_cls, tmp_db):
    """code-reviewer HIGH-4: early break path で末尾 sleep skip.

    max_requests=0 で即 break → sleep_between=999 を渡しても sleep 呼ばれない.
    """
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_brk",
            rival_watch_enabled=1,
            rival_search_keywords="k1\nk2",
        )
    mock_client = MagicMock()
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    with patch("tasks.task_rival_detection.time.sleep") as mock_sleep:
        res = run_rival_per_listing_detection_one(
            "eid_brk", cfg, sleep_between=999.0,
            max_requests_remaining=0,  # 即 break
        )
    # 末尾 sleep (sleep_between=999) は skip された
    sleep_calls = [c for c in mock_sleep.call_args_list if c.args[0] == 999.0]
    assert len(sleep_calls) == 0, "early break 後に末尾 sleep が走ってはいけない"
    assert res["_skip_final_sleep"] is True
