#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBay 知識ベース MCP サーバ (読み取り専用 / KB スコープ限定).

claude.ai のカスタムコネクタ(リモートMCP)から、このリポジトリの eBay ナレッジ
(コンサルKB + topics + eBay規制ルール)を **読み取り専用** で参照させるための
MCP サーバ。Web/スマホの相談チャットが、蓄積知識に基づいて答えられるようにする。

セキュリティ方針 (重要):
- **読み取り専用**。書き込み/削除ツールは一切持たない。
- 公開対象は ALLOWED_ROOTS のホワイトリストのみ (eBay ナレッジ + 規制ルール)。
  .env / monitor.db / ソースコード / memory 全般 は **物理的にスコープ外**。
- パストラバーサル防止 (resolve 後に許可ルート配下か厳格検査)。
- 拡張子は .md / .txt のみ配信 (raw JSON ログ等のノイズ/巨大ファイルは除外)。
- claude.ai は OAuth 未構成だと固定トークンを送れないため v1 はアプリ層トークンを
  必須にしない。**公開時はトンネル側で Anthropic IP レンジに限定する**こと
  (support.claude.com の custom connector ガイド参照)。

起動:
    python tools/ebay-manager/scripts/kb_mcp_server.py
    (環境変数 KB_MCP_HOST/KB_MCP_PORT で上書き可。既定 127.0.0.1:8765)
    → streamable-http エンドポイント: http://127.0.0.1:8765/mcp
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# tools/ebay-manager/scripts -> repo root
REPO = Path(__file__).resolve().parents[3]

# 公開を許可するルート (これ以外は一切配信しない)
ALLOWED_ROOTS: list[Path] = [
    (REPO / ".company/ebay-knowledge").resolve(),
    (REPO / "tools/ebay-manager/CLAUDE.md").resolve(),  # eBay 規制業務ルール (単一ファイル)
]
ALLOWED_SUFFIXES = {".md", ".txt"}
MAX_READ_CHARS = 200_000

mcp = FastMCP(
    "ebay-kb",
    instructions=(
        "MonoHonpo/TOYOTASUMI の eBay 越境EC 蓄積ナレッジ(読み取り専用)。"
        "list_kb で一覧、read_kb で本文取得、search_kb で全文検索。"
        "関税/送料/eBayポリシー等の時限性項目(⏰)は発言月を確認し最新を要確認。"
        "規制業務(HS分類/通関/VeRO)の最終責任は人間。原産国記載禁止など自社ルール優先。"
    ),
    host=os.environ.get("KB_MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("KB_MCP_PORT", "8765")),
)


def _iter_files() -> list[Path]:
    """配信対象ファイル (許可ルート配下の .md/.txt) を列挙。"""
    out: list[Path] = []
    for root in ALLOWED_ROOTS:
        if root.is_file():
            if root.suffix.lower() in ALLOWED_SUFFIXES:
                out.append(root)
        elif root.is_dir():
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES:
                    out.append(p)
    return sorted(set(out))


def _resolve_allowed(rel_or_name: str) -> Path | None:
    """要求パスを解決し、許可ルート配下の配信可能ファイルか厳格検査。

    パストラバーサル(..)・シンボリックリンク・スコープ外を全て拒否。
    rel_or_name は repo ルート相対 or list_kb が返す相対パス。
    """
    if not rel_or_name or any(c in rel_or_name for c in ("\x00",)):
        return None
    candidate = (REPO / rel_or_name).resolve()
    if candidate.suffix.lower() not in ALLOWED_SUFFIXES:
        return None
    if not candidate.is_file():
        return None
    for root in ALLOWED_ROOTS:
        try:
            if root.is_file() and candidate == root:
                return candidate
            if root.is_dir() and candidate.is_relative_to(root):
                return candidate
        except ValueError:
            continue
    return None


@mcp.tool()
def list_kb() -> str:
    """eBay ナレッジベースの配信対象ドキュメント一覧 (repo相対パスとサイズ) を返す。"""
    lines = ["# eBay ナレッジベース 一覧 (読み取り専用)"]
    for p in _iter_files():
        rel = p.relative_to(REPO).as_posix()
        kb = max(1, p.stat().st_size // 1024)
        lines.append(f"- {rel} ({kb}KB)")
    return "\n".join(lines) if len(lines) > 1 else "(配信対象なし)"


@mcp.tool()
def read_kb(path: str) -> str:
    """指定ドキュメントの本文を返す。path は list_kb が返す repo 相対パス。

    許可ルート外・非.md/.txt・存在しないパスは拒否(セキュリティ)。
    """
    f = _resolve_allowed(path)
    if f is None:
        return f"ERROR: '{path}' は配信対象外か存在しません。list_kb の一覧から指定してください。"
    text = f.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n\n…(先頭 {MAX_READ_CHARS} 文字まで。続きは別途)"
    return text


@mcp.tool()
def search_kb(query: str, max_results: int = 20) -> str:
    """ナレッジ全体を全文検索し、ヒット箇所(ファイル/行/前後文)を返す。

    query は大文字小文字を無視した部分一致。複数語はスペース区切りで AND。
    """
    q = (query or "").strip().lower()
    if not q:
        return "ERROR: query が空です。"
    terms = [t for t in q.split() if t]
    hits: list[str] = []
    for p in _iter_files():
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = p.relative_to(REPO).as_posix()
        for i, line in enumerate(lines):
            low = line.lower()
            if all(t in low for t in terms):
                ctx = line.strip()
                hits.append(f"[{rel}:{i+1}] {ctx[:300]}")
                if len(hits) >= max_results:
                    return "\n".join([f"# 検索結果 (query={query!r}, 上限{max_results})"] + hits)
    if not hits:
        return f"(ヒットなし: query={query!r})"
    return "\n".join([f"# 検索結果 (query={query!r})"] + hits)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
