"""W122: 朝 07:00 に Opus 4.8 が新商品候補 3 件を発掘.

5 階層構造のうち Phase 1 では階層 1+2+3 を実装:
- 階層 1 (horizontal_pattern): 自社売れ筋の兄弟製品・季節新色・上位機
- 階層 2 (meta_pattern): メタパターン拡張 (5 条件) で別ジャンル展開
- 階層 3 (competitor_sold): 既知日本セラーの sold 領域 (Phase 1 は seller リストを Opus に提示)

保存先: research_qa (source='morning_discovery') + morning_discovery_candidates 子テーブル.
1 日 1 回のみ生成 (重複防止). 既存 morning_brief (W24, 02:30) とは独立並設.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "monitor.db"


def _today_discovery_exists() -> bool:
    """SQLite の asked_at は UTC 保存 (sqlite-timezone.md). JST 日換算で比較."""
    with sqlite3.connect(str(DB_PATH)) as con:
        row = con.execute(
            "SELECT COUNT(*) FROM research_qa "
            "WHERE source='morning_discovery' "
            "  AND date(asked_at, '+9 hours') = date('now', '+9 hours')"
        ).fetchone()
    return (row[0] if row else 0) > 0


def _fetch_top_sellers(limit: int = 20) -> list[dict]:
    """自社売れ筋 TOP N (sales_count_30d DESC)."""
    with sqlite3.connect(str(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT title, current_price, watch_count, sales_count_30d, rank, sku
               FROM ebay_listings
               WHERE sales_count_30d > 0
               ORDER BY sales_count_30d DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_recent_sold(days: int = 90, limit: int = 30) -> list[dict]:
    """直近 N 日で実際に売れた取引実績 (sales_history テーブルから取得).

    Q0: DB 接続失敗時は空リストを返しログに記録する (silent fail 禁止)。
    query 呼び出し側は空リストでも正常動作すること。
    """
    try:
        from monitor.database import get_recent_sold_for_discovery
        return get_recent_sold_for_discovery(days=days, limit=limit)
    except Exception as e:
        logger.warning(
            f"_fetch_recent_sold 失敗 (sales_history 取得不可): {type(e).__name__}: {e}"
        )
        return []


def _fetch_user_decisions(days: int = 14) -> list[dict]:
    """過去 N 日の user 判定履歴 (Few-shot 注入用).

    SQLite の created_at は UTC 保存. 相対範囲 datetime('now', '-N days') で比較.
    """
    with sqlite3.connect(str(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"""SELECT product_name, user_decision, user_comment, star_rating,
                      layer_origin, created_at
               FROM morning_discovery_candidates
               WHERE user_decision IS NOT NULL
                 AND created_at >= datetime('now', '-{int(days)} days')
               ORDER BY user_decided_at DESC""",
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_known_jp_sellers() -> list[str]:
    """既知の日本セラー (過去レポート由来、Phase 1 hardcode).

    将来 Phase 2 で competitor_products テーブルから動的取得に切替予定.
    """
    return [
        "k.moto77",  # Vintage Walkman / Cassette
        "shiba_00_japan",  # Fujifilm instax
        "norifuku13",  # 90s Toys
        "mdrllc",  # Vintage Barbie Japan exclusive
        "mow-motor-law_nippon",  # Antique
        "flowerg2z",  # Anime Manga
        "pilina2.2.16",  # Sony Walkman vintage
        "tsumikichi030303.shop",  # Hatsune Miku 系
    ]


def _build_discovery_query(
    top_sellers: list[dict],
    user_decisions: list[dict],
    known_jp_sellers: list[str],
    is_monday: bool,
    recent_sold: list[dict] | None = None,
) -> str:
    """Opus 4.8 への query 構築 (5 階層構造 Phase 1)."""
    sellers_md = "| 商品名 | 価格 | Watch | Sold(30d) | Rank |\n|---|---|---|---|---|\n"
    for s in top_sellers:
        sellers_md += (
            f"| {(s.get('title') or '')[:60]} | "
            f"${(s.get('current_price') or 0):.0f} | "
            f"{s.get('watch_count') or 0} | "
            f"{s.get('sales_count_30d') or 0} | "
            f"{s.get('rank') or '?'} |\n"
        )

    decisions_md = "(なし - 初回実行)"
    if user_decisions:
        decisions_md = "| 商品 | 判定 | コメント | ★ |\n|---|---|---|---|\n"
        for d in user_decisions:
            comment = (d.get('user_comment') or '')[:60]
            decisions_md += (
                f"| {(d.get('product_name') or '')[:50]} | "
                f"{d.get('user_decision')} | {comment} | "
                f"{d.get('star_rating') or '?'} |\n"
            )

    # 実 sold 実績 (sales_history テーブル由来、直近 90 日)
    # recent_sold が空リストの場合は「実績なし」と明示してフォールバック説明を添える
    if recent_sold:
        sold_md = "| 商品名 (実際に売れた) | 販売件数 | 平均価格 | 最終販売日 |\n|---|---|---|---|\n"
        for r in recent_sold:
            last_sold = (r.get('last_sold_at') or '')[:10]  # YYYY-MM-DD のみ
            sold_md += (
                f"| {(r.get('title') or '')[:60]} | "
                f"{r.get('sold_count') or 0}件 | "
                f"${(r.get('avg_price_usd') or 0):.0f} | "
                f"{last_sold} |\n"
            )
    else:
        sold_md = "(実 sold 実績なし: sales_history 取得失敗 or 対象期間に販売なし。上記の Sold(30d) 推定値を参考にしてください)"

    sellers_list = ", ".join(known_jp_sellers)

    monday_hint = ""
    if is_monday:
        monday_hint = (
            "\n## 月曜限定タスク\n"
            "今週のレトロガジェット / 日本限定家電 / Japan exclusive コラボの "
            "最新ニュースを WebSearch で 1-2 件確認し、候補に反映してください.\n"
        )

    return f"""# 本日の MonoHonpo 新商品発掘リサーチ

