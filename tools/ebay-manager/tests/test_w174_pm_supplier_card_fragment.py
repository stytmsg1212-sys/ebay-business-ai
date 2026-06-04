"""W174-pm 回帰: 仕入先候補 UI の採用/不採用ボタンでタブが移動しない.

2026-05-25 user 報告:
    「別SKU出品機会」タブで採用/不採用ボタンを押下後、毎回「復活候補」タブに
    強制移動する UX バグ。`st.tabs()` は state を保持せず `st.rerun()` で tab 0
    にリセットされるのが根本原因.

fix:
    `_render_candidate_card` に `@st.fragment` decorator 追加で button rerun を
    fragment scope に限定. 不採用は default scope (タブ維持優先) + hide flag
    で「処理済」caption. 採用は `st.rerun(scope="app")` で full rerun (photo
    prompt section 表示優先、採用後は履歴タブに candidate 移動).

本 test は app.py の AST 静的検証で decorator + scope 指定が消えていないことを
ロック (Streamlit AppTest は実 runtime 必要なため別途整備).
"""
from __future__ import annotations

import ast
from pathlib import Path

# W221 Tier2 (2026-06-05): 仕入先候補タブ (_render_candidate_card 含む) は
# app.py から tabs/tab_supplier_candidates.py へ移動。AST 検証先を更新。
APP_PY = Path(__file__).resolve().parent.parent / "tabs" / "tab_supplier_candidates.py"


def _find_function_def(name: str) -> ast.FunctionDef | None:
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_render_candidate_card_has_fragment_decorator():
    """`_render_candidate_card` に @st.fragment decorator が付与されている.

    削除されると user 報告の bug (採用/不採用後タブ移動) が再発する.
    """
    fn = _find_function_def("_render_candidate_card")
    assert fn is not None, "_render_candidate_card 関数が見つからない"
    found = False
    for dec in fn.decorator_list:
        # @st.fragment or @st.fragment(...) どちらも許容
        if isinstance(dec, ast.Attribute) and dec.attr == "fragment":
            found = True
        elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                and dec.func.attr == "fragment":
            found = True
    assert found, \
        "_render_candidate_card に @st.fragment decorator が必要 " \
        "(W174-pm tab 維持、削除すると user 報告 bug 再発)"


def test_accept_button_uses_app_scope_rerun():
    """採用 button の st.rerun() は scope='app' で full rerun.

    photo prompt / desc prompt section (line 5314+) を outer scope で
    再描画するため. fragment scope だと「採用したが photo 反映 prompt が
    出ない」UX 退化 (code-reviewer HIGH-2).
    """
    src = APP_PY.read_text(encoding="utf-8")
    assert ('st.rerun(scope="app")' in src or "st.rerun(scope='app')" in src), \
        "採用 button は photo prompt section 表示のため st.rerun(scope=\"app\") 必須"


def test_reject_button_sets_hide_flag():
    """不採用 button は session_state hide flag を立てて card を隠す.

    fragment scope rerun では card 自体が消えないため、hide flag + 早期
    return で「処理されました」caption を出さないと user は「効いてない」
    と感じて二重 click する (code-reviewer HIGH-1).
    """
    src = APP_PY.read_text(encoding="utf-8")
    assert "_sup_rejected_{cid}" in src or "f\"_sup_rejected_{cid}\"" in src, \
        "不採用 button は session_state[`_sup_rejected_{cid}`] = True で hide flag を立てる必要あり"
    # 関数冒頭の早期 return path
    assert "次回画面更新で履歴タブに移動します" in src, \
        "card 冒頭で hide flag check + caption + return の path が必要"


def test_reject_button_uses_fragment_scope_rerun():
    """不採用 button の st.rerun() は scope='fragment' で限定 rerun.

    重要: Streamlit 1.37+ では `st.rerun()` の default は `scope="app"` (たとえ
    @st.fragment 内で呼ばれていても). 明示的に `scope="fragment"` 指定が
    必要 (user 報告 2026-05-25「不採用押下後もタブ移動」で発覚).
    """
    src = APP_PY.read_text(encoding="utf-8")
    # 不採用 button のコメント直後の st.rerun() スコープ確認
    assert ('st.rerun(scope="fragment")' in src
            or "st.rerun(scope='fragment')" in src), \
        "不採用 button は st.rerun(scope=\"fragment\") 必須 (タブ維持、" \
        "user 報告 W174-pm 真因対応)"
