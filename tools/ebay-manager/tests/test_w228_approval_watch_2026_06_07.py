#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 後続: 承認 UI + キーワード新着監視登録 テスト.

カバレッジ:
  (A) 承認 status 定数が research_candidates_db に存在すること
  (B) 承認 status 遷移 (sourced → identity_approved → watch_registered)
  (C) 不正遷移を拒否 (identity_approved → sourced は禁止)
  (D) needs_review → identity_approved も許容 (人間が直接承認)
  (E) identity_rejected → sourcing (再探索) が許容
  (F) watch_registered → needs_review が許容 (監視解除・再検討)
  (G) watch 登録: keyword_watch_db.add_watch が正しい引数で呼ばれる (mock)
  (H) 重複登録防止: add_watch が inserted_new=False を返したとき status は遷移するが
      st.success でなく st.info が出る (mock で検証)
  (I) watch 登録失敗時 (add_watch 例外) は status が watch_registered に遷移しない
  (J) _calc_price_max_jpy ロジック確認

mock 方針:
  DB は実 tmp DB (init_db)。keyword_watch_db.add_watch は monkeypatch。
  Streamlit 呼び出しは tabs.tab_w228_research から直接テストしない
  (Streamlit runtime 依存を避ける)。_register_keyword_watch を分離テスト。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# (A) 承認 status 定数の存在確認
# ---------------------------------------------------------------------------

def test_approval_status_constants_exist():
    """research_candidates_db に承認系 status 定数が追加されていること."""
    from monitor import research_candidates_db as rc_db

    assert hasattr(rc_db, "STATUS_IDENTITY_APPROVED")
    assert rc_db.STATUS_IDENTITY_APPROVED == "identity_approved"
    assert hasattr(rc_db, "STATUS_IDENTITY_REJECTED")
    assert rc_db.STATUS_IDENTITY_REJECTED == "identity_rejected"
    assert hasattr(rc_db, "STATUS_WATCH_REGISTERED")
    assert rc_db.STATUS_WATCH_REGISTERED == "watch_registered"

    # _VALID_STATUSES に含まれること
    assert rc_db.STATUS_IDENTITY_APPROVED in rc_db._VALID_STATUSES
    assert rc_db.STATUS_IDENTITY_REJECTED in rc_db._VALID_STATUSES
    assert rc_db.STATUS_WATCH_REGISTERED in rc_db._VALID_STATUSES


# ---------------------------------------------------------------------------
# (B) 承認 status 遷移: sourced → identity_approved → watch_registered
# ---------------------------------------------------------------------------

def test_approval_transition_sourced_to_identity_approved_to_watch_registered():
    """sourced → identity_approved → watch_registered の正常遷移."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("Sony WH-1000XM5 承認テスト")

    # new → sourcing
    assert rc_db.update_status(rc_id, rc_db.STATUS_SOURCING) is True
    # sourcing → sourced
    assert rc_db.update_status(rc_id, rc_db.STATUS_SOURCED) is True

    # sourced → identity_approved (承認)
    result = rc_db.update_status(rc_id, rc_db.STATUS_IDENTITY_APPROVED)
    assert result is True
    assert rc_db.get_research_candidate(rc_id)["status"] == "identity_approved"

    # identity_approved → watch_registered (監視登録)
    result = rc_db.update_status(rc_id, rc_db.STATUS_WATCH_REGISTERED)
    assert result is True
    assert rc_db.get_research_candidate(rc_id)["status"] == "watch_registered"


# ---------------------------------------------------------------------------
# (C) 不正遷移拒否
# ---------------------------------------------------------------------------

def test_invalid_transition_identity_approved_to_sourced_rejected():
    """identity_approved → sourced は禁止 (監視済から sourced に戻すのは state machine 違反)."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("不正遷移テスト")
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCED)
    rc_db.update_status(rc_id, rc_db.STATUS_IDENTITY_APPROVED)

    with pytest.raises(ValueError, match="transition not allowed"):
        rc_db.update_status(rc_id, rc_db.STATUS_SOURCED)

    with pytest.raises(ValueError, match="transition not allowed"):
        rc_db.update_status(rc_id, rc_db.STATUS_NOT_FOUND)


