#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#44 バッチC (dry-run 構築): ConditionDescription 空欄補完 (対象 ~211-213件)

data/tmp/coo_scan_result_2026_07_04.json の condition_description が空/null の
対象について、DB (ebay_listings.condition_rank) の商品状態ランクからランク別
定型文 (65字以内・英語、tools/ebay-manager/CLAUDE.md「ConditionDescription
運用方針」/ monitor/listing_generator.py の Condition Description ルールと
同じ調子) を dry-run 出力する。

condition_rank が DB に無い (NULL) 件・As-Is (理由必須で自動生成不可) の件は
**勝手に推定せず** skip リストへ回す (Q0: サイレントスキップ禁止 — 理由を明記
して記録する)。

送信は revise_item_condition (既存関数) を使用。ConditionID (cid) は
**現状維持** (GetItem で現行値を取得しそのまま渡す)、ConditionDescription
(cd) のみ新規に設定する。

⚠️ 既知のギャップ: 本スクリプトの対象は coo_scan_result JSON に収録されている
item のみ (JSON は「原産国が検出された 503件」の副産物として全項目に
condition_description を記録しているが、JSON自体は COO 一致した listing のみを
収録している)。scan summary (`coo_scan_summary_2026_07_04.json`) 上の
condition_description 空欄総数 (213件、全511 active listing 対象) のうち、
JSON に収録されているのは 211件で、残り 2件 (COO 該当なし かつ
condition_description 空欄の listing) は本スクリプトの対象外。この 2件は
別途フルスキャンで item_id を特定しない限り拾えない (dry-run 集計に明記)。

**eBay への書込 (ReviseFixedPriceItem 実送信) は既定で一切行わない。**
--execute 指定時のみ ebay_client.revise_item_condition で実送信する
(本タスクでは --execute は使用しない。実行は別途 canary 手順で行う)。

入力:
  data/tmp/coo_scan_result_2026_07_04.json (condition_description が空/null の対象)
  data/monitor.db ebay_listings.condition_rank (SELECT のみ)
出力:
  data/tmp/coo_fix_batch_c_dryrun.json   (plans / skips / no_action)
  data/tmp/coo_fix_batch_c_progress.json (50件ごとの進捗スナップショット)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.credentials import get_ebay_credentials  # noqa: E402
from monitor import ebay_client  # noqa: E402
from monitor.listing_content_change_log import log_content_change  # noqa: E402
# CD 定型文の正源 (2026-07-04 cascade sync、書式「Rank X — Label. 状態文.」)
from tabs._finishing_panel_state import (  # noqa: E402
    RANK_CONDITION_DESCRIPTION_TEMPLATE as _CANONICAL_RANK_CD_TEMPLATE,
)

_INPUT = _ROOT / "data" / "tmp" / "coo_scan_result_2026_07_04.json"
_SUMMARY = _ROOT / "data" / "tmp" / "coo_scan_summary_2026_07_04.json"
_DB = _ROOT / "data" / "monitor.db"
# HIGH-4 fix: mode 別 output file (batch A/B と同じ設計)
_OUT_DRYRUN = _ROOT / "data" / "tmp" / "coo_fix_batch_c_dryrun.json"
_OUT_EXECUTE = _ROOT / "data" / "tmp" / "coo_fix_batch_c_execute.json"
_PROGRESS_DRYRUN = _ROOT / "data" / "tmp" / "coo_fix_batch_c_progress.json"
_PROGRESS_EXECUTE = _ROOT / "data" / "tmp" / "coo_fix_batch_c_execute_progress.json"

_THROTTLE_SEC = 0.5
_NS = {"n": "urn:ebay:apis:eBLBaseComponents"}
_MAX_CD_LEN = 65  # tools/ebay-manager/CLAUDE.md ConditionDescription 65字制約

