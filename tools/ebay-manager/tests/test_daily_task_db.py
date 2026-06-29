"""W292 本日の作業タブ — データ層 (daily_task_db) テスト。

設計書 §9.1-§9.4 に対応:
  §9.1: 冪等性 (init_db 2 回でデータ保持 + user_version=83)
  §9.2: 選定ロジック (売れ筋 DESC 主キー・タイブレーク・active 条件・snapshot 固定・JST 境界・プール < 10 / 0)
  §9.3: streak (連続/リセット/同日冪等/best 追従)
  §9.4: 欠落バッジ (_missing_badges 5 種)

本番 data/monitor.db を汚染しない: 全テストで tmp_path + monkeypatch を使用。
listing 識別は ebay_item_id (sku-rules.md 準拠、SKU 不使用)。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import pytest


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """fresh DB を tmp_path に作成し monitor.database.DB_PATH を差し替える."""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    return db_path


def _insert_listing(
    conn: sqlite3.Connection,
    *,
    ebay_item_id: str,
    title: str = "Test Item",
    sku: str = "stock1",
    total_sold_count: int = 0,
    initial_registered: int = 0,
    is_ended: Optional[int] = None,
    purchase_yen: Optional[float] = None,
    weight_g: Optional[float] = None,
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    lp_breakeven_usd: Optional[float] = None,
) -> None:
    """テスト用 listing を ebay_listings に挿入するヘルパー。"""
    conn.execute(
        """
        INSERT OR IGNORE INTO ebay_listings
            (ebay_item_id, sku, title, total_sold_count, initial_registered, is_ended,
             purchase_yen, weight_g, length_cm, width_cm, height_cm, lp_breakeven_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ebay_item_id, sku, title, total_sold_count, initial_registered, is_ended,
            purchase_yen, weight_g, length_cm, width_cm, height_cm, lp_breakeven_usd,
        ),
    )


def _insert_competitor(conn: sqlite3.Connection, our_item_id: str) -> None:
    """テスト用ライバル商品を competitor_products に挿入するヘルパー。"""
    conn.execute(
        """
        INSERT OR IGNORE INTO competitor_products
            (our_item_id, competitor_item_id, is_active)
        VALUES (?, ?, 1)
        """,
        (our_item_id, f"rival_{our_item_id}"),
    )


# ============================================================================
# §9.1: 冪等性
# ============================================================================


def test_v83_idempotent_user_version(tmp_db):
    """init_db 2 回実行後も user_version が最新 migration 番号 (冪等)。
    v84 (W293 ebaymag_heartbeat_log) 追加で 84 が現在の最新。"""
    import monitor.database as db_mod
    db_mod.init_db()  # 2 回目
    with sqlite3.connect(str(tmp_db)) as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 84, f"user_version が 84 でない: {ver}"


def test_v83_idempotent_data_preserved(tmp_db):
    """init_db 2 回実行後も daily_task_set に挿入したデータが保持される (Q2)。"""
    import monitor.database as db_mod
    with sqlite3.connect(str(tmp_db)) as conn:
        conn.execute(
            "INSERT INTO daily_task_set (jst_date, rank, ebay_item_id, title_snap, sold_snap) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2099-01-01", 1, "ITEM_IDEM_001", "冪等テスト品", 5),
        )

    db_mod.init_db()  # 2 回目

    with sqlite3.connect(str(tmp_db)) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM daily_task_set WHERE ebay_item_id = ?",
            ("ITEM_IDEM_001",),
        ).fetchone()[0]
    assert cnt == 1, f"2 回目 init_db でデータ消失 (Q2 冪等性違反): count={cnt}"


def test_v83_tables_exist(tmp_db):
    """daily_task_set / daily_task_streak テーブルが存在する。"""
    with sqlite3.connect(str(tmp_db)) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "daily_task_set" in tables, "daily_task_set テーブルが存在しない"
    assert "daily_task_streak" in tables, "daily_task_streak テーブルが存在しない"


# ============================================================================
# §9.2: 選定ロジック
# ============================================================================