毎朝 07:00 の自動発掘. 5 階層構造のうち Phase 1 では階層 1+2+3 を実装.

## 売れる 5 条件 (評価軸)
1. 機能の組み合わせがニッチ (有線NCイヤホン / Bluetoothカセット 等)
2. 年式・コラボ限定で即廃番 (Baccarat 年限定 / Beams コラボ 等)
3. 廃番品の新品在庫 (Pioneer Lightning NC 等)
4. 海外正規販売なしの仕様・色 (Le Creuset Japan exclusive 等)
5. コア層に刺さる極端なスペック (320 色マーカー / 計測器 等)

## 除外条件
- 海外で正規販売されている (除く廃番品)
- 電池・危険物で SpeedPAK Economy 不可
- 関税込み利益率 20% 未満
- VeRO リスク high

## 階層構造 (3 件抽出、各階層 1 件ずつ)
- 階層 1 (layer_origin='horizontal_pattern'): 自社売れ筋の兄弟製品・季節新色・上位機
- 階層 2 (layer_origin='meta_pattern'): 5 条件のいずれかで全く別ジャンルへ拡張
- 階層 3 (layer_origin='competitor_sold'): 既知日本セラー [{sellers_list}] が売っているが自社未出品の領域

## 自社売れ筋 TOP {len(top_sellers)}

{sellers_md}

## 自社実 sold 実績 (直近 90 日、sales_history DB 由来)

**以下は推定ではなく、実際に eBay で成約した取引実績です。**
階層 1 (horizontal_pattern) では、この実績リストに含まれる商品のカテゴリ・ブランド・
機能軸を優先的に水平展開してください (兄弟製品・上位機・季節別モデル 等)。

{sold_md}

## 過去 14 日の user 判定履歴 (Few-shot 学習)

{decisions_md}

判定 'buy' / 'listed' の候補と類似パターンは優先, 'skip' は deprioritize.
{monday_hint}

## 出力フォーマット (厳密な JSON のみ、前後に説明文不要)