def test_invalid_transition_watch_registered_to_sourced():
    """watch_registered → sourced は禁止."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("watch_registered 不正遷移テスト")
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCED)
    rc_db.update_status(rc_id, rc_db.STATUS_IDENTITY_APPROVED)
    rc_db.update_status(rc_id, rc_db.STATUS_WATCH_REGISTERED)

    with pytest.raises(ValueError, match="transition not allowed"):
        rc_db.update_status(rc_id, rc_db.STATUS_SOURCED)


# ---------------------------------------------------------------------------
# (D) needs_review → identity_approved 許容
# ---------------------------------------------------------------------------

def test_needs_review_to_identity_approved_allowed():
    """needs_review 状態からも人間が直接承認できる."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("needs_review → 承認テスト")
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)
    rc_db.update_status(
        rc_id, rc_db.STATUS_NEEDS_REVIEW,
        needs_review_reason="weight 未入力で計算不能だが同一性は確認済"
    )

    # needs_review → identity_approved
    result = rc_db.update_status(rc_id, rc_db.STATUS_IDENTITY_APPROVED)
    assert result is True
    assert rc_db.get_research_candidate(rc_id)["status"] == "identity_approved"


# ---------------------------------------------------------------------------
# (E) identity_rejected → sourcing 許容 (再探索)
# ---------------------------------------------------------------------------

def test_identity_rejected_to_sourcing_allowed():
    """却下後に再探索 (sourcing) へ戻せる."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("却下 → 再探索テスト")
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCED)
    rc_db.update_status(rc_id, rc_db.STATUS_IDENTITY_REJECTED)

    result = rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)
    assert result is True
    assert rc_db.get_research_candidate(rc_id)["status"] == "sourcing"


# ---------------------------------------------------------------------------
# (F) watch_registered → needs_review 許容
# ---------------------------------------------------------------------------

def test_watch_registered_to_needs_review_allowed():
    """監視登録後に再検討 (needs_review) へ遷移できる."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("監視後 再検討テスト")
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCED)
    rc_db.update_status(rc_id, rc_db.STATUS_IDENTITY_APPROVED)
    rc_db.update_status(rc_id, rc_db.STATUS_WATCH_REGISTERED)

    result = rc_db.update_status(
        rc_id, rc_db.STATUS_NEEDS_REVIEW,
        needs_review_reason="価格変動で再検討が必要"
    )
    assert result is True
    assert rc_db.get_research_candidate(rc_id)["status"] == "needs_review"


# ---------------------------------------------------------------------------
# (G) watch 登録: add_watch が正しい引数で呼ばれる
# ---------------------------------------------------------------------------

def test_register_keyword_watch_calls_add_watch_correctly():
    """_register_keyword_watch が mercari / yahoo_auctions の 2 サイトに
    正しい keyword / price_max / source で add_watch を呼ぶ."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("Audio-Technica ATH-M50x")
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCED)
    rc_db.update_status(rc_id, rc_db.STATUS_IDENTITY_APPROVED)

    calls: list[dict] = []

    def _fake_add_watch(*, site, search_url, keyword, price_max_jpy=None,
                        memo="", source="manual", **kwargs):
        calls.append({
            "site": site, "search_url": search_url, "keyword": keyword,
            "price_max_jpy": price_max_jpy, "source": source,
        })
        return (len(calls), True)  # (watch_id, inserted_new=True)

    with patch("monitor.keyword_watch_db.add_watch", side_effect=_fake_add_watch):
        from tabs.tab_w228_research import _register_keyword_watch
        # Streamlit 呼び出しを mock
        with patch("tabs.tab_w228_research.st") as mock_st:
            mock_st.success = MagicMock()
            mock_st.info = MagicMock()
            mock_st.error = MagicMock()
            mock_st.rerun = MagicMock()
            _register_keyword_watch(
                rc_id=rc_id,
                title_ja="Audio-Technica ATH-M50x",
                price_max_jpy=8000,
            )

    # 2 サイトに登録
    assert len(calls) == 2
    sites_called = {c["site"] for c in calls}
    assert sites_called == {"mercari", "yahoo_auctions"}

    for c in calls:
        assert c["keyword"] == "Audio-Technica ATH-M50x"
        assert c["price_max_jpy"] == 8000
        assert c["source"] == "w228_research"
        assert "Audio-Technica" in c["search_url"]

    # status が watch_registered に遷移しているか
    assert rc_db.get_research_candidate(rc_id)["status"] == "watch_registered"


# ---------------------------------------------------------------------------
# (H) 重複登録防止: inserted_new=False でも status は遷移する
# ---------------------------------------------------------------------------

def test_register_keyword_watch_duplicate_still_transitions_status():
    """add_watch が inserted_new=False (既存) を返しても status=watch_registered に遷移."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("重複登録テスト商品")
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCED)
    rc_db.update_status(rc_id, rc_db.STATUS_IDENTITY_APPROVED)

    # add_watch が inserted_new=False を返す (既存 watch)
    def _fake_add_watch_existing(*, site, **kwargs):
        return (99, False)  # watch_id=99, inserted_new=False

    with patch("monitor.keyword_watch_db.add_watch", side_effect=_fake_add_watch_existing):
        from tabs.tab_w228_research import _register_keyword_watch
        with patch("tabs.tab_w228_research.st") as mock_st:
            mock_st.success = MagicMock()
            mock_st.info = MagicMock()
            mock_st.error = MagicMock()
            mock_st.rerun = MagicMock()
            _register_keyword_watch(
                rc_id=rc_id,
                title_ja="重複登録テスト商品",
                price_max_jpy=5000,
            )

            # st.info が呼ばれる (st.success でない)
            mock_st.info.assert_called_once()
            mock_st.success.assert_not_called()

    # status は watch_registered に遷移
    assert rc_db.get_research_candidate(rc_id)["status"] == "watch_registered"


