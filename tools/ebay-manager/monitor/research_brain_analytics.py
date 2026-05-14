"""W26: Research 脳 評価ループ analytics.

research_qa の rating + cost + duration を集計し、低評価パターンを surface する.
プロンプト改善 (subagent.md 更新) は user の rating データ蓄積後 (W26b 別タスク).

Karpathy K3 準拠: 測定可能な指標で品質を追跡.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "monitor.db"


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def get_overall_stats(days: int = 7) -> dict:
    """直近 N 日の Research 脳 利用統計."""
    since = (datetime.now() - timedelta(days=days)).isoformat()
    with _conn() as c:
        total = c.execute(
            "SELECT COUNT(*) AS n FROM research_qa WHERE asked_at >= ?", (since,)
        ).fetchone()["n"]
        rated = c.execute(
            "SELECT COUNT(*) AS n, AVG(user_rating) AS avg_r, "
            "MIN(user_rating) AS min_r, MAX(user_rating) AS max_r "
            "FROM research_qa WHERE asked_at >= ? AND user_rating IS NOT NULL",
            (since,),
        ).fetchone()
        action = c.execute(
            "SELECT COUNT(*) AS n FROM research_qa "
            "WHERE asked_at >= ? AND user_action_at IS NOT NULL", (since,)
        ).fetchone()["n"]
        cost = c.execute(
            "SELECT SUM(cost_usd) AS s, AVG(duration_ms) AS avg_d "
            "FROM research_qa WHERE asked_at >= ?", (since,)
        ).fetchone()
        by_source = c.execute(
            "SELECT source, COUNT(*) AS n, AVG(user_rating) AS avg_r "
            "FROM research_qa WHERE asked_at >= ? GROUP BY source", (since,)
        ).fetchall()
        by_model = c.execute(
            "SELECT model, COUNT(*) AS n, AVG(user_rating) AS avg_r, "
            "AVG(duration_ms) AS avg_d, SUM(cost_usd) AS sum_c "
            "FROM research_qa WHERE asked_at >= ? GROUP BY model", (since,)
        ).fetchall()
    return {
        "period_days": days,
        "total_qa": total,
        "rated_count": rated["n"] or 0,
        "rated_pct": (rated["n"] / total * 100) if total else 0,
        "avg_rating": round(rated["avg_r"], 2) if rated["avg_r"] else None,
        "rating_range": (rated["min_r"], rated["max_r"]),
        "action_taken_count": action,
        "action_pct": (action / total * 100) if total else 0,
        "total_cost_usd": round(cost["s"] or 0, 4),
        "avg_duration_ms": int(cost["avg_d"] or 0),
        "by_source": [dict(r) for r in by_source],
        "by_model": [dict(r) for r in by_model],
    }


def find_low_rated(threshold: int = 2, limit: int = 20) -> list[dict]:
    """低評価 (rating <= threshold) の Q&A を抽出. プロンプト改善の手がかり."""
    with _conn() as c:
        rows = c.execute(
            """SELECT id, asked_at, source, query, model, answer_md,
                      user_rating, duration_ms, cost_usd
               FROM research_qa
               WHERE user_rating IS NOT NULL AND user_rating <= ?
               ORDER BY asked_at DESC LIMIT ?""",
            (threshold, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def find_high_rated(threshold: int = 5, limit: int = 20) -> list[dict]:
    """高評価 (rating >= threshold) の Q&A. 良いパターンの参照例."""
    with _conn() as c:
        rows = c.execute(
            """SELECT id, asked_at, source, query, model, answer_md,
                      user_rating, duration_ms, cost_usd
               FROM research_qa
               WHERE user_rating IS NOT NULL AND user_rating >= ?
               ORDER BY user_rating DESC, asked_at DESC LIMIT ?""",
            (threshold, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def find_no_action(min_rating: int = 4, limit: int = 20) -> list[dict]:
    """高評価だが action_taken=False の Q&A.

    これは「良い回答だが実際には適用しなかった」ケース. 解釈:
    - 回答品質は OK だが業務優先度低
    - もしくは意思決定の参考にしただけ
    """
    with _conn() as c:
        rows = c.execute(
            """SELECT id, asked_at, source, query, user_rating
               FROM research_qa
               WHERE user_rating >= ? AND user_action_at IS NULL
               ORDER BY asked_at DESC LIMIT ?""",
            (min_rating, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def cost_by_period(days: int = 30) -> list[dict]:
    """日別コスト推移 (予算監視用)."""
    since = (datetime.now() - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            """SELECT date(asked_at) AS d,
                      COUNT(*) AS calls,
                      SUM(cost_usd) AS cost,
                      AVG(duration_ms) AS avg_d
               FROM research_qa
               WHERE asked_at >= ?
               GROUP BY date(asked_at)
               ORDER BY d DESC""",
            (since,),
        ).fetchall()
    return [
        {
            "date": r["d"], "calls": r["calls"],
            "cost_usd": round(r["cost"] or 0, 4),
            "avg_duration_ms": int(r["avg_d"] or 0),
        }
        for r in rows
    ]


def patterns_in_low_rated(low_rated: list[dict]) -> dict:
    """低評価回答に共通するパターンを抽出 (将来 W26b プロンプト改善の手がかり).

    抽出指標:
    - source 偏り (どの呼出元で低評価が多いか)
    - 平均 query 長 / 平均 answer 長
    - 共通キーワード (5 件以上の query で出る単語)
    """
    if not low_rated:
        return {"empty": True, "note": "低評価データなし"}
    sources = {}
    q_lens = []
    a_lens = []
    word_counter: dict[str, int] = {}
    for qa in low_rated:
        s = qa.get("source", "?")
        sources[s] = sources.get(s, 0) + 1
        q_lens.append(len(qa.get("query", "")))
        a_lens.append(len(qa.get("answer_md", "") or ""))
        for w in (qa.get("query", "") or "").split():
            if 2 <= len(w) <= 30:
                word_counter[w] = word_counter.get(w, 0) + 1
    common_words = sorted(
        ((w, n) for w, n in word_counter.items() if n >= 2),
        key=lambda x: -x[1],
    )[:15]
    return {
        "count": len(low_rated),
        "by_source": sources,
        "avg_query_len": int(sum(q_lens) / len(q_lens)) if q_lens else 0,
        "avg_answer_len": int(sum(a_lens) / len(a_lens)) if a_lens else 0,
        "common_words": [{"word": w, "count": n} for w, n in common_words],
    }


def render_analytics_report() -> dict:
    """UI / CLI 表示用に全集計を統合."""
    return {
        "overall_7d": get_overall_stats(7),
        "overall_30d": get_overall_stats(30),
        "low_rated_recent": find_low_rated(threshold=2, limit=10),
        "high_rated_recent": find_high_rated(threshold=5, limit=5),
        "no_action_high_rated": find_no_action(min_rating=4, limit=10),
        "cost_30d": cost_by_period(30),
        "patterns_in_low": patterns_in_low_rated(find_low_rated(2, 50)),
    }


if __name__ == "__main__":
    import sys
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    report = render_analytics_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
