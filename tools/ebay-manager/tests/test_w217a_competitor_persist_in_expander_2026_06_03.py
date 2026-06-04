"""W217-A 競合グリッド expander 化後も DB 競合が保持される回帰テスト.

# 背景

W217 (2026-06-03) で商品管理タブの「ライバル item id 編集グリッド (5x2)」
が `st.expander("...", expanded=False)` でラップされた (誤操作保護目的).
expander 折りたたみは UI 上の可視性のみ変える Streamlit 仕様で、body
の python code は毎 rerun 実行される (= seed/集計は折りたたみ時も走る) が、
過去にこの箇所は「signature 駆動再シードが効かず空欄保存で全消失」
事故 (③同型、2026-05-18) を起こした金銭直結 path のため、expander 導入
後も**挙動が完全に変わっていない**ことを DB 層まで含めて固定する.

# 既存テストとの分担

`tests/test_pm_competitor_prefill_2026_05_18.py` は `_pm_seed_comp_session`
純関数の seed/再シード/eviction 検証 (DB 非依存、6 case). 本 test は
重複を避け **DB 統合 end-to-end** に絞る:
  「DB登録3件 → seed → 集計 → upsert → DB が 3件のまま (0件化しない)」
expander の中に置かれても seed/集計/upsert の整合が壊れないことを実 DB
SELECT で確定 (もし将来誰かが expander 仕様変更で body 非実行化させたら
本 test が落ちる).

# Streamlit 非依存

実 widget (`st.text_input`) は Streamlit runtime 依存のため、`st.session_state`
は plain dict で模す (既存 prefill test と同方式). seed 関数と
`upsert_listing_competitors` は**実コードを直接呼ぶ** (drift 防止).
"""
from __future__ import annotations

import pytest

from tabs.tab_product_management import _pm_seed_comp_session

_MAX = 10


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """既存 tests/test_w98_lowest_price.py と同方式の DB_PATH 差し替え."""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _insert_listing(conn, eid: str, sku: str = "ebay_test_001"):
    conn.execute(
        "INSERT INTO ebay_listings (ebay_item_id, sku, title, is_ended) "
        "VALUES (?, ?, 'test', 0)",
        (eid, sku),
    )


def _collect(ss: dict, eid: str) -> list[str]:
    """render ループの収集 (= 実コード L1755-1762 と等価).

    ```
    val = st.text_input(..., key=f"pm_comp_{eid}_{idx}", ...)
    comp_inputs.append(val.strip())
    st.session_state[f"pm_comp_list_{eid}"] = [c for c in comp_inputs if c]
    ```
    """
    vals = [
        (ss.get(f"pm_comp_{eid}_{i}", "") or "").strip()
        for i in range(_MAX)
    ]
    return [v for v in vals if v]


def _active_competitors(conn, eid: str) -> list[str]:
    """DB から active 競合を返す (実 upsert_listing_competitors と同 SELECT)."""
    rows = conn.execute(
        "SELECT competitor_item_id FROM competitor_products "
        "WHERE our_item_id=? AND is_active=1 "
        "ORDER BY competitor_item_id",
        (eid,),
    ).fetchall()
    return [r[0] for r in rows]


