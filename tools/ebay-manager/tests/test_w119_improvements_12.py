"""W119 ①② 改善 regression (2026-05-18 user 報告由来).

① CLI一括検索チェックが毎回重い → bulk を st.form 化 (チェックは再描画なし、
   submit で 1 回だけ登録)。form submit から呼ばれる _execute_bulk_register が
   「全 listing 選択 0 件」を可視 warning + return する (旧 disabled の form 代替、
   silent skip 防止)。
② 登録済みライバルに価格・送料・合計が出ない → 登録 3 経路で登録直後に
   _fetch_pricing_after_register(refresh_competitor_pricing) を実行。部分失敗
   (429/404) は failed 計上で継続 (silent skip なし)。

本 test は Streamlit runtime を使わず純関数的に核心を固定する:
- _fetch_pricing_after_register が複数 listing を集計し、想定外例外も failed 計上
- _execute_bulk_register が全選択 0 件で warning+return (upsert/_finish 未呼出)
- _execute_bulk_register が登録成功 listing のみ価格取得対象に渡す
- refresh_competitor_pricing の rate_sleep_sec が kw-only / default 0.0
  (= W183 scheduler / 手動再取得の現挙動不変契約)
- app.py の嘘 caption「保存時に Browse API で価格・送料を取得します」が消えた
"""
from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest


class _Bar:
    """st.progress 戻り値の最小 stub."""

    def progress(self, *a, **k):
        return None

    def empty(self):
        return None


def test_fetch_pricing_after_register_aggregates(monkeypatch):
    """複数 listing の fetched/failed を集計して返す."""
    import tabs.tab_research_wizard as w

    calls: list = []

    def fake_refresh(oid, config, *, rate_sleep_sec=0.0):
        calls.append((oid, rate_sleep_sec))
        return {"fetched": 2, "failed": 1}

    monkeypatch.setattr(w, "refresh_competitor_pricing", fake_refresh)
    monkeypatch.setattr(w.st, "progress", lambda *a, **k: _Bar())

    out = w._fetch_pricing_after_register(
        ["A", "B"], {}, show_progress=False
    )

    assert out == {"listings": 2, "fetched": 4, "failed": 2}
    assert [c[0] for c in calls] == ["A", "B"]
    # show_progress=False のとき rate_sleep_sec は default 0.0 で渡る
    assert all(c[1] == 0.0 for c in calls)


def test_fetch_pricing_after_register_exception_counted_as_failed(monkeypatch):
    """1 listing の想定外例外で全体を止めず failed 計上 (silent skip なし)."""
    import tabs.tab_research_wizard as w

    def boom(oid, config, *, rate_sleep_sec=0.0):
        if oid == "B":
            raise sqlite3.OperationalError("db locked")
        return {"fetched": 1, "failed": 0}

    monkeypatch.setattr(w, "refresh_competitor_pricing", boom)

    out = w._fetch_pricing_after_register(
        ["A", "B", "C"], {}, show_progress=False
    )
    # A=成功1 / B=例外→failed+1 / C=成功1
    assert out["listings"] == 3
    assert out["fetched"] == 2
    assert out["failed"] == 1


def test_fetch_pricing_passes_rate_sleep_when_bulk(monkeypatch):
    """一括 (show_progress=True) は rate_sleep_sec を refresh へ伝播."""
    import tabs.tab_research_wizard as w

    seen: list = []
    monkeypatch.setattr(
        w, "refresh_competitor_pricing",
        lambda oid, cfg, *, rate_sleep_sec=0.0: seen.append(rate_sleep_sec)
        or {"fetched": 0, "failed": 0},
    )
    monkeypatch.setattr(w.st, "progress", lambda *a, **k: _Bar())

    w._fetch_pricing_after_register(
        ["A"], {}, show_progress=True, rate_sleep_sec=0.7
    )
    assert seen == [0.7]


def test_execute_bulk_register_zero_selection_warns_and_returns(monkeypatch):
    """① 全 listing 選択 0 件 = 可視 warning + return (旧 disabled 代替).

    upsert / _finish_register / _fetch_pricing は呼ばれない (silent skip 防止
    かつ誤って既存全消滅させない)。"""
    import tabs.tab_research_wizard as w

    warned: list = []
    monkeypatch.setattr(w.st, "warning", lambda m: warned.append(m))

    def must_not_call(*a, **k):
        raise AssertionError("0 選択で呼ばれてはいけない")

    monkeypatch.setattr(w, "upsert_listing_competitors", must_not_call)
    monkeypatch.setattr(w, "_finish_register", must_not_call)
    monkeypatch.setattr(w, "_fetch_pricing_after_register", must_not_call)

    # 全 listing 空選択
    w._execute_bulk_register({"A": [], "B": []}, {})

    assert warned and "チェック" in warned[0]


def test_execute_bulk_register_fetches_pricing_for_registered(monkeypatch):
    """② 登録成功 listing のみ価格取得対象に渡す + msg に価格取得結果."""
    import tabs.tab_research_wizard as w

    monkeypatch.setattr(
        w, "upsert_listing_competitors", lambda **k: None
    )
    fetched_args: dict = {}

    def fake_fetch(ids, config, *, show_progress, rate_sleep_sec=0.0):
        fetched_args["ids"] = list(ids)
        fetched_args["show_progress"] = show_progress
        fetched_args["rate_sleep_sec"] = rate_sleep_sec
        return {"listings": len(ids), "fetched": 3, "failed": 1}

    monkeypatch.setattr(w, "_fetch_pricing_after_register", fake_fetch)

    finished: dict = {}
    monkeypatch.setattr(
        w, "_finish_register",
        lambda level, text: finished.update(level=level, text=text),
    )

    w._execute_bulk_register({"A": ["111111111111"], "B": ["222222222222"]}, {})

    assert fetched_args["ids"] == ["A", "B"]
    assert fetched_args["show_progress"] is True
    assert fetched_args["rate_sleep_sec"] == w._BULK_BROWSE_SLEEP_SEC
    assert finished["level"] == "ok"
    assert "価格取得" in finished["text"]


def test_refresh_competitor_pricing_rate_sleep_contract():
    """refresh_competitor_pricing の rate_sleep_sec は kw-only / default 0.0.

    default 0.0 = W183 scheduler / 手動再取得の現挙動 (sleep なし) 不変契約。
    """
    from monitor.lowest_price import refresh_competitor_pricing

    sig = inspect.signature(refresh_competitor_pricing)
    p = sig.parameters.get("rate_sleep_sec")
    assert p is not None, "rate_sleep_sec パラメータが消えた"
    assert p.kind == inspect.Parameter.KEYWORD_ONLY, "kw-only であるべき"
    assert p.default == 0.0, "default 0.0 (現挙動不変) が変わった"


def test_caption_no_longer_lies():
    """② 最安値チェックタブの嘘 caption が実態整合に修正されたこと (regression).

    W221 Tier2 (2026-06-05): 最安値チェック UI は app.py から
    tabs/tab_lowest_price.py へ移動。検証先を更新。
    """
    app_src = (
        Path(__file__).resolve().parents[1] / "tabs" / "tab_lowest_price.py"
    ).read_text(encoding="utf-8")
    assert "保存時に Browse API で価格・送料を取得します" not in app_src, (
        "未実装の嘘 caption が復活している"
    )
    # 実態整合の文言が存在
    assert "ライバル価格を再取得" in app_src
