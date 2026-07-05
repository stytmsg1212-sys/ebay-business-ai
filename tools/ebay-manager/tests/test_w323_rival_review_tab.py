"""W323 (2026-07-05): AI店長「要確認」レビュー triage 画面.

カバレッジ:
  - get_review_discoveries_grouped: status='new' を ebay_item_id ごとに
    グループ化 + 最新 rival_classifications の reason/confidence/route を結合
    (再分類で複数行溜まっても MAX(id) の 1 行のみ結合されること)
  - resolve_review_discovery:
      - noise → status='dismissed'
      - real  → add_or_reactivate_competitor 経由で
                competitor_products upsert + status='monitoring_added'
                (pricing_eligible は Shadow 安全に 0 のまま)
      - 二重処理防止: 既に status != 'new' なら現状 status を返し no-op
  - dismiss_discoveries_by_seller: 同一セラーの status='new' を全商品横断で一括除外
  - count_new_rival_discoveries: 残件数
  - tabs/tab_rival_review.py: import + render 関数の存在確認 (import/renderable 回帰)
"""
from __future__ import annotations

import pytest

from monitor.database import get_conn


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _seed_listing(ebay_item_id: str, title: str, price: float, sku: str = "stock01") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, current_price, ebay_condition_id, condition_rank)
               VALUES (?, ?, ?, ?, '3000', 'B')""",
            (ebay_item_id, sku, title, price),
        )


def _seed_discovery(
    *,
    ebay_item_id: str,
    competitor_item_id: str,
    competitor_seller: str = "jp_seller_1",
    competitor_title: str = "競合商品",
    competitor_price: float = 100.0,
    status: str = "new",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO listing_rival_discoveries
               (ebay_item_id, competitor_seller, competitor_item_id,
                competitor_title, competitor_price_usd, search_keyword, status)
               VALUES (?, ?, ?, ?, ?, 'kw', ?)""",
            (ebay_item_id, competitor_seller, competitor_item_id,
             competitor_title, competitor_price, status),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _seed_classification(discovery_id: int, ebay_item_id: str, competitor_item_id: str,
                          *, classification: str = "review", route: str = "ai",
                          confidence: float = 0.7, reason: str = "やや不確か") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO rival_classifications
               (discovery_id, ebay_item_id, competitor_item_id, classification,
                route, confidence, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (discovery_id, ebay_item_id, competitor_item_id, classification,
             route, confidence, reason),
        )


# ────────────────────────────────────────────────────────────────
# get_review_discoveries_grouped
# ────────────────────────────────────────────────────────────────

def test_grouped_by_ebay_item_id_not_sku(tmp_db):
    """同一 SKU を共有する 2 listing の discovery が別グループになる (sku-rules 準拠)."""
    from monitor.database import get_review_discoveries_grouped

    _seed_listing("OUR1", "Sony Headphones A", 200.0, sku="stock01")
    _seed_listing("OUR2", "Sony Headphones B", 250.0, sku="stock01")  # 同じ SKU
    _seed_discovery(ebay_item_id="OUR1", competitor_item_id="COMP1")
    _seed_discovery(ebay_item_id="OUR2", competitor_item_id="COMP2")

    groups = get_review_discoveries_grouped()
    eids = {g["ebay_item_id"] for g in groups}
    assert eids == {"OUR1", "OUR2"}, "SKU 共有でもグループが潰れず listing 単位のまま"


def test_grouped_uses_latest_classification_only(tmp_db):
    """再分類で複数行溜まっても MAX(id) の最新行のみが結合される."""
    from monitor.database import get_review_discoveries_grouped

    _seed_listing("OUR1", "Sony Headphones", 200.0)
    did = _seed_discovery(ebay_item_id="OUR1", competitor_item_id="COMP1")
    _seed_classification(did, "OUR1", "COMP1", confidence=0.6, reason="1st pass")
    _seed_classification(did, "OUR1", "COMP1", confidence=0.75, reason="2nd pass (latest)")

    groups = get_review_discoveries_grouped()
    assert len(groups) == 1
    discs = groups[0]["discoveries"]
    assert len(discs) == 1, "rival_classifications 複数行で discovery 行が重複してはいけない"
    assert discs[0]["ai_reason"] == "2nd pass (latest)"
    assert discs[0]["ai_confidence"] == 0.75


def test_grouped_only_includes_status_new(tmp_db):
    from monitor.database import get_review_discoveries_grouped

    _seed_listing("OUR1", "Sony Headphones", 200.0)
    _seed_discovery(ebay_item_id="OUR1", competitor_item_id="COMP1", status="dismissed")
    _seed_discovery(ebay_item_id="OUR1", competitor_item_id="COMP2", status="monitoring_added")

    groups = get_review_discoveries_grouped()
    assert groups == []


# ────────────────────────────────────────────────────────────────
# resolve_review_discovery
# ────────────────────────────────────────────────────────────────

def test_resolve_noise_dismisses(tmp_db):
    from monitor.database import resolve_review_discovery

    _seed_listing("OUR1", "Sony Headphones", 200.0)
    did = _seed_discovery(ebay_item_id="OUR1", competitor_item_id="COMP1")

    action = resolve_review_discovery(did, "noise", our_item_id="OUR1")
    assert action == "dismissed"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM listing_rival_discoveries WHERE id=?", (did,)
        ).fetchone()
    assert row[0] == "dismissed"


