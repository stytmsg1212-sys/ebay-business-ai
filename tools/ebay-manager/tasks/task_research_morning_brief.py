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
    answer = ask(
        query,
        source="morning_brief",
        force_model="opus",  # 朝の重点提案は深く考える
        enable_thinking=False,  # cost 抑制 (UI 非表示なので thinking 不要)
        save_history=True,
        timeout=180,
    )

    if answer.error:
        logger.error(f"morning_brief 失敗: {answer.error}")
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
