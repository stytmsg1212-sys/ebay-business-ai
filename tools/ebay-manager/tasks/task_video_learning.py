#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
動画学習キュー処理タスク

UI（eBay Manager 動画学習タブ）から登録された pending 動画を順次処理:
  1. Gemini 2.5 Pro で URL から構造化知識抽出
  2. videos_learned を完了状態に更新
  3. knowledge_index にキーワード→video_id マッピング登録
  4. 秘書 inbox に新学習の通知を追記

単発処理 vs キュー処理:
- UI の「即時処理」ボタン: このモジュールの `process_single_video(url)` を直呼び
- scheduler の 02:30 枠: `run_video_learning_queue()` で pending 全件処理
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn  # noqa: E402
from monitor.gemini_video_learner import (  # noqa: E402
    extract_video_id, learn_from_youtube_url,
)

logger = logging.getLogger(__name__)

WORKDIR = Path(__file__).resolve().parent.parent / "data" / "video_cache"


def enqueue_video(url: str) -> dict:
    """UIから呼ばれる: URLを videos_learned に pending 登録。

    重複時 (同じ video_id 既に登録済) は {'success': True, 'status': 'exists'} を返す。
    """
    video_id = extract_video_id(url)
    if not video_id:
        return {"success": False, "message": "YouTube URL から video_id を抽出できません"}

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT status FROM videos_learned WHERE video_id=?",
            (video_id,),
        ).fetchone()
        if existing:
            return {
                "success": True,
                "status": "exists",
                "existing_status": existing["status"],
                "video_id": video_id,
                "message": f"登録済 (status={existing['status']})",
            }

        conn.execute(
            "INSERT INTO videos_learned (video_id, url, status) VALUES (?, ?, 'pending')",
            (video_id, url),
        )

    return {"success": True, "status": "enqueued", "video_id": video_id, "message": "学習キューに追加しました"}


def _save_knowledge_index(video_id: str, data: dict) -> int:
    """抽出データから knowledge_index を生成・保存。Returns: 登録件数。"""
    keywords = data.get("keywords_for_index") or []
    # products_mentioned の name も keyword として登録
    for p in data.get("products_mentioned") or []:
        if isinstance(p, dict) and p.get("name"):
            keywords.append(p["name"])
    for plat in data.get("platforms_mentioned") or []:
        keywords.append(plat)

    # 正規化 & 重複排除
    norm = []
    seen = set()
    for k in keywords:
        if not k or not isinstance(k, str):
            continue
        kk = k.strip()
        if not kk or kk.lower() in seen:
            continue
        seen.add(kk.lower())
        norm.append(kk)

    with get_conn() as conn:
        # 既存 knowledge_index 行 (この video_id に紐づく) を削除してから再登録
        conn.execute("DELETE FROM knowledge_index WHERE video_id=?", (video_id,))
        for kw in norm:
            conn.execute(
                "INSERT INTO knowledge_index (keyword, video_id, context) VALUES (?, ?, ?)",
                (kw, video_id, data.get("summary_ja", "")[:200]),
            )
    return len(norm)


def _notify_secretary(video_id: str, title: str, summary: str) -> None:
    """秘書 inbox に新学習の通知を追記。失敗しても処理は続行。"""
    try:
        inbox_dir = Path(__file__).resolve().parent.parent.parent.parent / ".company" / "secretary" / "inbox"
        if not inbox_dir.exists():
            return
        today = datetime.now().strftime("%Y-%m-%d")
        f = inbox_dir / f"{today}.md"
        ts = datetime.now().strftime("%H:%M")
        block = (
            f"\n## {ts} 動画学習完了\n\n"
            f"- 動画: **{title}** ({video_id})\n"
            f"- 要約: {summary[:200]}\n"
            f"- research/ と supplier_candidate_search で自動参照されます\n"
        )
        if f.exists():
            f.write_text(f.read_text(encoding='utf-8') + block, encoding='utf-8')
        else:
            f.write_text(f"# {today} 秘書 inbox\n" + block, encoding='utf-8')
    except Exception as e:
        logger.debug(f"秘書通知失敗: {e}")


