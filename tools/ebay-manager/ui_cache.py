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
