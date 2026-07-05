#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W325: PLOTTER 3件 (新規原産国混入) の ItemSpecifics 是正 one-shot script.

背景: b14a03a (#44 4層防御) 適用後も、以下 3件で eBay 側の Product Catalog
自動マッチ (Brand+MPN 一致 → ProductListingDetails.IncludeeBayProductDetails=true)
により Country of Origin=Japan が ItemSpecifics に自動付与されていた
(我々の listing_drafts.item_specifics には該当 Name が一切含まれておらず、
4層防御の対象=我々が明示送信する specifics とは別経路の混入と判明)。

対象 (ハードコード、この3件専用の使い捨てscript、K1 simplicity):
  - 358754325896 (PLOTTER 5012 A5)
  - 358754360321 (PLOTTER 5001 Bible Size)
  - 358755217619 (PLOTTER 5012 Mini)

ロジックは scripts/coo_fix_batch_a_specifics.py の process_one() と同一
(GetItem → _filter_forbidden_specifics → revise_item_specifics(replace_all=True)
→ log_content_change)。差分は対象リストのハードコードのみ。

実行:
  python scripts/coo_fix_w325_plotter3.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.credentials import get_ebay_credentials  # noqa: E402
from monitor import ebay_client  # noqa: E402
from monitor.listing_content_change_log import log_content_change  # noqa: E402

TARGETS = [
    "358754325896",  # PLOTTER 5012 A5 6-Ring Liscio Leather Binder
    "358754360321",  # PLOTTER 5001 Bible Size 6-Ring Pueblo Leather Binder
    "358755217619",  # PLOTTER 5012 Mini 6-Ring Liscio Leather Binder
]

SOURCE_TAB = "coo_fix_w325_plotter3"


def process_one(item_id: str, creds: dict) -> dict:
    app, dev, cert, tok = (
        creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
    )

    before = ebay_client._get_item_specifics_for_merge(item_id, app, dev, cert, tok)
    if before is None:
        return {"ebay_item_id": item_id, "status": "skip",
                 "reason": "GetItem失敗 (現行ItemSpecifics取得不能)"}

    filtered, removed_names = ebay_client._filter_forbidden_specifics(before)

    if not removed_names:
        return {"ebay_item_id": item_id, "status": "no_action_needed",
                 "before": before, "reason": "禁止Nameは既に存在しない"}

    has_brand = any(str(k).strip().lower() == "brand" for k in filtered)
    if not has_brand:
        return {"ebay_item_id": item_id, "status": "skip",
                 "before": before, "reason": f"Brand欠落のため送信不能 (removed={removed_names})"}

    result = ebay_client.revise_item_specifics(
        item_id, filtered, app_id=app, dev_id=dev, cert_id=cert,
        user_token=tok, replace_all=True,
    )

    # 独立 GetItem で read-back (revise_item_specifics 内部の検証とは別に、
    # 本 script 自身でも最終確認する。Q0: 偽装成功防止)
    after = ebay_client._get_item_specifics_for_merge(item_id, app, dev, cert, tok)

    ack = result.get("ack")
    if not ack and result.get("success"):
        ack = "Success (derived from success flag; revise_item_specifics has no 'ack' key)"

    try:
        log_content_change(
            item_id, "item_specifics",
            before_value=json.dumps(before, ensure_ascii=False),
            after_value=json.dumps(result.get("sent_specifics", filtered), ensure_ascii=False),
            source_tab=SOURCE_TAB,
            success=bool(result.get("success")),
            ebay_ack=ack,
        )
        log_error = None
    except Exception as e:  # noqa: BLE001
        log_error = f"log_content_change 失敗: {type(e).__name__}: {e}"

    return {
        "ebay_item_id": item_id,
        "status": "done" if result.get("success") else "send_failed",
        "before": before,
        "removed_names": removed_names,
        "sent_specifics": result.get("sent_specifics", filtered),
        "after_readback": after,
        "coo_still_present": bool(after and "Country of Origin" in after),
        "revise_message": result.get("message"),
        "log_error": log_error,
    }


def main() -> None:
    creds = get_ebay_credentials({})
    if not all([creds.get("app_id"), creds.get("dev_id"),
                creds.get("cert_id"), creds.get("user_token")]):
        print("ERROR: eBay credentials 不在 (.env 確認)")
        sys.exit(1)

    results = []
    for item_id in TARGETS:
        print(f"=== processing {item_id} ===", flush=True)
        r = process_one(item_id, creds)
        results.append(r)
        print(json.dumps(r, ensure_ascii=False, indent=1), flush=True)

    out_path = _ROOT / "data" / "tmp" / "coo_fix_w325_plotter3_result.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n結果を書込: {out_path}")

    n_ok = sum(1 for r in results if r["status"] == "done" and not r.get("coo_still_present"))
    print(f"DONE: {n_ok}/{len(TARGETS)} 件で Country of Origin 除去確認 (read-back)")


if __name__ == "__main__":
    main()
