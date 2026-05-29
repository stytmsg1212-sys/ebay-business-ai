#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Opus 4.7 → 4.8 移行の動作検証 (Q1 / K3 Goal-Driven) one-shot.

検証項目:
  1. 編集済みモジュールの import (parse / 構文チェック)
  2. research_router.choose_model が heavy/各 source で claude-opus-4-8 を返す
  3. api_logger._PRICING に claude-opus-4-8 があり _estimate_cost_usd が正しく計算
  4. 直接 SDK 経路 (opus_video_enricher / keyword batch) の model ID 定数が 4.8
  5. live API ping (claude-opus-4-8 が実 API で受理されるか + usage parse + cost 計算)

API キーは .env から load。**絶対に print/log しない**。
"""
from __future__ import annotations

import sys
from pathlib import Path

# tools/ebay-manager を import path に追加 (from monitor.xxx import ... 解決用)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((PASS if cond else FAIL, name, detail))


# ---------------------------------------------------------------------------
# 1. import (parse) チェック
# ---------------------------------------------------------------------------
try:
    from monitor import research_router, api_logger, opus_video_enricher
    from tasks import task_generate_search_keywords
    check("import 4 modules", True)
except Exception as e:  # noqa: BLE001 - 検証スクリプトなので broad catch で集約報告
    check("import 4 modules", False, f"{type(e).__name__}: {e}")
    # import 失敗時は以降の検証不能 → ここで report して exit
    for status, name, detail in results:
        print(f"[{status}] {name} {('- ' + detail) if detail else ''}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 2. routing チェック
# ---------------------------------------------------------------------------
m_heavy, think_heavy = research_router.choose_model("値付け戦略をどう設計すべきか", source="ui_chat")
check("routing heavy → opus-4-8", m_heavy == "claude-opus-4-8" and think_heavy is True,
      f"got ({m_heavy}, thinking={think_heavy})")

m_mb, _ = research_router.choose_model("今日の重点", source="morning_brief")
check("routing morning_brief → opus-4-8", m_mb == "claude-opus-4-8", f"got {m_mb}")

m_force, _ = research_router.choose_model("anything", force="opus")
check("routing force=opus → opus-4-8", m_force == "claude-opus-4-8", f"got {m_force}")

m_light, _ = research_router.choose_model("100g", source="ui_chat")
check("routing light → haiku (回帰なし)", m_light == "claude-haiku-4-5-20251001", f"got {m_light}")


# ---------------------------------------------------------------------------
# 3. pricing チェック
# ---------------------------------------------------------------------------
p = api_logger._PRICING.get("claude-opus-4-8")
check("pricing dict に opus-4-8 存在", p is not None, str(p))
if p:
    check("pricing input=$5 output=$25", p["input"] == 5.00 and p["output"] == 25.00, str(p))

# 1M in + 1M out = $5 + $25 = $30
cost = api_logger._estimate_cost_usd("claude-opus-4-8", 1_000_000, 1_000_000)
check("_estimate_cost_usd(1M,1M) ≈ $30", abs(cost - 30.0) < 0.01, f"got ${cost}")

# batch 50% off
cost_batch = api_logger._estimate_cost_usd("claude-opus-4-8", 1_000_000, 1_000_000, is_batch=True)
check("_estimate_cost_usd batch ≈ $15", abs(cost_batch - 15.0) < 0.01, f"got ${cost_batch}")


# ---------------------------------------------------------------------------
# 4. 直接 SDK 経路の model ID 定数
# ---------------------------------------------------------------------------
check("opus_video_enricher.OPUS_MODEL", opus_video_enricher.OPUS_MODEL == "claude-opus-4-8",
      opus_video_enricher.OPUS_MODEL)
check("task_generate_search_keywords.KEYWORD_MODEL",
      task_generate_search_keywords.KEYWORD_MODEL == "claude-opus-4-8",
      task_generate_search_keywords.KEYWORD_MODEL)


# ---------------------------------------------------------------------------
# 5. live API ping (claude-opus-4-8 が実 API で受理されるか)
# ---------------------------------------------------------------------------
live_detail = ""
live_ok = False
try:
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        live_detail = "ANTHROPIC_API_KEY 未設定 (skip)"
    else:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=5,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
        txt = "".join(getattr(b, "text", "") for b in msg.content
                      if getattr(b, "type", None) == "text").strip()
        in_t = getattr(msg.usage, "input_tokens", 0)
        out_t = getattr(msg.usage, "output_tokens", 0)
        ping_cost = api_logger._estimate_cost_usd("claude-opus-4-8", in_t, out_t)
        live_ok = bool(txt)  # 何か返れば model ID 受理 = OK
        live_detail = f"resp='{txt}' in={in_t} out={out_t} cost=${ping_cost:.6f}"
except Exception as e:  # noqa: BLE001
    live_detail = f"{type(e).__name__}: {e}"

check("live API ping claude-opus-4-8", live_ok, live_detail)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
print("=" * 60)
fails = 0
for status, name, detail in results:
    if status == FAIL:
        fails += 1
    print(f"[{status}] {name}{(' - ' + detail) if detail else ''}")
print("=" * 60)
print(f"{len(results)} checks, {fails} FAIL")
sys.exit(1 if fails else 0)