```json
{{
  "candidates": [
    {{
      "rank": 1,
      "layer_origin": "horizontal_pattern",
      "product_name": "商品名",
      "rationale": "売れる根拠 1-2 行 (5 条件のどれに該当か明示)",
      "supplier_price_jpy": 12000,
      "ebay_estimated_price_usd": 180,
      "estimated_profit_usd": 35,
      "similar_sold_count_30d": 8,
      "competitor_jp_count": 2,
      "vero_risk_level": "none",
      "star_rating": 4,
      "next_action": "次にやること 1-2 行 (どこで仕入れるか等)",
      "source_urls": ["https://..."]
    }}
  ]
}}
```

確信度の低い数値項目は null 許容 (similar_sold_count_30d / competitor_jp_count / supplier_price_jpy 等).
ただし `estimated_profit_usd` (想定粗利 USD) は **必ず数値で返す** (null 不可).
見積不能なら 0 を返し、見積不能な理由を rationale に明記すること.
理由: user の買う/見送る判断の核心軸であり、null は判断材料を奪うため.

必ず 3 件 (各階層 1 件) 出力."""


def _parse_response(answer_md: str) -> Optional[list[dict]]:
    """Opus 回答から JSON candidates を抽出 (D6: parse 失敗時は None)."""
    m = re.search(r'```(?:json)?\s*(.*?)\s*```', answer_md, re.DOTALL)
    raw = m.group(1) if m else answer_md
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m2 = re.search(r'\{[\s\S]*?"candidates"[\s\S]*\}', raw)
        if not m2:
            return None
        try:
            data = json.loads(m2.group(0))
        except json.JSONDecodeError:
            return None
    candidates = data.get('candidates') if isinstance(data, dict) else None
    if not isinstance(candidates, list):
        return None
    return candidates


def _save_candidates(qa_id: int, candidates: list[dict]) -> int:
    """morning_discovery_candidates に保存."""
    saved = 0
    with sqlite3.connect(str(DB_PATH)) as con:
        for idx, c in enumerate(candidates[:3]):
            try:
                con.execute(
                    """INSERT INTO morning_discovery_candidates
                       (qa_id, candidate_rank, product_name, rationale,
                        supplier_price_jpy, ebay_estimated_price_usd, estimated_profit_usd,
                        similar_sold_count_30d, competitor_jp_count, vero_risk_level,
                        star_rating, next_action, source_urls, layer_origin)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        qa_id,
                        int(c.get('rank') or idx + 1),
                        str(c.get('product_name') or '(no name)')[:200],
                        str(c.get('rationale') or '')[:1000],
                        c.get('supplier_price_jpy'),
                        c.get('ebay_estimated_price_usd'),
                        c.get('estimated_profit_usd'),
                        c.get('similar_sold_count_30d'),
                        c.get('competitor_jp_count'),
                        str(c.get('vero_risk_level') or 'unknown')[:20],
                        c.get('star_rating'),
                        str(c.get('next_action') or '')[:500],
                        json.dumps(c.get('source_urls', []), ensure_ascii=False),
                        str(c.get('layer_origin') or 'unknown')[:32],
                    ),
                )
                saved += 1
            except sqlite3.OperationalError as e:
                logger.error(
                    f"morning_discovery_candidates INSERT 失敗 "
                    f"rank={c.get('rank')}: {e}"
                )
    return saved


def _get_webhook_url(config: Optional[dict]) -> str:
    """Discord webhook URL を取得.

    優先順:
      1. 渡された config の discord.webhook_url (scheduler 経由 schedule_config.json)
      2. 渡された config の discord_webhook_url 直書き (settings.json 経由)
      3. fallback: config/schedule_config.json を直接 load (UI 手動実行ボタン用)
    """
    if config:
        wh = (
            (config.get("discord") or {}).get("webhook_url")
            or config.get("discord_webhook_url")
            or ""
        )
        if wh:
            return wh
    # fallback: schedule_config.json (scheduler が読む正規 location)
    try:
        import json as _json
        sched_cfg_path = (
            Path(__file__).resolve().parent.parent
            / "config" / "schedule_config.json"
        )
        if sched_cfg_path.exists():
            with open(sched_cfg_path, encoding="utf-8") as f:
                sched_cfg = _json.load(f)
            return (sched_cfg.get("discord") or {}).get("webhook_url") or ""
    except (OSError, ValueError) as e:
        logger.warning(f"schedule_config.json load 失敗: {e}")
    return ""


