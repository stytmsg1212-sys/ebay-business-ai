"""W119 wizard helper functions のテスト.

UI rendering 自体は Playwright でカバー (Q1 DoD Phase 2).
本ファイルでは pure function (URL builder, _count_listings_state) を test.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert_listing(
    ebay_item_id: str,
    title: str = "Test",
    is_ended: int = 0,
    weight_g=None,
    length_cm=None,
    width_cm=None,
    height_cm=None,
    lp_breakeven_usd=None,
    search_keyword=None,
):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT INTO ebay_listings (
                ebay_item_id, sku, title, is_ended, weight_g,
                length_cm, width_cm, height_cm, lp_breakeven_usd, search_keyword
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ebay_item_id, "stock:test", title, is_ended, weight_g,
                length_cm, width_cm, height_cm, lp_breakeven_usd, search_keyword,
            ),
        )


# =============================================================================
# build_ebay_search_url
# =============================================================================

def test_build_ebay_search_url_basic():
    from tabs.tab_research_wizard import build_ebay_search_url

    url = build_ebay_search_url("maxell MXCP-P100")
    expected = (
        "https://www.ebay.com/sch/i.html"
        "?_nkw=maxell+MXCP-P100"
        "&_sacat=0"
        "&_sop=15"
        "&_from=R40"
        "&_trksid=m570.l1313"
        "&LH_LocatedIn=1"
        "&_salic=104"
    )
    assert url == expected


def test_build_ebay_search_url_special_chars():
    """URL-unsafe な文字 (& / + / 空白) は quote_plus でエンコード."""
    from tabs.tab_research_wizard import build_ebay_search_url

    url = build_ebay_search_url("Sony WM-DD9 / 1980s")
    # `/` は %2F, ` ` は `+`
    assert "Sony+WM-DD9+%2F+1980s" in url


def test_build_ebay_search_url_empty():
    from tabs.tab_research_wizard import build_ebay_search_url
    assert build_ebay_search_url("") == ""
    assert build_ebay_search_url(None) == ""


def test_build_ebay_search_url_whitespace_only():
    from tabs.tab_research_wizard import build_ebay_search_url
    assert build_ebay_search_url("   ") == ""
    assert build_ebay_search_url("\t\n") == ""


def test_build_ebay_search_url_strips_whitespace():
    """前後の空白は trim される (URL は trimmed keyword でビルド)."""
    from tabs.tab_research_wizard import build_ebay_search_url

    url = build_ebay_search_url("  maxell  ")
    assert "_nkw=maxell&" in url


def test_build_ebay_search_url_jp_filter_present():
    """JP filter param が必ず含まれる (Q2-B 確定)."""
    from tabs.tab_research_wizard import build_ebay_search_url

    url = build_ebay_search_url("anything")
    assert "LH_LocatedIn=1" in url
    assert "_salic=104" in url
    assert "_sop=15" in url  # Price + Shipping lowest first


# =============================================================================
# _count_listings_state
# =============================================================================

def test_count_listings_state_empty(tmp_db):
    from tabs.tab_research_wizard import _count_listings_state

    counts = _count_listings_state()
    assert counts["total"] == 0
    assert counts["with_weight"] == 0
    assert counts["with_size"] == 0
    assert counts["with_breakeven"] == 0
    assert counts["with_keyword"] == 0


def test_count_listings_state_excludes_ended(tmp_db):
    from tabs.tab_research_wizard import _count_listings_state

    _insert_listing("active001", is_ended=0, weight_g=500)
    _insert_listing("ended001", is_ended=1, weight_g=500)

    counts = _count_listings_state()
    assert counts["total"] == 1
    assert counts["with_weight"] == 1


def test_count_listings_state_excludes_empty_title(tmp_db):
    from tabs.tab_research_wizard import _count_listings_state

    _insert_listing("good001", title="Good Title")
    _insert_listing("empty001", title="")

    counts = _count_listings_state()
    assert counts["total"] == 1


def test_count_listings_state_aggregates(tmp_db):
    from tabs.tab_research_wizard import _count_listings_state

    _insert_listing("a", weight_g=500, length_cm=10, width_cm=10, height_cm=10,
                    lp_breakeven_usd=50.0, search_keyword="kw a")
    _insert_listing("b", weight_g=600)
    _insert_listing("c", search_keyword="kw c")
    _insert_listing("d")  # 何も無し

    counts = _count_listings_state()
    assert counts["total"] == 4
    assert counts["with_weight"] == 2  # a, b
    assert counts["with_size"] == 1  # a (length+width+height 全部 NOT NULL)
    assert counts["with_breakeven"] == 1  # a
    assert counts["with_keyword"] == 2  # a, c


# =============================================================================
# _get_active_listings_for_keyword_edit
# =============================================================================

def test_get_active_listings_for_keyword_edit_returns_metadata(tmp_db):
    from tabs.tab_research_wizard import _get_active_listings_for_keyword_edit
    from monitor.database import get_conn

    _insert_listing("a", title="Title A", search_keyword="kw a")
    # search_keyword_source を設定
    with get_conn() as c:
        c.execute(
            "UPDATE ebay_listings SET search_keyword_source='opus_batch' WHERE ebay_item_id=?",
            ("a",),
        )

    listings = _get_active_listings_for_keyword_edit()
    assert len(listings) == 1
    assert listings[0]["ebay_item_id"] == "a"
    assert listings[0]["title"] == "Title A"
    assert listings[0]["search_keyword"] == "kw a"
    assert listings[0]["search_keyword_source"] == "opus_batch"


def test_get_active_listings_excludes_ended(tmp_db):
    from tabs.tab_research_wizard import _get_active_listings_for_keyword_edit

    _insert_listing("a", is_ended=0)
    _insert_listing("b", is_ended=1)

    ids = [it["ebay_item_id"] for it in _get_active_listings_for_keyword_edit()]
    assert "a" in ids
    assert "b" not in ids


# =============================================================================
# _process_browse_items (Q2 一括モード)
# =============================================================================

def test_process_browse_items_sorts_by_total_cost():
    """price + shipping の合計昇順で sort され、shipping None は末尾."""
    from tabs.tab_research_wizard import _process_browse_items

    raw = [
        {"item_id": "v1|111|0", "price_usd": 50.0, "shipping_cost_usd": 10.0},
        {"item_id": "v1|222|0", "price_usd": 30.0, "shipping_cost_usd": 25.0},  # total 55
        {"item_id": "v1|333|0", "price_usd": 40.0, "shipping_cost_usd": None},   # 末尾
        {"item_id": "v1|444|0", "price_usd": 20.0, "shipping_cost_usd": 30.0},   # total 50
    ]
    top = _process_browse_items(raw, my_ebay_item_id="999")
    # 期待順: 444 (50) → 111 (60) → 222 (55)... 違う
    # 444 (20+30=50) → 222 (30+25=55) → 111 (50+10=60) → 333 (None, 末尾)
    assert [it["legacy_item_id"] for it in top] == ["444", "222", "111", "333"]


def test_process_browse_items_excludes_self():
    """自分自身 (legacy_item_id == my_ebay_item_id) は除外."""
    from tabs.tab_research_wizard import _process_browse_items

    raw = [
        {"item_id": "v1|999|0", "price_usd": 50.0, "shipping_cost_usd": 10.0},  # 自分
        {"item_id": "v1|222|0", "price_usd": 60.0, "shipping_cost_usd": 5.0},
    ]
    top = _process_browse_items(raw, my_ebay_item_id="999")
    ids = [it["legacy_item_id"] for it in top]
    assert "999" not in ids
    assert "222" in ids


def test_process_browse_items_excludes_empty_legacy_id():
    """legacy_item_id が空のレコードは除外."""
    from tabs.tab_research_wizard import _process_browse_items

    raw = [
        {"item_id": "", "price_usd": 10.0, "shipping_cost_usd": 0.0},  # empty
        {"item_id": "v1|222|0", "price_usd": 60.0, "shipping_cost_usd": 5.0},
    ]
    top = _process_browse_items(raw, my_ebay_item_id="999")
    assert len(top) == 1
    assert top[0]["legacy_item_id"] == "222"


def test_process_browse_items_top_n_limit():
    """30 件入力でも _DISPLAY_TOP_N (= 20) 件に絞られる. 2026-05-12: 上限 10→20 拡大."""
    from tabs.tab_research_wizard import _process_browse_items, _DISPLAY_TOP_N

    raw = [
        {"item_id": f"v1|{i:03d}|0", "price_usd": float(i), "shipping_cost_usd": 0.0}
        for i in range(1, 31)
    ]
    top = _process_browse_items(raw, my_ebay_item_id="999")
    assert len(top) == _DISPLAY_TOP_N
    # 安い順: 001 〜 (上限件数)
    assert [it["legacy_item_id"] for it in top] == [f"{i:03d}" for i in range(1, _DISPLAY_TOP_N + 1)]


def test_execute_bulk_register_skips_empty_selection_keeps_existing_active(tmp_db):
    """M-3 regression: 選択 0 listing では upsert_listing_competitors を呼ばず既存 active を温存.

    H-1 fix と同じ silent skip 防止の精神:「選択 0 ≠ 既存全消去」.
    """
    from monitor.database import get_conn
    from tabs.tab_research_wizard import _execute_bulk_register

    # 既存 active 競合 2 件を投入
    with get_conn() as c:
        c.execute(
            """INSERT INTO competitor_products (our_item_id, competitor_item_id, is_active)
               VALUES (?, ?, 1), (?, ?, 1)""",
            ("test_listing_x", "c1", "test_listing_x", "c2"),
        )

    # 選択 0 で _execute_bulk_register を呼ぶ
    # (W119① 2026-05-18: form 化で config 引数追加。全選択 0 件は warning+return
    #  = upsert 未呼出で既存 active 温存、の挙動契約は不変)
    _execute_bulk_register({"test_listing_x": []}, {})

    # 既存 active 競合は維持される
    with get_conn() as c:
        active = c.execute(
            "SELECT competitor_item_id FROM competitor_products "
            "WHERE our_item_id='test_listing_x' AND is_active=1 ORDER BY competitor_item_id"
        ).fetchall()
    assert [r[0] for r in active] == ["c1", "c2"], "選択 0 で既存 active が消失 = silent destruction"


def test_execute_bulk_register_processes_selected_listings(tmp_db):
    """選択した listing は upsert_listing_competitors が呼ばれ正しく置換される."""
    from monitor.database import get_conn
    from tabs.tab_research_wizard import _execute_bulk_register

    # listing A に既存 active c1, c2 を投入
    with get_conn() as c:
        c.execute(
            """INSERT INTO competitor_products (our_item_id, competitor_item_id, is_active)
               VALUES (?, ?, 1), (?, ?, 1)""",
            ("listing_a", "c1", "listing_a", "c2"),
        )

    # 選択 c2, c3 で置換 upsert (c1 は inactive 化される, c3 が新規追加)
    # (W119① 2026-05-18: form 化で config 引数追加。config={} は Browse client
    #  None = 価格 fetch は no-op、competitor 集合の置換挙動契約は不変)
    _execute_bulk_register({"listing_a": ["c2", "c3"]}, {})

    with get_conn() as c:
        rows = c.execute(
            "SELECT competitor_item_id, is_active FROM competitor_products "
            "WHERE our_item_id='listing_a' ORDER BY competitor_item_id"
        ).fetchall()
    state = {r[0]: r[1] for r in rows}
    assert state.get("c1") == 0, "c1 (top 10 外) は inactive 化されるべき"
    assert state.get("c2") == 1, "c2 (選択済) は active 維持"
    assert state.get("c3") == 1, "c3 (新規選択) は active 追加"


def test_process_browse_items_total_cost_assigned():
    """total_cost_usd フィールドが追加される (sort 用)."""
    from tabs.tab_research_wizard import _process_browse_items

    raw = [{"item_id": "v1|111|0", "price_usd": 50.0, "shipping_cost_usd": 10.0}]
    top = _process_browse_items(raw, my_ebay_item_id="999")
    assert top[0]["total_cost_usd"] == 60.0

    raw_no_ship = [{"item_id": "v1|222|0", "price_usd": 50.0, "shipping_cost_usd": None}]
    top2 = _process_browse_items(raw_no_ship, my_ebay_item_id="999")
    assert top2[0]["total_cost_usd"] is None


# =============================================================================
# is_likely_long_window_shipping (Economy carrier proxy, 2026-05-11 W119 Round 3)
# 2026-05-12 訂正: 旧 is_likely_ddu_shipping は misnaming. 実態は Economy carrier proxy.
# 関税ポリシー (DDU/DDP) とは独立軸. 詳細: reference_shipping_method_vs_ddu_taxonomy.md.
# =============================================================================

def test_is_likely_long_window_14_days_is_economy():
    """配送窓 14 日 (SpeedPAK Economy 典型) は Economy 判定."""
    from tabs.tab_research_wizard import is_likely_long_window_shipping
    it = {
        "min_delivery_date": "2026-06-04T07:00:00.000Z",
        "max_delivery_date": "2026-06-18T07:00:00.000Z",
    }
    assert is_likely_long_window_shipping(it) is True


def test_is_likely_long_window_10_days_is_economy():
    """配送窓 10 日 (閾値ちょうど) も Economy 判定 (>=)."""
    from tabs.tab_research_wizard import is_likely_long_window_shipping
    it = {
        "min_delivery_date": "2026-06-04T07:00:00.000Z",
        "max_delivery_date": "2026-06-14T07:00:00.000Z",
    }
    assert is_likely_long_window_shipping(it) is True


def test_is_likely_long_window_3_days_is_express():
    """配送窓 3 日 (FedEx/DHL express 典型) は Economy 判定外."""
    from tabs.tab_research_wizard import is_likely_long_window_shipping
    it = {
        "min_delivery_date": "2026-06-01T07:00:00.000Z",
        "max_delivery_date": "2026-06-04T07:00:00.000Z",
    }
    assert is_likely_long_window_shipping(it) is False


def test_is_likely_long_window_6_days_is_express():
    """配送窓 6 日 (Standard 配送典型) も Economy 判定外."""
    from tabs.tab_research_wizard import is_likely_long_window_shipping
    it = {
        "min_delivery_date": "2026-05-29T07:00:00.000Z",
        "max_delivery_date": "2026-06-04T07:00:00.000Z",
    }
    assert is_likely_long_window_shipping(it) is False


def test_is_likely_long_window_no_dates_returns_false():
    """配送日不明 → 判別不能 = False (false positive 避け、残す方針)."""
    from tabs.tab_research_wizard import is_likely_long_window_shipping
    it = {"price_usd": 30.0, "shipping_cost_usd": 5.0}
    assert is_likely_long_window_shipping(it) is False


def test_is_likely_long_window_invalid_date_returns_false():
    """parse できない日付 → 判別不能 = False."""
    from tabs.tab_research_wizard import is_likely_long_window_shipping
    it = {
        "min_delivery_date": "not-a-date",
        "max_delivery_date": "2026-06-04T07:00:00.000Z",
    }
    assert is_likely_long_window_shipping(it) is False


def test_is_likely_ddu_shipping_alias_back_compat():
    """旧名 is_likely_ddu_shipping は新名への alias として残存 (後方互換)."""
    from tabs.tab_research_wizard import is_likely_ddu_shipping, is_likely_long_window_shipping
    assert is_likely_ddu_shipping is is_likely_long_window_shipping


def test_process_browse_items_filters_economy_speedpak():
    """SpeedPAK Economy (delivery window 14 日 = Economy carrier proxy) は top から除外."""
    from tabs.tab_research_wizard import _process_browse_items
    raw = [
        # Economy carrier proxy (SpeedPAK Economy): 安いが除外される
        {
            "item_id": "v1|eco1|0", "price_usd": 30.0, "shipping_cost_usd": 5.0,
            "min_delivery_date": "2026-06-04T07:00:00.000Z",
            "max_delivery_date": "2026-06-18T07:00:00.000Z",
        },
        # Express (FedEx): 多少高いが残る (関税ポリシーは別軸、ここでは判定外)
        {
            "item_id": "v1|exp1|0", "price_usd": 50.0, "shipping_cost_usd": 25.0,
            "min_delivery_date": "2026-06-01T07:00:00.000Z",
            "max_delivery_date": "2026-06-04T07:00:00.000Z",
        },
        # 配送日不明 (判別不能、残す)
        {
            "item_id": "v1|unk1|0", "price_usd": 40.0, "shipping_cost_usd": 10.0,
        },
    ]
    top = _process_browse_items(raw, my_ebay_item_id="999")
    ids = [it["legacy_item_id"] for it in top]
    assert "eco1" not in ids, "Economy carrier (SpeedPAK Economy 系) が除外されていない"
    assert "exp1" in ids, "Express (FedEx) が残っていない"
    assert "unk1" in ids, "配送日不明は残すべき (false positive 避け)"
