"""W140 (2026-05-19): listing 単位メモ + 売却時警告 — DB 層回帰.

- migration v44 (listing_notes / listing_sale_warnings) の冪等性 (Q2:
  init_db() 2 回連続でデータ保持・DROP/DELETE なし)。
- メモ CRUD (ebay_item_id キー、空文字 = メモ削除扱い、sku-rules 厳守)。
- 売却警告の claim-then-act dedup (同一 order の二重 polling → 1 回のみ
  True = Discord 二重通知防止)。
- ack/dismiss の冪等 (open のみ遷移)。
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


def test_v44_idempotent_init_db_twice_retains_data(tmp_db):
    """Q2: データ投入後 init_db() 再実行で listing_notes /
    listing_sale_warnings が消えない (DROP/DELETE 不在の担保)。"""
    db = tmp_db
    db.upsert_listing_note("IT1", "電池を抜いて発送")
    assert db.record_sale_warning("ORD1", "IT1", "電池を抜いて発送") is True

    db.init_db()  # 再実行 (起動毎呼出のシミュレート)

    assert db.get_listing_note("IT1") == "電池を抜いて発送"
    assert len(db.get_open_sale_warnings()) == 1
    from monitor.database import get_conn
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver >= 44


def test_v44_self_heals_when_tables_missing(tmp_db):
    """Codex 2段 HIGH-2: 過去に v44 の CREATE が失敗していた状況
    (user_version<44 かつ W140 テーブル不在) を再現 → 次の init_db で
    再作成 + version=44 へ自己修復 (版数だけ進み永久欠落する事象を排除)。"""
    db = tmp_db
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute("DROP TABLE listing_notes")
        c.execute("DROP TABLE listing_sale_warnings")
        c.execute("PRAGMA user_version = 43")  # 「v44 未適用」状態を再現

    db.init_db()  # version<44 → v44 block 再突入で再試行されるはず

    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        n = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('listing_notes','listing_sale_warnings')"
        ).fetchone()[0]
    assert ver == 44 and n == 2          # 再作成 + 版数前進
    db.upsert_listing_note("X", "y")     # 機能復活
    assert db.get_listing_note("X") == "y"


def test_listing_note_crud_and_empty_is_delete(tmp_db):
    """upsert→get→更新→空文字保存 (= メモ削除扱い)。"""
    db = tmp_db
    assert db.get_listing_note("IT1") is None
    db.upsert_listing_note("IT1", "通関書類に型番XYZ明記")
    assert db.get_listing_note("IT1") == "通関書類に型番XYZ明記"
    db.upsert_listing_note("IT1", "更新後メモ")          # 上書き
    assert db.get_listing_note("IT1") == "更新後メモ"
    db.upsert_listing_note("IT1", "")                     # 削除扱い
    assert db.get_listing_note("IT1") == ""               # 行は残るが空


def test_record_sale_warning_claim_then_act_dedup(tmp_db):
    """同一 (order_id, ebay_item_id) の二重 polling → 初回のみ True、
    2 回目 False (Discord 二重通知防止)。警告行は 1 件のみ。"""
    db = tmp_db
    assert db.record_sale_warning("ORD1", "IT1", "メモA") is True
    assert db.record_sale_warning("ORD1", "IT1", "メモA") is False  # 重複
    # 別注文・同 listing は別警告 (relist 後 別 order も拾う)
    assert db.record_sale_warning("ORD2", "IT1", "メモA") is True
    opens = db.get_open_sale_warnings()
    assert len(opens) == 2
    assert {o["order_id"] for o in opens} == {"ORD1", "ORD2"}
    # title 未登録 listing は LEFT JOIN で None (クラッシュしない)
    assert opens[0]["title"] is None


def test_ack_and_dismiss_are_idempotent(tmp_db):
    """ack/dismiss は status='open' のみ遷移 = 冪等。"""
    db = tmp_db
    db.record_sale_warning("ORD1", "IT1", "メモ")
    wid = db.get_open_sale_warnings()[0]["id"]

    assert db.ack_sale_warning(wid) is True       # open→acked
    assert db.ack_sale_warning(wid) is False      # 既 acked = no-op
    assert db.dismiss_sale_warning(wid) is False  # open でない = no-op
    assert db.get_open_sale_warnings() == []      # バナーから消える

    # dismiss 経路も冪等
    db.record_sale_warning("ORD2", "IT2", "x")
    wid2 = db.get_open_sale_warnings()[0]["id"]
    assert db.dismiss_sale_warning(wid2) is True
    assert db.dismiss_sale_warning(wid2) is False


def test_set_discord_sent_trace(tmp_db):
    """claim 成立後の Discord 送信痕跡列 (Q0: 送信可否を DB に残す)。"""
    db = tmp_db
    db.record_sale_warning("ORD1", "IT1", "メモ")
    db.set_sale_warning_discord_sent("ORD1", "IT1", True)
    from monitor.database import get_conn
    with get_conn() as c:
        v = c.execute(
            "SELECT discord_sent FROM listing_sale_warnings "
            "WHERE order_id='ORD1' AND ebay_item_id='IT1'"
        ).fetchone()[0]
    assert v == 1


# ── _process_memo_sale_warning (注文確定 hook) ──

def _seed_listing(db, eid: str, title: str = "Sample Item"):
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO ebay_listings (ebay_item_id, title) "
            "VALUES (?, ?)", (eid, title))


def test_memo_sale_warning_claims_once_and_notifies(tmp_db):
    """メモ付き listing 売却 → 警告 1 行 + Discord 1 回。
    同一注文の二重 polling では Discord を再送しない (dedup)。"""
    db = tmp_db
    _seed_listing(db, "IT1", "Camera Flash")
    db.upsert_listing_note("IT1", "電池を抜いて発送")
    from tasks import task_order_alert as toa
    order = {"order_id": "ORD1", "ebay_item_id": "IT1"}

    with patch.object(toa, "_send_discord", return_value=True) as snd:
        assert toa._process_memo_sale_warning(order, "http://wh") is True
        assert toa._process_memo_sale_warning(order, "http://wh") is False
    assert snd.call_count == 1                       # Discord は 1 回のみ
    opens = db.get_open_sale_warnings()
    assert len(opens) == 1 and opens[0]["order_id"] == "ORD1"
    assert opens[0]["note_snapshot"] == "電池を抜いて発送"
    # embed にメモ本文と order が含まれる
    embed = snd.call_args[0][1]
    assert "電池を抜いて発送" in embed["description"]
    assert "ORD1" in embed["description"]


def test_memo_sale_warning_missing_item_id_leaves_trace(tmp_db, caplog):
    """Codex 2段 HIGH-1 (Q0): ItemID 欠落注文は SKU fallback 禁止で
    メモ評価不能 → silent return せず logger.warning で痕跡を残す。"""
    import logging
    from tasks import task_order_alert as toa
    with caplog.at_level(logging.WARNING):
        with patch.object(toa, "_send_discord", return_value=True) as snd:
            out = toa._process_memo_sale_warning(
                {"order_id": "ORDX", "ebay_item_id": ""}, "http://wh")
    assert out is False
    snd.assert_not_called()
    assert "ebay_item_id 無し" in caplog.text          # Q0 痕跡
    assert tmp_db.get_open_sale_warnings() == []        # 警告行も作らない


def test_memo_sale_warning_skips_listing_without_note(tmp_db):
    """メモ無し listing は対象外 (False、警告行も作らない = 正当な非該当)。"""
    db = tmp_db
    _seed_listing(db, "IT9")
    from tasks import task_order_alert as toa
    with patch.object(toa, "_send_discord", return_value=True) as snd:
        out = toa._process_memo_sale_warning(
            {"order_id": "O9", "ebay_item_id": "IT9"}, "http://wh")
    assert out is False
    snd.assert_not_called()
    assert db.get_open_sale_warnings() == []


def test_memo_sale_warning_survives_discord_failure(tmp_db):
    """Discord 送信失敗でも警告は open のまま (MonoDeck バナーで残る =
    発送見落とし防止の主経路、Q0: 失敗を握り潰さず痕跡 discord_sent=0)。"""
    db = tmp_db
    db.upsert_listing_note("IT1", "通関書類に型番明記")
    from tasks import task_order_alert as toa
    with patch.object(toa, "_send_discord", return_value=False):
        assert toa._process_memo_sale_warning(
            {"order_id": "ORD1", "ebay_item_id": "IT1"}, "http://wh") is True
    opens = db.get_open_sale_warnings()
    assert len(opens) == 1                            # バナーに残る
    from monitor.database import get_conn
    with get_conn() as c:
        sent = c.execute(
            "SELECT discord_sent FROM listing_sale_warnings "
            "WHERE order_id='ORD1'").fetchone()[0]
    assert sent == 0                                  # 失敗痕跡


# ── 再出品 (End→Sell similar) でのメモ引き継ぎ ──

def test_relist_inherits_memo_old_to_new(tmp_db):
    """inherit_listing_on_relist が listing メモを旧→新 ebay_item_id へ
    引き継ぐ (user 確定 2026-05-19 = 引き継ぎ)。"""
    db = tmp_db
    _seed_listing(db, "OLD1", "Vintage EQ")
    db.upsert_listing_note("OLD1", "電源ケーブル別送")
    from tasks.task_daily_relist import inherit_listing_on_relist

    r = inherit_listing_on_relist(
        "OLD1", "NEW1", "stock:01", "Vintage EQ", 100.0)
    assert r["note_rows"] == 1
    assert db.get_listing_note("NEW1") == "電源ケーブル別送"
    assert db.get_listing_note("OLD1") == "電源ケーブル別送"  # 旧も残存


def test_relist_keeps_existing_new_note_and_skips_empty(tmp_db):
    """新側に既存メモがあれば尊重 (DO NOTHING)。空メモはコピーしない。"""
    db = tmp_db
    # 既存メモが新側にある → 上書きしない
    db.upsert_listing_note("OLDa", "旧メモ")
    db.upsert_listing_note("NEWa", "新側で先に書いたメモ")
    from tasks.task_daily_relist import inherit_listing_on_relist
    r1 = inherit_listing_on_relist("OLDa", "NEWa", "s", "t", 1.0)
    assert r1["note_rows"] == 0
    assert db.get_listing_note("NEWa") == "新側で先に書いたメモ"

    # 旧メモが空 → コピーしない
    db.upsert_listing_note("OLDb", "")
    r2 = inherit_listing_on_relist("OLDb", "NEWb", "s", "t", 1.0)
    assert r2["note_rows"] == 0
    assert db.get_listing_note("NEWb") is None
