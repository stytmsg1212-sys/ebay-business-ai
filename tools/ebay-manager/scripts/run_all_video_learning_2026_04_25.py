#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-04-25 動画学習キュー全件処理 one-shot.

failed 4 件は pending にリセット → pending 全件を順次 Gemini 投入.
進捗を stdout に逐次出力 (run_in_background から監視可能).
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3  # noqa: E402

from tasks.task_video_learning import process_single_video  # noqa: E402

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / f"video_learning_full_{datetime.now():%Y%m%d_%H%M%S}.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def reset_failed_to_pending(db_path: str = "data/monitor.db") -> int:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        "UPDATE videos_learned SET status='pending', "
        "error_detail=COALESCE(error_detail,'') || ' [bulk-retry 2026-04-25]' "
        "WHERE status='failed'"
    )
    n = cur.rowcount
    con.commit()
    con.close()
    return n


def list_pending(db_path: str = "data/monitor.db") -> list[tuple[str, str, str]]:
    """Pending 動画を **新しい順** で返す.
    2026-04-26 修正: 旧 ASC (古い順) は新しい動画 (post_tariff era で高価値) が
    quota 枯渇で全滅する problem を引き起こした. 新しい順 = 価値順で優先処理.
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        "SELECT video_id, url, COALESCE(title,'') AS t "
        "FROM videos_learned WHERE status='pending' ORDER BY added_at DESC"
    )
    rows = [(r["video_id"], r["url"], r["t"]) for r in cur.fetchall()]
    con.close()
    return rows


def _check_quota_available() -> tuple[bool, str]:
    """事前に Gemini API に ping して quota を確認.

    Returns: (available, reason)
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
        import os
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            return False, "API key 未設定"
        client = genai.Client(api_key=api_key)
        client.models.generate_content(model="gemini-2.5-flash", contents="ping")
        return True, "OK"
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            return False, "quota 枯渇 (Free Tier 20/日上限)"
        return False, f"その他エラー: {type(e).__name__}: {msg[:200]}"


def main():
    import os as _os
    n_reset = reset_failed_to_pending()
    log.info(f"failed → pending reset: {n_reset} 件")

    # 2026-04-26 追加: 事前 quota チェック (無駄な処理を防止)
    available, reason = _check_quota_available()
    if not available:
        log.error(f"Gemini quota 不可: {reason}")
        log.error("PT midnight (= JST 16:00) に reset されます. それまで待機推奨.")
        log.error("または Tier 1 (paid) にアップグレードすれば即時解消可能.")
        print(json.dumps({
            "success": False,
            "reason": "quota_unavailable",
            "detail": reason,
            "next_reset_jst": "今日 16:00",
        }, ensure_ascii=False))
        return

    queue = list_pending()
    # 日次クォータ (free tier 20 RPD) を超えないよう上限を環境変数で制御
    # 余裕を持って 15 件 (5 件分は他の用途用に確保) に下げる.
    max_n = int(_os.environ.get("VIDEO_LEARNING_MAX_PER_RUN", "15"))
    queue = queue[:max_n]
    log.info(f"処理対象: {len(queue)} 件 (上限 {max_n}、新しい順)")

    # RPM 制限 (free tier 5 RPM) 回避用 sleep (秒)
    sleep_sec = int(_os.environ.get("VIDEO_LEARNING_SLEEP_SEC", "15"))

    success, fail, quota_hit = 0, 0, False
    t0 = time.time()
    for i, (vid, url, title) in enumerate(queue, 1):
        if quota_hit:
            log.warning(f"[{i}/{len(queue)}] {vid} skipped (quota hit)")
            continue
        log.info(f"[{i}/{len(queue)}] {vid} | {title[:50]}")
        try:
            r = process_single_video(url)
            if r.get("success"):
                success += 1
                log.info(f"  ✓ {r.get('message','')}")
            else:
                fail += 1
                msg = r.get("message", "")
                log.warning(f"  ✗ {msg}")
                if "RESOURCE_EXHAUSTED" in msg or "429" in msg or "quota" in msg.lower():
                    log.error("daily quota 検出, 残りスキップ")
                    quota_hit = True
        except Exception as e:  # noqa: BLE001
            fail += 1
            log.exception(f"  ✗ 例外: {e}")
        # 進捗サマリ
        elapsed = time.time() - t0
        log.info(f"  進捗: success={success} fail={fail} elapsed={elapsed:.0f}s")
        # RPM 緩和 sleep (最後の item はスキップ)
        if i < len(queue) and not quota_hit and sleep_sec > 0:
            log.info(f"  sleep {sleep_sec}s for RPM throttle")
            time.sleep(sleep_sec)

    log.info("=" * 60)
    log.info(f"完了: success={success}/{len(queue)} fail={fail} total_time={time.time()-t0:.0f}s")
    log.info(f"log: {LOG_PATH}")
    print(json.dumps({
        "success": success,
        "fail": fail,
        "total": len(queue),
        "log_path": str(LOG_PATH),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
