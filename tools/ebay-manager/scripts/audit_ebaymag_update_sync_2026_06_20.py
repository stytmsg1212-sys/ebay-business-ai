#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag 更新同期 監査 (READ ONLY, 2026-06-20).

目的 (依頼ボード #4 W263 拡張 / user 要望):
  US 本体の「価格・画像・説明文」変更が eBaymag 各国版に反映されているか実データで確認。

手法:
  無在庫 ebay** SKU が US本体↔各国版で一意一致するペア (確信ペア) を対象に、
  GetItem で双方を取得して以下を照合:
    - 価格: 海外版価格 / US本体価格 の比率を通貨別に集計 → 比率が一定なら一貫換算
      (外れ値 = 価格更新が未反映で取り残された商品)
    - 画像: PictureURL の安定 ID 部分が一致するか (eBaymag は画像を翻訳しない)
    - 説明文: 文字数 / 翻訳有無 (各国版は翻訳されるので直接一致はしない想定)

注意: 完全 READ ONLY。GetItem 参照のみ。DB も eBay も一切変更しない。
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DB_PATH = _ROOT / "data" / "monitor.db"
OUT_DIR = _ROOT / "data" / "tmp"
TRADING_API_URL = "https://api.ebay.com/ws/api.dll"
API_VERSION = "1193"
NS = {"ns": "urn:ebay:apis:eBLBaseComponents"}

MAX_PAIRS = 47  # 確信ペア上限 (全件)


def _get_item_raw(item_id: str, cr: dict) -> dict | None:
    """GetItem を直接呼び price/currency/picture/description を抽出 (READ ONLY)."""
    from monitor.ebay_client import _resolve_active_token
    token = _resolve_active_token(cr["user_token"])
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{token}</eBayAuthToken></RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "GetItem",
        "X-EBAY-API-APP-NAME": cr["app_id"],
        "X-EBAY-API-DEV-NAME": cr["dev_id"],
        "X-EBAY-API-CERT-NAME": cr["cert_id"],
        "Content-Type": "text/xml",
    }
    try:
        resp = httpx.post(TRADING_API_URL, content=body.encode("utf-8"), headers=headers, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as e:
        return {"error": str(e)[:120]}
    if root.findtext("ns:Ack", namespaces=NS) not in ("Success", "Warning"):
        errs = root.findall(".//ns:Errors/ns:LongMessage", namespaces=NS)
        return {"error": "; ".join(e.text for e in errs if e.text)[:120] or "api error"}
    item = root.find(".//ns:Item", namespaces=NS)
    if item is None:
        return {"error": "no item"}
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
    """eBay 画像 URL から安定 ID 部分を抽出 (CDN host 差を吸収)."""
    if not url:
        return ""
    m = re.search(r"/([A-Za-z0-9~_-]{8,})/s-l\d+", url) or re.search(r"/([A-Za-z0-9~_-]{12,})\.(jpg|jpeg|png|webp)", url)
    return m.group(1) if m else url.rsplit("/", 1)[-1][:24]


def load_pairs() -> list[tuple[dict, dict]]:
    """audit raw snapshot + US本体 DB から ebay** SKU 一意一致の確信ペアを作る."""
    import glob
    raw = sorted(glob.glob(str(OUT_DIR / "audit_ebaymag_intl_raw_*.json")))[-1]
    intl = json.load(open(raw, encoding="utf-8"))
    ebay_intl = [x for x in intl if (x.get("sku") or "").lower().startswith("ebay")]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    us_by_sku = defaultdict(list)
    for r in conn.execute(
        "SELECT ebay_item_id, sku, title, current_price, quantity_ebay, "
        "COALESCE(is_ended,0) e, ebaymag_segment FROM ebay_listings"
    ).fetchall():
        s = (r["sku"] or "").strip().lower()
        if s.startswith("ebay"):
            us_by_sku[s].append(dict(r))
    conn.close()
    pairs = []
    for x in ebay_intl:
        s = (x.get("sku") or "").strip().lower()
        m = us_by_sku.get(s, [])
        if len(m) == 1:
            pairs.append((x, m[0]))
    return pairs


def main() -> int:
    from monitor.credentials import get_ebay_credentials
    cr = get_ebay_credentials()
    pairs = load_pairs()[:MAX_PAIRS]
    print(f"=== eBaymag 更新同期 監査 (READ ONLY) === 確信ペア {len(pairs)} 件")

    ratios = defaultdict(list)
    rows = []
    pic_match = pic_mismatch = 0
    for i, (intl, us) in enumerate(pairs, 1):
        us_item = _get_item_raw(str(us["ebay_item_id"]), cr)
        in_item = _get_item_raw(str(intl["item_id"]), cr)
        if us_item.get("error") or in_item.get("error"):
            print(f"  [{i}] ERR us={us_item.get('error')} intl={in_item.get('error')}")
            continue
        usp, inp = us_item["price"], in_item["price"]
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
            "us_item": us["ebay_item_id"], "intl_item": intl["item_id"],
            "us_price": usp, "intl_price": inp, "intl_cur": in_item["currency"],
            "ratio": round(ratio, 4),
            "us_npics": us_item["n_pics"], "intl_npics": in_item["n_pics"],
            "pic_overlap": pic_overlap,
            "us_desc_len": us_item["desc_len"], "intl_desc_len": in_item["desc_len"],
            "us_title": us_item["title"][:40], "intl_title": in_item["title"][:40],
        })
        print(f"  [{i}] US ${usp} ↔ {in_item['currency']} {inp} (比{ratio:.3f}) | "
              f"pic{'一致' if pic_overlap else '不一致'} us{us_item['n_pics']}/intl{in_item['n_pics']} | "
              f"desc us{us_item['desc_len']}/intl{in_item['desc_len']}")

    print("\n== 価格比率 (海外/US) 通貨別 ==")
    for cur, rs in sorted(ratios.items()):
        if rs:
            med = statistics.median(rs)
            outliers = [round(r, 3) for r in rs if med and abs(r - med) / med > 0.10]
            print(f"  {cur}: n={len(rs)} median={med:.3f} min={min(rs):.3f} max={max(rs):.3f} "
                  f"外れ値(>10%乖離)={outliers}")
    print(f"\n== 画像 == 一致(画像ID重複あり)={pic_match} / 不一致={pic_mismatch}")
    descs = [r for r in rows if r["us_desc_len"] and r["intl_desc_len"]]
    print(f"== 説明文 == 両方に説明あり={len(descs)}/{len(rows)} "
          f"(各国版は翻訳される想定なので文字数のみ比較)")

    out = OUT_DIR / "audit_ebaymag_update_sync_2026_06_20.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
