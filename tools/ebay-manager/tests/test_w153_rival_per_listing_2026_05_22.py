"""W153 (2026-05-22): 商品別ライバル検出 — pytest.

設計書: .company/engineering/docs/2026-05-22-W153-rival-per-listing-detection-design.md (v2.1)

【v2 2026-05-22 PM 改訂】: 「3-5 candidate 改行区切り → 各々別 query union」設計を
**空白区切り 1 query AND 検索** に変更. user 視認で「Black 単独 50 件 noise」発覚.

CRITICAL path tests covering v2.1 HIGH fixes (継続) + v2 単 query refactor:
- H-A: anchor (rival_watch_started_at) preservation against late initial_registration
- H-B: drift recovery (schema_ver 独立)
- H-C: add_or_reactivate_competitor (3 action: added/reactivated/conflict)
- H-D: errors>0 → success=False (no fake success)
- H-E: 0 listings weekly reminder
- H-F: Haiku output filter (apology/numbering/word-count)
- H-G: bad item_id counter
- H-H: max_requests_per_run early break + 429 backoff
- v2: 改行→空白 normalize、1 word query reject、1 listing = 1 API call
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
        conn.execute("PRAGMA user_version = 49")
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
        conn.execute("DROP TABLE listing_rival_discoveries")
    tmp_db.init_db()
    with get_conn() as conn:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='listing_rival_discoveries'"
        ).fetchone()
    assert has is not None, "drift recovery should re-create the table"


# ============================================================
# Section 2: DB helpers (H-A / H-C / MED-6 / v2 normalize)
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
    assert after["rival_watch_started_at"] == before


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
    set_rival_watch_enabled("eid3", True)
    with get_conn() as conn:
        anchor2 = conn.execute(
            "SELECT rival_watch_started_at FROM ebay_listings "
            "WHERE ebay_item_id = ?", ("eid3",),
        ).fetchone()["rival_watch_started_at"]
    assert anchor1 == anchor2


def test_set_rival_watch_enabled_returns_false_on_missing(tmp_db):
    from monitor.database import set_rival_watch_enabled
    assert set_rival_watch_enabled("nonexistent", True) is False


def test_set_rival_search_keywords_normalizes_to_space_single_query(tmp_db):
    """v2: 改行・連続空白を単一空白に collapse (1 query AND 検索用)."""
    from monitor.database import get_conn, set_rival_search_keywords
    with get_conn() as conn:
        _insert_listing(conn, "eid_kw")
    set_rival_search_keywords(
        "eid_kw", "  maxell\n\nMXCP-P100\n  Cassette  \n", mark_generated=False,
    )
    with get_conn() as conn:
        kw = conn.execute(
            "SELECT rival_search_keywords FROM ebay_listings "
            "WHERE ebay_item_id = ?", ("eid_kw",),
        ).fetchone()["rival_search_keywords"]
    assert kw == "maxell MXCP-P100 Cassette"


def test_set_rival_search_keywords_self_heals_legacy_newlines(tmp_db):
    """v2: 過去 data (\\n 区切り) を再保存で空白化."""
    from monitor.database import get_conn, set_rival_search_keywords
    with get_conn() as conn:
        _insert_listing(conn, "eid_legacy")
        # 旧 data 直接書込シミュレート
        conn.execute(
            "UPDATE ebay_listings SET rival_search_keywords = ? "
            "WHERE ebay_item_id = ?",
            ("maxell\nMXCP-P100\nBlack", "eid_legacy"),
        )
    # 同じ legacy 文字列で再保存 → normalize される
    set_rival_search_keywords(
        "eid_legacy", "maxell\nMXCP-P100\nBlack", mark_generated=False,
    )
    with get_conn() as conn:
        kw = conn.execute(
            "SELECT rival_search_keywords FROM ebay_listings "
            "WHERE ebay_item_id = ?", ("eid_legacy",),
        ).fetchone()["rival_search_keywords"]
    assert kw == "maxell MXCP-P100 Black"


def test_set_rival_search_keywords_refuses_single_word(tmp_db):
    """HIGH-2 regression (internal review 2026-05-22 PM):
    1-word query は DB 保存層で reject (UI ガード突破時の防衛).

    user が UI の 💾 ボタンで 'Black' 1 word を保存しようとしたら、戻り値 False で
    DB 値は元のまま (上書きされない). 過去 user 視認バグ (Black 単独 50 件 noise)
    の再発防止 + 第 7 次「内部+Codex で money-direct silent gap 突破」事例.
    """
    from monitor.database import get_conn, set_rival_search_keywords
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_1w_save",
            rival_search_keywords="initial query here",
        )
    ok = set_rival_search_keywords(
        "eid_1w_save", "Black", mark_generated=False,
    )
    assert ok is False, "1-word query should be rejected at DB layer"
    with get_conn() as conn:
        kw = conn.execute(
            "SELECT rival_search_keywords FROM ebay_listings "
            "WHERE ebay_item_id = ?", ("eid_1w_save",),
        ).fetchone()["rival_search_keywords"]
    assert kw == "initial query here", "元の値が保たれている (上書きされていない)"


def test_set_rival_search_keywords_allows_empty_string_for_delete(tmp_db):
    """HIGH-2 regression: 空文字列は許可 (user が keyword を消去 = 検索停止 UX)."""
    from monitor.database import get_conn, set_rival_search_keywords
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_empty_save",
            rival_search_keywords="initial",
        )
    ok = set_rival_search_keywords(
        "eid_empty_save", "", mark_generated=False,
    )
    assert ok is True
    with get_conn() as conn:
        kw = conn.execute(
            "SELECT rival_search_keywords FROM ebay_listings "
            "WHERE ebay_item_id = ?", ("eid_empty_save",),
        ).fetchone()["rival_search_keywords"]
    assert kw == ""


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
    assert r2 is None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT competitor_price_usd FROM listing_rival_discoveries "
            "WHERE ebay_item_id = ?", ("eid_d2",),
        ).fetchone()
    assert row["competitor_price_usd"] == 20.0


def test_get_rival_discoveries_since_filter(tmp_db):
    """since より前の discovery は返さない. SQLite CURRENT_TIMESTAMP は UTC."""
    from datetime import datetime, timedelta, timezone
    from monitor.database import get_conn, record_rival_discovery, get_rival_discoveries
    with get_conn() as conn:
        _insert_listing(conn, "eid_s")
    record_rival_discovery(
        ebay_item_id="eid_s", competitor_seller="s",
        competitor_item_id="1",
    )
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    future = (now_utc + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    rows = get_rival_discoveries("eid_s", status='new', since=future)
    assert len(rows) == 0
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
    """H-C: 同 our_item_id で is_active=0 → 1 復活 + action='reactivated'."""
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
    assert row["our_sku"] == "stock:02"


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
# Section 3: rival_keyword_generator (H-F / v2 単 query 化)
# ============================================================

@patch("monitor.rival_keyword_generator.anthropic.Anthropic")
def test_generate_keywords_returns_single_str(mock_anthropic_cls, monkeypatch):
    """v2: 単一 str (3-8 word 空白区切り) を返す."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from monitor.rival_keyword_generator import generate_keywords
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="Ohuhu PEN 320 marker")]
    mock_anthropic_cls.return_value.messages.create.return_value = mock_msg
    result = generate_keywords(title="Ohuhu Fine Tip Pen 320 colors")
    assert isinstance(result, str)
    words = result.split(" ")
    assert 3 <= len(words) <= 8


