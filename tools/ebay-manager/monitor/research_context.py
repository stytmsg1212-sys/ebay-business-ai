"""W23 Research 脳 — コンテキスト集約モジュール.

STABLE part (videos_learned 全件 summary + KB + listings stats) と
DYNAMIC part (query に応じた knowledge_index keyword マッチ top-K) を組み立てる.

設計方針 (Karpathy K1 Simplicity First):
- STABLE = 1 日 1 回更新で済む全体像 (将来の prompt cache 対象だが、Method A
  subagent ではキャッシュ不可、再ロードコストはユーザー Max 内なので無視)
- DYNAMIC = query から keyword 抽出 → knowledge_index で top-K video_id 引当
- ファイルレベルの全文ロードは避ける (token cost vs 質のトレードオフで凝縮版採用)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "monitor.db"


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def build_stable_context(max_videos: int = 30) -> str:
    """STABLE プロンプト. 動画 KB 全件凝縮 + listings stats + KB topics 索引.

    Returns:
        Markdown 文字列 (推定 10-30KB)
    """
    parts: list[str] = ["# Research 脳 知識ベース (STABLE)"]
    parts.append("")
    parts.append("以下は MonoHonpo の Research 脳が参照する知識の **目次** です. ")
    parts.append("詳細が必要な場合は Read tool で個別ファイルを開いてください.")
    parts.append("")

    # ────────────────────────────────────────
    # Section 1: 動画学習 KB 凝縮 (Opus 4.7 で深掘り済 30 件)
    # ────────────────────────────────────────
    parts.append("## 1. 動画学習 KB (Opus 4.7 enriched)")
    parts.append("")
    parts.append(
        "全 30 件の動画について core_lesson + applicable_to_us 抜粋. "
        "詳細 (red_flags / cross_video_links 等) は DB の videos_learned テーブルから引く."
    )
    parts.append("")
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT video_id, title, core_lesson, applicable_to_us
                   FROM videos_learned
                   WHERE status='done' AND opus_enriched_at IS NOT NULL
                   ORDER BY processed_at DESC
                   LIMIT ?""",
                (max_videos,),
            ).fetchall()
        for r in rows:
            vid = r["video_id"]
            title = (r["title"] or "")[:80]
            lesson = (r["core_lesson"] or "")[:300]
            appl = []
            try:
                appl = json.loads(r["applicable_to_us"] or "[]")
            except (ValueError, TypeError):
                pass
            parts.append(f"### [{vid}] {title}")
            parts.append(f"- **core**: {lesson}")
            if appl:
                # 上位 3 件だけ embed (token 節約)
                for a in appl[:3]:
                    parts.append(f"- **applicable**: {a[:200]}")
            parts.append("")
    except sqlite3.Error as e:
        logger.warning(f"videos_learned 読込失敗: {e}")
        parts.append("(動画 KB 読込失敗)")
        parts.append("")

    # ────────────────────────────────────────
    # Section 2: ebay_listings 統計
    # ────────────────────────────────────────
    parts.append("## 2. ebay_listings 統計 (現状把握)")
    parts.append("")
    try:
        with _conn() as c:
            stats: dict[str, Any] = {}
            stats["total"] = c.execute("SELECT COUNT(*) FROM ebay_listings WHERE COALESCE(is_ended,0)=0").fetchone()[0]
            stats["by_rank"] = {
                r[0] or "?": r[1]
                for r in c.execute(
                    "SELECT rank, COUNT(*) FROM ebay_listings "
                    "WHERE COALESCE(is_ended,0)=0 GROUP BY rank"
                ).fetchall()
            }
            stats["sold_30d"] = c.execute(
                "SELECT COUNT(*) FROM sales_history WHERE sold_at >= date('now','-30 day')"
            ).fetchone()[0]
            stats["supplier_candidates_pending"] = c.execute(
                "SELECT COUNT(*) FROM supplier_candidates WHERE status='pending'"
            ).fetchone()[0]
        parts.append(f"- アクティブ listing: {stats['total']} 件")
        parts.append(f"- ランク分布: {stats['by_rank']}")
        parts.append(f"- 直近 30 日 売上: {stats['sold_30d']} 件")
        parts.append(f"- supplier_candidates pending: {stats['supplier_candidates_pending']} 件")
    except sqlite3.Error as e:
        logger.warning(f"listings stats 読込失敗: {e}")
        parts.append("(stats 読込失敗)")
    parts.append("")

    # ────────────────────────────────────────
    # Section 3: KB ファイル索引 (詳細は必要時 Read)
    # ────────────────────────────────────────
    parts.append("## 3. KB ファイル索引 (必要時 Read tool で個別取得)")
    parts.append("")
    kb_files = [
        (".company/ebay-knowledge/topics/section_232_tariff_2026_04.md", "Section 232 関税派生品 25% 計算ワークフロー"),
        (".company/research/topics/daily-research-criteria.md", "デイリーリサーチ基準"),
    ]
    for path, desc in kb_files:
        full = PROJECT_ROOT.parent / path
        if full.exists():
            parts.append(f"- `{path}` — {desc}")
    parts.append("")

    parts.append("## 4. memory feedback 索引 (絶対遵守ルール)")
    parts.append("")
    feedback_files = [
        ("feedback_no_silent_skip_no_fake_success.md", "Q0 サイレントスキップ禁止"),
        ("feedback_karpathy_principles.md", "K0-K3 Karpathy 4 原則"),
        ("feedback_supplier_matching_rules.md", "仕入先候補判定ルール"),
        ("feedback_condition_rank_system.md", "8 段階ランク N/S/A/B/C/D/PO/As-Is"),
        ("feedback_customs_response_strategy.md", "通関回答 Manufacturer=日本代理店"),
        ("feedback_ddp_shipping_policy.md", "DDP 出荷=売主負担、Section 232 buffer 必須"),
        ("feedback_tariff_era.md", "pre/transition/post tariff 時代区分"),
        ("feedback_anthropic_video_cal_rueb_takeaways.md", "Cal Rueb 動画 7 適用案 + 5 red flags"),
    ]
    for fname, desc in feedback_files:
        parts.append(f"- `{fname}` — {desc}")
    parts.append("")

    return "\n".join(parts)


