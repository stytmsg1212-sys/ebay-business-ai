"""W148 (2026-05-21): キーワード新着監視 (AlertCrawler 移植) — DB 層 + ロジック回帰.

設計書: .company/engineering/docs/2026-05-20-W148-alertcrawler-keyword-watch-design.md (v2.2)

- migration v46 (keyword_watches / keyword_watch_hits) の冪等性 (Q2:
  init_db() 2 回連続でデータ保持・DROP/DELETE なし)。
- 自己修復 (テーブル不在 + ver<46 → 再作成、W140 v44 と同型 idiom)。
- add_watch UNIQUE(site, search_url) 重複防止。
- record_hit_claim claim-then-act dedup (二重 Discord 防止)。
- _check_price_range 全パターン (両方NULL = False / Q1)。
- sentinel idempotency (init_default_sentinels 2 回目で 0 件)。
- AlertCrawler legacy import: site 検出 + 価格レンジ抽出 + skip rule。
- run_keyword_watch_crawl: orphan_sites 検出 + disabled 経路。
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    return db_mod


# ---------- migration v46 idempotency & self-heal ----------

def test_v46_idempotent_init_db_twice_retains_data(tmp_db):
    """Q2: データ投入後 init_db() 再実行で keyword_watches /
    keyword_watch_hits が消えない (DROP/DELETE 不在の担保)。"""
    from monitor.keyword_watch_db import add_watch, record_hit_claim

    wid, new = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=test1",
        keyword="test1",
        price_min_jpy=1000, price_max_jpy=5000,
    )
    assert new
    hid = record_hit_claim(
        watch_id=wid, found_item_url="https://item/x",
        title="t", price_jpy=2000, image_url=None, in_price_range=True,
    )
    assert hid is not None

    tmp_db.init_db()  # 再実行

    from monitor.database import get_conn
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        wc = c.execute("SELECT COUNT(*) FROM keyword_watches").fetchone()[0]
        hc = c.execute("SELECT COUNT(*) FROM keyword_watch_hits").fetchone()[0]
    assert ver >= 46
    assert wc == 1 and hc == 1


def test_v46_self_heals_when_tables_missing(tmp_db):
    """過去に v46 CREATE が失敗していた状況 (user_version<46 かつ
    W148 テーブル不在) を再現 → 次の init_db で再作成 + version=46 自己修復。
    版数だけ進み永久欠落する事象を排除 (W140 v44 と同型)。"""
    from monitor.database import get_conn

    with get_conn() as c:
        c.execute("DROP TABLE keyword_watch_hits")
        c.execute("DROP TABLE keyword_watches")
        c.execute("PRAGMA user_version = 45")  # 「v46 未適用」を再現

    tmp_db.init_db()  # version<46 → v46 block 再突入

    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        n = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('keyword_watches','keyword_watch_hits')"
        ).fetchone()[0]
    assert ver >= 60  # cascade: init_db で v46→...→v60 まで進む (test の意図は「v46 block 再突入で必須 table 再作成 + 累積 bump 完了」、v60 = W209 news_action_reports canonical HEAD)
    assert n == 2


# ---------- add_watch / list_watches / update / delete ----------

def test_add_watch_unique_dedupe(tmp_db):
    """UNIQUE(site, search_url) で重複は静かに skip し既存 id を返す。"""
    from monitor.keyword_watch_db import add_watch
    url = "https://jp.mercari.com/search?keyword=foo"
    wid1, new1 = add_watch(site="mercari", search_url=url, keyword="foo")
    wid2, new2 = add_watch(site="mercari", search_url=url, keyword="foo")
    assert new1 and not new2
    assert wid1 == wid2


def test_update_watch_only_safe_fields(tmp_db):
    """site は不変、is_active / price / memo / keyword / search_url のみ更新可。
    (W148-fix 2026-06-01: search_url を updatable に追加 — 編集時 URL 再生成のため)。"""
    from monitor.keyword_watch_db import add_watch, update_watch, list_watches
    wid, _ = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=x",
        keyword="x",
    )
    assert update_watch(wid, keyword="y", price_min_jpy=100, is_active=0) is True
    # site のみ (許可フィールドなし) は無視 = rowcount=0 で False
    assert update_watch(wid, site="yahoo_auctions") is False
    rows = list_watches(active_only=False)
    target = next(r for r in rows if r["id"] == wid)
    assert target["keyword"] == "y"
    assert target["price_min_jpy"] == 100
    assert target["is_active"] == 0
    assert target["site"] == "mercari"  # 不変


def test_delete_watch_cascades_hits(tmp_db):
    """watch 削除で関連 hits も消える (FK 手動 cascade)。"""
    from monitor.keyword_watch_db import add_watch, record_hit_claim, delete_watch
    from monitor.database import get_conn
    wid, _ = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=z",
        keyword="z",
    )
    record_hit_claim(
        watch_id=wid, found_item_url="https://item/z1",
        title="t", price_jpy=100, image_url=None, in_price_range=False,
    )
    assert delete_watch(wid) is True
    with get_conn() as c:
        hc = c.execute(
            "SELECT COUNT(*) FROM keyword_watch_hits WHERE watch_id=?", (wid,)
        ).fetchone()[0]
    assert hc == 0


# ---------- record_hit_claim claim-then-act dedup ----------

def test_record_hit_claim_dedup(tmp_db):
    """同一 (watch_id, found_item_url) の 2 回目は None (二重 Discord 防止)。"""
    from monitor.keyword_watch_db import add_watch, record_hit_claim
    wid, _ = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=dup",
        keyword="dup",
    )
    h1 = record_hit_claim(
        watch_id=wid, found_item_url="https://item/dup1",
        title="t", price_jpy=100, image_url=None, in_price_range=True,
    )
    h2 = record_hit_claim(
        watch_id=wid, found_item_url="https://item/dup1",
        title="t (later edit)", price_jpy=200, image_url=None, in_price_range=True,
    )
    assert h1 is not None and h2 is None


# ---------- _check_price_range (Q1 両方NULL = 通知無効) ----------

@pytest.mark.parametrize("price,pmin,pmax,expected", [
    (None, 1000, 5000, False),  # 価格未取得 = 通知しない
    (2500, None, None, False),  # 両方 NULL = 通知無効 (Q1)
    (500,  1000, 5000, False),  # 下限割れ
    (6000, 1000, 5000, False),  # 上限超
    (2500, 1000, 5000, True),
    (2500, 1000, None, True),
    (2500, None, 5000, True),
    (1000, 1000, 5000, True),  # 境界
    (5000, 1000, 5000, True),  # 境界
])
def test_check_price_range_matrix(price, pmin, pmax, expected):
    from tasks.task_keyword_watch_crawl import _check_price_range
    assert _check_price_range(price, pmin, pmax) is expected


# ---------- sentinel idempotency ----------

def test_init_default_sentinels_idempotent(tmp_db):
    """2 回目以降は 0 件 (UNIQUE で重複 skip)。"""
    from monitor.keyword_watch_db import init_default_sentinels, list_active_sentinels
    n1 = init_default_sentinels()
    n2 = init_default_sentinels()
    assert n1 == 2  # mercari + yahoo_auctions
    assert n2 == 0
    sentinels = list_active_sentinels()
    assert len(sentinels) == 2
    assert all(s["is_sentinel"] == 1 for s in sentinels)


# ---------- AlertCrawler legacy import ----------

def test_legacy_site_detection():
    """yahoo_auctions / mercari のみ採用、他サイトは None。"""
    from scripts.import_alertcrawler_legacy import _detect_site
    assert _detect_site("https://auctions.yahoo.co.jp/search/search?p=x") == "yahoo_auctions"
    assert _detect_site("https://jp.mercari.com/search?keyword=x") == "mercari"
    assert _detect_site("https://www.suruga-ya.jp/search?key=x") is None
    assert _detect_site("https://fril.jp/s?query=x") is None
    assert _detect_site("not-a-url") is None


def test_legacy_price_range_yahoo_url():
    """ヤフオク URL の min=&max=N は 真実源として URL から抽出。"""
    from scripts.import_alertcrawler_legacy import _parse_price_range
    url = "https://auctions.yahoo.co.jp/search/search?min=&max=60000&p=test"
    pmin, pmax = _parse_price_range(url, "yahoo_auctions", "")
    assert pmin is None and pmax == 60000


def test_legacy_price_range_mercari_url():
    from scripts.import_alertcrawler_legacy import _parse_price_range
    url = "https://jp.mercari.com/search?keyword=test&price_min=1000&price_max=5000"
    pmin, pmax = _parse_price_range(url, "mercari", "")
    assert pmin == 1000 and pmax == 5000


def test_legacy_price_range_dataC_fallback():
    """URL に price 情報が無い時、dataC の「¥」or「\\」付き JPY を fallback で拾う。"""
    from scripts.import_alertcrawler_legacy import _parse_price_range
    # 実 AlertCrawler の dataC 例 (「\58000  \60000」)
    pmin, pmax = _parse_price_range(
        "https://jp.mercari.com/search?keyword=x",
        "mercari",
        "【古】有:\\58000  無:\\60000  最安$548",
    )
    assert pmin == 58000 and pmax == 60000


def test_legacy_decode_utf8():
    """text_factory=bytes で取り出した bytes を UTF-8 → SJIS の順で decode。"""
    from scripts.import_alertcrawler_legacy import _decode
    assert _decode(None) == ""
    assert _decode("already_str") == "already_str"
    assert _decode("日本語".encode("utf-8")) == "日本語"
    # SJIS fallback
    assert _decode("日本語".encode("cp932")) == "日本語"


# ---------- run_keyword_watch_crawl: disabled / orphan_sites ----------

def test_run_disabled_returns_success_dict():
    """enabled=False は success=True message='disabled' で返す。"""
    from tasks.task_keyword_watch_crawl import run_keyword_watch_crawl
    r = run_keyword_watch_crawl({"tasks_enabled": {"keyword_watch_crawl": {"enabled": False}}})
    assert r["success"] is True
    assert r["message"] == "disabled"
    # 必須キー全て揃う (Q0 必ず dict)
    assert all(k in r for k in
               ("watches_crawled", "new_hits", "in_range_hits", "errors",
                "discord_sent", "dom_rot_suspected", "dom_rot_orphan_sites"))


def test_run_top_level_exception_returns_dict(tmp_db, monkeypatch):
    """top-level 例外でも必ず dict (success=False) を返す (Q0)。"""
    from tasks.task_keyword_watch_crawl import run_keyword_watch_crawl
    # list_watches を例外化
    import tasks.task_keyword_watch_crawl as mod

    def boom(active_only=True):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(mod, "list_watches", boom)
    r = run_keyword_watch_crawl({})
    assert r["success"] is False
    assert "simulated DB outage" in r["message"]


def test_run_orphan_sites_detected(tmp_db, monkeypatch):
    """active watch があるのに sentinel が 0 件のサイトは orphan として記録される。"""
    from monitor.keyword_watch_db import add_watch
    # sentinel 登録せずに mercari の normal watch だけ追加
    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=orphan_test",
        keyword="orphan_test",
        price_min_jpy=1000, price_max_jpy=5000,
    )
    # 巡回時に Playwright が呼ばれないよう search_mercari を mock (空 hits)
    import tasks.task_keyword_watch_crawl as mod
    fake_mercari = MagicMock(return_value=[])
    monkeypatch.setattr(
        "monitor.mercari_search.search_mercari",
        fake_mercari,
    )

    r = mod.run_keyword_watch_crawl({})
    assert r["success"] is True
    # mercari は active だが sentinel 0 → orphan に入る
    assert "mercari" in r["dom_rot_orphan_sites"]


def test_run_sentinel_zero_triggers_dom_rot(tmp_db, monkeypatch):
    """sentinel が全て 0 件で dom_rot_suspected が増える。"""
    from monitor.keyword_watch_db import init_default_sentinels
    init_default_sentinels()
    import tasks.task_keyword_watch_crawl as mod
    # 両 search を空 hits で返す
    monkeypatch.setattr("monitor.mercari_search.search_mercari", MagicMock(return_value=[]))
    monkeypatch.setattr("monitor.yahoo_search.search_yahoo", MagicMock(return_value=[]))
    # webhook は空文字 = 実 Discord 送信無し
    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": ""}})
    assert r["success"] is True
    # 各 site で sentinel 1 件 / 0 hits = 全 sentinel zero → dom_rot 検出
    assert r["dom_rot_suspected"] == 2  # mercari + yahoo


# ---------- daily_scheduler integration ----------

def test_task_schedule_registered():
    """TASK_SCHEDULE に keyword_watch_crawl が登録され interval kind で expected slot から除外。"""
    from monitor.task_execution_log import TASK_SCHEDULE_BY_KEY
    entry = TASK_SCHEDULE_BY_KEY.get("keyword_watch_crawl")
    assert entry is not None
    assert entry["kind"] == "interval"
    assert entry["interval_minutes"] == 120


def test_schedule_config_has_keyword_watch_block():
    """schedule_config.json に W148 ブロックが存在し interval_hours=2。"""
    from pathlib import Path
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    block = cfg.get("tasks_enabled", {}).get("keyword_watch_crawl")
    assert block is not None
    assert block.get("enabled") is True
    assert block.get("interval_hours") == 2
    assert "max_watches_per_run" in block
    assert "subprocess_timeout_sec" in block


# ---------- Codex HIGH-1: legacy search_url is used (URL filter 保持) ----------

def test_legacy_search_url_is_used(tmp_db, monkeypatch):
    """w['search_url'] (URL に焼かれた min/max/category filter) が search_mercari /
    search_yahoo に直接渡されること = AlertCrawler 移植機能の根幹.

    内部 code-reviewer Opus は「列存在 = 使用」と思い込んで見落とし、
    Codex GPT-5.5 が「persist != use」silent gap として捕捉 (HIGH-1)."""
    from monitor.keyword_watch_db import add_watch
    import tasks.task_keyword_watch_crawl as mod

    legacy_url_m = (
        "https://jp.mercari.com/search?keyword=Razer&price_min=5000&price_max=20000"
        "&category_id=1234&item_condition_id=2"
    )
    legacy_url_y = (
        "https://auctions.yahoo.co.jp/search/search?p=Razer&aucminprice=5000"
        "&aucmaxprice=20000&auccat=23336"
    )
    add_watch(site="mercari", search_url=legacy_url_m, keyword="Razer",
              price_min_jpy=5000, price_max_jpy=20000)
    add_watch(site="yahoo_auctions", search_url=legacy_url_y, keyword="Razer",
              price_min_jpy=5000, price_max_jpy=20000)

    captured_m = MagicMock(return_value=[])
    captured_y = MagicMock(return_value=[])
    monkeypatch.setattr("monitor.mercari_search.search_mercari", captured_m)
    monkeypatch.setattr("monitor.yahoo_search.search_yahoo", captured_y)

    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": ""}})
    assert r["success"] is True

    # 各 search 関数が search_url 引数で 受け取っていること (URL filter 保持の verify)
    assert captured_m.call_count >= 1
    _, kw = captured_m.call_args
    assert kw.get("search_url") == legacy_url_m, \
        f"mercari search_url not propagated: {kw!r}"

    assert captured_y.call_count >= 1
    _, kw = captured_y.call_args
    assert kw.get("search_url") == legacy_url_y, \
        f"yahoo search_url not propagated: {kw!r}"


# ---------- Codex HIGH-2: sentinel exception path + orphan Discord ----------

def test_run_sentinel_exception_triggers_discord(tmp_db, monkeypatch):
    """sentinel watch が例外を吐いた時も Discord 発火 = DOM rot 検知が
    crash path (browser launch fail / OOM / network outage) で黙らない (Q0)."""
    from monitor.keyword_watch_db import init_default_sentinels
    init_default_sentinels()
    import tasks.task_keyword_watch_crawl as mod

    def boom_m(*a, **kw):
        raise RuntimeError("simulated chromium OOM")

    def boom_y(*a, **kw):
        raise RuntimeError("simulated PWTimeoutError")

    monkeypatch.setattr("monitor.mercari_search.search_mercari", boom_m)
    monkeypatch.setattr("monitor.yahoo_search.search_yahoo", boom_y)

    discord_calls = []
    monkeypatch.setattr(mod, "_send_discord_site_health",
                        lambda webhook, site, msg: discord_calls.append((site, msg)))

    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r["success"] is True
    # exception 経路の sentinel も dom_rot として扱われ Discord 発火
    sites_alerted = {site for site, _ in discord_calls}
    assert "mercari" in sites_alerted, "mercari sentinel exception path で Discord 未発火"
    assert "yahoo_auctions" in sites_alerted, "yahoo sentinel exception path で Discord 未発火"


def test_orphan_branch_sends_discord(tmp_db, monkeypatch):
    """orphan_sites (active watch あるが sentinel ゼロ) でも Discord 発火 = log warning だけで黙らない (Q0)."""
    from monitor.keyword_watch_db import add_watch
    import tasks.task_keyword_watch_crawl as mod
    add_watch(site="mercari",
              search_url="https://jp.mercari.com/search?keyword=orphan",
              keyword="orphan", price_min_jpy=1000, price_max_jpy=5000)
    monkeypatch.setattr("monitor.mercari_search.search_mercari", MagicMock(return_value=[]))

    discord_calls = []
    monkeypatch.setattr(mod, "_send_discord_site_health",
                        lambda webhook, site, msg: discord_calls.append((site, msg)))

    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r["success"] is True
    assert "mercari" in r["dom_rot_orphan_sites"]
    sites_alerted = {site for site, _ in discord_calls}
    assert "mercari" in sites_alerted, \
        "orphan branch で logger.warning のみ = Q0 silent skip"


# ---------- Codex HIGH-3: webhook 5xx retry (a) + (b) resend pass ----------

def test_webhook_5xx_then_recovered_resends_on_next_crawl(tmp_db, monkeypatch):
    """webhook 5xx で discord_sent=0 に残った in-range hit が、
    次回 crawl 末尾の resend pass で再送される (Section 232 機会 lost 防止).

    Codex HIGH-3 (a)+(b) 実装の verify."""
    from monitor.keyword_watch_db import add_watch
    import tasks.task_keyword_watch_crawl as mod

    add_watch(site="mercari",
              search_url="https://jp.mercari.com/search?keyword=hi-value",
              keyword="hi-value", price_min_jpy=1000, price_max_jpy=50000)

    class _Hit:
        def __init__(self, url, title, price, img):
            self.url = url
            self.title = title
            self.price_jpy = price
            self.image_url = img

    monkeypatch.setattr("monitor.mercari_search.search_mercari",
                        MagicMock(return_value=[_Hit(
                            "https://jp.mercari.com/item/AAA",
                            "Razer DeathAdder", 12000, None,
                        )]))

    # 1 回目: webhook 全失敗 (retry 含めて False)
    monkeypatch.setattr(mod, "_send_discord_for_hit",
                        MagicMock(return_value=False))
    r1 = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r1["success"] is True
    assert r1["in_range_hits"] == 1
    assert r1["discord_sent"] == 0  # webhook 全失敗

    # DB に discord_sent=0 の row が残る
    from monitor.database import get_conn
    with get_conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM keyword_watch_hits "
            "WHERE discord_sent=0 AND in_price_range=1"
        ).fetchone()
        assert row[0] == 1

    # 2 回目: webhook 復活、Playwright は同じ URL を返す (UNIQUE で claim None) が
    # 末尾の resend pass で discord_sent=0 row を再送して救済
    monkeypatch.setattr(mod, "_send_discord_for_hit",
                        MagicMock(return_value=True))
    r2 = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r2["success"] is True
    assert r2["discord_sent"] >= 1, \
        "resend pass で webhook 復活後の救済送信が動いていない"

    # DB の row は discord_sent=1 に更新済
    with get_conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM keyword_watch_hits "
            "WHERE discord_sent=0 AND in_price_range=1"
        ).fetchone()
        assert row[0] == 0, "resend pass 後も discord_sent=0 が残存"


def test_send_discord_for_hit_retries_once(monkeypatch):
    """_send_discord_for_hit は 1 回失敗 → 1s backoff → 2 回目成功で True を返す (a)。

    2026-07-03 実機 fb 対応で 1 回目は choke point (record_and_maybe_send) 経由、
    2 回目 (リトライ) は raw HTTP POST に変わった (DB 二重 INSERT 防止)。
    """
    import tasks.task_keyword_watch_crawl as mod

    fake_record = MagicMock(return_value={
        "notification_id": 1, "discord_sent": False,
        "gated": False, "deduped": False, "severity_bypassed": False,
    })
    fake_response = MagicMock(status_code=204)
    fake_requests_post = MagicMock(return_value=fake_response)

    monkeypatch.setattr(
        "notifiers.notification_center.record_and_maybe_send", fake_record)
    # raw retry の requests.post を捕捉。requests module は _send_discord_for_hit
    # 内で `import requests as _rq` されるので monkeypatch は sys.modules 経由で当てる。
    import requests as _real_requests
    monkeypatch.setattr(_real_requests, "post", fake_requests_post)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)  # backoff 高速化

    class _Hit:
        url = "https://jp.mercari.com/item/X"
        title = "T"
        price_jpy = 1000
        image_url = None

    ok = mod._send_discord_for_hit(
        "https://example/wh",
        {"site": "mercari", "keyword": "kw", "id": 1},
        _Hit(), hit_id=1,
    )
    assert ok is True
    # 1 回目 choke point、2 回目 raw POST の両方が走っていること
    assert fake_record.call_count == 1, "choke point 1 回目呼出が無い"
    assert fake_requests_post.call_count == 1, "raw HTTP retry が走っていない"


# ---------- Codex 2 周目: HIGH-A (resend DB error silent gap) / HIGH-B (二重送信レース) ----------

def test_resend_pass_db_error_does_not_fake_success(tmp_db, monkeypatch):
    """resend pass の DB 例外を握りつぶして success=True にする偽装成功を防ぐ (HIGH-A).

    get_unnotified_in_range_hits が落ちた時:
      - summary['success'] is False
      - errors カウンタ +1
      - Discord 警告 (_send_discord_site_health) 発火
    """
    import tasks.task_keyword_watch_crawl as mod

    def boom_query(days=7, limit=200):
        raise RuntimeError("simulated DB lock")

    monkeypatch.setattr(mod, "get_unnotified_in_range_hits", boom_query)
    discord_calls = []
    monkeypatch.setattr(mod, "_send_discord_site_health",
                        lambda webhook, site, msg: discord_calls.append((site, msg)))

    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r["success"] is False, "DB error を握りつぶして success=True (Q0 偽装成功)"
    assert "RESEND_PASS_FAILED" in r["message"]
    sites = {site for site, _ in discord_calls}
    assert "resend_pass" in sites, "DB error 時 Discord 警告未発火 = R-11 視認不能"


def test_resend_atomic_claim_prevents_double_send(tmp_db, monkeypatch):
    """同 hit に対する claim_hit_for_resend は 1 プロセスのみ True 返却 (HIGH-B).

    UI 巡回 + cron 巡回 並行で resend pass が同時実行されても Discord 2 重送信を防ぐ.
    """
    from monitor.keyword_watch_db import (
        add_watch, record_hit_claim,
        claim_hit_for_resend, release_hit_resend_claim,
    )

    wid, _ = add_watch(site="mercari",
                       search_url="https://jp.mercari.com/search?keyword=x",
                       keyword="x", price_min_jpy=1000, price_max_jpy=10000)
    hit_id = record_hit_claim(
        watch_id=wid, found_item_url="https://jp.mercari.com/item/Z",
        title="Z", price_jpy=5000, image_url=None, in_price_range=True,
    )
    assert hit_id is not None

    # 2 つの「process」が同時に claim を試みる
    ok1 = claim_hit_for_resend(hit_id)
    ok2 = claim_hit_for_resend(hit_id)
    assert ok1 is True, "最初の claim が失敗"
    assert ok2 is False, "2 つ目の claim が成功 = 二重送信レース未防御 (HIGH-B)"

    # claim 後 _send_discord_for_hit 失敗時のロールバックで次回 retry 可能
    release_hit_resend_claim(hit_id)
    ok3 = claim_hit_for_resend(hit_id)
    assert ok3 is True, "release 後の re-claim が失敗 = recovery 経路死亡"


# ---------- W148-fix (2026-06-01): _build_search_url 価格フィルタ込み URL 生成 ----------

def test_build_search_url_mercari_with_price():
    """mercari は price_min のみ URL に焼く。price_max は通知ゲート専用 (W206)。"""
    from tabs.tab_keyword_watch import _build_search_url
    url = _build_search_url("mercari", "テスト", 1000, 5000)
    assert "price_min=1000" in url
    assert "price_max" not in url  # W206: 上限は URL に焼かない
    assert "keyword=" in url
    # 新着順ソートは維持必須 (W206 でも変更不可)
    assert "sort=created_time" in url and "order=desc" in url


def test_build_search_url_yahoo_with_price():
    """yahoo_auctions は aucminprice のみ URL に焼く。aucmaxprice は通知ゲート専用 (W206)。"""
    from tabs.tab_keyword_watch import _build_search_url
    url = _build_search_url("yahoo_auctions", "テスト", 1000, 5000)
    assert "aucminprice=1000" in url
    assert "aucmaxprice" not in url  # W206: 上限は URL に焼かない
    assert "p=" in url
    # 新着順ソートは維持必須 (W206 でも変更不可)
    assert "s1=new" in url and "o1=d" in url


def test_build_search_url_omits_unset_price():
    """price_min が None / 0 の時は param を URL に付けない (0 = 未設定扱い)。
    price_max は W206 以降そもそも URL に焼かない (通知ゲート専用)。"""
    from tabs.tab_keyword_watch import _build_search_url
    # 両方 None
    u_none = _build_search_url("mercari", "x")
    assert "price_min" not in u_none and "price_max" not in u_none
    # 両方 0 (UI の未設定 fallback) = 省略
    u_zero = _build_search_url("mercari", "x", 0, 0)
    assert "price_min" not in u_zero and "price_max" not in u_zero
    # min のみ
    u_min = _build_search_url("yahoo_auctions", "x", 1000, None)
    assert "aucminprice=1000" in u_min and "aucmaxprice" not in u_min
    # max 指定でも URL には出ない (W206 通知ゲート専用)
    u_max = _build_search_url("mercari", "x", None, 5000)
    assert "price_max" not in u_max and "price_min" not in u_max


def test_build_search_url_unsupported_site():
    from tabs.tab_keyword_watch import _build_search_url
    with pytest.raises(ValueError):
        _build_search_url("rakuten", "x", 1000, 5000)


def test_update_watch_accepts_search_url(tmp_db):
    """編集保存フロー: keyword/価格変更時に再生成した search_url で巡回 URL を更新できる。"""
    from monitor.keyword_watch_db import add_watch, update_watch, list_watches
    wid, _ = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=old&status=on_sale",
        keyword="old",
    )
    new_url = "https://jp.mercari.com/search?keyword=new&status=on_sale&price_min=1000"
    assert update_watch(wid, keyword="new", price_min_jpy=1000,
                        search_url=new_url) is True
    target = next(r for r in list_watches(active_only=False) if r["id"] == wid)
    assert target["search_url"] == new_url
    assert target["keyword"] == "new"
    assert target["price_min_jpy"] == 1000


def test_update_watch_search_url_unique_collision_propagates(tmp_db):
    """別 watch と同 (site, search_url) になる更新は IntegrityError を握りつぶさず伝播 (Q0)。"""
    import sqlite3
    from monitor.keyword_watch_db import add_watch, update_watch
    url_a = "https://jp.mercari.com/search?keyword=a&status=on_sale"
    url_b = "https://jp.mercari.com/search?keyword=b&status=on_sale"
    add_watch(site="mercari", search_url=url_a, keyword="a")
    wid_b, _ = add_watch(site="mercari", search_url=url_b, keyword="b")
    # wid_b を url_a に書き換え → UNIQUE(site, search_url) 衝突
    with pytest.raises(sqlite3.IntegrityError):
        update_watch(wid_b, search_url=url_a)


# ---------- W148-fix (2026-06-01): _compute_watch_update 保存ロジック ----------

def test_compute_watch_update_regenerates_search_url_on_change():
    """keyword / 価格が変わったら search_url を再生成して含める (核心バグ修正)。"""
    from tabs.tab_keyword_watch import _compute_watch_update
    target = {
        "site": "mercari", "keyword": "old",
        "price_min_jpy": None, "price_max_jpy": None, "is_sentinel": 0,
    }
    fields = _compute_watch_update(target, "new", 1000, None, "memo", True)
    assert "search_url" in fields
    assert "keyword=new" in fields["search_url"]
    assert "price_min=1000" in fields["search_url"]


def test_compute_watch_update_no_url_when_unchanged():
    """keyword / 価格が同じなら search_url を再生成しない (memo/active のみ変更)。"""
    from tabs.tab_keyword_watch import _compute_watch_update
    target = {
        "site": "mercari", "keyword": "same",
        "price_min_jpy": 1000, "price_max_jpy": None, "is_sentinel": 0,
    }
    fields = _compute_watch_update(target, "same", 1000, None, "new memo", False)
    assert "search_url" not in fields
    assert fields["memo"] == "new memo"
    assert fields["is_active"] == 0


def test_compute_watch_update_sentinel_keyword_edit_blocked():
    """HIGH-1: sentinel の keyword 変更は黙ってドロップせず ValueError (Q0)。"""
    from tabs.tab_keyword_watch import _compute_watch_update
    target = {
        "site": "mercari", "keyword": "iPhone",
        "price_min_jpy": None, "price_max_jpy": None, "is_sentinel": 1,
    }
    with pytest.raises(ValueError):
        _compute_watch_update(target, "Android", None, None, "", True)


def test_compute_watch_update_sentinel_memo_active_allowed():
    """sentinel でも keyword/価格を変えなければ memo / active 変更は通る。"""
    from tabs.tab_keyword_watch import _compute_watch_update
    target = {
        "site": "mercari", "keyword": "iPhone",
        "price_min_jpy": None, "price_max_jpy": None, "is_sentinel": 1,
    }
    fields = _compute_watch_update(target, "iPhone", None, None, "checked", False)
    assert "search_url" not in fields  # URL 不変 (固定保護)
    assert fields["memo"] == "checked"
    assert fields["is_active"] == 0


def test_resend_pass_uses_atomic_claim_in_loop(tmp_db, monkeypatch):
    """resend pass loop が claim 経由で再送し、claim 失敗 hit は skip される (HIGH-B)."""
    from monitor.keyword_watch_db import add_watch, record_hit_claim
    import tasks.task_keyword_watch_crawl as mod

    wid, _ = add_watch(site="mercari",
                       search_url="https://jp.mercari.com/search?keyword=x",
                       keyword="x", price_min_jpy=1000, price_max_jpy=10000)
    hid_a = record_hit_claim(
        watch_id=wid, found_item_url="https://jp.mercari.com/item/A",
        title="A", price_jpy=5000, image_url=None, in_price_range=True,
    )
    hid_b = record_hit_claim(
        watch_id=wid, found_item_url="https://jp.mercari.com/item/B",
        title="B", price_jpy=6000, image_url=None, in_price_range=True,
    )

    # B は別 process が既に claim 済 (discord_sent=1) として用意
    from monitor.keyword_watch_db import claim_hit_for_resend
    assert claim_hit_for_resend(hid_b) is True  # 外部 process 相当

    # 通常 main loop で hits が 0 件になるよう mock
    monkeypatch.setattr("monitor.mercari_search.search_mercari", MagicMock(return_value=[]))
    sent_ids = []
    monkeypatch.setattr(mod, "_send_discord_for_hit",
                        lambda webhook, watch, hit, hit_id: sent_ids.append(hit_id) or True)

    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r["success"] is True
    # A は claim 成功 → 送信、B は既 claim 済 → skip
    assert hid_a in sent_ids, "A の resend が走っていない"
    assert hid_b not in sent_ids, "既 claim 済 B が二重送信 = HIGH-B 未防御"


# ---------- W206 (2026-06-01): キーワード新着監視 拡張 ----------

def test_build_search_url_excludes_price_max():
    """W206: 上限価格は URL に焼かない (通知ゲート専用、履歴保持の為)。"""
    from tabs.tab_keyword_watch import _build_search_url
    # 上限のみ指定 → URL に price_max / aucmaxprice が出ない
    u_m = _build_search_url("mercari", "x", None, 5000)
    assert "price_max" not in u_m
    u_y = _build_search_url("yahoo_auctions", "x", None, 5000)
    assert "aucmaxprice" not in u_y
    # 上下限指定でも上限のみ消える
    u_m2 = _build_search_url("mercari", "x", 1000, 5000)
    assert "price_min=1000" in u_m2 and "price_max" not in u_m2
    u_y2 = _build_search_url("yahoo_auctions", "x", 1000, 5000)
    assert "aucminprice=1000" in u_y2 and "aucmaxprice" not in u_y2


def test_v59_idempotent_ebay_item_id(tmp_db):
    """Q2: keyword_watches.ebay_item_id 追加が init_db 2 回連続でデータ保持 + ver=59。"""
    from monitor.keyword_watch_db import add_watch
    from monitor.database import get_conn

    # データ投入 (ebay_item_id 付き)
    wid, new = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=v59-test",
        keyword="v59-test",
        ebay_item_id="358505733121",
    )
    assert new

    # 2 回目の init_db でデータが消えない / 列も残る / ver>=60
    tmp_db.init_db()

    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        cols = {r[1] for r in c.execute("PRAGMA table_info(keyword_watches)").fetchall()}
        row = c.execute(
            "SELECT ebay_item_id FROM keyword_watches WHERE id=?", (wid,)
        ).fetchone()
    assert ver >= 60
    assert "ebay_item_id" in cols
    assert row is not None and row[0] == "358505733121"


def test_add_watch_with_ebay_item_id(tmp_db):
    """add_watch が ebay_item_id kwarg を受け付け、DB に INSERT される。"""
    from monitor.keyword_watch_db import add_watch, list_watches
    wid, _ = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=iid",
        keyword="iid",
        ebay_item_id="123456789012",
    )
    target = next(r for r in list_watches(active_only=False) if r["id"] == wid)
    assert target["ebay_item_id"] == "123456789012"


def test_add_watch_without_ebay_item_id_is_null(tmp_db):
    """ebay_item_id を渡さなければ NULL のまま (任意メタ)。"""
    from monitor.keyword_watch_db import add_watch, list_watches
    wid, _ = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=null-iid",
        keyword="null-iid",
    )
    target = next(r for r in list_watches(active_only=False) if r["id"] == wid)
    assert target["ebay_item_id"] is None


def test_update_watch_ebay_item_id(tmp_db):
    """update_watch で ebay_item_id を更新可、None でクリア可。"""
    from monitor.keyword_watch_db import add_watch, update_watch, list_watches
    wid, _ = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=upd",
        keyword="upd",
    )
    # 設定
    assert update_watch(wid, ebay_item_id="358505733121") is True
    t = next(r for r in list_watches(active_only=False) if r["id"] == wid)
    assert t["ebay_item_id"] == "358505733121"
    # 別 ID に更新
    assert update_watch(wid, ebay_item_id="999999999999") is True
    t2 = next(r for r in list_watches(active_only=False) if r["id"] == wid)
    assert t2["ebay_item_id"] == "999999999999"
    # None でクリア
    assert update_watch(wid, ebay_item_id=None) is True
    t3 = next(r for r in list_watches(active_only=False) if r["id"] == wid)
    assert t3["ebay_item_id"] is None


def test_compute_watch_update_includes_ebay_item_id():
    """_compute_watch_update が ebay_item_id を fields に含める。
    ebay_item_id の変更だけでは URL 再生成しない (changed 判定対象外)。"""
    from tabs.tab_keyword_watch import _compute_watch_update
    target = {
        "site": "mercari", "keyword": "same",
        "price_min_jpy": 1000, "price_max_jpy": None, "is_sentinel": 0,
        "ebay_item_id": None,
    }
    # ebay_item_id 設定だけ (keyword/価格は同じ)
    fields = _compute_watch_update(
        target, "same", 1000, None, "memo", True, ebay_item_id="123"
    )
    assert fields["ebay_item_id"] == "123"
    assert "search_url" not in fields  # URL 不変
    # None クリアも fields に出る
    fields_clear = _compute_watch_update(
        target, "same", 1000, None, "memo", True, ebay_item_id=None
    )
    assert "ebay_item_id" in fields_clear
    assert fields_clear["ebay_item_id"] is None


def test_get_ebay_meta_batch(tmp_db):
    """W207: get_ebay_meta_for_item_ids — title + current_price を ebay_item_id IN (...)
    で 1 query 取得 (N+1 回避)。None / 欠落 / 空リスト / NULL price の境界条件を網羅。

    W207 で旧 get_ebay_prices_for_item_ids を本 helper に一本化 (title も必要なため)。"""
    from monitor.keyword_watch_db import get_ebay_meta_for_item_ids
    from monitor.database import get_conn

    # 3 件 listing を直接挿入
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            ("AAA111", "stock:01", "Item A title", 99.99),
        )
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            ("BBB222", "stock:02", "Item B title", 1234.50),
        )
        # current_price NULL でも row は返る (title は使える)
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            ("CCC333", "stock:03", "Item C (no price)", None),
        )

    # 空リスト → {}
    assert get_ebay_meta_for_item_ids([]) == {}
    # None / 空文字列のみ → {}
    assert get_ebay_meta_for_item_ids([None, ""]) == {}

    # 3 件取得 + 欠落 ID + None 混在
    meta = get_ebay_meta_for_item_ids(
        ["AAA111", "BBB222", "ZZZ_missing", None, "CCC333"]
    )
    assert set(meta.keys()) == {"AAA111", "BBB222", "CCC333"}
    assert meta["AAA111"] == {"title": "Item A title", "current_price": 99.99}
    assert meta["BBB222"] == {"title": "Item B title", "current_price": 1234.50}
    # CCC333 は current_price NULL でも row は返る (title 取得可)
    assert meta["CCC333"]["title"] == "Item C (no price)"
    assert meta["CCC333"]["current_price"] is None
    # 欠落 ID は dict にない
    assert "ZZZ_missing" not in meta


def test_inherit_relist_follows_keyword_watch(tmp_db):
    """inherit_listing_on_relist が keyword_watches.ebay_item_id を旧→新 ItemID に追従更新。"""
    from monitor.keyword_watch_db import add_watch, list_watches
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    old_iid = "OLD_ID_001"
    new_iid = "NEW_ID_002"

    # 旧 ItemID の listing と、それに紐付く watch を用意
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            (old_iid, "stock:01", "old listing", 100.0),
        )
    wid, _ = add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=relist-test",
        keyword="relist-test",
        ebay_item_id=old_iid,
    )

    # relist 実行
    result = inherit_listing_on_relist(
        old_item_id=old_iid,
        new_item_id=new_iid,
        sku="stock:01",
        title="new listing",
        current_price=120.0,
    )
    # 戻り値 keyword_watch_rows = 1
    assert result.get("keyword_watch_rows") == 1

    # watch の ebay_item_id が new_iid に追従
    target = next(r for r in list_watches(active_only=False) if r["id"] == wid)
    assert target["ebay_item_id"] == new_iid


def test_inherit_relist_no_keyword_watch_is_zero_rows(tmp_db):
    """紐付け watch が無ければ keyword_watch_rows = 0 (silent skip ではなく明示)。"""
    from monitor.database import get_conn
    from tasks.task_daily_relist import inherit_listing_on_relist

    old_iid = "OLD_NO_WATCH"
    new_iid = "NEW_NO_WATCH"
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            (old_iid, "stock:01", "x", 100.0),
        )

    result = inherit_listing_on_relist(
        old_item_id=old_iid, new_item_id=new_iid,
        sku="stock:01", title="x", current_price=100.0,
    )
    assert result.get("keyword_watch_rows") == 0


def test_discord_embed_includes_ebay_price(monkeypatch):
    """W206: watch._ebay_price / ebay_item_id があれば embed fields に追加される。
    無ければ field 自体が出ない。

    2026-07-03: send_message → record_and_maybe_send に choke point 変更のため
    record_and_maybe_send の embed kwarg で受け取る。
    """
    import tasks.task_keyword_watch_crawl as mod

    sent = {}

    def fake_record(**kwargs):
        sent["embed"] = kwargs.get("embed")
        return {"notification_id": 1, "discord_sent": True,
                "gated": False, "deduped": False, "severity_bypassed": False}

    monkeypatch.setattr(
        "notifiers.notification_center.record_and_maybe_send", fake_record)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    class _Hit:
        url = "https://jp.mercari.com/item/X"
        title = "Razer DeathAdder"
        price_jpy = 5000
        image_url = None

    # case 1: ebay_item_id + _ebay_price あり → fields に両方含まれる
    watch_with = {
        "id": 1, "site": "mercari", "keyword": "kw",
        "memo": "—", "price_min_jpy": 1000, "price_max_jpy": 10000,
        "ebay_item_id": "358505733121", "_ebay_price": 89.99,
    }
    ok = mod._send_discord_for_hit("https://example/wh", watch_with, _Hit(), hit_id=1)
    assert ok is True
    field_names = [f["name"] for f in sent["embed"]["fields"]]
    assert "eBay Item ID" in field_names
    assert "eBay 販売価格" in field_names
    price_field = next(f for f in sent["embed"]["fields"] if f["name"] == "eBay 販売価格")
    # USD のみ表示 ($X,XXX.XX) / JPY 併記しない
    assert price_field["value"] == "$89.99"
    assert "¥" not in price_field["value"]
    iid_field = next(f for f in sent["embed"]["fields"] if f["name"] == "eBay Item ID")
    assert iid_field["value"] == "358505733121"

    # case 2: ebay_item_id 無 watch → 両 field とも embed に含まれない
    sent.clear()
    watch_without = {
        "id": 2, "site": "mercari", "keyword": "kw2",
        "memo": "—", "price_min_jpy": 1000, "price_max_jpy": 10000,
    }
    ok2 = mod._send_discord_for_hit("https://example/wh", watch_without, _Hit(), hit_id=2)
    assert ok2 is True
    field_names2 = [f["name"] for f in sent["embed"]["fields"]]
    assert "eBay Item ID" not in field_names2
    assert "eBay 販売価格" not in field_names2

    # case 3: ebay_item_id あり / _ebay_price 無 → Item ID のみ表示
    sent.clear()
    watch_id_only = {
        "id": 3, "site": "yahoo_auctions", "keyword": "kw3",
        "memo": "—", "price_min_jpy": 1000, "price_max_jpy": 10000,
        "ebay_item_id": "999",
        # _ebay_price 無
    }
    ok3 = mod._send_discord_for_hit("https://example/wh", watch_id_only, _Hit(), hit_id=3)
    assert ok3 is True
    field_names3 = [f["name"] for f in sent["embed"]["fields"]]
    assert "eBay Item ID" in field_names3
    assert "eBay 販売価格" not in field_names3


def test_crawl_loop_injects_ebay_price(tmp_db, monkeypatch):
    """run_keyword_watch_crawl が crawl loop 前に batch helper を呼んで
    各 watch dict に _ebay_price を注入し、_send_discord_for_hit に渡す (N+1 回避)。"""
    from monitor.keyword_watch_db import add_watch
    from monitor.database import get_conn
    import tasks.task_keyword_watch_crawl as mod

    iid = "BATCH_TEST_001"
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            (iid, "stock:01", "Watched listing", 55.55),
        )
    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=batch-test",
        keyword="batch-test", price_min_jpy=1000, price_max_jpy=50000,
        ebay_item_id=iid,
    )

    class _Hit:
        def __init__(self):
            self.url = "https://jp.mercari.com/item/BatchHit"
            self.title = "batch hit"
            self.price_jpy = 12000
            self.image_url = None

    monkeypatch.setattr(
        "monitor.mercari_search.search_mercari",
        MagicMock(return_value=[_Hit()]),
    )

    sent_watch_refs = []

    def fake_send(webhook, watch, hit, hit_id):
        sent_watch_refs.append(dict(watch))
        return True

    monkeypatch.setattr(mod, "_send_discord_for_hit", fake_send)

    # W207: ebay_item_id 付き watch では AI 判定が走るため、API 実 call を避けるべく
    # evaluate_match を mock して必ず通知する score を返す (この test の主旨は AI 判定
    # ではなく batch helper 注入の verify)。
    from monitor import claude_evaluator as ce
    from monitor.claude_evaluator import EvaluationResult
    monkeypatch.setattr(
        ce, "evaluate_match",
        lambda **kw: EvaluationResult(match_score=80, reasoning="test"),
    )

    # batch helper 呼出回数 = 1 (resend pass 含めて run 内 2 回でも OK だが、
    # main loop 前に 1 回確実に呼ばれることを check)
    # W207: get_ebay_prices_for_item_ids → get_ebay_meta_for_item_ids にリネーム.
    call_count = {"n": 0}
    real_fn = mod.get_ebay_meta_for_item_ids

    def counted(item_ids):
        call_count["n"] += 1
        return real_fn(item_ids)

    monkeypatch.setattr(mod, "get_ebay_meta_for_item_ids", counted)

    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r["success"] is True

    # _send_discord_for_hit に渡された watch dict が _ebay_price を持つ
    assert any(w.get("_ebay_price") == 55.55 for w in sent_watch_refs), \
        "crawl loop で _ebay_price 注入が動いていない (batch helper 結果が watch に乗らない)"
    # batch helper が main loop 前に呼ばれている (件数は 1 or 2、in-range hit があれば
    # resend pass も走るが detected_at >= now-7d で discord_sent=1 になるため 0 件)
    assert call_count["n"] >= 1


# ---------- W207 (2026-06-01): キーワード通知 専用 Discord チャンネル + AI 同一性判定 ----------


def test_inject_keyword_webhook_into_config(monkeypatch):
    """W207 Part1: DISCORD_KEYWORD_WEBHOOK_URL が config['discord']['keyword_webhook_url']
    に注入される (env 設定時のみ key 出現)。"""
    from notifiers.discord_notifier import inject_webhook_into_config
    kw_url = "https://discord.com/api/webhooks/777777/keyword-channel"
    monkeypatch.setenv("DISCORD_KEYWORD_WEBHOOK_URL", kw_url)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    cfg = inject_webhook_into_config({})
    assert cfg["discord"]["keyword_webhook_url"] == kw_url


def test_inject_keyword_webhook_absent_skips_key(monkeypatch):
    """W207 Part1: env 未設定なら keyword_webhook_url key 自体を作らない
    (caller の fallback to DISCORD_WEBHOOK_URL を効かせる)。"""
    from notifiers.discord_notifier import inject_webhook_into_config
    monkeypatch.delenv("DISCORD_KEYWORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/x")
    cfg = inject_webhook_into_config({})
    assert "keyword_webhook_url" not in cfg["discord"]


def test_inject_keyword_webhook_idempotent(monkeypatch):
    """W207 Part1: 既存値があれば env で上書きしない (idempotent)。"""
    from notifiers.discord_notifier import inject_webhook_into_config
    monkeypatch.setenv("DISCORD_KEYWORD_WEBHOOK_URL", "https://discord.com/api/webhooks/env/x")
    existing = "https://discord.com/api/webhooks/existing/y"
    cfg = inject_webhook_into_config(
        {"discord": {"keyword_webhook_url": existing}}
    )
    assert cfg["discord"]["keyword_webhook_url"] == existing


def test_keyword_webhook_routing_uses_dedicated_when_set(tmp_db, monkeypatch):
    """W207 Part1: keyword_webhook_url が config にあれば hit 通知がそちらへ送信される
    (DOM rot / orphan / resend 警告も同様に専用 webhook)。"""
    from monitor.keyword_watch_db import add_watch
    import tasks.task_keyword_watch_crawl as mod

    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=route",
        keyword="route", price_min_jpy=1000, price_max_jpy=50000,
    )

    class _Hit:
        url = "https://jp.mercari.com/item/RouteTest"
        title = "route hit"
        price_jpy = 5000
        image_url = None

    monkeypatch.setattr(
        "monitor.mercari_search.search_mercari",
        MagicMock(return_value=[_Hit()]),
    )
    sent_to = []
    monkeypatch.setattr(
        mod, "_send_discord_for_hit",
        lambda webhook, watch, hit, hit_id: sent_to.append(webhook) or True,
    )

    KW_WH = "https://example/keyword-channel"
    GEN_WH = "https://example/general-channel"
    r = mod.run_keyword_watch_crawl({
        "discord": {"webhook_url": GEN_WH, "keyword_webhook_url": KW_WH},
    })
    assert r["success"] is True
    # hit 通知は専用キーワード webhook へ
    assert sent_to and all(w == KW_WH for w in sent_to), \
        f"hit 通知が専用 keyword webhook に向いていない: {sent_to}"


def test_keyword_webhook_routing_falls_back_when_unset(tmp_db, monkeypatch):
    """W207 Part1: keyword_webhook_url 未設定なら既定 webhook にフォールバック
    (通知先消失を防ぐ / Q0)。"""
    from monitor.keyword_watch_db import add_watch
    import tasks.task_keyword_watch_crawl as mod

    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=fallback",
        keyword="fallback", price_min_jpy=1000, price_max_jpy=50000,
    )

    class _Hit:
        url = "https://jp.mercari.com/item/FbTest"
        title = "fb hit"
        price_jpy = 5000
        image_url = None

    monkeypatch.setattr(
        "monitor.mercari_search.search_mercari",
        MagicMock(return_value=[_Hit()]),
    )
    sent_to = []
    monkeypatch.setattr(
        mod, "_send_discord_for_hit",
        lambda webhook, watch, hit, hit_id: sent_to.append(webhook) or True,
    )

    GEN_WH = "https://example/general-channel"
    # discord に keyword_webhook_url 無し
    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": GEN_WH}})
    assert r["success"] is True
    assert sent_to and all(w == GEN_WH for w in sent_to), \
        "fallback 失敗: 既定 webhook に向いていない"


def test_keyword_webhook_dom_rot_uses_dedicated(tmp_db, monkeypatch):
    """W207 Part1: DOM rot 警告も専用 keyword webhook へ送信
    (keyword crawl task 内の全送信は専用チャンネル)。"""
    from monitor.keyword_watch_db import init_default_sentinels
    init_default_sentinels()
    import tasks.task_keyword_watch_crawl as mod

    # 両 search を空 hits = 全 sentinel zero
    monkeypatch.setattr("monitor.mercari_search.search_mercari", MagicMock(return_value=[]))
    monkeypatch.setattr("monitor.yahoo_search.search_yahoo", MagicMock(return_value=[]))

    sent_to = []
    monkeypatch.setattr(
        mod, "_send_discord_site_health",
        lambda webhook, site, msg: sent_to.append((webhook, site)) or True,
    )

    KW_WH = "https://example/kw-channel"
    GEN_WH = "https://example/general-channel"
    r = mod.run_keyword_watch_crawl({
        "discord": {"webhook_url": GEN_WH, "keyword_webhook_url": KW_WH},
    })
    assert r["success"] is True
    # site_health (DOM rot) も専用 webhook
    assert sent_to, "DOM rot Discord 未送信"
    for wh, _site in sent_to:
        assert wh == KW_WH, f"site_health が専用 webhook でない: {wh}"


# ---------- W207 Part2: AI 同一性判定 ----------


def _stub_evaluate(score: int, error: str | None = None):
    """evaluate_match を mock するための EvaluationResult スタブ生成 helper."""
    from monitor.claude_evaluator import EvaluationResult
    return EvaluationResult(match_score=score, reasoning="stub", error=error)


def test_ai_match_below_threshold_suppresses(tmp_db, monkeypatch, caplog):
    """W207 Part2: match_score < 40 → 通知抑制 + 履歴 (hit row) は残る + log 痕跡 (Q0)."""
    from monitor.keyword_watch_db import add_watch
    from monitor.database import get_conn
    import tasks.task_keyword_watch_crawl as mod
    import logging

    iid = "AI_SUPPRESS_001"
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            (iid, "stock:01", "Sony WH-1000XM5 Black", 199.99),
        )
    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=sony",
        keyword="sony", price_min_jpy=1000, price_max_jpy=50000,
        ebay_item_id=iid,
    )

    class _Hit:
        url = "https://jp.mercari.com/item/Different"
        title = "Bose QC45 Black (全く別商品)"
        price_jpy = 12000
        image_url = None

    monkeypatch.setattr("monitor.mercari_search.search_mercari",
                        MagicMock(return_value=[_Hit()]))
    # AI 判定 = match_score=30 < 40 → suppress
    monkeypatch.setattr(mod, "evaluate_match", lambda **kw: _stub_evaluate(30),
                        raising=False)
    # 重要: import 経路は _should_suppress_by_ai 内で `from monitor.claude_evaluator
    # import evaluate_match` するため module patch が必要
    from monitor import claude_evaluator as ce
    monkeypatch.setattr(ce, "evaluate_match", lambda **kw: _stub_evaluate(30))

    sent = []
    monkeypatch.setattr(mod, "_send_discord_for_hit",
                        lambda webhook, watch, hit, hit_id: sent.append(hit_id) or True)

    caplog.set_level(logging.INFO, logger="tasks.task_keyword_watch_crawl")
    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})

    assert r["success"] is True
    # 通知抑制 = discord_sent カウンタ 0
    assert r["discord_sent"] == 0
    assert r["ai_suppressed"] == 1
    assert sent == [], f"AI 抑制対象が通知された: {sent}"

    # 履歴は残る (hit row 1 件)
    with get_conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM keyword_watch_hits "
            "WHERE found_item_url=?", (_Hit.url,),
        ).fetchone()[0]
        sent_flag = c.execute(
            "SELECT discord_sent FROM keyword_watch_hits WHERE found_item_url=?",
            (_Hit.url,),
        ).fetchone()[0]
    assert n == 1, "AI 抑制 hit が DB に残っていない"
    # AI 抑制 = discord_sent=1 (resend 対象外化)
    assert sent_flag == 1, "AI 抑制 hit が discord_sent=0 のままなら resend が拾ってしまう"

    # log 痕跡 (Q0 silent skip 防止)
    suppress_logs = [r for r in caplog.records if "通知スキップ" in r.getMessage()]
    assert suppress_logs, "AI 抑制の log 痕跡が無い (Q0 silent skip)"


def test_ai_match_above_threshold_notifies(tmp_db, monkeypatch):
    """W207 Part2: match_score >= 40 → 従来通り通知される。"""
    from monitor.keyword_watch_db import add_watch
    from monitor.database import get_conn
    import tasks.task_keyword_watch_crawl as mod

    iid = "AI_NOTIFY_001"
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            (iid, "stock:01", "Sony WH-1000XM5 Black", 199.99),
        )
    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=sony",
        keyword="sony", price_min_jpy=1000, price_max_jpy=50000,
        ebay_item_id=iid,
    )

    class _Hit:
        url = "https://jp.mercari.com/item/SonyMatch"
        title = "Sony WH-1000XM5 Black 美品"
        price_jpy = 30000
        image_url = None

    monkeypatch.setattr("monitor.mercari_search.search_mercari",
                        MagicMock(return_value=[_Hit()]))
    from monitor import claude_evaluator as ce
    monkeypatch.setattr(ce, "evaluate_match", lambda **kw: _stub_evaluate(70))

    sent = []
    monkeypatch.setattr(mod, "_send_discord_for_hit",
                        lambda webhook, watch, hit, hit_id: sent.append(hit_id) or True)

    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r["success"] is True
    assert r["discord_sent"] == 1, "match_score>=40 の hit が通知されていない"
    assert r["ai_suppressed"] == 0
    assert len(sent) == 1


def test_ai_match_error_fail_open(tmp_db, monkeypatch, caplog):
    """W207 Part2: evaluate_match.error (API 未設定 / API 失敗) → fail-open 通知 (Q0)."""
    from monitor.keyword_watch_db import add_watch
    from monitor.database import get_conn
    import tasks.task_keyword_watch_crawl as mod
    import logging

    iid = "AI_FAIL_OPEN_001"
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            (iid, "stock:01", "Item title", 99.99),
        )
    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=fo",
        keyword="fo", price_min_jpy=1000, price_max_jpy=50000,
        ebay_item_id=iid,
    )

    class _Hit:
        url = "https://jp.mercari.com/item/FailOpen"
        title = "something"
        price_jpy = 5000
        image_url = None

    monkeypatch.setattr("monitor.mercari_search.search_mercari",
                        MagicMock(return_value=[_Hit()]))
    from monitor import claude_evaluator as ce
    monkeypatch.setattr(
        ce, "evaluate_match",
        lambda **kw: _stub_evaluate(0, error="ANTHROPIC_API_KEY not set"),
    )

    sent = []
    monkeypatch.setattr(mod, "_send_discord_for_hit",
                        lambda webhook, watch, hit, hit_id: sent.append(hit_id) or True)

    caplog.set_level(logging.WARNING, logger="tasks.task_keyword_watch_crawl")
    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r["success"] is True
    # fail-open: 通知される (機会を握り潰さない)
    assert r["discord_sent"] == 1, "fail-open でも通知されていない (機会喪失 = Q0)"
    assert r["ai_suppressed"] == 0
    # 痕跡 log
    api_err_logs = [r for r in caplog.records if "fail-open" in r.getMessage()]
    assert api_err_logs, "API error fail-open の log 痕跡が無い (Q0)"


def test_ai_match_unexpected_exception_fail_open(tmp_db, monkeypatch, caplog):
    """W207 Part2: evaluate_match が想定外の例外を吐いた時も fail-open 通知 (Q0)."""
    from monitor.keyword_watch_db import add_watch
    from monitor.database import get_conn
    import tasks.task_keyword_watch_crawl as mod
    import logging

    iid = "AI_EXC_001"
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            (iid, "stock:01", "T", 99.99),
        )
    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=exc",
        keyword="exc", price_min_jpy=1000, price_max_jpy=50000,
        ebay_item_id=iid,
    )

    class _Hit:
        url = "https://jp.mercari.com/item/Exc"
        title = "x"
        price_jpy = 5000
        image_url = None

    monkeypatch.setattr("monitor.mercari_search.search_mercari",
                        MagicMock(return_value=[_Hit()]))

    def boom(**kw):
        raise RuntimeError("simulated network outage")

    from monitor import claude_evaluator as ce
    monkeypatch.setattr(ce, "evaluate_match", boom)

    sent = []
    monkeypatch.setattr(mod, "_send_discord_for_hit",
                        lambda webhook, watch, hit, hit_id: sent.append(hit_id) or True)

    caplog.set_level(logging.WARNING, logger="tasks.task_keyword_watch_crawl")
    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r["success"] is True
    assert r["discord_sent"] == 1, "例外 fail-open 通知が動いていない"
    fo_logs = [r for r in caplog.records if "fail-open" in r.getMessage()]
    assert fo_logs, "想定外例外の fail-open log 痕跡なし (Q0)"


def test_ai_match_skipped_without_item_id(tmp_db, monkeypatch):
    """W207 Part2: ebay_item_id 無 watch (= _ebay_title 取得不可) → AI 判定スキップ、
    従来通り通知される。"""
    from monitor.keyword_watch_db import add_watch
    import tasks.task_keyword_watch_crawl as mod

    # ebay_item_id 無し
    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=noiid",
        keyword="noiid", price_min_jpy=1000, price_max_jpy=50000,
    )

    class _Hit:
        url = "https://jp.mercari.com/item/NoIID"
        title = "random thing"
        price_jpy = 5000
        image_url = None

    monkeypatch.setattr("monitor.mercari_search.search_mercari",
                        MagicMock(return_value=[_Hit()]))
    # evaluate_match が呼ばれたら fail (= AI 判定スキップしていない signal)
    eval_called = {"n": 0}

    def should_not_be_called(**kw):
        eval_called["n"] += 1
        return _stub_evaluate(30)

    from monitor import claude_evaluator as ce
    monkeypatch.setattr(ce, "evaluate_match", should_not_be_called)

    sent = []
    monkeypatch.setattr(mod, "_send_discord_for_hit",
                        lambda webhook, watch, hit, hit_id: sent.append(hit_id) or True)

    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r["success"] is True
    assert r["discord_sent"] == 1, "ebay_item_id 無 watch で通知されない (従来動作回帰)"
    assert eval_called["n"] == 0, "ebay_item_id 無で AI 判定が走っている (無駄 API call)"


def test_ai_match_suppressed_hit_not_resent_by_resend_pass(tmp_db, monkeypatch):
    """W207 Part2 核心: AI 抑制 hit が discord_sent=1 で記録されるため、
    次回 crawl の resend pass が拾わない (抑制無効化を防ぐ)。"""
    from monitor.keyword_watch_db import add_watch
    from monitor.database import get_conn
    import tasks.task_keyword_watch_crawl as mod

    iid = "AI_RESEND_GUARD"
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            (iid, "stock:01", "Pinpoint title", 99.99),
        )
    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=guard",
        keyword="guard", price_min_jpy=1000, price_max_jpy=50000,
        ebay_item_id=iid,
    )

    class _Hit:
        url = "https://jp.mercari.com/item/Guard"
        title = "totally unrelated thing"
        price_jpy = 5000
        image_url = None

    monkeypatch.setattr("monitor.mercari_search.search_mercari",
                        MagicMock(return_value=[_Hit()]))
    # AI: score=20 < 40 で抑制
    from monitor import claude_evaluator as ce
    monkeypatch.setattr(ce, "evaluate_match", lambda **kw: _stub_evaluate(20))
    # _send_discord_for_hit は呼ばれない想定
    sent = []
    monkeypatch.setattr(mod, "_send_discord_for_hit",
                        lambda webhook, watch, hit, hit_id: sent.append(hit_id) or True)

    # 1 回目 crawl: AI 抑制
    r1 = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r1["ai_suppressed"] == 1
    assert r1["discord_sent"] == 0
    assert sent == []

    # resend pass が拾わないことを確認: 同 row が discord_sent=1 なので
    # get_unnotified_in_range_hits の WHERE discord_sent=0 で除外される
    with get_conn() as c:
        unsent = c.execute(
            "SELECT COUNT(*) FROM keyword_watch_hits "
            "WHERE discord_sent=0 AND in_price_range=1"
        ).fetchone()[0]
    assert unsent == 0, \
        "AI 抑制 hit が discord_sent=0 のまま残存 → resend pass で抑制が無効化される"

    # 2 回目 crawl: 同じ hit (URL dedupe で record_hit_claim=None) + resend pass も拾わない
    monkeypatch.setattr("monitor.mercari_search.search_mercari",
                        MagicMock(return_value=[_Hit()]))
    r2 = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r2["discord_sent"] == 0, "2 回目 crawl で resend pass が AI 抑制 hit を送信した"
    assert sent == []


def test_ai_match_empty_candidate_title_skips_ai(tmp_db, monkeypatch):
    """W207 Part2 edge case: candidate_title が空なら AI 判定不能 → 通知側に倒す (Q0)."""
    from monitor.keyword_watch_db import add_watch
    from monitor.database import get_conn
    import tasks.task_keyword_watch_crawl as mod

    iid = "AI_EMPTY_TITLE"
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price) "
            "VALUES (?,?,?,?)",
            (iid, "stock:01", "Real eBay listing", 99.99),
        )
    add_watch(
        site="mercari",
        search_url="https://jp.mercari.com/search?keyword=empty",
        keyword="empty", price_min_jpy=1000, price_max_jpy=50000,
        ebay_item_id=iid,
    )

    class _Hit:
        url = "https://jp.mercari.com/item/EmptyTitle"
        title = ""  # 空タイトル
        price_jpy = 5000
        image_url = None

    monkeypatch.setattr("monitor.mercari_search.search_mercari",
                        MagicMock(return_value=[_Hit()]))
    # AI が呼ばれたら fail (= 空 title でも判定試みている signal)
    eval_called = {"n": 0}

    def fail(**kw):
        eval_called["n"] += 1
        return _stub_evaluate(0)

    from monitor import claude_evaluator as ce
    monkeypatch.setattr(ce, "evaluate_match", fail)

    sent = []
    monkeypatch.setattr(mod, "_send_discord_for_hit",
                        lambda webhook, watch, hit, hit_id: sent.append(hit_id) or True)

    r = mod.run_keyword_watch_crawl({"discord": {"webhook_url": "https://example/wh"}})
    assert r["success"] is True
    assert r["discord_sent"] == 1, "空 candidate title で通知されない (Q0)"
    assert eval_called["n"] == 0, "空 candidate title で AI 判定が走った (無駄 call)"