@patch("monitor.rival_keyword_generator.anthropic.Anthropic")
def test_generate_keywords_picks_first_valid_line(mock_anthropic_cls, monkeypatch):
    """v2: Haiku が複数行返した場合は最初の valid 行を採用 (defensive)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from monitor.rival_keyword_generator import generate_keywords
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=(
        "1. numbered line skip\n"
        "Ohuhu PEN 320 marker\n"
        "another line"
    ))]
    mock_anthropic_cls.return_value.messages.create.return_value = mock_msg
    result = generate_keywords(title="x")
    assert result == "Ohuhu PEN 320 marker"


@patch("monitor.rival_keyword_generator.anthropic.Anthropic")
def test_generate_keywords_collapses_internal_spaces(mock_anthropic_cls, monkeypatch):
    """v2: Haiku が連続空白を返したら 1 空白に collapse."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from monitor.rival_keyword_generator import generate_keywords
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="maxell  MXCP-P100   Cassette")]  # 連続空白
    mock_anthropic_cls.return_value.messages.create.return_value = mock_msg
    result = generate_keywords(title="x")
    assert result == "maxell MXCP-P100 Cassette"
    assert "  " not in result


@patch("monitor.rival_keyword_generator.anthropic.Anthropic")
def test_haiku_apology_filter(mock_anthropic_cls, monkeypatch):
    """H-F: apology / numbered のみ → no valid → ValueError."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from monitor.rival_keyword_generator import generate_keywords
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=(
        "I cannot answer this question\n"
        "Sorry I don't know\n"
        "1. numbered line"
    ))]
    mock_anthropic_cls.return_value.messages.create.return_value = mock_msg
    with pytest.raises(ValueError, match="no valid query line"):
        generate_keywords(title="x")


@patch("monitor.rival_keyword_generator.anthropic.Anthropic")
def test_generate_keywords_rejects_wrong_word_count(mock_anthropic_cls, monkeypatch):
    """v2: 1 word / 9 word は reject、3-8 word のみ採用."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from monitor.rival_keyword_generator import generate_keywords
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=(
        "Single\n"  # 1 word, reject
        "way too many words in this single one query line\n"  # 9 words, reject
        "Ohuhu PEN 320 marker\n"  # 4 words OK
        "ignored after first valid"
    ))]
    mock_anthropic_cls.return_value.messages.create.return_value = mock_msg
    result = generate_keywords(title="x")
    assert result == "Ohuhu PEN 320 marker"


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
    mock_msg.content = [MagicMock(text="a b c")]
    mock_anthropic_cls.return_value.messages.create.return_value = mock_msg
    generate_keywords(title="x")
    mock_anthropic_cls.assert_called_with(api_key="key1")


