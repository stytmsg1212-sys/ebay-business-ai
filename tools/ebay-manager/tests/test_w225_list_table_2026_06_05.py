#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W225 (2026-06-05): 商品管理タブを eBay連携と同じ st.dataframe 表形式化した際の
回帰テスト。

カバー範囲:
- _build_list_dataframe: 列構成 / 粗利符号付き整形 / 在庫 emoji / 競合最安 (純関数)
- 行選択 → 編集ゾーンが「Item ID 列値」で解決されること (HIGH-1 ミティゲーション)
- フィルタ変更時に残留選択が破棄され、別 listing を誤って開かないこと (HIGH-2)

HIGH-1 (組込みカラムソートと selection.rows の index ずれ / streamlit#11345) は
st.dataframe のヘッダソートを AppTest で再現できず、Streamlit 側で組込みソートを
無効化する API も無いため、本テストでは「解決が Item ID 列値ベースであること」と
「編集中バナーで listing を明示すること」を保証する (実機 Q1 で視認確認)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

import tabs.tab_product_management as pm  # noqa: E402


# ---------------------------------------------------------------------------
# 純関数: _build_list_dataframe
# ---------------------------------------------------------------------------

def _sample_products():
    return [
        {
            "ebay_item_id": "358343669478", "sku": "ebayyh_p1",
            "title": "X" * 70, "current_price": 145.0, "shipping_cost": 12.0,
            "source_status": "out_of_stock", "has_note": 1,
            "primary_market": "US_only", "rank": "A",
            "total_sold_count": 3, "watch_count": 5,
            "competitor_min_price": 150.0, "lp_breakeven_usd": 130.0,
        },
        {
            "ebay_item_id": "357000000001", "sku": "stock1",
            "title": "Short", "current_price": 50.0, "shipping_cost": 0.0,
            "source_status": "in_stock", "has_note": 0,
            "primary_market": "mixed_global", "rank": "B",
            "total_sold_count": 0, "watch_count": 0,
            "competitor_min_price": None, "lp_breakeven_usd": None,
        },
    ]


def test_build_list_dataframe_columns_and_ids():
    df = pm._build_list_dataframe(_sample_products())
    assert list(df.columns) == [
        "在庫", "📎", "Title", "Item ID", "SKU", "区分", "カテゴリ", "Rank",
        "価格", "送料", "総額", "粗利", "競合最安", "sold", "watch",
    ]
    # 行順 = 入力順 / Item ID 列に ebay_item_id (sku-rules: ebay_item_id 識別)
    assert list(df["Item ID"]) == ["358343669478", "357000000001"]


def test_build_list_dataframe_formatting():
    df = pm._build_list_dataframe(_sample_products())
    r0 = df.iloc[0]
    assert r0["在庫"] == "🔴"          # out_of_stock
    assert r0["📎"] == "📎"            # has_note
    assert r0["総額"] == "$157.00"      # 145 + 12
    assert r0["粗利"] == "+$15"         # 145 - 130 (符号 + が先頭)
    assert r0["競合最安"] == "$150.00"
    assert r0["Title"].endswith("…")    # 70 字 -> 切詰
    r1 = df.iloc[1]
    assert r1["粗利"] == "—"            # breakeven 無し -> 未入力
    assert r1["競合最安"] == "—"        # 競合最安 無し


def test_build_list_dataframe_negative_profit_sign():
    products = [{
        "ebay_item_id": "1", "sku": "stock", "title": "T",
        "current_price": 100.0, "shipping_cost": 0.0,
        "source_status": "in_stock", "lp_breakeven_usd": 130.0,
    }]
    df = pm._build_list_dataframe(products)
    assert df.iloc[0]["粗利"] == "-$30"   # 100 - 130 = -30 (負符号)


def test_build_list_dataframe_category_column():
    """W222: カテゴリ列に category_id を表示 (利益計算 FVF の根拠を可視化)。"""
    products = [
        {"ebay_item_id": "1", "sku": "stock", "title": "A",
         "current_price": 10.0, "shipping_cost": 0.0,
         "source_status": "in_stock", "category_id": 181708},
        {"ebay_item_id": "2", "sku": "stock", "title": "B",
         "current_price": 10.0, "shipping_cost": 0.0,
         "source_status": "in_stock", "category_id": None},
    ]
    df = pm._build_list_dataframe(products)
    assert df.iloc[0]["カテゴリ"] == "181708"
    assert df.iloc[1]["カテゴリ"] == "-"   # 未設定は "-"


def test_fetch_all_products_includes_category_id():
    """W222 根本修正: _fetch_all_products が category_id を返す (従来 SELECT 漏れで
    利益計算が全件 58248 fallback していた)。"""
    from monitor.database import init_db, get_conn
    init_db()
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
            "shipping_cost, category_id, is_ended) VALUES (?,?,?,?,?,?,0)",
            ("910000000001", "stock1", "W222 Cat Test", 100.0, 10.0, 181708),
        )
    prods = pm._fetch_all_products()
    target = next((p for p in prods if p["ebay_item_id"] == "910000000001"), None)
    assert target is not None, "seed listing が取得できない"
    assert "category_id" in target, "SELECT に category_id が無い (修正前の回帰)"
    assert target["category_id"] == 181708