def test_get_or_create_today_task_set_sold_desc(tmp_db, monkeypatch):
    """sold 高い listing が rank 1 になる (売れ筋 DESC 主キー)。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    with db_mod.get_conn() as conn:
        _insert_listing(conn, ebay_item_id="ITEM_LOW_001", title="低売上品", total_sold_count=1)
        _insert_listing(conn, ebay_item_id="ITEM_HIGH_001", title="高売上品", total_sold_count=99)

    tasks = ddb.get_or_create_today_task_set(today_jst="2099-06-01")
    assert len(tasks) >= 2
    # rank=1 が高売上品
    rank1 = next(t for t in tasks if t["rank"] == 1)
    assert rank1["ebay_item_id"] == "ITEM_HIGH_001", (
        f"rank 1 が高売上品でない: {rank1['ebay_item_id']}"
    )


def test_get_or_create_tiebreak_competitor_count(tmp_db, monkeypatch):
    """同 sold でライバル未登録 (competitor_count==0) が先。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    with db_mod.get_conn() as conn:
        _insert_listing(conn, ebay_item_id="ITEM_RIVAL_001", title="ライバルあり品", total_sold_count=10)
        _insert_listing(conn, ebay_item_id="ITEM_NO_RIVAL_001", title="ライバルなし品", total_sold_count=10)
        _insert_competitor(conn, "ITEM_RIVAL_001")

    tasks = ddb.get_or_create_today_task_set(today_jst="2099-06-02")
    eids = [t["ebay_item_id"] for t in tasks]
    # ライバルなし品がライバルあり品より先 (rank が小さい)
    idx_no_rival = eids.index("ITEM_NO_RIVAL_001")
    idx_rival = eids.index("ITEM_RIVAL_001")
    assert idx_no_rival < idx_rival, (
        f"ライバル未登録品がライバルあり品より後: idx_no_rival={idx_no_rival} idx_rival={idx_rival}"
    )


def test_get_or_create_active_condition(tmp_db, monkeypatch):
    """is_ended=1 の listing は選定されない (active 条件)。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    with db_mod.get_conn() as conn:
        _insert_listing(conn, ebay_item_id="ITEM_ENDED_001", title="終了品", is_ended=1, total_sold_count=100)
        _insert_listing(conn, ebay_item_id="ITEM_ACTIVE_001", title="アクティブ品", total_sold_count=5)

    tasks = ddb.get_or_create_today_task_set(today_jst="2099-06-03")
    eids = [t["ebay_item_id"] for t in tasks]
    assert "ITEM_ENDED_001" not in eids, "is_ended=1 の listing が選定されてはいけない"
    assert "ITEM_ACTIVE_001" in eids, "active listing が選定されるべき"


def test_get_or_create_excludes_initial_registered(tmp_db, monkeypatch):
    """initial_registered=1 の listing は選定されない。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    with db_mod.get_conn() as conn:
        _insert_listing(conn, ebay_item_id="ITEM_REG_001", title="登録済品", initial_registered=1, total_sold_count=100)
        _insert_listing(conn, ebay_item_id="ITEM_UNREG_001", title="未登録品", initial_registered=0, total_sold_count=5)

    tasks = ddb.get_or_create_today_task_set(today_jst="2099-06-04")
    eids = [t["ebay_item_id"] for t in tasks]
    assert "ITEM_REG_001" not in eids, "initial_registered=1 の listing が選定されてはいけない"
    assert "ITEM_UNREG_001" in eids, "initial_registered=0 の listing が選定されるべき"