# ============================================================
# Section 4: detection task (H-D / H-E / H-G / H-H / v2 1-query)
# ============================================================

def test_run_one_empty_query_is_skipped(tmp_db):
    """W153-UX (2026-05-26 Codex 推奨): 空 query は errors ではなく
    skipped_keywords_null として扱い、success=True を返す.
    旧 (H-D): errors++ + success=False (毎朝 first_err 騒音化)
    新: skipped_keywords_null++ + success=True (UI 生成待ちの半有効状態)"""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(conn, "eid_emp", rival_watch_enabled=1)
    res = run_rival_per_listing_detection_one(
        "eid_emp", {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}},
    )
    assert res["errors"] == 0
    assert res["skipped_keywords_null"] == 1
    assert res["success"] is True
    assert "keywords NULL" in res["message"]


def test_run_one_single_word_query_refused(tmp_db):
    """v2: 1 word query は AND 検索成立せず noise 過多 → 拒否."""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_1w",
            rival_watch_enabled=1,
            rival_search_keywords="Black",  # 1 word
        )
    res = run_rival_per_listing_detection_one(
        "eid_1w",
        {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}},
    )
    assert res["errors"] >= 1
    assert res["success"] is False
    assert "too short" in res["message"]


def test_run_one_normalizes_legacy_newline_query(tmp_db, monkeypatch):
    """v2: 旧 \\n 区切り data も runtime で空白化して 1 query AND 検索."""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_norm",
            rival_watch_enabled=1,
        )
        # 旧形式 \n 区切り data を直接書込
        conn.execute(
            "UPDATE ebay_listings SET rival_search_keywords = ? "
            "WHERE ebay_item_id = ?",
            ("maxell\nMXCP-P100\nCassette", "eid_norm"),
        )
    captured = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def search_items(self, *, query, **kw):
            captured["query"] = query
            return []

    monkeypatch.setattr(
        "tasks.ebay_browse_api.BrowseAPIClient", FakeClient,
    )
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    res = run_rival_per_listing_detection_one("eid_norm", cfg, sleep_between=0.0)
    assert captured["query"] == "maxell MXCP-P100 Cassette"
    assert res["success"] is True


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_run_one_inserts_discoveries(mock_browse_cls, tmp_db):
    """v52: 1 listing = 1 search + N enrich API calls (新規 N 件)."""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_ins",
            rival_watch_enabled=1,
            rival_search_keywords="maxell MXCP-P100 cassette",
        )
    mock_client = MagicMock()
    mock_client.search_items.return_value = [
        {"seller": "comp_a", "item_id": "v1|111|0",
         "title": "Test", "price_usd": 10.0},
        {"seller": "comp_b", "item_id": "v1|222|0",
         "title": "Test2", "price_usd": 20.0},
    ]
    # v52: enrich path で None を返して skip (test では detail 不要)
    mock_client.get_item_pricing.return_value = None
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    res = run_rival_per_listing_detection_one("eid_ins", cfg, sleep_between=0.0)
    assert res["success"] is True
    assert res["new_discoveries"] == 2
    assert res["errors"] == 0
    # v52: search 1 + enrich 2 (新規 INSERT 成功 ごと 1 call) = 3 requests
    assert mock_client.search_items.call_count == 1
    assert mock_client.get_item_pricing.call_count == 2
    assert res["requests_used"] == 3


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_run_one_skips_own_seller(mock_browse_cls, tmp_db):
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_self",
            rival_watch_enabled=1,
            rival_search_keywords="maxell cassette",
        )
    mock_client = MagicMock()
    mock_client.search_items.return_value = [
        {"seller": "me", "item_id": "v1|111|0", "title": "T", "price_usd": 1},
        {"seller": "other", "item_id": "v1|222|0", "title": "T2", "price_usd": 2},
    ]
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    res = run_rival_per_listing_detection_one("eid_self", cfg, sleep_between=0.0)
    assert res["new_discoveries"] == 1


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
            rival_search_keywords="maxell cassette",
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
    """v2.1 HIGH-3 (継続): retry も含めて counter は試行 *前* に decrement.

    v2: 1 listing = 1 query なので、3 retry 全部 5xx → requests_used=3.
    """
    import httpx
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_cnt",
            rival_watch_enabled=1,
            rival_search_keywords="maxell cassette",
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
            "eid_cnt", cfg, sleep_between=0.0,
        )
    assert res["requests_used"] == 3
    assert res["errors"] == 1


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_max_requests_per_run_early_break(mock_browse_cls, tmp_db):
    """H-H: max_requests_per_run で early break (v2: listing 単位の budget cap)."""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_detection
    with get_conn() as conn:
        for i in range(5):
            _insert_listing(
                conn, f"eid_q{i}",
                rival_watch_enabled=1,
                rival_search_keywords="maxell cassette",
            )
    mock_client = MagicMock()
    mock_client.search_items.return_value = []
    mock_browse_cls.return_value = mock_client
    cfg = {
        "ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"},
        "tasks_enabled": {"rival_detection": {
            "max_listings_per_run": 30,
            "max_requests_per_run": 3,
        }},
    }
    with patch("tasks.task_rival_detection.time.sleep"):
        res = run_rival_detection(cfg)
    # 3 calls 消費後 listing #4, #5 は skip
    assert res["requests_used"] == 3
    assert mock_client.search_items.call_count == 3


