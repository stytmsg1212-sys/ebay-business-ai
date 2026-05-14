#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ebay_listings 物理データ enrichment タスク

GetItem API で weight/dimensions を取得し ebay_listings テーブルに直接書き込む。
profit 計算で `weight_g > 0` が必須なため、これが走らないと supplier candidate
の採算判定が常に None/不採算になる。

従来の task_enrich_ebay_data.py は JSON ファイルにしか書き込まず、
scheduler にも未登録だったため、498件中0件しか populate されていなかった。

実行条件:
  - ebay_listings.is_ended=0 (退役済は無視)
  - ebay_listings.weight_g=0 or NULL (既に populate 済なら skip)

制御:
  - max_items_per_run: 1回で処理する最大件数（デフォルト 50、API cost と scheduler
    時間のバランス）
  - sleep_between_items_sec: API レート制限対策（デフォルト 0.5秒）

週1回程度の実行を想定（weight/dimensions は頻繁に変わらない）。
"""
from __future__ import annotations

import logging
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn, update_ebay_listing_physical  # noqa: E402
from monitor.credentials import get_ebay_credentials, ebay_credentials_ok  # noqa: E402

logger = logging.getLogger(__name__)

TRADING_API_URL = "https://api.ebay.com/ws/api.dll"
API_VERSION = "967"

# weight=0 だった場合のデフォルト (g)。送料計算で完全な0は困るので最小限の推定値。
# 将来的に weight_source カラムで "default" と "ebay_actual" を区別する拡張候補
DEFAULT_WEIGHT_G_WHEN_UNKNOWN = 500.0


def _build_get_item_xml(item_id: str, user_token: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{user_token}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""


def fetch_physical_data(item_id: str, creds: dict) -> Optional[dict]:
    """GetItem API から物理データを取得。失敗時 None。

    Returns: {weight_g, length_cm, width_cm, height_cm, includes, warranty, condition}
    """
    xml = _build_get_item_xml(item_id, creds["user_token"])
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "GetItem",
        "X-EBAY-API-APP-NAME": creds["app_id"],
        "X-EBAY-API-DEV-NAME": creds["dev_id"],
        "X-EBAY-API-CERT-NAME": creds["cert_id"],
        "Content-Type": "text/xml",
    }
    try:
        resp = httpx.post(TRADING_API_URL, content=xml.encode("utf-8"), headers=headers, timeout=30.0)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"GetItem HTTP error for {item_id}: {e}")
        return None

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.warning(f"GetItem XML parse error for {item_id}: {e}")
        return None

    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext("ns:Ack", namespaces=ns)
    if ack not in ("Success", "Warning"):
        errors = root.findall(".//ns:Errors/ns:LongMessage", namespaces=ns)
        msg = "; ".join(e.text for e in errors if e.text) or "Unknown error"
        logger.info(f"GetItem API returned {ack} for {item_id}: {msg}")
        return None

    item = root.find(".//ns:Item", ns)
    if item is None:
        return None

    # 重量 (lbs → g)
    wm = item.findtext(".//ns:ShippingDetails/ns:WeightMajor", namespaces=ns) or "0"
    wmi = item.findtext(".//ns:ShippingDetails/ns:WeightMinor", namespaces=ns) or "0"
    try:
        weight_lbs = float(wm) + float(wmi) / 16.0
    except ValueError:
        weight_lbs = 0.0
    weight_g = weight_lbs * 453.592
    weight_source = "ebay"
    if weight_g == 0:
        weight_g = DEFAULT_WEIGHT_G_WHEN_UNKNOWN
        weight_source = "default_500g"

    # 寸法 (inches → cm)
    length_cm = width_cm = height_cm = 0.0
    pkg = item.find(".//ns:ShippingPackageDetails", ns)
    if pkg is not None:
        def _to_cm(tag: str) -> float:
            txt = pkg.findtext(f"ns:{tag}", namespaces=ns) or "0"
            try:
                return float(txt) * 2.54
            except ValueError:
                return 0.0
        length_cm = _to_cm("DimensionLength")
        width_cm = _to_cm("DimensionWidth")
        height_cm = _to_cm("DimensionHeight")

    # 付属品・保証（Description から簡易抽出）
    description = item.findtext(".//ns:Description", namespaces=ns) or ""
    includes = _parse_includes(description)
    warranty = _parse_warranty(description)

    return {
        "weight_g": round(weight_g, 1),
        "weight_source": weight_source,
        "length_cm": round(length_cm, 1),
        "width_cm": round(width_cm, 1),
        "height_cm": round(height_cm, 1),
        "includes": includes,
        "warranty": warranty,
    }


def _parse_includes(desc: str) -> str:
    if not desc:
        return ""
    d = desc.lower()
    if "body only" in d or "本体のみ" in desc:
        return "本体のみ"
    if "complete set" in d or "完備" in desc:
        return "付属品完備"
    if "with box" in d or "箱あり" in desc:
        return "ボックス付き"
    if "case" in d or "ケース" in desc:
        return "ケース付き"
    return ""


def _parse_warranty(desc: str) -> str:
    if not desc:
        return ""
    d = desc.lower()
    if "no warranty" in d or "ノークレーム" in desc or "no claim" in d:
        return "ノークレーム"
    if "warranty" in d or "保証" in desc or "保障" in desc:
        if "1 year" in d or "12 month" in d:
            return "1年保証"
        if "90" in desc or "3 month" in d:
            return "90日保証"
        if "30" in desc or "1 month" in d:
            return "30日保証"
        return "保証あり"
    return ""


def _fetch_targets(limit: int) -> list[dict]:
    """populate 対象: is_ended=0 かつ weight_g<=0 の listing。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ebay_item_id, sku
               FROM ebay_listings
               WHERE (is_ended IS NULL OR is_ended=0)
                 AND (weight_g IS NULL OR weight_g=0)
               ORDER BY rank ASC, last_synced_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def run_enrich_listings_physical(config: dict) -> dict:
    """
    Entry point. daily_scheduler から呼ばれる。

    Args:
        config: schedule_config.json dict
    Returns:
        {success, processed, updated, errors, message}
    """
    ebay_cfg = get_ebay_credentials(config)
    if not ebay_credentials_ok(ebay_cfg):
        return {"success": False, "processed": 0, "updated": 0, "errors": 0,
                "message": "ebay credentials missing (.env または config)"}

    task_cfg = (config or {}).get("tasks_enabled", {}).get("enrich_listings_physical") or {}
    max_items = int(task_cfg.get("max_items_per_run", 50))
    sleep_sec = float(task_cfg.get("sleep_between_items_sec", 0.5))

    targets = _fetch_targets(max_items)
    logger.info(f"物理データenrichment 対象: {len(targets)}件 (max_items={max_items})")

    if not targets:
        return {"success": True, "processed": 0, "updated": 0, "errors": 0,
                "message": "全listing enriched 済み（あるいは対象なし）"}

    updated = 0
    errors = 0

    for idx, t in enumerate(targets, start=1):
        ebay_item_id = t["ebay_item_id"]
        logger.info(f"  [{idx}/{len(targets)}] {ebay_item_id} (sku={t['sku']})")
        try:
            data = fetch_physical_data(ebay_item_id, ebay_cfg)
            if not data:
                errors += 1
                continue
            update_ebay_listing_physical(
                ebay_item_id=ebay_item_id,
                weight_g=data["weight_g"],
                length_cm=data["length_cm"],
                width_cm=data["width_cm"],
                height_cm=data["height_cm"],
                includes=data.get("includes", ""),
                warranty=data.get("warranty", ""),
            )
            # weight_source マーキング（'ebay' or 'default_500g'）
            with get_conn() as _conn:
                _conn.execute(
                    "UPDATE ebay_listings SET weight_source=? WHERE ebay_item_id=?",
                    (data.get("weight_source", "ebay"), ebay_item_id),
                )
            updated += 1
        except Exception as e:
            logger.warning(f"    例外: {e}", exc_info=True)
            errors += 1

        if idx < len(targets) and sleep_sec > 0:
            time.sleep(sleep_sec)

    msg = f"{updated}件 populated / {errors}件エラー / 対象{len(targets)}件"
    logger.info(f"物理データ enrichment 完了: {msg}")
    return {
        "success": errors < len(targets),
        "processed": len(targets),
        "updated": updated,
        "errors": errors,
        "message": msg,
    }


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    cfg = json.loads(
        (Path(__file__).resolve().parent.parent / "config" / "schedule_config.json")
        .read_text(encoding="utf-8")
    )
    r = run_enrich_listings_physical(cfg)
    print(json.dumps(r, indent=2, ensure_ascii=False))
