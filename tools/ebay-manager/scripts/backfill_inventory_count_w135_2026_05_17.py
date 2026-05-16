"""W135 (2026-05-17): inventory_count 一括バックフィル one-shot.

目的: active 有在庫 listing で inventory_count IS NULL のものへ、eBay GetItem
の現 available (Quantity - QuantitySold) を投入し、W133 在庫同期を実発火
可能にする。初期値 = eBay 現状なので W133 同期は当面 no-op (buyer 影響ゼロ)、
以降の order 減算 / 入荷 confirm / 商品管理 手動編集で維持される。

eBay 在庫 API セマンティクス: source_ebay_inventory_api_semantics.md
  - GetItem Item/Quantity = 総数量 (available + sold)
  - GetItem Item/SellingStatus/QuantitySold = 売却済
  - available = Quantity - QuantitySold  ← これを inventory_count に入れる

Q2 準拠:
  - init_db を一切変更しない (本 one-shot のみ)。DROP/DELETE/ALTER なし。
  - 実行前に対象を SELECT で snapshot (rollback 用、JSON 保存)。
  - UPDATE は `WHERE ebay_item_id=? AND inventory_count IS NULL` で
    冪等ガード (2 回目以降 rowcount=0、order/手動で入った値を上書きしない)。
  - dry-run (--apply 無し) で件数 + 先頭サンプルのみ表示、書込まない。
  - eBay は GetItem (読み取り専用) のみ。eBay への write は一切しない。
  - 実行後 24h 以内に retrospective code-reviewer (db-migration-rules)。

sku-rules 準拠: 対象抽出は `sku LIKE 'stock%'` の prefix フィルタのみ
(集約/キー化しない)。listing 識別は ebay_item_id。

使い方:
  python scripts/backfill_inventory_count_w135_2026_05_17.py            # dry-run
  python scripts/backfill_inventory_count_w135_2026_05_17.py --apply    # 実書込
"""
from __future__ import annotations

import json
import logging
import random
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")

from monitor.database import get_conn
from monitor.ebay_client import (
    API_VERSION,
    TRADING_API_URL,
    _build_get_item_xml,
    _resolve_active_token,
)
from monitor.inventory_sync import _get_credentials

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("w135_backfill")

_NS = {"n": "urn:ebay:apis:eBLBaseComponents"}
_SLEEP_BASE = 2.0          # GetItem 間隔 (anti-bot / rate、W7-A 規約準拠)
# rollback artifact は技術データなので data/ 配下 (経理 .company/finance と
# 責務分離。cc-company フォルダ規約整合、code-reviewer MEDIUM 2026-05-17)
_SNAPSHOT_DIR = Path(
    r"C:/Users/gucch/projects/claude/tools/ebay-manager/data/w135_backfill"
)