def test_resolve_real_upserts_competitor_and_updates_status(tmp_db):
    from monitor.database import resolve_review_discovery

    _seed_listing("OUR1", "Sony Headphones", 200.0, sku="stock01")
    did = _seed_discovery(
        ebay_item_id="OUR1", competitor_item_id="COMP1",
        competitor_seller="jp_seller_x",
    )

    action = resolve_review_discovery(
        did, "real", our_item_id="OUR1", our_sku="stock01",
    )
    assert action in ("added", "reactivated")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM listing_rival_discoveries WHERE id=?", (did,)
        ).fetchone()
        comp = conn.execute(
            "SELECT is_active, pricing_eligible, our_item_id FROM competitor_products "
            "WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert row[0] == "monitoring_added"
    assert comp is not None
    assert comp[0] == 1
    assert (comp[1] or 0) == 0, "Shadow 中は pricing_eligible が絶対 0"
    assert comp[2] == "OUR1"


def test_resolve_invalid_decision_raises(tmp_db):
    from monitor.database import resolve_review_discovery

    _seed_listing("OUR1", "Sony Headphones", 200.0)
    did = _seed_discovery(ebay_item_id="OUR1", competitor_item_id="COMP1")

    with pytest.raises(ValueError):
        resolve_review_discovery(did, "bogus", our_item_id="OUR1")


def test_resolve_double_processing_is_noop_and_reports_current_status(tmp_db):
    """既に処理済 (status != 'new') の discovery を再度確定しようとしても状態を壊さない."""
    from monitor.database import resolve_review_discovery

    _seed_listing("OUR1", "Sony Headphones", 200.0)
    did = _seed_discovery(
        ebay_item_id="OUR1", competitor_item_id="COMP1", status="dismissed",
    )

    action = resolve_review_discovery(did, "real", our_item_id="OUR1")
    assert action == "dismissed", "既存 status をそのまま返す (二重処理防止)"

    with get_conn() as conn:
        comp = conn.execute(
            "SELECT 1 FROM competitor_products WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert comp is None, "既に dismissed の discovery から誤って競合登録されない"


# ────────────────────────────────────────────────────────────────
# dismiss_discoveries_by_seller (ノイズセラー一掃)
# ────────────────────────────────────────────────────────────────

def test_dismiss_by_seller_crosses_groups(tmp_db):
    """同一セラーの discovery は自社商品 (ebay_item_id) を跨いで一括除外される."""
    from monitor.database import dismiss_discoveries_by_seller

    _seed_listing("OUR1", "Item A", 100.0)
    _seed_listing("OUR2", "Item B", 150.0)
    _seed_discovery(
        ebay_item_id="OUR1", competitor_item_id="COMP1",
        competitor_seller="noise_seller",
    )
    _seed_discovery(
        ebay_item_id="OUR2", competitor_item_id="COMP2",
        competitor_seller="noise_seller",
    )
    _seed_discovery(
        ebay_item_id="OUR1", competitor_item_id="COMP3",
        competitor_seller="other_seller",
    )

    n = dismiss_discoveries_by_seller("noise_seller")
    assert n == 2

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT competitor_item_id, status FROM listing_rival_discoveries "
            "ORDER BY competitor_item_id"
        ).fetchall()
    status_by_id = {r[0]: r[1] for r in rows}
    assert status_by_id["COMP1"] == "dismissed"
    assert status_by_id["COMP2"] == "dismissed"
    assert status_by_id["COMP3"] == "new", "別セラーの discovery は影響を受けない"


def test_dismiss_by_seller_does_not_touch_already_resolved(tmp_db):
    from monitor.database import dismiss_discoveries_by_seller

    _seed_listing("OUR1", "Item A", 100.0)
    _seed_discovery(
        ebay_item_id="OUR1", competitor_item_id="COMP1",
        competitor_seller="noise_seller", status="monitoring_added",
    )

    n = dismiss_discoveries_by_seller("noise_seller")
    assert n == 0, "既に monitoring_added の行は status='new' でないため対象外"


# ────────────────────────────────────────────────────────────────
# count_new_rival_discoveries
# ────────────────────────────────────────────────────────────────

def test_count_new_rival_discoveries(tmp_db):
    from monitor.database import count_new_rival_discoveries

    assert count_new_rival_discoveries() == 0
    _seed_listing("OUR1", "Item A", 100.0)
    _seed_discovery(ebay_item_id="OUR1", competitor_item_id="COMP1")
    _seed_discovery(ebay_item_id="OUR1", competitor_item_id="COMP2", status="dismissed")
    assert count_new_rival_discoveries() == 1


# ────────────────────────────────────────────────────────────────
# import/renderable 回帰 (UI モジュール)
# ────────────────────────────────────────────────────────────────

def test_tab_rival_review_module_imports_and_exposes_render_func():
    import importlib
    mod = importlib.import_module("tabs.tab_rival_review")
    importlib.reload(mod)
    assert hasattr(mod, "render_rival_review_tab")
    assert callable(mod.render_rival_review_tab)
