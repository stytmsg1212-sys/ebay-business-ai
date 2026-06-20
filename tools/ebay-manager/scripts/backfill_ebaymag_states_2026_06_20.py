#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W284: eBaymag 台帳 (ebaymag_products) の site_states 未取得分を一括 fetch (2026-06-20).

user 要望: 既設定商品の 4択 を実態初期化するため、site_states NULL の mapping を
fetch_site_states (read-only、トグル/保存しない=eBaymag非変更) で埋める。

CDP Chrome (localhost:9222) + eBaymag ログインが必要。90分で TIMEOUT して exit。
Q0: 失敗は logger/print に件数記録 (silent skip 禁止)。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from monitor.database import get_conn, upsert_ebaymag_product  # noqa: E402
from monitor.ebaymag_driver import fetch_site_states  # noqa: E402

_TIMEOUT_SEC = 90 * 60  # 90 分上限 (hang 防止)


def main() -> int:
    with get_conn() as c:
        rows = [
            (r[0], r[1])
            for r in c.execute(
                "SELECT ebay_item_id, product_id FROM ebaymag_products "
                "WHERE site_states_json IS NULL OR site_states_json='' "
                "   OR site_states_json='{}' "
                "ORDER BY ebay_item_id"
            ).fetchall()
        ]
    total = len(rows)
    print(f"[backfill] 対象 {total} 件 (site_states 未取得)", flush=True)
    start = time.time()
    ok = fail = 0
    for i, (eid, pid) in enumerate(rows, 1):
        if time.time() - start > _TIMEOUT_SEC:
            print(f"[backfill] TIMEOUT ({_TIMEOUT_SEC}s) — {i-1}/{total} で打ち切り", flush=True)
            break
        try:
            res = fetch_site_states(pid, expected_itm=eid)
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  [{i}/{total}] eid={eid} 例外: {e}", flush=True)
            continue
        if res.ok and res.site_states:
            upsert_ebaymag_product(eid, pid, res.site_states)
            ok += 1
            on = [k for k, v in res.site_states.items() if v]
            print(f"  [{i}/{total}] eid={eid} OK ON={on}", flush=True)
        else:
            fail += 1
            print(f"  [{i}/{total}] eid={eid} 取得失敗: {res.error}", flush=True)
    elapsed = int(time.time() - start)
    print(f"[backfill] 完了: OK={ok} / 失敗={fail} / 経過={elapsed}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
