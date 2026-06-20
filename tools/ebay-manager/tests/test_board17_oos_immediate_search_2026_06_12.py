# -*- coding: utf-8 -*-
"""依頼ボード#17 regression (2026-06-12): 売り切れ検出時の仕入先候補 即時検索.

真因 5 つ (A-E) の修正を保証する:
  A: newly_oos の eid 解決が source_url 完全一致依存 → monitored_items 由来の
     ebay_item_id を第一に使う (URL 乖離 / listing 側 NULL でも silent drop しない)
  B: 7 日 throttle が OOS イベント非対応 → 「現在の OOS イベント以降の探索」のみ skip
  C: source_status='不明' 等が要対応 UI から silent drop → status_unknown バケツ追加
  D: Yahoo 定額 (フリマ形式) 出品が text シグナル不一致で不明 stuck →
     __NEXT_DATA__ 埋込 JSON の status で判定
  E: 探索結果が user に通知されない → Discord 通知 (_notify_supplier_search_results)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# =====================================================================
# fixture: ebay_listings + supplier_candidates の最小 schema
# =====================================================================

@pytest.fixture
def oos_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE ebay_listings (
            ebay_item_id TEXT PRIMARY KEY,
            sku TEXT,
            title TEXT,
            quantity_ebay INTEGER,
            source_status TEXT,
            source TEXT,
            current_price REAL,
            rank TEXT,
            source_url TEXT,
            source_last_checked TEXT,
            risk_confirmed INTEGER DEFAULT 0,
            is_ended INTEGER DEFAULT 0,
            ebay_image_url TEXT,
            source_out_of_stock_since TEXT,
            yahoo_grace_until TEXT,
            last_supplier_search_at TEXT
        );
        CREATE TABLE supplier_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ebay_item_id TEXT,
            created_at TEXT
        );
    """)
    conn.commit()
    conn.close()

    import monitor.database as db_mod

    def _fake_get_conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db_mod, "get_conn", _fake_get_conn)
    return db_path


def _insert_listing(db_path, eid, sku, status="在庫無", oos_since=None,
                    url=None, qty=1):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO ebay_listings "
        "(ebay_item_id, sku, title, quantity_ebay, source_status, source_url, "
        " source_out_of_stock_since, is_ended, risk_confirmed) "
        "VALUES (?,?,?,?,?,?,?,0,0)",
        (eid, sku, f"title-{eid}", qty, status, url, oos_since),
    )
    conn.commit()
    conn.close()


def _insert_candidate(db_path, eid, created_at):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO supplier_candidates (ebay_item_id, created_at) VALUES (?,?)",
        (eid, created_at),
    )
    conn.commit()
    conn.close()


