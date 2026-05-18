"""Bug A/B regression (2026-05-18 本番不具合, user 報告由来).

Bug A: W119 競合登録後に bump_db_version()+st.rerun が無く、登録済みライバル
       (_cd_competitors_grouped, @st.cache_data ttl=3, get_db_version キー) が
       stale のまま = 「登録したのに反映されない」。
Bug B: 商品データFIX「未FIXのみ」作業中に保存/EBAY反映で再実行 → 新DB値で
       フィルタ再適用 → FIX済になった編集中 listing が一覧脱落 + パネル collapse。

本 test は Streamlit runtime を使わず純関数的に修正の核心を固定する:
- _finish_register が必ず bump_db_version()+st.rerun() を呼び、msg を
  rerun 跨ぎ持越し、パネルを開いたまま維持すること
- _flush_pending_msg / _fix_flush_msg が 1 回 pop で二重表示しないこと
- _apply_filter が本セッション編集 listing を未FIXフィルタで脱落させず
  先頭ピン留めすること
"""
from __future__ import annotations

import pytest


class _Col:
    """st.columns 要素の最小 context manager stub."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_finish_register_bumps_db_version(monkeypatch):
    """Fix1 核心: _finish_register は bump_db_version+rerun を必ず呼ぶ.

    これが無いと _cd_competitors_grouped が ttl=3 内 stale = Bug A 再発.
    """
    import tabs.tab_research_wizard as w

    calls = {"bump": 0, "rerun": 0}
    fake_ss: dict = {}
    monkeypatch.setattr(w.st, "session_state", fake_ss)
    monkeypatch.setattr(w, "bump_db_version",
                        lambda: calls.__setitem__("bump", calls["bump"] + 1))
    monkeypatch.setattr(w.st, "rerun",
                        lambda: calls.__setitem__("rerun", calls["rerun"] + 1))

    w._finish_register("ok", "done")

    assert calls["bump"] == 1, "bump_db_version 未呼出 = Bug A 再発"
    assert calls["rerun"] == 1, "st.rerun 未呼出 = UI 非更新で未反映"
    assert fake_ss[w._PENDING_MSG_KEY] == ("ok", "done"), \
        "完了 msg が rerun を跨いで持ち越されない"
    assert fake_ss[w._WIZARD_OPEN_KEY] is True, \
        "登録後にパネルが畳まれる (Bug A/B 同型 collapse)"


def test_flush_pending_msg_consumes_once(monkeypatch):
    """_flush_pending_msg は 1 回 pop、二重表示しない."""
    import tabs.tab_research_wizard as w

    shown: list = []
    fake_ss = {w._PENDING_MSG_KEY: ("ok", "saved")}
    monkeypatch.setattr(w.st, "session_state", fake_ss)
    monkeypatch.setattr(w.st, "success", lambda m: shown.append(("ok", m)))
    monkeypatch.setattr(w.st, "error", lambda m: shown.append(("err", m)))

    w._flush_pending_msg()
    w._flush_pending_msg()  # 2 回目は no-op

    assert shown == [("ok", "saved")]
    assert w._PENDING_MSG_KEY not in fake_ss


def test_fix_flush_msg_consumes_once(monkeypatch):
    """tab_data_fix の _fix_flush_msg も 1 回 pop."""
    import tabs.tab_data_fix as f

    shown: list = []
    fake_ss = {f._FIX_MSG_KEY: ("ok", "保存しました")}
    monkeypatch.setattr(f.st, "session_state", fake_ss)
    monkeypatch.setattr(f.st, "success", lambda m: shown.append(("ok", m)))
    monkeypatch.setattr(f.st, "error", lambda m: shown.append(("err", m)))

    f._fix_flush_msg()
    f._fix_flush_msg()

    assert shown == [("ok", "保存しました")]
    assert f._FIX_MSG_KEY not in fake_ss


def test_apply_filter_pins_session_edited_listing(monkeypatch):
    """Bug B 核心: 本セッション編集 listing は未FIXフィルタでも脱落させず
    先頭ピン留め (保存直後にページから消える対策)."""
    import tabs.tab_data_fix as f

    listings = [
        {"ebay_item_id": "A", "title": "alpha", "purchase_yen": 3000},  # FIX済
        {"ebay_item_id": "B", "title": "beta", "purchase_yen": None},   # 未FIX
    ]
    fake_ss = {f._FIX_EDITED_KEY: {"A"}}  # A を本セッションで編集済とする
    monkeypatch.setattr(f.st, "session_state", fake_ss)
    monkeypatch.setattr(f.st, "text_input", lambda *a, **k: "")
    monkeypatch.setattr(f.st, "columns", lambda *a, **k: [_Col() for _ in range(5)])
    # 「仕入価格 未 FIX のみ」だけ ON
    monkeypatch.setattr(
        f.st, "checkbox",
        lambda label, **k: k.get("key") == "w119_fix_miss_pyen",
    )
    monkeypatch.setattr(f.st, "selectbox", lambda *a, **k: "ID 順")

    out = f._apply_filter(listings)
    ids = [it["ebay_item_id"] for it in out]

    assert ids and ids[0] == "A", "編集済 A が未FIXフィルタで脱落 = Bug B 再発"
    assert "B" in ids, "未FIX の B は通常通り表示される"


def test_apply_filter_no_pin_when_no_session_edits(monkeypatch):
    """編集履歴なし時は従来通り (未FIXフィルタで FIX済を除外)."""
    import tabs.tab_data_fix as f

    listings = [
        {"ebay_item_id": "A", "title": "alpha", "purchase_yen": 3000},
        {"ebay_item_id": "B", "title": "beta", "purchase_yen": None},
    ]
    monkeypatch.setattr(f.st, "session_state", {})  # 編集履歴なし
    monkeypatch.setattr(f.st, "text_input", lambda *a, **k: "")
    monkeypatch.setattr(f.st, "columns", lambda *a, **k: [_Col() for _ in range(5)])
    monkeypatch.setattr(
        f.st, "checkbox",
        lambda label, **k: k.get("key") == "w119_fix_miss_pyen",
    )
    monkeypatch.setattr(f.st, "selectbox", lambda *a, **k: "ID 順")

    out = f._apply_filter(listings)
    ids = [it["ebay_item_id"] for it in out]
    assert ids == ["B"], "編集履歴なし時は未FIXフィルタが従来通り効く"
