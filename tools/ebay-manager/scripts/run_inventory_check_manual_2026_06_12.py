# -*- coding: utf-8 -*-
"""依頼ボード#14 Q1 実機検証用 one-shot (2026-06-12).

PayPay 売り切れ検知修正 (_detect_paypay_signals 配線) 後に、本番経路
(tab_manual_run と同じ run_inventory_check → sync_inventory_status_to_db) を
1 回実行し、「不明」stuck の PayPay listing 9 件が正しい状態に更新されることを
確認する。スケジュール経路 (02:30 batch) と同一本体 (W50 統合)。
"""
import json
import logging
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

start = time.time()

cfg_path = BASE_DIR / "config" / "schedule_config.json"
with open(cfg_path, encoding="utf-8") as f:
    config = json.load(f)

from tasks.task_inventory_check import run_inventory_check  # noqa: E402
from tasks.task_sync_data_stores import sync_inventory_status_to_db  # noqa: E402

res = run_inventory_check(config)
print("RUN_INVENTORY_CHECK:", json.dumps(
    {k: v for k, v in res.items() if k != "changes"}, ensure_ascii=False))
print("CHANGES became_oos:", len(res.get("changes", {}).get("became_out_of_stock", [])))

sync_res = sync_inventory_status_to_db()
print("SYNC_TO_DB:", json.dumps(
    {k: v for k, v in sync_res.items() if k != "oos_to_zero"}, ensure_ascii=False))
print("oos_to_zero candidates:", len(sync_res.get("oos_to_zero", [])))

print(f"ELAPSED: {time.time() - start:.0f}s")
print("DONE")
