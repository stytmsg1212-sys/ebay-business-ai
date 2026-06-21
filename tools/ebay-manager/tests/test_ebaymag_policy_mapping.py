"""eBaymag 送料ポリシー canonical 値マッピング + migration のテスト (Phase1)。

設計書: `.company/engineering/docs/2026-06-21-ebaymag-shipping-policy-automation-design.md`

テスト方針:
  - band_for_weight_g: 境界値 / 異常値
  - build_canonical_policy: **実 manifest を使用** (本番値の整合も同時検証)。
    fixture でなく実ファイルを読むことで、manifest 更新時に値ズレを検知できる。
  - migration 冪等: init_db を 2 回連続実行してデータが保持されることを確認
    (db-migration-rules.md の必須冪等性テスト)。

DB は conftest.py の autouse fixture で tmp_path 配下に隔離される。
"""
from __future__ import annotations

import json

import pytest

from monitor.ebaymag_policy_mapping import (
    band_for_weight_g,
    build_canonical_policy,
    _load_zone_definitions,
    _BAND_UPPER_KG,
)
from monitor.database import init_db, get_conn


# ----------------------------------------------------------------------------
# config 定数との乖離検知 (reviewer LOW / cascade-update.md)
# ----------------------------------------------------------------------------

def test_band_upper_kg_matches_shipping_config():
    """ebaymag_policy_mapping._BAND_UPPER_KG が shipping_rate_batch.config.BAND_UPPER_KG
    と完全一致すること。monitor→scripts 逆 import 回避で複製しているため、config 側
    変更時に本テストが乖離を検知する (cascade-update.md / §15 MED-1)。"""
    from scripts.shipping_rate_batch.config import BAND_UPPER_KG as CONFIG_BAND_UPPER_KG
    assert _BAND_UPPER_KG == CONFIG_BAND_UPPER_KG, (
        "ebaymag_policy_mapping._BAND_UPPER_KG が config.BAND_UPPER_KG と乖離。"
        "config 変更時は mapping 側の複製も追従させること (cascade)。"
    )


# ----------------------------------------------------------------------------
# band_for_weight_g
# ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "weight_g, expected",
    [
        (1, "0-0.5kg"),       # 最小
        (500, "0-0.5kg"),     # 0.5kg 境界 (上限以下)
        (501, "0.5-1kg"),     # 境界超え → 次帯
        (1000, "0.5-1kg"),    # 1kg 境界
        (1001, "1-2kg"),
        (2000, "1-2kg"),      # 2kg 境界
        (2001, "2-3kg"),
        (3000, "2-3kg"),
        (8000, "6-8kg"),      # 8kg 境界
        (8001, "8-10kg"),
        (20000, "10-20kg"),   # 最重帯 上限
        (50000, "10-20kg"),   # 上限超 → 最重帯に丸め
    ],
)
def test_band_for_weight_g_boundaries(weight_g, expected):
    assert band_for_weight_g(weight_g) == expected


@pytest.mark.parametrize("bad", [None, 0, -100, "abc"])
def test_band_for_weight_g_invalid_raises(bad):
    # Q0: 無効重量を黙って最小帯に落とさず ValueError で伝播
    with pytest.raises(ValueError):
        band_for_weight_g(bad)


# ----------------------------------------------------------------------------
# build_canonical_policy (実 manifest)
# ----------------------------------------------------------------------------

# eBaymag タブ別期待値 (2026-06-21 実機確定 / compute で AU=$62 逆算検証済)。
# AU=zone11 / Europe=zone6 / Canada=zone5。
_EXPECTED_AU = {"1-2kg": 0, "2-3kg": 8, "6-8kg": 62, "10-20kg": 92}
_EXPECTED_EU = {"1-2kg": 9, "2-3kg": 13, "6-8kg": 0, "10-20kg": 123}
_EXPECTED_CA = {"1-2kg": 1, "2-3kg": 3, "6-8kg": 11, "10-20kg": 26}


@pytest.mark.parametrize("band", ["1-2kg", "2-3kg", "6-8kg", "10-20kg"])
def test_build_canonical_us_zero(band):
    """US タブは本体課金で必ず $0 固定 (二重課金回避、§4/§6)。"""
    policy = build_canonical_policy(band)
    assert policy["tab_values"]["US"] == 0


