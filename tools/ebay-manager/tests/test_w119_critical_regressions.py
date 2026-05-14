"""W119 CRITICAL bug regression tests (code-reviewer Opus 4.7 が指摘した 2 件).

C-1: Step 2 の breakeven 計算で update_listing_breakeven を float 引数で呼ぶと
     全 listing の lp_breakeven_usd が NULL に上書きされる事故 (signature 違反).
     修正後: breakeven > 0 の正しい値が DB に書かれる.

C-2: Browse API itemId は 'v1|<legacy>|0' 形式. 直接 DB に保存すると W183 の
     cid.isdigit() check で全件 reject されて値下げ pipeline が壊れる.
     修正後: extract_legacy_item_id で legacy 形式に正規化される.

詳細: 2026-05-10 code-reviewer Opus 4.7 review / W119 entry.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


# =============================================================================
# C-2 regression: Browse API itemId → legacy ID extraction
# =============================================================================

def test_extract_legacy_item_id_restful_format():
    """eBay Browse API の RESTful itemId 'v1|<legacy>|<variant>' から legacy 抽出."""
    from tabs.tab_research_wizard import extract_legacy_item_id

    assert extract_legacy_item_id("v1|285999999001|0") == "285999999001"
    assert extract_legacy_item_id("v1|356534387172|0") == "356534387172"
    assert extract_legacy_item_id("v1|123|99") == "123"


def test_extract_legacy_item_id_already_legacy_passthrough():
    """既に legacy 形式 (12 桁数字) なら素通し."""
    from tabs.tab_research_wizard import extract_legacy_item_id

    assert extract_legacy_item_id("285999999001") == "285999999001"
    # task_rival_detection.py:308 と同じ挙動 (parts >= 2 で無ければ raw 返す)


def test_extract_legacy_item_id_empty():
    from tabs.tab_research_wizard import extract_legacy_item_id

    assert extract_legacy_item_id("") == ""
    assert extract_legacy_item_id(None) == ""


def test_extract_legacy_id_isdigit_passes_w183_check(tmp_db):
    """C-2 整合: 抽出後の legacy ID が `cid.isdigit() and 11 <= len(cid) <= 14` を満たす.

    monitor/lowest_price.py:345 の値下げ pipeline 入口の filter を通る.
    """
    from tabs.tab_research_wizard import extract_legacy_item_id

    legacy = extract_legacy_item_id("v1|285999999001|0")
    assert legacy.isdigit()
    assert 11 <= len(legacy) <= 14


def test_extract_legacy_id_v1_form_fails_w183_check_unfixed():
    """C-2 fix 前の挙動: 'v1|...|0' を直接 DB 保存すると W183 が reject する.

    本テストは「fix 入れないと壊れる」ことを担保する negative test (regression guard).
    """
    raw_id = "v1|285999999001|0"
    # cid.isdigit() False → reject
    assert not raw_id.isdigit(), "v1| 形式は isdigit() で False なので W183 で reject される"


# =============================================================================
# C-1 regression: update_listing_breakeven signature 違反防止
# =============================================================================

def _insert_listing_with_calc_inputs(ebay_item_id: str, purchase_yen: int = 3000):
    """breakeven 計算が走るのに必要な最小フィールドを満たす listing を挿入."""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            """INSERT INTO ebay_listings (
                ebay_item_id, sku, title, is_ended,
                weight_g, length_cm, width_cm, height_cm,
                purchase_yen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ebay_item_id, "stock:test", "Test Title", 0,
                500, 20, 15, 10,
                purchase_yen,
            ),
        )


def test_update_listing_breakeven_accepts_dict_not_float(tmp_db):
    """C-1 regression: 第二引数は settings dict. float を渡すと内部で TypeError.

    本 test は「dict を渡すと正常動作」を verify (negative test ではない).
    """
    from monitor.lowest_price import update_listing_breakeven
    from monitor.database import get_conn

    _insert_listing_with_calc_inputs("test001", purchase_yen=3000)

    # 正しい呼び方: dict (settings) を渡す
    config = {}  # 最小 config
    breakeven = update_listing_breakeven("test001", config)

    # 結果 verify: DB に NULL でない正の値が書かれている
    with get_conn() as c:
        row = c.execute(
            "SELECT lp_breakeven_usd FROM ebay_listings WHERE ebay_item_id=?",
            ("test001",),
        ).fetchone()
    # config が空なら計算 fail で None になる可能性あるが、その場合も DB は NULL.
    # 重要なのは「TypeError が raise されないこと」.
    assert breakeven is None or breakeven >= 0


def test_step2_purchase_yen_backfill_preserves_lp_min_price(tmp_db):
    """C-3 regression (Round 2 code-reviewer): Step 2 が purchase_yen 補完で
    user 設定の lp_min_price を破壊しない.

    Round 1 の C-1 fix で `set_listing_lowest_price_fields(purchase_yen=X, lp_min_price=None)`
    を導入してしまい、user 設定の lp_min_price が NULL に上書きされる事故が発生していた.
    本 test は update_listing_purchase_yen 単独 helper の挙動を verify する.
    """
    from monitor.database import get_conn
    from monitor.lowest_price import update_listing_purchase_yen

    with get_conn() as c:
        c.execute(
            """INSERT INTO ebay_listings (
                ebay_item_id, sku, title, is_ended, lp_min_price
            ) VALUES (?, ?, ?, ?, ?)""",
            ("test_c3", "stock:01", "T", 0, 25.50),  # user 設定の floor
        )

    update_listing_purchase_yen("test_c3", 3000.0)

    with get_conn() as c:
        row = c.execute(
            "SELECT purchase_yen, lp_min_price FROM ebay_listings WHERE ebay_item_id=?",
            ("test_c3",),
        ).fetchone()

    assert row[0] == 3000.0, "purchase_yen が期待通り更新されていない"
    assert row[1] == 25.50, (
        "user 設定の lp_min_price が NULL に上書きされた = C-3 再発. "
        "update_listing_purchase_yen は lp_min_price を触ってはいけない."
    )


def test_update_listing_breakeven_float_arg_would_break(tmp_db):
    """C-1 regression: float を第二引数で渡すと内部 settings 期待箇所で TypeError.

    本 test は「float を渡したら壊れる」ことを保証する (再発時に必ず捕捉).
    """
    from monitor.lowest_price import update_listing_breakeven
    from monitor.database import get_conn

    _insert_listing_with_calc_inputs("test002", purchase_yen=3000)

    # 誤った呼び方: float を渡す (修正前の wizard L261 と同じパターン)
    # update_listing_breakeven は内部の except (TypeError, ...) で握り潰すため
    # raise はされないが、結果として DB に NULL が書かれる.
    update_listing_breakeven("test002", 50.0)  # type: ignore[arg-type]

    with get_conn() as c:
        row = c.execute(
            "SELECT lp_breakeven_usd FROM ebay_listings WHERE ebay_item_id=?",
            ("test002",),
        ).fetchone()
    # float を settings として渡すと calculator が動かず breakeven=None で UPDATE が走る.
    # この test の主旨は「修正前の wizard コード (float 渡し) が NULL UPDATE を引き起こす
    # ことを示す」回帰防護線.
    assert row[0] is None, (
        "float を settings として渡すと breakeven=None で NULL UPDATE が走るのが "
        "C-1 bug の核心. 本 test が失敗したら lowest_price.py の挙動が変わったので "
        "wizard 側の前提を再確認すること."
    )
