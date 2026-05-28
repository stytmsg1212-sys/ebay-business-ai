"""W182 (2026-05-28) availability gate の最小 unit test.

scope:
- migration v54 後の supplier_candidates 3 列存在確認
- add_supplier_candidate に availability_* を渡せること (backward-compat: 渡さなくても動く)
- check_candidate_availability の host 判定分岐 (paypay / yahoo_auctions / 他) が正しく動くこと

実 web fetch は flaky のため mock。実機 verify は Phase 0 で別途実施済
(monitor/scrapers.py 実機 5 URL 検証 OK、本 session の memory に記録).
"""

from __future__ import annotations

import sys
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

# sys.path に tools/ebay-manager を追加 (test 単独実行用)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_w182_v54_migration_columns_present():
    """migration v54 適用後、supplier_candidates に 3 列が存在することを確認."""
    from monitor.database import init_db, get_conn
    init_db()
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        cols = {r[1] for r in c.execute("PRAGMA table_info(supplier_candidates)").fetchall()}
    assert ver >= 54, f"expected user_version >= 54, got {ver}"
    assert "availability_status" in cols
    assert "availability_checked_at" in cols
    assert "availability_signal" in cols


def test_w182_add_supplier_candidate_with_availability():
    """add_supplier_candidate に availability_* を渡して INSERT/SELECT round-trip 確認."""
    from monitor.database import init_db, get_conn, add_supplier_candidate
    init_db()
    test_url = "https://test.example.com/w182_test_item_12345"
    test_sku = "test_w182_sku"
    # 既存重複は cleanup
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (test_url,))
    row_id = add_supplier_candidate(
        sku=test_sku,
        candidate_url=test_url,
        source_platform="test_platform",
        availability_status="available",
        availability_checked_at="2026-05-28T12:00:00+00:00",
        availability_signal="test signal",
    )
    assert row_id is not None
    with get_conn() as c:
        row = c.execute(
            "SELECT availability_status, availability_checked_at, availability_signal "
            "FROM supplier_candidates WHERE id=?", (row_id,)
        ).fetchone()
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (row_id,))
    assert row[0] == "available"
    assert row[1] == "2026-05-28T12:00:00+00:00"
    assert row[2] == "test signal"


def test_w182_add_supplier_candidate_backward_compat():
    """availability_* を渡さない既存呼出 (backward compat) でも INSERT 成功する."""
    from monitor.database import init_db, get_conn, add_supplier_candidate
    init_db()
    test_url = "https://test.example.com/w182_backcompat_67890"
    with get_conn() as c:
        c.execute("DELETE FROM supplier_candidates WHERE candidate_url=?", (test_url,))
    row_id = add_supplier_candidate(
        sku="bc_test_sku",
        candidate_url=test_url,
        source_platform="bc_platform",
    )
    assert row_id is not None
    with get_conn() as c:
        row = c.execute(
            "SELECT availability_status FROM supplier_candidates WHERE id=?", (row_id,)
        ).fetchone()
        c.execute("DELETE FROM supplier_candidates WHERE id=?", (row_id,))
    assert row[0] is None  # NULL = 旧 candidate と同等


def test_w182_check_paypay_sold_out_signal():
    """PayPay の HTML に '購入日時' があれば unavailable と判定 (mock fetch)."""
    from monitor.scrapers import _check_paypay_availability
    fake_html = """<html>...<div>購入日時：2025年12月13日 00:53</div>...
<script>{"availability":"SoldOut"}</script>...
<script type="application/ld+json">{"@type":"Product","offers":{"availability":"http://schema.org/InStock"}}</script>
</html>"""
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = fake_html
    with patch("monitor.scrapers.httpx.get", return_value=fake_resp):
        r = _check_paypay_availability(
            "https://paypayfleamarket.yahoo.co.jp/item/test", timeout_sec=5, checked_at="2026-05-28T12:00:00+00:00"
        )
    assert r["status"] == "unavailable"
    assert "購入日時" in r["signal"]


def test_w182_check_paypay_available_signal():
    """PayPay の HTML に sold_out signal が無く '購入手続きへ' があれば available."""
    from monitor.scrapers import _check_paypay_availability
    fake_html = """<html>...<button>購入手続きへ</button>...
<script type="application/ld+json">{"@type":"Product","offers":{"availability":"http://schema.org/InStock"}}</script>
</html>"""
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = fake_html
    with patch("monitor.scrapers.httpx.get", return_value=fake_resp):
        r = _check_paypay_availability(
            "https://paypayfleamarket.yahoo.co.jp/item/test", timeout_sec=5, checked_at="2026-05-28T12:00:00+00:00"
        )
    assert r["status"] == "available"


def test_w182_check_paypay_404():
    """PayPay 404 → not_found."""
    from monitor.scrapers import _check_paypay_availability
    fake_resp = MagicMock()
    fake_resp.status_code = 404
    fake_resp.text = ""
    with patch("monitor.scrapers.httpx.get", return_value=fake_resp):
        r = _check_paypay_availability(
            "https://paypayfleamarket.yahoo.co.jp/item/test", timeout_sec=5, checked_at="2026-05-28T12:00:00+00:00"
        )
    assert r["status"] == "not_found"


def test_w182_check_yahoo_auctions_ended():
    """ヤフオク 'このオークションは終了' → not_found."""
    from monitor.scrapers import _check_yahoo_auctions_availability
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = "<html>このオークションは終了しています</html>"
    with patch("monitor.scrapers.httpx.get", return_value=fake_resp):
        r = _check_yahoo_auctions_availability(
            "https://auctions.yahoo.co.jp/jp/auction/test", timeout_sec=5, checked_at="2026-05-28T12:00:00+00:00"
        )
    assert r["status"] == "not_found"


def test_w182_check_yahoo_auctions_active():
    """ヤフオク '入札する' → available."""
    from monitor.scrapers import _check_yahoo_auctions_availability
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = "<html><button>入札する</button></html>"
    with patch("monitor.scrapers.httpx.get", return_value=fake_resp):
        r = _check_yahoo_auctions_availability(
            "https://auctions.yahoo.co.jp/jp/auction/test", timeout_sec=5, checked_at="2026-05-28T12:00:00+00:00"
        )
    assert r["status"] == "available"


def test_w182_check_candidate_availability_dispatches_by_host():
    """check_candidate_availability が URL host に応じて専用 helper に dispatch する."""
    from monitor.scrapers import check_candidate_availability
    # 空 URL は unknown
    r = check_candidate_availability("")
    assert r["status"] == "unknown"
    assert r["signal"] == "empty url"
