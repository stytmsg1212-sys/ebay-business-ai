"""health-check auto-fix — 検知 finding を修正階層 (Tier) に分類する純関数 (Phase 0).

役割: `run_scheduler_health_check` の戻り値 (= 定時実行ヘルスチェックが見つけた
異常一覧) を入力に、各異常を「どう直すべきか」で 4 階層に振り分ける。

  Tier1 (transient)  … 単純な再実行で直る一過性。未実行 / orphan / 慢性失敗。
                       自動で対象 task を 1 回だけ再実行 (ループガード)。
  Tier2 (code_bug)   … コード修正が要る (subprocess returncode≠0 = codex_lint 型)。
                       Phase 2 で claude -p によるドライラン修正、commit はしない。
  Tier3 (db_write)   … DB 書込で直る (URL乖離 / 監視台帳漏れ)。診断 SELECT を自動実行し
                       修正案を保存 → Discord で user 承認待ち (自動 write しない、Q2)。
  escalate           … 安全な自動対処が無い (監視自体の故障 / 人手要 DLQ / DB lock 多発)。
                       user 判断へ回付するのみ。

設計方針:
- 本 module の `classify_finding` は **純関数** (DB/IO/副作用なし)。分類のみ。
  実際の再実行・診断・提案保存は Phase 1 の orchestrator (`run_health_autofix`) が担う。
- finding_hash は「同一異常の安定識別キー」。件数・時刻など揺れる値は evidence に
  含めない (`health_autofix_log.make_finding_hash` 規約)。per-task 異常は
  target_task_key で、集約異常 (URL乖離等) は kind と固定 source ラベルで識別する。
- 自己エラー (監視 query 自体の失敗) は「監視の監視が沈黙」= 最緊急だが、自動対処は
  危険なので必ず escalate (R-11 / 2026-05-18 Codex HIGH と同方針)。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from monitor.health_autofix_log import (
    count_attempts_today,
    make_finding_hash,
    record_attempt,
    record_db_proposal,
    _open_proposal_id,
)

logger = logging.getLogger(__name__)

# 修正階層。Phase 1 は TIER1/TIER3、Phase 2 で TIER2、ESCALATE は常時 user 回付。
TIER1 = "tier1"        # transient: 単純再実行
TIER2 = "tier2"        # code_bug: ソース修正 (Phase 2 dry-run)
TIER3 = "tier3"        # db_write: DB 書込提案 (承認待ち)
ESCALATE = "escalate"  # 自動対処不能: user 判断


def _classified(
    tier: str,
    kind: str,
    *,
    target_task_key: Optional[str] = None,
    evidence: Any = None,
) -> dict:
    """分類結果 1 件を組み立てる (finding_hash を付与)."""
    return {
        "tier": tier,
        "kind": kind,
        "target_task_key": target_task_key,
        "evidence": evidence,
        "finding_hash": make_finding_hash(kind, target_task_key, evidence),
    }


def classify_finding(health_result: dict) -> list[dict]:
    """ヘルスチェック結果を修正階層別に分類する (純関数).

    入力: `run_scheduler_health_check(config)` の戻り値 dict。
    出力: 分類済み finding のリスト。各要素は
          {tier, kind, target_task_key, evidence, finding_hash}。
    異常 0 件なら空リストを返す。
    """
    out: list[dict] = []
    if not isinstance(health_result, dict):
        return out

    coverage = health_result.get("coverage") or {}
    url_div = health_result.get("url_divergence") or {}
    phase_c = health_result.get("phase_c") or {}

    # --- 自己エラー (監視 query 自体の失敗) → 常に escalate ---
    # 監視の監視が沈黙 = 異常の有無すら不明。自動対処は危険なので user 回付のみ。
    # phase_c.url_divergence_error は url_divergence.divergence_error の複製
    # (task_scheduler_health_check L524-525) なので二重計上せず後段で skip。
    self_errors = [
        ("coverage_compute", coverage.get("coverage_error")),
        ("url_divergence_compute", url_div.get("divergence_error")),
        ("phase_c_db", phase_c.get("db_query_error")),
        ("phase_c_log_scan", phase_c.get("log_scan_error")),
    ]
    for source, err in self_errors:
        if err:
            out.append(_classified(
                ESCALATE, "monitor_self_error",
                target_task_key=source, evidence={"source": source}))

    # --- Tier1: 未実行タスク (期待 slot に成功ログなし) → 再実行 ---
    for m in health_result.get("missed") or []:
        tk = m.get("task_key")
        if tk:
            out.append(_classified(TIER1, "missed_task", target_task_key=tk))

    # --- Tier1: orphan (started のまま 2h+ 未完了 = stuck) → 再実行 ---
    for o in phase_c.get("orphans") or []:
        tk = o.get("task_key")
        if tk:
            out.append(_classified(TIER1, "orphan_task", target_task_key=tk))

    # --- Tier1: 慢性失敗 (24h で 3+ 回 failed) → 1 回だけ再実行 (ループガードで歯止め) ---
    for it in phase_c.get("intermittent") or []:
        tk = it.get("task_key")
        if tk:
            out.append(_classified(
                TIER1, "intermittent_failure", target_task_key=tk))

    # --- Tier2: subprocess returncode≠0 痕跡 (codex_lint 型 = コードバグ) ---
    # 再実行では直らない。Phase 2 で claude -p によるソース修正 (dry-run)。
    for se in phase_c.get("subprocess_errors") or []:
        tk = se.get("task_key")
        if tk:
            out.append(_classified(
                TIER2, "subprocess_error", target_task_key=tk))

    # --- Tier3: URL乖離 (listing.source_url ≠ monitored.source_url) → DB 書込提案 ---
    # divergence_count==-1 は self-error として上で escalate 済 (ここでは正の件数のみ)。
    if (url_div.get("divergence_count") or 0) > 0:
        out.append(_classified(TIER3, "url_divergence"))

    # --- Tier3: 監視台帳漏れ (active 無在庫が monitored_items に未登録) → DB 書込提案 ---
    # coverable>0 は ensure_monitor_coverage 不全なので同 task 再実行は無効。
    # 不足分を登録する DB write 提案 (承認待ち) が即時の正しい remediation。
    # coverable==-1 は self-error (上で escalate 済)。
    if (coverage.get("coverable") or 0) > 0:
        out.append(_classified(TIER3, "coverage_gap"))

    # --- escalate: DLQ (URL生成不能 / site_config 未登録) = 人手登録要 ---
    if (coverage.get("dlq") or 0) > 0:
        out.append(_classified(ESCALATE, "coverage_dlq"))

    # --- escalate: DB lock spike (1h で 3+ 回) = 並行 write 競合、安全な自動対処なし ---
    if (phase_c.get("db_locks") or 0) >= 3:
        out.append(_classified(ESCALATE, "db_lock_spike"))

    return out


# ──────────────────────────────────────────────────────────────────────
# Phase 1 orchestrator: 分類 → Tier 別自動対処.
#
# 設計方針 (本 module の最重要部):
# - run_scheduler_health_check の戻り値を **同じ object のまま** 受け取り
#   (daily_scheduler._run_health_check 側で注入)、再度ヘルスチェックを呼ばない。
#   別 cron で再呼出すると coverable/phase_c の非 dedupe alert が 2 重発火する。
# - Tier1 は missed_task のみ自動再実行 (_AUTORERUN_KINDS)。orphan/intermittent は
#   二重実行リスク・慢性バグの懸念から Phase 1 では記録のみ (escalate)。
# - Tier3 (URL乖離 / 監視台帳漏れ) は READ-ONLY 診断 + DB 書込提案保存 + 承認待ち
#   通知のみ。自動 write はしない (Q2 / user 決定「DB変更=書込は承認」)。
# - ループガード: 同一 finding は JST 当日 1 回だけ対処 (count_attempts_today)。
# - 通知: 「本日初」のアクション (再実行 / 新規提案 / 新規 escalate) がある時のみ
#   要約 1 embed を送る。健康チェック本体の検知 alert と二重通知しない。
# ──────────────────────────────────────────────────────────────────────

# Tier1 missed_task で再実行する task の dispatch (task_key → (module_path, func_name)).
# 単純な func(config) で再実行できるものに限定。CDP Chrome 依存
# (market_analysis_refresh) / subprocess 系 / health_check 自身は **含めない**
# (= dispatch 未定義 → escalate して人手に回す、暴走再実行を避ける)。
_RERUN_DISPATCH: dict[str, tuple[str, str]] = {
    "company_secretary": ("tasks.task_company_secretary", "run_company_secretary"),
    "ebay_sync": ("tasks.task_ebay_sync", "run_ebay_sync"),
    "ensure_monitor_coverage": ("tasks.task_ensure_monitor_coverage", "run_ensure_monitor_coverage"),
    "inventory_check": ("tasks.task_inventory_check", "run_inventory_check"),
    "inventory_alert": ("tasks.task_inventory_alert", "run_inventory_alert"),
    "supplier_select": ("tasks.task_supplier_select", "run_supplier_select"),
    "supplier_sweep": ("tasks.task_supplier_sweep", "run_supplier_sweep"),
    "enrich_listings_physical": ("tasks.task_enrich_listings_physical", "run_enrich_listings_physical"),
    "estimate_weights_claude": ("tasks.task_estimate_weights_claude", "run_estimate_weights_claude"),
    "daily_relist": ("tasks.task_daily_relist", "run_daily_relist"),
    "cleanup_old_relisted": ("tasks.task_cleanup_old_relisted", "run_cleanup_old_relisted"),
    "video_learning_queue": ("tasks.task_video_learning", "run_video_learning_queue"),
    "research_morning_brief": ("tasks.task_research_morning_brief", "run_research_morning_brief"),
    "email_pickup": ("tasks.task_email_pickup", "run_email_pickup"),
    "rival_detection": ("tasks.task_rival_detection", "run_rival_detection"),
    "data_sync": ("tasks.task_sync_data_stores", "run_sync_data_stores"),
    "price_optimization": ("tasks.task_price_optimization", "run_price_optimization"),
    "fuel_surcharge_check": ("tasks.task_fuel_surcharge_check", "run_fuel_surcharge_check"),
    "news_check": ("tasks.task_news_check", "run_news_check"),
    "customs_check": ("tasks.task_customs_check", "run_customs_check"),
    "budget_alert": ("tasks.task_budget_alert", "run_budget_alert"),
    "rival_pricing_refresh": ("tasks.task_rival_pricing", "run_rival_pricing_refresh"),
    "morning_discovery": ("tasks.task_morning_discovery", "run_morning_discovery"),
    # daily_codex_lint は **意図的に除外** (Codex CLI を subprocess 起動 = 上記方針の
    # 「subprocess 系は含めない」+ 再実行で GPT-5.5 API コスト発生)。missed 時は
    # dispatch 未定義 → escalate して user 判断に回す。
}

# Tier1 のうち自動再実行する kind。Phase 1 は missed_task のみ。
_AUTORERUN_KINDS = frozenset({"missed_task"})

_LOOP_GUARD_MAX = 1  # 同一 finding は JST 当日 1 回だけ対処

_KIND_LABELS = {
    "url_divergence": "URL乖離",
    "coverage_gap": "監視台帳漏れ",
    "orphan_task": "orphan (2h+未完了)",
    "intermittent_failure": "慢性失敗 (24h 3+回)",
    "subprocess_error": "subprocess 失敗 (codex_lint 型)",
    "monitor_self_error": "監視 query 自体の失敗",
    "coverage_dlq": "URL生成不能 (DLQ)",
    "db_lock_spike": "DB lock 多発",
}


def run_health_autofix(config: dict, health_result: dict) -> dict:
    """ヘルスチェック結果を受けて Tier 別に自動対処する (Phase 1).

    呼び出しは daily_scheduler._run_health_check 内、run_scheduler_health_check の
    **戻り値をそのまま渡す** こと (再度ヘルスチェックを呼ばない = 非 dedupe alert
    の二重発火を防ぐ)。返り値はアクションの要約 dict。
    """
    summary: dict[str, Any] = {
        "classified": 0, "reran": [], "rerun_failed": [],
        "proposed": [], "escalated": [], "skipped": [], "errors": [],
        "fix_dryrun": [], "notified": False,
    }
    findings = classify_finding(health_result)
    summary["classified"] = len(findings)
    if not findings:
        return summary

    # finding_hash で dedupe (同 task が複数 slot で missed → 1 件に集約)。
    seen: set[str] = set()
    unique: list[dict] = []
    for f in findings:
        if f["finding_hash"] in seen:
            continue
        seen.add(f["finding_hash"])
        unique.append(f)

    new_actions: list[tuple[str, str]] = []  # Discord 要約に載せる「本日初」アクション
    fix_diffs: list[dict] = []  # Tier2 proposed の diff 本文 (別メッセージで post)
    for f in unique:
        try:
            kind, tier = f["kind"], f["tier"]
            if kind in _AUTORERUN_KINDS:
                _handle_tier1_rerun(config, f, summary, new_actions)
            elif tier == TIER2 and _tier2_dryrun_enabled(config):
                _handle_tier2_fix_dryrun(
                    config, f, summary, new_actions, fix_diffs, health_result)
            elif tier == TIER3:
                _handle_tier3_proposal(config, f, summary, new_actions)
            else:
                _handle_escalate(f, summary, new_actions)
        except Exception as e:  # noqa: BLE001 — 1 件の失敗で全体を止めない (Q0: 記録)
            logger.error(
                f"autofix 処理失敗 (kind={f.get('kind')}, "
                f"hash={f.get('finding_hash')}): {e}", exc_info=True)
            summary["errors"].append({"kind": f.get("kind"), "error": str(e)})

    if new_actions:
        summary["notified"] = _notify_autofix_summary(config, new_actions, fix_diffs)

    logger.info(
        f"health autofix: classified={summary['classified']} "
        f"reran={len(summary['reran'])} rerun_failed={len(summary['rerun_failed'])} "
        f"proposed={len(summary['proposed'])} escalated={len(summary['escalated'])} "
        f"fix_dryrun={len(summary['fix_dryrun'])} "
        f"skipped={len(summary['skipped'])} notified={summary['notified']}"
    )
    return summary


def _handle_tier1_rerun(config: dict, f: dict, summary: dict,
                        new_actions: list) -> None:
    """Tier1 missed_task: killswitch + ループガード後に対象 task を 1 回再実行."""
    fh, kind, tk = f["finding_hash"], f["kind"], f["target_task_key"]
    # ループガード: JST 当日に既に対処済なら再実行しない (暴走防止)。
    if count_attempts_today(fh) >= _LOOP_GUARD_MAX:
        record_attempt(fh, TIER1, kind, "rerun", "skipped",
                       target_task_key=tk, detail="loop guard: 当日対処済")
        summary["skipped"].append({"task_key": tk, "reason": "loop_guard"})
        return
    # killswitch: user が無効化した task は再実行しない (意図の尊重)。
    if not _task_enabled(config, tk):
        record_attempt(fh, TIER1, kind, "rerun", "skipped",
                       target_task_key=tk,
                       detail="killswitch: tasks_enabled.enabled=False")
        summary["skipped"].append({"task_key": tk, "reason": "killswitch"})
        return
    # dispatch 未定義 (CDP/subprocess 依存等) → 暴走させず escalate (人手)。
    disp = _RERUN_DISPATCH.get(tk)
    if disp is None:
        record_attempt(fh, TIER1, kind, "rerun", "escalated",
                       target_task_key=tk,
                       detail="再実行 dispatch 未定義 (CDP/subprocess 等、要人手)")
        summary["escalated"].append({"task_key": tk, "reason": "no_dispatch"})
        new_actions.append(("escalate", f"`{tk}`: 自動再実行 dispatch 未定義 (要人手)"))
        return
    # 二重実行ガード: 当日すでに completed(success=1) または in-flight(started/NULL) なら
    # 再実行しない (daily_relist 14件/日 / supplier_sweep 並走二重 の本丸修正)。
    # is_completed_or_running_today は task_execution_log モジュールのヘルパー。
    # _display_for と同様に循環 import 回避のため lazy import する。
    from monitor.task_execution_log import is_completed_or_running_today
    if is_completed_or_running_today(tk):
        record_attempt(fh, TIER1, kind, "rerun", "skipped",
                       target_task_key=tk,
                       detail="already completed/in-flight today")
        summary["skipped"].append(
            {"task_key": tk, "reason": "already_completed_or_inflight"})
        return

    module_path, func_name = disp
    display = _display_for(tk)
    logger.warning(f"[autofix] Tier1 再実行: {tk} ({module_path}.{func_name})")
    result = _rerun_task(display, module_path, func_name, config, tk)
    # L1 fix: run が skipped:True を返した場合 (Layer2 run-once guard または Layer3 lock-held skip)。
    # _result_ok は success=True を返すため "resolved" として誤記録されるのを防ぐ。
    if isinstance(result, dict) and result.get("skipped"):
        record_attempt(fh, TIER1, kind, "rerun", "skipped",
                       target_task_key=tk,
                       detail=f"run skipped: {result.get('reason', '')}")
        summary["skipped"].append({"task_key": tk, "reason": "rerun_skipped"})
        return
    # 解消判定は **再実行の結果フラグ** で行う (find_missed_tasks を再 query しない)。
    # 理由: 再実行は health-check の batch_hour で execution_log に記録されるが、
    # find_missed の slot 窓は対象 task の expected slot 基準。各 slot の missed は
    # 「その slot 直後の最初の health-check」(slot2→04時/窓[2,10]、slot11→12時/窓[11,14]
    # …) で検知・再実行され batch_hour は窓内に入る。loop guard で後続 cron の
    # 再実行を抑止するため「窓外 batch_hour で再 missed」となる境界は実質発生しない。
    ok = _result_ok(result)
    if ok:
        record_attempt(fh, TIER1, kind, "rerun", "resolved",
                       target_task_key=tk, detail=_short(result))
        summary["reran"].append({"task_key": tk})
        new_actions.append(("rerun_ok", f"{display}: 自動再実行で解消"))
    else:
        record_attempt(fh, TIER1, kind, "rerun", "attempted",
                       target_task_key=tk, detail=_short(result))
        summary["rerun_failed"].append({"task_key": tk})
        new_actions.append(("rerun_fail", f"{display}: 自動再実行したが失敗 (要確認)"))


def _handle_tier3_proposal(config: dict, f: dict, summary: dict,
                           new_actions: list) -> None:
    """Tier3: READ-ONLY 診断 + DB 書込提案保存 + 新規時のみ承認待ち通知 (自動 write しない)."""
    fh, kind = f["finding_hash"], f["kind"]
    before = _open_proposal_id(fh)  # 既存 pending 提案があれば再通知しない
    if kind == "url_divergence":
        diag = _diagnose_url_divergence()
        proposed = ("scripts/cleanup_url_divergence_2026_05_26.py --apply "
                    "(各 listing の SKU を実 source_url に整合 → "
                    "update_ebay_listing_sku で monitored_items へ cascade)")
    elif kind == "coverage_gap":
        diag = _diagnose_coverage_gap()
        proposed = ("ensure_monitor_coverage 再実行 / 未登録 active 無在庫を "
                    "upsert_item で monitored_items に登録")
    else:
        record_attempt(fh, TIER3, kind, "propose", "escalated",
                       detail="未対応の Tier3 kind (要人手)")
        summary["escalated"].append({"kind": kind, "reason": "unknown_tier3"})
        new_actions.append(("escalate", f"{kind}: 未対応の DB 提案種別 (要人手)"))
        return
    pid = record_db_proposal(
        fh, kind, proposed,
        diagnosis_sql=diag["sql"], diagnosis_result=diag["rows"],
        affected_rows_est=diag["count"])
    if before is None:  # 新規提案のみ記録 + 通知
        record_attempt(fh, TIER3, kind, "propose", "proposed",
                       detail=f"proposal_id={pid} 件数={diag['count']}")
        summary["proposed"].append(
            {"kind": kind, "proposal_id": pid, "count": diag["count"]})
        new_actions.append(
            ("proposal",
             f"{_kind_label(kind)} {diag['count']} 件: DB 修正案を保存、承認待ち"))
    else:
        summary["skipped"].append({"kind": kind, "reason": "proposal_pending"})


def _handle_tier2_fix_dryrun(config: dict, f: dict, summary: dict,
                             new_actions: list, fix_diffs: list,
                             health_result: dict) -> None:
    """Tier2 (subprocess コードバグ): claude に修正案を diff で出させ gate 検証のみ.

    **commit / 本番 tree への適用は一切しない** (ドライラン)。verdict 別に記録 +
    通知。proposed の diff 本文は別メッセージ (fix_diffs) で post する。
    config `health_autofix.tier2_dryrun_enabled=true` の時のみ呼ばれる (killswitch)。
    """
    fh, kind, tk = f["finding_hash"], f["kind"], f["target_task_key"]
    # ループガード: JST 当日に既に対処済なら claude を再起動しない (稀 + コスト)。
    if count_attempts_today(fh) >= _LOOP_GUARD_MAX:
        record_attempt(fh, TIER2, kind, "fix_dryrun", "skipped",
                       target_task_key=tk, detail="loop guard: 当日対処済")
        summary["skipped"].append({"task_key": tk, "reason": "loop_guard"})
        return
    # killswitch: user が無効化した task は修正案も作らない (意図の尊重)。
    if not _task_enabled(config, tk):
        record_attempt(fh, TIER2, kind, "fix_dryrun", "skipped",
                       target_task_key=tk,
                       detail="killswitch: tasks_enabled.enabled=False")
        summary["skipped"].append({"task_key": tk, "reason": "killswitch"})
        return
    # subprocess エラーメッセージを health_result から引く (classify 元と同じ source)。
    error_message = _subprocess_error_message(health_result, tk)
    if not error_message:
        record_attempt(fh, TIER2, kind, "fix_dryrun", "escalated",
                       target_task_key=tk,
                       detail="subprocess エラーメッセージ取得不能 (要人手)")
        summary["escalated"].append({"task_key": tk, "reason": "no_error_message"})
        new_actions.append(("escalate", f"`{tk}`: subprocess エラー詳細不明 (要人手)"))
        return

    from monitor import health_fixer
    logger.warning(f"[autofix] Tier2 修正案ドライラン: {tk}")
    proposal = health_fixer.propose_fix(tk, error_message, config=config)

    # verdict → autofix_attempt_log の status 語彙へ対応付け。
    status_map = {"proposed": "proposed", "gate_failed": "gate_failed",
                  "escalated": "escalated", "error": "aborted"}
    status = status_map.get(proposal.verdict, "aborted")
    detail = json.dumps({
        "verdict": proposal.verdict, "reason": proposal.reason,
        "diff_path": proposal.diff_path, "changed_lines": proposal.changed_lines,
        "touched_files": proposal.touched_files,
        "duration_ms": proposal.duration_ms,
    }, ensure_ascii=False)
    record_attempt(fh, TIER2, kind, "fix_dryrun", status,
                   target_task_key=tk, gate_report=proposal.gates or None,
                   detail=detail)
    summary["fix_dryrun"].append({"task_key": tk, "verdict": proposal.verdict,
                                  "diff_path": proposal.diff_path})

    display = _display_for(tk)
    if proposal.verdict == "proposed":
        new_actions.append((
            "fix_dryrun",
            f"{display}: 修正案あり ({proposal.changed_lines}行/"
            f"{len(proposal.touched_files)}ファイル) `{proposal.diff_path}`"))
        fix_diffs.append({"task_key": tk, "display": display,
                          "diff": proposal.diff, "diff_path": proposal.diff_path})
    else:
        new_actions.append((
            "fix_dryrun",
            f"{display}: {proposal.verdict} — {proposal.reason[:120]}"))


def _handle_escalate(f: dict, summary: dict, new_actions: list) -> None:
    """自動対処不能 (orphan/intermittent/subprocess/monitor_self_error 等): 記録のみ.

    本日初回のみ記録 + 通知 (5x/日 spam 回避)。継続可視化は健康チェック本体の
    検知 alert が担う。
    """
    fh, tier, kind = f["finding_hash"], f["tier"], f["kind"]
    tk = f.get("target_task_key")
    if count_attempts_today(fh) >= 1:
        summary["skipped"].append({"kind": kind, "reason": "already_escalated_today"})
        return
    record_attempt(fh, tier, kind, "escalate", "escalated",
                   target_task_key=tk, detail=f"自動対処不能 (kind={kind})")
    summary["escalated"].append({"kind": kind, "task_key": tk})
    new_actions.append(("escalate", _escalate_label(kind, tk)))


# ---------- helpers ----------

def _tier2_dryrun_enabled(config: dict) -> bool:
    """Phase 2 段階フラグ (既定 False = killswitch).

    config `health_autofix.tier2_dryrun_enabled` が明示的に True の時だけ Tier2
    修正案ドライランを発火させる。未設定/False の間は従来通り escalate (記録のみ)。
    """
    ha = config.get("health_autofix") or {}
    return bool(ha.get("tier2_dryrun_enabled", False))


def _subprocess_error_message(health_result: dict, task_key: str) -> str:
    """health_result.phase_c.subprocess_errors から task_key 一致の message を引く."""
    phase_c = (health_result or {}).get("phase_c") or {}
    for se in phase_c.get("subprocess_errors") or []:
        if se.get("task_key") == task_key:
            return str(se.get("message") or "").strip()
    return ""


def _task_enabled(config: dict, task_key: str) -> bool:
    """config tasks_enabled.<task_key> の有効判定 (killswitch).

    should_task_run の enabled 判定のみを抜き出したもの (execution_times /
    weekday は missed = 既に本日 expected 確定なので再評価しない)。
    """
    tc = (config.get("tasks_enabled") or {}).get(task_key)
    if tc is None:
        return True
    if isinstance(tc, bool):
        return tc
    if isinstance(tc, dict):
        return bool(tc.get("enabled", True))
    return bool(tc)


def _rerun_task(display: str, module_path: str, func_name: str,
                config: dict, task_key: str) -> Any:
    """対象 task を 1 回再実行 (task_execution_log へ記録、import 失敗も安全化).

    daily_scheduler.safe_import_and_run を流用 (run_task 経由で execution_log を
    記録 = find_missed_tasks が解消を検知できる)。lazy import で循環 import 回避。
    """
    from daily_scheduler import safe_import_and_run
    return safe_import_and_run(
        display, module_path, func_name, config,
        max_retries=1, retry_delay=0, task_key=task_key)


def _result_ok(result: Any) -> bool:
    """再実行結果を success 判定 (run_task と同じ規約: dict は success キー既定 True)."""
    if isinstance(result, dict):
        return bool(result.get("success", True))
    return bool(result)


def _diagnose_url_divergence() -> dict:
    """listing.source_url ≠ monitored.source_url の listing を READ-ONLY 抽出.

    検出 SQL は task_scheduler_health_check._check_url_divergence と同一
    (ebay_item_id 結合 / GLOB 'ebay*' / active 同士 / DISTINCT)。
    """
    sql = """
        SELECT DISTINCT l.ebay_item_id, l.sku, l.title,
               l.source_url AS listing_url,
               m.source_url AS monitored_url
          FROM ebay_listings l
          JOIN monitored_items m
            ON m.ebay_item_id = l.ebay_item_id
           AND m.ebay_item_id IS NOT NULL
           AND m.ebay_item_id <> ''
         WHERE COALESCE(l.is_ended, 0) = 0
           AND (l.quantity_ebay IS NULL OR l.quantity_ebay >= 1)
           AND l.sku GLOB 'ebay*'
           AND l.source_url IS NOT NULL AND l.source_url <> ''
           AND m.source_url IS NOT NULL AND m.source_url <> ''
           AND COALESCE(m.is_active, 1) = 1
           AND l.source_url <> m.source_url
         ORDER BY l.ebay_item_id
    """
    from monitor.database import get_conn
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
    return {"sql": sql.strip(), "rows": rows[:50], "count": len(rows)}


def _diagnose_coverage_gap() -> dict:
    """active 無在庫で monitored_items 未登録の listing を READ-ONLY 抽出."""
    from tasks.task_ensure_monitor_coverage import find_coverage_gaps
    gaps = find_coverage_gaps()
    coverable = gaps.get("coverable") or []
    rows = [{"ebay_item_id": c.get("ebay_item_id"), "sku": c.get("sku"),
             "title": c.get("title")} for c in coverable[:50]]
    return {"sql": "tasks.task_ensure_monitor_coverage.find_coverage_gaps()",
            "rows": rows, "count": len(coverable)}


def _display_for(task_key: str) -> str:
    from monitor.task_execution_log import TASK_SCHEDULE_BY_KEY
    t = TASK_SCHEDULE_BY_KEY.get(task_key)
    return (t.get("display") if t else None) or task_key


def _short(result: Any) -> str:
    try:
        return json.dumps(result, ensure_ascii=False, default=str)[:500]
    except (TypeError, ValueError):
        return str(result)[:500]


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


def _escalate_label(kind: str, task_key: Optional[str]) -> str:
    base = _kind_label(kind)
    if kind == "monitor_self_error" and task_key:
        return f"{base}: source={task_key} (監視の監視沈黙 = 最緊急)"
    if task_key and not task_key.startswith("__"):
        return f"{base}: `{task_key}` (要人手)"
    return f"{base} (要人手)"


def _notify_autofix_summary(config: dict, new_actions: list,
                            fix_diffs: Optional[list] = None) -> bool:
    """本日初のアクション (再実行 / 新規提案 / 新規 escalate / 修正案ドライラン) を通知.

    要約 1 embed を送り、Tier2 proposed の diff 本文は **別メッセージ** で post する
    (commit/適用はしておらず人間レビュー用)。

    依頼ボード#39 S2 (2026-07-03): 要約 embed の実送信は
    record_and_maybe_send("system", ...) に一本化 (notification_log へ必ず記録 + config
    discord_category_gate に従いDiscord送信要否を判定)。Tier2 diff 本文
    (_post_fix_diff_message) は別メッセージのため choke point の対象外 (scope 外、
    従来通り httpx で webhook_url に直接 post)。
    """
    rerun_ok = [m for t, m in new_actions if t == "rerun_ok"]
    rerun_fail = [m for t, m in new_actions if t == "rerun_fail"]
    proposals = [m for t, m in new_actions if t == "proposal"]
    escalations = [m for t, m in new_actions if t == "escalate"]
    fix_dryruns = [m for t, m in new_actions if t == "fix_dryrun"]
    fields = []
    if rerun_ok:
        fields.append({"name": f"[自動修復] 再実行で解消 {len(rerun_ok)} 件",
                       "value": "\n".join(rerun_ok[:10]), "inline": False})
    if rerun_fail:
        fields.append({"name": f"[要確認] 再実行したが失敗 {len(rerun_fail)} 件",
                       "value": "\n".join(rerun_fail[:10]), "inline": False})
    if proposals:
        fields.append({"name": f"[承認待ち] DB 修正案 {len(proposals)} 件",
                       "value": "\n".join(proposals[:10])
                       + "\nMonoDeck で内容を確認し承認してください。",
                       "inline": False})
    if fix_dryruns:
        fields.append({"name": f"[修正案ドライラン] {len(fix_dryruns)} 件",
                       "value": "\n".join(fix_dryruns[:10])
                       + "\n※diff は未適用。レビュー後に手動適用してください。",
                       "inline": False})
    if escalations:
        fields.append({"name": f"[要人手] 自動対処不能 {len(escalations)} 件",
                       "value": "\n".join(escalations[:10]), "inline": False})
    if not fields:
        return False
    has_problem = bool(rerun_fail or escalations)
    embed = {
        "title": "[Auto-Fix] ヘルスチェック自動対処レポート",
        "description": ("定時実行ヘルスチェックの検知結果に自動対処しました "
                        "(Tier1=再実行 / Tier2=修正案ドライラン / Tier3=DB修正案承認待ち)。"),
        "color": 0xD84C38 if has_problem else 0x6AA84F,  # 問題残=赤 / 全解消=緑
        "timestamp": datetime.now().isoformat(),
        "fields": fields,
    }
    from notifiers.notification_center import record_and_maybe_send
    result = record_and_maybe_send(
        "system", "critical" if has_problem else "info",
        embed["title"], embed["description"], link_target="system", embed=embed,
    )
    ok = bool(result.get("discord_sent"))

    # Tier2 proposed の diff 本文を別メッセージで post (best-effort、ok 判定は変えない)。
    # choke point (record_and_maybe_send) の対象外 (診断 diff の補足メッセージのため、
    # 要約 embed とは別に webhook へ直接 post する従来経路を維持、K2 scope 限定)。
    if fix_diffs:
        try:
            from tasks.task_scheduler_health_check import _resolve_webhook_url
            webhook_url = _resolve_webhook_url(config)
        except Exception as e:  # noqa: BLE001
            logger.error(f"autofix diff post 用 webhook 解決失敗: {e}")
            webhook_url = ""
        if webhook_url:
            try:
                import httpx
            except ImportError as e:  # noqa: BLE001
                logger.error(f"httpx import 失敗 (diff post): {e}")
                httpx = None
            if httpx is not None:
                for fd in fix_diffs:
                    _post_fix_diff_message(webhook_url, httpx, fd)
    return ok


def _post_fix_diff_message(webhook_url: str, httpx, fd: dict) -> None:
    """proposed diff 1 件を ```diff fenced block で post (≤1800字、超は truncate)."""
    diff = fd.get("diff") or ""
    max_body = 1800
    truncated = diff[:max_body]
    note = "" if len(diff) <= max_body else f"\n(以下省略。全文は `{fd.get('diff_path')}`)"
    content = (
        f"修正案 diff — **{fd.get('display')}** (全 gate pass、未適用)\n"
        f"```diff\n{truncated}\n```{note}"
    )
    try:
        httpx.post(webhook_url, json={"content": content[:1990]}, timeout=10.0)
    except (httpx.HTTPError, OSError) as e:  # noqa: BLE001
        logger.error(f"autofix diff メッセージ送信エラー: {e}")
