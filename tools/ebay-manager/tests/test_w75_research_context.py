"""W75 Iteration 4a regression test: research_context.py:227 SQL `WHERE sku=?` 撤去.

検証対象 (`monitor/research_context.py:194-238` build_dynamic_context):
- ebay_item_id hint で listing detail block 取得 (新 path、sku-rules.md 準拠)
- hint なしで listing block skip (現状動作維持、draft ケース)
- legacy sku hint は完全無視 (caller 移行漏れ時も AI への random data 流入を物理 BLOCK)
- SQL `WHERE sku=?` 系発行ゼロ (regex spy で検証、将来回帰防止)
- caller (tab_individual_listing.py:1473) で context_hints から sku キー削除済 (regex grep)
- 混在期 safety: ebay_item_id + sku 両 hint 時に sku 完全 ignore

過去事故:
- 2026-05-01 H-1 verify: stock:01 hint で Vocaloid4 ソフト (rank S, $149.38, 450g) 返却 →
  draft 監修で random 商品 context が AI に渡る品質事故 (実証済)
- 2026-04-29 W7-A SKU 主キー崩壊 / 2026-04-30 SKU 一意性誤推論 (連続違反)

詳細: `.claude/rules/sku-rules.md` / `feedback_sku_misuse_repeat_offense.md`
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """fresh DB を作って monitor.database.DB_PATH + research_context.DB_PATH を差し替え"""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    import monitor.research_context as rc_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    monkeypatch.setattr(rc_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert(conn, eid, sku, **kw):
    defaults = dict(title="T", current_price=100.0, quantity_ebay=1, is_ended=0,
                    rank="C", watch_count=5, sales_count_30d=2,
                    weight_g=500.0, source_status="在庫有")
    defaults.update(kw)
    conn.execute(
        """INSERT INTO ebay_listings
           (ebay_item_id, sku, title, current_price, quantity_ebay, is_ended,
            rank, watch_count, sales_count_30d, weight_g, source_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (eid, sku, defaults["title"], defaults["current_price"],
         defaults["quantity_ebay"], defaults["is_ended"], defaults["rank"],
         defaults["watch_count"], defaults["sales_count_30d"],
         defaults["weight_g"], defaults["source_status"]),
    )


def test_build_dynamic_context_with_ebay_item_id_hint(tmp_db):
    """ebay_item_id hint で listing detail block 取得"""
    from monitor.database import get_conn
    from monitor.research_context import build_dynamic_context

    with get_conn() as c:
        _insert(c, "ITEM_TARGET", "ebayyh_xxx", title="Target listing", rank="A")

    ctx = build_dynamic_context("test query", hints={"ebay_item_id": "ITEM_TARGET"})
    assert "## 対象 listing" in ctx
    assert "ebay_item_id: ITEM_TARGET" in ctx
    assert "Target listing" in ctx
    assert "rank: A" in ctx


def test_build_dynamic_context_skips_listing_block_when_no_hint(tmp_db):
    """hints なし / 空 dict で listing block skip (draft 用、現状動作維持)"""
    from monitor.database import get_conn
    from monitor.research_context import build_dynamic_context

    with get_conn() as c:
        _insert(c, "ITEM_X", "stock:01")

    ctx_none = build_dynamic_context("test query", hints=None)
    ctx_empty = build_dynamic_context("test query", hints={})
    assert "## 対象 listing" not in ctx_none
    assert "## 対象 SKU" not in ctx_none
    assert "## 対象 listing" not in ctx_empty
    assert "## 対象 SKU" not in ctx_empty


def test_build_dynamic_context_legacy_sku_hint_ignored(tmp_db):
    """legacy sku hint は完全無視 (caller 移行漏れ時の AI random data 流入を物理 BLOCK)"""
    from monitor.database import get_conn
    from monitor.research_context import build_dynamic_context

    with get_conn() as c:
        _insert(c, "ITEM_A", "stock:01", title="Listing A")
        _insert(c, "ITEM_B", "stock:01", title="Listing B")

    ctx = build_dynamic_context("test query", hints={"sku": "stock:01"})
    # listing block 出現してはいけない (legacy hint ignore)
    assert "## 対象 listing" not in ctx
    assert "## 対象 SKU" not in ctx
    assert "Listing A" not in ctx
    assert "Listing B" not in ctx


def test_build_dynamic_context_with_both_hints_prefers_ebay_item_id(tmp_db):
    """ebay_item_id + legacy sku 両 hint 時、ebay_item_id のみ採用 (sku 完全無視、混在期 safety)"""
    from monitor.database import get_conn
    from monitor.research_context import build_dynamic_context

    with get_conn() as c:
        _insert(c, "ITEM_TARGET", "ebayyh_xxx", title="Correct target")
        _insert(c, "ITEM_OTHER", "stock:01", title="Wrong listing (sku-keyed)")

    ctx = build_dynamic_context(
        "test query",
        hints={"ebay_item_id": "ITEM_TARGET", "sku": "stock:01"},
    )
    assert "Correct target" in ctx
    assert "Wrong listing" not in ctx, (
        "両 hint 時に sku 経由で別 listing が混入: caller 移行漏れの safety net 機能不全"
    )


def test_no_where_sku_sql_in_research_context_module():
    """monitor/research_context.py module source に WHERE sku= / WHERE sku IN SQL がないことを保証.

    将来「fallback で SKU lookup 復活」の regression を物理 BLOCK (static source check).
    sku-rules.md 許可 pattern (LIKE 'stock%' / LIKE 'ebay%' prefix filter) は除外.
    sqlite3.Cursor.execute monkeypatch は immutable 制約で不可のため static 検証で代替.
    """
    code = (
        Path(__file__).resolve().parent.parent
        / "monitor" / "research_context.py"
    )
    text = code.read_text(encoding="utf-8")
    # regex で WHERE sku=? / WHERE sku IN を検出
    sku_violation_re = re.compile(
        r"WHERE\s+sku\s*(=|IN\s*\()", re.IGNORECASE
    )
    matches = sku_violation_re.findall(text)
    # 除外: LIKE 'stock%' / 'ebay%' prefix filter (sku-rules.md 許可)
    # ただし current research_context.py は LIKE pattern も使っていない → 全 0 が期待
    assert matches == [], (
        f"SKU rule 違反 SQL 残存 (sku-rules.md L41): {matches}"
    )


def test_individual_listing_omits_sku_hint():
    """tab_individual_listing.py:1473 周辺の caller が context_hints から sku キーを削除済 (regex grep)"""
    code = (
        Path(__file__).resolve().parent.parent
        / "tabs" / "tab_individual_listing.py"
    )
    text = code.read_text(encoding="utf-8")
    # context_hints の dict literal 内に "sku" / 'sku' キーが含まれてはいけない
    # (single quote / space variant / spread 全て catch する regex)
    violation_re = re.compile(
        r'context_hints\s*=\s*\{[^}]*["\']sku["\']', re.MULTILINE
    )
    matches = violation_re.findall(text)
    assert matches == [], (
        f"tab_individual_listing.py の context_hints に sku キー残存 (W75 移行漏れ): {matches}"
    )
