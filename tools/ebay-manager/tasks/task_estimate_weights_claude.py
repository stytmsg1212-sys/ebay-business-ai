#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude による weight 推定タスク

eBay listing のタイトル等から、送料計算用の重量(g)を Claude API で推定する。
`task_enrich_listings_physical` が default_500g でフォールバックした listing を
対象に、より精度の高い推定値で上書きする。

Model: Haiku 4.5（タイトル→数値推定は軽い判断で十分、コスト最小）
プロンプトキャッシュ: 共通指示（カテゴリ別重量目安など）をキャッシュ。

実行タイミング:
  - daily_scheduler の 02:30 枠で実行 (enrich_listings_physical の後)
  - 1回あたり max_items（デフォルト 50）を処理
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import (  # noqa: E402
    get_conn, update_ebay_listing_weight_estimate,
)

logger = logging.getLogger(__name__)

# .env 経由で ANTHROPIC_API_KEY ロード
try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """あなたは越境EC物販の発送業務で weight 推定を行う専門家です。
入力された商品タイトル・カテゴリから、発送時の実重量（梱包込み、g単位）を推定してください。

推定の指針:
- 工業計測器（KEYENCE/Mitutoyo/ADVANTEST等）: 500〜3000g が多い
- カセットデッキ・AV機器: 2000〜5000g
- 小型センサー/アンプユニット: 300〜800g
- レンズ・光学機器: 500〜2000g
- ケーブル・小物のみ: 100〜500g
- 大型機器（本体+筐体）: 3000〜10000g
- 梱包材・緩衝材を含めて最終発送重量を推定
- 不明瞭な場合は保守的に重めに推定（送料赤字を避ける）

confidence:
- 'high': 型番・スペックが明確で、類似品の重量が推定可能
- 'medium': カテゴリはわかるが具体的な型番情報が少ない
- 'low': 商品カテゴリ自体が不明瞭

出力: 厳密な JSON のみ（前後にテキスト禁止、```json フェンス禁止）
{"weight_g": 1234, "confidence": "medium", "reasoning": "一文の理由"}"""


def _estimate_with_claude(client, title: str, description: str = "") -> Optional[dict]:
    """1件分の推定。失敗時 None。"""
    user_content = f"Title: {title}\n"
    if description:
        user_content += f"Description (抜粋): {description[:500]}\n"
    user_content += "\n推定してください。"

    from monitor.api_logger import log_anthropic_response, _Timer
    msg = None
    try:
        with _Timer() as _t:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=200,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
        log_anthropic_response("weight_estimate", MODEL, msg,
                               duration_ms=_t.duration_ms, success=True)
    except Exception as e:
        logger.warning(f"Claude API error: {e}")
        log_anthropic_response("weight_estimate", MODEL, None,
                               success=False, error_message=str(e)[:500])
        return None

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )

    # fence や余分なテキストに耐える
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        logger.warning(f"no JSON in response: {text[:100]!r}")
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        logger.warning(f"JSON decode error: {e}, raw={text[:100]!r}")
        return None

    # weight_g が数値として取れるか
    try:
        w = float(data.get("weight_g") or 0)
    except (ValueError, TypeError):
        return None
    if w <= 0 or w > 50000:  # 50kg超過は不正扱い
        logger.warning(f"weight out of range: {w}")
        return None

    conf = data.get("confidence", "medium")
    if conf not in ("high", "medium", "low"):
        conf = "medium"

    return {
        "weight_g": int(round(w)),
        "confidence": conf,
        "reasoning": str(data.get("reasoning", ""))[:200],
    }


def _fetch_targets(limit: int) -> list[dict]:
    """Claude推定対象: default_500g でマークされた active listing。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ebay_item_id, sku, title
               FROM ebay_listings
               WHERE (is_ended IS NULL OR is_ended=0)
                 AND weight_source = 'default_500g'
               ORDER BY rank ASC, last_synced_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def run_estimate_weights_claude(config: dict) -> dict:
    """daily_scheduler から呼ばれる entry point。"""
    if not _ANTHROPIC_OK:
        return {"success": False, "processed": 0, "updated": 0,
                "message": "anthropic package not installed"}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"success": False, "processed": 0, "updated": 0,
                "message": "ANTHROPIC_API_KEY missing (.env)"}

    task_cfg = (config or {}).get("tasks_enabled", {}).get("estimate_weights_claude") or {}
    max_items = int(task_cfg.get("max_items_per_run", 50))
    sleep_sec = float(task_cfg.get("sleep_between_items_sec", 0.2))

    targets = _fetch_targets(max_items)
    if not targets:
        return {"success": True, "processed": 0, "updated": 0,
                "message": "推定対象の default_500g listing なし"}

    client = anthropic.Anthropic()
    logger.info(f"Claude weight推定 対象: {len(targets)}件")

    updated = 0
    errors = 0
    for idx, t in enumerate(targets, start=1):
        logger.info(f"  [{idx}/{len(targets)}] {t['ebay_item_id']} ({(t['title'] or '')[:60]})")
        est = _estimate_with_claude(client, t.get("title") or "")
        if not est:
            errors += 1
            continue
        try:
            update_ebay_listing_weight_estimate(
                t["ebay_item_id"], est["weight_g"], est["confidence"],
            )
            logger.info(f"    -> {est['weight_g']}g ({est['confidence']}): {est['reasoning']}")
            updated += 1
        except Exception as e:
            logger.warning(f"    DB update error: {e}")
            errors += 1

        if idx < len(targets) and sleep_sec > 0:
            time.sleep(sleep_sec)

    msg = f"{updated}件 推定 / {errors}件エラー / 対象{len(targets)}件"
    logger.info(f"Claude weight推定 完了: {msg}")
    return {
        "success": errors < len(targets),
        "processed": len(targets),
        "updated": updated,
        "errors": errors,
        "message": msg,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = json.loads(
        (Path(__file__).resolve().parent.parent / "config" / "schedule_config.json")
        .read_text(encoding="utf-8")
    )
    r = run_estimate_weights_claude(cfg)
    print(json.dumps(r, indent=2, ensure_ascii=False))
