# -*- coding: utf-8 -*-
"""board#19 (2026-06-13) 回帰: 在庫監視タブの即時探索が無反応になる問題の再発防止.

4 層の根治を固定:
(a) bg thread の st.session_state 書込が ScriptRunContext 無しで mock に
    fallback し flag が永遠に下りなかった → add_script_run_ctx 結線必須
(b) 探索成功でも persisted=0 だと UI が完全無表示だった → 成功時内訳表示必須
(c) INSERT OR IGNORE dedup (不採用済みと同一 URL) が無音だった
    → skipped_existing カウンタ必須
(d) Streamlit プロセス (WindowsSelectorEventLoopPolicy) 内では Playwright が
    NotImplementedError で起動不可 → search_* が握りつぶして「偽の市場 0 件」
    → run_supplier_candidate_search 全体を subprocess で実行 (W228 FIX-E と同根)
"""
import io
import json
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_TAB = _BASE / "tabs" / "tab_inventory_monitor.py"
_TASK = _BASE / "tasks" / "task_supplier_candidate_search.py"
_CLI = _BASE / "tasks" / "supplier_search_cli.py"


def test_bg_threads_get_script_run_ctx():
    """(a) 即時探索の bg thread 2 本とも ScriptRunContext を移植してから start."""
    src = _TAB.read_text(encoding="utf-8")
    # 一括探索 thread
    assert "_add_ctx_bulk(_t_bulk, _get_ctx_bulk())" in src
    # 個別探索 thread
    assert "_add_ctx(_t_cs, _get_ctx())" in src
    # ctx 無し直接 start の旧パターンが復活していないこと
    assert "Thread(target=_bg_bulk_search, daemon=True).start()" not in src
    assert "Thread(target=_bg_cs, daemon=True).start()" not in src


def test_search_success_with_zero_persisted_shows_breakdown():
    """(b) 成功 + 新規0件でも内訳 caption が出る分岐が存在する."""
    src = _TAB.read_text(encoding="utf-8")
    assert "新規候補なし" in src
    assert '_last_result.get("ok")' in src
    # 内訳の構成要素
    assert "類似度基準未満" in src
    assert "既存/不採用済みと同一" in src


def test_flag_set_before_thread_start():
    """HIGH-1: flag True / result クリアは thread.start() より前 (race 防止).

    爆速完了 thread の finally: flag=False を main が後から True で上書きすると
    flag を下ろす者が居なくなり「実行中…」恒久 stuck になる.
    """
    src = _TAB.read_text(encoding="utf-8")
    # bulk 側: flag 代入 → result pop → start の順
    _i_flag_b = src.index("st.session_state[_bulk_search_flag] = True")
    _i_start_b = src.index("_t_bulk.start()")
    assert _i_flag_b < _i_start_b
    assert src.index("st.session_state.pop(_bulk_search_result, None)") < _i_start_b
    # 個別側: flag 代入 → start の順
    _i_flag_i = src.index("st.session_state[_flag_k] = True")
    _i_start_i = src.index("_t_cs.start()")
    assert _i_flag_i < _i_start_i


def test_persisted_alt_counted_unconditionally():
    """HIGH-2: alt=1 persist 行を score 条件なしで計数し返り値に含める.

    alt_listing_possible=1 はカード一覧 (alt=0 filter) に出ないため、
    「ページ更新で上に表示されます」の対象から除外して虚偽表示を防ぐ.
    """
    src = _TASK.read_text(encoding="utf-8")
    assert "persisted_alt += 1" in src
    assert "'persisted_alt': persisted_alt" in src
    # UI 側が main/alt を区別して表示している
    ui = _TAB.read_text(encoding="utf-8")
    assert '"alt": int(r.get("persisted_alt") or 0)' in ui
    assert "別SKU出品機会として" in ui


def test_run_supplier_candidate_search_counts_skipped_existing():
    """(c) dedup (row_id=None) を skipped_existing として計数し返り値に含める."""
    src = _TASK.read_text(encoding="utf-8")
    assert "skipped_existing += 1" in src
    assert "'skipped_existing': skipped_existing" in src
    # message にも含める (UI / scheduler.log 双方で見えること)
    assert "skipped_existing={skipped_existing}" in src


# ---------------------------------------------------------------------------
# (d) subprocess 化 — Streamlit プロセス内 Playwright 起動不可の根治
# ---------------------------------------------------------------------------

def test_ui_search_runs_in_subprocess():
    """(d) UI 即時探索 2 箇所とも直接呼出ではなく subprocess helper を使う.

    直接呼出 (run_supplier_candidate_search を Streamlit プロセス内で実行) は
    Playwright NotImplementedError → search_* が握りつぶし → 偽 found=0 になる。
    """
    src = _TAB.read_text(encoding="utf-8")
    assert "tasks.supplier_search_cli" in src
    # def 1 + 一括/個別の呼出 2 = 3 箇所以上
    assert src.count("_run_candidate_search_in_subprocess(") >= 3
    # 直接呼出 import の復活防止
    assert "run_supplier_candidate_search as _run_cs" not in src


def test_supplier_search_cli_marker_output(monkeypatch, capsys):
    """(d) CLI は RESULT_JSON: マーカー付き JSON を stdout 最終行に出す."""
    import tasks.supplier_search_cli as cli

    monkeypatch.setattr(
        cli, "_run",
        lambda eid, sku, via: {"ok": True, "result": {"success": True, "found": 2}},
    )
    monkeypatch.setattr(sys, "argv", ["supplier_search_cli", "123456789012", "ui_on_demand"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("stock:01\n"))
    cli.main()
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("RESULT_JSON:")]
    assert lines, f"RESULT_JSON 行なし: {out!r}"
    payload = json.loads(lines[-1][len("RESULT_JSON:"):])
    assert payload["ok"] is True
    assert payload["result"]["found"] == 2


def test_subprocess_env_error_distinct_from_market_zero(monkeypatch):
    """(d) Q0: 環境エラー (exit!=0) を success=False で返し市場0件と区別する."""
    import subprocess
    from tabs.tab_inventory_monitor import _run_candidate_search_in_subprocess

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "NotImplementedError: ..."

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    r = _run_candidate_search_in_subprocess("123456789012", "stock:01", "ui_on_demand")
    assert r["success"] is False
    assert "探索プロセス" in r["message"]


def test_subprocess_parses_marker_among_noise(monkeypatch):
    """(d) pipeline の print/log で stdout が汚れても結果行を拾える."""
    import subprocess
    from tabs.tab_inventory_monitor import _run_candidate_search_in_subprocess

    _payload = {"ok": True,
                "result": {"success": True, "found": 3, "persisted": 1,
                           "persisted_alt": 0, "message": "ok"}}

    class _Proc:
        returncode = 0
        stdout = "some noise\nRESULT_JSON:" + json.dumps(_payload, ensure_ascii=False) + "\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    r = _run_candidate_search_in_subprocess("123456789012", "stock:01", "ui_on_demand")
    assert r["success"] is True
    assert r["found"] == 3
    assert r["persisted"] == 1
