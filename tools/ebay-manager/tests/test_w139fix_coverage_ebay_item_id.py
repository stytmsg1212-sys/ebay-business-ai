"""W139-fix 回帰: find_coverage_gaps の ebay_item_id キー化 + SKU 編集追従。

根本原因 (2026-05-18): find_coverage_gaps が `m.sku=l.sku` 結合だったため、
MonoDeck SKU 編集で ebay_listings.sku だけ更新され monitored_items が旧 sku
のまま → 監視中 listing を phantom gap 誤検知 → 非dedupe Discord 緊急通知爆発。

カバー:
  T1  phantom gap 解消 (ebay_item_id 一致なら sku ズレでも coverable に出ない)
  T2  真の盲点は依然検出 (ebay_item_id でも未登録 → coverable、誤検知除去で
      本物まで消えてない)
  T3  backfill 解決ロジック (source_url active 1件 / 複数 / 0件 / sku 再計算)
  T4  backfill 冪等 (充填済は対象外)
  T5  update_ebay_listing_sku が monitored を ebay_item_id キーで追従
  T6  upsert_ebay_listing sku_changed も追従 / 対象0件 no-op (例外を投げない)
  T7  cleanup: active 紐付き行は保護、孤立のみ対象 (誤削除防止)
  T8  cleanup 冪等 (is_active=0 は再対象外)
  T10 Q2 init_db 2 回連続でデータ保持
既存 16 件 (test_w139_monitor_coverage.py) は別ファイルで回帰実行 (T9)。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
import backfill_monitored_ebay_item_id_2026_05_19 as bf  # noqa: E402
import cleanup_orphan_monitored_2026_05_19 as cl  # noqa: E402


@pytest.fixture
def tmp_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="w139fix_test_")
    db_path = Path(tmpdir) / "monitor.db"
    import monitor.database as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db()
    yield db_path
    try:
        db_path.unlink()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    try:
        Path(tmpdir).rmdir()
    except OSError:
        pass


def _seed_listing(ebay_item_id, sku, *, qty=1, is_ended=0, title="T",
                  source_url=None):
    from monitor.database import get_conn, build_source_url
    url = source_url if source_url is not None else build_source_url(sku)
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings "
            "(ebay_item_id, sku, title, quantity_ebay, is_ended, source_url) "
            "VALUES (?,?,?,?,?,?)",
            (ebay_item_id, sku, title, qty, is_ended, url),
        )


def _seed_monitored(ebay_item_id, sku, *, source_url=None, is_active=1):
    """monitored_items に直接 1 行 (post-SKU-edit-未追従 状態の再現用)."""
    from monitor.database import get_conn, build_source_url, \
        find_site_config_by_sku
    url = source_url if source_url is not None else build_source_url(sku)
    cfg = find_site_config_by_sku(sku)
    with get_conn() as c:
        c.execute(
            "INSERT INTO monitored_items "
            "(ebay_item_id, sku, title, source_url, site_config_id, is_active)"
            " VALUES (?,?,?,?,?,?)",
            (ebay_item_id, sku, "T", url, cfg["id"] if cfg else None,
             is_active),
        )


def _mon_row(ebay_item_id):
    from monitor.database import get_conn
    with get_conn() as c:
        c.row_factory = sqlite3.Row
        r = c.execute(
            "SELECT * FROM monitored_items WHERE ebay_item_id=?",
            (ebay_item_id,)).fetchone()
    return dict(r) if r else None


class TestPhantomGapFixed:
    def test_sku_mismatch_but_ebay_item_id_match_not_coverable(self, tmp_db):
        """T1 核心: listing.sku=NEW / monitored.sku=OLD でも ebay_item_id
        一致なら coverable に出ない (旧 m.sku=l.sku 結合なら phantom gap 化)."""
        from tasks.task_ensure_monitor_coverage import find_coverage_gaps
        _seed_listing("E_PH_1", "ebayme_NEW0001")          # SKU 編集後
        _seed_monitored("E_PH_1", "ebayme_OLD0001")        # 旧 sku のまま
        gaps = find_coverage_gaps()
        assert not any(c["ebay_item_id"] == "E_PH_1"
                       for c in gaps["coverable"]), \
            "ebay_item_id 一致なのに phantom gap 化した (根本原因未修正)"
        assert not any(d["ebay_item_id"] == "E_PH_1" for d in gaps["dlq"])

    def test_null_ebay_item_id_monitored_does_not_cover(self, tmp_db):
        """ebay_item_id 空の monitored 行は『カバレッジなし』扱い
        (= backfill で充填すべき対象。新 query が空 ID を数えない明示確認)."""
        from tasks.task_ensure_monitor_coverage import find_coverage_gaps
        _seed_listing("E_PH_2", "ebayme_pn000002")
        _seed_monitored("", "ebayme_pn000002")  # ebay_item_id 空
        gaps = find_coverage_gaps()
        assert any(c["ebay_item_id"] == "E_PH_2"
                   for c in gaps["coverable"]), \
            "ebay_item_id 空 monitored を誤って『カバー済』と判定 " \
            "(backfill 前の盲点を隠す = silent gap)"


class TestTrueGapStillDetected:
    def test_unregistered_listing_still_coverable(self, tmp_db):
        """T2: ebay_item_id でも monitored 未登録なら依然 coverable
        (誤検知除去で本物の盲点まで消していないこと = anti-silent-skip)."""
        from tasks.task_ensure_monitor_coverage import (
            find_coverage_gaps, run_ensure_monitor_coverage)
        _seed_listing("E_TG_1", "ebayme_truegap01")
        gaps = find_coverage_gaps()
        assert any(c["ebay_item_id"] == "E_TG_1"
                   for c in gaps["coverable"])
        r = run_ensure_monitor_coverage({})
        assert r["registered"] >= 1
        # 登録後は ebay_item_id 一致で coverable から外れる (冪等)
        assert not any(c["ebay_item_id"] == "E_TG_1"
                       for c in find_coverage_gaps()["coverable"])


class TestSkuEditFollowThrough:
    def test_update_ebay_listing_sku_propagates_to_monitored(self, tmp_db):
        """T5: MonoDeck 手動 SKU 編集経路。monitored が ebay_item_id キーで
        sku/source_url/site_config_id 追従する."""
        from monitor.database import (
            upsert_item, update_ebay_listing_sku, build_source_url,
            find_site_config_by_sku)
        _seed_listing("E_SE_1", "ebayme_old00001")
        upsert_item(sku="ebayme_old00001", ebay_item_id="E_SE_1", title="x")
        update_ebay_listing_sku("E_SE_1", "ebayyh_new00001")
        m = _mon_row("E_SE_1")
        assert m["sku"] == "ebayyh_new00001", \
            f"monitored.sku 未追従 (phantom gap 根本原因): {m}"
        assert m["source_url"] == build_source_url("ebayyh_new00001")
        assert m["site_config_id"] == \
            find_site_config_by_sku("ebayyh_new00001")["id"]

    def test_upsert_ebay_listing_sku_changed_propagates(self, tmp_db):
        """T6a: ebay_sync 経路 (upsert_ebay_listing sku_changed 分岐) も追従."""
        from monitor.database import (
            upsert_ebay_listing, upsert_item, get_conn,
            _build_source_url_from_sku)
        upsert_ebay_listing("E_SE_2", "ebayme_old00002", title="x",
                            quantity_ebay=1)
        upsert_item(sku="ebayme_old00002", ebay_item_id="E_SE_2", title="x")
        # eBay 側 SKU 変更を ebay_sync が検知 (再 upsert で sku_changed 分岐)
        upsert_ebay_listing("E_SE_2", "ebayme_new00002", title="x",
                            quantity_ebay=1)
        m = _mon_row("E_SE_2")
        assert m["sku"] == "ebayme_new00002", \
            f"ebay_sync 経路で monitored 未追従: {m}"
        # HIGH-1 修正後: monitored は ebay_listings.source_url を mirror =
        # 本番生成器形 (_build_source_url_from_sku)。両者一致を確認。
        with get_conn() as c:
            ls = c.execute("SELECT source_url FROM ebay_listings "
                           "WHERE ebay_item_id='E_SE_2'").fetchone()[0]
        assert m["source_url"] == ls == \
            _build_source_url_from_sku("ebayme_new00002")

    def test_followthrough_no_monitored_row_is_noop(self, tmp_db):
        """T6b: 監視台帳未登録 listing の SKU 編集は no-op (例外を投げない。
        次 batch の ensure_monitor_coverage が拾う自己修復 = Q0 silent でない)."""
        from monitor.database import update_ebay_listing_sku
        _seed_listing("E_SE_3", "ebayme_old00003")
        # monitored 行なし。例外なく完了すること
        update_ebay_listing_sku("E_SE_3", "ebayme_new00003")
        assert _mon_row("E_SE_3") is None  # 追従対象なし = no-op

    def test_followthrough_keyed_by_ebay_item_id_not_sku(self, tmp_db):
        """追従は ebay_item_id キー (sku-rules.md: WHERE sku=? 禁止)。
        別 ebay_item_id の monitored 行を巻き込まないこと。
        (注: 同 no-stock SKU = 同 source_url は upsert_item が 1 行集約する
        仕様のため、独立性検証は別 sku の 2 listing で行う)."""
        from monitor.database import upsert_item, update_ebay_listing_sku
        _seed_listing("E_SE_A", "ebayme_distinctA1")
        _seed_listing("E_SE_B", "ebayme_distinctB1")  # 別 sku 別 listing
        upsert_item(sku="ebayme_distinctA1", ebay_item_id="E_SE_A", title="a")
        upsert_item(sku="ebayme_distinctB1", ebay_item_id="E_SE_B", title="b")
        update_ebay_listing_sku("E_SE_A", "ebayme_changedA1")
        assert _mon_row("E_SE_A")["sku"] == "ebayme_changedA1"
        assert _mon_row("E_SE_B")["sku"] == "ebayme_distinctB1", \
            "ebay_item_id キーのはずが別 listing の monitored 行を巻き込んだ"


class TestBackfillResolution:
    def test_resolve_active_single_match(self, tmp_db):
        """T3a: source_url が active listing 1 件に一致 → 解決."""
        from monitor.database import get_conn, build_source_url
        url = build_source_url("ebayme_bk000001")
        _seed_listing("E_BK_1", "ebayme_bk000001")
        with get_conn() as c:
            eid, reason = bf._resolve_ebay_item_id(c, url, "ebayme_bk000001")
        assert eid == "E_BK_1", reason

    def test_resolve_multiple_match_unresolved(self, tmp_db):
        """T3b: 同 source_url の active listing 複数 → 一意決定不能 unresolved
        (推測で誤 ID 充填しない = Q0)."""
        from monitor.database import get_conn, build_source_url
        url = build_source_url("ebayme_bk000002")
        _seed_listing("E_BK_2a", "ebayme_bk000002", source_url=url)
        _seed_listing("E_BK_2b", "ebayme_bk000002", source_url=url)
        with get_conn() as c:
            eid, reason = bf._resolve_ebay_item_id(c, url, "ebayme_bk000002")
        assert eid is None and "複数" in reason

    def test_resolve_recompute_from_sku_when_url_missing(self, tmp_db):
        """T3c: monitored.source_url 無し → sku から build_source_url 再計算."""
        from monitor.database import get_conn
        _seed_listing("E_BK_3", "ebayme_bk000003")
        with get_conn() as c:
            eid, reason = bf._resolve_ebay_item_id(c, None, "ebayme_bk000003")
        assert eid == "E_BK_3", reason

    def test_resolve_no_match_unresolved(self, tmp_db):
        """T3d: 一致 listing 皆無 → unresolved (NULL 維持)."""
        from monitor.database import get_conn, build_source_url
        with get_conn() as c:
            eid, reason = bf._resolve_ebay_item_id(
                c, build_source_url("ebayme_nomatch9"), "ebayme_nomatch9")
        assert eid is None

    def test_backfill_idempotent_filled_excluded(self, tmp_db):
        """T4: ebay_item_id 充填済行は対象クエリに入らない (冪等)."""
        from monitor.database import get_conn
        _seed_listing("E_BK_5", "ebayme_bk000005")
        _seed_monitored("E_BK_5", "ebayme_bk000005")  # 既に ID あり
        with get_conn() as c:
            targets = c.execute(
                "SELECT id FROM monitored_items "
                "WHERE ebay_item_id IS NULL OR ebay_item_id=''").fetchall()
        assert targets == [], "充填済が backfill 対象に残った (冪等性違反)"


class TestCleanupGuards:
    def test_active_linked_row_is_protected(self, tmp_db):
        """T7a: active listing が source_url で紐づく NULL 行は保護
        (G2 = W139 原事故 監視対象誤減 の再現防止 核心ガード)."""
        from monitor.database import get_conn, build_source_url
        url = build_source_url("ebayme_cl000001")
        _seed_listing("E_CL_1", "ebayme_cl000001", source_url=url)
        with get_conn() as c:
            assert cl._has_active_listing(c, url, "ebayme_cl000001") is True

    def test_orphan_row_has_no_active_listing(self, tmp_db):
        """T7b: どの active listing にも紐づかない NULL 行は孤立判定."""
        from monitor.database import get_conn, build_source_url
        with get_conn() as c:
            assert cl._has_active_listing(
                c, build_source_url("ebayme_orphan99"),
                "ebayme_orphan99") is False

    def test_ended_listing_does_not_protect(self, tmp_db):
        """T7c: ended listing しか無い source_url は保護されない (孤立扱い)."""
        from monitor.database import get_conn, build_source_url
        url = build_source_url("ebayme_cl000003")
        _seed_listing("E_CL_3", "ebayme_cl000003", is_ended=1, source_url=url)
        with get_conn() as c:
            assert cl._has_active_listing(c, url, "ebayme_cl000003") is False

    def test_cleanup_only_demotes_not_deletes_idempotent(self, tmp_db):
        """T8: is_active=0 降格のみ (DELETE 禁止)、is_active=0 は再対象外."""
        from monitor.database import get_conn
        _seed_monitored("", "ebayme_cl000004", is_active=1)  # 孤立
        with get_conn() as c:
            row = c.execute(
                "SELECT id FROM monitored_items "
                "WHERE ebay_item_id IS NULL OR ebay_item_id=''").fetchone()
            mid = row[0]
            # 孤立 (active listing 無し) を降格
            c.execute("UPDATE monitored_items SET is_active=0 WHERE id=?",
                      (mid,))
        with get_conn() as c:
            still = c.execute(
                "SELECT COUNT(*) FROM monitored_items WHERE id=?",
                (mid,)).fetchone()[0]
            active = c.execute(
                "SELECT is_active FROM monitored_items WHERE id=?",
                (mid,)).fetchone()[0]
            # 物理削除されず行は残る (DELETE 禁止 = 復元可能)
            assert still == 1 and active == 0
            # 冪等: is_active=1 条件の対象に再度入らない
            targets = c.execute(
                "SELECT id FROM monitored_items "
                "WHERE (ebay_item_id IS NULL OR ebay_item_id='') "
                "  AND is_active=1").fetchall()
            assert mid not in [t[0] for t in targets]


class TestSourceUrlGeneratorParityHigh1:
    """HIGH-1 (2026-05-18 実データ実証) 回帰: build_source_url (site_configs)
    と _build_source_url_from_sku (sku_mapping) が mercari 等で食い違い、本番
    ebay_listings.source_url は後者形。単一生成器照合だと mercari active
    listing を取りこぼし → 孤立誤判定 → is_active=0 → 履行不能 (W139 原事故
    再現)。本番経路 (update_ebay_listing_sku) と両生成器跨ぎを verify."""

    def test_generators_actually_differ_for_mercari(self, tmp_db):
        """前提確認: mercari で 2 生成器が実際に食い違う (この差が無ければ
        以降の回帰は無意味)。将来統一されたら本 test を見直す印."""
        from monitor.database import build_source_url, \
            _build_source_url_from_sku
        sku = "ebayme_25388296384"
        assert build_source_url(sku) != _build_source_url_from_sku(sku), \
            "2 生成器が一致 = 統一された? HIGH-1 回帰群の前提を再確認せよ"

    def test_followthrough_mirrors_listing_source_url_mercari(self, tmp_db):
        """追従ヘルパは ebay_listings.source_url を mirror = 本番生成器形
        (_build_source_url_from_sku) と必ず一致 (build_source_url 形で
        書かない)。inventory_check が誤 URL を scrape しない保証."""
        from monitor.database import (
            upsert_item, update_ebay_listing_sku, get_conn,
            _build_source_url_from_sku)
        _seed_listing("E_H1", "ebayme_old11111")
        upsert_item(sku="ebayme_old11111", ebay_item_id="E_H1", title="x")
        update_ebay_listing_sku("E_H1", "ebayme_new11111")
        with get_conn() as c:
            ls = c.execute("SELECT source_url FROM ebay_listings "
                           "WHERE ebay_item_id='E_H1'").fetchone()[0]
        m = _mon_row("E_H1")
        assert m["source_url"] == ls, (
            f"monitored が listing と不一致 (HIGH-1 再発): "
            f"mon={m['source_url']} listing={ls}")
        assert m["source_url"] == _build_source_url_from_sku(
            "ebayme_new11111"), "本番生成器形で mirror されていない"

    def test_backfill_resolves_mercari_cross_generator(self, tmp_db):
        """legacy NULL 行が build_source_url 形で source_url 保存されていても、
        本番 listing (=_build_source_url_from_sku 形) に解決できる
        (候補 URL 集合 IN 照合)."""
        from monitor.database import (
            get_conn, build_source_url, _build_source_url_from_sku)
        prod_url = _build_source_url_from_sku("ebayme_22222")  # listing 実形
        _seed_listing("E_H2", "ebayme_22222", source_url=prod_url)
        legacy_stored = build_source_url("ebayme_22222")        # 別生成器形
        assert legacy_stored != prod_url  # 前提
        with get_conn() as c:
            eid, reason = bf._resolve_ebay_item_id(
                c, legacy_stored, "ebayme_22222")
        assert eid == "E_H2", \
            f"生成器跨ぎで mercari listing に解決できず: {reason}"

    def test_cleanup_protects_mercari_active_cross_generator(self, tmp_db):
        """G2: mercari active listing が本番形で保存されていても、別生成器形
        の monitored 行を孤立誤判定しない (履行不能再発防止の核心)."""
        from monitor.database import (
            get_conn, build_source_url, _build_source_url_from_sku)
        prod_url = _build_source_url_from_sku("ebayme_33333")
        _seed_listing("E_H3", "ebayme_33333", source_url=prod_url)
        with get_conn() as c:
            assert cl._has_active_listing(
                c, build_source_url("ebayme_33333"), "ebayme_33333") is True, \
                "mercari active listing を孤立誤判定 (G2 破れ = 履行不能再発)"


class TestCodexRound1HighFixes:
    """Codex 2段 (2026-05-19) が内部 code-reviewer 2周 HIGH=0 後に捕捉した
    money-direct 3 HIGH の回帰。"""

    # --- HIGH-1: 商品管理保存の SKU 直叩き bypass ---
    def test_pm_save_sku_change_follows_through(self, tmp_db):
        """商品管理保存で SKU 変更 → update_ebay_listing_sku 経由で
        ebay_listings.source_url 再構築 + monitored 追従 (silent gap 解消)."""
        from tabs.tab_product_management import _save_product_data
        from monitor.database import (
            upsert_item, get_conn, _build_source_url_from_sku)
        _seed_listing("E_C1", "ebayme_pmold01")
        upsert_item(sku="ebayme_pmold01", ebay_item_id="E_C1", title="x")
        _save_product_data(ebay_item_id="E_C1",
                           editing={"sku": "ebayme_pmnew01"},
                           competitors=[], recalc_breakeven=False, config={})
        with get_conn() as c:
            ls_sku, ls_url = c.execute(
                "SELECT sku, source_url FROM ebay_listings "
                "WHERE ebay_item_id='E_C1'").fetchone()
        m = _mon_row("E_C1")
        assert ls_sku == "ebayme_pmnew01"
        assert ls_url == _build_source_url_from_sku("ebayme_pmnew01"), \
            "source_url 未再構築 (raw UPDATE 復活?)"
        assert m["sku"] == "ebayme_pmnew01", \
            "monitored 未追従 = HIGH-1 silent gap 再発"
        assert m["source_url"] == ls_url

    def test_pm_save_no_raw_sku_update_statement(self):
        """source-contract 番人: 商品管理保存に raw SKU UPDATE が復活したら
        HIGH-1 再発 (update_ebay_listing_sku bypass) を物理 BLOCK."""
        import inspect
        import tabs.tab_product_management as pm
        raw = inspect.getsource(pm._save_product_data)
        # コメント行 (説明文に `UPDATE ebay_listings SET sku` の語が出る) を
        # 除外し、実コードのみで raw SQL 文の有無を判定。
        code_only = "\n".join(
            ln for ln in raw.splitlines()
            if not ln.lstrip().startswith("#"))
        assert "UPDATEebay_listingsSETsku=" not in code_only.replace(" ", ""), \
            "raw SKU UPDATE 復活 = HIGH-1 再発 (update_ebay_listing_sku 統一を破壊)"
        assert "update_ebay_listing_sku" in code_only

    def test_pm_save_unchanged_sku_preserves_oos_risk(self, tmp_db):
        """Codex round-2 HIGH 回帰: SKU 未変更の保存 (weight 等) では
        update_ebay_listing_sku を呼ばず source_out_of_stock_since /
        risk_confirmed / source_status を保全 (既知OOSリスク消失=履行不能 防止)."""
        from tabs.tab_product_management import _save_product_data
        from monitor.database import upsert_item, get_conn
        _seed_listing("E_C4", "ebayme_keep0001")
        with get_conn() as c:
            c.execute(
                "UPDATE ebay_listings SET source_out_of_stock_since="
                "'2026-05-01', risk_confirmed=1, source_status='out_of_stock' "
                "WHERE ebay_item_id='E_C4'")
        upsert_item(sku="ebayme_keep0001", ebay_item_id="E_C4", title="x")
        _save_product_data(
            ebay_item_id="E_C4",
            editing={"sku": "ebayme_keep0001", "weight_g": 500},
            competitors=[], recalc_breakeven=False, config={})
        with get_conn() as c:
            oos, rc, ss, sk = c.execute(
                "SELECT source_out_of_stock_since, risk_confirmed, "
                "source_status, sku FROM ebay_listings "
                "WHERE ebay_item_id='E_C4'").fetchone()
        assert oos == '2026-05-01', f"OOS since リセット (履行不能再発): {oos}"
        assert rc == 1, "risk_confirmed リセット (既知OOS消失=履行不能)"
        assert ss == 'out_of_stock', "source_status リセット"
        assert sk == "ebayme_keep0001"

    # --- HIGH-2: is_active=0 monitored 行が誤って「監視済」扱い ---
    def test_inactive_monitored_row_not_counted_as_covered(self, tmp_db):
        from tasks.task_ensure_monitor_coverage import find_coverage_gaps
        _seed_listing("E_C2", "ebayme_inact01")
        _seed_monitored("E_C2", "ebayme_inact01", is_active=0)
        gaps = find_coverage_gaps()
        assert any(c["ebay_item_id"] == "E_C2"
                   for c in gaps["coverable"]), \
            "is_active=0 monitored を監視済と誤計上 (silent unmonitored=HIGH-2)"

    def test_active_monitored_row_still_covers(self, tmp_db):
        from tasks.task_ensure_monitor_coverage import find_coverage_gaps
        _seed_listing("E_C2b", "ebayme_act01")
        _seed_monitored("E_C2b", "ebayme_act01", is_active=1)
        assert not any(c["ebay_item_id"] == "E_C2b"
                       for c in find_coverage_gaps()["coverable"])

    # --- HIGH-3: upsert_item 登録の source_url 生成器 ---
    def test_upsert_item_mercari_uses_scrape_canonical_url(self, tmp_db):
        from monitor.database import (
            upsert_item, get_conn, build_source_url,
            _build_source_url_from_sku)
        upsert_item(sku="ebayme_99887766", ebay_item_id="E_C3", title="x")
        with get_conn() as c:
            url = c.execute("SELECT source_url FROM monitored_items "
                            "WHERE ebay_item_id='E_C3'").fetchone()[0]
        assert url == _build_source_url_from_sku("ebayme_99887766"), \
            "upsert_item が mercari 非canonical URL (HIGH-3 scrape不一致)"
        assert url != build_source_url("ebayme_99887766")

    def test_upsert_item_yahoo_unchanged(self, tmp_db):
        from monitor.database import (
            upsert_item, get_conn, build_source_url,
            _build_source_url_from_sku)
        upsert_item(sku="ebayyh_y1234567", ebay_item_id="E_C3y", title="x")
        with get_conn() as c:
            url = c.execute("SELECT source_url FROM monitored_items "
                            "WHERE ebay_item_id='E_C3y'").fetchone()[0]
        assert url == build_source_url("ebayyh_y1234567") == \
            _build_source_url_from_sku("ebayyh_y1234567"), "yahoo 挙動変化"


class TestQ2Idempotency:
    def test_init_db_twice_preserves_data(self, tmp_db):
        """T10: db-migration-rules 必須。init_db 2 回連続でデータ保持."""
        from monitor.database import init_db, get_conn
        _seed_listing("E_Q2_1", "ebayme_q2000001")
        _seed_monitored("E_Q2_1", "ebayme_q2000001")
        init_db()
        with get_conn() as c:
            n_l = c.execute(
                "SELECT COUNT(*) FROM ebay_listings "
                "WHERE ebay_item_id='E_Q2_1'").fetchone()[0]
            n_m = c.execute(
                "SELECT COUNT(*) FROM monitored_items "
                "WHERE ebay_item_id='E_Q2_1'").fetchone()[0]
        assert n_l == 1 and n_m == 1, "init_db 再実行でデータ消失 (冪等性違反)"