def _send_discord(
    qa_id: int,
    candidates: list[dict],
    config: Optional[dict] = None,
    warning: str = "",
) -> None:
    """Discord 通知 (D3: 毎日送信). webhook_url 未設定なら skip.

    warning: Haiku fallback 等の警告メッセージ (空でなければ先頭に追加)
    """
    # board#22: 商品発掘は research ch (未設定なら既定 ch に fallback)
    from notifiers.discord_notifier import resolve_webhook
    webhook_url = resolve_webhook("research")
    if not webhook_url:
        logger.info("Discord webhook 未設定、通知 skip")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"今日の発掘候補 - {today}"]
    if warning:
        lines.append(warning)
    if not candidates:
        lines.append("発掘候補なし (Opus 4.8 は今朝 0 件を返答)")
    else:
        for c in candidates[:3]:
            name = (c.get('product_name') or '(no name)')[:60]
            star = c.get('star_rating') or '?'
            rationale = (c.get('rationale') or '')[:80]
            profit = c.get('estimated_profit_usd')
            # W129 (2026-05-15): profit=0 は「見積不能シグナル」(prompt L181-184 で null 不可
            # → 0+理由 rationale 制約). $0 と表示すると赤字判定と誤読され Few-shot 履歴を歪める.
            if isinstance(profit, (int, float)):
                profit_str = (
                    "想定粗利 見積不能 (理由は根拠欄)"
                    if profit == 0 else f"想定粗利 ${profit:.0f}"
                )
            else:
                profit_str = ""
            lines.append(f"#{c.get('rank', '?')} {name} (★{star})")
            lines.append(f"   {rationale} {profit_str}".rstrip())
    lines.append("→ MonoDeck の『今日の発掘』タブで詳細")

    try:
        import httpx
        r = httpx.post(
            webhook_url, json={"content": "\n".join(lines)}, timeout=10,
        )
        if r.status_code not in (200, 204):
            logger.warning(
                f"Discord 通知 HTTP {r.status_code}: {(r.text or '')[:200]}"
            )
    except Exception as e:  # httpx.HTTPError + OSError + その他
        # httpx import error 等も拾うため広めに. ただし silent ではなく warn 記録.
        logger.warning(f"Discord 送信失敗: {type(e).__name__}: {e}")


