"""W301 AI 店長 Phase1 S6 (2026-07-02): MonoDeck UI (AI 判定列 / Shadow バッジ /
pricing_eligible トグル / Shadow 突合レポート / 昇格ボタン) の表示ロジック単体テスト.

設計書: .company/engineering/docs/2026-06-24-ai-manager-phase1-design.md §7(S6)

対象:
  - migration v87 (pricing_eligible_change_log) 冪等性
  - set_competitor_pricing_eligible: 競合 1 件のみ更新 + 変更ログ記録
  - get_shadow_reconciliation_report: 不一致率 / noise 誤棄却率 / 卒業基準の分子分母
  - get_competitors_with_pricing (monitor/lowest_price.py 拡張): AI 判定 LEFT JOIN

Streamlit 実機・Playwright 検証は本タスクスコープ外 (次段 Q1 で main が指示)。
"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


# ────────────────────────────────────────────────────────────────
# migration v87
# ────────────────────────────────────────────────────────────────

def test_v87_table_exists_and_version(tmp_db):
    from monitor.database import get_conn
    with get_conn() as c:
        assert _table_exists(c, "pricing_eligible_change_log")
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 87, f"user_version={ver} (期待 >=87)"


def test_v87_idempotent_data_preserved(tmp_db):
    """init_db 2 回実行でも pricing_eligible_change_log のデータが保持される (Q2)."""
    from monitor.database import get_conn, init_db
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id) "
            "VALUES ('OUR1','COMP1')"
        )
        cp_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='COMP1'"
        ).fetchone()[0]
        c.execute(
            "INSERT INTO pricing_eligible_change_log "
            "(competitor_product_id, our_item_id, competitor_item_id, "
            " old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
            (cp_id, "OUR1", "COMP1", 0, 1, "user"),
        )
    init_db()
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 87, f"version drift: {ver}"
        count = c.execute(
            "SELECT COUNT(*) FROM pricing_eligible_change_log"
        ).fetchone()[0]
        assert count == 1, "pricing_eligible_change_log データ消失 (Q2 冪等性違反)"


def test_v87_forced_reentry_no_crash(tmp_db):
    from monitor.database import get_conn, init_db
    with get_conn() as c:
        c.execute("PRAGMA user_version = 86")
    init_db()  # v87 block 再突入 (CREATE TABLE IF NOT EXISTS 冪等) → 落ちないこと
    with sqlite3.connect(tmp_db) as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 87
        assert _table_exists(c, "pricing_eligible_change_log")


def test_v87_no_sku_column(tmp_db):
    """sku-rules.md: listing 識別は competitor_products.id (PK) / ebay_item_id /
    competitor_item_id、sku 列は持たない."""
    from monitor.database import get_conn
    with get_conn() as c:
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info(pricing_eligible_change_log)").fetchall()}
        assert "sku" not in cols


# ────────────────────────────────────────────────────────────────
# set_competitor_pricing_eligible: 1 件のみ更新 + 変更ログ
# ────────────────────────────────────────────────────────────────

def test_set_pricing_eligible_updates_single_row_only(tmp_db):
    from monitor.database import get_conn, set_competitor_pricing_eligible
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible) VALUES ('OUR1','COMP1',0)"
        )
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible) VALUES ('OUR1','COMP2',0)"
        )
        cp1_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='COMP1'"
        ).fetchone()[0]

    set_competitor_pricing_eligible(cp1_id, True, changed_by="user")

    with get_conn() as c:
        row1 = c.execute(
            "SELECT pricing_eligible FROM competitor_products WHERE competitor_item_id='COMP1'"
        ).fetchone()
        row2 = c.execute(
            "SELECT pricing_eligible FROM competitor_products WHERE competitor_item_id='COMP2'"
        ).fetchone()
        assert row1[0] == 1, "対象行が ON にならなかった"
        assert row2[0] == 0, "対象外の competitor_products 行が変更された (影響範囲逸脱)"


def test_set_pricing_eligible_writes_change_log(tmp_db):
    from monitor.database import get_conn, set_competitor_pricing_eligible
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible) VALUES ('OUR1','COMP1',0)"
        )
        cp_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='COMP1'"
        ).fetchone()[0]

    set_competitor_pricing_eligible(cp_id, True, changed_by="user")
    set_competitor_pricing_eligible(cp_id, False, changed_by="user")

    with get_conn() as c:
        rows = c.execute(
            "SELECT old_value, new_value, changed_by FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=? ORDER BY id",
            (cp_id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["old_value"] == 0 and rows[0]["new_value"] == 1
        assert rows[1]["old_value"] == 1 and rows[1]["new_value"] == 0
        assert rows[0]["changed_by"] == "user"


def test_set_pricing_eligible_unknown_id_raises(tmp_db):
    from monitor.database import set_competitor_pricing_eligible
    with pytest.raises(ValueError):
        set_competitor_pricing_eligible(999999, True)


# ────────────────────────────────────────────────────────────────
# get_shadow_reconciliation_report: 分子/分母
# ────────────────────────────────────────────────────────────────

def _insert_classification(conn, ebay_item_id, competitor_item_id, classification,
                            would_be_eligible):
    conn.execute(
        """INSERT INTO rival_classifications
           (ebay_item_id, competitor_item_id, classification, would_be_eligible,
            shadow_mode)
           VALUES (?,?,?,?,1)""",
        (ebay_item_id, competitor_item_id, classification, would_be_eligible),
    )


def test_shadow_report_no_data_returns_none_rates(tmp_db):
    from monitor.database import get_shadow_reconciliation_report
    report = get_shadow_reconciliation_report()
    assert report["ai_real_count"] == 0
    assert report["mismatch_rate"] is None
    assert report["ai_noise_count"] == 0
    assert report["noise_false_reject_rate"] is None
    assert report["graduation_met"] is False


def test_shadow_report_mismatch_rate_calculation(tmp_db):
    """AI real 4 件のうち 1 件だけ user が pricing_eligible=1 で採用 →
    不一致率 = 3/4 = 0.75 (逐語: ai_real_user_not_adopted / ai_real_count)."""
    from monitor.database import get_conn, get_shadow_reconciliation_report
    with get_conn() as c:
        for i in range(4):
            comp_id = f"COMP_REAL_{i}"
            c.execute(
                "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
                "pricing_eligible, is_active) VALUES (?,?,?,1)",
                ("OUR1", comp_id, 1 if i == 0 else 0),
            )
            _insert_classification(c, "OUR1", comp_id, "real", 1)

    report = get_shadow_reconciliation_report()
    assert report["ai_real_count"] == 4
    assert report["ai_real_user_adopted"] == 1
    assert report["ai_real_user_not_adopted"] == 3
    assert report["mismatch_rate"] == pytest.approx(0.75)


def test_shadow_report_noise_false_reject_rate(tmp_db):
    """AI noise 2 件のうち 1 件を user が pricing_eligible=1 のまま維持 →
    noise 誤棄却率 = 1/2 = 0.5."""
    from monitor.database import get_conn, get_shadow_reconciliation_report
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible, is_active) VALUES ('OUR1','COMP_NOISE_0',1,1)"
        )
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible, is_active) VALUES ('OUR1','COMP_NOISE_1',0,1)"
        )
        _insert_classification(c, "OUR1", "COMP_NOISE_0", "noise", 0)
        _insert_classification(c, "OUR1", "COMP_NOISE_1", "noise", 0)

    report = get_shadow_reconciliation_report()
    assert report["ai_noise_count"] == 2
    assert report["ai_noise_user_kept_eligible"] == 1
    assert report["noise_false_reject_rate"] == pytest.approx(0.5)


def test_shadow_report_uses_latest_classification_per_competitor(tmp_db):
    """同一 competitor_item_id に複数回判定がある場合、最新 (MAX(id)) のみ集計対象."""
    from monitor.database import get_conn, get_shadow_reconciliation_report
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible, is_active) VALUES ('OUR1','COMP_X',1,1)"
        )
        # 1 回目: noise → 2 回目 (最新): real
        _insert_classification(c, "OUR1", "COMP_X", "noise", 0)
        _insert_classification(c, "OUR1", "COMP_X", "real", 1)

    report = get_shadow_reconciliation_report()
    # 最新判定 (real) のみ数える = noise 側には出てこない
    assert report["ai_noise_count"] == 0
    assert report["ai_real_count"] == 1
    assert report["ai_real_user_adopted"] == 1


def test_shadow_report_graduation_requires_both_conditions(tmp_db):
    """不一致率 5% 以下でも、Shadow 開始から 2 週間未満なら卒業基準未達."""
    from monitor.database import get_conn, get_shadow_reconciliation_report
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible, is_active) VALUES ('OUR1','COMP_OK',1,1)"
        )
        # created_at は CURRENT_TIMESTAMP default = "たった今" (2 週間未経過)
        _insert_classification(c, "OUR1", "COMP_OK", "real", 1)

    report = get_shadow_reconciliation_report()
    assert report["mismatch_rate"] == 0.0
    assert report["days_since_shadow_start"] is not None
    assert report["days_since_shadow_start"] < 14
    assert report["graduation_met"] is False


def test_shadow_report_graduation_met_when_both_satisfied(tmp_db):
    """2 週間経過 + 不一致率 5% 以下 → graduation_met=True."""
    from monitor.database import get_conn, get_shadow_reconciliation_report
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible, is_active) VALUES ('OUR1','COMP_OLD',1,1)"
        )
        c.execute(
            """INSERT INTO rival_classifications
               (ebay_item_id, competitor_item_id, classification, would_be_eligible,
                shadow_mode, created_at)
               VALUES ('OUR1','COMP_OLD','real',1,1,
                       datetime('now','-20 days'))"""
        )

    report = get_shadow_reconciliation_report()
    assert report["mismatch_rate"] == 0.0
    assert report["days_since_shadow_start"] >= 14
    assert report["graduation_met"] is True


# ────────────────────────────────────────────────────────────────
# get_competitors_with_pricing (monitor/lowest_price.py): AI 判定 LEFT JOIN
# ────────────────────────────────────────────────────────────────

def test_get_competitors_with_pricing_includes_ai_and_eligible_fields(tmp_db):
    from monitor.database import get_conn
    from monitor.lowest_price import get_competitors_with_pricing
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id, "
            "pricing_eligible, is_active, competitor_price_usd, "
            "competitor_shipping_usd) VALUES ('OUR1','COMP1',1,1,10.0,5.0)"
        )
        _insert_classification(c, "OUR1", "COMP1", "real", 1)
        c.execute(
            "UPDATE rival_classifications SET confidence=0.91, "
            "reason='型番一致 [warning_brand:Holbein]' WHERE competitor_item_id='COMP1'"
        )

    rows = get_competitors_with_pricing("OUR1")
    assert len(rows) == 1
    r = rows[0]
    assert r["pricing_eligible"] is True
    assert r["ai_classification"] == "real"
    assert r["ai_confidence"] == pytest.approx(0.91)
    assert "warning_brand:Holbein" in r["ai_reason"]
    assert r["ai_would_be_eligible"] is True
    assert r["total_usd"] == pytest.approx(15.0)


def test_get_competitors_with_pricing_no_classification_is_none(tmp_db):
    """rival_classifications に判定が無い競合は AI 判定列が None (未判定)."""
    from monitor.database import get_conn
    from monitor.lowest_price import get_competitors_with_pricing
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products (our_item_id, competitor_item_id) "
            "VALUES ('OUR2','COMP_UNJUDGED')"
        )
    rows = get_competitors_with_pricing("OUR2")
    assert len(rows) == 1
    assert rows[0]["ai_classification"] is None
    assert rows[0]["pricing_eligible"] is False


# ────────────────────────────────────────────────────────────────
# W301 HIGH-1 (2026-07-02): add_or_reactivate_competitor が reactivate 時に
# pricing_eligible=0 で必ずリセットする (Shadow 不変条件保護 / money-direct).
#
# シナリオ:
#   1. backfill 済み (active=1, eligible=1) の competitor がある
#   2. user が UI で「値下げ適格 OFF」→ delete_competitor_product 相当 (is_active=0)
#      だけ実行される (現行 delete は pricing_eligible を触らない = HIGH-1 の原因)
#   3. その後 03:00 rival_classify → task_rival_classify → add_or_reactivate_competitor
#      で real 判定 → is_active=1 に復活
#   4. 修正前: pricing_eligible=1 残留 + is_active=1 で W183 ゲート (is_active=1 AND
#      pricing_eligible=1) が真になり自動値下げ対象に黙って復帰
#   5. 修正後: reactivate で pricing_eligible=0 に強制リセット、W183 ゲート対象外
# ────────────────────────────────────────────────────────────────

def test_reactivate_resets_pricing_eligible_from_1_to_0(tmp_db):
    """HIGH-1 コア: eligible=1 の competitor を is_active=0 → reactivate すると
    is_active=1 かつ pricing_eligible=0 になる."""
    from monitor.database import add_or_reactivate_competitor, get_conn
    add_or_reactivate_competitor(
        our_item_id="our_h1", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_h1",
    )
    with get_conn() as c:
        # backfill 済み active=1, eligible=1 (=HIGH-1 の前提条件) をシミュレート
        c.execute(
            "UPDATE competitor_products SET pricing_eligible=1 "
            "WHERE competitor_item_id='comp_h1'"
        )
        # user 手動 OFF (delete_competitor_product 相当) をシミュレート
        c.execute(
            "UPDATE competitor_products SET is_active=0 "
            "WHERE competitor_item_id='comp_h1'"
        )

    # rival_classify → add_or_reactivate_competitor で real 判定
    rid, action = add_or_reactivate_competitor(
        our_item_id="our_h1", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_h1",
    )
    assert action == 'reactivated'

    with get_conn() as c:
        row = c.execute(
            "SELECT is_active, pricing_eligible FROM competitor_products WHERE id=?",
            (rid,),
        ).fetchone()
    assert row["is_active"] == 1
    assert row["pricing_eligible"] == 0, (
        "HIGH-1: reactivate 時に pricing_eligible がリセットされていない "
        "→ Shadow 不変条件破れ (money-direct)"
    )


def test_reactivate_reset_excluded_from_w183_gate(tmp_db):
    """W183 抽出条件 `is_active=1 AND COALESCE(pricing_eligible,0)=1` で拾わないこと."""
    from monitor.database import add_or_reactivate_competitor, get_conn
    add_or_reactivate_competitor(
        our_item_id="our_h1b", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_h1b",
    )
    with get_conn() as c:
        c.execute(
            "UPDATE competitor_products SET pricing_eligible=1, is_active=0 "
            "WHERE competitor_item_id='comp_h1b'"
        )
    add_or_reactivate_competitor(
        our_item_id="our_h1b", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_h1b",
    )
    with get_conn() as c:
        w183_hit = c.execute(
            "SELECT COUNT(*) FROM competitor_products "
            "WHERE competitor_item_id='comp_h1b' "
            "  AND is_active=1 AND COALESCE(pricing_eligible,0)=1"
        ).fetchone()[0]
    assert w183_hit == 0, "W183 自動値下げ対象に復帰してはならない (HIGH-1)"


def test_reactivate_from_eligible_1_writes_reset_change_log(tmp_db):
    """1→0 の変化があった場合、pricing_eligible_change_log に
    changed_by='reactivate_reset' で 1 行入る (Q0 痕跡)."""
    from monitor.database import add_or_reactivate_competitor, get_conn
    add_or_reactivate_competitor(
        our_item_id="our_h1c", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_h1c",
    )
    with get_conn() as c:
        c.execute(
            "UPDATE competitor_products SET pricing_eligible=1, is_active=0 "
            "WHERE competitor_item_id='comp_h1c'"
        )
        cp_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='comp_h1c'"
        ).fetchone()[0]

    add_or_reactivate_competitor(
        our_item_id="our_h1c", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_h1c",
    )

    with get_conn() as c:
        logs = c.execute(
            "SELECT old_value, new_value, changed_by "
            "FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=? ORDER BY id",
            (cp_id,),
        ).fetchall()
    assert len(logs) == 1
    assert logs[0]["old_value"] == 1
    assert logs[0]["new_value"] == 0
    assert logs[0]["changed_by"] == 'reactivate_reset'


def test_reactivate_from_eligible_0_does_not_write_change_log(tmp_db):
    """0→0 は no-op のためログを増やさない (指示 Q0 「0→0 は記録不要」逐語)."""
    from monitor.database import add_or_reactivate_competitor, get_conn
    add_or_reactivate_competitor(
        our_item_id="our_h1d", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_h1d",
    )
    with get_conn() as c:
        # eligible=0 (default) のまま、is_active のみ 0 に (通常の delete 経路)
        c.execute(
            "UPDATE competitor_products SET is_active=0 "
            "WHERE competitor_item_id='comp_h1d'"
        )
        cp_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='comp_h1d'"
        ).fetchone()[0]

    add_or_reactivate_competitor(
        our_item_id="our_h1d", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_h1d",
    )

    with get_conn() as c:
        logs = c.execute(
            "SELECT COUNT(*) FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=?",
            (cp_id,),
        ).fetchone()[0]
        # ついでに競合の状態も verify (通常経路の後方互換)
        row = c.execute(
            "SELECT is_active, pricing_eligible FROM competitor_products WHERE id=?",
            (cp_id,),
        ).fetchone()
    assert logs == 0, "0→0 でログが増えてはいけない"
    assert row["is_active"] == 1
    assert row["pricing_eligible"] == 0


def test_added_path_still_defaults_to_pricing_eligible_zero(tmp_db):
    """新規 INSERT 分岐は現状のまま (default 0)、変更ログ増えない."""
    from monitor.database import add_or_reactivate_competitor, get_conn
    rid, action = add_or_reactivate_competitor(
        our_item_id="our_new", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_new",
    )
    assert action == 'added'
    with get_conn() as c:
        row = c.execute(
            "SELECT is_active, COALESCE(pricing_eligible,0) AS pe "
            "FROM competitor_products WHERE id=?",
            (rid,),
        ).fetchone()
        logs = c.execute(
            "SELECT COUNT(*) FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=?",
            (rid,),
        ).fetchone()[0]
    assert row["is_active"] == 1
    assert row["pe"] == 0
    assert logs == 0, "新規 INSERT 経路で変更ログを増やしてはいけない"


# ────────────────────────────────────────────────────────────────
# W301 統一方針 MED#2 (2026-07-02): reactivate の過剰リセット防止 (核心回帰).
# 既に is_active=1 (user 採用中、eligible=1) の competitor が再 discovery で
# real 判定されても eligible=1 のまま維持 = W183 追従から黙って脱落しない.
# 「反対側の穴」= 停止経路のクリアと表裏を成すライフサイクル 1 方針の核心.
# ────────────────────────────────────────────────────────────────

def test_reactivate_active_eligible_row_keeps_eligible_1(tmp_db):
    """MED#2 核心: 既に active+eligible=1 の競合 (user 採用中) が再 discovery で
    real 判定されても、eligible=1/is_active=1 は維持されて W183 から脱落しない."""
    from monitor.database import add_or_reactivate_competitor, get_conn
    add_or_reactivate_competitor(
        our_item_id="our_m2", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_m2",
    )
    # user が UI トグルで採用 + 値下げ適格 ON にした状態をシミュレート
    with get_conn() as c:
        c.execute(
            "UPDATE competitor_products SET is_active=1, pricing_eligible=1 "
            "WHERE competitor_item_id='comp_m2'"
        )

    # 再 discovery + real 判定で add_or_reactivate を再呼出
    rid, action = add_or_reactivate_competitor(
        our_item_id="our_m2", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_m2",
    )
    assert action == 'reactivated'

    with get_conn() as c:
        row = c.execute(
            "SELECT is_active, pricing_eligible FROM competitor_products WHERE id=?",
            (rid,),
        ).fetchone()
        logs = c.execute(
            "SELECT COUNT(*) FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=?",
            (rid,),
        ).fetchone()[0]
    assert row["is_active"] == 1
    assert row["pricing_eligible"] == 1, (
        "MED#2 核心回帰: 採用中 (active+eligible=1) 競合が再 discovery で "
        "eligible=0 に落ちて W183 追従から黙って脱落してはいけない"
    )
    assert logs == 0, "採用中維持の再活性化ではログを増やしてはいけない"

    # W183 ゲート `is_active=1 AND pricing_eligible=1` で正しく残ること
    with get_conn() as c:
        w183_hit = c.execute(
            "SELECT COUNT(*) FROM competitor_products "
            "WHERE competitor_item_id='comp_m2' "
            "  AND is_active=1 AND COALESCE(pricing_eligible,0)=1"
        ).fetchone()[0]
    assert w183_hit == 1


# ────────────────────────────────────────────────────────────────
# W301 統一方針 HIGH#1 (2026-07-02): 停止経路のクリア.
# delete_competitor_product / upsert_listing_competitors の削除分岐で
# is_active=1→0 と同時に pricing_eligible=0 にクリア + 変更ログ.
# ────────────────────────────────────────────────────────────────

def test_delete_competitor_product_clears_pricing_eligible_and_logs(tmp_db):
    """delete_competitor_product: 停止時に eligible=1→0 に必ずクリア + ログ."""
    from monitor.database import (
        add_or_reactivate_competitor,
        delete_competitor_product,
        get_conn,
    )
    add_or_reactivate_competitor(
        our_item_id="our_del", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_del",
    )
    with get_conn() as c:
        c.execute(
            "UPDATE competitor_products SET pricing_eligible=1 "
            "WHERE competitor_item_id='comp_del'"
        )
        cp_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='comp_del'"
        ).fetchone()[0]

    delete_competitor_product("comp_del")

    with get_conn() as c:
        row = c.execute(
            "SELECT is_active, pricing_eligible FROM competitor_products WHERE id=?",
            (cp_id,),
        ).fetchone()
        logs = c.execute(
            "SELECT old_value, new_value, changed_by FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=? ORDER BY id",
            (cp_id,),
        ).fetchall()
    assert row["is_active"] == 0
    assert row["pricing_eligible"] == 0
    assert len(logs) == 1
    assert logs[0]["old_value"] == 1
    assert logs[0]["new_value"] == 0
    assert logs[0]["changed_by"] == 'deactivate_clear'


def test_delete_competitor_product_from_eligible_0_no_log(tmp_db):
    """0 → 0 は no-op のためログを増やさない (Q0: 0→0 記録不要)."""
    from monitor.database import (
        add_or_reactivate_competitor,
        delete_competitor_product,
        get_conn,
    )
    add_or_reactivate_competitor(
        our_item_id="our_del2", our_sku="stock:01",
        competitor_seller="s", competitor_item_id="comp_del2",
    )
    with get_conn() as c:
        cp_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='comp_del2'"
        ).fetchone()[0]

    delete_competitor_product("comp_del2")

    with get_conn() as c:
        logs = c.execute(
            "SELECT COUNT(*) FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=?",
            (cp_id,),
        ).fetchone()[0]
    assert logs == 0


def test_ui_delete_then_readd_starts_from_eligible_zero(tmp_db):
    """HIGH#1 再現シナリオ: UI 削除 → 再追加で eligible=0 起点になる
    (upsert_listing_competitors 経由)."""
    from monitor.database import get_conn
    from monitor.lowest_price import upsert_listing_competitors

    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
            "ebay_condition_id) VALUES ('OUR_UI','stock:01','t',100.0,'3000')"
        )

    # 登録 → user が UI でトグル ON → eligible=1 状態
    upsert_listing_competitors("OUR_UI", ["285999888001"])
    with get_conn() as c:
        c.execute(
            "UPDATE competitor_products SET pricing_eligible=1 "
            "WHERE competitor_item_id='285999888001'"
        )
        cp_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='285999888001'"
        ).fetchone()[0]

    # UI 削除 (空リストで置換)
    upsert_listing_competitors("OUR_UI", [])
    with get_conn() as c:
        row_after_delete = c.execute(
            "SELECT is_active, pricing_eligible FROM competitor_products WHERE id=?",
            (cp_id,),
        ).fetchone()
    assert row_after_delete["is_active"] == 0
    assert row_after_delete["pricing_eligible"] == 0, (
        "HIGH#1: 停止時に eligible がクリアされていない → W183 復帰穴"
    )

    # UI 再追加 (第 2 復活経路 = upsert_listing_competitors の再 active 化 UPDATE)
    upsert_listing_competitors("OUR_UI", ["285999888001"])
    with get_conn() as c:
        row_after_readd = c.execute(
            "SELECT is_active, pricing_eligible FROM competitor_products WHERE id=?",
            (cp_id,),
        ).fetchone()
    assert row_after_readd["is_active"] == 1
    assert row_after_readd["pricing_eligible"] == 0, (
        "HIGH#1 第 2 復活経路: UI 再追加でも eligible=0 起点 (Shadow) から始まる"
    )

    # 変更ログは delete 時の 1→0 のみ (再追加は 0→0 で記録なし)
    with get_conn() as c:
        logs = c.execute(
            "SELECT COUNT(*) FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=?",
            (cp_id,),
        ).fetchone()[0]
    assert logs == 1


def test_upsert_second_reactivate_path_from_legacy_eligible_1_writes_log(tmp_db):
    """W301 MEDIUM: upsert_listing_competitors 第 2 復活経路 (past 行再 active 化)
    でも、prev eligible=1 → 0 の遷移を必ず change_log に 'reactivate_reset' で記録
    (add_or_reactivate の reactivate_reset ログと対称、Q0 監査痕跡欠落解消)."""
    from monitor.database import get_conn
    from monitor.lowest_price import upsert_listing_competitors

    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
            "ebay_condition_id) VALUES ('OUR_LEGACY_A','stock:01','ta',100.0,'3000')"
        )
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
            "ebay_condition_id) VALUES ('OUR_NEW_B','stock:02','tb',100.0,'3000')"
        )
        # legacy row を直接 seed: 別 our_item_id で is_active=0 だが eligible=1 残留
        # (= HIGH-1 修正前の穴を通ってきた backfill 状態、または過去仕様の残骸)
        c.execute(
            """INSERT INTO competitor_products
               (our_item_id, competitor_item_id, is_active, pricing_eligible,
                price_rule, min_price, max_discount)
               VALUES ('OUR_LEGACY_A','285999666001',0,1,
                       'competitor - 0.99', 99.0, 99.0)"""
        )
        cp_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='285999666001'"
        ).fetchone()[0]

    # 別 owner (OUR_NEW_B) で UI 登録 → past 行を再 active 化する第 2 復活経路
    upsert_listing_competitors("OUR_NEW_B", ["285999666001"])

    with get_conn() as c:
        row = c.execute(
            "SELECT our_item_id, is_active, pricing_eligible, price_rule "
            "FROM competitor_products WHERE id=?",
            (cp_id,),
        ).fetchone()
        logs = c.execute(
            "SELECT old_value, new_value, changed_by, our_item_id "
            "FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=? ORDER BY id",
            (cp_id,),
        ).fetchall()

    assert row["is_active"] == 1
    assert row["pricing_eligible"] == 0, (
        "第 2 復活経路: eligible=0 起点 (Shadow) に戻る"
    )
    assert row["our_item_id"] == "OUR_NEW_B"
    assert row["price_rule"] == "competitor - 0.01", "旧 owner rule はデフォルト化"
    assert len(logs) == 1, "MEDIUM: prev eligible=1→0 のログが必ず 1 件記録される"
    assert logs[0]["old_value"] == 1
    assert logs[0]["new_value"] == 0
    assert logs[0]["changed_by"] == 'reactivate_reset'
    assert logs[0]["our_item_id"] == "OUR_LEGACY_A", (
        "監査痕跡: 遷移時点の旧 owner を記録"
    )


def test_upsert_second_reactivate_path_from_eligible_0_no_log(tmp_db):
    """第 2 復活経路で prev eligible=0 の場合はログを増やさない (0→0 は no-op)."""
    from monitor.database import get_conn
    from monitor.lowest_price import upsert_listing_competitors

    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
            "ebay_condition_id) VALUES ('OUR_C','stock:01','tc',100.0,'3000')"
        )
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
            "ebay_condition_id) VALUES ('OUR_D','stock:02','td',100.0,'3000')"
        )
        # legacy row: is_active=0, eligible=0 (通常の削除経路を通ってきた状態)
        c.execute(
            """INSERT INTO competitor_products
               (our_item_id, competitor_item_id, is_active, pricing_eligible)
               VALUES ('OUR_C','285999555001',0,0)"""
        )
        cp_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='285999555001'"
        ).fetchone()[0]

    upsert_listing_competitors("OUR_D", ["285999555001"])

    with get_conn() as c:
        row = c.execute(
            "SELECT is_active, pricing_eligible FROM competitor_products WHERE id=?",
            (cp_id,),
        ).fetchone()
        logs = c.execute(
            "SELECT COUNT(*) FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=?",
            (cp_id,),
        ).fetchone()[0]
    assert row["is_active"] == 1
    assert row["pricing_eligible"] == 0
    assert logs == 0, "0→0 では第 2 復活経路もログを増やしてはいけない"


def test_upsert_delete_from_eligible_1_writes_deactivate_clear_log(tmp_db):
    """upsert_listing_competitors の削除分岐: eligible=1→0 のログが記録される."""
    from monitor.database import get_conn
    from monitor.lowest_price import upsert_listing_competitors

    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
            "ebay_condition_id) VALUES ('OUR_LOG','stock:01','t',100.0,'3000')"
        )
    upsert_listing_competitors("OUR_LOG", ["285999777001"])
    with get_conn() as c:
        c.execute(
            "UPDATE competitor_products SET pricing_eligible=1 "
            "WHERE competitor_item_id='285999777001'"
        )
        cp_id = c.execute(
            "SELECT id FROM competitor_products WHERE competitor_item_id='285999777001'"
        ).fetchone()[0]

    upsert_listing_competitors("OUR_LOG", [])

    with get_conn() as c:
        logs = c.execute(
            "SELECT old_value, new_value, changed_by FROM pricing_eligible_change_log "
            "WHERE competitor_product_id=? ORDER BY id",
            (cp_id,),
        ).fetchall()
    assert len(logs) == 1
    assert logs[0]["old_value"] == 1
    assert logs[0]["new_value"] == 0
    assert logs[0]["changed_by"] == 'deactivate_clear'


# ────────────────────────────────────────────────────────────────
# W301 MED#3 (2026-07-02): task_rival_classify で add_or_reactivate 失敗時に
# discovery.status='new' が維持され翌 run で再試行可能であること.
# ────────────────────────────────────────────────────────────────

def test_classify_real_add_failure_keeps_status_new_for_retry(tmp_db, monkeypatch):
    """add_or_reactivate_competitor が例外を投げた場合、status='new' 維持 =
    翌 run の rival_classify で再試行できる (Q0 silent gap 防止)."""
    from monitor.database import get_conn
    from monitor.rival_classifier import AIJudgeResult

    with get_conn() as c:
        c.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, current_price, ebay_condition_id, condition_rank)
               VALUES ('OUR_M3','stock01','Sony WH-1000XM5 Wireless Headphones',
                       200.0, '3000', 'B')"""
        )
        c.execute(
            """INSERT INTO listing_rival_discoveries
               (ebay_item_id, competitor_seller, competitor_item_id,
                competitor_title, competitor_price_usd, search_keyword, status)
               VALUES ('OUR_M3', 's', 'COMP_M3',
                       'ソニー WH-1000XM5 ワイヤレスヘッドホン', 190.0,
                       'sony headphones', 'new')"""
        )
        discovery_id = c.execute(
            "SELECT id FROM listing_rival_discoveries WHERE competitor_item_id='COMP_M3'"
        ).fetchone()[0]

    # judge_rival を real 確信度 0.95 で固定
    def _fake_judge(signals, model="unused"):
        return AIJudgeResult(
            same_product=True, variant_risk="none", condition="USED",
            confidence=0.95, reason="ok", ai_model="claude-haiku-4-5-20251001",
            route="ai",
        )

    import monitor.rival_classifier as rc
    monkeypatch.setattr(rc, "judge_rival", _fake_judge)

    # add_or_reactivate_competitor を例外に差し替え (task_rival_classify モジュール
    # の import 済参照を直接差し替える)。
    import tasks.task_rival_classify as trc
    monkeypatch.setattr(
        trc, "add_or_reactivate_competitor",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("simulated DB failure")),
    )

    result = trc.run_rival_classify({})

    assert result["success"] is True  # run 全体は継続 (Q0: 個別失敗で止めない)
    assert result["real"] == 1  # 分類自体は real 判定
    with get_conn() as c:
        status = c.execute(
            "SELECT status FROM listing_rival_discoveries WHERE id=?",
            (discovery_id,),
        ).fetchone()[0]
    assert status == 'new', (
        "MED#3: add 失敗時は status='new' 維持で翌 run 再試行 (silent gap 防止)"
    )


