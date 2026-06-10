"""ebay_listings.ebaymag_segment を出品国プラン v2 ロジックで populate (one-shot).

ロジック本体は monitor/ebaymag_segment.recompute_ebaymag_segments() に集約
(market_analysis_refresh も同関数を末尾で呼ぶ = DRY)。本 script は初回 backfill 用。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from monitor.database import init_db, get_conn  # migration 適用
from monitor.ebaymag_segment import recompute_ebaymag_segments


def main():
    init_db()
    result = recompute_ebaymag_segments()
    print("populate 完了:", result)
    with get_conn() as c:
        chk = c.execute(
            "SELECT COALESCE(ebaymag_segment,'(NULL)') s, COUNT(*) n FROM ebay_listings "
            "WHERE COALESCE(is_ended,0)=0 GROUP BY s"
        ).fetchall()
    print("DB 検証:", {r["s"]: r["n"] for r in chk})


if __name__ == "__main__":
    main()