@pytest.mark.parametrize("band", ["1-2kg", "2-3kg", "6-8kg", "10-20kg"])
def test_build_canonical_au_zone11(band):
    """Australia タブ = zone11 値。"""
    policy = build_canonical_policy(band)
    assert policy["tab_values"]["Australia"] == _EXPECTED_AU[band]


@pytest.mark.parametrize("band", ["1-2kg", "2-3kg", "6-8kg", "10-20kg"])
def test_build_canonical_eu_zone6(band):
    """Europe タブ = zone6(EU) 値 (UK/DE/IT/FR/ES を一括)。"""
    policy = build_canonical_policy(band)
    assert policy["tab_values"]["Europe"] == _EXPECTED_EU[band]


@pytest.mark.parametrize("band", ["1-2kg", "2-3kg", "6-8kg", "10-20kg"])
def test_build_canonical_canada_zone5(band):
    """Canada タブ = zone5 値 (DHL SpeedPAK で CA=zone5、2026-06-21 実機確定)。"""
    policy = build_canonical_policy(band)
    assert policy["tab_values"]["Canada"] == _EXPECTED_CA[band]


@pytest.mark.parametrize("band", ["1-2kg", "2-3kg", "6-8kg", "10-20kg"])
def test_build_canonical_no_asia(band):
    """Asia は eBaymag 対象外 (user 確定 2026-06-21) → tab に存在しない。"""
    policy = build_canonical_policy(band)
    assert "Asia" not in policy["tab_values"]
    assert policy["worldwide_free"] is True


@pytest.mark.parametrize("band", ["1-2kg", "2-3kg", "6-8kg", "10-20kg"])
def test_build_canonical_excluded_from_zones(band):
    """除外国は zone4/7/8/9 の iso[] から展開される (手書きでない、§6)。"""
    policy = build_canonical_policy(band)
    excluded = set(policy["excluded_countries"])
    zone_iso = _load_zone_definitions()
    expected = set()
    for z in (4, 7, 8, 9):
        expected.update(zone_iso[z])
    assert excluded == expected
    # 主要な高コスト国が含まれること (回帰用の明示アサート)
    for iso in ("IND", "ISR", "ISL", "KAZ"):
        assert iso in excluded


def test_build_canonical_unknown_band_raises():
    with pytest.raises(ValueError):
        build_canonical_policy("99-100kg")


# ----------------------------------------------------------------------------
# migration 冪等 (init_db 2 回でデータ保持)
# ----------------------------------------------------------------------------

def test_migration_idempotent_data_preserved():
    """init_db を 2 回連続実行してもデータが消えない (db-migration-rules.md)。"""
    init_db()

    # v79 テーブル / v80 列が存在すること
    with get_conn() as conn:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='ebaymag_shipping_policies'"
        ).fetchone()
        assert tbl is not None, "v79 table 未作成"

        cols = {
            r[1] for r in conn.execute(
                "PRAGMA table_info(ebay_listings)"
            ).fetchall()
        }
        for c in (
            "ebaymag_shipping_band",
            "ebaymag_policy_applied_at",
            "ebaymag_applied_policy_token",
        ):
            assert c in cols, f"v80 列 {c} 未追加"

        # データ投入
        conn.execute(
            """
            INSERT INTO ebaymag_shipping_policies
                (band, policy_title, site_values_json, region_values_json,
                 excluded_countries_json, source_run_id, status)
            VALUES ('6-8kg', 'DDP_6-8kg', '{"US":0,"AU":62}', '{"Europe":0}',
                    '["IND"]', 'test_run', 'draft')
            """
        )

    # 2 回目の init_db でもデータ保持
    init_db()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM ebaymag_shipping_policies"
        ).fetchone()
        assert rows[0] >= 1, "init_db 再実行でデータ消失 (冪等性違反)"


def test_unique_band_status_constraint():
    """UNIQUE(band, status): 同帯・同 status の二重 INSERT は弾かれる。"""
    import sqlite3

    init_db()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ebaymag_shipping_policies (band, status)
            VALUES ('1-2kg', 'draft')
            """
        )
    with get_conn() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ebaymag_shipping_policies (band, status)
                VALUES ('1-2kg', 'draft')
                """
            )


def test_canonical_json_serializable():
    """canonical dict が JSON 直列化可能 (one-shot が DB / file へ書ける前提)。"""
    policy = build_canonical_policy("6-8kg")
    s = json.dumps(policy)
    loaded = json.loads(s)
    assert "tab_values" in loaded
    assert loaded["tab_values"]["Canada"] == 11