def test_run_rival_detection_zero_listings_returns_success_with_message(tmp_db):
    """0 listings → success=True で reminder ガード経路."""
    from tasks.task_rival_detection import run_rival_detection
    cfg = {"discord": {"webhook_url": ""}}
    res = run_rival_detection(cfg)
    assert res["success"] is True
    assert "0 listings" in res["message"]


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_empty_keywords_yields_skipped_not_errors(mock_browse_cls, tmp_db):
    """W153-UX (2026-05-26 Codex 推奨): 空 keyword listing は skipped 集計.
    旧 (H-D): errors>0 で全体 success=False
    新: skipped_keywords_null>0 / errors=0 / success=True
    (ok 1 listing + empty 1 listing で全体 success=True)"""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_detection
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_ok",
            rival_watch_enabled=1,
            rival_search_keywords="maxell cassette",
        )
        _insert_listing(
            conn, "eid_empty",
            rival_watch_enabled=1,
            rival_search_keywords=None,
        )
    mock_client = MagicMock()
    mock_client.search_items.return_value = []
    mock_browse_cls.return_value = mock_client
    cfg = {
        "ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"},
        "discord": {"webhook_url": ""},
        "tasks_enabled": {"rival_detection": {
            "max_listings_per_run": 30,
            "max_requests_per_run": 150,
        }},
    }
    with patch("tasks.task_rival_detection.time.sleep"):
        res = run_rival_detection(cfg)
    assert res["errors"] == 0
    assert res["skipped_keywords_null"] == 1
    assert res["success"] is True