def find_relevant_videos(query: str, k: int = 5) -> list[dict]:
    """query から keyword を抽出して knowledge_index に top-K マッチ.

    既存の knowledge_lookup.py を可能なら使う (本日 weight=1.5 で Opus 由来優遇済).
    """
    try:
        from monitor.knowledge_lookup import find_related_knowledge
        return find_related_knowledge(query, max_videos=k) or []
    except ImportError:
        logger.warning("knowledge_lookup not importable, using fallback")

    # フォールバック: 単純 LIKE 検索
    keywords = [w for w in query.split() if len(w) >= 2][:8]
    if not keywords:
        return []
    placeholders = " OR ".join(["keyword LIKE ?"] * len(keywords))
    params = [f"%{w}%" for w in keywords]
    with _conn() as c:
        rows = c.execute(
            f"""SELECT DISTINCT video_id, COUNT(*) AS hits
                FROM knowledge_index
                WHERE {placeholders}
                GROUP BY video_id
                ORDER BY hits DESC
                LIMIT ?""",
            (*params, k),
        ).fetchall()
        result = []
        for r in rows:
            vrow = c.execute(
                "SELECT title, core_lesson, applicable_to_us FROM videos_learned WHERE video_id=?",
                (r["video_id"],),
            ).fetchone()
            if vrow:
                result.append({
                    "video_id": r["video_id"],
                    "title": vrow["title"],
                    "core_lesson": vrow["core_lesson"],
                    "applicable_to_us": vrow["applicable_to_us"],
                    "hits": r["hits"],
                })
    return result


def build_dynamic_context(query: str, hints: Optional[dict] = None) -> str:
    """毎回フレッシュな部分. query 関連 SKU / 動画 / news を抽出して整形."""
    hints = hints or {}
    parts: list[str] = ["# 関連知識 (DYNAMIC, query 関連)"]
    parts.append("")

    # 関連動画 top-K
    videos = find_relevant_videos(query, k=5)
    if videos:
        parts.append(f"## 関連動画 (knowledge_index keyword マッチ top {len(videos)})")
        parts.append("")
        for v in videos:
            parts.append(f"### {v.get('video_id','')} hits={v.get('hits','?')}")
            parts.append(f"- title: {(v.get('title','') or '')[:80]}")
            lesson = (v.get('core_lesson','') or '')[:200]
            if lesson:
                parts.append(f"- core: {lesson}")
            try:
                appl = json.loads(v.get("applicable_to_us") or "[]")
                if appl:
                    parts.append(f"- applicable[0]: {appl[0][:150]}")
            except (ValueError, TypeError):
                pass
            parts.append("")

    # ebay_item_id hint がある場合 listing 詳細を引く (sku-rules.md 準拠).
    # W75 Iteration 4a (2026-05-01): SKU 経由 lookup は同 SKU 多 listing 時に random 1 件返却で
    # 無関係商品データが AI prompt に流入する事故 (例: stock:01 hint で Vocaloid4 ソフト返却
    # → 別 draft 監修にこのデータが乗る) を実証 → ebay_item_id 経由のみに変更.
    # legacy `hints["sku"]` は完全無視 (混在期 safety、caller 移行漏れ時も AI 事故防止).
    ebay_item_id = hints.get("ebay_item_id")
    if ebay_item_id:
        try:
            from monitor.database import get_ebay_listing_by_item_id
            row = get_ebay_listing_by_item_id(ebay_item_id)
            if row:
                parts.append(f"## 対象 listing")
                parts.append(f"- ebay_item_id: {ebay_item_id}")
                parts.append(f"- sku: {row.get('sku')}")
                parts.append(f"- title: {row.get('title')}")
                parts.append(f"- rank: {row.get('rank')} / price: ${row.get('current_price')}")
                parts.append(f"- watch: {row.get('watch_count')} / 30d sales: {row.get('sales_count_30d')}")
                parts.append(f"- weight: {row.get('weight_g')}g / source: {row.get('source_status')}")
                parts.append("")
        except sqlite3.Error as e:
            logger.warning(f"ebay_item_id lookup failed: {e}")

    return "\n".join(parts)


if __name__ == "__main__":
    import sys
    print("=== STABLE CONTEXT ===")
    s = build_stable_context()
    print(f"size: {len(s)} chars (~{len(s)//4} tokens)")
    print(s[:2000])
    print("...")
    print()
    if len(sys.argv) > 1:
        print("=== DYNAMIC CONTEXT ===")
        print(build_dynamic_context(sys.argv[1]))