# ────────────────────────────────────────────────────────────────
# W301 MED#4 (2026-07-02): competitor_snapshot 全滅時は success=False.
# ────────────────────────────────────────────────────────────────

def test_snapshot_all_failed_returns_success_false(tmp_db, monkeypatch):
    """batch>0 かつ captured==0 (全滅) → success=False (W245 パターン整合)."""
    from monitor.database import get_conn

    # pricing_eligible=1 の active 競合を 2 件 seed → get_snapshot_targets で拾う
    with get_conn() as c:
        c.execute(
            """INSERT INTO competitor_products
               (our_item_id, competitor_item_id, is_active, pricing_eligible)
               VALUES ('OUR_S4','COMP_S4_1',1,1)"""
        )
        c.execute(
            """INSERT INTO competitor_products
               (our_item_id, competitor_item_id, is_active, pricing_eligible)
               VALUES ('OUR_S4','COMP_S4_2',1,1)"""
        )

    _fake_creds = {"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}
    monkeypatch.setattr(
        "tasks.task_competitor_snapshot.get_ebay_credentials",
        lambda config=None: dict(_fake_creds),
    )
    # 全滅: snapshots dict に何も入らない
    import monitor.ebay_client as ebay_client_mod
    monkeypatch.setattr(
        ebay_client_mod, "get_competitor_snapshot_batch",
        lambda item_ids, *a, **kw: ({}, len(item_ids)),
    )

    from tasks.task_competitor_snapshot import run_competitor_snapshot
    result = run_competitor_snapshot({})

    assert result["captured"] == 0
    assert result["failed"] == 2
    assert result["success"] is False, (
        "MED#4: 全滅 (captured=0) を success=True で偽装成功にしてはいけない"
    )


def test_snapshot_partial_success_still_true(tmp_db, monkeypatch):
    """部分成功 (captured>0) は従来どおり success=True."""
    from monitor.database import get_conn

    with get_conn() as c:
        c.execute(
            """INSERT INTO competitor_products
               (our_item_id, competitor_item_id, is_active, pricing_eligible)
               VALUES ('OUR_S4b','COMP_OK',1,1)"""
        )
        c.execute(
            """INSERT INTO competitor_products
               (our_item_id, competitor_item_id, is_active, pricing_eligible)
               VALUES ('OUR_S4b','COMP_BAD',1,1)"""
        )

    _fake_creds = {"app_id": "a", "dev_id": "d", "cert_id": "c", "user_token": "t"}
    monkeypatch.setattr(
        "tasks.task_competitor_snapshot.get_ebay_credentials",
        lambda config=None: dict(_fake_creds),
    )

    def _fake_batch(item_ids, *a, **kw):
        # COMP_OK のみ成功
        return ({"COMP_OK": {
            "quantity_sold": 1, "quantity_total": 2, "quantity_available": 1,
            "seller_feedback_score": 10, "seller_positive_pct": 90.0,
            "seller_country": "JP", "price_usd": 50.0, "shipping_usd": 5.0,
        }}, len(item_ids))

    import monitor.ebay_client as ebay_client_mod
    monkeypatch.setattr(ebay_client_mod, "get_competitor_snapshot_batch", _fake_batch)

    from tasks.task_competitor_snapshot import run_competitor_snapshot
    result = run_competitor_snapshot({})

    assert result["captured"] == 1
    assert result["failed"] == 1
    assert result["success"] is True, (
        "MED#4: 部分成功 (captured>0) は success=True を維持"
    )
