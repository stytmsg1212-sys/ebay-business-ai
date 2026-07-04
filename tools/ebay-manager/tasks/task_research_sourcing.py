#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 Phase 3: gate_passed 候補の AI重量推定→フリマ探索→利益計算→承認キュー積みバッチ.

設計書: .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md §6 / §9
仕様書: .company/engineering/docs/2026-06-07-product-research-automation-spec.md

処理フロー (設計書 §9 Phase 3):
  1. config (schedule_config.json "research_sourcing") の enabled 確認 → disabled は skip + 痕跡 (Q0)
  2. 日次 AI コスト cap 確認 (fail-CLOSED: 集計失敗 = AI 呼び出し中断)
  3. gate_passed 候補を列挙 (max_items_per_run 上限)
  4. 各候補: weight_source='ai_estimate' 済を skip、それ以外は AI 重量推定 (Haiku 4.5)
     - 推定失敗 → needs_review (reason='重量推定失敗') で次へ (P1-1: 0 clip 禁止)
  5. evaluate_product() で B 工程実行 (フリマ探索→AI同一性→利益真値計算)
     - sourced + match_score < 60 → not_found に降格 + 利益値クリア
       (supplier-matching-rules: <60 は別商品 = 仕入先未発見。evaluate_product は
        手動 UI 前提「保存のみ、最終確定は人間」§2-B のため task 側で適用 /
        2026-06-11 Q1 実機で 15/15 件が誤マッチ profit のまま承認待ちに積まれて発覚)
     - sourced → keisuke_check の borderline 判定 → needs_review or awaiting_approval
     - not_found → awaiting_approval (在庫0+過去取引あり = 監視候補)
     - needs_review → そのまま保持
  6. Section 232 推定 → section232_flag / section232_reason を更新
  7. Discord 通知 (承認待ち N 件 + 商品名/利益、W257 教訓: 1900 字以内に truncate)
  8. 結果 dict 返却

Q0 silent skip 防止:
  - enabled=false → log + Discord 通知
  - コスト cap 超過 → 残候補は gate_passed のまま温存 (翌日処理)、中断件数をログ + Discord
  - 技術失敗 (検索エラー/計算不能) → needs_review (reason 必須)
  - 業務判断 (検索 0 件) → not_found (P2: 混同禁止)

P1-1: weight 欠落 = 0 clip で偽黒字を作らない → needs_review に落とす
P2:  技術失敗と業務判断を別状態で管理
W257 教訓: Discord embed/メッセージ長超過 400 対策 — 商品リストは最大 10 件に truncate

SQLite TIMESTAMP は UTC. コスト集計は get_todays_api_cost_by_context に委任 (自前日付 SQL 不可)。
pythonw ガード: print(file=sys.stderr) 禁止。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# pythonw ガード (quality-gate.sh hook: print(file=sys.stderr) は物理 BLOCK)
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# .env ロード
try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ---- コスト保護定数 (設計書 §6-4 / task_news_check.py HIGH-2 パターン準拠) ----
_DEFAULT_DAILY_COST_CAP_USD = 3.0
# 1 件処理前の安全マージン: AI 推定 (Haiku) + evaluate_product (Haiku) 最悪コスト見積
# Haiku 4.5 は 1 回 ~$0.001 レベル。バッファを大きめに取る
_SAFETY_MARGIN_USD = 0.05