def test_get_or_create_snapshot_fixed(tmp_db, monkeypatch):
    """同日 2 回 get_or_create_today_task_set → 同一 10 件・同一 rank (snapshot 固定、方針 C)。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    with db_mod.get_conn() as conn:
        for i in range(12):
            _insert_listing(conn, ebay_item_id=f"ITEM_SNAP_{i:03d}", title=f"品{i}", total_sold_count=i)

    tasks1 = ddb.get_or_create_today_task_set(today_jst="2099-06-05")

    # 新たな listing を追加 (スナップショット後)
    with db_mod.get_conn() as conn:
        _insert_listing(conn, ebay_item_id="ITEM_NEW_999", title="後追加品", total_sold_count=9999)

    tasks2 = ddb.get_or_create_today_task_set(today_jst="2099-06-05")

    # 同一日付では同一 10 件・同一 rank を返す
    assert [t["ebay_item_id"] for t in tasks1] == [t["ebay_item_id"] for t in tasks2], (
        "snapshot 固定: 同日 2 回呼出で内容が変わってはいけない"
    )
    # 後追加品は含まれない
    eids2 = [t["ebay_item_id"] for t in tasks2]
    assert "ITEM_NEW_999" not in eids2, "後追加品が snapshot に混入してはいけない"


def test_get_or_create_jst_boundary(tmp_db, monkeypatch):
    """today_jst が異なる日付では別セットが生成される。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    with db_mod.get_conn() as conn:
        _insert_listing(conn, ebay_item_id="ITEM_BOUND_001", title="境界テスト品", total_sold_count=1)

    tasks_day1 = ddb.get_or_create_today_task_set(today_jst="2099-07-01")
    tasks_day2 = ddb.get_or_create_today_task_set(today_jst="2099-07-02")

    # 同一 ebay_item_id を含む別エントリが作成される
    with sqlite3.connect(str(tmp_db)) as conn:
        rows = conn.execute(
            "SELECT jst_date FROM daily_task_set WHERE ebay_item_id = 'ITEM_BOUND_001' "
            "ORDER BY jst_date"
        ).fetchall()
    dates = [r[0] for r in rows]
    assert "2099-07-01" in dates, "day1 のエントリが存在しない"
    assert "2099-07-02" in dates, "day2 のエントリが存在しない"


def test_get_or_create_pool_less_than_10(tmp_db, monkeypatch):
    """未済 listing が 3 件なら 3 件だけ凍結 (エラーにしない)。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    with db_mod.get_conn() as conn:
        for i in range(3):
            _insert_listing(conn, ebay_item_id=f"ITEM_FEW_{i:03d}", title=f"少件数品{i}", total_sold_count=i)

    tasks = ddb.get_or_create_today_task_set(today_jst="2099-08-01")
    assert len(tasks) == 3, f"プール < 10: 3 件を期待、実際: {len(tasks)}"


def test_get_or_create_pool_empty_warns(tmp_db, monkeypatch, caplog):
    """未済 listing が 0 件なら logger.warning が発火 (Q0: silent skip 防止)。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb
    import logging

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)
    # listing を 1 件だけ追加して initial_registered=1 にする → プール空
    with db_mod.get_conn() as conn:
        _insert_listing(conn, ebay_item_id="ITEM_ALL_REG_001", title="全登録済品", initial_registered=1)

    with caplog.at_level(logging.WARNING, logger="monitor.daily_task_db"):
        tasks = ddb.get_or_create_today_task_set(today_jst="2099-09-01")

    assert len(tasks) == 0, f"空プールで tasks 空を期待、実際: {len(tasks)}"
    assert any("凍結 0 件" in r.message for r in caplog.records), (
        "凍結 0 件で logger.warning が発火しなかった (Q0 silent skip 防止違反)"
    )


# ============================================================================
# §9.3: streak
# ============================================================================


def test_streak_first_completion(tmp_db, monkeypatch):
    """初回 all_done → current_streak=1, best_streak=1。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    result = ddb.bump_streak_on_completion(today_jst="2099-10-01")
    assert result["current_streak"] == 1
    assert result["best_streak"] == 1
    assert result["last_done_date"] == "2099-10-01"


def test_streak_consecutive(tmp_db, monkeypatch):
    """連続 2 日 all_done → current_streak=2。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    ddb.bump_streak_on_completion(today_jst="2099-10-02")
    result = ddb.bump_streak_on_completion(today_jst="2099-10-03")
    assert result["current_streak"] == 2
    assert result["best_streak"] == 2


