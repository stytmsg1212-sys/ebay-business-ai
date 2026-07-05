#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W326 (2026-07-05): MonoDeck ドキュメントビューア (設計書/mockup/説明書/KB).

カバレッジ:
  - collect_documents: 3 ディレクトリ + USER_MANUAL.md からの収集、
    拡張子による種別振り分け (.md=設計書 / .html=mockup、KB dir 配下は .md=KB)、
    更新日 (mtime) 降順ソート
  - 存在しないディレクトリは例外を出さず skip すること
  - _extract_date_title: `YYYY-MM-DD-slug` 形式からの日付/タイトル分離、
    日付なしファイル名のフォールバック
  - tabs/tab_docs_viewer.py: import + render_tab の存在確認 (import/renderable 回帰)
"""
from __future__ import annotations

import os
import time

from tabs.tab_docs_viewer import _extract_date_title, collect_documents


def test_extract_date_title_with_date_prefix():
    date_str, title = _extract_date_title("2026-07-05-w317-ebaymag-id-match-design")
    assert date_str == "2026-07-05"
    assert title == "w317 ebaymag id match design"


def test_extract_date_title_without_date_prefix():
    date_str, title = _extract_date_title("qa-checklist")
    assert date_str == ""
    assert title == "qa checklist"


def test_collect_documents_categorizes_and_sorts(tmp_path):
    root = tmp_path
    docs_dir = root / ".company" / "engineering" / "docs"
    kb_dir = root / ".company" / "ebay-knowledge" / "topics"
    docs_dir.mkdir(parents=True)
    kb_dir.mkdir(parents=True)

    design_md = docs_dir / "2026-07-01-old-design.md"
    design_md.write_text("# old design", encoding="utf-8")
    os.utime(design_md, (time.time() - 300, time.time() - 300))

    # ファイル名に "mockup" を含む HTML は mockup 分類
    mockup_html = docs_dir / "2026-07-05-new-mockup.html"
    mockup_html.write_text("<html><body>mockup</body></html>", encoding="utf-8")
    os.utime(mockup_html, (time.time() - 100, time.time() - 100))

    # ファイル名に "mockup" を含まない HTML は設計書 (W326 QA 修正)
    design_html = docs_dir / "2026-07-06-daily-workflow-design.html"
    design_html.write_text("<html><body>design</body></html>", encoding="utf-8")
    os.utime(design_html, (time.time() - 150, time.time() - 150))

    kb_md = kb_dir / "business-policies.md"
    kb_md.write_text("# kb", encoding="utf-8")
    os.utime(kb_md, (time.time() - 200, time.time() - 200))

    manual = root / "USER_MANUAL.md"
    manual.write_text("# manual", encoding="utf-8")
    os.utime(manual, (time.time() - 50, time.time() - 50))

    entries = collect_documents(root)
    by_name = {e["name"]: e for e in entries}

    # 更新日 (mtime) 降順ソート: USER_MANUAL.md → mockup → design.html → KB → old-design.md
    assert [e["name"] for e in entries] == [
        "USER_MANUAL.md",
        "2026-07-05-new-mockup.html",
        "2026-07-06-daily-workflow-design.html",
        "business-policies.md",
        "2026-07-01-old-design.md",
    ]
    assert by_name["USER_MANUAL.md"]["category"] == "説明書"
    assert by_name["2026-07-05-new-mockup.html"]["category"] == "mockup"
    # ファイル名基準: "mockup" を含まない HTML は設計書
    assert by_name["2026-07-06-daily-workflow-design.html"]["category"] == "設計書"
    assert by_name["business-policies.md"]["category"] == "KB"
    assert by_name["2026-07-01-old-design.md"]["category"] == "設計書"
    assert by_name["2026-07-01-old-design.md"]["date_str"] == "2026-07-01"
    assert by_name["2026-07-01-old-design.md"]["title"] == "old design"


def test_collect_documents_md_mockup_classified_as_mockup(tmp_path):
    """.md でもファイル名に mockup を含めば mockup 分類 (K1: 拡張子でなくファイル名基準)."""
    docs_dir = tmp_path / ".company" / "engineering" / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "2026-06-27-today-tasks-tab-proposal-mockup.md").write_text("# mockup", encoding="utf-8")

    entries = collect_documents(tmp_path)
    assert len(entries) == 1
    assert entries[0]["category"] == "mockup"


def test_collect_documents_skips_missing_dirs(tmp_path):
    # ディレクトリを一切作らない = 全 skip でも例外を出さず空リストを返す
    entries = collect_documents(tmp_path)
    assert entries == []


def test_selected_index_out_of_range_guard():
    """W326 QA 追補: dataframe on_select="rerun" は selection を widget state に
    残すため、フィルタ縮小で `filtered[selected_idx]` が IndexError を出すクラッシュを再現。
    修正後は `selected_idx >= len(filtered)` で選択なし扱い (guard) となることを、
    直接ソース文字列で検証する (Streamlit UI ランタイム不要)。
    """
    from pathlib import Path as _P
    src = _P("tabs/tab_docs_viewer.py").read_text(encoding="utf-8")
    # 選択 index が filtered 長を超えた場合の guard が存在すること
    assert "selected_idx is None or selected_idx >= len(filtered)" in src
    # guard の後に _render_viewer(filtered[selected_idx]) が呼ばれる正規経路が
    # 残っていること (guard を消して素アクセスに戻していないことの確認)
    assert "_render_viewer(filtered[selected_idx])" in src


def test_tab_docs_viewer_importable():
    import importlib

    m = importlib.import_module("tabs.tab_docs_viewer")
    assert hasattr(m, "render_tab")
    assert callable(m.render_tab)
