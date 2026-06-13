#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仕入先候補探索 full-pipeline subprocess エントリポイント (board#19 / 2026-06-13).

使い方:
    python -m tasks.supplier_search_cli <ebay_item_id> <discovered_via>
    # sku は stdin (UTF-8, 1 行) から読む (cp932 console 経由の文字化け回避)

背景 (W228 FIX-E = monitor/research_search_cli.py と同根):
    Streamlit プロセスは tornado 互換のため WindowsSelectorEventLoopPolicy が
    プロセス全体に設定され、SelectorEventLoop は Windows で asyncio subprocess
    非対応 → Playwright sync API がどの thread でも NotImplementedError で即死し、
    mercari/yahoo/paypay の search_* 内 except Exception が握りつぶして空リストを
    返す = UI 即時探索が常に「偽の市場 0 件」になっていた (Q0: 環境エラーを
    市場 0 件として表示)。本 CLI を subprocess として起動するとフレッシュな
    python プロセスが既定の ProactorEventLoop で Playwright を正常実行できる。

    research_search_cli (検索のみ) と異なり、本 CLI は評価 + 利益計算 + persist を
    含む run_supplier_candidate_search 全体を子プロセスで実行する (UI 即時探索は
    scheduler バッチと同じ結果 dict を必要とするため)。

出力 (stdout):
    成功:   RESULT_JSON:{"ok": true, "result": {...run_supplier_candidate_search 返り値...}}
    エラー: RESULT_JSON:{"ok": false, "error": "<type>: <msg>"}
    マーカー方式の理由: pipeline が import する多数モジュールの logging / print が
    stdout を汚しても、親が結果行だけを確実に拾えるようにするため
    (research_search_cli は検索のみで stdout が綺麗なため裸 JSON で足りたが、
    full pipeline では保証できない)。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

RESULT_MARKER = "RESULT_JSON:"


def _reconfigure_streams() -> None:
    """pythonw 環境でも stdout/stdin/stderr が UTF-8 になるよう再設定する (house パターン).

    stderr も含める (reviewer MEDIUM-2): 親は encoding="utf-8" で stderr を decode
    するため、cp932 のままだと日本語 log で親の reader が壊れ診断情報が消える。
    """
    for stream in (sys.stdout, sys.stdin, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _load_config() -> dict[str, Any]:
    """schedule_config.json を読む (UI 側 tab_inventory_monitor と同一ソース)."""
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Q0: 空 config fallback を無音にしない (stderr 経由で親 log に残る)
            logging.getLogger(__name__).warning(
                "schedule_config.json 読込失敗 (空 config で続行): %s", exc)
    return {}


def _run(ebay_item_id: str, sku: str, discovered_via: str) -> dict[str, Any]:
    """run_supplier_candidate_search を実行し marker 用 payload を返す."""
    from tasks.task_supplier_candidate_search import run_supplier_candidate_search

    result = run_supplier_candidate_search(
        ebay_item_id=ebay_item_id,
        sku=sku,
        config=_load_config(),
        discovered_via=discovered_via,
    )
    return {"ok": True, "result": result}


def main() -> None:
    _reconfigure_streams()
    # pipeline 内の logger 出力は stderr へ (stdout は RESULT_JSON 行のため温存)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    if len(sys.argv) != 3:
        payload: dict[str, Any] = {
            "ok": False,
            "error": f"usage: supplier_search_cli <ebay_item_id> <discovered_via>, got {sys.argv[1:]}",
        }
    else:
        ebay_item_id, discovered_via = sys.argv[1], sys.argv[2]
        sku = sys.stdin.readline().rstrip("\n").rstrip("\r")
        try:
            payload = _run(ebay_item_id, sku, discovered_via)
        except Exception as exc:
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(RESULT_MARKER + json.dumps(payload, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