def _set_search_marker(db_path, eid, offset: str):
    """last_supplier_search_at (v74 探索試行マーカー) を相対時刻でセット."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE ebay_listings SET last_supplier_search_at=datetime('now', ?) "
        "WHERE ebay_item_id=?",
        (offset, eid),
    )
    conn.commit()
    conn.close()


def _get_search_marker(db_path, eid):
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT last_supplier_search_at FROM ebay_listings WHERE ebay_item_id=?",
        (eid,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _run_search(monkeypatch, changes=None, config=None,
                persisted=1, raise_error=False):
    """_start_supplier_candidate_search_async を synchronous で実行し、
    実際に探索された (eid, sku, src) list を返す。"""
    import tasks.task_inventory_check as tic
    import tasks.task_supplier_candidate_search as tscs

    searched = []

    def _fake_search(ebay_item_id, sku, config, discovered_via, **kw):
        searched.append((ebay_item_id, sku, discovered_via))
        if raise_error:
            raise RuntimeError("simulated search failure")
        return {"success": True, "found": 1, "persisted": persisted,
                "message": "fake"}

    monkeypatch.setattr(tscs, "run_supplier_candidate_search", _fake_search)
    # grace 判定は外部 HTTP を叩くため identity 化
    monkeypatch.setattr(tic, "_classify_yahoo_grace", lambda pairs: (pairs, 0))
    # Discord 通知は別 test で検証
    monkeypatch.setattr(tic, "_notify_supplier_search_results", lambda c, o: None)

    cfg = config or {"tasks_enabled": {"supplier_sweep": {
        "skip_if_searched_within_days": 7, "max_skus_per_run": 30}}}
    tic._start_supplier_candidate_search_async(
        changes or {}, cfg, synchronous=True)
    return searched


# =====================================================================
# A: newly_oos の eid 解決
# =====================================================================

def test_detect_changes_carries_ebay_item_id():
    """became_out_of_stock dict に monitored_items 由来の ebay_item_id が乗ること."""
    from tasks.task_inventory_check import detect_inventory_changes
    prev = {"results": [
        {"url": "https://x/1", "status": "在庫有", "sku": "ebayyh_p1", "source": "ヤフオク"},
    ]}
    cur = [
        {"url": "https://x/1", "status": "在庫無", "sku": "ebayyh_p1",
         "source": "ヤフオク", "ebay_id": "358000000001"},
    ]
    changes = detect_inventory_changes(cur, prev)
    assert len(changes["became_out_of_stock"]) == 1
    assert changes["became_out_of_stock"][0]["ebay_item_id"] == "358000000001"


def test_newly_oos_uses_direct_eid_when_url_diverged(oos_db, monkeypatch):
    """A 修正: listing 側 source_url が乖離/NULL でも eid 直接指定で探索されること.

    旧実装は source_url 完全一致逆引きのみで、HIOKI 8972 (listing 側
    source_url=NULL) が silent drop していた。
    """
    # listing の source_url は NULL (URL 乖離ケース)
    _insert_listing(oos_db, "358000000001", "ebayyh_p1", status="在庫有", url=None)
    changes = {"became_out_of_stock": [{
        "url": "https://page.auctions.yahoo.co.jp/jp/auction/p1",
        "sku": "ebayyh_p1", "source": "ヤフオク",
        "ebay_item_id": "358000000001",
    }]}
    searched = _run_search(monkeypatch, changes)
    assert ("358000000001", "ebayyh_p1", "pattern_1_newly_oos") in searched


def test_newly_oos_falls_back_to_source_url(oos_db, monkeypatch):
    """eid 無し (旧 JSON 互換) は従来の source_url 逆引き fallback で解決されること."""
    url = "https://page.auctions.yahoo.co.jp/jp/auction/p2"
    _insert_listing(oos_db, "358000000002", "ebayyh_p2", status="在庫有", url=url)
    changes = {"became_out_of_stock": [{
        "url": url, "sku": "ebayyh_p2", "source": "ヤフオク",
        # ebay_item_id キー無し = 旧 schema
    }]}
    searched = _run_search(monkeypatch, changes)
    assert ("358000000002", "ebayyh_p2", "pattern_1_newly_oos") in searched


# =====================================================================
# B: throttle の OOS イベント対応
# =====================================================================

def test_continuing_oos_researches_after_new_oos_event(oos_db, monkeypatch):
    """B 修正: 「探索→その後に再売切」は throttle 内でも再探索されること.

    GS-71N5 / CB100 実例: 採用 or 探索の翌日に売切 → 旧実装は 7 日ブロック。
    """
    # 3 日前に探索済み (マーカー)、しかし OOS イベントは 1 日前 (探索より後)
    _insert_listing(oos_db, "358000000003", "ebayyh_p3")
    conn = sqlite3.connect(str(oos_db))
    conn.execute("UPDATE ebay_listings SET source_out_of_stock_since="
                 "datetime('now', '-1 day') WHERE ebay_item_id='358000000003'")
    conn.commit()
    conn.close()
    _set_search_marker(oos_db, "358000000003", "-3 days")

    searched = _run_search(monkeypatch)
    assert ("358000000003", "ebayyh_p3", "pattern_1_continuing_oos") in searched


def test_continuing_oos_skips_when_searched_after_oos(oos_db, monkeypatch):
    """OOS イベント後に探索済み (throttle 内) なら skip されること."""
    _insert_listing(oos_db, "358000000004", "ebayyh_p4")
    conn = sqlite3.connect(str(oos_db))
    conn.execute("UPDATE ebay_listings SET source_out_of_stock_since="
                 "datetime('now', '-3 days') WHERE ebay_item_id='358000000004'")
    conn.commit()
    conn.close()
    # OOS (3 日前) より後 = 1 日前に探索試行済み
    _set_search_marker(oos_db, "358000000004", "-1 day")

    searched = _run_search(monkeypatch)
    assert all(eid != "358000000004" for eid, _, _ in searched)


def test_continuing_oos_null_oos_since_keeps_old_throttle(oos_db, monkeypatch):
    """oos_since NULL の行は旧仕様 (N 日 throttle のみ) に fallback すること."""
    _insert_listing(oos_db, "358000000005", "ebayyh_p5", oos_since=None)
    _set_search_marker(oos_db, "358000000005", "-2 days")

    searched = _run_search(monkeypatch)
    # 2 日前の探索試行が throttle として効く (旧挙動維持)
    assert all(eid != "358000000005" for eid, _, _ in searched)


def test_continuing_oos_old_search_expires_after_skip_days(oos_db, monkeypatch):
    """throttle 期限 (N 日) を過ぎた探索は block しないこと (既存挙動 regression)."""
    _insert_listing(oos_db, "358000000006", "ebayyh_p6", oos_since=None)
    _set_search_marker(oos_db, "358000000006", "-10 days")

    searched = _run_search(monkeypatch)
    assert ("358000000006", "ebayyh_p6", "pattern_1_continuing_oos") in searched


# =====================================================================
# B-2 (HIGH-1 regression): 探索試行マーカーによる再探索ループ防止
# =====================================================================

def test_persisted_zero_search_not_relooped_next_cycle(oos_db, monkeypatch):
    """HIGH-1: 候補 0 件 (persisted=0) の探索でも試行マーカーが記録され、
    次サイクルで再探索されないこと (毎サイクル課金 API ループの根治)."""
    _insert_listing(oos_db, "358000000020", "ebayyh_p20")
    conn = sqlite3.connect(str(oos_db))
    conn.execute("UPDATE ebay_listings SET source_out_of_stock_since="
                 "datetime('now', '-1 hour') WHERE ebay_item_id='358000000020'")
    conn.commit()
    conn.close()

    # 1 サイクル目: 探索される (persisted=0 = 候補保存なし)
    searched1 = _run_search(monkeypatch, persisted=0)
    assert ("358000000020", "ebayyh_p20", "pattern_1_continuing_oos") in searched1
    assert _get_search_marker(oos_db, "358000000020") is not None

    # 2 サイクル目: マーカーが throttle として効き再探索されない
    searched2 = _run_search(monkeypatch, persisted=0)
    assert all(eid != "358000000020" for eid, _, _ in searched2)


def test_stale_candidates_do_not_cause_reloop(oos_db, monkeypatch):
    """HIGH-1: 既存候補行が created_at < oos_since (INSERT OR IGNORE で更新されない)
    でも、探索試行マーカーが OOS 後なら skip されること (旧 SQL は毎サイクル再探索)."""
    _insert_listing(oos_db, "358000000021", "ebayyh_p21")
    conn = sqlite3.connect(str(oos_db))
    conn.execute("UPDATE ebay_listings SET source_out_of_stock_since="
                 "datetime('now', '-1 day') WHERE ebay_item_id='358000000021'")
    # 古い候補行 (OOS より前) — 旧 SQL ではこれが throttle にならずループした
    conn.execute("INSERT INTO supplier_candidates (ebay_item_id, created_at) "
                 "VALUES ('358000000021', datetime('now', '-3 days'))")
    conn.commit()
    conn.close()
    # OOS 後に探索試行済み
    _set_search_marker(oos_db, "358000000021", "-2 hours")

    searched = _run_search(monkeypatch)
    assert all(eid != "358000000021" for eid, _, _ in searched)


def test_marker_recorded_even_on_search_error(oos_db, monkeypatch):
    """HIGH-1: 探索が例外で失敗しても試行マーカーは記録されること (finally 経路)."""
    _insert_listing(oos_db, "358000000022", "ebayyh_p22")
    conn = sqlite3.connect(str(oos_db))
    conn.execute("UPDATE ebay_listings SET source_out_of_stock_since="
                 "datetime('now', '-1 hour') WHERE ebay_item_id='358000000022'")
    conn.commit()
    conn.close()

    searched = _run_search(monkeypatch, raise_error=True)
    assert ("358000000022", "ebayyh_p22", "pattern_1_continuing_oos") in searched
    assert _get_search_marker(oos_db, "358000000022") is not None


# =====================================================================
# A-2 (HIGH-2 regression): eid 直接経路の listing 検証
# =====================================================================

def _newly_changes(eid, sku):
    return {"became_out_of_stock": [{
        "url": f"https://page.auctions.yahoo.co.jp/jp/auction/{sku[7:]}",
        "sku": sku, "source": "ヤフオク", "ebay_item_id": eid,
    }]}


def test_newly_oos_skips_ended_listing_via_direct_eid(oos_db, monkeypatch, caplog):
    """HIGH-2: monitored_items 由来 eid が退役 listing (is_ended=1) を指す場合は
    探索 skip + log されること (drift 由来の課金探索防止)."""
    import logging
    _insert_listing(oos_db, "358000000030", "ebayyh_p30")
    conn = sqlite3.connect(str(oos_db))
    conn.execute("UPDATE ebay_listings SET is_ended=1 "
                 "WHERE ebay_item_id='358000000030'")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.INFO):
        searched = _run_search(
            monkeypatch, _newly_changes("358000000030", "ebayyh_p30"))
    assert all(eid != "358000000030" for eid, _, _ in searched)
    assert any("358000000030" in r.message and "skip" in r.message
               for r in caplog.records)


def test_newly_oos_skips_qty_zero_listing_via_direct_eid(oos_db, monkeypatch):
    """HIGH-2: quantity_ebay=0 (販売停止済 = RISK でない) は eid 直接でも skip."""
    _insert_listing(oos_db, "358000000031", "ebayyh_p31", qty=0)
    searched = _run_search(
        monkeypatch, _newly_changes("358000000031", "ebayyh_p31"))
    assert all(eid != "358000000031" for eid, _, _ in searched)


def test_newly_oos_skips_missing_listing_via_direct_eid(oos_db, monkeypatch):
    """HIGH-2: ebay_listings に実在しない eid (孤立 monitored_items) は skip."""
    searched = _run_search(
        monkeypatch, _newly_changes("358000000032", "ebayyh_p32"))
    assert searched == []


# =====================================================================
# C: status_unknown バケツ
# =====================================================================

def test_supply_risk_exposes_status_unknown(oos_db):
    """『不明』が status_unknown バケツに入り silent drop されないこと."""
    _insert_listing(oos_db, "358000000007", "ebayyh_p7", status="不明")
    _insert_listing(oos_db, "358000000008", "ebayyh_p8", status="在庫無")
    from monitor.database import get_ebay_listings_supply_risk
    result = get_ebay_listings_supply_risk()
    assert "status_unknown" in result
    unk_ids = {it["ebay_item_id"] for it in result["status_unknown"]}
    assert "358000000007" in unk_ids
    # 既存 2 バケツは従来通り
    oos_ids = {it["ebay_item_id"] for it in result["out_of_stock"]}
    assert "358000000008" in oos_ids
    assert "358000000007" not in oos_ids


# =====================================================================
# D: Yahoo __NEXT_DATA__ status 判定 (定額出品の不明 stuck 解消)
# =====================================================================

def _yahoo_html(status: str) -> str:
    import json
    data = {"props": {"pageProps": {"initialState": {"item": {"detail": {
        "item": {"status": status, "bids": 0}}}}}}}
    return ('<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data) + "</script></html>")


def test_yahoo_next_data_open_is_available():
    """定額出品 (購入手続きのみ、入札する無し) でも status='open' で在庫有判定."""
    from monitor.scrapers import _detect_yahoo_auction_status
    url = "https://page.auctions.yahoo.co.jp/jp/auction/t1233209327"
    assert _detect_yahoo_auction_status(url, _yahoo_html("open")) == "available"


def test_yahoo_next_data_closed_is_unavailable():
    from monitor.scrapers import _detect_yahoo_auction_status
    url = "https://page.auctions.yahoo.co.jp/jp/auction/x123"
    assert _detect_yahoo_auction_status(url, _yahoo_html("closed")) == "unavailable"


def test_yahoo_next_data_missing_returns_none():
    """JSON 取れない時は None (既定のテキスト判定へ — 確定を作らない / Q0)."""
    from monitor.scrapers import _detect_yahoo_auction_status
    url = "https://page.auctions.yahoo.co.jp/jp/auction/x123"
    assert _detect_yahoo_auction_status(url, "<html>no json</html>") is None


def test_yahoo_detector_ignores_non_yahoo_urls():
    """ヤフオク以外の URL では発動しないこと (K2 surgical)."""
    from monitor.scrapers import _detect_yahoo_auction_status
    assert _detect_yahoo_auction_status(
        "https://jp.mercari.com/item/m123", _yahoo_html("open")) is None


# =====================================================================
# E: 探索結果 Discord 通知
# =====================================================================

def test_notify_supplier_search_results_sends_embed(oos_db, monkeypatch):
    """探索結果が embed 1 本にまとまり、候補あり/なし/失敗が集計されること."""
    _insert_listing(oos_db, "358000000009", "ebayyh_p9")
    import tasks.task_inventory_check as tic
    import notifiers.discord_notifier as dn

    sent = []

    class _FakeNotifier:
        webhook_url = "https://discord.test/webhook"

        def send_message(self, msg, embed=None):
            sent.append(embed)
            return True

    # 依頼ボード#22 (2026-06-20): notifier_for("inventory") 経由に変更したため
    # DiscordNotifier ではなく notifier_for を monkeypatch する
    monkeypatch.setattr(dn, "notifier_for", lambda category="default": _FakeNotifier())
    outcomes = [
        {"eid": "358000000009", "src": "pattern_1_newly_oos",
         "persisted": 2, "found": 5, "error": None},
        {"eid": "358000000010", "src": "pattern_1_continuing_oos",
         "persisted": 0, "found": 3, "error": None},
        {"eid": "358000000011", "src": "pattern_1_continuing_oos",
         "persisted": 0, "found": 0, "error": "boom"},
    ]
    tic._notify_supplier_search_results(
        {"discord": {"webhook_url": "https://discord.test/webhook"}}, outcomes)
    assert len(sent) == 1
    desc = sent[0]["description"]
    assert "候補あり 1" in desc
    assert "候補なし 1" in desc
    assert "失敗 1" in desc
    # 商品呼称規約: title (eid 末尾 4 桁)
    assert "title-358000000009" in desc
    assert "(0009)" in desc
    assert len(desc) <= 1900  # W257 教訓


def test_notify_skips_silently_when_no_outcomes(monkeypatch):
    """探索 0 件なら通知しない (空通知 spam 防止)."""
    import tasks.task_inventory_check as tic
    import notifiers.discord_notifier as dn

    called = []
    monkeypatch.setattr(
        dn, "DiscordNotifier",
        lambda url: (_ for _ in ()).throw(AssertionError("呼ばれてはいけない")))
    tic._notify_supplier_search_results({"discord": {"webhook_url": "x"}}, [])
    assert called == []