def run_morning_discovery(
    config: Optional[dict] = None, dry_run: bool = False
) -> dict:
    """daily_scheduler から呼ばれる. 1 日 1 回のみ生成."""
    # ── enabled ゲート (2026-07-03 user承認で生成停止、task_research_harvest と同パターン) ──
    cfg = config or {}
    md_cfg = (cfg.get("tasks_enabled") or {}).get("morning_discovery") or {}
    if not md_cfg.get("enabled", True):
        msg = "morning_discovery: enabled=false → skip (2026-07-03 user承認で生成停止, Q0 痕跡)"
        logger.info(msg)
        try:
            from daily_scheduler import _batch_ctx
            from monitor.task_execution_log import log_task_skip
            _bid = _batch_ctx.get("id")
            _bhr = _batch_ctx.get("hour")
            if _bid is not None and _bhr is not None:
                log_task_skip(
                    task_key="morning_discovery",
                    display_name="W122 朝の新商品発掘",
                    batch_id=_bid,
                    batch_hour=int(_bhr),
                    reason="disabled_by_config",
                    skip_kind="skip_disabled",
                )
        except Exception as _le:  # noqa: BLE001
            logger.warning(f"morning_discovery: log_task_skip 失敗: {_le}")
        return {
            "success": True,  # skip は正常終了 (偽装成功ではない)
            "skipped": True,
            "message": msg,
        }
    if _today_discovery_exists() and not dry_run:
        logger.info("morning_discovery: 本日分は既に生成済 (skip)")
        return {
            "success": True,
            "skipped": True,
            "message": "morning_discovery 本日分既存 (skip)",
        }

    top_sellers = _fetch_top_sellers(limit=20)
    user_decisions = _fetch_user_decisions(days=14)
    known_jp_sellers = _fetch_known_jp_sellers()
    recent_sold = _fetch_recent_sold(days=90, limit=30)
    is_monday = datetime.now().weekday() == 0

    logger.info(
        f"morning_discovery: データ取得完了 "
        f"top_sellers={len(top_sellers)} recent_sold={len(recent_sold)} "
        f"decisions={len(user_decisions)}"
    )

    if not top_sellers:
        logger.warning("自社売れ筋 0 件 = リサーチ skip")
        return {
            "success": False,
            "message": "自社売れ筋 0 件、リサーチ条件不足",
        }

    query = _build_discovery_query(
        top_sellers, user_decisions, known_jp_sellers, is_monday,
        recent_sold=recent_sold,
    )

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "query_preview": query[:1500],
            "top_sellers_count": len(top_sellers),
            "user_decisions_count": len(user_decisions),
            "recent_sold_count": len(recent_sold),
            "is_monday": is_monday,
        }

    try:
        from monitor.research_brain import ask
    except ImportError as e:
        logger.error(f"research_brain import 失敗: {e}")
        return {"success": False, "message": f"import error: {e}"}

    logger.info(
        f"morning_discovery: Opus 4.8 リサーチ開始 "
        f"(sellers={len(top_sellers)}, decisions={len(user_decisions)}, "
        f"monday={is_monday})"
    )
    answer = ask(
        query,
        source="morning_discovery",
        force_model="opus",
        enable_thinking=False,
        save_history=True,
        # 2026-05-22 W122-fix: 240s → 480s. 5/21 は 184s で成功したが 5/22 は
        # 240s でタイムアウト (sellers=20, decisions=4, monday=False)。day-by-day
        # で query 規模が変動するため、$2.50 budget cap を超えない範囲で
        # ヘッドルームを 2x 確保. CLAUDE_CLI_DEFAULT_TIMEOUT(120) の 4 倍.
        timeout=480,
        max_budget_usd=2.50,  # 5 階層構造 + 自社売れ筋 20 件で Opus は $1-2 消費見込
    )

    # D6-a: API エラー時 (placeholder row + Discord)
    if answer.error:
        logger.error(f"morning_discovery 失敗: {answer.error}")
        if answer.qa_id:
            try:
                with sqlite3.connect(str(DB_PATH)) as con:
                    con.execute(
                        """INSERT INTO morning_discovery_candidates
                           (qa_id, candidate_rank, product_name, rationale,
                            layer_origin)
                           VALUES (?, 0, ?, ?, 'error')""",
                        (answer.qa_id, '(API error)', str(answer.error)[:500]),
                    )
            except sqlite3.OperationalError:
                pass
        try:
            _send_discord(answer.qa_id, [], config)
        except Exception as e:
            # Q0 silent skip 防止: Discord (error path) 失敗を logger.warning で痕跡記録
            logger.warning(
                f"Discord 通知 (error path) 失敗: {type(e).__name__}: {e}"
            )
        return {
            "success": False,
            "qa_id": answer.qa_id,
            "message": f"morning_discovery failed: {answer.error}",
        }

    # D6-b: JSON parse 失敗 / 候補 0 件 (placeholder row + Discord)
    # M-3 fix: success=False を返す. task_execution_log で failed 記録 + MonoDeck 赤表示.
    candidates = _parse_response(answer.answer_md)
    if not candidates:
        logger.warning("morning_discovery: JSON parse 失敗 or 候補 0 件")
        try:
            with sqlite3.connect(str(DB_PATH)) as con:
                con.execute(
                    """INSERT INTO morning_discovery_candidates
                       (qa_id, candidate_rank, product_name, rationale,
                        layer_origin)
                       VALUES (?, 0, ?, ?, 'parse_error')""",
                    (
                        answer.qa_id,
                        '(parse failed or empty)',
                        (answer.answer_md or '')[:1000],
                    ),
                )
        except sqlite3.OperationalError:
            pass
        try:
            _send_discord(
                answer.qa_id, [], config,
                warning="JSON parse 失敗 or 候補 0 件 (placeholder)",
            )
        except Exception as e:
            # Q0 silent skip 防止: Discord (parse_error path) 失敗を logger.warning で痕跡記録
            logger.warning(
                f"Discord 通知 (parse_error path) 失敗: {type(e).__name__}: {e}"
            )
        return {
            "success": False,  # M-3: 失敗扱いに変更
            "qa_id": answer.qa_id,
            "candidates_saved": 0,
            "message": "Opus 応答の JSON parse 失敗 or 候補 0 件 (placeholder 保存)",
        }

    saved = _save_candidates(answer.qa_id, candidates)

    # H-3: Haiku fallback 検知 → 偽装成功防止 (Q0)
    # ask() 内で Opus quota over 時に Haiku に降格されると user は気付けないため、
    # model_used を check して明示警告.
    fallback_warning = ""
    if "haiku" in (answer.model_used or "").lower():
        fallback_warning = (
            f"Opus 4.8 quota over で Haiku fallback. "
            f"発掘品質が低下している可能性 (model={answer.model_used})."
        )
        logger.warning(f"morning_discovery: {fallback_warning}")

    # M-4 partial: saved < len(candidates) なら警告
    drop_warning = ""
    if saved < len(candidates[:3]):
        drop_warning = f"candidates {len(candidates[:3])} 件中 {saved} 件のみ保存."
        logger.warning(f"morning_discovery: {drop_warning}")

    try:
        _send_discord(answer.qa_id, candidates, config, warning=fallback_warning)
    except Exception as e:
        logger.warning(f"Discord 通知失敗: {e}")

    logger.info(
        f"morning_discovery: 生成完了 qa_id={answer.qa_id} "
        f"saved={saved} ({answer.duration_ms}ms) model={answer.model_used}"
    )
    return {
        "success": True,
        "qa_id": answer.qa_id,
        "candidates_saved": saved,
        "model_used": answer.model_used,
        "fallback_warning": fallback_warning,
        "drop_warning": drop_warning,
        "duration_ms": answer.duration_ms,
        "message": (
            f"morning_discovery 完了: {saved} 件"
            + (f" ({fallback_warning})" if fallback_warning else "")
        ),
    }