def test_cd_profit_breakdown_varies_by_category():
    """W222: 利益計算が category_id で変わる (FVF はカテゴリ依存)。
    cat 261186 (FVF 15.3%) vs 58248 (12.7%) で利益が異なるはず。
    本テストが「等しい」になったら FVF がカテゴリ非連動に退行している。"""
    # _cd_profit_breakdown(price, pyen, weight_g, l, w, h, category_id, smt, db_version, adr, point)
    a = pm._cd_profit_breakdown(200.0, 8000.0, 500.0, 20.0, 15.0, 5.0, 58248, 0.0, 0)
    b = pm._cd_profit_breakdown(200.0, 8000.0, 500.0, 20.0, 15.0, 5.0, 261186, 0.0, 0)
    assert a is not None and b is not None, (a, b)
    assert a["noref_us"] != b["noref_us"], \
        "category 変更で利益が変わらない = FVF がカテゴリ非連動に退行"


# ---------------------------------------------------------------------------
# AppTest: 行選択 -> 編集ゾーン解決 / フィルタ変更で選択破棄
# ---------------------------------------------------------------------------

def _app_script() -> str:
    """商品管理タブを描画する AppTest 用スクリプト source を返す.

    AppTest.from_string で実行する (from_function は exec 生成関数の source を
    inspect できず失敗するため)。sys.path 補完と live eBay Account API stub を
    自己完結で埋め込み、network 非依存で render 経路だけを検証する。
    """
    return (
        "import sys\n"
        "from pathlib import Path\n"
        f"_root = Path(r'{_ROOT}')\n"
        "if str(_root) not in sys.path:\n"
        "    sys.path.insert(0, str(_root))\n"
        "import tabs.tab_product_management as _pm\n"
        "from monitor.ebay_account_policy import ShippingPolicyList\n"
        "_pm._cached_shipping_policies = lambda: ShippingPolicyList(\n"
        "    policies=(), ok=False, error='(test stub)')\n"
        "_pm.render_product_management({})\n"
    )


@pytest.fixture
def _seeded_db():
    """隔離 tmp DB (conftest autouse) を init_db で全テーブル作成し、active listing
    2 件 (in_stock / out_of_stock) を seed する。render を network・本番 DB 非依存で
    走らせるための最小データ。row0 = Alpha(in_stock) / row1 = Bravo(out_of_stock)。"""
    from monitor.database import init_db, get_conn
    init_db()
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
            "shipping_cost, primary_market, rank, source_status, is_ended) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            ("900000000001", "stock1", "W225 Test Alpha", 100.0, 10.0,
             "US_only", "A", "in_stock"),
        )
        c.execute(
            "INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, "
            "shipping_cost, primary_market, rank, source_status, is_ended) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            ("900000000002", "ebayyh_x", "W225 Test Bravo", 50.0, 5.0,
             "global_only", "B", "out_of_stock"),
        )
    return 2


def test_no_selection_shows_prompt(_seeded_db):
    at = AppTest.from_string(_app_script(), default_timeout=60)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    infos = [m.value for m in at.info]
    assert any("行をクリック" in s for s in infos), infos
    # 編集フォームは未選択時は出ない
    assert len(at.get("form")) == 0


def test_row_selection_opens_matching_item_id(_seeded_db):
    at = AppTest.from_string(_app_script(), default_timeout=60)
    at.run()
    df = at.dataframe[0].value
    expected_eid = str(df.iloc[0]["Item ID"])
    # 行 0 を選択して再 run
    at.session_state["pm_list_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    # 編集中バナー (success) に iloc[0] の Item ID が出ている = 表示行と一致解決
    banners = [m.value for m in at.success]
    assert any(expected_eid in b for b in banners), (banners, expected_eid)
    # 編集ゾーン (form) が描画されている
    assert len(at.get("form")) >= 1


def test_filter_change_clears_stale_selection(_seeded_db):
    at = AppTest.from_string(_app_script(), default_timeout=60)
    at.run()
    # 行 0 (in_stock の Alpha) を選択 -> editor 展開
    at.session_state["pm_list_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    assert len(at.get("form")) >= 1, "前提: 選択で editor が開く"
    # フィルタ (在庫切れのみ) を on -> filtered が 1 件 (Bravo) に変わる
    at.checkbox(key="pm_only_oos").set_value(True).run()
    assert not at.exception, [str(e) for e in at.exception]
    # 残留選択が破棄され、編集ゾーンは開いていない (= 別 listing 誤編集を防ぐ)
    assert len(at.get("form")) == 0, "フィルタ変更後も editor が残る = 選択未破棄"
    infos = [m.value for m in at.info]
    assert any("行をクリック" in s for s in infos), infos