def test_streak_reset_on_gap(tmp_db, monkeypatch):
    """1 日飛ばし → current_streak=1 にリセット。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    ddb.bump_streak_on_completion(today_jst="2099-10-04")
    ddb.bump_streak_on_completion(today_jst="2099-10-05")  # streak=2
    # 10-06 をスキップして 10-07 → リセット
    result = ddb.bump_streak_on_completion(today_jst="2099-10-07")
    assert result["current_streak"] == 1, f"飛びでリセット期待: {result['current_streak']}"
    assert result["best_streak"] == 2, f"best は維持: {result['best_streak']}"


def test_streak_same_day_idempotent(tmp_db, monkeypatch):
    """同日 2 回 bump → streak 不変 (冪等)。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    result1 = ddb.bump_streak_on_completion(today_jst="2099-10-08")
    result2 = ddb.bump_streak_on_completion(today_jst="2099-10-08")
    assert result1["current_streak"] == result2["current_streak"], "同日二度押しで streak が変動"


def test_streak_best_tracks_max(tmp_db, monkeypatch):
    """best_streak は max 追従。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    # 3 日連続
    for day in ["2099-11-01", "2099-11-02", "2099-11-03"]:
        ddb.bump_streak_on_completion(today_jst=day)
    # リセット
    ddb.bump_streak_on_completion(today_jst="2099-11-10")
    result = ddb.get_streak()
    assert result["best_streak"] == 3, f"best_streak が 3 でない: {result['best_streak']}"
    assert result["current_streak"] == 1, f"リセット後 current_streak が 1 でない: {result['current_streak']}"


def test_get_streak_no_row(tmp_db, monkeypatch):
    """行不在 → 全 0 / None 返却 (副作用なし)。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    result = ddb.get_streak()
    assert result == {"current_streak": 0, "best_streak": 0, "last_done_date": None}

    # DB に行が挿入されていない (純読取)
    with db_mod.get_conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM daily_task_streak").fetchone()[0]
    assert cnt == 0, "get_streak が副作用で行を挿入してはいけない"


# ============================================================================
# §9.4: 欠落バッジ
# ============================================================================


def test_missing_badges_all_filled():
    """5 列すべて埋まり → 空 list (完備)。"""
    from monitor.daily_task_db import _missing_badges

    t = {
        "competitor_count": 2,
        "purchase_yen": 5000.0,
        "weight_g": 300.0,
        "length_cm": 10.0,
        "width_cm": 5.0,
        "height_cm": 3.0,
        "lp_breakeven_usd": 25.0,
    }
    assert _missing_badges(t) == [], "全項目埋まりなのにバッジが出た"


def test_missing_badges_no_competitor():
    """ライバル未登録 → 'ライバル未登録' バッジ。"""
    from monitor.daily_task_db import _missing_badges

    t = {
        "competitor_count": 0,
        "purchase_yen": 5000.0,
        "weight_g": 300.0,
        "length_cm": 10.0,
        "width_cm": 5.0,
        "height_cm": 3.0,
        "lp_breakeven_usd": 25.0,
    }
    badges = _missing_badges(t)
    assert "ライバル未登録" in badges


def test_missing_badges_no_purchase_yen():
    """仕入¥欠落 → '仕入¥未' バッジ。"""
    from monitor.daily_task_db import _missing_badges

    t = {
        "competitor_count": 1,
        "purchase_yen": None,
        "weight_g": 300.0,
        "length_cm": 10.0,
        "width_cm": 5.0,
        "height_cm": 3.0,
        "lp_breakeven_usd": 25.0,
    }
    assert "仕入¥未" in _missing_badges(t)


def test_missing_badges_no_weight():
    """重量欠落 → '重量未' バッジ。"""
    from monitor.daily_task_db import _missing_badges

    t = {
        "competitor_count": 1,
        "purchase_yen": 5000.0,
        "weight_g": 0,
        "length_cm": 10.0,
        "width_cm": 5.0,
        "height_cm": 3.0,
        "lp_breakeven_usd": 25.0,
    }
    assert "重量未" in _missing_badges(t)


def test_missing_badges_partial_dimension():
    """寸法 3 軸のうち 1 つ欠落 → '寸法未' バッジ。"""
    from monitor.daily_task_db import _missing_badges

    # width_cm だけ None
    t = {
        "competitor_count": 1,
        "purchase_yen": 5000.0,
        "weight_g": 300.0,
        "length_cm": 10.0,
        "width_cm": None,
        "height_cm": 3.0,
        "lp_breakeven_usd": 25.0,
    }
    assert "寸法未" in _missing_badges(t)