# ============================================================
# code-reviewer regression tests (HIGH-1 / HIGH-3 / v2 化で HIGH-4 統合)
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
            rival_search_keywords="maxell cassette",
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
    assert res["errors"] == 1


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
            rival_search_keywords="maxell cassette",
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
    assert res["requests_used"] == 1
    assert res["errors"] == 1


# ============================================================
# v51 (2026-05-22 PM): shipping info 保存 + UI hide design
# ============================================================
# 業務知識 (reference_ebay_economy_shipping_seller_pattern.md):
# Economy 系は安商品で seller が使い分けるため seller block list は誤り.
# 検索段階 skip ではなく UI hide で対応 (検索は全件 record).


def test_v52_idempotent_init_db_adds_shipping_cols(tmp_db):
    """v52: listing_rival_discoveries に shipping_cost_usd / min/max_delivery_date /
    shipping_service_code 列追加."""
    from monitor.database import get_conn
    tmp_db.init_db()
    with get_conn() as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(listing_rival_discoveries)"
        ).fetchall()}
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert "competitor_shipping_cost_usd" in cols
    assert "min_delivery_date" in cols
    assert "max_delivery_date" in cols
    assert "shipping_service_code" in cols
    assert ver >= 52


def test_v52_drift_recovery_when_cols_missing(tmp_db):
    """v52: 列が drift して欠損していれば init_db で復活."""
    from monitor.database import get_conn
    with get_conn() as conn:
        conn.execute("DROP TABLE listing_rival_discoveries")
        conn.execute("""
            CREATE TABLE listing_rival_discoveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ebay_item_id TEXT NOT NULL,
                competitor_seller TEXT NOT NULL,
                competitor_item_id TEXT NOT NULL,
                competitor_title TEXT,
                competitor_price_usd REAL,
                search_keyword TEXT,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'new',
                status_changed_at TIMESTAMP,
                UNIQUE(ebay_item_id, competitor_seller, competitor_item_id)
            )
        """)
        conn.execute("PRAGMA user_version = 50")
    tmp_db.init_db()
    with get_conn() as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(listing_rival_discoveries)"
        ).fetchall()}
    assert "competitor_shipping_cost_usd" in cols
    assert "shipping_service_code" in cols


