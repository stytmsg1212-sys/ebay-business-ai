"""W119 検索ワード生成 (Opus batch) のロジックテスト.

検証対象 (`tasks/task_generate_search_keywords.py`):
- _fetch_target_listings: search_keyword IS NULL の listing のみ抽出 (force_all=False)
- _fetch_target_listings: force_all=True で全 active listing 抽出
- _parse_keyword_from_response: 様々な response 形式から keyword 抽出
- _apply_keyword_to_db: DB UPDATE が正しく走る (source='opus_batch')
- update_search_keyword_manual: 手動編集経路 (source='manual_edit')

mock 戦略: Anthropic API は呼ばない. response object のみ stub する.
real signature regression は test_w119_keyword_real_signature.py で別途.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert_listing(ebay_item_id: str, title: str, is_ended: int = 0,
                    search_keyword=None):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, is_ended, search_keyword) "
            "VALUES (?, ?, ?, ?, ?)",
            (ebay_item_id, "stock:test", title, is_ended, search_keyword),
        )


def test_fetch_target_listings_excludes_already_generated(tmp_db):
    """search_keyword IS NOT NULL の listing は除外される (default)."""
    from tasks.task_generate_search_keywords import _fetch_target_listings

    _insert_listing("item001", "maxell MXCP-P100 Black", search_keyword=None)
    _insert_listing("item002", "Sony Walkman WM-DD9", search_keyword="sony WM-DD9")
    _insert_listing("item003", "Audio-Technica ATH-CKS330NC", search_keyword=None)

    targets = _fetch_target_listings(force_all=False)
    ids = [t.ebay_item_id for t in targets]
    assert "item001" in ids
    assert "item002" not in ids, "search_keyword 既存 listing が抽出されてはいけない"
    assert "item003" in ids


def test_fetch_target_listings_force_all_includes_generated(tmp_db):
    """force_all=True で search_keyword 既存 listing も抽出される."""
    from tasks.task_generate_search_keywords import _fetch_target_listings

    _insert_listing("item001", "maxell MXCP-P100", search_keyword=None)
    _insert_listing("item002", "Sony Walkman", search_keyword="sony walkman")

    targets = _fetch_target_listings(force_all=True)
    ids = [t.ebay_item_id for t in targets]
    assert "item001" in ids
    assert "item002" in ids


def test_fetch_target_listings_excludes_ended(tmp_db):
    """is_ended=1 は除外."""
    from tasks.task_generate_search_keywords import _fetch_target_listings

    _insert_listing("active001", "Active title", is_ended=0)
    _insert_listing("ended001", "Ended title", is_ended=1)

    targets = _fetch_target_listings(force_all=False)
    ids = [t.ebay_item_id for t in targets]
    assert "active001" in ids
    assert "ended001" not in ids


def test_fetch_target_listings_excludes_empty_title(tmp_db):
    """title が空 / NULL の listing は除外."""
    from tasks.task_generate_search_keywords import _fetch_target_listings

    _insert_listing("good001", "Valid Title")
    # 空 title
    _insert_listing("empty001", "")
    # NULL title (database.py の現状 schema で NULL 許可なら)
    from monitor.database import get_conn
    with get_conn() as c:
        try:
            c.execute(
                "INSERT INTO ebay_listings (ebay_item_id, sku, title, is_ended) "
                "VALUES (?, ?, ?, ?)",
                ("null001", "stock:test", None, 0),
            )
        except Exception:
            pass  # NOT NULL constraint 等で reject されてもテスト主旨は通る

    targets = _fetch_target_listings(force_all=False)
    ids = [t.ebay_item_id for t in targets]
    assert "good001" in ids
    assert "empty001" not in ids
    assert "null001" not in ids


def test_parse_keyword_from_response_basic():
    from tasks.task_generate_search_keywords import _parse_keyword_from_response

    # 正常 response
    msg = MagicMock()
    block = MagicMock()
    block.text = "maxell MXCP-P100"
    msg.content = [block]
    assert _parse_keyword_from_response(msg) == "maxell MXCP-P100"


def test_parse_keyword_from_response_strip_quotes_punctuation():
    from tasks.task_generate_search_keywords import _parse_keyword_from_response

    msg = MagicMock()
    block = MagicMock()
    block.text = '  "Sony WM-DD9".  '
    msg.content = [block]
    assert _parse_keyword_from_response(msg) == "Sony WM-DD9"


def test_parse_keyword_from_response_multiline_takes_first():
    from tasks.task_generate_search_keywords import _parse_keyword_from_response

    msg = MagicMock()
    block = MagicMock()
    block.text = "Audio-Technica ATH-CKS330NC\n(extra explanation)"
    msg.content = [block]
    assert _parse_keyword_from_response(msg) == "Audio-Technica ATH-CKS330NC"


def test_parse_keyword_from_response_empty_returns_none():
    from tasks.task_generate_search_keywords import _parse_keyword_from_response

    msg = MagicMock()
    block = MagicMock()
    block.text = "   "
    msg.content = [block]
    assert _parse_keyword_from_response(msg) is None


def test_parse_keyword_from_response_too_long_returns_none():
    """200 文字超は異常 (URL 制約 / prompt 暴走の signal)."""
    from tasks.task_generate_search_keywords import _parse_keyword_from_response

    msg = MagicMock()
    block = MagicMock()
    block.text = "a" * 250
    msg.content = [block]
    assert _parse_keyword_from_response(msg) is None


def test_parse_keyword_from_response_no_content_returns_none():
    from tasks.task_generate_search_keywords import _parse_keyword_from_response

    msg = MagicMock()
    msg.content = []
    assert _parse_keyword_from_response(msg) is None


def test_apply_keyword_to_db_sets_source(tmp_db):
    """_apply_keyword_to_db で source='opus_batch' が刻まれる."""
    from tasks.task_generate_search_keywords import _apply_keyword_to_db

    _insert_listing("item001", "Some Title")
    _apply_keyword_to_db("item001", "extracted keyword")

    from monitor.database import get_conn
    with get_conn() as c:
        row = c.execute(
            "SELECT search_keyword, search_keyword_source, search_keyword_generated_at "
            "FROM ebay_listings WHERE ebay_item_id=?",
            ("item001",),
        ).fetchone()
        assert row[0] == "extracted keyword"
        assert row[1] == "opus_batch"
        assert row[2] is not None  # generated_at がセットされる


def test_update_search_keyword_manual_sets_source(tmp_db):
    """update_search_keyword_manual で source='manual_edit' になる."""
    from tasks.task_generate_search_keywords import update_search_keyword_manual

    _insert_listing("item001", "Some Title")
    ok = update_search_keyword_manual("item001", "user-edited keyword")
    assert ok is True

    from monitor.database import get_conn
    with get_conn() as c:
        row = c.execute(
            "SELECT search_keyword, search_keyword_source FROM ebay_listings WHERE ebay_item_id=?",
            ("item001",),
        ).fetchone()
        assert row[0] == "user-edited keyword"
        assert row[1] == "manual_edit"


def test_update_search_keyword_manual_empty_rejected(tmp_db):
    """空文字 / 空白のみは reject (False を返す, DB 変更しない)."""
    from tasks.task_generate_search_keywords import update_search_keyword_manual

    _insert_listing("item001", "Some Title", search_keyword="existing keyword")

    assert update_search_keyword_manual("item001", "") is False
    assert update_search_keyword_manual("item001", "   ") is False

    # DB は元の値を保持
    from monitor.database import get_conn
    with get_conn() as c:
        row = c.execute(
            "SELECT search_keyword FROM ebay_listings WHERE ebay_item_id=?",
            ("item001",),
        ).fetchone()
        assert row[0] == "existing keyword"


def test_run_generate_search_keywords_no_targets(tmp_db):
    """対象 0 件で submitted=0 で early return (API call せず)."""
    from tasks.task_generate_search_keywords import run_generate_search_keywords

    # 対象 0 件 (DB に listing 無し)
    result = run_generate_search_keywords(force_all=False)
    assert result.submitted == 0
    assert result.batch_id == ""


def test_run_generate_search_keywords_no_api_key(tmp_db, monkeypatch):
    """ANTHROPIC_API_KEY 未設定で error_message 返す + DB 変更なし."""
    from tasks.task_generate_search_keywords import run_generate_search_keywords

    _insert_listing("item001", "Title")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = run_generate_search_keywords(force_all=False)
    assert result.submitted == 1
    assert result.errored == 1
    assert "ANTHROPIC_API_KEY" in (result.error_message or "")

    # DB の search_keyword は NULL のまま
    from monitor.database import get_conn
    with get_conn() as c:
        row = c.execute(
            "SELECT search_keyword FROM ebay_listings WHERE ebay_item_id=?",
            ("item001",),
        ).fetchone()
        assert row[0] is None


def test_keyword_item_custom_id_format():
    """custom_id は ebay_item_id を含み、batch API 制約 (1-64 文字) を満たす."""
    from tasks.task_generate_search_keywords import KeywordItem

    it = KeywordItem(ebay_item_id="356534387172", title="Test")
    assert it.custom_id == "w119-356534387172"
    assert 1 <= len(it.custom_id) <= 64
