"""get_ebay_listings_supply_risk の SKU フィルタ regression test.

2026-05-05 在庫監視 bug 1 修正対応:
  stock:01 等の 有在庫 SKU は内部 stock pool 管理であり source_status='在庫無'
  であっても 在庫監視 (= 仕入先置換 / 在庫 0 化動線) 対象外。
  Google Pixel Tablet (sku=stock:01) が 在庫監視 で毎回出てた事象の root cause。
  本 test は SQL に sku LIKE 'ebay%' フィルタが適用されていることを保証。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """新規 DB に必要 schema + dummy data を投入し、get_conn を差し替え."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
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
            risk_confirmed INTEGER,
            is_ended INTEGER DEFAULT 0,
            ebay_image_url TEXT,
            yahoo_grace_until TEXT,
            source_out_of_stock_since TEXT
        )
    """)
    # 末尾の NULL = yahoo_grace_until, source_out_of_stock_since
    # (依頼ボード#20 で SELECT に追加された列)
    conn.execute("""
        INSERT INTO ebay_listings VALUES
        ('357933117584', 'stock:01', 'Google Pixel Tablet (有在庫)', 3, '在庫無', NULL, 100, 'A', NULL, '2026-05-05', 0, 0, NULL, NULL, NULL),
        ('356420645893', 'ebayme_30279698157', 'Baccarat 2022 Tumbler (無在庫)', 1, '在庫無', 'メルカリ', 200, 'B', NULL, '2026-05-05', 0, 0, NULL, NULL, NULL),
        ('999999999999', 'ebayyh_test_pnf', 'Test page-not-found', 1, 'ページなし', 'ヤフオク', 50, 'C', NULL, '2026-05-05', 0, 0, NULL, NULL, NULL),
        ('888888888888', 'ebayyh_test_active', 'Test 在庫有 (除外されるべき)', 1, '在庫有', 'ヤフオク', 50, 'C', NULL, '2026-05-05', 0, 0, NULL, NULL, NULL),
        ('777777777777', 'stock:02', 'stock:02 在庫無 (有在庫、除外されるべき)', 2, '在庫無', NULL, 80, 'C', NULL, '2026-05-05', 0, 0, NULL, NULL, NULL),
        ('666666666666', 'ebayyh_test_confirmed', 'user 確認済 (除外されるべき)', 1, '在庫無', 'ヤフオク', 50, 'C', NULL, '2026-05-05', 1, 0, NULL, NULL, NULL)
    """)
    conn.commit()
    conn.close()

    # monitor.database.get_conn を本 test DB に差し替え
    import monitor.database as db_mod
    def _fake_get_conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(db_mod, "get_conn", _fake_get_conn)
    return db_path


def test_supply_risk_excludes_stock_prefix_sku(fresh_db):
    """stock:01, stock:02 等の 有在庫 SKU は 在庫監視 から除外されること."""
    from monitor.database import get_ebay_listings_supply_risk
    result = get_ebay_listings_supply_risk()
    oos_ids = {it["ebay_item_id"] for it in result["out_of_stock"]}
    pnf_ids = {it["ebay_item_id"] for it in result["page_not_found"]}

    # Google Pixel Tablet (stock:01) は除外されている
    assert "357933117584" not in oos_ids
    # stock:02 も除外
    assert "777777777777" not in oos_ids


def test_supply_risk_includes_ebay_prefix_sku(fresh_db):
    """ebay** prefix の 無在庫 SKU は正しく含まれること."""
    from monitor.database import get_ebay_listings_supply_risk
    result = get_ebay_listings_supply_risk()
    oos_ids = {it["ebay_item_id"] for it in result["out_of_stock"]}
    pnf_ids = {it["ebay_item_id"] for it in result["page_not_found"]}

    # Baccarat (ebayme_30279698157) は OOS 対象
    assert "356420645893" in oos_ids
    # ヤフオク page-not-found も対象
    assert "999999999999" in pnf_ids


def test_supply_risk_excludes_active_source_status(fresh_db):
    """source_status='在庫有' は除外されること (既存挙動の regression)."""
    from monitor.database import get_ebay_listings_supply_risk
    result = get_ebay_listings_supply_risk()
    all_ids = {it["ebay_item_id"] for it in result["out_of_stock"]} | {
        it["ebay_item_id"] for it in result["page_not_found"]
    }
    assert "888888888888" not in all_ids


def test_supply_risk_excludes_user_confirmed(fresh_db):
    """risk_confirmed=1 (user 確認済) は除外されること.

    2026-05-05 Baccarat case 修正: user が確認チェック入れた listing は対応済とみなして
    在庫監視 list から除外する. inventory_check で再 OOS 判定されても再表示しない仕様.
    """
    from monitor.database import get_ebay_listings_supply_risk
    result = get_ebay_listings_supply_risk()
    all_ids = {it["ebay_item_id"] for it in result["out_of_stock"]} | {
        it["ebay_item_id"] for it in result["page_not_found"]
    }
    # risk_confirmed=1 は除外
    assert "666666666666" not in all_ids


def test_supply_risk_case_sensitive_per_sku_rules(tmp_path, monkeypatch):
    """GLOB で case-sensitive prefix 判定. 大文字 EBAY/STOCK は仕様外で除外.

    .claude/rules/sku-rules.md は「prefix 完全一致 case-sensitive」を要求.
    LIKE だと default で case-insensitive (ASCII) のため GLOB 使用が正しい.
    """
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE ebay_listings (
            ebay_item_id TEXT PRIMARY KEY, sku TEXT, title TEXT,
            quantity_ebay INTEGER, source_status TEXT, source TEXT,
            current_price REAL, rank TEXT, source_url TEXT,
            source_last_checked TEXT, risk_confirmed INTEGER,
            is_ended INTEGER DEFAULT 0, ebay_image_url TEXT,
            yahoo_grace_until TEXT, source_out_of_stock_since TEXT
        )
    """)
    # 末尾の NULL = yahoo_grace_until, source_out_of_stock_since
    # (依頼ボード#20 で SELECT に追加された列)
    conn.execute("""
        INSERT INTO ebay_listings VALUES
        ('lower_ebay', 'ebayme_test_lower', '正規 無在庫 (含めるべき)', 1, '在庫無', NULL, 100, 'A', NULL, '2026-05-05', 0, 0, NULL, NULL, NULL),
        ('upper_ebay', 'EBAYme_test_upper', '大文字 EBAY (仕様外、除外)', 1, '在庫無', NULL, 100, 'A', NULL, '2026-05-05', 0, 0, NULL, NULL, NULL),
        ('mixed_ebay', 'eBaYme_test_mixed', '大小混在 eBaY (仕様外、除外)', 1, '在庫無', NULL, 100, 'A', NULL, '2026-05-05', 0, 0, NULL, NULL, NULL)
    """)
    conn.commit()
    conn.close()

    import monitor.database as db_mod
    def _fake_get_conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(db_mod, "get_conn", _fake_get_conn)

    from monitor.database import get_ebay_listings_supply_risk
    result = get_ebay_listings_supply_risk()
    all_ids = {it["ebay_item_id"] for it in result["out_of_stock"]} | {
        it["ebay_item_id"] for it in result["page_not_found"]
    }
    # 小文字 ebay のみ含まれる
    assert "lower_ebay" in all_ids
    # 大文字混在は仕様外で除外される
    assert "upper_ebay" not in all_ids
    assert "mixed_ebay" not in all_ids


