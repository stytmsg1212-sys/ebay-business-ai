"""W100 H-NEW-1 regression test: yahoo_grace_until の TZ format 整合性.

検証対象:
- inventory_check が保存する形式 ("YYYY-MM-DD HH:MM:SS" UTC naive) と
  SQLite datetime('now') (同形式) の lexicographic 比較が正しく機能する
- past 時刻 → clear_yahoo_grace_if_due が 1 行 NULL 化
- future 時刻 → 0 行 (進行中 grace 保護、H-1 race fix 動作確認)

過去事故 (2026-05-06): isoformat() の "T" 区切り + offset 文字列で SQL 比較が
永遠に false 評価される silent regression が発生。本 test で再発防止。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert_listing(conn, eid, sku="ebayyh_test"):
    conn.execute(
        "INSERT INTO ebay_listings (ebay_item_id, sku, title, is_ended) "
        "VALUES (?, ?, 'tz test', 0)",
        (eid, sku),
    )


def _utc_naive_str(dt: datetime) -> str:
    """inventory_check と同じ format ("YYYY-MM-DD HH:MM:SS" UTC naive) で保存."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_grace_due_clear_with_past_utc(tmp_db):
    """past 時刻 (2h 前) を保存 → clear_yahoo_grace_if_due が 1 行 NULL 化"""
    from monitor.database import (
        get_conn, set_yahoo_grace_until, clear_yahoo_grace_if_due,
    )

    eid = "TZ_TEST_PAST"
    with get_conn() as c:
        _insert_listing(c, eid)

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    set_yahoo_grace_until(eid, _utc_naive_str(past))

    n = clear_yahoo_grace_if_due(eid)
    assert n == 1, "past 時刻が due 判定されない (TZ format mismatch / SQL 比較不整合)"

    with get_conn() as c:
        row = c.execute(
            "SELECT yahoo_grace_until FROM ebay_listings WHERE ebay_item_id=?",
            (eid,)
        ).fetchone()
    assert row[0] is None


def test_grace_future_protected(tmp_db):
    """future 時刻 (24h 後) → 0 行 (進行中 grace 保護、H-1 race fix 動作確認)"""
    from monitor.database import (
        get_conn, set_yahoo_grace_until, clear_yahoo_grace_if_due,
    )

    eid = "TZ_TEST_FUTURE"
    with get_conn() as c:
        _insert_listing(c, eid)

    future = datetime.now(timezone.utc) + timedelta(hours=24)
    set_yahoo_grace_until(eid, _utc_naive_str(future))

    n = clear_yahoo_grace_if_due(eid)
    assert n == 0, "未来 grace を誤クリア (H-1 race 保護崩壊)"

    with get_conn() as c:
        row = c.execute(
            "SELECT yahoo_grace_until FROM ebay_listings WHERE ebay_item_id=?",
            (eid,)
        ).fetchone()
    assert row[0] is not None  # 保護されている


def test_grace_iso_with_offset_does_NOT_clear(tmp_db):
    """旧形式 (isoformat with +00:00 offset) で保存すると due 判定に失敗することを documentation.

    これが H-NEW-1 の核心 bug。本 test は旧 bug の再現確認用。
    将来 inventory_check の保存形式が ISO 8601 に戻った場合、本 test が fail して
    silent regression を検知する。
    """
    from monitor.database import (
        get_conn, set_yahoo_grace_until, clear_yahoo_grace_if_due,
    )

    eid = "TZ_TEST_ISO_BUG"
    with get_conn() as c:
        _insert_listing(c, eid)

    # 2026-06-02 flake fix: 旧 `now-2h` は UTC 0-2 時帯に past が前日日付へ跨ぎ、
    # 「同日なら ISO+offset ('...T..+00:00') は naive ('... ...') より lexicographic で
    # 大きい (T=0x54 > 空白=0x20)」という本 test の前提が崩れて誤クリア (flake) した。
    # 同一 UTC 日付の過去時刻 (本日 00:00:01) を使い、日付部を必ず一致させて時刻非依存化。
    now = datetime.now(timezone.utc)
    past = now.replace(hour=0, minute=0, second=1, microsecond=0)
    # 旧 bug 形式: "2026-06-02T00:00:01+00:00"
    set_yahoo_grace_until(eid, past.isoformat())

    n = clear_yahoo_grace_if_due(eid)
    # 期待: 0 行 (lexicographic 比較で "T..+00:00" > "YYYY-MM-DD HH:MM:SS")
    # = 旧形式は機能しないことを documentation
    assert n == 0, (
        "ISO 8601 with offset でも due 判定が機能した。"
        "将来 sqlite3 の datetime() が offset 認識するようになった可能性。"
        "inventory_check の保存形式 (UTC naive) を維持する必然性が薄れる。"
    )


def test_grace_naive_utc_due_clear_at_exact_now(tmp_db):
    """過去 1 秒の grace でも clear される (境界値 / lexicographic 整合)"""
    from monitor.database import (
        get_conn, set_yahoo_grace_until, clear_yahoo_grace_if_due,
    )

    eid = "TZ_TEST_BOUNDARY"
    with get_conn() as c:
        _insert_listing(c, eid)

    # 1 秒前 (まれだが境界値)
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    set_yahoo_grace_until(eid, _utc_naive_str(past))

    n = clear_yahoo_grace_if_due(eid)
    assert n == 1, "1 秒前の境界 grace が clear されない"
