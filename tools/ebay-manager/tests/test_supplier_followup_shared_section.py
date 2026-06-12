"""依頼ボード#11 回帰テスト — 採用後フォローアップ欄の共有化 + 在庫監視結線。

2026-06-12: 在庫監視タブの採用 (チェックボックス+一括実行) では写真/description
生成プロンプトが展開されない (user 期待と乖離) → followup render を
tabs/_supplier_followup_section.py へ移設し、在庫監視の採用成功時にも
`_sup_photo_prompt_` / `_sup_desc_prompt_` フラグを set + タブ先頭で render。

render 本体は Streamlit runtime 依存のため、本テストは
  1. 共有モジュールの import 契約
  2. 両タブの結線 (render 呼出 + フラグ set) がソースに存在すること
を守る (結線が消える regression を検出する wiring test)。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_TABS = _PROJECT_ROOT / "tabs"


def test_shared_section_importable():
    """共有モジュールが import でき、render 関数を公開している。"""
    from tabs._supplier_followup_section import render_supplier_followup_section
    assert callable(render_supplier_followup_section)


def test_supplier_candidates_tab_uses_shared_section():
    """仕入先候補タブが共有 render を呼ぶ (inline 復活 / 呼出消失の検出)。"""
    src = (_TABS / "tab_supplier_candidates.py").read_text(encoding="utf-8")
    assert "render_supplier_followup_section" in src
    # inline ブロックが二重定義で復活していないこと (描画とフラグの二重消費防止)
    assert "採 用 後 フ ォ ロ ー ア ッ プ" not in src


def test_inventory_monitor_tab_uses_shared_section():
    """在庫監視タブが共有 render を呼ぶ (依頼ボード#11 の本体)。"""
    src = (_TABS / "tab_inventory_monitor.py").read_text(encoding="utf-8")
    assert "render_supplier_followup_section" in src


def test_inventory_monitor_sets_prompt_flags_on_adopt():
    """採用経路が成功時に photo+desc prompt フラグを set する。

    依頼ボード#18 (2026-06-13): 一括 UI (_process_apply / _process_apply_pnf
    の 2 経路) を撤去し、_adopt_and_apply 単一経路に統合。OOS/PNF 両ブロック
    がこのヘルパーを共有するため、フラグ set は 1 箇所のみが正。
    """
    src = (_TABS / "tab_inventory_monitor.py").read_text(encoding="utf-8")
    assert src.count('st.session_state[f"_sup_photo_prompt_{cid}"] = True') == 1
    assert src.count('st.session_state[f"_sup_desc_prompt_{cid}"] = True') == 1
    # meta (url/eid/title) も同時 set (followup 欄のタイトル/URL 表示用)
    assert 'st.session_state[f"_sup_photo_meta_{cid}"]' in src


def test_shared_section_keeps_later_notice():
    """依頼ボード#12 の行き先通知 (later_notice pop) が移設先に保持されている。"""
    src = (_TABS / "_supplier_followup_section.py").read_text(encoding="utf-8")
    assert "_sup_followup_later_notice" in src
    assert "仕入先候補タブの『履歴』に移動しました" in src
