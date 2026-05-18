"""③同型データ損失 共通ヘルパー回帰テスト (Codex 監査 2026-05-18 由来).

Streamlit は key 付き widget の value= を session_state 既出後無視 → DB
prefill が stale → REPLACE 保存で silent 全消滅 / floor 巻戻し。共通化した
ui_cache.seed_keyed_list_from_db (競合 id 群: app lp_comp_/pm_comp_/
tab_data_fix w119_fix_comp_) と seed_keyed_value_from_db (scalar: app
lp_pyen_/lp_minp_) を **実関数で直接検証** (再実装コピーでなく本物、
drift 防止)。st.session_state は dict 互換のため plain dict で純検証。
"""
from __future__ import annotations

from ui_cache import seed_keyed_list_from_db, seed_keyed_value_from_db

_SLOTS = 10


def _collect_list(ss: dict, prefix: str) -> list[str]:
    vals = [(ss.get(f"{prefix}{i}", "") or "").strip() for i in range(_SLOTS)]
    return [v for v in vals if v]


# ───────── list helper (競合 id 群: A / pm / lp_comp 共通) ─────────

def test_list_initial_seeds_db_values():
    """初回 (widget 不在): DB 登録済が必ず seed (空→全消滅の根治核心)."""
    ss: dict = {}
    seed_keyed_list_from_db(ss, "w119_fix_comp_X_", "_sig_X",
                            ["285000000001", "285000000002"], _SLOTS)
    assert _collect_list(ss, "w119_fix_comp_X_") == [
        "285000000001", "285000000002"]  # NOT [] → upsert で消えない
    assert ss["_sig_X"] == ("285000000001", "285000000002")


def test_list_plain_rerun_preserves_user_input():
    """plain rerun (sig 不変): user 入力途中を温存 (再シードしない)."""
    ss: dict = {}
    seed_keyed_list_from_db(ss, "p_", "_sig", ["111111111111"], _SLOTS)
    ss["p_1"] = "999_typed"
    seed_keyed_list_from_db(ss, "p_", "_sig", ["111111111111"], _SLOTS)
    assert "999_typed" in _collect_list(ss, "p_")


def test_list_db_change_reseeds():
    """別経路で DB 変化 → sig 変化で DB 値へ再シード."""
    ss: dict = {}
    seed_keyed_list_from_db(ss, "p_", "_sig", ["111111111111"], _SLOTS)
    seed_keyed_list_from_db(ss, "p_", "_sig",
                            ["111111111111", "222222222222"], _SLOTS)
    assert _collect_list(ss, "p_") == ["111111111111", "222222222222"]


def test_list_widget_evicted_roundtrip_reseeds():
    """listing 切替で Streamlit が widget 破棄 → 戻り時 widget 不在で
    sig 残存でも DB 再シード (③往復穴の同型ガード、空欄保存全消滅防止)."""
    ss: dict = {}
    seed_keyed_list_from_db(ss, "p_", "_sig", ["111111111111"], _SLOTS)
    for i in range(_SLOTS):
        ss.pop(f"p_{i}", None)          # Streamlit が未描画 widget を破棄
    assert "_sig" in ss                 # sig は plain key で残存
    seed_keyed_list_from_db(ss, "p_", "_sig", ["111111111111"], _SLOTS)
    assert _collect_list(ss, "p_") == ["111111111111"]  # NOT []


def test_list_widget_present_sig_absent_empty_db_reseeds():
    """Codex HIGH エッジ: 旧 value= コード由来で widget あり / sig なし /
    DB 空 の初回移行。stale slot を温存せず空へ再シード (誤って残ると
    保存で全置換 = 既存 active 競合を別経路登録分まで巻込み消滅)."""
    ss: dict = {"p_0": "stale_old_999", "p_1": "stale_old_888"}
    # sig key は未設定 (本修正前 or 旧コードで widget だけ存在した状態)
    seed_keyed_list_from_db(ss, "p_", "_sig", [], _SLOTS)
    assert _collect_list(ss, "p_") == []  # stale が消え空 = DB 真集合と一致
    assert ss["_sig"] == ()


def test_list_deliberate_clear_all_works():
    """user が表示 id を意図的に空欄化 → 保存で削除 は正常 (③同様)."""
    ss: dict = {}
    seed_keyed_list_from_db(ss, "p_", "_sig", ["111111111111"], _SLOTS)
    for i in range(_SLOTS):
        ss[f"p_{i}"] = ""
    seed_keyed_list_from_db(ss, "p_", "_sig", ["111111111111"], _SLOTS)
    assert _collect_list(ss, "p_") == []


# ───────── scalar helper (lp_pyen_/lp_minp_ = B) ─────────

def test_scalar_initial_seeds_db_value():
    """初回: DB 値 (数値) を seed."""
    ss: dict = {}
    seed_keyed_value_from_db(ss, "lp_pyen_X", "_pyen_sig_X", 3000)
    assert ss["lp_pyen_X"] == 3000
    assert ss["_pyen_sig_X"] == 3000


def test_scalar_none_db_value_seeds_none():
    """DB 値 None (仕入価格未設定) → None で seed (number_input 空表示)."""
    ss: dict = {}
    seed_keyed_value_from_db(ss, "lp_pyen_X", "_pyen_sig_X", None)
    assert ss["lp_pyen_X"] is None
    assert "_pyen_sig_X" in ss  # sig も None で記録 (None→値 変化を検知可)


def test_scalar_widget_present_sig_absent_db_none_reseeds():
    """Codex HIGH エッジ (本修正の核心): 旧 value= コード由来で widget
    あり / sig なし / db_value=None。sig_key 不在ガードが無いと None !=
    None が False で stale 旧 floor が残存 → 無編集保存で W183 赤字防止
    floor 巻戻り。必ず DB(None)へ再シードされること."""
    ss: dict = {"k": 9999}  # 旧 stale floor 値が widget に残存、sig は未設定
    seed_keyed_value_from_db(ss, "k", "_s", None)
    assert ss["k"] is None  # stale 9999 が消え DB の None へ追従
    assert "_s" in ss       # sig も確立 (以後の変化検知が効く)


def test_scalar_plain_rerun_preserves_user_edit():
    """plain rerun (DB 不変): user が編集中の値を温存 (再シードしない)."""
    ss: dict = {}
    seed_keyed_value_from_db(ss, "k", "_s", 1000)
    ss["k"] = 1500  # user が手編集
    seed_keyed_value_from_db(ss, "k", "_s", 1000)  # DB 不変
    assert ss["k"] == 1500  # 温存


def test_scalar_db_change_reseeds_from_db():
    """別経路 (自動取得ボタン/supplier sweep) で DB 変化 → DB 値へ再シード
    (stale 旧値のまま保存で W183 floor 巻戻る事故の根治核心)."""
    ss: dict = {}
    seed_keyed_value_from_db(ss, "k", "_s", 1000)
    ss["k"] = 1000  # widget は seed 直後
    seed_keyed_value_from_db(ss, "k", "_s", 2000)  # 別経路で DB 更新
    assert ss["k"] == 2000  # DB 真値へ追従 (古い 1000 が残らない)


def test_scalar_widget_evicted_roundtrip_reseeds():
    """listing 切替で widget 破棄 → 戻り時 widget 不在で DB 再シード."""
    ss: dict = {}
    seed_keyed_value_from_db(ss, "k", "_s", 2500)
    ss.pop("k", None)            # 切替で Streamlit が破棄
    seed_keyed_value_from_db(ss, "k", "_s", 2500)  # sig 残存でも不在で再シード
    assert ss["k"] == 2500
