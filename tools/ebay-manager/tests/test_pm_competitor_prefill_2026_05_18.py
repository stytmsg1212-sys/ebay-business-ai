"""W183 競合 silent 全消滅 回帰テスト (商品管理タブ ③同型、user 実報告由来).

機序: tab_product_management の `st.text_input(key=..., value=cur)` の value=
が Streamlit 仕様で「その key が session_state 既出の後」無視 → DB 登録済
competitor が編集欄空欄 → `pm_comp_list_{eid}`=[] → 💾DB保存/📤eBay反映 が
`upsert_listing_competitors(eid, [])` 全置換で active 競合全消滅 (W183 自動
値下げ追従対象の恒久損失=金銭直結)。
fix = DB id 集合を signature 化し (widget 不在=listing切替で破棄/初回) OR
sig 変化 の時だけ session_state を DB 値で再シード。

本 test は **実コードの `_pm_seed_comp_session` を直接呼ぶ** (再実装コピー
ではなく本物を検証 = drift 防止)。`st.session_state` は dict 互換のため
plain dict を渡して Streamlit 非依存で機序を固定する。
"""
from __future__ import annotations

import pytest

from tabs.tab_product_management import _pm_seed_comp_session

_MAX = 10


def _collect(ss: dict, eid: str) -> list[str]:
    """render ループの収集 (st.text_input が session_state[key] を返す) と
    `[c for c in comp_inputs if c]` 相当を再現。実 widget は Streamlit
    依存のため、seed 後の session_state からの収集部のみ模す。"""
    vals = [
        (ss.get(f"pm_comp_{eid}_{i}", "") or "").strip()
        for i in range(_MAX)
    ]
    return [v for v in vals if v]


def test_initial_render_seeds_db_competitors():
    """初回 (widget 不在): DB 登録済が必ず seed される (空 → 全消滅の根治核心)."""
    ss: dict = {}
    _pm_seed_comp_session(ss, "111", ["285000000001", "285000000002"], _MAX)
    assert _collect(ss, "111") == ["285000000001", "285000000002"]  # NOT []
    assert ss["_pm_comp_loaded_sig_111"] == ("285000000001", "285000000002")


def test_plain_rerun_preserves_user_input_when_sig_unchanged():
    """plain rerun (sig 不変): user 入力途中を温存 (再シードしない)."""
    ss: dict = {}
    _pm_seed_comp_session(ss, "111", ["285000000001"], _MAX)
    ss["pm_comp_111_1"] = "999_user_typed"  # user が手入力
    _pm_seed_comp_session(ss, "111", ["285000000001"], _MAX)  # DB 不変
    assert "999_user_typed" in _collect(ss, "111")  # 温存される


def test_db_change_reseeds_from_db_truth():
    """別経路 (最安値チェック登録/W184 等) で DB 変化 → sig 変化で再シード."""
    ss: dict = {}
    _pm_seed_comp_session(ss, "111", ["285000000001"], _MAX)
    _pm_seed_comp_session(
        ss, "111", ["285000000001", "285000000099"], _MAX
    )
    assert _collect(ss, "111") == ["285000000001", "285000000099"]


def test_listing_switch_then_back_reseeds_when_widget_evicted():
    """③往復穴: listing 切替で Streamlit が pm_comp_* を破棄 → 戻り時
    widget 不在で必ず DB 再シード (空欄→保存で全消滅の往復再発を防ぐ)."""
    ss: dict = {}
    _pm_seed_comp_session(ss, "AAA", ["111111111111", "222222222222"], _MAX)
    # listing 切替を模す: Streamlit が AAA の widget state を破棄
    for i in range(_MAX):
        ss.pop(f"pm_comp_AAA_{i}", None)
    # sig key は plain key で残存 (③ で検出した往復穴の条件)
    assert "_pm_comp_loaded_sig_AAA" in ss
    # AAA に戻る: widget 不在検知で sig 残存でも再シードされる
    _pm_seed_comp_session(ss, "AAA", ["111111111111", "222222222222"], _MAX)
    assert _collect(ss, "AAA") == ["111111111111", "222222222222"]  # NOT []


def test_deliberate_clear_all_still_works():
    """user が表示 id を意図的に空欄化 → 保存で削除 は正常 (③同様、
    『見えてる物を消すのは正常』。sig 不変なので再シードしない)."""
    ss: dict = {}
    _pm_seed_comp_session(ss, "111", ["285000000001", "285000000002"], _MAX)
    for i in range(_MAX):
        ss[f"pm_comp_111_{i}"] = ""  # user が全消し (意図的)
    _pm_seed_comp_session(ss, "111", ["285000000001", "285000000002"], _MAX)
    assert _collect(ss, "111") == []  # 意図的削除は機能する


def test_per_eid_namespace_isolation():
    """eid 別 key/sig: 別 listing が独立 seed され相互干渉しない."""
    ss: dict = {}
    _pm_seed_comp_session(ss, "AAA", ["111111111111"], _MAX)
    _pm_seed_comp_session(ss, "BBB", ["222222222222"], _MAX)
    assert _collect(ss, "AAA") == ["111111111111"]
    assert _collect(ss, "BBB") == ["222222222222"]
    assert ss["_pm_comp_loaded_sig_AAA"] != ss["_pm_comp_loaded_sig_BBB"]