def process_single_video(url: str) -> dict:
    """1動画の処理（enqueue→処理→結果保存を一気通貫）。UIの即時処理で呼ぶ。"""
    enq = enqueue_video(url)
    if not enq.get("success"):
        return enq
    video_id = enq["video_id"]

    # processing に更新
    with get_conn() as conn:
        conn.execute(
            "UPDATE videos_learned SET status='processing', status_message='Geminiへ送信中' "
            "WHERE video_id=?", (video_id,),
        )

    # W19 (2026-04-26): 失敗時の retry_count incrementer.
    # 3 回まで failed (リトライ可能), 4 回目以降は permanent_failed (キューから除外).
    _MAX_RETRY = 3

    def _record_failure(err_detail: str) -> tuple[str, int]:
        """failed/permanent_failed に更新し (status, retry_count) を返す."""
        with get_conn() as conn:
            row = conn.execute(
                "SELECT retry_count FROM videos_learned WHERE video_id=?", (video_id,)
            ).fetchone()
            new_retry = (row["retry_count"] or 0) + 1 if row else 1
            if new_retry > _MAX_RETRY:
                from datetime import datetime as _dt
                conn.execute(
                    "UPDATE videos_learned SET status='permanent_failed', "
                    "error_detail=?, retry_count=?, last_retry_at=?, "
                    "permanent_failed_at=? WHERE video_id=?",
                    (err_detail[:500], new_retry, _dt.now(), _dt.now(), video_id),
                )
                return ('permanent_failed', new_retry)
            from datetime import datetime as _dt
            conn.execute(
                "UPDATE videos_learned SET status='failed', error_detail=?, "
                "retry_count=?, last_retry_at=? WHERE video_id=?",
                (err_detail[:500], new_retry, _dt.now(), video_id),
            )
            return ('failed', new_retry)

    try:
        meta, extracted = learn_from_youtube_url(url, WORKDIR)
    except Exception as e:
        logger.exception("動画処理例外")
        new_status, retry_n = _record_failure(str(e))
        return {
            "success": False, "video_id": video_id,
            "message": f"例外 [{new_status} retry={retry_n}/{_MAX_RETRY}]: {e}",
            "permanent_failed": new_status == 'permanent_failed',
        }

    if not extracted:
        new_status, retry_n = _record_failure('Gemini抽出失敗')
        return {
            "success": False, "video_id": video_id,
            "message": f"Gemini抽出失敗 [{new_status} retry={retry_n}/{_MAX_RETRY}]",
            "permanent_failed": new_status == 'permanent_failed',
        }

    # 正常: videos_learned に結果書き込み
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # published_date / tariff_era は Gemini が判定した値を優先（空なら既存の date_hint を維持）
    ai_pub_date = (extracted.get("published_date") or "").strip()
    ai_era = (extracted.get("tariff_era") or "").strip()
    ai_sensitive = extracted.get("time_sensitive_topics") or []

    with get_conn() as conn:
        # 既存の published_date/tariff_era を取得（scraper登録時の推定値）
        _existing = conn.execute(
            "SELECT published_date, tariff_era FROM videos_learned WHERE video_id=?",
            (video_id,),
        ).fetchone()
        final_pub = ai_pub_date if ai_pub_date else (_existing["published_date"] if _existing else None)
        final_era = ai_era if ai_era else (_existing["tariff_era"] if _existing else None)

        conn.execute(
            """UPDATE videos_learned SET
                 title=?, channel=?, duration_sec=?,
                 status='done', status_message='完了',
                 summary_ja=?, key_insights=?, products_mentioned=?,
                 platforms_mentioned=?, actionable_steps=?, pricing_hints=?,
                 topics=?, gemini_response_raw=?, processed_at=?,
                 published_date=?, tariff_era=?, time_sensitive_topics=?
               WHERE video_id=?""",
            (
                meta.get("title", ""),
                meta.get("channel", ""),
                meta.get("duration", 0),
                extracted.get("summary_ja", ""),
                json.dumps(extracted.get("key_insights") or [], ensure_ascii=False),
                json.dumps(extracted.get("products_mentioned") or [], ensure_ascii=False),
                json.dumps(extracted.get("platforms_mentioned") or [], ensure_ascii=False),
                json.dumps(extracted.get("actionable_steps") or [], ensure_ascii=False),
                json.dumps(extracted.get("pricing_hints") or [], ensure_ascii=False),
                ",".join(extracted.get("topics") or []),
                extracted.get("_raw_response") or "",
                now,
                final_pub,
                final_era,
                json.dumps(ai_sensitive, ensure_ascii=False),
                video_id,
            ),
        )

    kw_count = _save_knowledge_index(video_id, extracted)
    _notify_secretary(video_id, meta.get("title", video_id), extracted.get("summary_ja", ""))

    # 2026-04-26 W22 ハイブリッド: Gemini 成功直後に Opus 4.7 深掘りも実施.
    # eBay 業務適用視点で MonoHonpo 用に深く再解釈する.
    # 失敗しても Gemini 段階は成功扱い (Opus は別途リトライ可能).
    opus_status = "skipped"
    opus_cost = 0.0
    try:
        from monitor.opus_video_enricher import enrich_video
        enriched = enrich_video(video_id, save_to_db=True)
        if enriched:
            opus_status = "enriched"
            opus_cost = float(enriched.get("_meta", {}).get("cost_usd", 0.0))
        else:
            opus_status = "failed"
    except Exception as _opus_e:  # noqa: BLE001
        logger.warning(f"Opus 4.7 enrichment 失敗 ({video_id}): {_opus_e}")
        opus_status = "error"

    return {
        "success": True,
        "video_id": video_id,
        "title": meta.get("title"),
        "keywords_indexed": kw_count,
        "opus_enrichment": opus_status,
        "opus_cost_usd": opus_cost,
        "message": f"学習完了 [Gemini+Opus={opus_status}]: {meta.get('title','')[:60]}",
    }


def run_video_learning_queue(config: dict = None) -> dict:
    """daily_scheduler から呼ばれる: pending 全件を処理。"""
    task_cfg = (config or {}).get("tasks_enabled", {}).get("video_learning") or {}
    max_items = int(task_cfg.get("max_items_per_run", 3))

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT video_id, url FROM videos_learned WHERE status='pending' "
            "ORDER BY added_at ASC LIMIT ?",
            (max_items,),
        ).fetchall()

    if not rows:
        return {"success": True, "processed": 0, "message": "処理待ち動画なし"}

    processed = 0
    errors = 0
    for r in rows:
        d = dict(r)
        logger.info(f"処理中: {d['url']}")
        result = process_single_video(d["url"])
        if result.get("success"):
            processed += 1
        else:
            errors += 1

    return {
        "success": errors < len(rows),
        "processed": processed,
        "errors": errors,
        "message": f"{processed}件処理 / {errors}件エラー",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python -m tasks.task_video_learning <YouTube URL>")
        sys.exit(1)
    url = sys.argv[1]
    r = process_single_video(url)
    print(json.dumps(r, indent=2, ensure_ascii=False))