# ランク → 定型 ConditionDescription。
# 【単一情報源】tabs/_finishing_panel_state.RANK_CONDITION_DESCRIPTION_TEMPLATE
# を import して使用 (2026-07-04 書式変更「Rank X — Label. 状態文.」で cascade sync)。
# 独自コピーを持たないことで、正源側の書式変更が本スクリプトへ自動反映される。
#
# 正源の設計 (再掲、単一情報源の docstring):
#   - N (ConditionID 1000): eBay 仕様上 CD 非対応 → テンプレを**持たない**
#   - As-Is (7000): 商品固有の理由が必須 → テンプレを**持たない** (AI 生成が必要)
#   - S/A/B/C/D/PO のみテンプレを保持 (全 65 字以内)
#
# 特記 (rank=S、ConditionID 1500): 1500 は **カテゴリ依存** (Consumer Electronics
# 一部で制限、CLAUDE.md「8 段階体系」注記)。canary 実行時は Ack=Failure が返っ
# たら 1000 fallback or 3000 + "Open box" description に降格する判断を残す。
_RANK_CD_TEMPLATES: dict[str, str] = dict(_CANONICAL_RANK_CD_TEMPLATE)

# HIGH-2: rank=N は Brand New (1000) で CD 対象外なので明示 skip する。
# 呼出側の process_one でこの集合を確認して skip reason を出す。
_RANK_SKIP_NO_CD_NEEDED: frozenset[str] = frozenset({"N"})

for _rank, _text in _RANK_CD_TEMPLATES.items():
    if len(_text) > _MAX_CD_LEN:
        raise ValueError(
            f"rank={_rank} の定型文が{_MAX_CD_LEN}字を超過しています "
            f"({len(_text)}字): {_text!r}"
        )


def _load_targets() -> list[str]:
    data = json.loads(_INPUT.read_text(encoding="utf-8"))
    return [d["ebay_item_id"] for d in data if not d.get("condition_description")]


def _load_condition_ranks(item_ids: list[str]) -> dict[str, str | None]:
    if not item_ids:
        return {}
    conn = sqlite3.connect(str(_DB))
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT ebay_item_id, condition_rank FROM ebay_listings "
        f"WHERE ebay_item_id IN ({placeholders})",
        item_ids,
    ).fetchall()
    conn.close()
    return {eid: rank for eid, rank in rows}


def _load_existing(out_path: Path) -> dict:
    if out_path.exists():
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # L1 fix (T3 4巡目): 破損 file の silent 読み飛ばしを warning 出力へ
            print(
                f"WARNING: _load_existing: {out_path.name} 読込失敗 "
                f"({type(e).__name__}: {e}) → 空 dict で継続 (done_ids リセット)",
                file=sys.stderr, flush=True,
            )
    # HIGH-1 fix (T3 Codex): send_failed バケット
    return {"plans": [], "skips": [], "no_action": [], "send_failed": []}


def _derive_ack(exec_result: dict) -> str | None:
    """MED-1 fix (T3 2巡目): revise_item_condition の返却 dict には ack キーが**無い**
    (自前 XML parse 実装、_call_trading_api 未経由)。success から 'Success' を導出。"""
    ack = exec_result.get("ack")
    if ack:
        return ack
    if exec_result.get("success"):
        return "Success (derived from success flag; revise_item_condition dict has no 'ack' key)"
    return None


def _fetch_current_condition(
    item_id: str, creds: dict,
) -> tuple[str | None, str | None, str | None]:
    """Returns (condition_id, condition_description, error)."""
    app, dev, cert, tok = (
        creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
    )
    result = ebay_client._call_trading_api(
        "GetItem", ebay_client._build_get_item_xml(item_id),
        app, dev, cert, tok,
    )
    if not result.get("success") or not result.get("raw"):
        return None, None, result.get("message", "GetItem失敗 (詳細不明)")
    try:
        root = ET.fromstring(result["raw"])
    except ET.ParseError as e:
        return None, None, f"XML parse error: {e}"
    cid = root.findtext(".//n:Item/n:ConditionID", namespaces=_NS)
    cd = root.findtext(".//n:Item/n:ConditionDescription", namespaces=_NS)
    return cid, cd, None