# AI 重量推定モデル (task_estimate_weights_claude.py と同一)
_WEIGHT_MODEL = "claude-haiku-4-5-20251001"
_WEIGHT_SYSTEM_PROMPT = """あなたは越境EC物販の発送業務で weight 推定を行う専門家です。
入力された商品タイトルから、発送時の実重量（梱包込み、g単位）を推定してください。

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


def _get_sourcing_cfg(config: dict) -> dict:
    """schedule_config.json から research_sourcing セクションを取得."""
    tasks = config.get("tasks_enabled") or {}
    return tasks.get("research_sourcing") or {}


def _check_cost_cap(daily_cost_cap_usd: float) -> tuple[bool, float, Optional[str]]:
    """日次 AI コスト残量を確認し (ok, remaining, agg_error) を返す.

    fail-CLOSED: 集計例外 → (False, 0.0, エラー文字列) = AI 呼び出し中断。
    agg_error は「cap 到達 (正常 skip)」と「集計失敗 (技術失敗)」の判別用 —
    後者まで success=True で返すと偽装成功 (Q0) になるため、呼出側で分岐する。
    task_news_check.py HIGH-2 パターン準拠。
    """
    try:
        from monitor.database import get_todays_api_cost_by_context
        used = get_todays_api_cost_by_context("research_sourcing", provider="anthropic")
    except Exception as e:
        logger.warning(
            f"research_sourcing: コスト集計失敗 (fail-closed) = AI 呼び出し中断: {e}"
        )
        return False, 0.0, f"{type(e).__name__}: {e}"
    remaining = daily_cost_cap_usd - used
    return remaining >= _SAFETY_MARGIN_USD, remaining, None


def _estimate_weight(title_ja: str) -> Optional[dict]:
    """Haiku 4.5 で重量推定 (task_estimate_weights_claude.py の _estimate_with_claude 流用).

    Returns:
        {"weight_g": int, "confidence": str, "reasoning": str} or None (失敗時)
    """
    import re
    try:
        import anthropic
    except ImportError:
        logger.warning("research_sourcing: anthropic パッケージ未インストール → 重量推定不可")
        return None

    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("research_sourcing: ANTHROPIC_API_KEY 未設定 → 重量推定不可")
        return None

    client = anthropic.Anthropic(api_key=api_key)
    user_content = f"Title: {title_ja}\n\n推定してください。"

    msg = None
    try:
        from monitor.api_logger import log_anthropic_response, _Timer
        with _Timer() as _t:
            msg = client.messages.create(
                model=_WEIGHT_MODEL,
                max_tokens=200,
                system=[
                    {
                        "type": "text",
                        "text": _WEIGHT_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
        # context="research_sourcing" でコスト cap 集計と一致させる (設計書 §A)
        log_anthropic_response(
            "research_sourcing", _WEIGHT_MODEL, msg,
            duration_ms=_t.duration_ms, success=True,
        )
    except Exception as e:
        logger.warning(f"research_sourcing: Claude API 重量推定失敗: {e}")
        try:
            from monitor.api_logger import log_anthropic_response
            log_anthropic_response(
                "research_sourcing", _WEIGHT_MODEL, None,
                success=False, error_message=str(e)[:500],
            )
        except Exception as log_err:
            # API ログ記録自体の失敗 — 痕跡は残す (except-pass 禁止 / Q0)
            logger.warning(f"research_sourcing: API エラーのログ記録に失敗: {log_err}")
        return None

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        logger.warning(f"research_sourcing: 重量推定 JSON なし: {text[:100]!r}")
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        logger.warning(f"research_sourcing: 重量推定 JSON parse 失敗: {e}")
        return None

    weight_g = data.get("weight_g")
    if not isinstance(weight_g, (int, float)) or weight_g <= 0:
        logger.warning(f"research_sourcing: 重量推定値が不正: {weight_g!r}")
        return None

    return {
        "weight_g": float(weight_g),
        "confidence": str(data.get("confidence") or "low"),
        "reasoning": str(data.get("reasoning") or ""),
    }


def _save_weight_estimate(rc_id: int, weight_g: float, confidence: str) -> None:
    """research_candidates の manual_weight_g / weight_source / weight_confidence を更新."""
    from monitor.database import get_conn
    with get_conn() as conn:
        conn.execute(
            "UPDATE research_candidates "
            "SET manual_weight_g=?, weight_source='ai_estimate', weight_confidence=?, "
            "updated_at=CURRENT_TIMESTAMP "
            "WHERE rc_id=?",
            (weight_g, confidence, rc_id),
        )


def _save_section232(rc_id: int, flag: bool, annex: Optional[str], rate: Optional[float],
                     matched_keyword: Optional[str]) -> None:
    """Section 232 推定結果を research_candidates に保存."""
    from monitor.database import get_conn
    reason_parts = []
    if matched_keyword:
        reason_parts.append(f"キーワード一致: {matched_keyword!r}")
    if annex:
        rate_pct = f"{int(rate * 100)}%" if rate is not None else "不明"
        reason_parts.append(f"Annex {annex} ({rate_pct})")
    reason = " / ".join(reason_parts) if reason_parts else None

    with get_conn() as conn:
        conn.execute(
            "UPDATE research_candidates "
            "SET section232_flag=?, section232_reason=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE rc_id=?",
            (1 if flag else 0, reason, rc_id),
        )


def _send_discord(config: dict, message: str, severity: str = "info") -> bool:
    """Discord 通知ヘルパ (依頼ボード#22: notifier_for("research") 経由)."""
    from notifiers.discord_notifier import notifier_for
    notifier = notifier_for("research")
    if not notifier.webhook_url:
        logger.warning("research_sourcing: Discord webhook 未設定 — 通知 skip")
        return False
    color = {"info": 0x3399FF, "warn": 0xC89B2A, "error": 0xD84C38}.get(severity, 0x3399FF)
    try:
        embed = {
            "title": "W228 リサーチ探索 (04:30)",
            "description": message,
            "color": color,
            "timestamp": datetime.now().isoformat(),
        }
        return notifier.send_message("", embed=embed)
    except Exception as e:
        logger.warning(f"research_sourcing: Discord 送信失敗: {e}")
        return False


def _build_discord_message(
    awaiting_items: list[dict],
    counters: dict,
    remaining_usd: float,
    duration_sec: float,
) -> str:
    """Discord 通知メッセージを構築 (W257 教訓: 1900 字以内 / 最大 10 件)."""
    lines = [
        f"処理: {counters['processed']} 件",
        f"  sourced→承認待ち: {counters['awaiting_approval']} / "
        f"not_found→承認待ち: {counters['not_found_approval']}",
        f"  needs_review: {counters['needs_review']} / "
        f"同一性<60で見送り: {counters.get('low_match', 0)} / "
        f"コスト中断: {counters['cost_aborted']}",
        f"コスト残量: ${remaining_usd:.3f}",
        f"所要時間: {duration_sec:.0f}秒",
    ]
    if awaiting_items:
        lines.append(f"\n承認待ち ({min(len(awaiting_items), 10)}/{len(awaiting_items)} 件):")
        for item in awaiting_items[:10]:
            title = (item.get("title_ja") or "")[:30]
            profit = item.get("profit_jpy_true")
            profit_str = f"¥{profit:,}" if profit is not None else "利益未計算"
            # W265: 中古候補は売値減額済み = 利益が状態整合済みであることを明示
            cond_tag = ""
            if item.get("condition_is_used") == 1:
                cond = (item.get("found_condition_ja") or "中古")[:16]
                cond_tag = f" [中古:{cond}]"
            lines.append(f"  ・{title} {profit_str}{cond_tag}")
        if len(awaiting_items) > 10:
            lines.append(f"  他 {len(awaiting_items) - 10} 件")
        lines.append("→ 商品リサーチ(W228)タブで承認してください。")

    msg = "\n".join(lines)
    # 1900 字で安全に truncate (Discord embed description 上限 ~4096 だが W257 対策)
    if len(msg) > 1900:
        msg = msg[:1897] + "..."
    return msg


def run_research_sourcing(config: Optional[dict] = None) -> dict:
    """W228 Phase 3 sourcing バッチ本体.

    Returns:
        {
            'success': bool,
            'processed': int,
            'sourced': int,
            'not_found': int,
            'needs_review': int,
            'awaiting_approval': int,
            'not_found_approval': int,
            'low_match': int,
            'cost_aborted': int,
            'message': str,
            'errors': list[str],
        }
    """
    cfg = config or {}
    started_at = datetime.now()
    counters = {
        "processed": 0,
        "sourced": 0,
        "not_found": 0,
        "needs_review": 0,
        "awaiting_approval": 0,
        "not_found_approval": 0,
        "low_match": 0,  # match_score < floor で not_found に降格した件数
        "cost_aborted": 0,
    }
    result: dict = {
        "success": False,
        "message": "",
        "errors": [],
        **counters,
    }

    # ── 1. enabled チェック ───────────────────────────────────────────
    sourcing_cfg = _get_sourcing_cfg(cfg)
    if not sourcing_cfg.get("enabled", False):
        msg = "research_sourcing: enabled=false → skip (設計書 §6-2, Q0)"
        logger.info(msg)
        try:
            from daily_scheduler import _batch_ctx
            from monitor.task_execution_log import log_task_skip
            _bid = _batch_ctx.get("id")
            _bhr = _batch_ctx.get("hour")
            if _bid is not None and _bhr is not None:
                log_task_skip(
                    task_key="research_sourcing",
                    display_name="research_sourcing",
                    batch_id=_bid,
                    batch_hour=int(_bhr),
                    reason="disabled_by_config",
                    skip_kind="skip_disabled",
                )
        except Exception as _le:
            logger.warning(f"research_sourcing: log_task_skip 失敗: {_le}")
        _send_discord(cfg, msg, severity="info")
        result["message"] = msg
        result["success"] = True  # skip は正常終了
        return result

    daily_cost_cap_usd: float = float(
        sourcing_cfg.get("daily_cost_cap_usd", _DEFAULT_DAILY_COST_CAP_USD)
    )
    max_items: int = int(sourcing_cfg.get("max_items_per_run", 20))

    # ── 2. コスト cap 確認 (fail-CLOSED) ─────────────────────────────
    cost_ok, remaining, agg_error = _check_cost_cap(daily_cost_cap_usd)
    if agg_error:
        # 集計失敗 = 技術失敗。success=True で返すと「集計関数が壊れていても
        # 毎日 completed に見える」偽装成功 (Q0) になるため failed で返す。
        msg = (
            f"research_sourcing: コスト集計失敗 (fail-closed) → AI 呼び出し中断: {agg_error}"
        )
        logger.error(msg)
        _send_discord(cfg, msg, severity="error")
        result["message"] = msg
        result["errors"].append(msg)
        result["success"] = False
        return result
    if not cost_ok:
        msg = (
            f"research_sourcing: 日次コスト cap 到達 → 本日分 skip (翌日処理)。"
            f"残量 ${remaining:.3f} / cap ${daily_cost_cap_usd:.2f}"
        )
        logger.warning(msg)
        _send_discord(cfg, msg, severity="warn")
        result["message"] = msg
        result["success"] = True  # cap 到達 = 正常 skip
        return result

    # ── 3. gate_passed 候補列挙 ───────────────────────────────────────
    from monitor.research_candidates_db import (
        list_research_candidates,
        update_status,
        get_research_candidate,
        clear_profit_fields,
        clear_found_fields,
        STATUS_NEEDS_REVIEW,
        STATUS_SOURCED,
        STATUS_NOT_FOUND,
        STATUS_AWAITING_APPROVAL,
        STATUS_GATE_PASSED,
    )

    candidates = list_research_candidates(status=STATUS_GATE_PASSED, limit=max_items)
    if not candidates:
        msg = "research_sourcing: gate_passed 候補 0 件 → 処理なし"
        logger.info(msg)
        _send_discord(cfg, msg, severity="info")
        result["message"] = msg
        result["success"] = True
        return result

    logger.info(f"research_sourcing: gate_passed 候補 {len(candidates)} 件を処理開始")

    from monitor.research_poc import (
        evaluate_product,
        keisuke_check,
        MATCH_SCORE_SUGGESTED_FLOOR,
    )
    from monitor.research_section232 import estimate_section232

    awaiting_new_items: list[dict] = []  # Discord 通知用: 今回追加した awaiting_approval
    mid_loop_agg_failed = False  # ループ中のコスト集計失敗 (技術失敗) → success=False

    for cand in candidates:
        rc_id: int = int(cand["rc_id"])
        title_ja: str = cand.get("title_ja") or ""

        if not title_ja.strip():
            logger.warning(f"research_sourcing: rc_id={rc_id} title_ja が空 → skip")
            result["errors"].append(f"rc_id={rc_id}: title_ja が空")
            continue

        # ── コスト cap 残量チェック (1 件処理前) ────────────────────
        cost_ok, remaining, agg_error = _check_cost_cap(daily_cost_cap_usd)
        if not cost_ok:
            left = len(candidates) - counters["processed"]
            if agg_error:
                # 集計失敗 = 技術失敗 → success=False (HIGH-1 と同根、偽装成功防止)
                mid_loop_agg_failed = True
                msg = (
                    f"research_sourcing: コスト集計失敗 (fail-closed) で中断 → "
                    f"残 {left} 件を gate_passed のまま温存: {agg_error}"
                )
            else:
                msg = (
                    f"research_sourcing: コスト cap 到達 → 残 {left} 件を "
                    f"gate_passed のまま温存 (翌日処理)"
                )
            logger.warning(msg)
            result["errors"].append(msg)
            counters["cost_aborted"] = left
            break

        counters["processed"] += 1
        logger.info(f"research_sourcing: 処理中 rc_id={rc_id} title={title_ja[:50]!r}")

        # ── 4. AI 重量推定 ────────────────────────────────────────
        manual_weight_g: Optional[float] = cand.get("manual_weight_g")
        weight_source: Optional[str] = cand.get("weight_source")

        # weight_source='ai_estimate' 済は再推定 skip (設計書 §6-4)
        if manual_weight_g and manual_weight_g > 0 and weight_source == "ai_estimate":
            logger.info(
                f"research_sourcing: rc_id={rc_id} AI推定済 (weight={manual_weight_g}g) → 再推定 skip"
            )
        elif manual_weight_g and manual_weight_g > 0:
            # 手動入力済み (weight_source != 'ai_estimate') → AI 推定不要
            logger.info(
                f"research_sourcing: rc_id={rc_id} 手動重量入力済 ({manual_weight_g}g) → AI 推定 skip"
            )
        else:
            # 重量未設定 → AI 推定
            est = _estimate_weight(title_ja)
            if est is None:
                # P1-1: 推定失敗 → needs_review (0 clip 禁止)
                reason = "重量推定失敗 (Claude API エラー or JSON 不正)"
                logger.warning(f"research_sourcing: rc_id={rc_id} {reason}")
                result["errors"].append(f"rc_id={rc_id}: {reason}")
                try:
                    update_status(rc_id, STATUS_NEEDS_REVIEW, needs_review_reason=reason)
                except ValueError as e:
                    logger.error(f"research_sourcing: rc_id={rc_id} needs_review 遷移失敗: {e}")
                    result["errors"].append(f"rc_id={rc_id}: needs_review 遷移失敗: {e}")
                counters["needs_review"] += 1
                continue

            manual_weight_g = est["weight_g"]
            try:
                _save_weight_estimate(rc_id, manual_weight_g, est["confidence"])
                logger.info(
                    f"research_sourcing: rc_id={rc_id} AI重量推定完了 "
                    f"weight={manual_weight_g}g confidence={est['confidence']}"
                )
            except Exception as e:
                logger.error(f"research_sourcing: rc_id={rc_id} 重量保存失敗: {e}")
                result["errors"].append(f"rc_id={rc_id}: 重量保存失敗: {e}")
                # 保存失敗でも manual_weight_g は使って続行 (次ステップで再保存なし)

        # ── 5. B工程実行 (evaluate_product) ─────────────────────
        terapeak_avg_price_usd: Optional[float] = cand.get("terapeak_avg_price_usd")

        try:
            eval_result = evaluate_product(
                title_ja,
                rc_id=rc_id,
                manual_weight_g=manual_weight_g,
                terapeak_avg_price_usd=terapeak_avg_price_usd,
            )
        except ValueError as e:
            # rc_id 不存在 or title 不一致 (Q0 の ValueError = プログラムバグ)
            reason = f"evaluate_product ValueError: {e}"
            logger.error(f"research_sourcing: rc_id={rc_id} {reason}")
            result["errors"].append(f"rc_id={rc_id}: {reason}")
            try:
                update_status(rc_id, STATUS_NEEDS_REVIEW, needs_review_reason=reason)
            except ValueError as e2:
                logger.error(f"research_sourcing: rc_id={rc_id} needs_review 遷移失敗: {e2}")
            counters["needs_review"] += 1
            continue
        except Exception as e:
            # その他の技術失敗 = needs_review (P2)
            reason = f"evaluate_product 予期しない例外: {type(e).__name__}: {e}"
            logger.error(f"research_sourcing: rc_id={rc_id} {reason}")
            result["errors"].append(f"rc_id={rc_id}: {reason}")
            try:
                update_status(rc_id, STATUS_NEEDS_REVIEW, needs_review_reason=reason)
            except ValueError as e2:
                logger.error(f"research_sourcing: rc_id={rc_id} needs_review 遷移失敗: {e2}")
            counters["needs_review"] += 1
            continue

        final_status: str = eval_result.get("status", STATUS_NEEDS_REVIEW)

        # ── 6. Section 232 推定 ───────────────────────────────────
        try:
            s232 = estimate_section232(title_ja)
            _save_section232(
                rc_id,
                flag=s232["flag"],
                annex=s232["annex"],
                rate=s232["rate"],
                matched_keyword=s232["matched_keyword"],
            )
        except Exception as e:
            logger.warning(f"research_sourcing: rc_id={rc_id} Section232 保存失敗 (続行): {e}")

        # ── 6.5. 同一性スコア floor (supplier-matching-rules: <60 は別商品) ──
        # evaluate_product は手動 UI 前提「保存のみ、最終確定は人間」(§2-B) のため
        # match_score=0 でも sourced を返す。自動経路はここで業務判定に変換:
        # 最有力候補が別商品 = 仕入先未発見 (not_found)。誤マッチ商品の価格で
        # 計算された利益真値は虚偽数値のためクリアする (verify_numbers)。
        # found_url / match_score / match_reason は棄却監査痕跡として残す。
        if final_status == STATUS_SOURCED:
            match_score = eval_result.get("match_score")
            if match_score is not None and match_score < MATCH_SCORE_SUGGESTED_FLOOR:
                try:
                    clear_profit_fields(rc_id)
                    update_status(rc_id, STATUS_NOT_FOUND)
                    final_status = STATUS_NOT_FOUND
                    counters["low_match"] += 1
                    logger.info(
                        f"research_sourcing: rc_id={rc_id} match_score={match_score} < "
                        f"{MATCH_SCORE_SUGGESTED_FLOOR} → 別商品判定 = not_found に降格 "
                        "(利益値クリア)"
                    )
                except ValueError as e:
                    logger.error(
                        f"research_sourcing: rc_id={rc_id} not_found 降格失敗: {e}"
                    )
                    result["errors"].append(f"rc_id={rc_id}: not_found 降格失敗: {e}")
                    continue

        # ── 7. awaiting_approval / needs_review 遷移 ────────────────
        if final_status == STATUS_SOURCED:
            counters["sourced"] += 1
            # keisuke borderline チェック
            keisuke_detail = eval_result.get("keisuke_detail") or {}
            is_borderline = bool(keisuke_detail.get("borderline", False))

            if is_borderline:
                # 境界±20% → needs_review (設計書 §14-Q2)
                reason = "けいすけ基準境界±20%帯 (AI推定重量の誤差で利益判定が反転し得る)"
                logger.info(
                    f"research_sourcing: rc_id={rc_id} borderline → needs_review"
                )
                try:
                    update_status(rc_id, STATUS_NEEDS_REVIEW, needs_review_reason=reason)
                except ValueError as e:
                    logger.error(f"research_sourcing: rc_id={rc_id} needs_review 遷移失敗: {e}")
                    result["errors"].append(f"rc_id={rc_id}: needs_review 遷移失敗: {e}")
                counters["needs_review"] += 1
            else:
                # sourced → awaiting_approval
                try:
                    update_status(rc_id, STATUS_AWAITING_APPROVAL)
                    counters["awaiting_approval"] += 1
                    refreshed = get_research_candidate(rc_id)
                    if refreshed:
                        awaiting_new_items.append(refreshed)
                    logger.info(
                        f"research_sourcing: rc_id={rc_id} → awaiting_approval"
                    )
                except ValueError as e:
                    logger.error(
                        f"research_sourcing: rc_id={rc_id} awaiting_approval 遷移失敗: {e}"
                    )
                    result["errors"].append(
                        f"rc_id={rc_id}: awaiting_approval 遷移失敗: {e}"
                    )

        elif final_status == STATUS_NOT_FOUND:
            counters["not_found"] += 1
            # not_found → awaiting_approval (在庫0+過去取引ありで監視候補 / 設計書 §4-3)
            # gate_inputs_json の sold_1_2yr で判定
            gate_inputs = {}
            try:
                gate_inputs = json.loads(cand.get("gate_inputs_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                pass

            sold_1_2yr = gate_inputs.get("sold_1_2yr") or 0
            if sold_1_2yr and sold_1_2yr > 0:
                # 承認キューに戻す前に誤マッチ仕入先フィールドを必ず除去
                # (残すと承認 UI が found_url/found_price_jpy を下書きに消費 =
                #  誤商品 URL・虚偽原価の draft 汚染。retrospective H1 / rc 36)
                if not clear_found_fields(rc_id):
                    logger.error(
                        f"research_sourcing: rc_id={rc_id} found フィールド除去失敗 "
                        "→ 汚染防止のため監視候補再キュー中止 (not_found のまま)"
                    )
                    result["errors"].append(
                        f"rc_id={rc_id}: clear_found_fields 失敗 → 再キュー中止"
                    )
                    continue
                try:
                    update_status(rc_id, STATUS_AWAITING_APPROVAL)
                    counters["not_found_approval"] += 1
                    refreshed = get_research_candidate(rc_id)
                    if refreshed:
                        awaiting_new_items.append(refreshed)
                    logger.info(
                        f"research_sourcing: rc_id={rc_id} not_found → awaiting_approval "
                        f"(sold_1_2yr={sold_1_2yr})"
                    )
                except ValueError as e:
                    logger.error(
                        f"research_sourcing: rc_id={rc_id} awaiting_approval 遷移失敗: {e}"
                    )
                    result["errors"].append(
                        f"rc_id={rc_id}: awaiting_approval 遷移失敗: {e}"
                    )
            else:
                # sold_1_2yr=0 の not_found は承認キューに積まない
                logger.info(
                    f"research_sourcing: rc_id={rc_id} not_found (sold_1_2yr=0) "
                    "→ 承認キュー対象外"
                )

        elif final_status == STATUS_NEEDS_REVIEW:
            counters["needs_review"] += 1
            logger.info(
                f"research_sourcing: rc_id={rc_id} → needs_review "
                f"(reason={eval_result.get('needs_review_reason')!r})"
            )
        else:
            # 想定外 status — Q0: silent に見逃さない
            logger.warning(
                f"research_sourcing: rc_id={rc_id} 想定外の final_status={final_status!r}"
            )
            result["errors"].append(
                f"rc_id={rc_id}: 想定外 status={final_status!r}"
            )

    # ── 8. Discord 通知 ───────────────────────────────────────────
    duration_sec = (datetime.now() - started_at).total_seconds()
    _, remaining_final, _ = _check_cost_cap(daily_cost_cap_usd)
    total_awaiting = counters["awaiting_approval"] + counters["not_found_approval"]

    discord_msg = _build_discord_message(
        awaiting_items=awaiting_new_items,
        counters={**counters, "awaiting_approval": total_awaiting},
        remaining_usd=remaining_final,
        duration_sec=duration_sec,
    )
    severity = "error" if result["errors"] else "info"
    _send_discord(cfg, discord_msg, severity=severity)

    # ── 9. 結果 dict 更新 ─────────────────────────────────────────
    result.update(counters)
    result["awaiting_approval"] = total_awaiting
    # 集計失敗 break は技術失敗 = failed (HIGH-1 と同根)。cap 到達 break は正常。
    result["success"] = not mid_loop_agg_failed
    result["message"] = discord_msg
    logger.info(f"research_sourcing 完了: {discord_msg}")
    return result
