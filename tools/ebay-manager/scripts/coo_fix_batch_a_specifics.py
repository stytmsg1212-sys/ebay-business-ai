#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#44 バッチA (dry-run 構築): Item Specifics 原産国除去 (対象 498件)

各対象 ebay_item_id について GetItem (C1 修正済み builder =
ebay_client._build_get_item_xml、IncludeItemSpecifics=true) で現行
ItemSpecifics を取得 → ebay_client._filter_forbidden_specifics で禁止 Name
(Country of Origin / Country of Manufacture / Country/Region of Manufacture /
Manufacturer) を除去 → Brand 欠落・値65字超過など送信不能な件は skip リスト
へ分類 → 送信予定 specifics (禁止Name除去後) を dry-run JSON へ出力する。

**eBay への書込 (ReviseItem 実送信) は既定で一切行わない。**
--execute 指定時のみ ebay_client.revise_item_specifics(replace_all=False) で
実送信する (本タスクでは --execute は使用しない。実行は別途 canary 手順で行う)。

入力:
  data/tmp/coo_scan_result_2026_07_04.json (matched_specifics 非空の対象を抽出)
出力:
  data/tmp/coo_fix_batch_a_dryrun.json   (plans / skips / no_action、中断再開可能)
  data/tmp/coo_fix_batch_a_progress.json (50件ごとの進捗スナップショット)

使い方:
  python coo_fix_batch_a_specifics.py            # dry-run (既定)
  python coo_fix_batch_a_specifics.py --limit 10 # 先頭10件だけ dry-run (動作確認用)
  python coo_fix_batch_a_specifics.py --execute  # 実際に eBay へ送信 (未使用・将来のcanary用)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.credentials import get_ebay_credentials  # noqa: E402
from monitor import ebay_client  # noqa: E402
from monitor.listing_content_change_log import log_content_change  # noqa: E402

_INPUT = _ROOT / "data" / "tmp" / "coo_scan_result_2026_07_04.json"

# HIGH-4 fix (T3 2巡目): mode 別 output file。同一 _OUT を dry-run/execute で
# 共有していると、全件 dry-run 済み JSON がある状態で --execute --limit N すると
# done_ids に全件入り remaining=0 = 1 件も送信せず「DONE plans=X」と偽装成功する。
# execute 経路は独立ファイルに分離、execute→execute の resume は維持する。
_OUT_DRYRUN = _ROOT / "data" / "tmp" / "coo_fix_batch_a_dryrun.json"
_OUT_EXECUTE = _ROOT / "data" / "tmp" / "coo_fix_batch_a_execute.json"
_PROGRESS_DRYRUN = _ROOT / "data" / "tmp" / "coo_fix_batch_a_progress.json"
_PROGRESS_EXECUTE = _ROOT / "data" / "tmp" / "coo_fix_batch_a_execute_progress.json"

_THROTTLE_SEC = 0.5


def _load_targets() -> list[str]:
    data = json.loads(_INPUT.read_text(encoding="utf-8"))
    return [d["ebay_item_id"] for d in data if d.get("matched_specifics")]


def _load_existing(out_path: Path) -> dict:
    if out_path.exists():
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # L1 fix (T3 4巡目): 破損 output file を silent に空 dict へ落とすと
            # done_ids が消え resume で全件再送になる。stderr へ 1 行 warning を
            # 出して user が異常に気付けるようにする (Q0 silent 化防止)。
            print(
                f"WARNING: _load_existing: {out_path.name} 読込失敗 "
                f"({type(e).__name__}: {e}) → 空 dict で継続 (done_ids リセット)",
                file=sys.stderr, flush=True,
            )
    # HIGH-1 fix (T3 Codex): send_failed バケットを分離。exec_result.success=False
    # や postverify.ok=False の item を plans に入れると done_ids に吸収されて
    # resume で再送されず silent 取り残しになる。send_failed は done_ids から
    # 除外して次回実行で再試行可能にする (canary 合否ゲートは success AND
    # postverify.ok)。
    return {"plans": [], "skips": [], "no_action": [], "send_failed": []}


def _derive_ack(exec_result: dict) -> str | None:
    """MED-1 fix (T3 2巡目): revise_* API のうち _call_trading_api 経由の
    revise_item_specifics は ack キーを返すが、revise_item_description /
    revise_item_condition は返さない (dict にキー自体が無い)。呼出側で本 helper
    を挟み、実 ack が無い場合は success フラグから 'Success' を **導出** して
    ebay_ack に保存する。「実 ack ではない=導出値」であることは source_tab
    (coo_fix_batch_x) と併せて監査時に判断可能。"""
    ack = exec_result.get("ack")
    if ack:
        return ack
    if exec_result.get("success"):
        return "Success (derived from success flag; revise_* dict has no 'ack' key)"
    return None


