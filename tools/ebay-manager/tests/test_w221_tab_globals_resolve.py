#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W221 Tier2: 抽出タブの render 関数が参照する名前が全て解決できることを検証。

出典: 2026-06-05 code-reviewer 指摘。app.py のインライン分岐を tabs/ へ抽出した際、
分岐 body が app.py の top-level import を **グローバル参照** していたため、抽出先
モジュールに import が無いと render 実行時に NameError でタブがクラッシュする。
import smoke (モジュール import だけ) では render を呼ばないため検出できなかった
(= Q1 実機検証が必須な理由)。本 test は AST で「関数内で Load されるが、ローカル
束縛・モジュールグローバル・builtins のいずれにも無い名前」を静的検出し、
NameError を事前に弾く番人。
"""
from __future__ import annotations

import ast
import builtins
import importlib
import pathlib

import pytest

_TABS = pathlib.Path(__file__).resolve().parent.parent / "tabs"

# (module, render 関数名) — W221 で app.py から抽出した 12 タブ
_TARGETS = [
    ("tab_sku_conversion", "render_sku_conversion_tab"),
    ("tab_video_learning", "render_video_learning_tab"),
    ("tab_agent_monitor", "render_agent_monitor_tab"),
    ("tab_model_comparison", "render_model_comparison_tab"),
    ("tab_profit_calc", "render_profit_calc_tab"),
    ("tab_customs", "render_customs_tab"),
    ("tab_ebay_sync", "render_ebay_sync_tab"),
    ("tab_manual_run", "render_manual_run_tab"),
    ("tab_lowest_price", "render_lowest_price_tab"),
    ("tab_supplier_candidates", "render_supplier_candidates_tab"),
    ("tab_inventory_monitor", "render_inventory_monitor_tab"),
    ("tab_dashboard", "render_dashboard_tab"),
]

_BUILTINS = set(dir(builtins))


def _collect_bound(func: ast.FunctionDef) -> set[str]:
    """関数ツリー内でローカル束縛される名前を (過大近似で) 収集。

    引数 (本体/nested func/lambda) / import / 代入 Store / for / with-as /
    except-as / comprehension target を全て束縛とみなす。
    """
    bound: set[str] = set()
    for n in ast.walk(func):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = n.args
            for x in (a.posonlyargs + a.args + a.kwonlyargs):
                bound.add(x.arg)
            if a.vararg:
                bound.add(a.vararg.arg)
            if a.kwarg:
                bound.add(a.kwarg.arg)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.add(n.name)
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                bound.add((al.asname or al.name).split(".")[0])
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            bound.add(n.id)
        if isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        if isinstance(n, ast.comprehension):
            for nm in ast.walk(n.target):
                if isinstance(nm, ast.Name):
                    bound.add(nm.id)
    return bound


@pytest.mark.parametrize("module_name,func_name", _TARGETS)
def test_render_func_has_no_unresolved_globals(module_name, func_name):
    """render 関数本体の Load 名が全て解決可能 (NameError の事前検出)."""
    mod = importlib.import_module(f"tabs.{module_name}")
    module_globals = set(vars(mod))
    tree = ast.parse((_TABS / f"{module_name}.py").read_text(encoding="utf-8"))
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == func_name
    )
    bound = _collect_bound(func)
    loads = {
        n.id for n in ast.walk(func)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    unresolved = sorted(loads - bound - module_globals - _BUILTINS)
    assert not unresolved, (
        f"{module_name}.{func_name} に未解決グローバル (render 実行時 NameError): "
        f"{unresolved}。関数内 lazy import か引数で解決すること。"
    )


_COMP = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _scan_forward_refs(func: ast.FunctionDef) -> list[tuple[str, int, int]]:
    """render 本体スコープで「後の行で束縛される名前を前方参照」している箇所を返す
    (UnboundLocalError 検出)。出典: 2026-06-05 HIGH-7 (get_conn が深い lazy import
    で関数ローカル化し、前方の bare get_conn() が UnboundLocal)。

    本体スコープのみ対象 (nested def/lambda/comprehension は別スコープ=走査除外、
    ただし nested def の NAME は本体束縛として記録)。
    """
    binds: dict[str, int] = {}  # name -> first 束縛 lineno
    loads: list[tuple[str, int]] = []

    def rec(node):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                binds.setdefault(c.name, c.lineno)  # 名前束縛のみ (body は別スコープ)
                continue
            if isinstance(c, (ast.Lambda,) + _COMP):
                continue  # 別スコープ
            if isinstance(c, (ast.Import, ast.ImportFrom)):
                for al in c.names:
                    nm = (al.asname or al.name).split(".")[0]
                    binds.setdefault(nm, c.lineno)
            if isinstance(c, ast.Name):
                if isinstance(c.ctx, ast.Store):
                    binds.setdefault(c.id, c.lineno)
                elif isinstance(c.ctx, ast.Load):
                    loads.append((c.id, c.lineno))
            if isinstance(c, ast.ExceptHandler) and c.name:
                binds.setdefault(c.name, c.lineno)
            rec(c)

    rec(func)
    bad = []
    for nm, ln in loads:
        b = binds.get(nm)
        if b is not None and ln < b:
            bad.append((nm, ln, b))
    return bad


@pytest.mark.parametrize("module_name,func_name", _TARGETS)
def test_render_func_no_use_before_local_binding(module_name, func_name):
    """render 本体で「後で import/代入される名前」を前方参照していない (UnboundLocal 検出)."""
    tree = ast.parse((_TABS / f"{module_name}.py").read_text(encoding="utf-8"))
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == func_name
    )
    bad = _scan_forward_refs(func)
    assert not bad, (
        f"{module_name}.{func_name} 前方参照 (render 実行時 UnboundLocalError): "
        f"{bad}。関数冒頭で import/束縛すること。"
    )


# ─────────────────────────────────────────────────────────────────────
# HIGH-1 回帰: _ebay_creds / _cfg が PNF ハンドラより前に定義されているか
# ─────────────────────────────────────────────────────────────────────

def test_inventory_monitor_ebay_creds_defined_before_pnf_handler():
    """HIGH-1 fix 回帰: _ebay_creds と _cfg は OOS / PNF の if ブロックより前の
    共通スコープで定義されている (pnf_submitted ハンドラより前に束縛)。"""
    src = (_TABS / "tab_inventory_monitor.py").read_text(encoding="utf-8")
    lines = src.splitlines()

    def first_assign_line(name: str) -> int:
        """関数本体内で `name =` が最初に現れる行番号 (1-indexed)。"""
        for i, ln in enumerate(lines, 1):
            stripped = ln.strip()
            if stripped.startswith(f"{name} =") or stripped.startswith(f"{name}:"):
                return i
        return -1

    def first_use_in_pnf(name: str) -> int:
        """pnf_submitted ハンドラ内で name が最初に現れる行番号 (1-indexed)。"""
        in_pnf = False
        for i, ln in enumerate(lines, 1):
            if "if _pnf_submitted:" in ln:
                in_pnf = True
            if in_pnf and name in ln:
                return i
        return -1

    for varname in ("_ebay_creds", "_cfg"):
        assign_ln = first_assign_line(varname)
        use_ln = first_use_in_pnf(varname)
        assert assign_ln > 0, f"{varname} の代入が見つかりません"
        assert use_ln > 0, f"{varname} の PNF ハンドラ内での参照が見つかりません"
        assert assign_ln < use_ln, (
            f"{varname} の代入 (L{assign_ln}) が PNF ハンドラ内の参照 (L{use_ln}) より後にある。"
            f"共通スコープで定義されていません (HIGH-1 NameError)。"
        )


# ─────────────────────────────────────────────────────────────────────
# HIGH-4 回帰: PNF セクションが SKU IN / dict[sku] 紐付けを使っていないか
# ─────────────────────────────────────────────────────────────────────

def test_inventory_monitor_pnf_no_sku_keyed_query():
    """HIGH-4 fix 回帰: PNF セクションの候補取得 SQL が WHERE sku IN を使っておらず、
    dict も sku キーで候補を紐付けていない。"""
    src = (_TABS / "tab_inventory_monitor.py").read_text(encoding="utf-8")
    # PNF セクション = "### 仕入先在庫切れ（確認不可）" 以降のテキストを切り出す
    marker = "仕入先在庫切れ（確認不可）"
    idx = src.find(marker)
    assert idx >= 0, f"PNF セクションマーカー '{marker}' が見つかりません"
    pnf_section = src[idx:]

    # "WHERE sku IN" が PNF セクション内に存在しないこと
    assert "WHERE sku IN" not in pnf_section, (
        "PNF セクションに 'WHERE sku IN' が残っています。"
        "ebay_item_id IN に張り替えてください (SKU 規約 HIGH-4)。"
    )

    # "_pnf_cand_by_sku" という SKU キー dict が存在しないこと
    assert "_pnf_cand_by_sku" not in pnf_section, (
        "PNF セクションに '_pnf_cand_by_sku' (SKU キー dict) が残っています。"
        "_pnf_cand_by_eid に変更してください (SKU 規約 HIGH-4)。"
    )

    # eid→sku 橋渡し (_pnf_eid_to_sku) が存在しないこと (HIGH-1' 回帰)。
    # 同一 sku 共有 listing で alt 件数が後勝ち上書きされ誤 caption になるため、
    # _pnf_alt_only_count は eid キーのまま格納する。
    assert "_pnf_eid_to_sku" not in pnf_section, (
        "PNF セクションに '_pnf_eid_to_sku' (eid→sku 橋渡し) が残っています。"
        "_pnf_alt_only_count は ebay_item_id キーのまま格納してください (HIGH-1')。"
    )


# ─────────────────────────────────────────────────────────────────────
# HIGH-2/3 回帰: _supplier_description_pipeline の閉じるボタンが
#               cache を pop せず、Step E が on_apply_* に依存しているか
# ─────────────────────────────────────────────────────────────────────

def test_supplier_desc_pipeline_close_button_no_cache_pop():
    """HIGH-2 fix 回帰: 閉じるボタンが sk_prefetch を pop しない。
    close_flag_key を False にするだけの安全な実装になっているか。"""
    src = (_TABS / "_supplier_description_pipeline.py").read_text(encoding="utf-8")

    # 新設ボタン (btn_close_) の周囲に sk_prefetch pop がないこと
    btn_idx = src.find("btn_close_")
    assert btn_idx >= 0, "閉じるボタン (btn_close_) が見つかりません"
    # btn_close_ の前後 300 文字以内に "sk_prefetch" pop がないこと
    context = src[max(0, btn_idx - 50): btn_idx + 300]
    assert "sk_prefetch" not in context or ".pop(" not in context[context.find("sk_prefetch"):context.find("sk_prefetch") + 20], (
        "閉じるボタン付近で sk_prefetch を pop しています。"
        "auto-prefetch 再発火の原因になります (HIGH-2)。"
    )


def test_image_pipeline_step_e_requires_callback():
    """HIGH-3 fix 回帰: render_image_pipeline_section の Step E が
    on_apply_image / on_apply_description / on_apply_both の少なくとも
    1 つが truthy でなければ表示されない条件になっているか。"""
    src = (_TABS / "_image_pipeline_ui.py").read_text(encoding="utf-8")
    # 実装側コメント "# ── Step E: 反映ボタン" を探す (docstring の "Step E:" より後)
    step_e_idx = src.find("# ── Step E:")
    assert step_e_idx >= 0, "Step E 実装コメント '# ── Step E:' が見つかりません"
    context = src[step_e_idx: step_e_idx + 400]
    # 修正後の条件: on_apply_image or on_apply_description or on_apply_both
    assert "on_apply_image or on_apply_description or on_apply_both" in context, (
        "Step E の条件が 'on_apply_image or on_apply_description or on_apply_both' を含んでいません。"
        "callback が全て None の時に Step E が非表示にならない (HIGH-3)。"
    )
