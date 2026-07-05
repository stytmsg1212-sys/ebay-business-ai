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

    mockup_html = docs_dir / "2026-07-05-new-mockup.html"
    mockup_html.write_text("<html><body>mockup</body></html>", encoding="utf-8")
    os.utime(mockup_html, (time.time() - 100, time.time() - 100))

    kb_md = kb_dir / "business-policies.md"
    kb_md.write_text("# kb", encoding="utf-8")
    os.utime(kb_md, (time.time() - 200, time.time() - 200))

    manual = root / "USER_MANUAL.md"
    manual.write_text("# manual", encoding="utf-8")
    os.utime(manual, (time.time() - 50, time.time() - 50))

    entries = collect_documents(root)

    assert [e["category"] for e in entries] == ["説明書", "mockup", "KB", "設計書"]
    assert entries[0]["name"] == "USER_MANUAL.md"
    assert entries[1]["name"] == "2026-07-05-new-mockup.html"
    assert entries[1]["ext"] == ".html"
    assert entries[3]["date_str"] == "2026-07-01"
    assert entries[3]["title"] == "old design"


def test_collect_documents_skips_missing_dirs(tmp_path):
    # ディレクトリを一切作らない = 全 skip でも例外を出さず空リストを返す
    entries = collect_documents(tmp_path)
    assert entries == []


def test_tab_docs_viewer_importable():
    import importlib

    m = importlib.import_module("tabs.tab_docs_viewer")
    assert hasattr(m, "render_tab")
    assert callable(m.render_tab)
