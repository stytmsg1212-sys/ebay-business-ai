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
                     rule: str = 'competitor - 0.01',
                     pricing_eligible: int = 1):
    """competitor_products に test 用 row を 1 件 INSERT.

    W301 S3 (2026-07-02): pricing_eligible ゲート追加後も既存 test の期待値
    (値下げロジックそのものの検証) を変えないため、default で eligible=1 を
    明示 seed する (design doc 通り実運用の新規行は 0 だが、本 fixture は
    「ゲート通過後」の挙動を検証する既存 test 群のための helper)。ゲート自体
    の挙動は `pricing_eligible=0` を明示指定して別途検証する.
    """
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO competitor_products "
            "(our_item_id, competitor_item_id, price_rule, min_price, "
            " max_discount, is_active, competitor_price_usd, "
            " competitor_shipping_usd, pricing_eligible) "
            "VALUES (?, ?, ?, 0.0, 10.0, 1, ?, ?, ?)",
            (our_item_id, competitor_item_id, rule, price_usd, shipping_usd,
             pricing_eligible)
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
        """2026-07-02 main HIGH 修正後: 床判定は raw target で行われる.
        raw=$79.99 が floor=$116 を割っているので、clamp を通す前に skip される
        (旧 L5 意味論を完全維持: 勝てない値下げは据え置き)."""
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('TEST_W183_E3', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=116.0)
        # competitor $80, shipping $10 → comp_total=$90 < our_total=$130
        # raw target = 80 + 10 - 0.01 - 10 = 79.99 → floor=$116 で below_floor
        _seed_competitor('TEST_W183_E3', 'C1', price_usd=80.0, shipping_usd=10.0)
        r = _evaluate_and_apply_one('TEST_W183_E3', {})
        assert r['action'] == 'skip_below_floor'
        # message は raw target を報告 (clamp 前の $79.99 が使われることを確認)
        assert 'target=$79.99' in r['message']

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
        """API は mock し、target_price と DB 更新まで通る経路を確認.

        2026-07-02 5% clamp 追加後: raw target=$79.99 は現価格 $120 から
        33%超の下げなので clamp が発動し、実際に適用される価格は
        $120 * 0.95 = $114.00 になる (floor=$50 はクランプ後価格を下回るため
        不発動、clamp が実質上の下限として効く)。
        """
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _evaluate_and_apply_one

        _seed_listing('TEST_W183_E8', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W183_E8', 'C1', price_usd=80.0, shipping_usd=10.0)
        # comp total=$90, our_total=$130, raw target = 89.99-10 = 79.99
        # → 5% clamp で $114.00 ($50 floor は $114 を下回るため不発動)

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
        assert captured['price'] == 114.0
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
            rule_applied = c.execute(
                "SELECT rule_applied FROM price_change_log "
                "WHERE ebay_item_id=? AND success=1",
                ('TEST_W183_E8',)
            ).fetchone()[0]
        assert cur == 114.0
        assert log_cnt == 1
        # Q0: clamp 発動が rule_applied 列に痕跡として残る
        assert 'clamp' in rule_applied


# ────────────────────────────────────────
# W301 S3 (2026-07-02): pricing_eligible ゲート
# ────────────────────────────────────────

class TestPricingEligibleGate:
    """AI 店長 Phase1 S1 (migration v86) の pricing_eligible 列を W183 抽出に
    ゲートとして結線した挙動の検証 (design doc §8)。
    """

    def test_get_min_competitor_excludes_ineligible(self, tmp_db):
        from tasks.task_rival_pricing import _get_min_competitor
        _seed_listing('TEST_W301_G1', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W301_G1', 'C1', price_usd=80.0, shipping_usd=10.0,
                         pricing_eligible=0)
        assert _get_min_competitor('TEST_W301_G1') is None

    def test_get_min_competitor_excludes_null_eligible(self, tmp_db):
        """pricing_eligible が NULL (未分類) も対象外 (COALESCE(...,0)=1 判定)."""
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _get_min_competitor
        _seed_listing('TEST_W301_G2', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        with get_conn() as c:
            c.execute(
                "INSERT INTO competitor_products "
                "(our_item_id, competitor_item_id, price_rule, min_price, "
                " max_discount, is_active, competitor_price_usd, "
                " competitor_shipping_usd, pricing_eligible) "
                "VALUES ('TEST_W301_G2', 'C1', 'competitor - 0.01', 0.0, 10.0, "
                "        1, 80.0, 10.0, NULL)"
            )
        assert _get_min_competitor('TEST_W301_G2') is None

    def test_get_min_competitor_includes_eligible(self, tmp_db):
        from tasks.task_rival_pricing import _get_min_competitor
        _seed_listing('TEST_W301_G3', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W301_G3', 'C1', price_usd=80.0, shipping_usd=10.0,
                         pricing_eligible=1)
        result = _get_min_competitor('TEST_W301_G3')
        assert result is not None
        assert result['competitor_item_id'] == 'C1'

    def test_get_listings_with_active_competitors_excludes_all_ineligible(self, tmp_db):
        """全競合が ineligible な listing は抽出対象に出てこない."""
        from tasks.task_rival_pricing import _get_listings_with_active_competitors
        _seed_listing('TEST_W301_G4', lp_min_price=50.0)
        _seed_competitor('TEST_W301_G4', 'C1', price_usd=80.0, shipping_usd=10.0,
                         pricing_eligible=0)
        assert 'TEST_W301_G4' not in _get_listings_with_active_competitors()

    def test_get_listings_with_active_competitors_includes_eligible(self, tmp_db):
        from tasks.task_rival_pricing import _get_listings_with_active_competitors
        _seed_listing('TEST_W301_G5', lp_min_price=50.0)
        _seed_competitor('TEST_W301_G5', 'C1', price_usd=80.0, shipping_usd=10.0,
                         pricing_eligible=1)
        assert 'TEST_W301_G5' in _get_listings_with_active_competitors()

    def test_evaluate_and_apply_one_skips_when_all_ineligible(self, tmp_db):
        """listing 単位ではなく _evaluate_and_apply_one 経由でも
        competitor_price_unknown (= 値下げ対象なし) として skip される."""
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('TEST_W301_G6', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W301_G6', 'C1', price_usd=80.0, shipping_usd=10.0,
                         pricing_eligible=0)
        r = _evaluate_and_apply_one('TEST_W301_G6', {})
        assert r['action'] == 'skip_competitor_price_unknown'

    def test_count_gated_out_competitors(self, tmp_db):
        from tasks.task_rival_pricing import _count_gated_out_competitors
        _seed_listing('TEST_W301_G7', lp_min_price=50.0)
        _seed_competitor('TEST_W301_G7', 'C1', price_usd=80.0, shipping_usd=10.0,
                         pricing_eligible=0)
        _seed_competitor('TEST_W301_G7', 'C2', price_usd=81.0, shipping_usd=10.0,
                         pricing_eligible=1)
        # C1 のみ ineligible (is_active=1 かつ pricing_eligible!=1)
        assert _count_gated_out_competitors() == 1

    def test_count_gated_out_competitors_zero_when_all_eligible(self, tmp_db):
        from tasks.task_rival_pricing import _count_gated_out_competitors
        _seed_listing('TEST_W301_G8', lp_min_price=50.0)
        _seed_competitor('TEST_W301_G8', 'C1', price_usd=80.0, shipping_usd=10.0,
                         pricing_eligible=1)
        assert _count_gated_out_competitors() == 0

    def test_run_returns_gated_out_count_when_all_zero(self, tmp_db):
        """全 active 競合が eligible=0 でも run_rival_pricing_refresh は例外に
        ならず正常完走し (success=True)、gated_out_ineligible で件数が
        report される (Q0: 対象なしが silent にならない)."""
        from tasks.task_rival_pricing import run_rival_pricing_refresh
        _seed_listing('TEST_W301_G9', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W301_G9', 'C1', price_usd=80.0, shipping_usd=10.0,
                         pricing_eligible=0)
        r = run_rival_pricing_refresh({'ebay': {}})
        assert r['success'] is True
        assert r['listings_processed'] == 0
        assert r['gated_out_ineligible'] == 1
        assert 'gated_out_ineligible=1' in r['message']

    def test_run_processes_only_eligible_listing(self, tmp_db, monkeypatch):
        """eligible=1 の listing だけが処理され、eligible=0 の listing は
        listings_processed に含まれない (混在ケース)."""
        from tasks.task_rival_pricing import run_rival_pricing_refresh

        monkeypatch.setattr(
            'tasks.task_rival_pricing.refresh_competitor_pricing',
            lambda our_item_id, config: {'fetched': 1, 'failed': 0}
        )

        _seed_listing('TEST_W301_G10A', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W301_G10A', 'CA', price_usd=200.0, shipping_usd=0.0,
                         pricing_eligible=1)  # our_total=$130 <= comp=$200 → cheapest

        _seed_listing('TEST_W301_G10B', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W301_G10B', 'CB', price_usd=80.0, shipping_usd=10.0,
                         pricing_eligible=0)

        r = run_rival_pricing_refresh({'ebay': {}})
        assert r['listings_processed'] == 1
        assert r['gated_out_ineligible'] == 1


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


# ════════════════════════════════════════════════════════════════
# 2026-05-17 W7/W183 race 堅牢化: H4 (予約) / H5 (stale sync) / migration v42
# ════════════════════════════════════════════════════════════════

def _seed_price_log(ebay_item_id, *, success=1, claim_status=None,
                    changed_at_sql="datetime('now')",
                    triggered_by='auto_6h_batch', new_price=99.0):
    """price_change_log に test 用 row を 1 件 INSERT.

    changed_at_sql は test 専用の SQL 式 (定数のみ、injection 安全).
    UTC 保存なので 'datetime('now','-1 hour')' 等で相対指定する.
    """
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO price_change_log "
            "(ebay_item_id, old_price_usd, new_price_usd, competitor_item_id, "
            " competitor_total_usd, rule_applied, triggered_by, success, "
            f" claim_status, changed_at) "
            f"VALUES (?, 100, ?, 'C', 99, 'competitor - 0.01', ?, ?, ?, "
            f"{changed_at_sql})",
            (ebay_item_id, new_price, triggered_by, success, claim_status)
        )


class TestMigrationV42Idempotent:
    """Q2: v42 (claim_status 列追加) の冪等性 + データ保持."""

    def test_claim_status_column_exists(self, tmp_db):
        from monitor.database import get_conn
        with get_conn() as c:
            cols = [r[1] for r in c.execute(
                "PRAGMA table_info(price_change_log)").fetchall()]
        assert 'claim_status' in cols

    def test_init_db_twice_retains_data_and_version(self, tmp_db):
        from monitor.database import init_db, get_conn
        with get_conn() as c:
            c.execute(
                "INSERT INTO price_change_log "
                "(ebay_item_id, triggered_by, success) "
                "VALUES ('MIGV42', 'auto_6h_batch', 1)"
            )
        init_db()
        init_db()  # 2 回連続再実行でデータ消失しないこと
        with get_conn() as c:
            cnt = c.execute(
                "SELECT COUNT(*) FROM price_change_log "
                "WHERE ebay_item_id='MIGV42'"
            ).fetchone()[0]
            ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert cnt == 1, "v42 migration が冪等でない (データ消失 = Q2 違反)"
        assert ver >= 42


class TestH4Reservation:
    """H4: 予約パターンで cross-process race による cap 超過を防ぐ."""

    def test_claim_succeeds_under_cap_inserts_pending(self, tmp_db):
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _claim_price_change_slot
        for _ in range(3):
            _seed_price_log('H4A', success=1, claim_status='final')
        cid = _claim_price_change_slot(
            'H4A', 120.0, 110.0, 'C', 99.0, 'competitor - 0.01', 'auto_6h_batch')
        assert cid is not None
        with get_conn() as c:
            row = c.execute(
                "SELECT success, claim_status FROM price_change_log WHERE id=?",
                (cid,)).fetchone()
        assert row[0] == 0 and row[1] == 'pending'

    def test_claim_blocked_at_cap_with_success_rows(self, tmp_db):
        from tasks.task_rival_pricing import _claim_price_change_slot
        for _ in range(4):
            _seed_price_log('H4B', success=1, claim_status='final')
        assert _claim_price_change_slot(
            'H4B', 120.0, 110.0, 'C', 99.0,
            'competitor - 0.01', 'auto_6h_batch') is None

    def test_recent_pending_blocks_racer(self, tmp_db):
        """H4 核心: 3 success + 1 件目予約(pending) = 4 → 2 人目は弾かれる.

        scheduler が予約した直後に Streamlit ボタンが来ても cap=4 を
        超えない (旧実装は COUNT→API→INSERT の隙間で 5 になり得た).
        """
        from tasks.task_rival_pricing import _claim_price_change_slot
        for _ in range(3):
            _seed_price_log('H4C', success=1, claim_status='final')
        cid1 = _claim_price_change_slot(
            'H4C', 120.0, 110.0, 'C', 99.0,
            'competitor - 0.01', 'auto_6h_batch')
        assert cid1 is not None  # consumed 3 < 4 → 1 人目予約成功
        cid2 = _claim_price_change_slot(
            'H4C', 120.0, 109.0, 'C', 99.0,
            'competitor - 0.01', 'manual_button')
        assert cid2 is None  # 3 success + 1 pending(recent) = 4 → 2 人目 None

    def test_stale_pending_ages_out(self, tmp_db):
        """crash で漏れた >15 分前 pending は枠を恒久 block しない."""
        from tasks.task_rival_pricing import _claim_price_change_slot
        for _ in range(3):
            _seed_price_log('H4D', success=1, claim_status='final')
        _seed_price_log('H4D', success=0, claim_status='pending',
                        changed_at_sql="datetime('now','-30 minutes')")
        assert _claim_price_change_slot(
            'H4D', 120.0, 110.0, 'C', 99.0,
            'competitor - 0.01', 'auto_6h_batch') is not None

    def test_failed_finalize_releases_slot(self, tmp_db):
        """失敗確定 (success=0, final) は本日 4 回にカウントしない (user 確定)."""
        from monitor.database import get_conn
        from tasks.task_rival_pricing import (
            _claim_price_change_slot, _finalize_price_change)
        for _ in range(3):
            _seed_price_log('H4E', success=1, claim_status='final')
        cid = _claim_price_change_slot(
            'H4E', 120.0, 110.0, 'C', 99.0,
            'competitor - 0.01', 'auto_6h_batch')
        assert cid is not None
        _finalize_price_change(cid, success=False, error_message='eBay 500')
        with get_conn() as c:
            row = c.execute(
                "SELECT success, claim_status, error_message "
                "FROM price_change_log WHERE id=?", (cid,)).fetchone()
        assert row[0] == 0 and row[1] == 'final' and row[2] == 'eBay 500'
        # 失敗は枠解放 → 3 success のみ消費 → 次 claim 成功
        assert _claim_price_change_slot(
            'H4E', 120.0, 110.0, 'C', 99.0,
            'competitor - 0.01', 'auto_6h_batch') is not None

    def test_success_finalize_consumes_slot(self, tmp_db):
        from monitor.database import get_conn
        from tasks.task_rival_pricing import (
            _claim_price_change_slot, _finalize_price_change,
            _count_today_changes_jst)
        cid = _claim_price_change_slot(
            'H4F', 120.0, 110.0, 'C', 99.0,
            'competitor - 0.01', 'auto_6h_batch')
        _finalize_price_change(cid, success=True, new_price_usd=110.0)
        with get_conn() as c:
            row = c.execute(
                "SELECT success, claim_status, new_price_usd "
                "FROM price_change_log WHERE id=?", (cid,)).fetchone()
        assert row[0] == 1 and row[1] == 'final' and row[2] == 110.0
        assert _count_today_changes_jst('H4F') == 1

    def test_pending_excluded_from_audit_log(self, tmp_db):
        """get_price_change_log は確定前 pending を監査ログから除外."""
        from monitor.lowest_price import get_price_change_log
        _seed_price_log('H4G', success=1, claim_status='final')
        _seed_price_log('H4G', success=0, claim_status='pending')
        rows = get_price_change_log('H4G')
        assert len(rows) == 1 and rows[0]['success'] is True


class TestH5StaleSuspect:
    """H5: 値下げ直後の stale sync 上書きを検知してその回 skip."""

    def _state(self, **kw):
        base = {'current_price': 145.0, 'shipping_cost': 10.0,
                'lp_min_price': 50.0, 'lp_breakeven_usd': 40.0,
                'is_ended': False, 'last_synced_at': None}
        base.update(kw)
        return base

    def _lsa(self, expr):
        from monitor.database import get_conn
        with get_conn() as c:
            return c.execute(f"SELECT {expr}").fetchone()[0]

    def test_no_history_not_suspect(self, tmp_db):
        from tasks.task_rival_pricing import _is_price_stale_suspect
        assert _is_price_stale_suspect(self._state(), 'H5A') is None

    def test_suspect_when_sync_after_recent_reduction_mismatch(self, tmp_db):
        from tasks.task_rival_pricing import _is_price_stale_suspect
        _seed_price_log('H5B', success=1, claim_status='final', new_price=124.0,
                        changed_at_sql="datetime('now','-1 hour')")
        lsa = self._lsa("datetime('now','-1 hour','+9 hours','+10 minutes')")
        st = self._state(current_price=145.0, last_synced_at=lsa)
        assert _is_price_stale_suspect(st, 'H5B') is not None

    def test_not_suspect_when_db_matches_applied(self, tmp_db):
        from tasks.task_rival_pricing import _is_price_stale_suspect
        _seed_price_log('H5C', success=1, claim_status='final', new_price=124.0,
                        changed_at_sql="datetime('now','-1 hour')")
        lsa = self._lsa("datetime('now','-1 hour','+9 hours','+10 minutes')")
        st = self._state(current_price=124.0, last_synced_at=lsa)
        assert _is_price_stale_suspect(st, 'H5C') is None

    def test_not_suspect_when_sync_before_reduction(self, tmp_db):
        from tasks.task_rival_pricing import _is_price_stale_suspect
        _seed_price_log('H5D', success=1, claim_status='final', new_price=124.0,
                        changed_at_sql="datetime('now','-1 hour')")
        lsa = self._lsa("datetime('now','-3 hours','+9 hours')")
        st = self._state(current_price=145.0, last_synced_at=lsa)
        assert _is_price_stale_suspect(st, 'H5D') is None

    def test_not_suspect_when_reduction_older_than_guard(self, tmp_db):
        from tasks.task_rival_pricing import _is_price_stale_suspect
        _seed_price_log('H5E', success=1, claim_status='final', new_price=124.0,
                        changed_at_sql="datetime('now','-7 hours')")
        lsa = self._lsa("datetime('now','-1 hour','+9 hours')")
        st = self._state(current_price=145.0, last_synced_at=lsa)
        assert _is_price_stale_suspect(st, 'H5E') is None

    def test_not_suspect_when_no_last_synced(self, tmp_db):
        from tasks.task_rival_pricing import _is_price_stale_suspect
        _seed_price_log('H5F', success=1, claim_status='final', new_price=124.0,
                        changed_at_sql="datetime('now','-1 hour')")
        st = self._state(current_price=145.0, last_synced_at=None)
        assert _is_price_stale_suspect(st, 'H5F') is None

    def test_pending_reduction_not_treated_as_applied(self, tmp_db):
        """claim_status='pending' の行は『最後に設定した値』に含めない."""
        from tasks.task_rival_pricing import _latest_successful_change
        _seed_price_log('H5H', success=0, claim_status='pending',
                        new_price=124.0,
                        changed_at_sql="datetime('now','-1 hour')")
        assert _latest_successful_change('H5H') is None

    def test_evaluate_returns_skip_stale_price_end_to_end(self, tmp_db):
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('H5G', current_price=145.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('H5G', 'C1', price_usd=80.0, shipping_usd=10.0)
        _seed_price_log('H5G', success=1, claim_status='final', new_price=124.0,
                        changed_at_sql="datetime('now','-1 hour')")
        with get_conn() as c:
            lsa = c.execute(
                "SELECT datetime('now','-1 hour','+9 hours','+10 minutes')"
            ).fetchone()[0]
            c.execute(
                "UPDATE ebay_listings SET last_synced_at=? "
                "WHERE ebay_item_id='H5G'", (lsa,))
        r = _evaluate_and_apply_one('H5G', {})
        assert r['action'] == 'skip_stale_price'


class TestH4LockContention:
    """code-reviewer HIGH-1 回帰: BEGIN IMMEDIATE が lock timeout した時に
    silent 取りこぼし / skip_daily_cap 誤分類 / ROLLBACK 漏れ をしない."""

    class _LockOnBegin:
        """get_conn() proxy: BEGIN IMMEDIATE だけ指定 OperationalError を投げ、
        他は inner に委譲する (with 文 context manager 対応)."""
        def __init__(self, inner, exc):
            self._inner = inner
            self._exc = exc
        def execute(self, sql, *a, **kw):
            if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                raise self._exc
            return self._inner.execute(sql, *a, **kw)
        def __enter__(self):
            self._inner.__enter__()
            return self
        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    def test_claim_lock_timeout_returns_sentinel_and_logs(
            self, tmp_db, monkeypatch, caplog):
        from monitor import database
        from tasks.task_rival_pricing import (
            _claim_price_change_slot, _SLOT_LOCKED)
        real = database.get_conn
        exc = sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(
            database, "get_conn",
            lambda: self._LockOnBegin(real(), exc))
        with caplog.at_level("WARNING"):
            r = _claim_price_change_slot(
                'LOCK1', 120.0, 110.0, 'C', 99.0,
                'competitor - 0.01', 'auto_6h_batch')
        assert r is _SLOT_LOCKED
        assert any('lock' in rec.message.lower() for rec in caplog.records), \
            "Q0: lock 競合の痕跡が log に残っていない"

    def test_claim_non_lock_operational_error_reraises(
            self, tmp_db, monkeypatch):
        """lock 以外の OperationalError (schema 不整合等) は隠さず送出."""
        from monitor import database
        from tasks.task_rival_pricing import _claim_price_change_slot
        real = database.get_conn
        exc = sqlite3.OperationalError("no such table: price_change_log")
        monkeypatch.setattr(
            database, "get_conn",
            lambda: self._LockOnBegin(real(), exc))
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            _claim_price_change_slot(
                'LOCK2', 120.0, 110.0, 'C', 99.0,
                'competitor - 0.01', 'auto_6h_batch')

    def test_evaluate_returns_skip_lock_contention_not_daily_cap(
            self, tmp_db, monkeypatch):
        """lock 競合は skip_daily_cap と別 action で返る (user 誤誘導防止)."""
        import tasks.task_rival_pricing as trp
        from tasks.task_rival_pricing import (
            _evaluate_and_apply_one, _SLOT_LOCKED)
        _seed_listing('LOCK3', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('LOCK3', 'C1', price_usd=80.0, shipping_usd=10.0)
        monkeypatch.setattr(
            'monitor.credentials.get_ebay_credentials',
            lambda config: {'app_id': 'A', 'dev_id': 'D',
                            'cert_id': 'C', 'user_token': 'T'})
        monkeypatch.setattr(
            'monitor.credentials.ebay_credentials_ok', lambda creds: True)
        monkeypatch.setattr(
            trp, "_claim_price_change_slot",
            lambda *a, **kw: _SLOT_LOCKED)
        r = _evaluate_and_apply_one('LOCK3', {'ebay': {}})
        assert r['action'] == 'skip_lock_contention'
        assert r['action'] != 'skip_daily_cap'


class TestH4InflightCrashSafety:
    """Codex 2 段レビュー HIGH (2026-05-17) 回帰: eBay API 成功直後〜finalize
    前の crash で実値下げ済の枠が 15 分時効で消え、同日 4 回上限を超過
    (過剰値下げ = margin 浸食) するのを防ぐ (api_inflight 時効なし)."""

    def test_inflight_does_not_age_out_blocks_cap(self, tmp_db):
        """3 success + 1 古い api_inflight = 4 → claim None (時効なしで消費継続).

        旧実装 (pending 15 分時効) は crash 後 api_inflight 相当が消え、
        4 回上限を超えて追加予約できてしまった (Codex HIGH)."""
        from tasks.task_rival_pricing import _claim_price_change_slot
        for _ in range(3):
            _seed_price_log('INF1', success=1, claim_status='final')
        # crash 残骸を模擬: 本日 JST 0:00 の api_inflight (= API 成功したかも)。
        # api_inflight は 15 分時効を持たないので「古くても本日 JST なら
        # cap を消費し続ける」ことを検証。changed_at は JST 当日固定
        # (datetime('now','-2 hours') は JST 日跨ぎ時間帯で前日判定になり
        # flaky だったため start-of-JST-day 固定に修正、sqlite-timezone.md)。
        _seed_price_log(
            'INF1', success=0, claim_status='api_inflight',
            changed_at_sql="datetime('now','+9 hours','start of day','-9 hours')")
        assert _claim_price_change_slot(
            'INF1', 120.0, 110.0, 'C', 99.0,
            'competitor - 0.01', 'auto_6h_batch') is None

    def test_old_pending_still_ages_out(self, tmp_db):
        """対照: API 未呼出の古い pending は従来通り 15 分で時効解放
        (実 eBay 変更なし確実なので枠を塞がない)."""
        from tasks.task_rival_pricing import _claim_price_change_slot
        for _ in range(3):
            _seed_price_log('INF2', success=1, claim_status='final')
        _seed_price_log('INF2', success=0, claim_status='pending',
                        changed_at_sql="datetime('now','-30 minutes')")
        assert _claim_price_change_slot(
            'INF2', 120.0, 110.0, 'C', 99.0,
            'competitor - 0.01', 'auto_6h_batch') is not None

    def test_mark_claim_inflight_transition(self, tmp_db):
        from monitor.database import get_conn
        from tasks.task_rival_pricing import (
            _claim_price_change_slot, _mark_claim_inflight,
            _finalize_price_change)
        cid = _claim_price_change_slot(
            'INF3', 120.0, 110.0, 'C', 99.0,
            'competitor - 0.01', 'auto_6h_batch')
        with get_conn() as c:
            assert c.execute(
                "SELECT claim_status FROM price_change_log WHERE id=?",
                (cid,)).fetchone()[0] == 'pending'
        _mark_claim_inflight(cid)
        with get_conn() as c:
            assert c.execute(
                "SELECT claim_status FROM price_change_log WHERE id=?",
                (cid,)).fetchone()[0] == 'api_inflight'
        # 二重呼出は no-op (pending 限定 guard)、finalize で final へ
        _mark_claim_inflight(cid)
        _finalize_price_change(cid, success=True, new_price_usd=110.0)
        with get_conn() as c:
            row = c.execute(
                "SELECT claim_status, success FROM price_change_log WHERE id=?",
                (cid,)).fetchone()
        assert row[0] == 'final' and row[1] == 1

    def test_inflight_visible_in_audit_log(self, tmp_db):
        """Codex MEDIUM 回帰: crash 残骸 (api_inflight = 実値下げの可能性) は
        監査ログに見える (pending のみ除外、api_inflight は調査のため可視)."""
        from monitor.lowest_price import get_price_change_log
        _seed_price_log('INF4', success=1, claim_status='final')
        _seed_price_log('INF4', success=0, claim_status='api_inflight')
        _seed_price_log('INF4', success=0, claim_status='pending')
        rows = get_price_change_log('INF4')
        statuses = {(r['success']) for r in rows}
        # final(success=1) + api_inflight(success=0) = 2 行、pending は除外
        assert len(rows) == 2


# ────────────────────────────────────────
# W245: 偽装成功の根絶 (run-level success 判定 + Discord 通知)
# ────────────────────────────────────────

class TestW245RunLevelSuccess:
    """run_rival_pricing_refresh が失敗を success: True で隠さないこと (F12 根治)."""

    @staticmethod
    def _mock_creds(monkeypatch):
        monkeypatch.setattr(
            'monitor.credentials.get_ebay_credentials',
            lambda config: {'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C',
                            'user_token': 'T'}
        )
        monkeypatch.setattr(
            'monitor.credentials.ebay_credentials_ok', lambda creds: True
        )

    @staticmethod
    def _capture_discord(monkeypatch):
        """DiscordNotifier を capture mock に差し替え、送信内容リストを返す."""
        sent = []

        class _FakeNotifier:
            def __init__(self, webhook, bypass_env=False):
                self.webhook = webhook

            def send_message(self, content):
                sent.append(content)
                return True

        monkeypatch.setattr(
            'notifiers.discord_notifier.DiscordNotifier', _FakeNotifier
        )
        return sent

    _CFG = {'ebay': {}, 'discord': {'webhook_url': 'https://discord.test/hook'}}

    def test_failed_api_marks_run_failed_and_alerts(self, tmp_db, monkeypatch):
        """eBay ReviseItem 失敗 → success=False + Discord 失敗 alert (旧: 無条件 True)."""
        from tasks.task_rival_pricing import run_rival_pricing_refresh

        _seed_listing('TEST_W245_F1', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W245_F1', 'C1', price_usd=80.0, shipping_usd=10.0)
        self._mock_creds(monkeypatch)
        sent = self._capture_discord(monkeypatch)
        monkeypatch.setattr(
            'tasks.task_rival_pricing.refresh_competitor_pricing',
            lambda iid, cfg: {'fetched': 1, 'failed': 0}
        )
        monkeypatch.setattr(
            'monitor.ebay_client.revise_fixed_price_item',
            lambda *a, **k: {'success': False, 'message': 'API down (test)'}
        )

        r = run_rival_pricing_refresh(self._CFG)
        assert r['success'] is False, "偽装成功: failed_api>0 なのに success=True"
        assert r['failed_api'] == 1
        assert 'FAILED' in r['message']
        assert any('失敗' in m for m in sent), "失敗 alert が Discord に飛んでいない"

    def test_fetch_outage_marks_run_failed(self, tmp_db, monkeypatch):
        """Browse fetch 全滅 (fetched=0, failed>0) → success=False (6/4-6/8 OAuth 事故型)."""
        from tasks.task_rival_pricing import run_rival_pricing_refresh

        _seed_listing('TEST_W245_F2', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W245_F2', 'C1', price_usd=80.0, shipping_usd=10.0)
        sent = self._capture_discord(monkeypatch)

        def _raise(iid, cfg):
            raise RuntimeError('Browse API 403 (test)')

        monkeypatch.setattr(
            'tasks.task_rival_pricing.refresh_competitor_pricing', _raise
        )

        r = run_rival_pricing_refresh(self._CFG)
        assert r['success'] is False, "偽装成功: fetch 全滅なのに success=True"
        assert r['fetched_total'] == 0 and r['failed_total'] >= 1
        assert any('失敗' in m for m in sent)

    def test_reduced_sends_discord_report(self, tmp_db, monkeypatch):
        """値下げ成功 → success=True + 値下げ結果が Discord に通知される."""
        from tasks.task_rival_pricing import run_rival_pricing_refresh

        _seed_listing('TEST_W245_R1', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W245_R1', 'C1', price_usd=80.0, shipping_usd=10.0)
        self._mock_creds(monkeypatch)
        sent = self._capture_discord(monkeypatch)
        monkeypatch.setattr(
            'tasks.task_rival_pricing.refresh_competitor_pricing',
            lambda iid, cfg: {'fetched': 1, 'failed': 0}
        )
        monkeypatch.setattr(
            'monitor.ebay_client.revise_fixed_price_item',
            lambda *a, **k: {'success': True, 'ack': 'Success', 'raw': '<ok/>'}
        )

        r = run_rival_pricing_refresh(self._CFG)
        assert r['success'] is True
        assert r['reduced'] == 1
        assert any('自動値下げ' in m for m in sent), "値下げ結果通知が飛んでいない"
        # 通知に old→new 価格が含まれる (money-direct 可視化)。
        # 2026-07-02 5% clamp 追加後: raw target $79.99 は clamp されて $114.00
        # ($120 * 0.95) になる。
        assert any('$120.00' in m and '$114.00' in m for m in sent)

    def test_healthy_skip_run_is_success_and_silent(self, tmp_db, monkeypatch):
        """全件 skip (already cheapest) の健全 run → success=True + Discord 無音."""
        from tasks.task_rival_pricing import run_rival_pricing_refresh

        _seed_listing('TEST_W245_S1', current_price=80.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('TEST_W245_S1', 'C1', price_usd=200.0, shipping_usd=10.0)
        sent = self._capture_discord(monkeypatch)
        monkeypatch.setattr(
            'tasks.task_rival_pricing.refresh_competitor_pricing',
            lambda iid, cfg: {'fetched': 1, 'failed': 0}
        )

        r = run_rival_pricing_refresh(self._CFG)
        assert r['success'] is True
        assert r['skipped_already_cheapest'] == 1
        assert sent == [], "健全 skip なのに通知が飛んだ (alert fatigue)"


# ════════════════════════════════════════════════════════════════
# 2026-07-02 (user 指示): 値下げ合戦スパイラル抑止 — 第 2・第 3 の安全弁
# ════════════════════════════════════════════════════════════════

class TestMaxDropClamp:
    """第 2 安全弁: 1 回の値下げ幅は現価格の 5% まで (_apply_max_drop_clamp)."""

    def test_within_5pct_not_clamped(self):
        from tasks.task_rival_pricing import _apply_max_drop_clamp
        # 120 → 115 (4.17% 下げ) は 5% 以内、clamp 不発動
        price, clamped = _apply_max_drop_clamp(120.0, 115.0)
        assert clamped is False
        assert price == 115.0

    def test_exactly_5pct_not_clamped(self):
        """ちょうど 5% ジャストの下げは非 clamp (境界は許容側)."""
        from tasks.task_rival_pricing import _apply_max_drop_clamp
        price, clamped = _apply_max_drop_clamp(120.0, 114.0)  # 120*0.95=114.0
        assert clamped is False
        assert price == 114.0

    def test_over_5pct_clamped_to_95pct(self):
        """5% 超の下げは current_price * 0.95 に clamp される."""
        from tasks.task_rival_pricing import _apply_max_drop_clamp
        price, clamped = _apply_max_drop_clamp(120.0, 79.99)  # 33%超の下げ
        assert clamped is True
        assert price == 114.0

    def test_just_over_5pct_boundary_clamped(self):
        """5.01% 下げ (境界のすぐ外) は clamp される."""
        from tasks.task_rival_pricing import _apply_max_drop_clamp
        price, clamped = _apply_max_drop_clamp(100.0, 94.9)  # 5.1% 下げ
        assert clamped is True
        assert price == 95.0

    def test_float_precision_boundary_not_falsely_clamped(self):
        """整数セント判定により float 誤差で境界 (端数価格) を誤 clamp しない.

        99.99 * 0.95 = 94.9905 (端数セント) → ceil で $95.00 に確定。
        target がちょうど $95.00 ならジャスト境界として非 clamp、
        $94.99 (1 セント下) なら clamp される (float 誤差で揺れない)."""
        from tasks.task_rival_pricing import _apply_max_drop_clamp
        price, clamped = _apply_max_drop_clamp(99.99, 95.00)
        assert clamped is False
        assert price == 95.00
        price2, clamped2 = _apply_max_drop_clamp(99.99, 94.99)
        assert clamped2 is True
        assert price2 == 95.00


class TestMaxDropClampIntegration:
    """5% clamp と既存 floor (L5) の統合挙動 (_evaluate_and_apply_one 経由)."""

    def test_raw_target_below_floor_skips_regardless_of_clamp(self, tmp_db):
        """★HIGH 修正の核心: raw target が床を割っていたら、clamp を通す前に
        skip_below_floor で据え置き (旧意味論を完全維持)。

        旧実装 (clamp を先に適用) では raw=$45 → clamp $114 → 床 $50 通過 →
        値下げが「解禁」され $114 に降下 → 競合には勝てないのに利幅だけ削る
        純損経路が発生した。新実装は raw で床判定するのでこの経路が塞がれる。
        """
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('CLAMP_A', current_price=120.0, shipping_cost=0.0,
                      lp_min_price=50.0)
        # comp_total=$45.01 → raw target=$45.00 < floor $50.00 → skip
        # (旧実装は clamp $114 で通過し reduced になっていた)
        _seed_competitor('CLAMP_A', 'C1', price_usd=45.01, shipping_usd=0.0)
        r = _evaluate_and_apply_one('CLAMP_A', {})
        assert r['action'] == 'skip_below_floor'
        # message は raw target ($45.00) を報告 (clamp 前で判定されている痕跡)
        assert 'target=$45.00' in r['message']
        assert 'floor=$50.00' in r['message']

    def test_raw_target_equals_floor_boundary_passes_then_clamps(
            self, tmp_db, monkeypatch):
        """境界: raw target がちょうど floor と同額の時は floor 通過
        (`<`, not `<=`)、その後 clamp が適用される (旧挙動 = 通過を維持)."""
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('CLAMP_A_EQ', current_price=120.0, shipping_cost=0.0,
                      lp_min_price=50.0)
        # comp_price=$50.01 → raw target=$50.00 == floor $50.00 → 通過
        # 5% clamp: $50.00 << $114.00 (95% of $120) → clamp
        _seed_competitor('CLAMP_A_EQ', 'C1', price_usd=50.01, shipping_usd=0.0)
        monkeypatch.setattr(
            'monitor.credentials.get_ebay_credentials',
            lambda config: {'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C',
                            'user_token': 'T'}
        )
        monkeypatch.setattr(
            'monitor.credentials.ebay_credentials_ok', lambda creds: True
        )
        monkeypatch.setattr(
            'monitor.ebay_client.revise_fixed_price_item',
            lambda *a, **k: {'success': True, 'ack': 'Success', 'raw': '<ok/>'}
        )
        r = _evaluate_and_apply_one('CLAMP_A_EQ', {'ebay': {}})
        assert r['action'] == 'reduced'
        assert r['new_price'] == 114.0

    def test_raw_target_above_floor_and_over_5pct_clamps_to_95pct(
            self, tmp_db, monkeypatch):
        """raw が床以上、かつ 5% 超の下げ → clamp が適用され current * 0.95 に着地."""
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        _seed_listing('CLAMP_B', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        # comp_total=$90 → raw target=$79.99 > floor $50 → clamp で $114.00
        _seed_competitor('CLAMP_B', 'C1', price_usd=80.0, shipping_usd=10.0)
        monkeypatch.setattr(
            'monitor.credentials.get_ebay_credentials',
            lambda config: {'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C',
                            'user_token': 'T'}
        )
        monkeypatch.setattr(
            'monitor.credentials.ebay_credentials_ok', lambda creds: True
        )
        monkeypatch.setattr(
            'monitor.ebay_client.revise_fixed_price_item',
            lambda *a, **k: {'success': True, 'ack': 'Success', 'raw': '<ok/>'}
        )
        r = _evaluate_and_apply_one('CLAMP_B', {'ebay': {}})
        assert r['action'] == 'reduced'
        assert r['new_price'] == 114.0

    def test_clamp_logged_in_rule_applied_column(self, tmp_db, monkeypatch, caplog):
        """Q0: clamp 発動が scheduler.log (logger.info) + DB (rule_applied) に残る."""
        import logging
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _evaluate_and_apply_one

        _seed_listing('CLAMP_C', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('CLAMP_C', 'C1', price_usd=80.0, shipping_usd=10.0)
        monkeypatch.setattr(
            'monitor.credentials.get_ebay_credentials',
            lambda config: {'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C',
                            'user_token': 'T'}
        )
        monkeypatch.setattr(
            'monitor.credentials.ebay_credentials_ok', lambda creds: True
        )
        monkeypatch.setattr(
            'monitor.ebay_client.revise_fixed_price_item',
            lambda *a, **k: {'success': True, 'ack': 'Success', 'raw': '<ok/>'}
        )
        with caplog.at_level(logging.INFO):
            r = _evaluate_and_apply_one('CLAMP_C', {'ebay': {}})
        assert r['action'] == 'reduced'
        assert r['new_price'] == 114.0
        assert any('5%clamp' in rec.message for rec in caplog.records), \
            "Q0: clamp 発動が scheduler.log に残っていない"
        with get_conn() as c:
            rule_applied = c.execute(
                "SELECT rule_applied FROM price_change_log "
                "WHERE ebay_item_id=? AND success=1", ('CLAMP_C',)
            ).fetchone()[0]
        assert 'clamp' in rule_applied, "Q0: clamp 発動が DB (rule_applied) に残っていない"

    def test_no_clamp_no_marker_in_rule_applied(self, tmp_db, monkeypatch):
        """clamp 非発動時は rule_applied に clamp マーカーが付かない (誤爆確認)."""
        from monitor.database import get_conn
        from tasks.task_rival_pricing import _evaluate_and_apply_one

        # raw target が 5% 以内に収まるよう競合価格を調整
        # our=120, shipping=10 → floor(5%)=$114.00
        # comp shipping=0, comp_price=125 → comp_total=125, target=124.99-10=114.99
        # (>= our_price(120)? いいえ、114.99 < 120 = 値下げ方向、かつ 5%以内)
        _seed_listing('CLAMP_D', current_price=120.0, shipping_cost=10.0,
                      lp_min_price=50.0)
        _seed_competitor('CLAMP_D', 'C1', price_usd=125.0, shipping_usd=0.0)
        monkeypatch.setattr(
            'monitor.credentials.get_ebay_credentials',
            lambda config: {'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C',
                            'user_token': 'T'}
        )
        monkeypatch.setattr(
            'monitor.credentials.ebay_credentials_ok', lambda creds: True
        )
        monkeypatch.setattr(
            'monitor.ebay_client.revise_fixed_price_item',
            lambda *a, **k: {'success': True, 'ack': 'Success', 'raw': '<ok/>'}
        )
        r = _evaluate_and_apply_one('CLAMP_D', {'ebay': {}})
        assert r['action'] == 'reduced'
        assert r['new_price'] == 114.99
        with get_conn() as c:
            rule_applied = c.execute(
                "SELECT rule_applied FROM price_change_log "
                "WHERE ebay_item_id=? AND success=1", ('CLAMP_D',)
            ).fetchone()[0]
        assert 'clamp' not in rule_applied


# ────────────────────────────────────────
# 第 3 安全弁: 同一商品 3 連続値下げ Discord アラート
# ────────────────────────────────────────

def _seed_reduction(ebay_item_id: str, old_price: float, new_price: float,
                    *, success: int = 1, changed_at_sql: str = "datetime('now')"):
    """price_change_log に「old→new」の値下げ/値上げ 1 件を INSERT するヘルパ."""
    from monitor.database import get_conn
    with get_conn() as c:
        c.execute(
            "INSERT INTO price_change_log "
            "(ebay_item_id, old_price_usd, new_price_usd, competitor_item_id, "
            " competitor_total_usd, rule_applied, triggered_by, success, "
            f" changed_at) "
            f"VALUES (?, ?, ?, 'C1', 99, 'competitor - 0.01', 'auto_6h_batch', ?, "
            f"{changed_at_sql})",
            (ebay_item_id, old_price, new_price, success)
        )


class TestConsecutiveReductionStreak:
    """第 3 安全弁: _check_consecutive_reduction_streak の判定ロジック."""

    def test_fewer_than_3_no_alert(self, tmp_db):
        from tasks.task_rival_pricing import _check_consecutive_reduction_streak
        _seed_reduction('STK_A', 100.0, 95.0)
        _seed_reduction('STK_A', 95.0, 90.0)
        assert _check_consecutive_reduction_streak('STK_A') is None

    def test_3_consecutive_reductions_fires(self, tmp_db):
        from tasks.task_rival_pricing import _check_consecutive_reduction_streak
        _seed_reduction('STK_B', 100.0, 95.0,
                        changed_at_sql="datetime('now','-2 hours')")
        _seed_reduction('STK_B', 95.0, 90.0,
                        changed_at_sql="datetime('now','-1 hours')")
        _seed_reduction('STK_B', 90.0, 85.0)
        streak = _check_consecutive_reduction_streak('STK_B')
        assert streak is not None
        assert streak['count'] == 3
        assert streak['prices'] == [85.0, 90.0, 95.0]

    def test_value_increase_in_between_resets_streak(self, tmp_db):
        """値上げを挟むとストリークがリセットされ、直後の 1 回だけでは発火しない."""
        from tasks.task_rival_pricing import _check_consecutive_reduction_streak
        _seed_reduction('STK_C', 100.0, 95.0,
                        changed_at_sql="datetime('now','-3 hours')")
        _seed_reduction('STK_C', 95.0, 90.0,
                        changed_at_sql="datetime('now','-2 hours')")
        _seed_reduction('STK_C', 90.0, 100.0,   # 値上げ (increase)
                        changed_at_sql="datetime('now','-1 hours')")
        _seed_reduction('STK_C', 100.0, 95.0)   # 値上げ後 1 回目の値下げ
        assert _check_consecutive_reduction_streak('STK_C') is None

    def test_after_reset_needs_3_more_to_refire(self, tmp_db):
        """値上げでリセット後、さらに値下げが 3 回連続したら再度発火する."""
        from tasks.task_rival_pricing import _check_consecutive_reduction_streak
        _seed_reduction('STK_D', 100.0, 95.0,
                        changed_at_sql="datetime('now','-5 hours')")
        _seed_reduction('STK_D', 95.0, 100.0,   # 値上げ
                        changed_at_sql="datetime('now','-4 hours')")
        _seed_reduction('STK_D', 100.0, 95.0,
                        changed_at_sql="datetime('now','-3 hours')")
        _seed_reduction('STK_D', 95.0, 90.0,
                        changed_at_sql="datetime('now','-2 hours')")
        _seed_reduction('STK_D', 90.0, 85.0,
                        changed_at_sql="datetime('now','-1 hours')")
        streak = _check_consecutive_reduction_streak('STK_D')
        assert streak is not None
        assert streak['count'] == 3

    def test_outside_7day_window_no_alert(self, tmp_db):
        """3 連続値下げだが最も古い 1 件が 7 日超前 → window 外で非発火."""
        from tasks.task_rival_pricing import _check_consecutive_reduction_streak
        _seed_reduction('STK_E', 100.0, 95.0,
                        changed_at_sql="datetime('now','-8 days')")
        _seed_reduction('STK_E', 95.0, 90.0,
                        changed_at_sql="datetime('now','-4 days')")
        _seed_reduction('STK_E', 90.0, 85.0,
                        changed_at_sql="datetime('now','-1 hours')")
        assert _check_consecutive_reduction_streak('STK_E') is None

    def test_within_7day_window_boundary_fires(self, tmp_db):
        """最も古い 1 件がちょうど 7 日以内 (境界内) なら発火する."""
        from tasks.task_rival_pricing import _check_consecutive_reduction_streak
        _seed_reduction('STK_F', 100.0, 95.0,
                        changed_at_sql="datetime('now','-6 days','-23 hours')")
        _seed_reduction('STK_F', 95.0, 90.0,
                        changed_at_sql="datetime('now','-3 days')")
        _seed_reduction('STK_F', 90.0, 85.0,
                        changed_at_sql="datetime('now','-1 hours')")
        assert _check_consecutive_reduction_streak('STK_F') is not None

    def test_equal_price_not_counted_as_reduction(self, tmp_db):
        """同額 (値下げでない) が混ざるとストリーク非成立."""
        from tasks.task_rival_pricing import _check_consecutive_reduction_streak
        _seed_reduction('STK_G', 100.0, 95.0,
                        changed_at_sql="datetime('now','-2 hours')")
        _seed_reduction('STK_G', 95.0, 95.0,   # 同額
                        changed_at_sql="datetime('now','-1 hours')")
        _seed_reduction('STK_G', 95.0, 90.0)
        assert _check_consecutive_reduction_streak('STK_G') is None

    def test_same_second_tie_breaks_by_id_desc(self, tmp_db):
        """changed_at が同一秒 (CURRENT_TIMESTAMP tie) の時、id 降順で新しい行が
        優先されて「直近 3 件」が決定的に選ばれる (Codex Finding 1 hardening)。

        シナリオ: 4 件全て changed_at='2026-07-02 05:00:00' を明示指定。
        SQL 上の挿入順 (= id 昇順) は
          id1: old=90 new=95  (値上げ)
          id2: old=100 new=95 (値下げ)
          id3: old=95 new=90  (値下げ)
          id4: old=90 new=85  (値下げ)
        id DESC で LIMIT 3 なら [id4, id3, id2] = 全て値下げ → 発火。
        id タイブレークが無いと id1 (値上げ) が混ざる非決定順が起き得た。
        """
        from tasks.task_rival_pricing import _check_consecutive_reduction_streak
        same_ts = "'2026-07-02 05:00:00'"
        # 挿入順が id 昇順になる。id1 が最古扱いの「値上げ」ダミー行。
        _seed_reduction('STK_TIE', 90.0, 95.0, changed_at_sql=same_ts)   # id1 値上げ
        _seed_reduction('STK_TIE', 100.0, 95.0, changed_at_sql=same_ts)  # id2 値下げ
        _seed_reduction('STK_TIE', 95.0, 90.0, changed_at_sql=same_ts)   # id3 値下げ
        _seed_reduction('STK_TIE', 90.0, 85.0, changed_at_sql=same_ts)   # id4 値下げ
        streak = _check_consecutive_reduction_streak('STK_TIE')
        assert streak is not None, "id 降順 tie-break が効いていない (id1 値上げが混入)"
        assert streak['count'] == 3
        # id4, id3, id2 の new_price_usd (新しい順)
        assert streak['prices'] == [85.0, 90.0, 95.0]

    def test_failed_rows_not_counted(self, tmp_db):
        """success=0 の失敗行は判定対象に含まれない."""
        from tasks.task_rival_pricing import _check_consecutive_reduction_streak
        _seed_reduction('STK_H', 100.0, 95.0,
                        changed_at_sql="datetime('now','-3 hours')")
        _seed_reduction('STK_H', 95.0, 90.0, success=0,
                        changed_at_sql="datetime('now','-2 hours')")
        _seed_reduction('STK_H', 95.0, 90.0,
                        changed_at_sql="datetime('now','-1 hours')")
        _seed_reduction('STK_H', 90.0, 85.0)
        # success=1 な行だけ数えると [85,90,95] の 3 連続 → 発火
        streak = _check_consecutive_reduction_streak('STK_H')
        assert streak is not None
        assert streak['count'] == 3


class TestSpiralAlertDedupeAndDiscord:
    """第 3 安全弁: Discord 通知 + dedupe (_send_discord_spiral_alert)."""

    _CFG = {'ebay': {}, 'discord': {'webhook_url': 'https://discord.test/hook'}}

    @staticmethod
    def _capture_discord(monkeypatch):
        sent = []

        class _FakeNotifier:
            def __init__(self, webhook, bypass_env=False):
                self.webhook = webhook

            def send_message(self, content):
                sent.append(content)
                return True

        monkeypatch.setattr(
            'notifiers.discord_notifier.DiscordNotifier', _FakeNotifier
        )
        return sent

    def test_sends_alert_with_price_history(self, tmp_db, monkeypatch):
        from tasks.task_rival_pricing import _send_discord_spiral_alert
        sent = self._capture_discord(monkeypatch)
        streak = {'count': 3, 'oldest_changed_at': 'x', 'newest_changed_at': 'y',
                  'prices': [85.0, 90.0, 95.0]}
        _send_discord_spiral_alert(self._CFG, 'STK_ALERT_1', streak)
        assert len(sent) == 1
        assert '値下げ合戦アラート' in sent[0]
        assert '$85.00' in sent[0] and '$95.00' in sent[0]

    def test_dedupe_suppresses_second_call_same_day(self, tmp_db, monkeypatch):
        """同一 ebay_item_id への 2 回目通知は同日中 dedupe で抑制される."""
        from tasks.task_rival_pricing import _send_discord_spiral_alert
        sent = self._capture_discord(monkeypatch)
        streak = {'count': 3, 'oldest_changed_at': 'x', 'newest_changed_at': 'y',
                  'prices': [85.0, 90.0, 95.0]}
        _send_discord_spiral_alert(self._CFG, 'STK_ALERT_2', streak)
        _send_discord_spiral_alert(self._CFG, 'STK_ALERT_2', streak)
        assert len(sent) == 1, "同一商品への重複通知が dedupe されていない"

    def test_different_items_not_deduped_against_each_other(self, tmp_db, monkeypatch):
        """別商品への通知は互いに dedupe されない (per-item dedupe)."""
        from tasks.task_rival_pricing import _send_discord_spiral_alert
        sent = self._capture_discord(monkeypatch)
        streak = {'count': 3, 'oldest_changed_at': 'x', 'newest_changed_at': 'y',
                  'prices': [85.0, 90.0, 95.0]}
        _send_discord_spiral_alert(self._CFG, 'STK_ALERT_3', streak)
        _send_discord_spiral_alert(self._CFG, 'STK_ALERT_4', streak)
        assert len(sent) == 2

    def test_no_webhook_no_crash(self, tmp_db, monkeypatch):
        from tasks.task_rival_pricing import _send_discord_spiral_alert
        sent = self._capture_discord(monkeypatch)
        streak = {'count': 3, 'oldest_changed_at': 'x', 'newest_changed_at': 'y',
                  'prices': [85.0, 90.0, 95.0]}
        _send_discord_spiral_alert({'discord': {}}, 'STK_ALERT_5', streak)
        assert sent == []


class TestSpiralAlertIntegration:
    """_evaluate_and_apply_one 経由で reduced 後に spiral streak が検知され
    Discord に飛ぶ end-to-end 確認 (値下げ自体は継続すること = 通知のみ)."""

    _CFG = {'ebay': {}, 'discord': {'webhook_url': 'https://discord.test/hook'}}

    @staticmethod
    def _capture_discord(monkeypatch):
        sent = []

        class _FakeNotifier:
            def __init__(self, webhook, bypass_env=False):
                self.webhook = webhook

            def send_message(self, content):
                sent.append(content)
                return True

        monkeypatch.setattr(
            'notifiers.discord_notifier.DiscordNotifier', _FakeNotifier
        )
        return sent

    def test_third_consecutive_reduction_triggers_alert_and_still_reduces(
            self, tmp_db, monkeypatch):
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        sent = self._capture_discord(monkeypatch)
        _seed_listing('E2E_STK1', current_price=100.0, shipping_cost=0.0,
                      lp_min_price=10.0)
        _seed_competitor('E2E_STK1', 'C1', price_usd=97.0, shipping_usd=0.0)
        # 過去 2 回の成功値下げ (直近 7 日以内) を仕込む
        _seed_reduction('E2E_STK1', 110.0, 105.0,
                        changed_at_sql="datetime('now','-2 hours')")
        _seed_reduction('E2E_STK1', 105.0, 100.0,
                        changed_at_sql="datetime('now','-1 hours')")
        monkeypatch.setattr(
            'monitor.credentials.get_ebay_credentials',
            lambda config: {'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C',
                            'user_token': 'T'}
        )
        monkeypatch.setattr(
            'monitor.credentials.ebay_credentials_ok', lambda creds: True
        )
        monkeypatch.setattr(
            'monitor.ebay_client.revise_fixed_price_item',
            lambda *a, **k: {'success': True, 'ack': 'Success', 'raw': '<ok/>'}
        )
        r = _evaluate_and_apply_one('E2E_STK1', self._CFG)
        # 値下げ自体は止まらない (通知のみ、停止判断は user)
        assert r['action'] == 'reduced'
        assert len(sent) == 1
        assert '値下げ合戦アラート' in sent[0]

    def test_second_consecutive_reduction_does_not_trigger_alert(
            self, tmp_db, monkeypatch):
        """2 回連続 (閾値未満) では通知しない."""
        from tasks.task_rival_pricing import _evaluate_and_apply_one
        sent = self._capture_discord(monkeypatch)
        _seed_listing('E2E_STK2', current_price=100.0, shipping_cost=0.0,
                      lp_min_price=10.0)
        _seed_competitor('E2E_STK2', 'C1', price_usd=97.0, shipping_usd=0.0)
        _seed_reduction('E2E_STK2', 105.0, 100.0,
                        changed_at_sql="datetime('now','-1 hours')")
        monkeypatch.setattr(
            'monitor.credentials.get_ebay_credentials',
            lambda config: {'app_id': 'A', 'dev_id': 'D', 'cert_id': 'C',
                            'user_token': 'T'}
        )
        monkeypatch.setattr(
            'monitor.credentials.ebay_credentials_ok', lambda creds: True
        )
        monkeypatch.setattr(
            'monitor.ebay_client.revise_fixed_price_item',
            lambda *a, **k: {'success': True, 'ack': 'Success', 'raw': '<ok/>'}
        )
        r = _evaluate_and_apply_one('E2E_STK2', self._CFG)
        assert r['action'] == 'reduced'
        assert sent == []
