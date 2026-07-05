#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ドキュメントビューア (W326、2026-07-05) — 設計書 / mockup / 説明書 / KB を
MonoDeck の「設定」タブから一覧・閲覧する read-only 機能 (K1: 編集機能なし)。

収集元 (存在しないディレクトリは skip、caption で明示 / Q0):
  - .company/engineering/docs/     … .md = 設計書, .html = mockup
  - USER_MANUAL.md (project root)   … 説明書
  - .company/ebay-knowledge/topics/ … .md = KB (種別フィルタの既定で非表示 = 折りたたみ相当)

パス解決は Path(__file__) 基準の絶対化 (streamlit の cwd 依存を避ける)。
"""
from __future__ import annotations

import re
import webbrowser
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st
import streamlit.components.v1 as _components

# タブ密度化リファクタ準拠 (2026-07-04 系タブと同一パターン)。
# st.container(key="docsviewer_root") 配下だけに効くスコープ CSS。
# user 承認済み密度スペック: フォント12px / 行高22-28px。
_DOCSVIEWER_DENSITY_CSS = """<style>
div[class*="st-key-docsviewer_root"] [data-testid="stMarkdownContainer"] p {
    font-size: 12px !important;
    line-height: 24px !important;
    margin: 2px 0 !important;
}
div[class*="st-key-docsviewer_root"] [data-testid="stCaptionContainer"] p {
    font-size: 11px !important;
    line-height: 20px !important;
    margin: 2px 0 !important;
}
div[class*="st-key-docsviewer_root"] [data-testid="stDataFrame"] * {
    font-size: 12px !important;
}
</style>"""

_DATE_TITLE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)$")


def _project_root() -> Path:
    # tools/ebay-manager/tabs/tab_docs_viewer.py -> ebay-manager -> tools -> claude (root)
    return Path(__file__).resolve().parent.parent.parent.parent


def _extract_date_title(stem: str) -> tuple[str, str]:
    m = _DATE_TITLE_RE.match(stem)
    if m:
        date_str, rest = m.group(1), m.group(2)
    else:
        date_str, rest = "", stem
    title = rest.replace("-", " ").replace("_", " ").strip()
    return date_str, (title or stem)


def _make_entry(path: Path, category: str) -> dict:
    date_str, title = _extract_date_title(path.stem)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return {
        "path": path,
        "name": path.name,
        "title": title,
        "date_str": date_str,
        "category": category,
        "ext": path.suffix.lower(),
        "mtime": mtime,
    }


def collect_documents(project_root: Path) -> list[dict]:
    """収集ロジック本体 (pytest で tmp_path fixture 経由に単体テストする対象)。"""
    entries: list[dict] = []

    docs_dir = project_root / ".company" / "engineering" / "docs"
    if docs_dir.is_dir():
        for f in sorted(docs_dir.glob("*.md")):
            entries.append(_make_entry(f, "設計書"))
        for f in sorted(docs_dir.glob("*.html")):
            entries.append(_make_entry(f, "mockup"))

    kb_dir = project_root / ".company" / "ebay-knowledge" / "topics"
    if kb_dir.is_dir():
        for f in sorted(kb_dir.glob("*.md")):
            entries.append(_make_entry(f, "KB"))

    manual = project_root / "USER_MANUAL.md"
    if manual.is_file():
        entries.append(_make_entry(manual, "説明書"))

    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def _missing_sources(project_root: Path) -> list[str]:
    missing = []
    if not (project_root / ".company" / "engineering" / "docs").is_dir():
        missing.append("設計書・mockup (.company/engineering/docs)")
    if not (project_root / ".company" / "ebay-knowledge" / "topics").is_dir():
        missing.append("KB (.company/ebay-knowledge/topics)")
    if not (project_root / "USER_MANUAL.md").is_file():
        missing.append("説明書 (USER_MANUAL.md)")
    return missing


def render_tab() -> None:
    """app.py の 設定タブから呼ばれるエントリポイント。"""
    st.subheader(
        "ドキュメントビューア",
        help="設計書・mockup・説明書・KB を一覧から選んで確認できます (read-only)。",
    )
    root = st.container(key="docsviewer_root")
    root.markdown(_DOCSVIEWER_DENSITY_CSS, unsafe_allow_html=True)
    with root:
        _render_body()


def _render_body() -> None:
    project_root = _project_root()
    entries = collect_documents(project_root)

    missing = _missing_sources(project_root)
    if missing:
        st.caption(f"見つからないため対象外: {', '.join(missing)}")

    if not entries:
        st.caption("表示可能なドキュメントが見つかりません。")
        return

    c1, c2 = st.columns([2, 1])
    with c1:
        query = st.text_input("検索 (ファイル名部分一致)", value="", key="docsviewer_q")
    with c2:
        categories = st.multiselect(
            "種別フィルタ",
            options=["設計書", "mockup", "KB", "説明書"],
            default=["設計書", "mockup", "説明書"],
            help="KB は既定で非表示 (折りたたみ相当)。表示するには選択してください。",
            key="docsviewer_cat",
        )

    q = query.strip().lower()
    filtered = [
        e for e in entries
        if e["category"] in categories
        and (not q or q in e["name"].lower() or q in e["title"].lower())
    ]

    if not filtered:
        st.caption("該当するドキュメントがありません。")
        return

    df = pd.DataFrame([
        {
            "種別": e["category"],
            "タイトル": e["title"],
            "日付": e["date_str"] or "—",
            "ファイル名": e["name"],
        }
        for e in filtered
    ])
    sel = st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        height=min(360, 40 + 32 * len(filtered)),
        on_select="rerun",
        selection_mode="single-row",
        key="docsviewer_table",
    )

    selected_idx: Optional[int] = None
    if sel is not None and getattr(sel, "selection", None) is not None:
        rows = sel.selection.rows
        if rows:
            selected_idx = rows[0]

    if selected_idx is None:
        st.caption("上の一覧から行を選択すると内容を表示します。")
        return

    _render_viewer(filtered[selected_idx])


def _render_viewer(entry: dict) -> None:
    st.divider()
    st.markdown(f"**{entry['title']}** （{entry['category']} / {entry['date_str'] or '日付不明'}）")
    st.code(str(entry["path"]), language=None)

    if st.button("ブラウザで開く", key=f"docsviewer_open_{entry['name']}"):
        try:
            webbrowser.open(entry["path"].resolve().as_uri())
        except OSError as e:
            st.warning(f"ブラウザ起動に失敗しました: {e}")

    try:
        text = entry["path"].read_text(encoding="utf-8")
    except OSError as e:
        st.error(f"読み込みに失敗しました: {e}")
        return

    if entry["ext"] == ".html":
        _components.html(text, height=800, scrolling=True)
    else:
        st.markdown(text)