def process_one(
    item_id: str, rank: str | None, creds: dict, execute: bool,
) -> dict:
    if rank is None:
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": "condition_rank が DB に未設定 (NULL) — 自動推定しない",
        }
    if rank in _RANK_SKIP_NO_CD_NEEDED:
        # HIGH-2 fix (T3 review): rank=N (Brand New / ConditionID 1000) は
        # CD 対象外 (中古品のみ CD 必須、CLAUDE.md 準拠)。
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": (
                f"rank={rank} (Brand New / ConditionID 1000) は "
                f"ConditionDescription 対象外 — 中古品 (S/A/B/C/D/PO/As-Is) "
                f"のみ CD 必須 (CLAUDE.md「eBay XML 制約」)"
            ),
        }
    if rank == "As-Is":
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": (
                "As-Is は理由 (why untested 等) 必須のため自動生成不可 "
                "(tools/ebay-manager/CLAUDE.md「As-Is出品のXML必須要件」)"
            ),
        }
    template = _RANK_CD_TEMPLATES.get(rank)
    if template is None:
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": f"未対応の condition_rank: {rank!r}",
        }

    cid, current_cd, err = _fetch_current_condition(item_id, creds)
    if err is not None:
        return {"ebay_item_id": item_id, "status": "skip", "reason": err}
    if not cid:
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": "GetItem で現行ConditionID取得不能 (空)",
        }
    if current_cd and current_cd.strip():
        # スキャン時点は空だったが、既に別経路で設定済み。送信不要。
        return {
            "ebay_item_id": item_id, "status": "no_action_needed",
            "reason": (
                f"現在ConditionDescriptionが既に設定済み (スキャン後に変更): "
                f"{current_cd[:65]!r}"
            ),
        }

    if len(template) > _MAX_CD_LEN:
        raise ValueError(
            f"rank={rank} の定型文が{_MAX_CD_LEN}字を超過しています: {template!r}"
        )

    plan = {
        "ebay_item_id": item_id,
        "status": "plan",
        "condition_rank": rank,
        "current_condition_id": cid,
        "planned_condition_description": template,
    }

    if execute:
        app, dev, cert, tok = (
            creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
        )
        exec_result = ebay_client.revise_item_condition(
            item_id, cid, app, dev, cert, tok,
            condition_description=template,
        )
        plan["execute_result"] = exec_result
        # MED-4 fix (T3 2巡目): batch A/B と同等の revise 後 GetItem read-back verify.
        # M2 fix (T3 4巡目): log_content_change は postverify 後に移動し、success
        # フラグを bool(exec_ok AND verify_ok) で記録する。
        if exec_result.get("success"):
            cid_after, cd_after, err_after = _fetch_current_condition(item_id, creds)
            if err_after is not None:
                plan["postverify"] = {
                    "ok": False,
                    "reason": f"GetItem 再取得失敗: {err_after}",
                }
            else:
                expected = template[:_MAX_CD_LEN]
                actual = (cd_after or "").strip()
                plan["postverify"] = {
                    "ok": actual == expected,
                    "expected_cd": expected,
                    "actual_cd": actual,
                    "cid_after": cid_after,
                }
        # 監査ログを最後に記録 (postverify 結果を反映)
        exec_ok = bool(exec_result.get("success"))
        verify_ok = plan.get("postverify", {}).get("ok", True)
        try:
            log_content_change(
                item_id, "condition_description",
                before_value=current_cd or "",
                after_value=template,
                source_tab="coo_fix_batch_c",
                success=bool(exec_ok and verify_ok),
                ebay_ack=_derive_ack(exec_result),
            )
        except (ValueError, RuntimeError, sqlite3.Error, OSError) as e:
            plan["log_error"] = f"log_content_change 失敗: {type(e).__name__}: {e}"

    return plan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                     help="実際に eBay へ ReviseFixedPriceItem を送信する (既定は dry-run)")
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

    targets = _load_targets()
    ranks = _load_condition_ranks(targets)

    # 既知のギャップ報告 (summary の空欄総数 vs JSON 収録件数)
    gap_note = None
    if _SUMMARY.exists():
        try:
            summary = json.loads(_SUMMARY.read_text(encoding="utf-8"))
            total_empty = summary.get("condition_description_distribution", {}) \
                                  .get("empty_or_none")
            if total_empty is not None and total_empty != len(targets):
                gap_note = (
                    f"scan summary 上の空欄総数={total_empty} だが coo_scan_result "
                    f"JSON 収録分={len(targets)} (差={total_empty - len(targets)}件は "
                    f"COO非該当のため JSON 未収録、本スクリプト対象外)"
                )
        except (json.JSONDecodeError, OSError):
            pass
    if gap_note:
        print(f"NOTE: {gap_note}", flush=True)

    # HIGH-4 fix: mode 別 output path
    out_path = _OUT_EXECUTE if args.execute else _OUT_DRYRUN
    progress_path = _PROGRESS_EXECUTE if args.execute else _PROGRESS_DRYRUN
    print(f"output: {out_path.name}", flush=True)

    if args.limit:
        targets = targets[: args.limit]

    existing = _load_existing(out_path)
    plans = existing.get("plans", [])
    skips = existing.get("skips", [])
    no_action = existing.get("no_action", [])
    send_failed = existing.get("send_failed", [])
    # HIGH-1 fix: send_failed は done_ids に**含めない**。
    done_ids = (
        {p["ebay_item_id"] for p in plans}
        | {s["ebay_item_id"] for s in skips}
        | {n["ebay_item_id"] for n in no_action}
    )
    remaining = [t for t in targets if t not in done_ids]
    total = len(remaining)
    print(f"START batch C: target={len(targets)} already_done={len(done_ids)} "
          f"remaining={total} (retry_from_failed={len(send_failed)})", flush=True)

    # HIGH-4 fix: execute + remaining==0 は loud warning + exit(2)
    if args.execute and total == 0:
        print("!" * 60, flush=True)
        print("!! FATAL: execute 指定なのに remaining=0 (送信対象 0 件)。", flush=True)
        print(
            f"!! done_ids は {out_path.name} から読込。追加送信したい場合は"
            f" out_path を rename か削除してから再実行してください。",
            flush=True,
        )
        print("!" * 60, flush=True)
        sys.exit(2)

    def _flush_c():
        out_path.write_text(
            json.dumps({
                "plans": plans, "skips": skips, "no_action": no_action,
                "send_failed": send_failed, "gap_note": gap_note,
            }, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    for i, item_id in enumerate(remaining, 1):
        rank = ranks.get(item_id)
        result = process_one(item_id, rank, creds, args.execute)
        status = result["status"]
        # HIGH-1/2 fix (T3 Codex): 送信失敗 or postverify.ok=False は send_failed へ。
        if status == "plan" and args.execute:
            exec_result = result.get("execute_result") or {}
            postverify = result.get("postverify") or {}
            exec_ok = bool(exec_result.get("success"))
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
                # M1 fix (T3 4巡目): retry 成功時に旧 send_failed エントリ除去
                send_failed[:] = [
                    x for x in send_failed if x.get("ebay_item_id") != item_id
                ]

        if status == "plan":
            plans.append(result)
        elif status == "no_action_needed":
            no_action.append(result)
        elif status == "send_failed":
            pass
        else:
            skips.append(result)

        # HIGH-3 fix: execute 時は毎件 flush
        flush_now = args.execute or (i % 50 == 0) or (i == total)
        if flush_now:
            _flush_c()
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

    _flush_c()
    print("=" * 60)
    print(f"DONE batch C. plans={len(plans)} skips={len(skips)} "
          f"no_action={len(no_action)} send_failed={len(send_failed)}")
    if gap_note:
        print(f"GAP: {gap_note}")
    print(f"出力: {out_path}")


if __name__ == "__main__":
    main()