def test_expander_collapsed_path_preserves_db_competitors(tmp_db):
    """⭐ 主検証: expander 折りたたみ時 (= body 毎 rerun 実行) も DB 競合が
    保持される. 「seed → 集計 → upsert」の end-to-end で 3件→3件 を確認.

    W183 ③同型事故 (空欄→0件 全置換消滅) の expander 越し再発を防ぐ.
    """
    from monitor.database import get_conn
    from monitor.lowest_price import upsert_listing_competitors

    eid = "300000000001"
    seed_ids = ["285000000001", "285000000002", "285000000003"]

    # 事前 setup: listing + active 3件をDB登録
    with get_conn() as c:
        _insert_listing(c, eid)
        for cid in seed_ids:
            c.execute(
                "INSERT INTO competitor_products "
                "(our_item_id, competitor_item_id, price_rule, "
                " min_price, max_discount, is_active) "
                "VALUES (?, ?, 'competitor - 0.01', 0.0, 10.0, 1)",
                (eid, cid),
            )

    # === expander 折りたたみ rerun を模す ===
    # Streamlit 仕様: expander の expanded=False は **UI 可視性のみ**変える.
    # body の python code (seed + render ループ + 集計) は毎 rerun 実行される.
    # 折りたたみ rerun でも下記が走るのが正常動作.
    ss: dict = {}
    _pm_seed_comp_session(ss, eid, seed_ids, _MAX)  # body 内 seed
    comp_inputs = _collect(ss, eid)                  # body 内 集計
    ss[f"pm_comp_list_{eid}"] = comp_inputs

    # seed と集計が DB 値どおりに走る = 折りたたみ時も body は実行される
    assert comp_inputs == seed_ids, (
        "expander 折りたたみで body が実行されない仕様変更があった可能性. "
        "DB 競合が session_state に seed されず、保存で全消失する."
    )
    assert ss[f"pm_comp_list_{eid}"] == seed_ids

    # === user が 💾DB保存 押下を模す ===
    upsert_listing_competitors(eid, ss[f"pm_comp_list_{eid}"])

    # ⭐ 核心: DB active が 3件のまま (0件化しない = ③同型事故が再発していない)
    with get_conn() as c:
        active = _active_competitors(c, eid)
    assert active == seed_ids, (
        f"競合が DB から消失. 期待 {seed_ids}, 実際 {active}. "
        "W183 ③同型事故 (空欄→全消滅) が expander 越しに再発した可能性."
    )


def test_expander_re_open_after_listing_switch_recovers_competitors(tmp_db):
    """③往復穴: listing 切替で widget state が破棄 → 戻り時に widget 不在
    検知で再シードされ、expander 内 集計が DB 値に復活する.

    expander の expanded=False 状態でも上記 recovery が機能することを担保.
    """
    from monitor.database import get_conn
    from monitor.lowest_price import upsert_listing_competitors

    eid = "300000000002"
    seed_ids = ["285000000010", "285000000011"]

    with get_conn() as c:
        _insert_listing(c, eid, sku="ebay_test_002")

    # 初回 expander 開時: seed → 集計 → 保存
    ss: dict = {}
    _pm_seed_comp_session(ss, eid, [], _MAX)  # 初回 DB 空 (まだ何も保存していない)
    # user が編集欄に手入力 (expander 内で入力 → 折りたたんで保存)
    for i, cid in enumerate(seed_ids):
        ss[f"pm_comp_{eid}_{i}"] = cid
    ss[f"pm_comp_list_{eid}"] = _collect(ss, eid)
    upsert_listing_competitors(eid, ss[f"pm_comp_list_{eid}"])

    # listing 切替: Streamlit が widget state (pm_comp_eid_*) を破棄
    for i in range(_MAX):
        ss.pop(f"pm_comp_{eid}_{i}", None)
    ss.pop(f"pm_comp_list_{eid}", None)
    # sig key は残存 (③で検出した条件)
    assert f"_pm_comp_loaded_sig_{eid}" in ss

    # === 戻る: expander 折りたたみ状態でも body 走行 → seed 復活 ===
    with get_conn() as c:
        current = _active_competitors(c, eid)
    _pm_seed_comp_session(ss, eid, current, _MAX)
    comp_inputs = _collect(ss, eid)
    ss[f"pm_comp_list_{eid}"] = comp_inputs

    assert comp_inputs == seed_ids, (
        "listing 切替→戻りで競合が空のまま. widget 不在検知の再シード "
        "が expander 内で動かなくなった可能性."
    )

    # ここで意図せず再保存しても DB が壊れない (3件→3件)
    upsert_listing_competitors(eid, ss[f"pm_comp_list_{eid}"])
    with get_conn() as c:
        assert _active_competitors(c, eid) == seed_ids