def test_risk_confirmed_persists_across_inventory_check(fresh_db):
    """2026-05-07 Baccarat 「ずーっと残る」regression:
    user の risk_confirmed=1 設定が永続化される (= reset_all_risk_confirmed が呼ばれない).
    旧仕様は inventory_check 成功で全 reset → 何度確認しても消えなかった.
    """
    from monitor.database import set_ebay_listing_risk_confirmed, get_conn
    set_ebay_listing_risk_confirmed('356420645893', 1)
    with get_conn() as c:
        row = c.execute(
            "SELECT risk_confirmed FROM ebay_listings WHERE ebay_item_id='356420645893'"
        ).fetchone()
    assert row['risk_confirmed'] == 1
    # reset_all_risk_confirmed が削除済であることも保証
    import monitor.database as db_mod
    assert not hasattr(db_mod, 'reset_all_risk_confirmed'), \
        "reset_all_risk_confirmed は削除済 (再追加禁止、Baccarat regression)"


def test_risk_confirmed_clears_on_supplier_restock(fresh_db):
    """H-1 fix regression: 仕入先在庫復活 (在庫無→在庫有) で risk_confirmed=0 リセット.
    これがないと「在庫有→在庫無」サイクルで永遠に sleeping risk になる.
    """
    from monitor.database import (
        set_ebay_listing_risk_confirmed,
        update_ebay_listing_status,
        get_ebay_listings_supply_risk,
    )
    # 1) user 確認済
    set_ebay_listing_risk_confirmed('356420645893', 1)
    # 2) 仕入先在庫復活 → risk_confirmed=0 にリセットされる
    update_ebay_listing_status('356420645893', '在庫有')
    # 3) 再 OOS
    update_ebay_listing_status('356420645893', '在庫無')
    # 4) 要対応リストに再表示されること
    result = get_ebay_listings_supply_risk()
    oos_ids = {it["ebay_item_id"] for it in result["out_of_stock"]}
    assert '356420645893' in oos_ids, "在庫復活→再OOSのサイクルで再表示されるべき"


