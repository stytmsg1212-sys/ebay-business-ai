# -*- coding: utf-8 -*-
"""W284 Phase 2: eBaymag 更新同期 監査タスク (日次).

US 本体の価格・説明文・画像が eBaymag 各国版に同期されているかを検査し、
乖離を検出した場合に Discord 通知 + 件数を返す。

参考実装: scripts/audit_ebaymag_update_sync_2026_06_20.py (READ ONLY、手動実行用)。
本タスクは上記を daily scheduler に組み込むため task 化したもの。

識別キー: ebay_item_id (SKU 禁止。ただし本監査で対象ペア選定に
ebay** SKU 一致を使うのは「US本体 1件:各国版 1件」の確信ペアを作るため。
積極的なキー使用ではなく、マッチング品質フィルタとして使用している)。
timezone: UTC (datetime('now') 系)。
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRADING_API_URL = "https://api.ebay.com/ws/api.dll"
API_VERSION = "1193"
NS = {"ns": "urn:ebay:apis:eBLBaseComponents"}

# 1 run で比較するペア上限 (API 呼び出しコスト抑制)
_MAX_PAIRS = 50
# 価格比率の外れ値閾値 (中央値から 10% 超を乖離とみなす)
_PRICE_OUTLIER_RATIO = 0.10
# 説明文長の乖離比率閾値
_DESC_LEN_OUTLIER_RATIO = 0.30


def _get_item_raw(item_id: str, cr: dict) -> dict:
    """GetItem で price/currency/picture/description を取得 (READ ONLY)。

    エラー時は {"error": str} を返す (例外は内部で吸収)。
    """
    try:
        from monitor.ebay_client import _resolve_active_token
        import httpx
        token = _resolve_active_token(cr["user_token"])
        body = (
            f'<?xml version="1.0" encoding="utf-8"?>'
            f'<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<RequesterCredentials><eBayAuthToken>{token}</eBayAuthToken></RequesterCredentials>'
            f'<ItemID>{item_id}</ItemID>'
            f'<DetailLevel>ReturnAll</DetailLevel>'
            f'</GetItemRequest>'
        )
        headers = {
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-APP-NAME": cr["app_id"],
            "X-EBAY-API-DEV-NAME": cr["dev_id"],
            "X-EBAY-API-CERT-NAME": cr["cert_id"],
            "Content-Type": "text/xml",
        }
        resp = httpx.post(TRADING_API_URL, content=body.encode("utf-8"), headers=headers, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}

    ack = root.findtext("ns:Ack", namespaces=NS)
    if ack not in ("Success", "Warning"):
        errs = root.findall(".//ns:Errors/ns:LongMessage", namespaces=NS)
        msg = "; ".join(e.text for e in errs if e.text)[:120] or "api error"
        return {"error": msg}
    item = root.find(".//ns:Item", namespaces=NS)
    if item is None:
        return {"error": "no item element"}
    ss = item.find("ns:SellingStatus", namespaces=NS)
    cur_el = ss.find("ns:CurrentPrice", namespaces=NS) if ss is not None else None
    price = float(cur_el.text) if (cur_el is not None and cur_el.text) else 0.0
    currency = (cur_el.get("currencyID") if cur_el is not None else "") or ""
    pics = [p.text for p in item.findall(".//ns:PictureDetails/ns:PictureURL", namespaces=NS) if p.text]
    desc = item.findtext("ns:Description", namespaces=NS) or ""
    title = item.findtext("ns:Title", namespaces=NS) or ""
    return {
        "item_id": item_id, "price": price, "currency": currency,
        "n_pics": len(pics), "pics": pics, "desc_len": len(desc), "title": title,
    }


def _pic_id(url: str) -> str:
    """eBay 画像 URL から安定 ID 部分を抽出 (CDN host 差吸収)。"""
    if not url:
        return ""
    m = (
        re.search(r"/([A-Za-z0-9~_-]{8,})/s-l\d+", url)
        or re.search(r"/([A-Za-z0-9~_-]{12,})\.(jpg|jpeg|png|webp)", url)
    )
    return m.group(1) if m else url.rsplit("/", 1)[-1][:24]


def _load_pairs() -> list[tuple[dict, dict]]:
    """audit_ebaymag_intl_raw_*.json (手動取得) から確信ペアを読み込む。

    ebay** SKU が US本体と各国版で 1:1 に一致するペアのみ対象。
    ファイルが存在しない場合は [] を返す (監査スキップ)。
    """
    import glob
    tmp_dir = Path(__file__).resolve().parent.parent / "data" / "tmp"
    files = sorted(glob.glob(str(tmp_dir / "audit_ebaymag_intl_raw_*.json")))
    if not files:
        return []
    try:
        intl_data: list[dict] = json.load(open(files[-1], encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[ebaymag_sync_audit] raw ファイル読込失敗 %s: %s", files[-1], e)
        return []

    from monitor.database import get_conn
    us_by_sku: defaultdict[str, list[dict]] = defaultdict(list)
    with get_conn() as conn:
        for r in conn.execute(
            "SELECT ebay_item_id, sku, title, current_price, ebaymag_segment "
            "FROM ebay_listings WHERE COALESCE(is_ended,0)=0"
        ).fetchall():
            s = (r["sku"] or "").strip().lower()
            if s.startswith("ebay"):
                us_by_sku[s].append(dict(r))

    pairs: list[tuple[dict, dict]] = []
    for x in intl_data:
        s = (x.get("sku") or "").strip().lower()
        if not s.startswith("ebay"):
            continue
        m = us_by_sku.get(s, [])
        if len(m) == 1:
            pairs.append((x, m[0]))
    return pairs


def run_ebaymag_sync_audit(config: dict) -> dict:
    """W284 Phase 2: eBaymag 更新同期 監査 (日次)。

    US 本体 vs 各国版の価格比 / 画像 / 説明文長を比較し、乖離を Discord 通知する。

    Returns:
        {"success": bool, "pairs_checked": int, "price_outliers": int,
         "pic_mismatches": int, "desc_outliers": int, "message": str}
    """
    logger.info("[ebaymag_sync_audit] 監査開始")

    # eBay 認証情報取得
    try:
        from monitor.credentials import get_ebay_credentials
        cr = get_ebay_credentials()
    except Exception as e:  # noqa: BLE001
        msg = f"eBay 認証情報取得失敗: {e}"
        logger.error("[ebaymag_sync_audit] %s", msg)
        return {"success": False, "pairs_checked": 0,
                "price_outliers": 0, "pic_mismatches": 0, "desc_outliers": 0, "message": msg}

    # ペア読込
    try:
        all_pairs = _load_pairs()
    except Exception as e:  # noqa: BLE001
        msg = f"確信ペア読込失敗: {e}"
        logger.error("[ebaymag_sync_audit] %s", msg)
        return {"success": False, "pairs_checked": 0,
                "price_outliers": 0, "pic_mismatches": 0, "desc_outliers": 0, "message": msg}

    if not all_pairs:
        msg = ("確信ペアなし — data/tmp/audit_ebaymag_intl_raw_*.json が必要です。"
               "scripts/audit_ebaymag_intl_2026_06_20.py を手動実行してください。")
        logger.info("[ebaymag_sync_audit] %s", msg)
        return {"success": True, "pairs_checked": 0,
                "price_outliers": 0, "pic_mismatches": 0, "desc_outliers": 0, "message": msg}

    pairs = all_pairs[:_MAX_PAIRS]
    logger.info("[ebaymag_sync_audit] 確信ペア %d 件 (max %d) を検査", len(pairs), _MAX_PAIRS)

    ratios: defaultdict[str, list[float]] = defaultdict(list)
    rows: list[dict] = []
    pic_match = 0
    pic_mismatch = 0
    api_errors = 0

    for intl, us in pairs:
        us_eid = str(us["ebay_item_id"])
        intl_eid = str(intl.get("item_id", ""))
        if not intl_eid:
            continue

        us_item = _get_item_raw(us_eid, cr)
        in_item = _get_item_raw(intl_eid, cr)

        if us_item.get("error") or in_item.get("error"):
            api_errors += 1
            logger.warning(
                "[ebaymag_sync_audit] GetItem error: us=%s err=%s / intl=%s err=%s",
                us_eid, us_item.get("error"), intl_eid, in_item.get("error"),
            )
            continue

        usp: float = us_item["price"]
        inp: float = in_item["price"]
        ratio = (inp / usp) if usp else 0.0
        ratios[in_item["currency"]].append(ratio)

        us_pids = {_pic_id(u) for u in us_item["pics"]}
        in_pids = {_pic_id(u) for u in in_item["pics"]}
        pic_overlap = bool(us_pids & in_pids)
        if pic_overlap:
            pic_match += 1
        else:
            pic_mismatch += 1

        rows.append({
            "us_item": us_eid,
            "intl_item": intl_eid,
            "us_price": usp, "intl_price": inp, "intl_cur": in_item["currency"],
            "ratio": round(ratio, 4),
            "us_npics": us_item["n_pics"], "intl_npics": in_item["n_pics"],
            "pic_overlap": pic_overlap,
            "us_desc_len": us_item["desc_len"], "intl_desc_len": in_item["desc_len"],
            "us_title": us_item["title"][:40], "intl_title": in_item["title"][:40],
        })

    # 乖離検出
    price_outlier_items: list[str] = []
    for cur, rs in ratios.items():
        if len(rs) < 2:
            continue
        med = statistics.median(rs)
        for row in rows:
            if row["intl_cur"] != cur:
                continue
            if med and abs(row["ratio"] - med) / med > _PRICE_OUTLIER_RATIO:
                price_outlier_items.append(
                    f"{row['us_title'][:30]} (US={row['us_price']:.0f} "
                    f"intl={row['intl_price']:.0f}{cur} ratio={row['ratio']:.3f} med={med:.3f})"
                )

    pic_mismatch_items = [
        f"{r['us_title'][:30]} (us_pics={r['us_npics']} intl_pics={r['intl_npics']})"
        for r in rows if not r["pic_overlap"] and r["us_npics"] > 0 and r["intl_npics"] > 0
    ]

    desc_outlier_items = [
        f"{r['us_title'][:30]} (us_desc={r['us_desc_len']} intl_desc={r['intl_desc_len']})"
        for r in rows
        if r["us_desc_len"] > 0 and r["intl_desc_len"] > 0
        and abs(r["intl_desc_len"] - r["us_desc_len"]) / r["us_desc_len"] > _DESC_LEN_OUTLIER_RATIO
    ]

    n_price = len(price_outlier_items)
    n_pic = len(pic_mismatch_items)
    n_desc = len(desc_outlier_items)

    # Discord 通知 (乖離あり時)
    if n_price + n_pic + n_desc > 0:
        lines = [
            f"[eBaymag 同期監査] 確信ペア {len(rows)}/{len(pairs)} 件検査",
        ]
        if n_price:
            lines.append(f"価格乖離 {n_price}件:\n" + "\n".join(f"  - {s}" for s in price_outlier_items[:3]))
        if n_pic:
            lines.append(f"画像不一致 {n_pic}件:\n" + "\n".join(f"  - {s}" for s in pic_mismatch_items[:3]))
        if n_desc:
            lines.append(f"説明文長乖離 {n_desc}件:\n" + "\n".join(f"  - {s}" for s in desc_outlier_items[:3]))
        # 乖離検知 (mutate 監査の失敗系 = money-direct) → severity='error'
        _discord_notify(config, "\n".join(lines), severity="error")
        logger.warning("[ebaymag_sync_audit] 乖離検出: price=%d pic=%d desc=%d", n_price, n_pic, n_desc)
    else:
        logger.info("[ebaymag_sync_audit] 乖離なし (pairs=%d)", len(rows))

    # 結果を data/tmp に保存
    try:
        from datetime import date
        out_dir = Path(__file__).resolve().parent.parent / "data" / "tmp"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"audit_ebaymag_sync_{date.today()}.json"
        out_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("[ebaymag_sync_audit] 結果保存: %s", out_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ebaymag_sync_audit] 結果保存失敗 (本処理に影響なし): %s", e)

    msg = (
        f"pairs_checked={len(rows)} api_errors={api_errors} "
        f"price_outliers={n_price} pic_mismatches={n_pic} desc_outliers={n_desc}"
    )
    return {
        "success": True,
        "pairs_checked": len(rows),
        "price_outliers": n_price,
        "pic_mismatches": n_pic,
        "desc_outliers": n_desc,
        "message": msg,
    }


def _discord_notify(config: dict, message: str, *, severity: str = "info") -> None:
    """Discord 既定 ch に通知。送信失敗は warn のみ。

    依頼ボード#39 S2 follow-up (2026-07-03): severity 引数を追加。同期監査の乖離検知
    (価格 / 画像 / 説明文乖離 = 各国版の実態が本体とずれている money-direct 検知) は
    'error' で _ALWAYS_SEND_SEVERITIES bypass を効かせる。
    """
    try:
        from notifiers.discord_notifier import notifier_for
        notifier_for("default").send_message(message, severity=severity)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ebaymag_sync_audit] Discord 通知失敗: %s", e)
