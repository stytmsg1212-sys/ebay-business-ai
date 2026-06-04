"""在庫監視タブ サマリバー `_render_inventory_summary_html` unit tests.

設計 (2026-06-04): 在庫監視「要対応」サブタブ先頭に表示する 1 枚 HTML サマリバー.
DB アクセス禁止 (呼出側が集計済の数値を渡す純関数). K1 Simplicity / K2 Surgical.

検証ポイント:
  1. `total_risk == 0` → 緑系 (rgba(118,255,3,...)) を含む
  2. `total_risk > 0` → 赤系 (rgba(255,90,90,...) / rgba(255,140,140,...)) を含む
  3. oos_n / pnf_n / last_checked_str が出力 HTML 内に含まれる
  4. 件数値が表示されている

`app.py` 全体を import すると Streamlit runtime が起動するため、
ast で `_render_inventory_summary_html` 関数定義のみ抽出して exec で取り出す.
"""
from __future__ import annotations

import ast
import html as _html_mod
from pathlib import Path

APP_PY = Path(__file__).resolve().parent.parent / "app.py"


def _load_summary_func():
    """`app.py` から `_render_inventory_summary_html` 関数定義のみ抽出して取り出す.

    Streamlit runtime / 重い import を回避するため、関数本体だけを exec.
    関数は `html` モジュール (標準ライブラリ) に依存するため、scope に注入する.
    """
    src = APP_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_render_inventory_summary_html":
            # 関数定義 1 つだけを含む module を作成
            new_module = ast.Module(body=[node], type_ignores=[])
            code = compile(new_module, str(APP_PY), "exec")
            scope: dict = {"html": _html_mod}
            exec(code, scope)
            return scope["_render_inventory_summary_html"]
    raise AssertionError("_render_inventory_summary_html 関数が app.py に見つからない")


class TestZeroRisk:
    """要対応 0 件 → 緑系."""

    def test_zero_risk_uses_green(self):
        fn = _load_summary_func()
        out = fn(total_risk=0, oos_n=0, pnf_n=0, last_checked_str="2026-06-04 02:35:21")
        # 緑系 (rgba(118,255,3,...))
        assert "rgba(118,255,3" in out, "0 件のときは緑系トーンであるべき"

    def test_zero_risk_does_not_use_red(self):
        fn = _load_summary_func()
        out = fn(total_risk=0, oos_n=0, pnf_n=0, last_checked_str="2026-06-04 02:35:21")
        # 赤系 (rgba(255,90,90 / 255,140,140) が含まれない)
        assert "rgba(255,90,90" not in out
        assert "rgba(255,140,140" not in out

    def test_zero_risk_shows_count(self):
        fn = _load_summary_func()
        out = fn(total_risk=0, oos_n=0, pnf_n=0, last_checked_str="x")
        assert "0件" in out


class TestPositiveRisk:
    """要対応 >0 件 → 赤系."""

    def test_positive_risk_uses_red(self):
        fn = _load_summary_func()
        out = fn(total_risk=5, oos_n=3, pnf_n=2, last_checked_str="2026-06-04 02:35:21")
        # 赤系 (border) or 数値色 (rgba(255,140,140,...))
        assert ("rgba(255,90,90" in out) or ("rgba(255,140,140" in out), \
            ">0 件のときは赤系トーンであるべき"

    def test_positive_risk_does_not_use_green_border(self):
        fn = _load_summary_func()
        out = fn(total_risk=5, oos_n=3, pnf_n=2, last_checked_str="x")
        # 緑系の border / 数値色は含まない
        # ※「[採用済]」「最終チェック」など他要素で緑が漏れても困らないよう、
        # 本関数の出力に限定すれば緑トーンは出てこないはず.
        assert "rgba(118,255,3" not in out

    def test_positive_risk_shows_total(self):
        fn = _load_summary_func()
        out = fn(total_risk=5, oos_n=3, pnf_n=2, last_checked_str="x")
        assert "5件" in out

    def test_positive_risk_shows_oos_and_pnf(self):
        fn = _load_summary_func()
        out = fn(total_risk=5, oos_n=3, pnf_n=2, last_checked_str="x")
        # 「在庫切れ 3」「ページ消失 2」相当の数値が含まれる
        assert "3" in out
        assert "2" in out


class TestLastChecked:
    """最終チェック文字列の表示."""

    def test_last_checked_str_is_shown(self):
        fn = _load_summary_func()
        out = fn(total_risk=0, oos_n=0, pnf_n=0, last_checked_str="2026-06-04 02:35:21 (3 時間前)")
        assert "2026-06-04 02:35:21" in out
        assert "3 時間前" in out

    def test_last_checked_empty_safe(self):
        fn = _load_summary_func()
        # 空文字でも例外なし
        out = fn(total_risk=0, oos_n=0, pnf_n=0, last_checked_str="")
        assert "最終チェック" in out

    def test_last_checked_html_escaped(self):
        """`last_checked_str` に HTML 特殊文字があれば escape されること (XSS 防御)."""
        fn = _load_summary_func()
        out = fn(total_risk=0, oos_n=0, pnf_n=0, last_checked_str="<script>alert(1)</script>")
        # raw <script> は含まれず、escape された形になる
        assert "<script>" not in out
        assert "&lt;script&gt;" in out


class TestStructure:
    """出力が単一 <div> 構造であること."""

    def test_output_is_single_div(self):
        fn = _load_summary_func()
        out = fn(total_risk=3, oos_n=2, pnf_n=1, last_checked_str="x")
        # 最低でも開きと閉じ <div> がある
        assert out.startswith("<div")
        assert out.endswith("</div>")

    def test_int_coercion(self):
        """件数が `int` で format されること (float 渡しても例外なし)."""
        fn = _load_summary_func()
        # 数値は呼出側で int を渡すが、念のため robustness を担保
        out = fn(total_risk=2, oos_n=1, pnf_n=1, last_checked_str="x")
        assert "2件" in out