def process_one(item_id: str, creds: dict, execute: bool) -> dict:
    app, dev, cert, tok = (
        creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
    )

    current = ebay_client._get_item_specifics_for_merge(item_id, app, dev, cert, tok)
    if current is None:
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": "GetItem失敗 (現行ItemSpecifics取得不能)",
        }

    filtered, removed_names = ebay_client._filter_forbidden_specifics(current)

    if not removed_names:
        # スキャン時点では原産国系Nameがあったはずだが、現時点では既に存在しない
        # (別経路で対応済み、または再スキャン時点で消えていた)。送信は不要。
        return {
            "ebay_item_id": item_id, "status": "no_action_needed",
            "reason": "現行ItemSpecificsに禁止Nameが既に存在しない (対応済み or 再取得時に変化)",
        }

    if not filtered:
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": f"禁止Name除外後に送信対象なし (removed={removed_names})",
        }

    has_brand = any(str(k).strip().lower() == "brand" for k in filtered)
    if not has_brand:
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": f"Brand欠落のため送信不能 (removed={removed_names})",
        }

    try:
        ebay_client._build_item_specifics_nvl_xml(filtered)
    except ValueError as e:
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": f"値が65字を超過: {e}",
        }

    plan = {
        "ebay_item_id": item_id,
        "status": "plan",
        "removed_names": removed_names,
        "sent_specifics": filtered,
    }

    if execute:
        # HIGH-1 fix (T3 review): 空 dict + replace_all=False は API 冒頭の
        # 'item_specifics is empty' ガードで全件失敗になる。dry-run で算出済み
        # の filtered (現行フルセットから禁止Name除去、Brand含む、65字検証済)
        # を replace_all=True で送信する。API 側の禁止Name二重フィルタ + Brand
        # 欠落 reject + post-verify (GetItem read-back) はそのまま働く。
        result = ebay_client.revise_item_specifics(
            item_id, filtered, app_id=app, dev_id=dev, cert_id=cert,
            user_token=tok, replace_all=True,
        )
        plan["execute_result"] = result
        # HIGH-3 fix: 監査ログを毎件即時記録
        # MED-1 fix (T3 2巡目): revise_item_specifics は _call_trading_api 経由
        #   で 'ack' キーを実返却するため、_derive_ack でも実 ack がそのまま渡る
        #   (本バッチは「実 ack」記録)。B/C は導出値 = _derive_ack で success フラグ
        #   から補完。
        # MED-2 fix (T3 2巡目): eBay 送信成功直後に監査ログ INSERT で
        #   sqlite3.OperationalError / OSError が発生すると証跡欠落する。except
        #   を拡張し、失敗時は plan.log_error に記録して呼出側で救う (Q0: silent 化
        #   はしない)。
        try:
            log_content_change(
                item_id, "item_specifics",
                before_value=json.dumps(current, ensure_ascii=False),
                after_value=json.dumps(result.get("sent_specifics", filtered),
                                       ensure_ascii=False),
                source_tab="coo_fix_batch_a",
                success=bool(result.get("success")),
                ebay_ack=_derive_ack(result),
            )
        except (ValueError, RuntimeError, sqlite3.Error, OSError) as e:
            plan["log_error"] = f"log_content_change 失敗: {type(e).__name__}: {e}"

    return plan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                     help="実際に eBay へ ReviseItem を送信する (既定は dry-run)")
    ap.add_argument("--limit", type=int, default=None,
                     help="先頭 N 件だけ処理 (動作確認用)")
    args = ap.parse_args()

    if args.execute:
        print("*** --execute 指定: 実際に eBay へ書込みます ***", flush=True)
    else:
        print("dry-run モード (eBay への書込みなし)", flush=True)

    creds = get_ebay_credentials({})
    if not all([creds.get("app_id"), creds.get("dev_id"),
                creds.get("cert_id"), creds.get("user_token")]):
        print("ERROR: eBay credentials 不在 (.env 確認)")
        sys.exit(1)

    # HIGH-4 fix (T3 2巡目): mode 別 output path。execute 時は _execute.json、
    # dry-run 時は _dryrun.json に分離。execute→execute の resume は _execute.json
    # の done_ids で維持される。dry-run の done_ids は execute の resume 判定に
    # 混入しない (これが根治の要)。
    out_path = _OUT_EXECUTE if args.execute else _OUT_DRYRUN
    progress_path = _PROGRESS_EXECUTE if args.execute else _PROGRESS_DRYRUN
    print(f"output: {out_path.name}", flush=True)

    targets = _load_targets()
    if args.limit:
        targets = targets[: args.limit]

    existing = _load_existing(out_path)
    plans = existing.get("plans", [])
    skips = existing.get("skips", [])
    no_action = existing.get("no_action", [])
    send_failed = existing.get("send_failed", [])
    # HIGH-1 fix (T3 Codex): send_failed は done_ids に**入れない** (次回実行で再試行)。
    done_ids = (
        {p["ebay_item_id"] for p in plans}
        | {s["ebay_item_id"] for s in skips}
        | {n["ebay_item_id"] for n in no_action}
    )
    remaining = [t for t in targets if t not in done_ids]

    total = len(remaining)
    print(f"START batch A: target={len(targets)} already_done={len(done_ids)} "
          f"remaining={total} (retry_from_failed={len(send_failed)})", flush=True)

    # HIGH-4 fix: execute + remaining==0 は loud warning + exit(2) で偽装成功防止
    if args.execute and total == 0:
        print("!" * 60, flush=True)
        print(
            "!! FATAL: execute 指定なのに remaining=0 (送信対象 0 件)。",
            flush=True,
        )
        print(
            f"!! 原因候補: (a) 過去 execute 完走済 (再実行不要), "
            f"(b) targets が {out_path.name} の done_ids と全一致 = 実際は送信不要, "
            f"(c) --limit N 指定で N 件全て done_ids 済み",
            flush=True,
        )
        print(
            "!! 追加送信したい場合は out_path を rename か削除して done_ids を "
            "クリアしてから再実行してください。",
            flush=True,
        )
        print("!" * 60, flush=True)
        sys.exit(2)

    def _flush():
        out_path.write_text(
            json.dumps({"plans": plans, "skips": skips, "no_action": no_action,
                        "send_failed": send_failed},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    for i, item_id in enumerate(remaining, 1):
        result = process_one(item_id, creds, args.execute)
        status = result["status"]
        # HIGH-1/2 fix (T3 Codex): 送信失敗 or postverify.ok=False は send_failed へ。
        # 合否ゲート: 成功 = exec_result.success AND (postverify 未計上 OR postverify.ok)
        if status == "plan" and args.execute:
            exec_result = result.get("execute_result") or {}
            postverify = result.get("postverify") or {}
            exec_ok = bool(exec_result.get("success"))
            # postverify がある場合 (batch B/C 相当) は ok 必須。無い場合 (batch A の
            # 現状 = 明示 postverify 節無し) は revise_item_specifics 側の read-back
            # verify (Ack + GetItem 一致) で保証済 = success フラグで足りる。
            verify_ok = postverify.get("ok", True) if postverify else True
            if not (exec_ok and verify_ok):
                result["status"] = "send_failed"
                result["failure_reason"] = (
                    f"exec_ok={exec_ok} verify_ok={verify_ok} "
                    f"exec_msg={exec_result.get('message', '')[:200]}"
                )
                send_failed.append(result)
                status = "send_failed"
            else:
                # M1 fix (T3 4巡目): retry で成功した item は旧 send_failed エントリ
                # を除去 (残すと同 item が failed と plans の両方に存在し、監査で
                # 「まだ失敗中」と誤認する)。
                send_failed[:] = [
                    x for x in send_failed if x.get("ebay_item_id") != item_id
                ]

        if status == "plan":
            plans.append(result)
        elif status == "no_action_needed":
            no_action.append(result)
        elif status == "send_failed":
            pass  # 既に append 済 (上記ブロック)
        else:
            skips.append(result)

        # HIGH-3 fix: execute 時は毎件 flush (dry-run 時は 50件ごと)
        flush_now = args.execute or (i % 50 == 0) or (i == total)
        if flush_now:
            _flush()
            progress_path.write_text(
                json.dumps({
                    "processed_this_run": i, "remaining_total": total,
                    "plans": len(plans), "skips": len(skips),
                    "no_action": len(no_action),
                    "send_failed": len(send_failed),
                }, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            print(f"[{i}/{total}] plans={len(plans)} skips={len(skips)} "
                  f"no_action={len(no_action)} send_failed={len(send_failed)}",
                  flush=True)

        time.sleep(_THROTTLE_SEC)

    _flush()
    print("=" * 60)
    print(f"DONE batch A. plans={len(plans)} skips={len(skips)} "
          f"no_action={len(no_action)} send_failed={len(send_failed)}")
    print(f"出力: {out_path}")


if __name__ == "__main__":
    main()
