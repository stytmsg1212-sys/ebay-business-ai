"""バグ修正テスト 2026-06-11.

Bug 1: ヤフオク終了ページ (HTTP 404 + 本文に「このオークションは終了」) が not_found に
       誤分類される問題。_check_with_httpx と _check_urls_batch_async (Playwright) の
       404 処理で本文キーワード判定を先行させることで修正。

Bug 2a: source_out_of_stock_since の書込値が JST naive (checked_at 由来) のため、
        消費側の datetime('now') UTC 比較と 9h ズレる問題。
        task_sync_data_stores.py で UTC 形式に統一する修正。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ===========================================================================
# Bug 1: _check_with_httpx の 404 + 本文キーワード判定
# ===========================================================================

class TestStatusFor404:
    """_status_for_404 helper の単体テスト。"""

    def _call(self, content: str) -> str:
        from monitor.scrapers import _status_for_404
        sold_out_texts = ["このオークションは終了"]
        no_page_texts = ["このオークションは存在しません"]
        in_stock_texts = ["入札する"]
        return _status_for_404(content, in_stock_texts, sold_out_texts, no_page_texts)

    def test_404_with_ended_text_returns_unavailable(self):
        """404 + 本文に「このオークションは終了」→ unavailable。"""
        content = "<html>このオークションは終了しました</html>"
        result = self._call(content)
        assert result == "unavailable"

    def test_404_with_not_found_text_returns_not_found(self):
        """404 + 本文に「このオークションは存在しません」→ not_found。"""
        content = "<html>このオークションは存在しません</html>"
        result = self._call(content)
        assert result == "not_found"

    def test_404_with_only_in_stock_text_returns_not_found(self):
        """404 + 本文に in_stock 文字列のみ (テンプレ残存) → not_found (安全弁)。"""
        content = "<html>入札するボタン(テンプレート)</html>"
        result = self._call(content)
        assert result == "not_found"

    def test_404_with_undetermined_content_returns_not_found(self):
        """404 + 判定不能な本文 → not_found。"""
        content = "<html><head></head><body>Error</body></html>"
        result = self._call(content)
        assert result == "not_found"


class TestCheckWithHttpx404:
    """_check_with_httpx の 404 処理が本文判定を経由すること。"""

    def _make_resp(self, status_code: int, text: str) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        return resp

    def test_404_with_auction_ended_keyword_returns_unavailable(self):
        """httpx 経路: 404 + 終了キーワード → unavailable。"""
        from monitor.scrapers import _check_with_httpx
        fake_resp = self._make_resp(404, "<html>このオークションは終了しました</html>")
        with patch("monitor.scrapers.httpx.get", return_value=fake_resp):
            result = _check_with_httpx(
                url="https://auctions.yahoo.co.jp/jp/auction/b1189813429",
                in_stock_texts=["入札する"],
                sold_out_texts=["このオークションは終了"],
                no_page_texts=["このオークションは存在しません"],
            )
        assert result == "unavailable"

    def test_404_with_not_found_keyword_returns_not_found(self):
        """httpx 経路: 404 + ページなしキーワード → not_found。"""
        from monitor.scrapers import _check_with_httpx
        fake_resp = self._make_resp(404, "<html>このオークションは存在しません</html>")
        with patch("monitor.scrapers.httpx.get", return_value=fake_resp):
            result = _check_with_httpx(
                url="https://auctions.yahoo.co.jp/jp/auction/xxxx",
                in_stock_texts=["入札する"],
                sold_out_texts=["このオークションは終了"],
                no_page_texts=["このオークションは存在しません"],
            )
        assert result == "not_found"

    def test_404_with_no_keyword_returns_none_for_playwright_fallback(self):
        """httpx 経路: 404 + 判定不能本文 → None (Playwright fallback に逃がす)。

        2026-06-11 実機検証: ヤフオクの 404 ページは httpx 本文に終了文言が無く、
        JS 描画後にのみ「このオークションは終了」が出る個体がある。not_found 確定
        にすると在庫無を取りこぼすため、None で Playwright に回す。
        """
        from monitor.scrapers import _check_with_httpx
        fake_resp = self._make_resp(404, "<html><body>Generic error page</body></html>")
        with patch("monitor.scrapers.httpx.get", return_value=fake_resp):
            result = _check_with_httpx(
                url="https://auctions.yahoo.co.jp/jp/auction/xxxx",
                in_stock_texts=["入札する"],
                sold_out_texts=["このオークションは終了"],
                no_page_texts=["このオークションは存在しません"],
            )
        assert result is None

    def test_404_with_only_in_stock_text_returns_none_for_playwright_fallback(self):
        """httpx 経路: 404 + in_stock 文字列のみ (テンプレ残存) → None (available 確定は禁止)。"""
        from monitor.scrapers import _check_with_httpx
        fake_resp = self._make_resp(404, "<html>入札するボタン(テンプレート)</html>")
        with patch("monitor.scrapers.httpx.get", return_value=fake_resp):
            result = _check_with_httpx(
                url="https://auctions.yahoo.co.jp/jp/auction/xxxx",
                in_stock_texts=["入札する"],
                sold_out_texts=["このオークションは終了"],
                no_page_texts=["このオークションは存在しません"],
            )
        assert result is None

    def test_200_ok_still_works(self):
        """200 正常レスポンスは従来通り動作する (回帰)。"""
        from monitor.scrapers import _check_with_httpx
        fake_resp = self._make_resp(200, "<html>入札する</html>")
        with patch("monitor.scrapers.httpx.get", return_value=fake_resp):
            result = _check_with_httpx(
                url="https://auctions.yahoo.co.jp/jp/auction/active",
                in_stock_texts=["入札する"],
                sold_out_texts=["このオークションは終了"],
                no_page_texts=["このオークションは存在しません"],
            )
        assert result == "available"


# ===========================================================================
# Bug 2a: task_sync_data_stores の source_out_of_stock_since 書込値が UTC 形式
# ===========================================================================

class TestOosSinceUtcFormat:
    """sync_inventory_status_to_db が source_out_of_stock_since に UTC 形式を書き込むこと。"""

    # NOTE: 2026-06-11 code-reviewer 指摘で production コードを通らない
    # トートロジーテスト (ローカル strftime の自己検証) を 1 本削除。
    # 実カバーは下の DB レベルテストが担う。

    def test_oos_since_db_write_is_utc_format(self, tmp_path):
        """sync_inventory_status_to_db が実際に 'T' なし UTC 形式で書き込むことを DB レベルで検証。"""
        import json
        from monitor.database import init_db, get_conn

        init_db()
        test_eid = "test_oos_utc_write_8888888"

        # テスト用レコードを準備 (active, 初回在庫無になる状態)
        with get_conn() as conn:
            conn.execute("DELETE FROM ebay_listings WHERE ebay_item_id=?", (test_eid,))
            conn.execute(
                """INSERT INTO ebay_listings (ebay_item_id, sku, title, source_status, source_out_of_stock_since)
                   VALUES (?, ?, ?, ?, ?)""",
                (test_eid, "ebayme_test8888", "Test Item 8888", "在庫有", None),
            )

        # inventory_check_results.json を tmp_path/data に用意
        # (task_sync_data_stores は BASE_DIR / 'data' / 'inventory_check_results.json' を読む)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        inv_data = {
            "results": [
                {
                    "ebay_id": test_eid,
                    "sku": "ebayme_test8888",
                    "url": "https://jp.mercari.com/item/test8888",
                    "status": "在庫無",
                    "checked_at": "2026-06-11T09:00:00.123456",  # JST naive 形式 (旧バグの値)
                }
            ]
        }
        inv_file = data_dir / "inventory_check_results.json"
        inv_file.write_text(json.dumps(inv_data), encoding="utf-8")

        # BASE_DIR をモックして tmp_path を向ける
        import tasks.task_sync_data_stores as tsd
        original_base = tsd.BASE_DIR
        tsd.BASE_DIR = tmp_path
        try:
            tsd.sync_inventory_status_to_db()
        finally:
            tsd.BASE_DIR = original_base

        # DB の値を確認: 'T' を含まない UTC 形式であること
        with get_conn() as conn:
            row = conn.execute(
                "SELECT source_out_of_stock_since FROM ebay_listings WHERE ebay_item_id=?",
                (test_eid,),
            ).fetchone()

        assert row is not None, "レコードが見つからない"
        oos_val = row[0]
        assert oos_val is not None, "source_out_of_stock_since が NULL のまま"
        assert "T" not in oos_val, f"UTC 形式でない (T 区切り含む): {oos_val!r}"
        # パース可能かつ '%Y-%m-%d %H:%M:%S' 形式
        from datetime import datetime
        parsed = datetime.strptime(oos_val, "%Y-%m-%d %H:%M:%S")
        assert parsed is not None

        # クリーンアップ
        with get_conn() as conn:
            conn.execute("DELETE FROM ebay_listings WHERE ebay_item_id=?", (test_eid,))