def test_enrich_rival_discovery_shipping_updates_only_provided(tmp_db):
    """v52: enrich_rival_discovery_shipping が COALESCE で NULL field は維持."""
    from monitor.database import (
        get_conn, record_rival_discovery,
        enrich_rival_discovery_shipping,
    )
    with get_conn() as conn:
        _insert_listing(conn, "eid_enrich")
    rid = record_rival_discovery(
        ebay_item_id="eid_enrich",
        competitor_seller="s",
        competitor_item_id="123",
        competitor_shipping_cost_usd=5.0,
        min_delivery_date="2026-06-01T00:00:00.000Z",
    )
    assert rid is not None
    # enrich: shipping_service_code のみ与え、shipping_cost は None (COALESCE 維持)
    ok = enrich_rival_discovery_shipping(
        rid,
        shipping_service_code="USPS Priority Mail International",
        shipping_cost_usd=None,
        min_delivery_date=None,
        max_delivery_date="2026-06-08T00:00:00.000Z",
    )
    assert ok is True
    with get_conn() as conn:
        row = conn.execute(
            "SELECT shipping_service_code, competitor_shipping_cost_usd, "
            "min_delivery_date, max_delivery_date "
            "FROM listing_rival_discoveries WHERE id = ?", (rid,),
        ).fetchone()
    assert row["shipping_service_code"] == "USPS Priority Mail International"
    assert row["competitor_shipping_cost_usd"] == 5.0  # COALESCE 維持
    assert row["min_delivery_date"] == "2026-06-01T00:00:00.000Z"  # COALESCE 維持
    assert row["max_delivery_date"] == "2026-06-08T00:00:00.000Z"  # 上書き


def test_enrich_rival_discovery_shipping_returns_false_on_missing(tmp_db):
    """v52: 不在 id で enrich → False."""
    from monitor.database import enrich_rival_discovery_shipping
    ok = enrich_rival_discovery_shipping(
        99999,
        shipping_service_code="X",
    )
    assert ok is False


def test_record_rival_discovery_persists_shipping_info(tmp_db):
    """v51: record_rival_discovery が shipping_cost_usd / delivery date を保存."""
    from monitor.database import get_conn, record_rival_discovery
    with get_conn() as conn:
        _insert_listing(conn, "eid_ship")
    rid = record_rival_discovery(
        ebay_item_id="eid_ship",
        competitor_seller="seller_a",
        competitor_item_id="111",
        competitor_title="Test",
        competitor_price_usd=99.99,
        search_keyword="kw1 kw2",
        competitor_shipping_cost_usd=12.50,
        min_delivery_date="2026-06-01T00:00:00.000Z",
        max_delivery_date="2026-06-08T00:00:00.000Z",
    )
    assert rid is not None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT competitor_shipping_cost_usd, min_delivery_date, max_delivery_date "
            "FROM listing_rival_discoveries WHERE id = ?", (rid,),
        ).fetchone()
    assert row["competitor_shipping_cost_usd"] == 12.50
    assert row["min_delivery_date"] == "2026-06-01T00:00:00.000Z"
    assert row["max_delivery_date"] == "2026-06-08T00:00:00.000Z"


