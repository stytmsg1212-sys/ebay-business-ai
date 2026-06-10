"""W#3 ライバルセラー新規出品モニター テスト。

不変条件:
1. migration v68: monitored_sellers / monitored_seller_listings が正しく生成される。
2. init_db 2 回でデータ保持 (冪等性 Q2)。
3. add_monitored_seller: INSERT OR IGNORE で UNIQUE(seller_id) 重複は既存行を返す。
4. _claim_new_listing: UNIQUE(ebay_item_id) 重複は False を返す (dedupe)。
5. get_recent_detections: listing 識別は ebay_item_id (sku 不使用)。
6. JP未確認 (is_jp_verified=0) で登録は成功し、警告フラグが立つ。
7. Browse API / Claude / Discord は全 mock。
8. delete_monitored_seller: seller + listing が両方削除される。

テスト DB は tmp_path を使い本番 DB を一切触らない (Q2)。
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ebay-manager root を sys.path に追加
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── DB を tmp_path にリダイレクトするフィクスチャ ─────────────────────────

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """monitor.database.DB_PATH を一時 DB にリダイレクト。"""
    db_file = tmp_path / "test_monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    # rival_seller_monitor は get_conn を monitor.database からインポートするため
    # こちらも差し替える
    import monitor.rival_seller_monitor as rsm
    monkeypatch.setattr(rsm, "get_conn", db_mod.get_conn)
    db_mod.init_db()
    return db_file, db_mod, rsm


# ── テスト ────────────────────────────────────────────────────────────────

class TestMigrationV68:
    """migration v68 冪等性テスト。"""

    def test_tables_created(self, tmp_db):
        db_file, db_mod, _ = tmp_db
        conn = sqlite3.connect(str(db_file))
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "monitored_sellers" in tables
        assert "monitored_seller_listings" in tables
        conn.close()

    def test_schema_version_at_least_68(self, tmp_db):
        # v69 追加後: user_version は 69 以上 (v69 migration が正常に実行された証拠)。
        # 「== 68」から「>= 68」に緩和し、将来の migration 追加でも壊れない。
        db_file, db_mod, _ = tmp_db
        conn = sqlite3.connect(str(db_file))
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 68
        conn.close()

    def test_idempotent_init_db(self, tmp_db):
        """init_db 2 回でデータが消えないこと (Q2 冪等性)。"""
        db_file, db_mod, rsm = tmp_db

        # 1回目: データ挿入
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("seller_test", "Test Seller")

        # 2回目: init_db 再実行
        db_mod.init_db()

        conn = sqlite3.connect(str(db_file))
        count = conn.execute(
            "SELECT COUNT(*) FROM monitored_sellers WHERE seller_id='seller_test'"
        ).fetchone()[0]
        assert count == 1, "init_db 2 回目でデータが消えた (冪等性違反)"
        conn.close()

    def test_required_columns_sellers(self, tmp_db):
        db_file, _, _ = tmp_db
        conn = sqlite3.connect(str(db_file))
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(monitored_sellers)"
        ).fetchall()}
        required = {"id", "seller_id", "seller_label", "is_jp_verified",
                    "added_at", "last_checked_at", "is_active"}
        assert required <= cols, f"不足列: {required - cols}"
        conn.close()

    def test_required_columns_listings(self, tmp_db):
        db_file, _, _ = tmp_db
        conn = sqlite3.connect(str(db_file))
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(monitored_seller_listings)"
        ).fetchall()}
        required = {"id", "seller_id", "ebay_item_id", "title",
                    "price_usd", "first_seen_at", "notified",
                    "eval_score", "eval_reason"}
        assert required <= cols, f"不足列: {required - cols}"
        conn.close()


class TestAddMonitoredSeller:
    """add_monitored_seller のテスト。"""

    def test_insert_jp_verified(self, tmp_db):
        _, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            db_id, inserted, is_jp = rsm.add_monitored_seller("seller_a", "Seller A")
        assert inserted is True
        assert is_jp is True
        assert db_id > 0

    def test_insert_jp_unverified(self, tmp_db):
        _, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=False):
            db_id, inserted, is_jp = rsm.add_monitored_seller("seller_b", "Seller B")
        assert inserted is True
        assert is_jp is False  # JP未確認でも登録は成功

    def test_duplicate_returns_existing(self, tmp_db):
        _, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            id1, ins1, _ = rsm.add_monitored_seller("seller_dup", "First")
            id2, ins2, _ = rsm.add_monitored_seller("seller_dup", "Second")
        assert ins1 is True
        assert ins2 is False  # 重複: 新規挿入されない
        assert id1 == id2

    def test_empty_seller_id_raises(self, tmp_db):
        _, _, rsm = tmp_db
        with pytest.raises(ValueError):
            rsm.add_monitored_seller("", "Empty ID")

    def test_is_jp_verified_stored_in_db(self, tmp_db):
        db_file, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("seller_jp_ok", "JP OK")
        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT is_jp_verified FROM monitored_sellers WHERE seller_id='seller_jp_ok'"
        ).fetchone()
        assert row[0] == 1
        conn.close()


class TestClaimNewListing:
    """_claim_new_listing の dedupe テスト (SKU不使用、ebay_item_id識別)。"""

    def test_new_listing_returns_true(self, tmp_db):
        _, _, rsm = tmp_db
        # seller を先に登録 (FK は無いが論理的に必要)
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("s1", "S1")
        result = rsm._claim_new_listing("s1", "111111111111", "Test Item", 99.0)
        assert result is True

    def test_duplicate_ebay_item_id_returns_false(self, tmp_db):
        _, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("s2", "S2")
        rsm._claim_new_listing("s2", "222222222222", "Item 2", 50.0)
        # 同じ ebay_item_id を別セラーIDで再投入しても False (UNIQUE(ebay_item_id))
        result2 = rsm._claim_new_listing("s2", "222222222222", "Item 2 dup", 50.0)
        assert result2 is False

    def test_different_item_id_returns_true(self, tmp_db):
        _, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("s3", "S3")
        rsm._claim_new_listing("s3", "333333333333", "Item 3a", 30.0)
        result = rsm._claim_new_listing("s3", "444444444444", "Item 3b", 40.0)
        assert result is True


class TestGetRecentDetections:
    """get_recent_detections: listing識別は ebay_item_id (SKU不使用)。"""

    def test_returns_inserted_items(self, tmp_db):
        _, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("s_det", "S Det")
        rsm._claim_new_listing("s_det", "555555555555", "Detection Item", 120.0)

        detections = rsm.get_recent_detections(limit=10)
        assert len(detections) >= 1
        item_ids = [d["ebay_item_id"] for d in detections]
        assert "555555555555" in item_ids

    def test_seller_filter(self, tmp_db):
        _, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("s_x", "SX")
            rsm.add_monitored_seller("s_y", "SY")
        rsm._claim_new_listing("s_x", "666666666666", "X Item", 10.0)
        rsm._claim_new_listing("s_y", "777777777777", "Y Item", 20.0)

        detections_x = rsm.get_recent_detections(seller_id="s_x", limit=10)
        assert all(d["seller_id"] == "s_x" for d in detections_x)
        assert "666666666666" in [d["ebay_item_id"] for d in detections_x]


class TestDeleteSeller:
    """delete_monitored_seller: seller + listing が両方削除される。"""

    def test_deletes_seller_and_listings(self, tmp_db):
        db_file, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            db_id, _, _ = rsm.add_monitored_seller("s_del", "S Del")
        rsm._claim_new_listing("s_del", "888888888888", "Del Item", 5.0)

        result = rsm.delete_monitored_seller(db_id)
        assert result is True

        conn = sqlite3.connect(str(db_file))
        sellers = conn.execute(
            "SELECT COUNT(*) FROM monitored_sellers WHERE seller_id='s_del'"
        ).fetchone()[0]
        listings = conn.execute(
            "SELECT COUNT(*) FROM monitored_seller_listings WHERE seller_id='s_del'"
        ).fetchone()[0]
        conn.close()
        assert sellers == 0
        assert listings == 0

    def test_delete_nonexistent_returns_false(self, tmp_db):
        _, _, rsm = tmp_db
        result = rsm.delete_monitored_seller(99999)
        assert result is False


class TestRunRivalSellerSweep:
    """run_rival_seller_sweep: Browse API / AI / Discord は全 mock。"""

    def test_no_active_sellers_returns_zero(self, tmp_db):
        _, _, rsm = tmp_db
        result = rsm.run_rival_seller_sweep(config={})
        assert result["sellers_checked"] == 0
        assert result["total_new"] == 0
        assert result["errors"] == []

    def test_sweep_with_mock_api(self, tmp_db):
        _, _, rsm = tmp_db

        # セラー登録
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("mock_seller", "Mock Seller")

        # Browse API → 2件返す mock
        mock_listings = [
            {"item_id": "100000000001", "title": "Item A", "price_usd": 50.0,
             "item_url": "https://www.ebay.com/itm/100000000001",
             "category_path": "Electronics", "image_url": ""},
            {"item_id": "100000000002", "title": "Item B", "price_usd": 30.0,
             "item_url": "https://www.ebay.com/itm/100000000002",
             "category_path": "Cameras", "image_url": ""},
        ]
        with (
            patch.object(rsm, "_get_browse_client") as mock_client_fn,
            patch.object(rsm, "_evaluate_listing", return_value=(75, "テスト評価")),
            patch.object(rsm, "_send_rival_new_listing_alert", return_value=True),
        ):
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            with patch.object(rsm, "_fetch_seller_listings", return_value=mock_listings):
                result = rsm.run_rival_seller_sweep(config={
                    "notifications": {"discord_webhook_url": "https://discord.com/mock"}
                })

        assert result["sellers_checked"] == 1
        assert result["total_new"] == 2
        assert result["total_notified"] == 2
        assert result["errors"] == []

    def test_sweep_dedupe_second_run(self, tmp_db):
        """2回目 sweep で同じ item_id は new_items=0 になる (dedupe)。"""
        _, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("mock_s2", "Mock S2")

        mock_listings = [
            {"item_id": "200000000001", "title": "Dedup Item", "price_usd": 20.0,
             "item_url": "", "category_path": "", "image_url": ""},
        ]
        common_patches = dict(
            _evaluate_listing=(75, "test"),
            _send_rival_new_listing_alert=True,
        )
        with (
            patch.object(rsm, "_get_browse_client") as mcf,
            patch.object(rsm, "_evaluate_listing", return_value=(75, "test")),
            patch.object(rsm, "_send_rival_new_listing_alert", return_value=True),
            patch.object(rsm, "_fetch_seller_listings", return_value=mock_listings),
        ):
            mcf.return_value = MagicMock()
            r1 = rsm.run_rival_seller_sweep(config={})
            r2 = rsm.run_rival_seller_sweep(config={})

        assert r1["total_new"] == 1
        assert r2["total_new"] == 0  # 2回目は既知 = dedupe

    def test_browse_api_none_returns_error(self, tmp_db):
        """Browse API client が None の場合、エラーが errors リストに記録される。
        sellers_checked は 1 (実行試行済み)、total_new=0。
        """
        _, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("s_no_api", "No API")

        with patch.object(rsm, "_get_browse_client", return_value=None):
            result = rsm.run_rival_seller_sweep(config={})

        # sellers_checked=1 (試行したセラー数)、new=0、errors に記録
        assert result["sellers_checked"] == 1
        assert result["total_new"] == 0
        assert len(result["errors"]) == 1
        assert "credentials" in result["errors"][0]


class TestReviewFixes20260607:
    """code-reviewer/Codex 2段で捕捉した HIGH 修正の回帰テスト。"""

    def test_sweep_skips_jp_unverified_seller(self, tmp_db):
        """HIGH-1: is_jp_verified=0 のセラーは sweep で巡回・通知されない (競合=日本限定)."""
        _, _, rsm = tmp_db
        # JP未確認で登録
        with patch.object(rsm, "_verify_seller_is_jp", return_value=False):
            rsm.add_monitored_seller("non_jp_seller", "Non JP")
        # check_seller_new_listings が呼ばれたら失敗 (= JP未確認は巡回対象外)
        with patch.object(
            rsm, "check_seller_new_listings",
            side_effect=AssertionError("JP未確認セラーが巡回された (HIGH-1)"),
        ):
            result = rsm.run_rival_seller_sweep(config={})
        assert result["skipped_not_jp"] == 1
        assert result["sellers_checked"] == 0
        assert result["total_notified"] == 0

    def test_fetch_failure_distinguished_from_empty(self, tmp_db):
        """HIGH-2: Browse 取得失敗は error 化され『新規0件』と区別される (silent skip 防止)."""
        _, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("s_fail", "S Fail")
        with (
            patch.object(rsm, "_get_browse_client", return_value=MagicMock()),
            patch.object(
                rsm, "_fetch_seller_listings",
                side_effect=RuntimeError("anti-bot block (simulated)"),
            ),
        ):
            r = rsm.check_seller_new_listings("s_fail", config={})
        assert r["error"] is not None and "取得失敗" in r["error"]
        assert r["new_items"] == 0
        # sweep 経由でも errors に積まれる
        with (
            patch.object(rsm, "_get_browse_client", return_value=MagicMock()),
            patch.object(
                rsm, "_fetch_seller_listings",
                side_effect=RuntimeError("anti-bot"),
            ),
        ):
            sweep = rsm.run_rival_seller_sweep(config={})
        assert len(sweep["errors"]) == 1

    def test_webhook_missing_not_marked_notified(self, tmp_db):
        """MEDIUM-2: webhook 未設定なら notified=1 を立てない (偽通知防止)."""
        db_file, _, rsm = tmp_db
        with patch.object(rsm, "_verify_seller_is_jp", return_value=True):
            rsm.add_monitored_seller("s_nowh", "S NoWebhook")
        with (
            patch.object(rsm, "_get_browse_client", return_value=MagicMock()),
            patch.object(rsm, "_fetch_seller_listings", return_value=[
                {"item_id": "910000000001", "title": "Test", "price_usd": 50.0,
                 "item_url": "", "category_path": "", "image_url": ""}
            ]),
            patch.object(rsm, "_evaluate_listing", return_value=(90, "ok")),
        ):
            r = rsm.check_seller_new_listings("s_nowh", config={})  # webhook 無し
        assert r["notified"] == 0  # 未送信は notified に計上しない
        conn = sqlite3.connect(str(db_file))
        notified = conn.execute(
            "SELECT notified FROM monitored_seller_listings WHERE ebay_item_id='910000000001'"
        ).fetchone()[0]
        conn.close()
        assert notified == 0, "webhook未設定なのに notified=1 (偽通知)"