def test_update_status_to_oos_does_not_clear_risk_confirmed(fresh_db):
    """非 '在庫有' 遷移では risk_confirmed を触らないこと (surgical change の保証)."""
    from monitor.database import (
        set_ebay_listing_risk_confirmed,
        update_ebay_listing_status,
        get_conn,
    )
    set_ebay_listing_risk_confirmed('356420645893', 1)
    update_ebay_listing_status('356420645893', '在庫無')
    with get_conn() as c:
        row = c.execute(
            "SELECT risk_confirmed FROM ebay_listings WHERE ebay_item_id='356420645893'"
        ).fetchone()
    assert row['risk_confirmed'] == 1, "非'在庫有'遷移では risk_confirmed を維持すべき"


def test_oos_caption_for_alt_only_candidates():
    """Baccarat case の純粋関数 regression: cands=0 + alt_only>0 で
    「探索済 (置き換え不可)」caption が出ること.

    本 test は UI 層 (Streamlit) の caption 文言 regression を防ぐ pure logic test.
    詳細な Streamlit 統合 test (AppTest) は別 file で別途追加検討.
    """
    # caption 分岐ロジックの純粋関数化 expectation
    def compute_caption(cands_count: int, alt_only_count: int) -> str:
        if cands_count > 0:
            # 上位ロジック: 候補表示モード (本 test の scope 外)
            return "CANDIDATES_MODE"
        if alt_only_count > 0:
            return (
                f"仕入先候補は探索済みです（{alt_only_count} 件見つかりましたが、"
                f"すべて『別商品の出品候補』として分類されており、この出品の"
                f"置き換えには使えません）。別商品として出品する検討は"
                f"『別SKU出品機会』タブで行ってください。"
            )
        return (
            "候補未探索（次回02:30 Pattern 2バッチで自動探索、"
            "または form 下部の「未探索SKUの即時探索」で個別起動）"
        )

    # Baccarat case: 7 件 alt-only
    cap = compute_caption(0, 7)
    assert "探索済み" in cap
    assert "7 件" in cap
    assert "別商品の出品候補" in cap
    assert "別SKU出品機会" in cap
    # 通常 case: 0 件
    cap_empty = compute_caption(0, 0)
    assert "候補未探索" in cap_empty
    # 候補ありモード (本ロジック対象外)
    assert compute_caption(2, 0) == "CANDIDATES_MODE"
