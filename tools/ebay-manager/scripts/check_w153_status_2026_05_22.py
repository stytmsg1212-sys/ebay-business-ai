"""W153 E2E 視認前の DB 状態確認 (one-shot, read-only)."""
import sqlite3
from pathlib import Path

DB = Path(__file__).parent.parent / "data" / "monitor.db"

with sqlite3.connect(DB) as c:
    print(f"user_version = {c.execute('PRAGMA user_version').fetchone()[0]}")

    cols = [r[1] for r in c.execute("PRAGMA table_info(ebay_listings)").fetchall()]
    active_col = "status" if "status" in cols else ("listing_status" if "listing_status" in cols else None)
    print(f"active filter column candidate: {active_col}")

    rows = c.execute(
        "SELECT ebay_item_id, title, rival_watch_enabled, "
        "initial_registered, initial_registered_at, rival_watch_started_at, "
        "rival_search_keywords_generated_at "
        "FROM ebay_listings WHERE rival_watch_enabled=1"
    ).fetchall()
    print(f"\n--- rival_watch_enabled=1 ({len(rows)} 件) ---")
    for r in rows:
        print(f"  item_id={r[0]}")
        print(f"  title={(r[1] or '')[:70]}")
        print(f"  initial_registered={r[3]} at={r[4]}")
        print(f"  rival_watch_started_at={r[5]}")
        print(f"  keywords_generated_at={r[6]}")

    if active_col:
        total = c.execute(f"SELECT COUNT(*) FROM ebay_listings WHERE {active_col}='Active'").fetchone()[0]
        print(f"\nactive listings ({active_col}='Active'): {total}")

    disc = c.execute(
        "SELECT COUNT(*), status FROM listing_rival_discoveries GROUP BY status"
    ).fetchall()
    print(f"\n--- listing_rival_discoveries ({sum(r[0] for r in disc)} 件) ---")
    for cnt, st in disc:
        print(f"  {st}: {cnt}")
