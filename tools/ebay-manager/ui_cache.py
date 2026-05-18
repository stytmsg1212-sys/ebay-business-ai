"""W134 Step2: MonoDeck UI read-cache invalidation token.

体感改善の核心。重い DB ローダは ``@st.cache_data(ttl=3, show_spinner=False)``
でラップし、引数に ``get_db_version()`` を混ぜる。

2 層の安全設計 (2026-05-16 user 判断 = 短 TTL 方式):

1. **ttl=3s = 正しさの保証 (primary safety net)**
   単一巨大 app.py (6400+ 行・全タブ) では「在庫/価格を変える全書込を
   漏れなく bump 配線した」ことを証明できない (reviewer/Codex で +13 → +4 →
   +3 と網羅漏れが次々判明)。そこで invalidation の正しさは bump 網羅では
   なく **短い TTL** で担保する: どの書込で bump を漏らしても、古い在庫数・
   価格・競合数は最悪 3 秒で自動的に最新へ切替わる (金銭直結の誤判断を
   構造的に防止)。

2. ``bump_db_version()`` = 既知ホットパスの即時反映最適化 (secondary)
   在庫確定 (W133) / PM 保存 / 最安値保存 等、operator が即座に結果を
   見たい既知経路でのみ呼ぶ。呼ぶと cache key が進み次回 read が即 recompute
   = 0 秒反映。**未配線でも correctness 違反ではなく「最大 3 秒の遅延」に
   縮退する** (= 網羅を追いかけ続ける必要がない)。

設計上の約束 (Q1 / 金銭データ stale 防止):
- **読み取り関数だけ** を cache する。書き込み関数は一切 cache しない。
- **SQLite 接続を ``st.cache_resource`` で共有しない** (rerun 跨ぎで書込
  接続を共有すると lock / 中途半端な commit が起きるため。本モジュールは
  接続を一切保持しない)。
- ``get_db_version()`` を渡す引数は **先頭 ``_`` を付けない**
  (``st.cache_data`` は ``_`` 始まり引数を hash 対象外にするため。token を
  cache key に含めるには通常引数である必要)。
"""

import streamlit as st

_KEY = "_w134_db_version"


def get_db_version() -> int:
    """現在の DB バージョン (cache key 用)。未初期化なら 0。"""
    return int(st.session_state.get(_KEY, 0))


def bump_db_version() -> None:
    """既知書込ホットパスで呼ぶ任意の即時反映最適化。

    呼べば次回 cached read が 0 秒で recompute。呼ばなくても ttl=3s で
    最大 3 秒後に自動更新されるため、欠落は correctness 違反にならない。
    """
    st.session_state[_KEY] = get_db_version() + 1


# =============================================================================
# ③同型データ損失 共通修正ヘルパー (2026-05-18、K1: 3rd occurrence で共通化)
# =============================================================================
# Streamlit は key 付き widget の value= を「その key が session_state に既出の
# 後」無視する。DB 値を value= で prefill する keyed widget が、(a) listing
# 切替で Streamlit が未描画 widget state を破棄 (b) 初回空表示後 の経路で
# stale (空/旧値) のまま表示され、その値で DB を REPLACE 保存すると登録済み
# データが silent 全消滅・誤上書きする (③: app.py lp_comp_ / tab_product_
# management pm_comp_ / tab_data_fix w119_fix_comp_ / app.py lp_pyen_ 等)。
# 対策 = DB 値を signature 化し、widget key 不在 (=切替で破棄/初回) OR
# signature 変化 の時だけ session_state を DB 値で再シード。plain rerun
# (signature 不変) では再シードせず user 入力途中を温存。意図的 clear
# (表示された値を user が空に) は再シードされず削除として機能する。
# 呼出側は widget から value= を撤去し session_state[key] を唯一の真実源に。


def seed_keyed_list_from_db(
    session_state, widget_prefix: str, sig_key: str,
    values: list, slots: int,
) -> None:
    """list 型 DB 値 (競合 id 群等) を slot 数ぶん keyed widget へ再シード.

    widget key = f"{widget_prefix}{i}" (i=0..slots-1)。widget_prefix は
    末尾に区切りを含めて渡す (例 f"pm_comp_{eid}_")。純関数 (Streamlit
    非依存、session_state は dict 互換) でテスト可能。
    """
    db_sig = tuple(values)
    widget_present = f"{widget_prefix}0" in session_state
    # Codex 監査 HIGH: sig_key 不在 (= 旧 value= コード由来 or 本修正前に
    # 既に widget state があった初回移行) を再シード条件に必ず含める。
    # 含めないと「widget あり / sig なし / DB が空」で session_state.get(
    # sig)=None と db_sig の比較が誤って False になり stale が残る。
    if (not widget_present
            or sig_key not in session_state
            or session_state.get(sig_key) != db_sig):
        for i in range(slots):
            session_state[f"{widget_prefix}{i}"] = (
                values[i] if i < len(values) else ""
            )
        session_state[sig_key] = db_sig


def seed_keyed_value_from_db(
    session_state, widget_key: str, sig_key: str, db_value,
) -> None:
    """scalar 型 DB 値 (仕入価格/下限価格等) を 1 keyed widget へ再シード.

    db_value は数値 or None (number_input の「空」)。signature = DB 値
    そのもの (別経路で DB が変われば再シード、user 入力途中は温存)。
    """
    # Codex 監査 HIGH: sig_key 不在 を必ず再シード条件に含める。含めないと
    # 「widget あり (旧 value= コード由来) / sig なし / db_value=None」で
    # None != None が False になり stale 旧 floor が残存 → 無編集保存で
    # W183 赤字防止 floor が巻戻る (NULL 移行ケースの根治漏れ)。
    if (widget_key not in session_state
            or sig_key not in session_state
            or session_state.get(sig_key) != db_value):
        session_state[widget_key] = db_value
        session_state[sig_key] = db_value
