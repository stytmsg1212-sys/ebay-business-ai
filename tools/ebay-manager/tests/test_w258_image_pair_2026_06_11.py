"""W258/Phase-B テスト (2026-06-11).

検証項目:
1. migration v71 冪等性
   - init_db 2 回連続でデータ保持
   - 部分 migration 状態 (candidate_image_url 列のみ存在) から再実行で
     candidate_image_fetched_at も追加されること (Codex L2)
2. _supplier_card_html: 両画像あり / 片方欠落 / 両方欠落 の 3 ケース
3. backfill 2 本: --apply なしで dry-run (DB 書込ゼロ)
"""
from __future__ import annotations

import importlib
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# ヘルパー: テスト用インメモリ DB のセットアップ
# ---------------------------------------------------------------------------

def _make_db(path: str) -> sqlite3.Connection:
    """最低限のスキーマ (v70 相当) を持つテスト DB を作る。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS supplier_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            ebay_item_id TEXT NOT NULL,
            source_platform TEXT,
            candidate_url TEXT NOT NULL,
            candidate_price_jpy INTEGER,
            candidate_title TEXT,
            match_score INTEGER,
            match_reasoning TEXT,
            profit_jpy REAL,
            profitable INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            user_action_at TIMESTAMP,
            discovered_via TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            junk_likely_untested INTEGER DEFAULT 0,
            alt_listing_possible INTEGER DEFAULT 0,
            alt_listing_note TEXT,
            auto_rejected INTEGER DEFAULT 0,
            eval_model TEXT,
            availability_status TEXT,
            availability_checked_at TIMESTAMP,
            availability_signal TEXT,
            UNIQUE(ebay_item_id, candidate_url)
        );
        CREATE TABLE IF NOT EXISTS research_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );
        CREATE TABLE IF NOT EXISTS ebay_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ebay_item_id TEXT UNIQUE,
            title TEXT,
            current_price REAL,
            ebay_image_url TEXT,
            ebay_image_fetched_at TEXT,
            is_ended INTEGER DEFAULT 0
        );
        PRAGMA user_version = 70;
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# B-1 migration v71: 冪等性テスト
# ---------------------------------------------------------------------------

class TestMigrationV71:
    """migration v71 の冪等性と部分 migration 再実行を検証する。"""

    def _run_v71_block(self, conn: sqlite3.Connection) -> None:
        """database.py の v71 ブロックと等価なロジックを直接実行する。"""
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver != 70:
            return
        cols_to_add = {
            "candidate_image_url": "TEXT",
            "candidate_image_fetched_at": "TEXT",
        }
        for col, typ in cols_to_add.items():
            try:
                conn.execute(
                    f"ALTER TABLE supplier_candidates ADD COLUMN {col} {typ}"
                )
            except sqlite3.OperationalError:
                pass  # 列既存 = 冪等
        existing = {
            r[1] for r in conn.execute(
                "PRAGMA table_info(supplier_candidates)"
            ).fetchall()
        }
        required = set(cols_to_add.keys())
        if required <= existing:
            conn.execute("PRAGMA user_version = 71")
        conn.commit()

    def test_v71_adds_both_columns(self, tmp_path):
        """v70 DB に v71 を適用すると 2 列追加され user_version=71 になる。"""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        self._run_v71_block(conn)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(supplier_candidates)").fetchall()}
        assert "candidate_image_url" in cols
        assert "candidate_image_fetched_at" in cols
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 71
        conn.close()

    def test_v71_idempotent_double_apply(self, tmp_path):
        """v71 を 2 回適用してもデータが消えず user_version が変わらない。"""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        # データ挿入
        conn.execute(
            "INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, source_platform) "
            "VALUES ('stock1', 'eid001', 'https://example.com/1', 'mercari')"
        )
        conn.commit()

        # 1 回目
        self._run_v71_block(conn)
        # 2 回目 (user_version=71 なので条件 `schema_ver==70` が偽になり何もしない)
        self._run_v71_block(conn)

        cnt = conn.execute("SELECT COUNT(*) FROM supplier_candidates").fetchone()[0]
        assert cnt == 1, "データが消えてはいけない"
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 71
        conn.close()

    def test_v71_partial_migration_recovery(self, tmp_path):
        """部分 migration 状態 (candidate_image_url のみ存在) から再実行で
        candidate_image_fetched_at が追加される (Codex L2 対応)。"""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        # 手動で candidate_image_url だけ追加 (部分 migration 再現)
        conn.execute("ALTER TABLE supplier_candidates ADD COLUMN candidate_image_url TEXT")
        conn.commit()
        # user_version は 70 のまま → v71 ブロックが再実行される
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 70

        self._run_v71_block(conn)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(supplier_candidates)").fetchall()}
        assert "candidate_image_url" in cols
        assert "candidate_image_fetched_at" in cols
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 71
        conn.close()


# ---------------------------------------------------------------------------
# B-1' migration v71: 実 init_db() 経由テスト (Q2 必須 / code-reviewer H-1 対応)
# ---------------------------------------------------------------------------
# 上の TestMigrationV71 は v71 ブロックのコピー実装でロジック検証するもの。
# Q2 (db-migration-rules.md) は「本物の init_db() を 2 回連続実行してデータ保持」
# の自動テストを要求するため、v70 テスト (test_v70_migration_harvest_pattern.py)
# と同じ DB_PATH monkeypatch 流儀で実 init_db を通す。

@pytest.fixture
def tmp_real_db(tmp_path, monkeypatch):
    """tests/ 専用の tmp DB に monkeypatch して本物の init_db を実行する。"""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


class TestMigrationV71RealInitDb:
    """本物の init_db() を経由した v71 検証 (コピー実装ではなく本番コードを実行)。"""

    def test_v71_columns_exist_via_real_init_db(self, tmp_real_db):
        """fresh DB → 実 init_db で v71 の 2 列が存在し user_version >= 71。"""
        from monitor.database import get_conn

        with get_conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(supplier_candidates)").fetchall()}
            ver = c.execute("PRAGMA user_version").fetchone()[0]

        assert "candidate_image_url" in cols, f"v71 列なし。cols={cols}"
        assert "candidate_image_fetched_at" in cols, f"v71 列なし。cols={cols}"
        assert ver >= 71, f"user_version={ver} (期待 >= 71)"

    def test_v71_real_init_db_twice_keeps_data(self, tmp_real_db):
        """実 init_db を 2 回連続実行してもデータ保持 (Q2 冪等性必須テスト)。"""
        import monitor.database as db_mod
        from monitor.database import get_conn

        with get_conn() as c:
            c.execute(
                "INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, source_platform) "
                "VALUES ('stock1', 'eid001', 'https://example.com/1', 'mercari')"
            )

        db_mod.init_db()  # 2 回目

        with get_conn() as c:
            cnt = c.execute("SELECT COUNT(*) FROM supplier_candidates").fetchone()[0]
            ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert cnt == 1, "init_db 再実行でデータが消えた (冪等性違反)"
        assert ver >= 71

    def test_v71_add_supplier_candidate_writes_image_url(self, tmp_real_db):
        """add_supplier_candidate が candidate_image_url + fetched_at を書き込む (実 DB)。"""
        from monitor.database import add_supplier_candidate, get_conn

        row_id = add_supplier_candidate(
            sku="ebayme_m999",
            candidate_url="https://jp.mercari.com/item/m999",
            source_platform="mercari",
            ebay_item_id="999999999999",
            candidate_image_url="https://static.mercdn.net/item/detail/orig/photos/m999_1.jpg",
        )
        assert row_id is not None

        with get_conn() as c:
            r = c.execute(
                "SELECT candidate_image_url, candidate_image_fetched_at "
                "FROM supplier_candidates WHERE id=?", (row_id,)
            ).fetchone()
        assert r[0] == "https://static.mercdn.net/item/detail/orig/photos/m999_1.jpg"
        assert r[1] is not None, "candidate_image_fetched_at が自動セットされていない"


# ---------------------------------------------------------------------------
# B-5 _supplier_card_html: imgpair 3 ケース
# ---------------------------------------------------------------------------

class TestSupplierCardHtmlImgpair:
    """render_supplier_card_html の imgpair ブロックを 3 ケースで検証する。"""

    def _make_row(self) -> dict:
        return {
            "id": 1,
            "match_score": 75,
            "source_platform": "mercari",
            "candidate_price_jpy": 10000,
            "candidate_title": "テスト商品",
            "candidate_url": "https://jp.mercari.com/item/m123",
            "status": "pending",
            "profitable": 1,
            "alt_listing_possible": 0,
            "junk_likely_untested": 0,
            "match_reasoning": "良い一致",
            "alt_listing_note": "",
            "ebay_item_id": "123456789012",
            "sku": "stock1",
            "eval_model": "claude-haiku-4-5",
            "profit_jpy": 3000.0,
            "candidate_image_url": None,
        }

    def test_both_images_present(self):
        """両画像あり: sc-imgpair ブロックが存在し、両 img タグが含まれる。"""
        from tabs._supplier_card_html import render_supplier_card_html
        row = self._make_row()
        html = render_supplier_card_html(
            row=row,
            ebay_price_usd=100.0,
            ebay_price_jpy=15000,
            profit_jpy=3000.0,
            parent_status="",
            ebay_image_url="https://i.ebayimg.com/images/g/abc/s-l1600.jpg",
            candidate_image_url="https://static.mercdn.net/item/detail/orig/photos/m123_1.jpg",
        )
        assert 'class="sc-imgpair"' in html
        assert 'i.ebayimg.com' in html
        assert 'static.mercdn.net' in html
        # img タグが 2 つ存在する
        assert html.count("<img ") == 2
        # プレースホルダなし
        assert "画像未取得" not in html

    def test_ebay_image_only(self):
        """eBay 画像あり、仕入先なし: 仕入先側にプレースホルダが出る。"""
        from tabs._supplier_card_html import render_supplier_card_html
        row = self._make_row()
        html = render_supplier_card_html(
            row=row,
            ebay_price_usd=100.0,
            ebay_price_jpy=15000,
            profit_jpy=3000.0,
            parent_status="",
            ebay_image_url="https://i.ebayimg.com/images/g/abc/s-l1600.jpg",
            candidate_image_url=None,
        )
        assert 'class="sc-imgpair"' in html
        assert 'i.ebayimg.com' in html
        assert "画像未取得" in html
        # img タグは eBay 側の 1 つのみ
        assert html.count("<img ") == 1

    def test_candidate_image_only(self):
        """仕入先画像あり、eBay なし: eBay 側にプレースホルダが出る。"""
        from tabs._supplier_card_html import render_supplier_card_html
        row = self._make_row()
        html = render_supplier_card_html(
            row=row,
            ebay_price_usd=None,
            ebay_price_jpy=None,
            profit_jpy=3000.0,
            parent_status="",
            ebay_image_url=None,
            candidate_image_url="https://static.mercdn.net/item/detail/orig/photos/m123_1.jpg",
        )
        assert 'class="sc-imgpair"' in html
        assert 'static.mercdn.net' in html
        assert "画像未取得" in html
        assert html.count("<img ") == 1

    def test_both_images_absent(self):
        """両方 None: sc-imgpair ブロック自体が出ない。"""
        from tabs._supplier_card_html import render_supplier_card_html
        row = self._make_row()
        html = render_supplier_card_html(
            row=row,
            ebay_price_usd=100.0,
            ebay_price_jpy=15000,
            profit_jpy=3000.0,
            parent_status="",
            ebay_image_url=None,
            candidate_image_url=None,
        )
        assert 'class="sc-imgpair"' not in html
        assert "画像未取得" not in html

    def test_url_escaped_in_html(self):
        """XSS: URL に特殊文字が含まれる場合に HTML エスケープされる。"""
        from tabs._supplier_card_html import render_supplier_card_html
        row = self._make_row()
        # 意図的に特殊文字を含む URL (実際には存在しないが XSS 検証用)
        evil_url = "https://i.ebayimg.com/img?a=1&b=<script>"
        html = render_supplier_card_html(
            row=row,
            ebay_price_usd=100.0,
            ebay_price_jpy=15000,
            profit_jpy=3000.0,
            parent_status="",
            ebay_image_url=evil_url,
            candidate_image_url=None,
        )
        # <script> が生で出てはいけない
        assert "<script>" not in html
        # エスケープ済みの & が含まれる
        assert "&amp;" in html or "amp;" in html


# ---------------------------------------------------------------------------
# B-3/B-4 backfill: dry-run で DB 書込ゼロ
# ---------------------------------------------------------------------------

class TestBackfillDryRun:
    """backfill 2 本の dry-run モード (--apply なし) で DB 書込がゼロなことを確認。"""

    def _load_script(self, script_name: str):
        """scripts/ 配下のスクリプトをモジュールとしてロードして返す。"""
        script_path = _ROOT / "scripts" / script_name
        spec = importlib.util.spec_from_file_location("_backfill_mod", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_candidate_images_dryrun(self, tmp_path, monkeypatch):
        """backfill_candidate_images: dry-run で DB 書込なし。"""
        # get_conn を tmp DB に差し替え
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        # v71 列追加 (migration 済み想定)
        try:
            conn.execute("ALTER TABLE supplier_candidates ADD COLUMN candidate_image_url TEXT")
            conn.execute("ALTER TABLE supplier_candidates ADD COLUMN candidate_image_fetched_at TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "INSERT INTO supplier_candidates (sku, ebay_item_id, candidate_url, source_platform, status) "
            "VALUES ('stock1', 'eid001', 'https://jp.mercari.com/item/m123', 'mercari', 'pending')"
        )
        conn.commit()
        conn.close()

        import contextlib
        import sqlite3 as _sqlite3

        def _mock_get_conn():
            class _CM:
                def __enter__(self):
                    c = _sqlite3.connect(db_path)
                    c.row_factory = _sqlite3.Row
                    self._conn = c
                    return c
                def __exit__(self, *a):
                    self._conn.commit()
                    self._conn.close()
            return _CM()

        mod = self._load_script("backfill_candidate_images_2026_06_11.py")
        monkeypatch.setattr(mod, "get_conn", _mock_get_conn)
        # snapshot 書込先を tmp に向ける (本番 data/ を汚染しない)
        (tmp_path / "data").mkdir(exist_ok=True)
        monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)
        # dry-run (apply=False) なので書込なし
        with mock.patch("sys.argv", ["backfill"]):
            mod.main()

        # DB が書き換わっていないことを確認
        check_conn = _sqlite3.connect(db_path)
        row = check_conn.execute(
            "SELECT candidate_image_url FROM supplier_candidates WHERE ebay_item_id='eid001'"
        ).fetchone()
        check_conn.close()
        assert row[0] is None, "dry-run では candidate_image_url が NULL のまま"

    def test_ebay_images_dryrun(self, tmp_path, monkeypatch):
        """backfill_ebay_images: dry-run で DB 書込なし。"""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.execute(
            "INSERT INTO ebay_listings (ebay_item_id, title, is_ended) "
            "VALUES ('eid001', 'テスト', 0)"
        )
        conn.commit()
        conn.close()

        import sqlite3 as _sqlite3

        def _mock_get_conn():
            class _CM:
                def __enter__(self):
                    c = _sqlite3.connect(db_path)
                    c.row_factory = _sqlite3.Row
                    self._conn = c
                    return c
                def __exit__(self, *a):
                    self._conn.commit()
                    self._conn.close()
            return _CM()

        mod = self._load_script("backfill_ebay_images_2026_06_11.py")
        monkeypatch.setattr(mod, "get_conn", _mock_get_conn)
        # snapshot/state 書込先を tmp に向ける (本番 data/ を汚染しない)
        (tmp_path / "data").mkdir(exist_ok=True)
        monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(mod, "_STATE_PATH", tmp_path / "data" / "state.json")
        # get_ebay_image_url は呼ばれないはずだが念のためモック
        monkeypatch.setattr(mod, "get_ebay_image_url", lambda eid: "https://i.ebayimg.com/img/1.jpg")

        with mock.patch("sys.argv", ["backfill"]):
            mod.main()

        # dry-run なので ebay_image_url は None のまま
        check_conn = _sqlite3.connect(db_path)
        row = check_conn.execute(
            "SELECT ebay_image_url FROM ebay_listings WHERE ebay_item_id='eid001'"
        ).fetchone()
        check_conn.close()
        assert row[0] is None, "dry-run では ebay_image_url が NULL のまま"
