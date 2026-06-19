"""回帰テスト (2026-06-18): cleanup_stale_supplier_candidates の SKU キー違反 fix.

背景: 旧実装は `sku IN (SELECT sku FROM ebay_listings WHERE ...)` で親 listing を照合して
いた。`stock**` SKU は多数 listing が共有し、無在庫 `ebay**` SKU も再出品/重複で複数行に
存在し得る。同一 SKU の別 listing が在庫有/ended になると、本来は無在庫 listing に紐づく
正当な pending 候補まで巻き添えで auto_rejected され、review キューが枯渇した (誤却下 148 件)。
fix: 親 listing 照合を ebay_item_id 主導へ是正 (sku-rules.md / W139 / W185 と同系統)。

不変条件:
1. 同一 SKU の別 listing が在庫有でも、自分の親 (ebay_item_id) が無在庫なら候補は pending 維持。
2. 同一 SKU の別 listing が ended でも、自分の親が未終了なら候補は pending 維持。
3. 自分の親 (ebay_item_id) が在庫有 → 正しく auto_rejected。
4. 自分の親 (ebay_item_id) が ended → 正しく auto_rejected。
"""
from __future__ import annotations


def _add_listing(db, eid: str, sku: str, source_status: str = "在庫無", is_ended: int = 0):
    with db.get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO ebay_listings (ebay_item_id, sku, source_status, is_ended) "
            "VALUES (?,?,?,?)",
            (eid, sku, source_status, is_ended),
        )


def _status(db, eid: str):
    with db.get_conn() as c:
        row = c.execute(
            "SELECT status, auto_rejected FROM supplier_candidates WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _setup(db, tmp_path, monkeypatch, name):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / name)
    db.init_db()


def test_shared_sku_in_stock_sibling_does_not_reject_valid_candidate(tmp_path, monkeypatch):
    """核心: 同一 SKU の別 listing が在庫有でも、無在庫の親に紐づく候補は pending 維持."""
    import monitor.database as db
    _setup(db, tmp_path, monkeypatch, "shared_instock.db")
    # 親 A = 無在庫 / 兄弟 B = 同一 SKU だが在庫有
    _add_listing(db, "EID_A", "ebayyh_shared", source_status="在庫無", is_ended=0)
    _add_listing(db, "EID_B", "ebayyh_shared", source_status="在庫有", is_ended=0)
    db.add_supplier_candidate(
        sku="ebayyh_shared", candidate_url="https://supplier/a",
        source_platform="yahoo", ebay_item_id="EID_A",
    )
    db.cleanup_stale_supplier_candidates()
    assert _status(db, "EID_A") == ("pending", 0), "在庫有の同SKU兄弟で巻き添え却下された (SKUキー違反再発)"


def test_shared_sku_ended_sibling_does_not_reject_valid_candidate(tmp_path, monkeypatch):
    """同一 SKU の別 listing が ended でも、未終了の親に紐づく候補は pending 維持."""
    import monitor.database as db
    _setup(db, tmp_path, monkeypatch, "shared_ended.db")
    _add_listing(db, "EID_E", "ebayyh_e_shared", source_status="在庫無", is_ended=0)
    _add_listing(db, "EID_F", "ebayyh_e_shared", source_status="在庫無", is_ended=1)
    db.add_supplier_candidate(
        sku="ebayyh_e_shared", candidate_url="https://supplier/e",
        source_platform="yahoo", ebay_item_id="EID_E",
    )
    db.cleanup_stale_supplier_candidates()
    assert _status(db, "EID_E") == ("pending", 0), "ended の同SKU兄弟で巻き添え却下された"


def test_own_listing_in_stock_is_rejected(tmp_path, monkeypatch):
    """正の確認: 自分の親 (ebay_item_id) が在庫有なら正しく auto_reject."""
    import monitor.database as db
    _setup(db, tmp_path, monkeypatch, "own_instock.db")
    _add_listing(db, "EID_C", "ebayyh_c", source_status="在庫有", is_ended=0)
    db.add_supplier_candidate(
        sku="ebayyh_c", candidate_url="https://supplier/c",
        source_platform="yahoo", ebay_item_id="EID_C",
    )
    db.cleanup_stale_supplier_candidates()
    assert _status(db, "EID_C") == ("rejected", 1), "在庫有の自親で auto_reject されていない"


def test_own_listing_ended_is_rejected(tmp_path, monkeypatch):
    """正の確認: 自分の親 (ebay_item_id) が ended なら正しく auto_reject."""
    import monitor.database as db
    _setup(db, tmp_path, monkeypatch, "own_ended.db")
    _add_listing(db, "EID_D", "ebayyh_d", source_status="在庫無", is_ended=1)
    db.add_supplier_candidate(
        sku="ebayyh_d", candidate_url="https://supplier/d",
        source_platform="yahoo", ebay_item_id="EID_D",
    )
    db.cleanup_stale_supplier_candidates()
    assert _status(db, "EID_D") == ("rejected", 1), "ended の自親で auto_reject されていない"
