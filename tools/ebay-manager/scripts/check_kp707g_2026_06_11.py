"""KP-707G / 同型番複数 listing の確認 (read-only)."""
import sqlite3
import sys

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

c = sqlite3.connect(r"data\monitor.db")
q = """SELECT ebay_item_id, title, COALESCE(is_ended,0), quantity_ebay
       FROM ebay_listings WHERE title LIKE '%KP-707G%' OR title LIKE '%KP-717G%'"""
for eid, title, ended, qty in c.execute(q):
    print(f"{eid} | ended={ended} | qty={qty} | {title}")
