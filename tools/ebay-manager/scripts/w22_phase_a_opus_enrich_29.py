#!/usr/bin/env python3
"""W22 Phase A: 既存 done 動画 29 件を Opus 4.7 で深掘り (一括)."""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.opus_video_enricher import enrich_video  # noqa: E402

LOG_PATH = (
    Path(__file__).resolve().parent.parent
    / "logs" / f"w22_phase_a_{datetime.now():%Y%m%d_%H%M%S}.log"
)
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


def main() -> None:
    db = "data/monitor.db"
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    c = con.cursor()
    c.execute(
        "SELECT video_id, title FROM videos_learned "
        "WHERE status='done' AND opus_enriched_at IS NULL "
        "ORDER BY processed_at DESC"
    )
    targets = [dict(r) for r in c.fetchall()]
    con.close()

    log.info(f"=== W22 Phase A: {len(targets)} 件 Opus 深掘り開始 ===")
    total_cost = 0.0
    success = 0
    fail = 0
    t_start = time.time()

    for i, r in enumerate(targets, 1):
        vid = r["video_id"]
        title = (r["title"] or "")[:60]
        log.info(f"[{i}/{len(targets)}] {vid} | {title}")
        t0 = time.time()
        try:
            result = enrich_video(vid, save_to_db=True)
            if result:
                cost = float(result.get("_meta", {}).get("cost_usd", 0))
                total_cost += cost
                success += 1
                log.info(f"  OK ${cost:.4f} ({time.time()-t0:.1f}s)")
            else:
                fail += 1
                log.warning(f"  FAIL (None) ({time.time()-t0:.1f}s)")
        except Exception as e:  # noqa: BLE001
            fail += 1
            log.exception(f"  EXCEPTION: {type(e).__name__}: {e}")
        time.sleep(2)  # rate limit 緩和

    elapsed = time.time() - t_start
    summary = {
        "total": len(targets),
        "success": success,
        "fail": fail,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_sec": int(elapsed),
        "log_path": str(LOG_PATH),
    }
    log.info(f"=== 完了 ===")
    log.info(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