# ---------------------------------------------------------------------------
# (I) watch 登録失敗時は status が遷移しない
# ---------------------------------------------------------------------------

def test_register_keyword_watch_failure_does_not_transition_status():
    """add_watch が例外を投げたとき status は identity_approved のまま (Q0 偽装成功禁止)."""
    from monitor.database import init_db
    from monitor import research_candidates_db as rc_db

    init_db()
    rc_id = rc_db.insert_research_candidate("watch登録失敗テスト")
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCED)
    rc_db.update_status(rc_id, rc_db.STATUS_IDENTITY_APPROVED)

    def _fake_add_watch_error(*, site, **kwargs):
        raise RuntimeError("DB接続失敗 (mock)")

    with patch("monitor.keyword_watch_db.add_watch", side_effect=_fake_add_watch_error):
        from tabs.tab_w228_research import _register_keyword_watch
        with patch("tabs.tab_w228_research.st") as mock_st:
            mock_st.error = MagicMock()
            mock_st.rerun = MagicMock()
            _register_keyword_watch(
                rc_id=rc_id,
                title_ja="watch登録失敗テスト",
                price_max_jpy=None,
            )

            # st.error が呼ばれていること
            mock_st.error.assert_called_once()
            # st.rerun は呼ばれない (失敗なので更新しない)
            mock_st.rerun.assert_not_called()

    # status は identity_approved のまま (watch_registered に遷移していない)
    assert rc_db.get_research_candidate(rc_id)["status"] == "identity_approved"


# ---------------------------------------------------------------------------
# (J) _calc_price_max_jpy ロジック
# ---------------------------------------------------------------------------

def test_calc_price_max_jpy_returns_found_price_as_ceiling():
    """_calc_price_max_jpy は found_price_jpy を保守的上限として返す."""
    from tabs.tab_w228_research import _calc_price_max_jpy

    # 通常ケース: found_price_jpy がある
    result = _calc_price_max_jpy(found_price_jpy=8000, estimated_profit_usd=5.5)
    assert result == 8000

    # found_price_jpy がない
    result = _calc_price_max_jpy(found_price_jpy=None, estimated_profit_usd=5.5)
    assert result is None

    # found_price_jpy=0
    result = _calc_price_max_jpy(found_price_jpy=0, estimated_profit_usd=5.5)
    assert result is None

    # Codex#3 修正後: 利益 None (未検証) は安全な自動上限を出さない → None
    result = _calc_price_max_jpy(found_price_jpy=12000, estimated_profit_usd=None)
    assert result is None
    # 利益 ≤0 (損) も None (損失価格を上限にしない)
    assert _calc_price_max_jpy(found_price_jpy=12000, estimated_profit_usd=0) is None
    assert _calc_price_max_jpy(found_price_jpy=12000, estimated_profit_usd=-3.0) is None
    # 利益 >0 のときのみ found_price を上限に
    assert _calc_price_max_jpy(found_price_jpy=12000, estimated_profit_usd=4.0) == 12000


def test_mercari_and_yahoo_search_url_builders():
    """URL ビルダーが keyword を正しくエンコードした URL を返す."""
    from tabs.tab_w228_research import _mercari_search_url, _yahoo_auctions_search_url

    mercari_url = _mercari_search_url("Audio-Technica ATH-M50x")
    assert mercari_url.startswith("https://jp.mercari.com/search?keyword=")
    assert "Audio-Technica" in mercari_url

    yahoo_url = _yahoo_auctions_search_url("Pioneer DJM-450")
    assert yahoo_url.startswith("https://auctions.yahoo.co.jp/search/search?p=")
    assert "Pioneer" in yahoo_url

    # スペースが %20 または + でエンコードされること
    url_with_space = _mercari_search_url("Sony ヘッドホン")
    assert " " not in url_with_space
