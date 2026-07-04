"""item_id=358754421540 description本文中の自己言及 "Rank B" 誤記修正 (2026-07-04 one-shot).

背景: 実ランクはA (ConditionID 3000)。ConditionDescriptionは既に
      "Rank A -- Excellent. Tested, fully working. Minor wear." に修正済みだが、
      description本文(HTML)の "Condition Rank" バッジセクション
      (<div class="mh-rb-letter">B</div> / <h3>Rank B &mdash; Good</h3>) に
      古い "Rank B" 表記が2箇所残存していた。

方針: ピンポイント修正のみ。定義表 (Condition Rank Definitions、全ランク
      N/S/A/B/C/D/PO/As-Is列挙) のRank B行、CSSコメント "Rank Block (Enso
      brush)" "Rank definitions table" は一切変更しない。

usage:
  python fix_desc_rank_358754421540_2026_07_04.py fetch    # GetItemで現行description取得+保存
  python fix_desc_rank_358754421540_2026_07_04.py apply    # revise_item_descriptionで反映+監査ログ
  python fix_desc_rank_358754421540_2026_07_04.py verify   # GetItemで再取得し反映確認
"""
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import xml.etree.ElementTree as ET

from monitor.inventory_sync import _get_credentials
from monitor.ebay_client import _call_trading_api, revise_item_description
from monitor.listing_content_change_log import log_content_change

ITEM_ID = "358754421540"
SCRATCH = (
    r"C:/Users/gucch/AppData/Local/Temp/claude/"
    r"C--Users-gucch-projects-claude/97bd4562-55e3-4a94-a338-23b41e7c79ec/scratchpad"
)
ORIG_PATH = f"{SCRATCH}/rankb_original_desc.txt"
PROPOSED_PATH = f"{SCRATCH}/rankb_proposed_desc.txt"
READBACK_PATH = f"{SCRATCH}/rankb_readback_desc.txt"

# ピンポイント置換対象 (自己言及バッジ2箇所のみ、定義表・CSSコメントは対象外)
TARGET_LETTER_OLD = '<div class="mh-rb-letter">B</div>'
TARGET_LETTER_NEW = '<div class="mh-rb-letter">A</div>'
TARGET_H3_OLD = "<h3>Rank B &mdash; Good</h3>"
TARGET_H3_NEW = "<h3>Rank A &mdash; Excellent</h3>"


def _ns():
    return {"ns": "urn:ebay:apis:eBLBaseComponents"}


def fetch_description(creds) -> str:
    app_id, dev_id, cert_id, user_token = creds
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken></RequesterCredentials>
  <ItemID>{ITEM_ID}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
  <IncludeItemSpecifics>true</IncludeItemSpecifics>
</GetItemRequest>"""
    res = _call_trading_api("GetItem", xml_body, app_id, dev_id, cert_id, user_token)
    if not res.get("success"):
        raise RuntimeError(f"GetItem 失敗: {res.get('message')}")
    root = ET.fromstring(res["raw"])
    item = root.find("ns:Item", _ns())
    return item.findtext("ns:Description", namespaces=_ns()) or ""


def build_proposed(original: str) -> str:
    if original.count(TARGET_LETTER_OLD) != 1:
        raise RuntimeError(
            f"letter target 出現数想定外: {original.count(TARGET_LETTER_OLD)}"
        )
    if original.count(TARGET_H3_OLD) != 1:
        raise RuntimeError(f"h3 target 出現数想定外: {original.count(TARGET_H3_OLD)}")
    proposed = original.replace(TARGET_LETTER_OLD, TARGET_LETTER_NEW, 1)
    proposed = proposed.replace(TARGET_H3_OLD, TARGET_H3_NEW, 1)
    # 定義表・CSSコメントは無傷であることの保険assert (silent skip 防止)
    if '<tr><td>B</td><td>Good &mdash; Visible use marks, tested and fully working</td></tr>' not in proposed:
        raise RuntimeError("定義表 Rank B 行が破壊された疑い")
    if "/* ==== Rank Block (Enso brush) ==== */" not in proposed:
        raise RuntimeError("CSSコメント Rank Block が破壊された疑い")
    if "/* ==== Rank definitions table ==== */" not in proposed:
        raise RuntimeError("CSSコメント Rank definitions table が破壊された疑い")
    return proposed


def cmd_fetch(creds) -> None:
    desc = fetch_description(creds)
    with open(ORIG_PATH, "w", encoding="utf-8") as f:
        f.write(desc)
    print(f"fetch OK: {len(desc)}字 -> {ORIG_PATH}")


def cmd_apply(creds) -> None:
    with open(ORIG_PATH, encoding="utf-8") as f:
        original = f.read()
    proposed = build_proposed(original)
    with open(PROPOSED_PATH, "w", encoding="utf-8") as f:
        f.write(proposed)
    print(f"proposed 作成: {len(proposed)}字 (orig {len(original)}字)")

    app_id, dev_id, cert_id, user_token = creds
    res = revise_item_description(ITEM_ID, proposed, app_id, dev_id, cert_id, user_token)
    print(f"revise_item_description: success={res.get('success')} "
          f"message={res.get('message')!r} desc_len={res.get('description_len')}")

    log_id = log_content_change(
        ITEM_ID,
        "description",
        original,
        proposed,
        source_tab="manual_fix_358754421540",
        success=bool(res.get("success")),
        ebay_ack=res.get("message") if not res.get("success") else "Success",
    )
    print(f"log_content_change id={log_id}")


def cmd_verify(creds) -> None:
    desc = fetch_description(creds)
    with open(READBACK_PATH, "w", encoding="utf-8") as f:
        f.write(desc)
    print(f"readback 保存: {len(desc)}字 -> {READBACK_PATH}")

    checks = {
        "letter now A": TARGET_LETTER_NEW in desc,
        "letter old B gone": TARGET_LETTER_OLD not in desc,
        "h3 now Rank A": TARGET_H3_NEW in desc,
        "h3 old Rank B gone": TARGET_H3_OLD not in desc,
        "defs table B row intact": (
            '<tr><td>B</td><td>Good &mdash; Visible use marks, tested and fully working</td></tr>'
            in desc
        ),
        "defs table A row intact": (
            '<tr><td>A</td><td>Excellent &mdash; Minor wear, tested and fully working</td></tr>'
            in desc
        ),
        "css comment Rank Block intact": (
            "/* ==== Rank Block (Enso brush) ==== */" in desc
        ),
        "css comment Rank definitions table intact": (
            "/* ==== Rank definitions table ==== */" in desc
        ),
    }
    for k, v in checks.items():
        print(f"  [{'OK' if v else 'NG'}] {k}")
    print(f"desc length: {len(desc)}字")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    creds = _get_credentials()
    if not creds:
        print("FAIL: creds 解決不可")
        sys.exit(1)
    if cmd == "fetch":
        cmd_fetch(creds)
    elif cmd == "apply":
        cmd_apply(creds)
    elif cmd == "verify":
        cmd_verify(creds)
    else:
        print(f"unknown cmd: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
