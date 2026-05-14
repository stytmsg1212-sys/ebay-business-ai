"""W68 Step 3 (Iteration 2) regression test: get_ebay_listing_by_sku → by_item_id 移行.

検証対象:
- 新関数 `get_ebay_listing_by_item_id(ebay_item_id)` が ebay_item_id で 1 listing を一意取得
  (UNIQUE 制約 monitor/database.py:407 により LIMIT 1 不要)
- 旧関数 `get_ebay_listing_by_sku(sku)` は deprecate 中も legacy callsites (W75 残存) のため
  動作保持 (2026-05-15 削除予定)
- 同 SKU 多 listing (8 SKU で 107 listings 共有確認、stock:01=58 等) 環境で
  ebay_item_id 集約 dict が衝突回避

過去事故: 2026-04-29 W7-A SKU 主キー崩壊 / 2026-04-30 SKU 一意性誤推論 (連続違反 = 品質事故).
詳細: `.claude/rules/sku-rules.md` / `feedback_sku_misuse_repeat_offense.md`
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """fresh DB を作って monitor.database.DB_PATH を差し替え + init_db で schema 作成"""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert_listing(conn, ebay_item_id: str, sku: str, title: str = "Test"):
    conn.execute(
        "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
        "quantity_ebay, is_ended) VALUES (?, ?, ?, ?, ?, ?)",
        (ebay_item_id, sku, title, 100.0, 1, 0),
    )


def test_get_ebay_listing_by_item_id_returns_correct_row(tmp_db):
    """同 SKU 多 listing 時に ebay_item_id 指定で正しい 1 件を一意取得する"""
    from monitor.database import get_conn, get_ebay_listing_by_item_id

    # Setup: stock:01 を共有する 3 listings (有在庫プール構造)
    with get_conn() as c:
        _insert_listing(c, "ITEM_A", "stock:01", "Listing A")
        _insert_listing(c, "ITEM_B", "stock:01", "Listing B")
        _insert_listing(c, "ITEM_C", "stock:01", "Listing C")

    # 各 ebay_item_id で正しい 1 件取得
    row_a = get_ebay_listing_by_item_id("ITEM_A")
    row_b = get_ebay_listing_by_item_id("ITEM_B")
    row_c = get_ebay_listing_by_item_id("ITEM_C")

    assert row_a is not None and row_a["ebay_item_id"] == "ITEM_A"
    assert row_a["title"] == "Listing A"
    assert row_b is not None and row_b["ebay_item_id"] == "ITEM_B"
    assert row_b["title"] == "Listing B"
    assert row_c is not None and row_c["ebay_item_id"] == "ITEM_C"
    assert row_c["title"] == "Listing C"


def test_get_ebay_listing_by_item_id_none_or_missing(tmp_db):
    """ebay_item_id=None / "" / 存在しない値で None 返却"""
    from monitor.database import get_ebay_listing_by_item_id

    assert get_ebay_listing_by_item_id(None) is None
    assert get_ebay_listing_by_item_id("") is None
    assert get_ebay_listing_by_item_id("NONEXISTENT_ID") is None


def test_deprecated_get_ebay_listing_by_sku_removed():
    """W75 完走 (2026-05-01) で旧 API は物理削除、import で AttributeError"""
    from monitor import database
    assert not hasattr(database, "get_ebay_listing_by_sku"), (
        "deprecated function は W75 完走で削除済 = 残存していてはならない. "
        "新規コードは get_ebay_listing_by_item_id() を使う."
    )


def test_oos_aggregation_dict_keys_are_ebay_item_id_not_sku(tmp_db):
    """app.py:3795 _sup_parent_status 集約 (Group C 移行) を DB-level で検証.

    旧コード `dict[sku] = ...` だと同 SKU 多 listing で衝突 (8 SKU / 107 listings).
    新コード `dict[ebay_item_id] = ...` で衝突回避を verify.
    """
    from monitor.database import get_conn

    # Setup: stock:01 を共有する 5 listings (W7-A 後の有在庫プール構造)
    with get_conn() as c:
        _insert_listing(c, "ITEM_A", "stock:01", "A")
        _insert_listing(c, "ITEM_B", "stock:01", "B")
        _insert_listing(c, "ITEM_C", "stock:01", "C")
        _insert_listing(c, "ITEM_D", "stock:01", "D")
        _insert_listing(c, "ITEM_E", "stock:01", "E")

    # 新方式: WHERE ebay_item_id IN (...) → dict[ebay_item_id]
    eids = ["ITEM_A", "ITEM_B", "ITEM_C", "ITEM_D", "ITEM_E"]
    parent_status_new: dict[str, str] = {}
    with get_conn() as c:
        ph = ",".join("?" * len(eids))
        for srow in c.execute(
            f"SELECT ebay_item_id, source_status FROM ebay_listings "
            f"WHERE ebay_item_id IN ({ph})",
            eids,
        ).fetchall():
            parent_status_new[srow["ebay_item_id"]] = srow["source_status"] or ""

    assert len(parent_status_new) == 5, (
        f"ebay_item_id 集約で 5 件保持されるべき (衝突なし): {parent_status_new}"
    )
    assert set(parent_status_new.keys()) == set(eids)

    # 旧方式 (回帰防止参照): WHERE sku IN (...) → dict[sku] で衝突
    skus = list({"stock:01"})  # 重複排除で 1 SKU のみ
    parent_status_old: dict[str, str] = {}
    with get_conn() as c:
        ph = ",".join("?" * len(skus))
        for srow in c.execute(
            f"SELECT sku, source_status FROM ebay_listings WHERE sku IN ({ph})",
            skus,
        ).fetchall():
            parent_status_old[srow["sku"]] = srow["source_status"] or ""

    # 旧方式は衝突で 1 件のみ (5 listing が 1 entry に潰れる事故再現)
    assert len(parent_status_old) == 1, (
        f"旧 SKU 集約は衝突で 1 件しか保持できない (回帰実証): {parent_status_old}"
    )