def test_missing_badges_no_breakeven():
    """損益分岐欠落 → '損益分岐未' バッジ。"""
    from monitor.daily_task_db import _missing_badges

    t = {
        "competitor_count": 1,
        "purchase_yen": 5000.0,
        "weight_g": 300.0,
        "length_cm": 10.0,
        "width_cm": 5.0,
        "height_cm": 3.0,
        "lp_breakeven_usd": None,
    }
    assert "損益分岐未" in _missing_badges(t)


def test_missing_badges_each_missing_one():
    """各列を 1 つずつ欠落させると対応ラベルが出る (5 ケース)。"""
    from monitor.daily_task_db import _missing_badges

    base = {
        "competitor_count": 1,
        "purchase_yen": 5000.0,
        "weight_g": 300.0,
        "length_cm": 10.0,
        "width_cm": 5.0,
        "height_cm": 3.0,
        "lp_breakeven_usd": 25.0,
    }

    cases = [
        ("competitor_count", 0, "ライバル未登録"),
        ("purchase_yen", None, "仕入¥未"),
        ("weight_g", None, "重量未"),
        ("width_cm", None, "寸法未"),      # 寸法: 1 軸欠落
        ("lp_breakeven_usd", 0, "損益分岐未"),
    ]

    for field, bad_val, expected_badge in cases:
        t = {**base, field: bad_val}
        badges = _missing_badges(t)
        assert expected_badge in badges, (
            f"{field}={bad_val!r} で '{expected_badge}' バッジが出なかった: {badges}"
        )


# ============================================================================
# §9.5: metric PRIMARY KEY 回帰検知
# ============================================================================


def test_streak_metric_is_primary_key(tmp_db):
    """daily_task_streak.metric が PRIMARY KEY であることを schema で検証。"""
    with sqlite3.connect(str(tmp_db)) as conn:
        cols = conn.execute("PRAGMA table_info(daily_task_streak)").fetchall()
    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
    pk_cols = [col[1] for col in cols if col[5] == 1]
    assert pk_cols == ["metric"], (
        f"daily_task_streak の PRIMARY KEY が 'metric' でない: pk_cols={pk_cols}"
    )


# ============================================================================
# §9.6: listing hard-delete 後の all_done 発火
# ============================================================================


def test_get_today_tasks_with_status_listing_gone_excluded(tmp_db, monkeypatch):
    """凍結後に listing が ebay_listings から物理削除された場合、
    gone を集計母数から除外して残りを全完了すると all_done=True になる。"""
    import monitor.database as db_mod
    import monitor.daily_task_db as ddb

    monkeypatch.setattr(ddb, "get_conn", db_mod.get_conn)

    # 2件の listing を用意して凍結
    with db_mod.get_conn() as conn:
        _insert_listing(conn, ebay_item_id="ITEM_GONE_001", title="削除される品", total_sold_count=10)
        _insert_listing(conn, ebay_item_id="ITEM_STAY_001", title="残る品", total_sold_count=5)

    ddb.get_or_create_today_task_set(today_jst="2099-12-01")

    # ITEM_GONE_001 を ebay_listings から物理削除
    with db_mod.get_conn() as conn:
        conn.execute("DELETE FROM ebay_listings WHERE ebay_item_id = 'ITEM_GONE_001'")

    # ITEM_STAY_001 を initial_registered=1 に更新 (残り1件を完了)
    with db_mod.get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET initial_registered = 1 WHERE ebay_item_id = 'ITEM_STAY_001'"
        )

    result = ddb.get_today_tasks_with_status(today_jst="2099-12-01")

    # tasks は 2 件（gone も含む）
    assert len(result["tasks"]) == 2, f"tasks は全件返す: {len(result['tasks'])}"

    # 集計母数 (gone 除外): total=1, done=1, all_done=True
    assert result["total"] == 1, f"total は gone 除外で 1 を期待: {result['total']}"
    assert result["done"] == 1, f"done は 1 を期待: {result['done']}"
    assert result["all_done"] is True, "残り1件を完了したので all_done=True のはず"