def test_record_rival_discovery_existing_updates_shipping_with_coalesce(tmp_db):
    """v51: 既存重複時に shipping info を COALESCE で更新 (NULL は維持)."""
    from monitor.database import get_conn, record_rival_discovery
    with get_conn() as conn:
        _insert_listing(conn, "eid_ship2")
    # 1st: 完全な shipping info で INSERT
    record_rival_discovery(
        ebay_item_id="eid_ship2", competitor_seller="s",
        competitor_item_id="222",
        competitor_shipping_cost_usd=10.0,
        min_delivery_date="2026-06-01T00:00:00.000Z",
        max_delivery_date="2026-06-05T00:00:00.000Z",
    )
    # 2nd: shipping_cost のみ更新、delivery date は NULL で COALESCE 期待
    record_rival_discovery(
        ebay_item_id="eid_ship2", competitor_seller="s",
        competitor_item_id="222",
        competitor_shipping_cost_usd=15.0,
        min_delivery_date=None,
        max_delivery_date=None,
    )
    with get_conn() as conn:
        row = conn.execute(
            "SELECT competitor_shipping_cost_usd, min_delivery_date "
            "FROM listing_rival_discoveries WHERE ebay_item_id = ?", ("eid_ship2",),
        ).fetchone()
    assert row["competitor_shipping_cost_usd"] == 15.0  # 更新
    assert row["min_delivery_date"] == "2026-06-01T00:00:00.000Z"  # COALESCE 維持


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_run_one_passes_shipping_info_to_db(mock_browse_cls, tmp_db):
    """v51: search response の shipping info が DB に保存される."""
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_shipsave",
            rival_watch_enabled=1,
            rival_search_keywords="maxell cassette",
        )
    mock_client = MagicMock()
    mock_client.search_items.return_value = [
        {
            "seller": "exp_seller", "item_id": "v1|111|0",
            "title": "Express seller", "price_usd": 100.0,
            "shipping_cost_usd": 8.50,
            "min_delivery_date": "2026-06-01T00:00:00.000Z",
            "max_delivery_date": "2026-06-05T00:00:00.000Z",
        },
        {
            "seller": "eco_seller", "item_id": "v1|222|0",
            "title": "Economy seller", "price_usd": 50.0,
            "shipping_cost_usd": 2.00,
            "min_delivery_date": "2026-06-01T00:00:00.000Z",
            "max_delivery_date": "2026-06-20T00:00:00.000Z",  # 19 日 = UI で hide だが DB は保存
        },
    ]
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    res = run_rival_per_listing_detection_one(
        "eid_shipsave", cfg, sleep_between=0.0,
    )
    # v51: 両方とも DB record (UI 側で hide)
    assert res["new_discoveries"] == 2
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT competitor_seller, competitor_shipping_cost_usd, max_delivery_date "
            "FROM listing_rival_discoveries WHERE ebay_item_id = ? "
            "ORDER BY competitor_seller", ("eid_shipsave",),
        ).fetchall()
    assert len(rows) == 2
    sellers = {r["competitor_seller"]: r for r in rows}
    assert sellers["exp_seller"]["competitor_shipping_cost_usd"] == 8.50
    assert sellers["eco_seller"]["competitor_shipping_cost_usd"] == 2.00


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_run_one_top_level_exception_increments_errors(mock_browse_cls, tmp_db):
    """Codex GPT-5.5 HIGH regression (2026-05-22 PM):
    top-level except で errors を increment しないと cron 集約 (res["errors"] 合算) で
    silent skip され Discord error alert にも乗らない (money-direct silent gap).
    """
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_per_listing_detection_one
    with get_conn() as conn:
        _insert_listing(
            conn, "eid_topexc",
            rival_watch_enabled=1,
            rival_search_keywords="maxell cassette",
        )
    mock_client = MagicMock()
    # search_items が予期しない型を返す → for it in items 内で AttributeError 等
    mock_client.search_items.side_effect = RuntimeError("simulated unexpected error")
    mock_browse_cls.return_value = mock_client
    cfg = {"ebay": {"app_id": "x", "cert_id": "x", "seller_id": "me"}}
    res = run_rival_per_listing_detection_one(
        "eid_topexc", cfg, sleep_between=0.0,
    )
    assert res["success"] is False
    # Codex HIGH fix: top-level except でも errors >= 1 (cron 集約で検出可能)
    assert res["errors"] >= 1, "top-level except must increment errors (Codex HIGH fix)"
    assert "top-level" in res["message"] or "simulated" in res["message"]


@patch("tasks.ebay_browse_api.BrowseAPIClient")
def test_max_requests_budget_consumed_across_listings(mock_browse_cls, tmp_db):
    """code-reviewer HIGH-3: 複数 listing 跨ぎで max_requests budget が cumulative.

    v2: 3 listings × 1 query each、max_requests=2 → listing #1 + #2 で 2 calls 消費、
    listing #3 は一切 search_items 呼ばれない.
    """
    from monitor.database import get_conn
    from tasks.task_rival_detection import run_rival_detection
    with get_conn() as conn:
        for i in range(3):
            _insert_listing(
                conn, f"eid_x{i}",
                rival_watch_enabled=1,
                rival_search_keywords="maxell cassette",
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
    assert res["requests_used"] == 2
    assert mock_client.search_items.call_count == 2
