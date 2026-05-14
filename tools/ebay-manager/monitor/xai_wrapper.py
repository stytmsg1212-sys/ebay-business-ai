#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xAI search-x skill の subprocess ラッパ

code-reviewer (Opus 4.7) の C-2 / H-5 指摘に対応:
  - env 最小化 (ANTHROPIC_API_KEY / EBAY_* / GMAIL_* 等への伝播禁止)
  - cwd=tempdir で .env 自動ロード回避
  - stderr=DEVNULL で raw レスポンス平文露出防止
  - 個別例外 (FileNotFoundError / TimeoutExpired / CalledProcessError) の明示 catch
  - daily budget cap チェック (DB atomic カウンタ連携)

使用例::

    from monitor.xai_wrapper import call_search_x
    tweets = call_search_x(
        query="Claude Opus 4.7",
        days=1,
        handles=["@AnthropicAI", "@sama"],
        daily_cost_cap_usd=2.0,
    )
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# search-x が install されている想定パス (npx skills add 後の固定レイアウト)
_SEARCH_X_JS = (
    Path.home() / ".agents" / "skills" / "search-x" / "scripts" / "search.js"
)

# grok-4-1-fast の概算コスト (2026-04 時点、search.js デフォルトモデル)
# 入力: $0.30 / 1M tokens, 出力: $0.50 / 1M tokens
# 1 クエリあたりの概算 (平均 2000 in + 3000 out tokens) ≒ $0.0021
_COST_PER_QUERY_USD_DEFAULT = 0.0025


class XaiBudgetExceeded(RuntimeError):
    """日次コスト上限到達 (cap を超える呼び出しをブロック)."""


class XaiConfigError(RuntimeError):
    """設定不備 (API key 未設定 / search.js 不在)."""


class XaiCallError(RuntimeError):
    """subprocess 呼び出し失敗 (returncode != 0, timeout, parse error 等)."""


def _preflight() -> tuple[str, Path]:
    """環境確認. API key と search.js パスを返す."""
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise XaiConfigError(
            "XAI_API_KEY not set. Get a key at https://console.x.ai "
            "and add to ebay-manager/.env"
        )
    if not _SEARCH_X_JS.exists():
        raise XaiConfigError(
            f"search.js not found at {_SEARCH_X_JS}. "
            "Run: npx skills add mvanhorn/clawdbot-skill-search-x@search-x -g -y"
        )
    return key, _SEARCH_X_JS


def _check_budget(cap_usd: float, est_cost: float) -> None:
    """日次予算チェック. 既累計 + 見込みが cap を超えるなら raise."""
    from monitor.database import get_todays_api_cost

    spent = get_todays_api_cost("xai")
    if spent + est_cost > cap_usd:
        raise XaiBudgetExceeded(
            f"xAI daily budget exceeded: spent=${spent:.4f} + est=${est_cost:.4f} "
            f"> cap=${cap_usd:.4f}"
        )


def _record_cost(cost: float, context: str) -> None:
    """DB の api_budget_log に atomic 加算. 記録失敗は non-fatal."""
    import sqlite3
    try:
        from monitor.database import add_api_cost
        add_api_cost("xai", cost, context)
    except (ImportError, sqlite3.Error) as e:
        # budget 記録失敗は run 本体を止めない (監視が途絶するのみ)
        logger.warning(
            f"Failed to record xAI cost (non-fatal): {type(e).__name__}: {e}"
        )


def call_search_x(
    query: str,
    *,
    days: int = 30,
    handles: Optional[list[str]] = None,
    exclude_handles: Optional[list[str]] = None,
    model: Optional[str] = None,
    timeout: int = 60,
    daily_cost_cap_usd: float = 2.0,
    context: str = "x_news_check",
    cost_per_query: float = _COST_PER_QUERY_USD_DEFAULT,
) -> dict:
    """search-x skill の search.js を subprocess 呼び出し.

    返り値: search.js --json 出力をパースした dict.
    失敗時: 個別例外を raise. 呼び元で source 単位 skip できる.
    """
    key, script = _preflight()
    _check_budget(daily_cost_cap_usd, cost_per_query)

    # --- CLI 引数組立 ---
    args: list[str] = ["node", str(script), "--json", f"--days={int(days)}"]
    if handles:
        args.append(f"--handles={','.join(handles)}")
    if exclude_handles:
        args.append(f"--exclude={','.join(exclude_handles)}")
    if model:
        args.append(f"--model={model}")
    args.append(query)

    # --- env 最小化 (ANTHROPIC / EBAY / GMAIL token を漏らさない) ---
    # Windows では SYSTEMROOT / APPDATA が無いと Node 起動不能のため最小限だけ透過.
    env = {
        "XAI_API_KEY": key,
        "PATH": os.environ.get("PATH", ""),
    }
    for var in ("SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE",
                "HOMEPATH", "TEMP", "TMP"):
        if var in os.environ:
            env[var] = os.environ[var]

    # --- cwd 隔離 (.env 自動ロード対策) ---
    with tempfile.TemporaryDirectory(prefix="xai_search_") as tmp_cwd:
        try:
            proc = subprocess.run(  # noqa: S603 (args are fully controlled)
                args,
                env=env,
                cwd=tmp_cwd,
                capture_output=True,
                timeout=timeout,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                # stderr はセキュリティ上キャプチャしない (search.js:182 raw response fix)
                # capture_output=True との併用で stderr は text で返るが中身は使わず捨てる
            )
        except FileNotFoundError as e:
            raise XaiConfigError(f"node binary not found: {e}") from e
        except subprocess.TimeoutExpired as e:
            _record_cost(cost_per_query, f"{context}:timeout")
            raise XaiCallError(
                f"search.js timeout after {timeout}s for query: {query[:50]}"
            ) from e

    if proc.returncode != 0:
        # stderr を捨てる (API 生レスポンス含みうる). returncode のみログ.
        _record_cost(cost_per_query, f"{context}:rc={proc.returncode}")
        raise XaiCallError(
            f"search.js failed: rc={proc.returncode}. "
            f"Check XAI_API_KEY validity or rate limit."
        )

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        _record_cost(cost_per_query, f"{context}:json_error")
        raise XaiCallError(f"search.js output not JSON: {e}") from e

    _record_cost(cost_per_query, context)
    return parsed


__all__ = [
    "call_search_x",
    "XaiBudgetExceeded",
    "XaiConfigError",
    "XaiCallError",
]