def get_today_candidates() -> list[dict]:
    """DASHBOARD 表示用: 本日 (JST) の発掘候補.

    SQLite の asked_at は UTC 保存 (sqlite-timezone.md). +9 hours で JST 比較.
    """
    with sqlite3.connect(str(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT mdc.*, rq.asked_at as session_asked_at
               FROM morning_discovery_candidates mdc
               JOIN research_qa rq ON mdc.qa_id = rq.id
               WHERE rq.source='morning_discovery'
                 AND date(rq.asked_at, '+9 hours') = date('now', '+9 hours')
               ORDER BY mdc.candidate_rank"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_feedback(days: int = 7) -> list[dict]:
    """過去 N 日のフィードバック履歴 (UTC 保存、相対範囲で比較)."""
    with sqlite3.connect(str(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"""SELECT mdc.*, rq.asked_at as session_asked_at
               FROM morning_discovery_candidates mdc
               JOIN research_qa rq ON mdc.qa_id = rq.id
               WHERE mdc.user_decision IS NOT NULL
                 AND mdc.user_decided_at >= datetime('now', '-{int(days)} days')
               ORDER BY mdc.user_decided_at DESC""",
        ).fetchall()
    return [dict(r) for r in rows]


def update_candidate_feedback(
    candidate_id: int, decision: str, comment: str = "",
) -> bool:
    """user フィードバックを保存. decision in (buy, skip, hold, listed).

    H-4 (code-reviewer 指摘): rowcount==0 で False を返す.
    UI の「保存しました」表示と実 DB 更新の乖離 = silent fail を防止.
    """
    if decision not in ('buy', 'skip', 'hold', 'listed'):
        return False
    with sqlite3.connect(str(DB_PATH)) as con:
        cur = con.execute(
            """UPDATE morning_discovery_candidates
               SET user_decision=?, user_comment=?,
                   user_decided_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (decision, (comment or "")[:2000], candidate_id),
        )
        if cur.rowcount == 0:
            logger.warning(
                f"update_candidate_feedback: id={candidate_id} 該当行なし "
                f"(decision={decision} skip)"
            )
            return False
    return True


if __name__ == "__main__":
    import sys
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    dry = "--dry-run" in sys.argv
    result = run_morning_discovery(dry_run=dry)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
