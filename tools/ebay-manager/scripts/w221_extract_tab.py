#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W221 Tier2: app.py のインライン分岐を tabs/tab_<slug>.py へ byte-exact 抽出する one-shot ヘルパー。

body を手写しせず app.py から正確にコピー = 転記ミス排除。インデント境界で
body / module-level helper を切り出す。ナビ・routing・ラベル・widget key・
session_state キーは一切変更しない (K2)。app.py は CRLF、tabs/*.py は LF を維持。

設計: code-architect ブループリント (2026-06-04)。
"""
from __future__ import annotations

from pathlib import Path as _P

_APP = _P(__file__).resolve().parent.parent / "app.py"
_TABS = _P(__file__).resolve().parent.parent / "tabs"


def _find_body(src: list[str], label: str) -> tuple[int, list[str]]:
    """`if _w134_sel == "<label>":` を探し、(開始 index, body 行) を返す (indent 境界)."""
    start = None
    for i, l in enumerate(src):
        if l.rstrip() == f'if _w134_sel == "{label}":':
            start = i
            break
    if start is None:
        raise SystemExit(f"branch not found: {label!r}")
    body: list[str] = []
    j = start + 1
    while j < len(src):
        l = src[j]
        if l.strip() == "" or l[:1] in (" ", "\t"):
            body.append(l)
            j += 1
        else:
            break
    while body and body[-1].strip() == "":
        body.pop()
    return start, body


def _capture_block(src: list[str], name: str) -> tuple[int, int, list[str]]:
    """top-level の `def name(...)` (decorator 含む) or `name = {...}` を捉え
    (開始 index, 終了 index(exclusive), 行リスト) を返す。"""
    # def or assignment の開始行
    di = None
    is_def = False
    for i, l in enumerate(src):
        if l.startswith(f"def {name}(") or l.startswith(f"def {name} ("):
            di = i
            is_def = True
            break
        if l.startswith(f"{name} =") or l.startswith(f"{name}="):
            di = i
            is_def = False
            break
    if di is None:
        raise SystemExit(f"helper not found: {name!r}")
    # decorator を遡って含める
    start = di
    while start - 1 >= 0 and src[start - 1].lstrip().startswith("@"):
        start -= 1
    if is_def:
        # body は indent。次の col-0 非空行で終端。
        j = di + 1
        while j < len(src) and (src[j].strip() == "" or src[j][:1] in (" ", "\t")):
            j += 1
        end = j
        # 末尾空行を block から除外 (app.py 側に残す)
        while end - 1 > di and src[end - 1].strip() == "":
            end -= 1
    else:
        # 代入: 単一行 or 複数行 dict/list/tuple。col-0 の閉じ括弧行 inclusive まで。
        first = src[di].rstrip()
        if first.endswith(("{", "(", "[")):
            j = di + 1
            while j < len(src) and not (src[j][:1] in ("}", ")", "]")):
                j += 1
            end = j + 1  # 閉じ括弧行を含む
        else:
            end = di + 1
    return start, end, src[start:end]


def extract_tab(label: str, slug: str, func: str, arg: str | None,
                module_imports: list[str], logger: bool, doc: str,
                helpers: list[str] | None = None) -> None:
    """1 タブを抽出。app.py を書き換え、tabs/tab_<slug>.py を生成する。

    helpers: app.py top-level から tab module へ移動するヘルパー名 (def or 代入)。
             その唯一の利用タブへ同梱 (architect: 全 helper は単一分岐専用)。
    """
    src = open(_APP, encoding="utf-8").read().splitlines()

    # ---- helper ブロックを捕捉 (body より上なので先に捕捉、削除は後でまとめて) ----
    helper_blocks: list[list[str]] = []
    helper_spans: list[tuple[int, int]] = []
    for hname in (helpers or []):
        hs, he, hlines = _capture_block(src, hname)
        helper_blocks.append(hlines)
        helper_spans.append((hs, he))

    start, body = _find_body(src, label)

    # ---- 新タブファイル生成 ----
    mod_imports = list(module_imports)
    if "import streamlit as st" not in mod_imports:
        mod_imports.append("import streamlit as st")
    header = [
        "#!/usr/bin/env python3",
        "# -*- coding: utf-8 -*-",
        f'"""{doc} (W221 Tier2 抽出、2026-06-04)。',
        "",
        f'app.py の `if _w134_sel == "{label}":` 分岐 body をそのまま移植。挙動不変 (K2 surgical)。',
    ]
    if helpers:
        header.append(f"同梱ヘルパー (app.py top-level から移動、単一タブ専用): {', '.join(helpers)}")
    header.append('"""')
    header += ["from __future__ import annotations", ""]
    header += mod_imports
    if logger:
        header += ["", "logger = logging.getLogger(__name__)"]

    # module-level helpers (関数の外、import の後)
    helper_section: list[str] = []
    for hb in helper_blocks:
        helper_section += ["", ""] + hb

    argsig = arg if arg else ""
    fn = ["", "", f"def {func}({argsig}) -> None:"]

    new_lines = header + helper_section + fn + body
    out = _TABS / f"tab_{slug}.py"
    out.write_text("\n".join(new_lines) + "\n", encoding="utf-8", newline="\n")

    # ---- app.py: helper 削除 (index 降順) + branch を dispatch に差し替え ----
    # body 範囲を span として加え、全 span を降順削除してから dispatch 挿入。
    body_span = (start, start + 1 + len(body))
    call_arg = arg.split(":")[0].strip() if arg else ""
    dispatch = [
        f'if _w134_sel == "{label}":',
        f"    from tabs.tab_{slug} import {func}",
        f"    {func}({call_arg})",
    ]
    # 先に body を dispatch に置換 (index 不変な範囲外の helper span に影響なし: helper は上)
    src = src[:body_span[0]] + dispatch + src[body_span[1]:]
    # helper span は body より上 = index 不変。降順削除。
    for hs, he in sorted(helper_spans, key=lambda x: -x[0]):
        del src[hs:he]

    with open(_APP, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(src) + "\n")
    print(f"OK {label}: body {len(body)} 行 + helper {len(helpers or [])} 個 → tabs/tab_{slug}.py")
