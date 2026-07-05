"""依頼ボード #52 (2026-07-06) — キーワード新着監視 メルカリ風ギャラリー化の回帰テスト.

設計: .company/engineering/docs/2026-07-06-keyword-watch-gallery-mockup.html

カバー範囲:
  - migration v91 (keyword_watch_hits.confirmed_at) の冪等性 (Q2: init_db() 2 回
    連続でデータ保持・DROP/DELETE なし)
  - get_unconfirmed_hits / confirm_hit / confirm_all_hits (DB 層、確認済フラグ)
  - _build_gallery_items: ebay_item_id 紐付け有無での「eBay想定価格・想定利益」マッピング
    (算出不能時は None のまま = 「—」表示、誤情報を出さない)
  - _gallery_card_html: 想定利益マイナス時の赤字リスク badge / 未算出時の「—」表示
  - discord_category_gate.keyword が既定 OFF (Discord からタブ運用へ移行、record は継続)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    return db_mod


# ---------- migration v91 idempotency ----------

def test_v91_idempotent_init_db_twice_retains_confirmed_at(tmp_db):
    from monitor.keyword_watch_db import add_watch, record_hit_claim, confirm_hit

    wid, _ = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=v91test",
        keyword="v91test",
    )
    hid = record_hit_claim(
        watch_id=wid, found_item_url="https://item/v91",
        title="t", price_jpy=1000, image_url=None, in_price_range=True,
    )
    assert confirm_hit(hid) is True

    tmp_db.init_db()  # 再実行 (Q2 冪等性)

    from monitor.database import get_conn
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        row = c.execute(
            "SELECT confirmed_at FROM keyword_watch_hits WHERE id=?", (hid,)
        ).fetchone()
    assert ver >= 91
    assert row["confirmed_at"] is not None


def test_v91_confirmed_at_column_exists(tmp_db):
    from monitor.database import get_conn
    with get_conn() as c:
        cols = [r[1] for r in c.execute(
            "PRAGMA table_info(keyword_watch_hits)"
        ).fetchall()]
    assert "confirmed_at" in cols


# ---------- get_unconfirmed_hits / confirm_hit / confirm_all_hits ----------

def test_get_unconfirmed_hits_excludes_confirmed(tmp_db):
    from monitor.keyword_watch_db import (
        add_watch, record_hit_claim, confirm_hit, get_unconfirmed_hits,
    )

    wid, _ = add_watch(
        site="mercari", search_url="https://jp.mercari.com/search?keyword=a",
        keyword="a",
    )
    h1 = record_hit_claim(
        watch_id=wid, found_item_url="https://item/1", title="one",
        price_jpy=1000, image_url=None, in_price_range=True,
    )
    h2 = record_hit_claim(
        watch_id=wid, found_item_url="https://item/2", title="two",
        price_jpy=2000, image_url=None, in_price_range=True,
    )
    confirm_hit(h1)

    hits = get_unconfirmed_hits()
    ids = [h["hit_id"] for h in hits]
    assert h2 in ids
    assert h1 not in ids
    # watch 情報が JOIN されている
    row = next(h for h in hits if h["hit_id"] == h2)
    assert row["site"] == "mercari"
    assert row["keyword"] == "a"


def test_confirm_hit_returns_false_for_already_confirmed_or_missing(tmp_db):
    from monitor.keyword_watch_db import add_watch, record_hit_claim, confirm_hit

    wid, _ = add_watch(
        site="mercari", search_url="https://jp.mercari.com/search?keyword=b",
        keyword="b",
    )
    hid = record_hit_claim(
        watch_id=wid, found_item_url="https://item/3", title="three",
        price_jpy=500, image_url=None, in_price_range=True,
    )
    assert confirm_hit(hid) is True
    assert confirm_hit(hid) is False  # 既に確認済
    assert confirm_hit(999999) is False  # 存在しない hit_id


def test_confirm_all_hits_bulk(tmp_db):
    from monitor.keyword_watch_db import (
        add_watch, record_hit_claim, confirm_all_hits, get_unconfirmed_hits,
    )

    wid, _ = add_watch(
        site="mercari", search_url="https://jp.mercari.com/search?keyword=c",
        keyword="c",
    )
    for i in range(3):
        record_hit_claim(
            watch_id=wid, found_item_url=f"https://item/c{i}", title=f"c{i}",
            price_jpy=100 * i, image_url=None, in_price_range=True,
        )

    n = confirm_all_hits()
    assert n == 3
    assert get_unconfirmed_hits() == []
    assert confirm_all_hits() == 0  # 二回目は 0 件 (冪等)


# ---------- _build_gallery_items マッピング ----------

def test_build_gallery_items_with_ebay_item_id_computes_price_and_profit(tmp_db):
    from tabs.tab_keyword_watch import _build_gallery_items

    hits = [{
        "hit_id": 1, "watch_id": 10, "found_item_url": "https://item/x",
        "title": "test item", "price_jpy": 5000, "image_url": None,
        "in_price_range": 1, "detected_at": "2026-07-06 00:00:00",
        "site": "mercari", "keyword": "kw1", "memo": "memo1",
        "ebay_item_id": "111222333444",
    }]
    fake_listing = {"current_price": 100.0, "weight_g": 500}

    with patch("calculator.load_settings", return_value={"exchange_rate": 150.0}), \
         patch("monitor.database.get_ebay_listing_by_item_id", return_value=fake_listing), \
         patch("tasks.task_supplier_candidate_search._estimate_profit_for_candidate",
               return_value=(3000.0, 2900.0)):
        items = _build_gallery_items(hits)

    assert len(items) == 1
    item = items[0]
    assert item["ebay_price_usd"] == 100.0
    assert item["ebay_price_jpy"] == pytest.approx(15000.0)
    assert item["profit_jpy"] == 3000.0


def test_build_gallery_items_without_ebay_item_id_is_none(tmp_db):
    from tabs.tab_keyword_watch import _build_gallery_items

    hits = [{
        "hit_id": 2, "watch_id": 11, "found_item_url": "https://item/y",
        "title": "no link item", "price_jpy": 3000, "image_url": None,
        "in_price_range": 1, "detected_at": "2026-07-06 00:00:00",
        "site": "yahoo_auctions", "keyword": "kw2", "memo": "",
        "ebay_item_id": None,
    }]
    items = _build_gallery_items(hits)
    assert items[0]["ebay_price_usd"] is None
    assert items[0]["ebay_price_jpy"] is None
    assert items[0]["profit_jpy"] is None


def test_build_gallery_items_calc_failure_falls_back_to_none(tmp_db):
    """calculator が例外を投げても profit_jpy=None のまま (誤情報より空、K0)。
    価格 (ebay_price_usd) は別経路のため取得できていることも確認する。"""
    from tabs.tab_keyword_watch import _build_gallery_items

    hits = [{
        "hit_id": 3, "watch_id": 12, "found_item_url": "https://item/z",
        "title": "calc error item", "price_jpy": 4000, "image_url": None,
        "in_price_range": 1, "detected_at": "2026-07-06 00:00:00",
        "site": "mercari", "keyword": "kw3", "memo": "",
        "ebay_item_id": "555666777888",
    }]
    fake_listing = {"current_price": 80.0, "weight_g": 100}
    with patch("calculator.load_settings", return_value={"exchange_rate": 150.0}), \
         patch("monitor.database.get_ebay_listing_by_item_id", return_value=fake_listing), \
         patch("tasks.task_supplier_candidate_search._estimate_profit_for_candidate",
               side_effect=RuntimeError("boom")):
        items = _build_gallery_items(hits)

    assert items[0]["ebay_price_usd"] == 80.0
    assert items[0]["profit_jpy"] is None


def test_build_gallery_items_listing_not_found_is_none(tmp_db):
    """ebay_item_id はあるが listing が見つからない (relist で ID が変わった等)。"""
    from tabs.tab_keyword_watch import _build_gallery_items

    hits = [{
        "hit_id": 4, "watch_id": 13, "found_item_url": "https://item/w",
        "title": "orphan link item", "price_jpy": 1000, "image_url": None,
        "in_price_range": 1, "detected_at": "2026-07-06 00:00:00",
        "site": "mercari", "keyword": "kw4", "memo": "",
        "ebay_item_id": "999888777666",
    }]
    with patch("calculator.load_settings", return_value={"exchange_rate": 150.0}), \
         patch("monitor.database.get_ebay_listing_by_item_id", return_value=None):
        items = _build_gallery_items(hits)

    assert items[0]["ebay_price_usd"] is None
    assert items[0]["profit_jpy"] is None


# ---------- _gallery_card_html ----------

def test_gallery_card_html_marks_loss_as_warning():
    from tabs.tab_keyword_watch import _gallery_card_html

    item = {
        "hit_id": 1, "found_item_url": "https://item/x", "title": "赤字商品",
        "price_jpy": 5000, "image_url": None, "site": "mercari",
        "keyword": "kw1", "memo": "", "ebay_price_usd": 10.0,
        "ebay_price_jpy": 1500.0, "profit_jpy": -3500.0,
    }
    html = _gallery_card_html(item)
    assert "kwg-alert" in html
    assert "赤字リスク" in html
    assert "−¥3,500" in html


def test_gallery_card_html_shows_dash_when_unavailable():
    from tabs.tab_keyword_watch import _gallery_card_html

    item = {
        "hit_id": 2, "found_item_url": "https://item/y", "title": "リンク無し商品",
        "price_jpy": 2000, "image_url": None, "site": "yahoo_auctions",
        "keyword": "kw2", "memo": "", "ebay_price_usd": None,
        "ebay_price_jpy": None, "profit_jpy": None,
    }
    html = _gallery_card_html(item)
    assert "kwg-alert" not in html
    assert "赤字リスク" not in html
    assert ">—<" in html  # eBay想定価格欄が「—」表示


def test_confirm_hits_filter_aware(tmp_db):
    """MED-1: 指定 hit_ids の未確認分のみ確定 (フィルタ絞込中の一括確認用)。"""
    from monitor.keyword_watch_db import (
        add_watch, record_hit_claim, confirm_hits, get_unconfirmed_hits,
    )

    wid_m, _ = add_watch(
        site="mercari", search_url="https://jp.mercari.com/search?keyword=m",
        keyword="m",
    )
    wid_y, _ = add_watch(
        site="yahoo_auctions",
        search_url="https://auctions.yahoo.co.jp/search/search?p=y",
        keyword="y",
    )
    h_m1 = record_hit_claim(
        watch_id=wid_m, found_item_url="https://m/1", title="m1",
        price_jpy=100, image_url=None, in_price_range=True,
    )
    h_m2 = record_hit_claim(
        watch_id=wid_m, found_item_url="https://m/2", title="m2",
        price_jpy=200, image_url=None, in_price_range=True,
    )
    h_y1 = record_hit_claim(
        watch_id=wid_y, found_item_url="https://y/1", title="y1",
        price_jpy=300, image_url=None, in_price_range=True,
    )

    # メルカリ 2 件だけ確定 (絞込中の一括確認相当)
    n = confirm_hits([h_m1, h_m2])
    assert n == 2

    remaining = [h["hit_id"] for h in get_unconfirmed_hits()]
    assert remaining == [h_y1]

    # 空リスト = 0 件
    assert confirm_hits([]) == 0
    # 二度目 = 既に確定済で 0 件 (冪等)
    assert confirm_hits([h_m1, h_m2]) == 0


def test_count_unconfirmed_hits(tmp_db):
    """MED-2: LIMIT を跨いだ真の総件数。"""
    from monitor.keyword_watch_db import (
        add_watch, record_hit_claim, confirm_hit, count_unconfirmed_hits,
    )

    wid, _ = add_watch(
        site="mercari", search_url="https://jp.mercari.com/search?keyword=cnt",
        keyword="cnt",
    )
    ids = []
    for i in range(5):
        ids.append(record_hit_claim(
            watch_id=wid, found_item_url=f"https://cnt/{i}", title=f"c{i}",
            price_jpy=i, image_url=None, in_price_range=True,
        ))
    assert count_unconfirmed_hits() == 5
    confirm_hit(ids[0])
    assert count_unconfirmed_hits() == 4


def test_apply_gallery_filter():
    """MED-1: フィルタ純関数 (site_pick × loss_only 全パターン)。"""
    from tabs.tab_keyword_watch import _apply_gallery_filter

    items = [
        {"hit_id": 1, "site": "mercari", "profit_jpy": 100},
        {"hit_id": 2, "site": "mercari", "profit_jpy": -50},
        {"hit_id": 3, "site": "yahoo_auctions", "profit_jpy": None},
        {"hit_id": 4, "site": "yahoo_auctions", "profit_jpy": -200},
    ]
    assert [i["hit_id"] for i in _apply_gallery_filter(items, "all", False)] == [1, 2, 3, 4]
    assert [i["hit_id"] for i in _apply_gallery_filter(items, "mercari", False)] == [1, 2]
    assert [i["hit_id"] for i in _apply_gallery_filter(items, "yahoo_auctions", False)] == [3, 4]
    assert [i["hit_id"] for i in _apply_gallery_filter(items, "all", True)] == [2, 4]
    assert [i["hit_id"] for i in _apply_gallery_filter(items, "mercari", True)] == [2]


# ---------- HIGH-1 (依頼ボード#52 レビュー): gate OFF で raw POST しない ----------

def test_keyword_gate_off_crawl_does_not_post_to_discord(monkeypatch, tmp_db):
    """HIGH-1: record_and_maybe_send が gate OFF (discord_sent=False, gated=True)
    を返した時、raw requests.post での retry が起きないことを実測 (POST 呼出 0 回)。
    かつ _send_discord_for_hit は True を返し呼出側の mark_hit_notified で
    discord_sent=1 になる (resend pass の永久 churn 防止)。"""
    from tasks import task_keyword_watch_crawl as tk

    # record_and_maybe_send を gate OFF 応答 (依頼ボード#52 で keyword を off にした挙動) に固定
    monkeypatch.setattr(
        "notifiers.notification_center.record_and_maybe_send",
        lambda **kw: {
            "notification_id": 1, "discord_sent": False,
            "gated": True, "deduped": False, "severity_bypassed": False,
        },
    )
    # 追加: raw requests.post が絶対に呼ばれないよう例外を仕込む
    call_counter = {"n": 0}

    def _forbidden_post(*args, **kwargs):
        call_counter["n"] += 1
        raise AssertionError("raw requests.post must NOT be called when gate is OFF")

    # tasks.task_keyword_watch_crawl 内の retry ブロックは `import requests as _rq`
    # の後 `_rq.post(...)` を呼ぶため、`requests.post` を monkeypatch すれば効く
    monkeypatch.setattr("requests.post", _forbidden_post)

    class _Hit:
        title = "test title"
        url = "https://item/x"
        price_jpy = 1000
        image_url = None

    watch = {"id": 10, "site": "mercari", "keyword": "kw", "memo": "",
             "price_min_jpy": None, "price_max_jpy": None}

    result = tk._send_discord_for_hit(
        webhook="https://discord.example/wh",  # webhook が空でない = 「送りに行く」経路
        watch=watch, hit=_Hit(), hit_id=1,
    )

    assert result is True, "gate OFF は handled 扱いで True を返す (mark_hit_notified させる)"
    assert call_counter["n"] == 0, "gate OFF なのに raw POST が呼ばれた (ゲート迂回)"


def test_keyword_gate_off_dedupe_also_returns_true(monkeypatch, tmp_db):
    """HIGH-1 派生: deduped=True でも raw retry を打たず True を返す
    (dedupe は「意図的に送らない」経路のため gate と同扱い)。"""
    from tasks import task_keyword_watch_crawl as tk

    monkeypatch.setattr(
        "notifiers.notification_center.record_and_maybe_send",
        lambda **kw: {
            "notification_id": 2, "discord_sent": False,
            "gated": False, "deduped": True, "severity_bypassed": False,
        },
    )

    def _forbidden_post(*args, **kwargs):
        raise AssertionError("raw requests.post must NOT be called when deduped")

    monkeypatch.setattr("requests.post", _forbidden_post)

    class _Hit:
        title = "test title 2"
        url = "https://item/y"
        price_jpy = 500
        image_url = None

    watch = {"id": 11, "site": "yahoo_auctions", "keyword": "kw2", "memo": ""}
    assert tk._send_discord_for_hit(
        webhook="https://discord.example/wh",
        watch=watch, hit=_Hit(), hit_id=2,
    ) is True


def test_keyword_gate_on_but_post_fail_still_retries(monkeypatch, tmp_db):
    """HIGH-1 の残す挙動: gate ON かつ最初の POST 失敗 (discord_sent=False,
    gated=False, deduped=False) は raw retry で救済する (回帰防止)。"""
    from tasks import task_keyword_watch_crawl as tk

    monkeypatch.setattr(
        "notifiers.notification_center.record_and_maybe_send",
        lambda **kw: {
            "notification_id": 3, "discord_sent": False,
            "gated": False, "deduped": False, "severity_bypassed": False,
        },
    )
    # sleep(1.0) を no-op に (テスト高速化)
    monkeypatch.setattr(tk.time, "sleep", lambda s: None)

    class _Resp:
        status_code = 204

    call_counter = {"n": 0}

    def _ok_post(*args, **kwargs):
        call_counter["n"] += 1
        return _Resp()

    monkeypatch.setattr("requests.post", _ok_post)

    class _Hit:
        title = "test title 3"
        url = "https://item/z"
        price_jpy = 750
        image_url = None

    watch = {"id": 12, "site": "mercari", "keyword": "kw3", "memo": ""}
    assert tk._send_discord_for_hit(
        webhook="https://discord.example/wh",
        watch=watch, hit=_Hit(), hit_id=3,
    ) is True
    assert call_counter["n"] == 1, "gate ON + 初回 POST 失敗の retry が発火しない = 機会損失"


def test_gallery_card_html_positive_profit_no_warning():
    from tabs.tab_keyword_watch import _gallery_card_html

    item = {
        "hit_id": 3, "found_item_url": "https://item/z", "title": "黒字商品",
        "price_jpy": 3000, "image_url": None, "site": "mercari",
        "keyword": "kw3", "memo": "備考テスト", "ebay_price_usd": 68.0,
        "ebay_price_jpy": 10200.0, "profit_jpy": 4900.0,
    }
    html = _gallery_card_html(item)
    assert "kwg-alert" not in html
    assert "赤字リスク" not in html
    assert "+¥4,900" in html
    assert "備考テスト" in html
