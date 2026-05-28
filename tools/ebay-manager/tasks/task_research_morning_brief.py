"""W24: 朝 02:30 に Research 脳が「本日の重点 3 つ」を自動生成.

DASHBOARD で `[本日の重点 by Research 脳]` セクションとして表示される.
保存先: research_qa テーブル (source='morning_brief').
1 日 1 回のみ実行 (重複防止).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "monitor.db"


def _today_brief_exists() -> bool:
    """本日の morning_brief が既に生成されているか."""
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(str(DB_PATH)) as con:
        row = con.execute(
            "SELECT COUNT(*) FROM research_qa "
            "WHERE source='morning_brief' AND date(asked_at)=?",
            (today,),
        ).fetchone()
    return (row[0] if row else 0) > 0


def _build_brief_query() -> str:
    """morning brief の自動生成 query を構築."""
    return (
        "本日の MonoHonpo eBay 越境EC 業務について、優先度順に **3 つの重点項目** を提案してください。\n"
        "\n"
        "条件:\n"
        "1. 各項目は 80 文字以内に凝縮\n"
        "2. 「該当 listing 件数 + 一言分析: 推奨アクション」の形式\n"
        "3. 以下の観点を網羅 (DB stats を参照しつつ):\n"
        "   a. 関税/価格 関連の変動 (Section 232 / DDP / 為替)\n"
        "   b. supplier_candidates の borderline / 緊急判定対象\n"
        "   c. 在庫/出品の即時アクション要件 (rank E 多数 / 売上 0 件 / token 期限など)\n"
        "4. 数値は実 DB 値で根拠を示す (推測禁止 = Karpathy K0)\n"
        "5. 動画 KB から関連 video_id を citations に含める\n"
        "\n"
        "出力フォーマット:\n"
        "[本日の重点 — YYYY-MM-DD]\n"
        "1. <該当件数 + 一言分析>: <推奨アクション>\n"
        "2. <該当件数 + 一言分析>: <推奨アクション>\n"
        "3. <該当件数 + 一言分析>: <推奨アクション>\n"
    )


def _notify_budget_exceeded(config: Optional[dict], answer) -> None:
    """予算超過時に Discord 明示通知 (silent skip 防止、R-11).

    notify 自体の失敗も silent skip 禁止: webhook 空 / 送信失敗時は logger.error
    で痕跡を残す (code-reviewer W164-pm HIGH-3 対応). 例外は具体型に絞る.
    """
    try:
        from notifiers.discord_notifier import DiscordNotifier
        notifier = DiscordNotifier(webhook_url="")  # .env 優先
        if not notifier.webhook_url:
            logger.error(
                "budget_exceeded notify: webhook_url 空 = user に届かない. "
                ".env DISCORD_WEBHOOK_URL を確認 (Q0 silent skip 防止)"
            )
            return
        ok = notifier.send_message(
            f"⚠️ **morning_brief 予算超過**\n"
            f"Opus 4.7 budget $1.0 を超過. cost={getattr(answer, 'cost_usd', '?')} "
            f"qa_id={getattr(answer, 'qa_id', '?')}. CLAUDE.md Q6 1日30 calls 上限近接の可能性."
        )
        if not ok:
            logger.error("budget_exceeded notify: Discord 送信失敗 (R-11 user 不達)")
    except (ImportError, ConnectionError, TimeoutError, OSError) as e:
        # 具体例外に絞る (bare except Exception 禁止、CLAUDE.md coding-standards)
        logger.exception(f"budget_exceeded notify 例外: {e}")


def run_research_morning_brief(config: Optional[dict] = None) -> dict:
    """daily_scheduler から呼ばれる. 1 日 1 回のみ生成 (重複防止)."""
    if _today_brief_exists():
        logger.info("morning_brief: 本日分は既に生成済 (skip)")
        return {
            "success": True,
            "skipped": True,
            "message": "morning_brief 本日分既存 (skip)",
        }

    try:
        from monitor.research_brain import ask
    except ImportError as e:
        logger.error(f"research_brain import 失敗: {e}")
        return {"success": False, "message": f"import error: {e}"}

    query = _build_brief_query()
    logger.info("morning_brief: Research 脳 (Opus 4.7) で生成開始 (~60-90 秒)")
    # 2026-05-25 (W164-pm Codex review #3): 既定 $0.50 だと 5/25 03:39 で
    # $0.5099 で error_max_budget_usd 超過、claude exit 1. 直近実コスト + 100%
    # buffer で $1.0 に引上げ. 超過時は Discord で明示通知 (Q0 silent skip 防止).
    answer = ask(
        query,
        source="morning_brief",
        force_model="opus",  # 朝の重点提案は深く考える
        enable_thinking=False,  # cost 抑制 (UI 非表示なので thinking 不要)
        save_history=True,
        # 2026-05-29: 5/29 朝 03:44 に timeout(180s) 失敗。通常 60-90s で完了 (5/26-28)
        # だが API 遅延で 180s 超過し得る。off-peak 02:00 実行 + max_budget が cost を
        # cap するため timeout 延長に cost リスクなし。300s に引上げ。
        timeout=300,
        max_budget_usd=1.0,
    )

    if answer.error:
        logger.error(f"morning_brief 失敗: {answer.error}")
        # 予算超過は user に明示通知 (silent skip 防止、R-11 user 実視認 verify)
        if "error_max_budget_usd" in str(answer.error):
            _notify_budget_exceeded(config, answer)
        return {
            "success": False,
            "message": f"morning_brief failed: {answer.error}",
            "qa_id": answer.qa_id,
        }

    logger.info(
        f"morning_brief: 生成完了 qa_id={answer.qa_id} "
        f"({answer.duration_ms}ms, ${answer.cost_usd:.4f}, "
        f"citations={len(answer.citations)})"
    )
    return {
        "success": True,
        "qa_id": answer.qa_id,
        "answer_preview": answer.answer_md[:200],
        "citations_count": len(answer.citations),
        "duration_ms": answer.duration_ms,
        "cost_usd": answer.cost_usd,
        "message": (
            f"morning brief 生成完了: 3 重点項目 (Opus 4.7, "
            f"{answer.duration_ms//1000}s, citations={len(answer.citations)})"
        ),
    }


def get_today_brief() -> Optional[dict]:
    """DASHBOARD 表示用: 本日の morning brief を返す."""
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(str(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            """SELECT id, asked_at, answer_md, citations, duration_ms, cost_usd
               FROM research_qa
               WHERE source='morning_brief' AND date(asked_at)=?
               ORDER BY asked_at DESC LIMIT 1""",
            (today,),
        ).fetchone()
    return dict(row) if row else None


if __name__ == "__main__":
    import sys
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_research_morning_brief()
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