def _fetch_available(item_id: str, creds: tuple) -> tuple:
    """GetItem で (available, total_qty, sold, error) を返す. 読み取り専用.

    available = Quantity - QuantitySold (>=0 にクランプ).
    error None = 成功。失敗時 available=None。
    """
    app_id, dev_id, cert_id, user_token = creds
    token = _resolve_active_token(user_token)
    xml_body = _build_get_item_xml(item_id).replace("{USER_TOKEN}", token)
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "GetItem",
        "X-EBAY-API-APP-NAME": app_id,
        "X-EBAY-API-DEV-NAME": dev_id,
        "X-EBAY-API-CERT-NAME": cert_id,
        "Content-Type": "text/xml",
    }
    try:
        resp = httpx.post(
            TRADING_API_URL, content=xml_body.encode("utf-8"),
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
    except (httpx.HTTPError, OSError) as e:
        return None, None, None, f"通信エラー: {e}"
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return None, None, None, f"XML parse error: {e}"
    ack = root.findtext("n:Ack", namespaces=_NS)
    if ack not in ("Success", "Warning"):
        errs = root.findall(".//n:Errors/n:LongMessage", namespaces=_NS)
        msg = "; ".join(e.text for e in errs if e.text) or "Unknown"
        return None, None, None, f"Ack={ack}: {msg}"
    item = root.find(".//n:Item", namespaces=_NS)
    if item is None:
        return None, None, None, "Item ノード無し"
    try:
        qty = int(item.findtext("n:Quantity", namespaces=_NS) or -1)
    except (ValueError, TypeError):
        qty = -1
    ss = item.find("n:SellingStatus", namespaces=_NS)
    try:
        sold = int(
            ss.findtext("n:QuantitySold", namespaces=_NS) or 0
        ) if ss is not None else 0
    except (ValueError, TypeError):
        sold = 0
    if qty < 0:
        return None, qty, sold, "Quantity 読取不能"
    avail = max(0, qty - sold)
    return avail, qty, sold, None


def main(argv) -> int:
    apply = "--apply" in argv
    mode = "APPLY (実書込)" if apply else "DRY-RUN (書込まない)"
    logger.info(f"W135 inventory_count backfill 開始 — {mode}")

    creds = _get_credentials()
    if not creds:
        logger.error("eBay 認証取得失敗 (中止)")
        return 1
    if creds[0] == "app_id":
        logger.error("creds がキー文字列 = dict→tuple バグ再発 (中止)")
        return 1

    with get_conn() as c:
        rows = c.execute(
            "SELECT ebay_item_id, substr(title,1,40) t, quantity_ebay "
            "FROM ebay_listings "
            "WHERE sku LIKE 'stock%' AND is_ended=0 "
            "AND inventory_count IS NULL "
            "AND ebay_item_id IS NOT NULL AND ebay_item_id != '' "
            "ORDER BY ebay_item_id"
        ).fetchall()
    targets = [dict(r) for r in rows]
    logger.info(f"対象 (active 有在庫 / inventory_count NULL): {len(targets)} 件")
    if not targets:
        logger.info("対象 0 件 — 何もしない")
        return 0

    # rollback 用 snapshot (実行前、JSON)
    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snap_path = _SNAPSHOT_DIR / (
        f"w135-backfill-snapshot-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    snap_path.write_text(
        json.dumps(targets, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    logger.info(f"snapshot 保存: {snap_path}")

    filled = skipped = errored = 0
    skip_log: list[dict] = []
    for i, t in enumerate(targets, 1):
        eid = t["ebay_item_id"]
        avail, qty, sold, err = _fetch_available(eid, creds)
        if err is not None:
            errored += 1
            skip_log.append({"ebay_item_id": eid, "reason": err})
            logger.warning(f"[{i}/{len(targets)}] {eid} GetItem 失敗: {err}")
        elif avail is None:
            skipped += 1
            skip_log.append({"ebay_item_id": eid, "reason": "available 不明"})
            logger.warning(f"[{i}/{len(targets)}] {eid} available 不明 skip")
        else:
            if apply:
                # 冪等ガード: NULL のままのものだけ埋める (order/手動の値を守る)
                with get_conn() as c:
                    cur = c.execute(
                        "UPDATE ebay_listings SET inventory_count=? "
                        "WHERE ebay_item_id=? AND inventory_count IS NULL",
                        (avail, eid),
                    )
                    rc = cur.rowcount
                if rc == 1:
                    filled += 1
                    logger.info(
                        f"[{i}/{len(targets)}] {eid} inventory_count="
                        f"{avail} (qty{qty}-sold{sold}) | {t['t']}"
                    )
                else:
                    skipped += 1
                    skip_log.append(
                        {"ebay_item_id": eid,
                         "reason": f"rowcount={rc} (既に値あり/対象外)"}
                    )
            else:
                filled += 1
                logger.info(
                    f"[{i}/{len(targets)}] DRY {eid} → "
                    f"inventory_count={avail} (qty{qty}-sold{sold}) | {t['t']}"
                )
        time.sleep(_SLEEP_BASE * random.uniform(0.8, 1.4))

    logger.info(
        f"完了 [{mode}] 対象{len(targets)} / "
        f"{'書込' if apply else '対象'}{filled} / skip{skipped} / err{errored}"
    )
    if skip_log:
        sp = _SNAPSHOT_DIR / (
            f"w135-backfill-skips-{datetime.now():%Y%m%d-%H%M%S}.json"
        )
        sp.write_text(
            json.dumps(skip_log, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        logger.info(f"skip/err 明細: {sp}")
    if not apply:
        logger.info("DRY-RUN でした。実書込は --apply を付けて再実行。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
