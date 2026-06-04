"""W219 補助: Finances API レスポンスの実 schema 1 件 dump.

parse_sale_fees が itemId=0 / AD_FEE=0 を返した原因調査用. read-only.
"""
from __future__ import annotations
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main():
    from monitor.ebay_client import get_transactions

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    res = get_transactions(
        start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        limit=200,
    )
    print(f"fetched={res['fetched']} success={res['success']}")

    type_counter: Counter = Counter()
    for t in res["transactions"]:
        type_counter[str(t.get("transactionType") or "UNKNOWN")] += 1
    print("transactionType 分布:")
    for k, v in type_counter.most_common():
        print(f"  {k}: {v}")
    print()

    # 各 type 1 件ずつ dump (sensitive field は orderId/buyer 等のみ keep)
    seen: set = set()
    for t in res["transactions"]:
        ttype = str(t.get("transactionType") or "UNKNOWN")
        if ttype in seen:
            continue
        seen.add(ttype)
        print(f"\n=== sample [{ttype}] (full JSON) ===")
        # 出力長制御のため keys だけと全文 dump 両方
        print("top-level keys:", list(t.keys()))
        print(json.dumps(t, ensure_ascii=False, indent=2, default=str)[:3500])

    # SALE で marketplaceFees がある or orderLineItems がある最初の 1 件 詳細
    print("\n\n=== SALE with marketplaceFees (raw) ===")
    for t in res["transactions"]:
        if str(t.get("transactionType") or "") != "SALE":
            continue
        # 各 SALE で orderLineItems / 直下 marketplaceFees どちらが正 schema か
        if t.get("orderLineItems") or t.get("marketplaceFees"):
            print(json.dumps(t, ensure_ascii=False, indent=2,
                             default=str)[:5000])
            break

    # AD_FEE がある transaction を探す (NON_SALE_CHARGE か?)
    print("\n\n=== ad fee / promoted listing 探索 ===")
    for t in res["transactions"][:200]:
        s = json.dumps(t, default=str).upper()
        if "AD_FEE" in s or "PROMOTED" in s:
            print(f"  type={t.get('transactionType')} "
                  f"date={t.get('transactionDate')} "
                  f"amount={t.get('amount')} "
                  f"orderId={t.get('orderId')}")


if __name__ == "__main__":
    main()
