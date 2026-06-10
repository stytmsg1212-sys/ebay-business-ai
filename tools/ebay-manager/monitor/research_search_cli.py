#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""フリマ検索 subprocess エントリポイント (W228 FIX-E).

使い方:
    python -m monitor.research_search_cli <platform> <max_results>
    # keyword は stdin (UTF-8, 1 行) から読む

背景:
    Streamlit プロセス内では Windows の SelectorEventLoop が子プロセスを起動できず
    Playwright が NotImplementedError で即死する (2026-06-10 Q1 実機発見)。
    research_poc._search_freemarket が直接 search_mercari 等を呼ぶと、Streamlit 起動時
    に NotImplementedError が mercari_search.py L173 の except Exception で握りつぶされ
    空リストが返り、evaluate_product が「0 件 = not_found」と誤判定する。

    本スクリプトを subprocess として起動することで、フレッシュな python プロセスが
    既定の ProactorEventLoop で Playwright を正常実行できる。

出力:
    正常: {"ok": true, "hits": [{"url": ..., "title": ..., "price_jpy": ..., "image_url": ...}]}
    エラー: {"ok": false, "error": "<type>: <msg>"}
    プラットフォーム不正: {"ok": false, "error": "unknown platform: <name>"}
    想定外クラッシュ: 非 0 exit code
"""
from __future__ import annotations

import json
import sys
from typing import Any


def _reconfigure_streams() -> None:
    """pythonw 環境でも stdout/stdin が UTF-8 になるよう再設定する (house パターン)."""
    for stream in (sys.stdout, sys.stdin):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _run(platform: str, max_results: int, keyword: str) -> dict[str, Any]:
    """platform に応じた検索を実行し、正規化した結果辞書を返す."""
    if platform == "mercari":
        from monitor.mercari_search import search_mercari  # type: ignore[import]
        raw = search_mercari(keyword, max_results=max_results)
        hits = [
            {
                "url": h.url,
                "title": h.title or "",
                "price_jpy": h.price_jpy,
                "image_url": h.image_url,
            }
            for h in raw
        ]
        return {"ok": True, "hits": hits}

    if platform == "yahoo_auctions":
        from monitor.yahoo_search import search_yahoo  # type: ignore[import]
        raw = search_yahoo(keyword, max_results=max_results)
        hits = [
            {
                "url": h.url,
                "title": h.title or "",
                "price_jpy": h.price_jpy,
                "image_url": h.image_url,
            }
            for h in raw
        ]
        return {"ok": True, "hits": hits}

    if platform == "paypay_furima":
        from monitor.paypay_search import search_paypay  # type: ignore[import]
        raw = search_paypay(keyword, max_results=max_results)
        hits = [
            {
                "url": h.url,
                "title": h.title or "",
                "price_jpy": h.price_jpy,
                "image_url": h.image_url,
            }
            for h in raw
        ]
        return {"ok": True, "hits": hits}

    return {"ok": False, "error": f"unknown platform: {platform!r}"}


def main() -> None:
    _reconfigure_streams()

    if len(sys.argv) != 3:
        print(
            json.dumps(
                {"ok": False, "error": f"usage: research_search_cli <platform> <max_results>, got {sys.argv[1:]}"}
            ),
            flush=True,
        )
        return

    platform = sys.argv[1]
    try:
        max_results = int(sys.argv[2])
    except ValueError:
        print(
            json.dumps({"ok": False, "error": f"max_results must be int, got {sys.argv[2]!r}"}),
            flush=True,
        )
        return

    keyword_raw = sys.stdin.readline()
    keyword = keyword_raw.rstrip("\n").rstrip("\r")

    try:
        result = _run(platform, max_results, keyword)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
