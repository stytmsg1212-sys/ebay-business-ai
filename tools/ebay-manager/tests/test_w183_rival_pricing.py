"""W183 ライバル価格対抗自動値下げの pytest 回帰 test.

code-reviewer (Opus 4.7) HIGH=0 ループの 1 周回目で H1 (test 0 件) を指摘されたので、
本ファイルで補完する.

カバー範囲:
- _compute_target_price (H2 round 罠 / H3 min StartPrice / unsupported rule)
- _decide_floor_price (NULL / 0 / 負数の境界)
- _evaluate_and_apply_one (skip 7 分岐 + happy_path) [DB fixture を作って実 SQLite 経由]
- _count_today_changes_jst (JST 境界 23:59 / 00:01 の off-by-one)
- _build_revise_fixed_price_xml / revise_fixed_price_item (XML 妥当性 + 入力 reject)
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


# ────────────────────────────────────────
# fixture: 一時 DB
# ────────────────────────────────────────

@pytest.fixture
def tmp_db(monkeypatch):
    """tests 用の一時 SQLite DB を作成し、monitor.database.DB_PATH を差し替える.

    init_db() を呼んで最新スキーマ (v33 含む) を構築してから返す.
    """
    tmpdir = tempfile.mkdtemp(prefix="w183_test_")
    db_path = Path(tmpdir) / "monitor.db"

    import monitor.database as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)

    db_module.init_db()
    yield db_path

    # cleanup. Windows は SQLite file lock が残ることがあるので best-effort.
    try:
        db_path.unlink()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    try:
        Path(tmpdir).rmdir()
    except OSError:
        pass


def _seed_listing(ebay_item_id: str, *, current_price: float = 120.0,
                  shipping_cost: float = 10.0, lp_min_price=None,
                  lp_breakeven_usd=None, is_ended: int = 0):
    """ebay_listings に test 用 row を 1 件 INSERT."""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO ebay_listings "
            "(ebay_item_id, sku, title, current_price, shipping_cost, "
            " lp_min_price, lp_breakeven_usd, is_ended) "
            "VALUES (?, 'stock:TEST', 'Test', ?, ?, ?, ?, ?)",
            (ebay_item_id, current_price, shipping_cost,
             lp_min_price, lp_breakeven_usd, is_ended)
        )


def _seed_competitor(our_item_id: str, competitor_item_id: str,
                     price_usd=None, shipping_usd=None,
                     rule: str = 'competitor - 0.01'):
    """competitor_products に test 用 row を 1 件 INSERT."""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products "
            "(our_item_id, competitor_item_id, price_rule, min_price, "
            " max_discount, is_active, competitor_price_usd, "
            " competitor_shipping_usd) "
            "VALUES (?, ?, ?, 0.0, 10.0, 1, ?, ?)",
            (our_item_id, competitor_item_id, rule, price_usd, shipping_usd)
        )


# ────────────────────────────────────────
# _compute_target_price (H2 / H3 重点)
# ────────────────────────────────────────

class TestComputeTargetPrice:
    def test_basic_competition(self):
        from tasks.task_rival_pricing import _compute_target_price
        # competitor_total $10, shipping $2
        # target buyer total = 10 - 0.01 = 9.99
        # target StartPrice = 9.99 - 2 = 7.99
        assert _compute_target_price(10.0, 2.0, 'competitor - 0.01') == 7.99

    def test_zero_shipping(self):
        from tasks.task_rival_pricing import _compute_target_price
        # competitor $5, shipping $0 → start = 4.99
        assert _compute_target_price(5.0, 0.0, 'competitor - 0.01') == 4.99

    def test_h2_no_bankers_rounding_collision(self):
        """H2: shipping $2.005 (DB 浮動小数誤差) でも buyer total < competitor を保証.

        旧実装 (round + f-string :.2f) は target=7.99 → buyer total=$9.995
        → eBay UI 表示で $10.00 と等しくなる事故ケース.
        新実装 (math.floor / 整数 cents) では target=7.98 で必ず competitor 未満.
        """
        from tasks.task_rival_pricing import _compute_target_price
        target = _compute_target_price(10.0, 2.005, 'competitor - 0.01')
        assert target is not None
        # buyer total が必ず competitor (10.0) - 0.01 (= 9.99) 以下になる
        buyer_total = target + 2.005
        assert buyer_total <= 9.99 + 1e-9, (
            f"buyer_total {buyer_total} > competitor-0.01 9.99: H2 regression"
        )

    def test_h2_rounding_exact_cent_competitor(self):
        """整数セント境界 (10.00 USD) でも float 誤差で fail しない."""
        from tasks.task_rival_pricing import _compute_target_price
        target = _compute_target_price(10.00, 2.00, 'competitor - 0.01')
        # 期待: target = 10*100=1000, ship=200, buyer=999, start=799 → $7.99
        assert target == 7.99

    def test_h3_below_ebay_min_returns_none(self):
        """H3: target < $0.99 (eBay min StartPrice) なら None を返す."""
        from tasks.task_rival_pricing import _compute_target_price
        # competitor $0.50 のとき start = 0.49 < 0.99
        assert _compute_target_price(0.50, 0.0, 'competitor - 0.01') is None

    def test_h3_at_ebay_min_returns_value(self):
        """target = $0.99 ちょうどなら有効."""
        from tasks.task_rival_pricing import _compute_target_price
        # competitor $1.00 shipping $0 → start = 0.99
        assert _compute_target_price(1.00, 0.0, 'competitor - 0.01') == 0.99

    def test_unsupported_rule_returns_none(self):
        from tasks.task_rival_pricing import _compute_target_price
        assert _compute_target_price(10.0, 0.0, 'beat by 5%') is None
        assert _compute_target_price(10.0, 0.0, '') is None
        assert _compute_target_price(10.0, 0.0, None) is None

    def test_rule_normalization_case_and_space(self):
        from tasks.task_rival_pricing import _compute_target_price
        assert _compute_target_price(10.0, 0.0, 'COMPETITOR - 0.01') == 9.99
        assert _compute_target_price(10.0, 0.0, 'competitor-0.01') == 9.99
        assert _compute_target_price(10.0, 0.0, '  Competitor - 0.01  ') == 9.99


# ────────────────────────────────────────
# _decide_floor_price
# ────────────────────────────────────────

class TestDecideFloorPrice:
    def test_lp_min_priority(self):
        from tasks.task_rival_pricing import _decide_floor_price
        assert _decide_floor_price({'lp_min_price': 50.0, 'lp_breakeven_usd': 30.0}) == 50.0

    def test_lp_min_zero_falls_to_breakeven(self):
        from tasks.task_rival_pricing import _decide_floor_price
        # lp_min_price=0 は「未設定」扱い → lp_breakeven_usd へ fallback
        assert _decide_floor_price({'lp_min_price': 0.0, 'lp_breakeven_usd': 30.0}) == 30.0

    def test_lp_min_none_falls_to_breakeven(self):
        from tasks.task_rival_pricing import _decide_floor_price
        assert _decide_floor_price({'lp_min_price': None, 'lp_breakeven_usd': 30.0}) == 30.0

    def test_breakeven_zero_returns_none(self):
        from tasks.task_rival_pricing import _decide_floor_price
        assert _decide_floor_price({'lp_min_price': None, 'lp_breakeven_usd': 0.0}) is None

    def test_both_none_returns_none(self):
        from tasks.task_rival_pricing import _decide_floor_price
        assert _decide_floor_price({'lp_min_price': None, 'lp_breakeven_usd': None}) is None


# ────────────────────────────────────────
# _build_revise_fixed_price_xml + revise_fixed_price_item
# ────────────────────────────────────────

class TestReviseFixedPriceXml:
    def test_xml_currency_and_format(self):
        from monitor.ebay_client import _build_revise_fixed_price_xml
        xml = _build_revise_fixed_price_xml('357935772166', 99.99)
        assert '<ItemID>357935772166</ItemID>' in xml
        assert '<StartPrice currencyID="USD">99.99</StartPrice>' in xml

    def test_xml_two_decimal_format(self):
        from monitor.ebay_client import _build_revise_fixed_price_xml
        xml = _build_revise_fixed_price_xml('123', 9.5)
        assert '<StartPrice currencyID="USD">9.50</StartPrice>' in xml

    def test_xml_parses_as_valid_xml(self):
        import xml.etree.ElementTree as ET
        from monitor.ebay_client import _build_revise_fixed_price_xml
        xml = _build_revise_fixed_price_xml('123', 9.99)
        # 解析できれば構造健全
        root = ET.fromstring(xml)
        assert root.tag.endswith('ReviseFixedPriceItemRequest')

    def test_xml_special_char_escape(self):
        """ItemID は数字なので escape は通常不要だが、XML 安全性確認."""
        from monitor.ebay_client import _build_revise_fixed_price_xml
        # ItemID に 12 桁数字以外を渡しても XML として安全に escape されること
        xml = _build_revise_fixed_price_xml('a&b<c', 9.99)
        assert '<ItemID>a&amp;b&lt;c</ItemID>' in xml

    def test_revise_rejects_zero(self):
        from monitor.ebay_client import revise_fixed_price_item
        r = revise_fixed_price_item('123', 0.0, 'a', 'b', 'c', 'd')
        assert r['success'] is False
        assert 'invalid' in r['message']

    def test_revise_rejects_negative(self):
        from monitor.ebay_client import revise_fixed_price_item
        r = revise_fixed_price_item('123', -1.0, 'a', 'b', 'c', 'd')
        assert r['success'] is False

    def test_revise_rejects_none(self):
        from monitor.ebay_client import revise_fixed_price_item
        r = revise_fixed_price_item('123', None, 'a', 'b', 'c', 'd')
        assert r['success'] is False


# ────────────────────────────────────────
# _evaluate_and_apply_one (DB fixture 経由)
# ────────────────────────────────────────

class TestEvaluateAndApply:
    def test_skip_no_competitor_row(self, tmp_db):
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('TEST_W183_E1', lp_min_price=50.0)
        r = _evaluate_and_apply_one('TEST_W183_E1', {})
        assert r['action'] == 'skip_competitor_price_unknown'

    def test_skip_already_cheapest(self, tmp_db):
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('TEST_W183_E2', current_price=100.0, shipping_cost=10.0,
                      lp_min_price=50.0)  # our_total=$110
        _seed_competitor('TEST_W183_E2', 'C1', price_usd=120.0, shipping_usd=0.0)  # comp=$120
        r = _evaluate_and_apply_one('TEST_W183_E2', {})
        assert r['action'] == 'skip_already_cheapest'

    def test_skip_below_floor(self, tmp_db):
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('TEST_W183_E3', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=100.0)
        # competitor $80, shipping $10 → comp_total=$90 < our_total=$130
        # target = 80 + 10 - 0.01 - 10 = 79.99 < floor 100 → skip
        _seed_competitor('TEST_W183_E3', 'C1', price_usd=80.0, shipping_usd=10.0)
        r = _evaluate_and_apply_one('TEST_W183_E3', {})
        assert r['action'] == 'skip_below_floor'

    def test_skip_no_floor(self, tmp_db):
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('TEST_W183_E4')  # no lp_min_price, no lp_breakeven_usd
        _seed_competitor('TEST_W183_E4', 'C1', price_usd=80.0, shipping_usd=10.0)
        r = _evaluate_and_apply_one('TEST_W183_E4', {})
        assert r['action'] == 'skip_no_floor'

    def test_skip_listing_ended(self, tmp_db):
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('TEST_W183_E5', is_ended=1)
        _seed_competitor('TEST_W183_E5', 'C1', price_usd=80.0, shipping_usd=10.0)
        r = _evaluate_and_apply_one('TEST_W183_E5', {})
        assert r['action'] == 'skip_listing_ended'

    def test_skip_daily_cap(self, tmp_db):
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('TEST_W183_E6', lp_min_price=50.0)
        _seed_competitor('TEST_W183_E6', 'C1', price_usd=80.0, shipping_usd=10.0)
        # 4 件成功 row を seed (本日 JST)
        with get_conn() as c:
            for i in range(4):
                c.execute(
                    "INSERT INTO price_change_log "
                    "(ebay_item_id, old_price_usd, new_price_usd, "
                    " competitor_item_id, competitor_total_usd, rule_applied, "
                    " triggered_by, success) "
                    "VALUES (?, 100, ?, 'C1', 90, 'competitor - 0.01', 'auto_6h_batch', 1)",
                    ('TEST_W183_E6', 99 - i)
                )
        r = _evaluate_and_apply_one('TEST_W183_E6', {})
        assert r['action'] == 'skip_daily_cap'

    def test_unsupported_rule_logged_not_silent(self, tmp_db):
        """H7 fix verify: unsupported price_rule の skip でも痕跡が log に残る."""
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('TEST_W183_E7', lp_min_price=50.0)
        _seed_competitor('TEST_W183_E7', 'C1', price_usd=80.0, shipping_usd=10.0,
                         rule='ARBITRARY-INVALID-RULE')
        r = _evaluate_and_apply_one('TEST_W183_E7', {})
        assert r['action'] == 'skip_invalid_state'
        # log に 1 件 INSERT 済みであること
        with get_conn() as c:
            cnt = c.execute(
                "SELECT COUNT(*) FROM price_change_log WHERE ebay_item_id=?",
                ('TEST_W183_E7',)
            ).fetchone()[0]
        assert cnt == 1, "Q0 silent skip 違反: log row が入っていない"

    def test_happy_path_calls_revise_api(self, tmp_db, monkeypatch):
        """API は mock し、target_price と DB 更新まで通る経路を確認."""
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _evaluate_and_apply_one

        _seed_listing('TEST_W183_E8', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W183_E8', 'C1', price_usd=80.0, shipping_usd=10.0)
        # comp total=$90, our_total=$130, target = 89.99-10 = 79.99 ($50 floor 越え)

        # creds と revise_fixed_price_item を monkeypatch
        monkeypatch.setattr(
            'monitor.credentials.get_ebay_credentials',
            lambda config: {'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C',
                            'user_token': 'T'}
        )
        monkeypatch.setattr(
            'monitor.credentials.ebay_credentials_ok',
            lambda creds: True
        )
        captured = {}

        def fake_revise(item_id, new_price, *args, **kwargs):
            captured['item_id'] = item_id
            captured['price'] = new_price
            return {'success': True, 'ack': 'Success', 'raw': '<ok/>'}

        monkeypatch.setattr(
            'monitor.ebay_client.revise_fixed_price_item', fake_revise
        )

        r = _evaluate_and_apply_one('TEST_W183_E8', {'ebay': {}})
        assert r['action'] == 'reduced'
        assert captured['item_id'] == 'TEST_W183_E8'
        assert captured['price'] == 79.99
        # DB 更新確認
        with get_conn() as c:
            cur = c.execute(
                "SELECT current_price FROM ebay_listings WHERE ebay_item_id=?",
                ('TEST_W183_E8',)
            ).fetchone()[0]
            log_cnt = c.execute(
                "SELECT COUNT(*) FROM price_change_log "
                "WHERE ebay_item_id=? AND success=1",
                ('TEST_W183_E8',)
            ).fetchone()[0]
        assert cur == 79.99
        assert log_cnt == 1


# ────────────────────────────────────────
# _count_today_changes_jst (JST 境界)
# ────────────────────────────────────────

class TestCountTodayChangesJst:
    def test_count_zero_for_unknown_listing(self, tmp_db):
        from tasks.task_rival_pricing import _count_today_changes_jst
        assert _count_today_changes_jst('NONEXISTENT_W183') == 0

    def test_only_success_counted(self, tmp_db):
        """success=0 の row は count されない (failed_api は cap に消費しない)."""
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _count_today_changes_jst
        with get_conn() as c:
            for s in (0, 1, 1, 0):
                c.execute(
                    "INSERT INTO price_change_log "
                    "(ebay_item_id, old_price_usd, new_price_usd, "
                    " competitor_item_id, competitor_total_usd, rule_applied, "
                    " triggered_by, success) "
                    "VALUES ('JSTBOUND_X', 100, 99, 'C', 99, 'r', 'auto_6h_batch', ?)",
                    (s,)
                )
        assert _count_today_changes_jst('JSTBOUND_X') == 2

    def test_jst_date_boundary_uses_plus9_offset(self, tmp_db):
        """UTC 14:59 (= JST 23:59 同日) は本日 JST、UTC 15:01 (= JST 00:01 翌日) は翌日.

        SQLite の changed_at は UTC で保存される. DATE(x, '+9 hours') で JST 日付化.
        本テストは: 「いま」の UTC 時刻を見て、本日 JST 0:00 の境界が変な動きしないことを確認.
        """
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _count_today_changes_jst

        # 本日 JST 23:30 相当の UTC 時刻を計算
        now_utc = datetime.now(timezone.utc)
        jst_now = now_utc + timedelta(hours=9)
        # 本日 JST の任意時刻 (jst_today 12:00) を UTC 換算
        jst_today_noon = jst_now.replace(hour=12, minute=0, second=0, microsecond=0)
        # UTC 換算 = JST - 9h
        utc_today_noon_for_jst = jst_today_noon - timedelta(hours=9)

        with get_conn() as c:
            c.execute(
                "INSERT INTO price_change_log "
                "(ebay_item_id, old_price_usd, new_price_usd, competitor_item_id, "
                " competitor_total_usd, rule_applied, triggered_by, success, "
                " changed_at) "
                "VALUES ('JST_BD', 100, 99, 'C', 99, 'r', 'auto_6h_batch', 1, ?)",
                (utc_today_noon_for_jst.strftime('%Y-%m-%d %H:%M:%S'),)
            )
            # 「昨日 JST」相当 = jst_today_noon - 30h を UTC で
            yesterday_jst = jst_today_noon - timedelta(hours=30)
            utc_yesterday = yesterday_jst - timedelta(hours=9)
            c.execute(
                "INSERT INTO price_change_log "
                "(ebay_item_id, old_price_usd, new_price_usd, competitor_item_id, "
                " competitor_total_usd, rule_applied, triggered_by, success, "
                " changed_at) "
                "VALUES ('JST_BD', 100, 98, 'C', 98, 'r', 'auto_6h_batch', 1, ?)",
                (utc_yesterday.strftime('%Y-%m-%d %H:%M:%S'),)
            )
        # 本日 JST 1 件のみ count される
        assert _count_today_changes_jst('JST_BD') == 1
