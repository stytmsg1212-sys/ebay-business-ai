"""Phase 9 オーケストレータ (設計 v2 §5 / Codex F3,F4)。

実行フェーズ (部分適用の根絶):
  preflight_all → diff_all → guard_all → snapshot_all → apply_all → verify_all
preflight / guard が 1 件でも失敗 → 10 table 全て未更新 (auto)。
mutation 後の失敗 → PARTIAL_APPLIED で即 alert、再実行は snapshot/run_id recovery のみ。

mode:
  dry_run : updateShippingCost を一切呼ばない。diff を計算し Discord/DB に記録。
  auto    : 全ゲート green の時のみ snapshot→apply→verify。1つでも未充足なら dry_run に降格。

横断原則: ログできない (ensure_tables 失敗) なら更新しない / 通知できないなら auto しない。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from . import compute, config, db, ebay_api, fetch_fx, fuel, manifest, parse_base_rates

logger = logging.getLogger(__name__)


def _run_id(now: datetime) -> str:
    return "rtb_" + now.strftime("%Y%m%dT%H%M%S")


def _load_schedule_config() -> dict:
    try:
        with open(config.SCHEDULE_CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _notify(message: str) -> bool:
    """Discord('pricing') 送信。戻り = 送信できたか (R-11: 受信 signal にすぎない)。"""
    try:
        from notifiers.discord_notifier import notifier_for
        return bool(notifier_for(config.DISCORD_CATEGORY).send_message(message))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Discord 送信失敗 (rate table batch): {e}")
        return False


def run_batch(sched_config: dict | None = None, today=None, force_mode: str | None = None) -> dict:
    """月次バッチ本体。戻り = 結果サマリ dict。

    Args:
        sched_config: schedule_config.json の内容 (省略時ファイルから読む)。
        today: 基準日 (テスト注入)。
        force_mode: "dry_run" / "auto" を明示 (省略時 config.rate_table_batch.mode、既定 dry_run)。
    """
    now = datetime.now()
    run_id = _run_id(now)
    sched_config = sched_config if sched_config is not None else _load_schedule_config()
    batch_cfg = (sched_config.get("rate_table_batch") or {})
    requested_mode = force_mode or batch_cfg.get("mode", "dry_run")

    # 横断原則: ログできないなら更新しない。ensure_tables を最初に通す。
    try:
        db.ensure_tables()
    except Exception as e:  # noqa: BLE001
        msg = f":rotating_light: 送料月次バッチ 中止: 監査テーブル作成失敗 = ログ不能のため更新しない ({e})"
        logger.error(msg)
        _notify(msg)
        return {"run_id": run_id, "outcome": "aborted", "reason": "ensure_tables_failed"}

    auto_blockers: list[str] = []
    warnings: list[str] = []

    # === INPUTS ===
    fx_res = fetch_fx.fetch_prev_month_fx(today)
    try:
        fetch_fx.save_fx_state(fx_res)
    except Exception as e:  # noqa: BLE001 - 監査補助。失敗してもバッチ本体は継続。
        logger.warning(f"FX state 保存失敗 (監査のみ、継続): {e}")
    fuel_res = fuel.load_rate_table_fuel(now=now)
    base_res = parse_base_rates.load_base_rates()
    zone_defs = manifest.load_zone_definitions()
    zone_numbers = sorted(zone_defs.keys())

    fx = fx_res["fx"]
    if not fx_res["ok"]:
        auto_blockers.append("FX: " + "; ".join(fx_res["errors"]))
        # dry-run は last_good FX で表示計算を試みる
        lg = _last_good_fx()
        if fx is None and lg is not None:
            fx = lg
            warnings.append(f"FX 取得失敗 → dry-run 表示に last_good FX={lg} を使用")
    if not fuel_res["auto_allowed"]:
        auto_blockers.append("fuel: " + ("; ".join(fuel_res["errors"]) or "未設定/stale"))
    warnings.extend(fuel_res["warnings"])
    if not base_res["fresh"]:
        auto_blockers.append("base_rates: PDF パース不可でキャッシュ使用 (cache 時は dry-run 限定)")
        warnings.extend(base_res["warnings"])

    if fx is None:
        msg = ":rotating_light: 送料月次バッチ 中止: FX 不能 (取得失敗かつ last_good 無し) で計算不能。"
        logger.error(msg)
        _notify(msg)
        db.insert_run(run_id, requested_mode, "aborted",
                      _inputs(fx_res, fuel_res, base_res), {"warnings": warnings, "error": "no_fx"})
        return {"run_id": run_id, "outcome": "aborted", "reason": "no_fx"}

    # auto 要求でも blocker あれば dry_run に降格 (fail-closed)
    effective_mode = "auto" if (requested_mode == "auto" and not auto_blockers) else "dry_run"

    # === 計算 ===
    computed = compute.compute_band_zone_table(
        base_res["base_rates"], zone_numbers,
        fuel_res["dhl_pct"], fuel_res["fedex_pct"], float(fx), config.BANDS,
    )

    iso_map = manifest._country_iso_map()

    def _match(rows):
        return manifest.match_live_rows_to_zones(rows, zone_defs, iso_map)

    # === PREFLIGHT_ALL (read-only, bijection) ===
    preflight: dict[str, dict] = {}
    preflight_errors: list[str] = []
    token = None
    try:
        token = ebay_api._token()
    except Exception as e:  # noqa: BLE001
        msg = f":rotating_light: 送料月次バッチ 中止: OAuth token 取得失敗 ({e})"
        logger.error(msg)
        _notify(msg)
        db.insert_run(run_id, effective_mode, "aborted",
                      _inputs(fx_res, fuel_res, base_res), {"warnings": warnings, "error": "no_token"})
        return {"run_id": run_id, "outcome": "aborted", "reason": "no_token"}

    for band, _ in config.BANDS:
        table_id = config.BAND_TO_TABLE[band]
        try:
            data = ebay_api.get_rate_table(table_id, token=token)
        except Exception as e:  # noqa: BLE001
            preflight_errors.append(f"{band}({table_id}): getRateTable 失敗 {e}")
            continue
        matched = _match(data.get("rates", []))
        if not matched["ok"]:
            preflight_errors.append(f"{band}({table_id}): bijection 失敗 " + "; ".join(matched["errors"]))
            continue
        preflight[band] = {"table_id": table_id, "zone_to_rate": matched["zone_to_rate"],
                           "zone_to_old": matched["zone_to_old_usd"]}

    if preflight_errors:
        msg = (":rotating_light: 送料月次バッチ 中止 (preflight): "
               f"{len(preflight_errors)} 件の整合エラー → 全 table 未更新。\n" + "\n".join(preflight_errors[:8]))
        logger.error(msg)
        _notify(msg)
        db.insert_run(run_id, effective_mode, "aborted",
                      _inputs(fx_res, fuel_res, base_res),
                      {"warnings": warnings, "preflight_errors": preflight_errors})
        return {"run_id": run_id, "outcome": "aborted", "reason": "preflight", "errors": preflight_errors}

    # === DIFF_ALL + GUARD_ALL ===
    details: list[dict] = []          # 全 rate の old/new (DB 明細)
    guard_fires: list[str] = []
    changes_by_table: dict[str, list[dict]] = {}   # table_id -> [{rateId, usd, zone, old}]

    for band, _ in config.BANDS:
        info = preflight[band]
        table_id = info["table_id"]
        changes_by_table[table_id] = []
        for z in zone_numbers:
            rate_id = info["zone_to_rate"][z]
            old = info["zone_to_old"].get(z)
            new = computed[band][z]
            within, reason = config.variation_threshold(old if old is not None else 0, new)
            if not within:
                guard_fires.append(f"{band} z{z} (rate {rate_id}): {reason} (${old}→${new})")
            details.append({"table_id": table_id, "band": band, "zone": z, "rate_id": rate_id,
                            "old_usd": old, "new_usd": new,
                            "action": "dryrun", "note": reason})
            if old != new:
                changes_by_table[table_id].append({"rateId": rate_id, "usd": new, "zone": z, "old": old})

    n_changes = sum(len(v) for v in changes_by_table.values())

    # auto かつ guard 発火 → 全 table 未更新 (F3)
    if effective_mode == "auto" and guard_fires:
        effective_mode = "dry_run"
        auto_blockers.append(f"guard 発火 {len(guard_fires)} 件 → auto 中止 (dry-run 降格)")

    # === dry_run ならここで終了 (記録 + 通知) ===
    if effective_mode == "dry_run":
        db.insert_details(run_id, details)
        db.insert_run(run_id, "dry_run", "ok" if not guard_fires else "held",
                      _inputs(fx_res, fuel_res, base_res),
                      {"warnings": warnings, "auto_blockers": auto_blockers,
                       "guard_fires": guard_fires, "n_changes": n_changes,
                       "fx_observed_days": fx_res.get("observed_days"),
                       "fuel_last_verified": fuel_res.get("last_verified_at"),
                       "base_fresh": base_res.get("fresh")})
        _notify(_build_message("dry_run", run_id, fx, fuel_res, base_res, details,
                               guard_fires, auto_blockers, n_changes))
        return {"run_id": run_id, "outcome": "dry_run", "n_changes": n_changes,
                "guard_fires": guard_fires, "auto_blockers": auto_blockers}

    # === 横断原則「通知できないなら auto しない」(Codex HIGH-4) ===
    # apply 前に通知疎通を確認。Discord が死んでいれば money-direct 変更を無通知で行わず
    # dry_run へ降格 (適用済みになってから「誰も気付かない」を防ぐ)。
    pre_msg = (
        f":truck: 送料 rate table 月次バッチ — **auto 適用を開始します** (run {run_id})\n"
        f"FX={fx}円/$ / FedEx燃料={fuel_res['fedex_pct']}% / DHL燃料={fuel_res['dhl_pct']}%"
        f" / 変更 {n_changes} rate を適用します"
    )
    if not _notify(pre_msg):
        logger.error("pre-apply 通知失敗 → auto 中止 (通知できないなら auto しない)")
        db.insert_details(run_id, details)
        db.insert_run(run_id, "dry_run", "held",
                      _inputs(fx_res, fuel_res, base_res),
                      {"warnings": warnings + ["pre-apply Discord 通知失敗 → auto 中止・dry_run 降格"],
                       "auto_blockers": ["Discord 通知不能"], "n_changes": n_changes})
        return {"run_id": run_id, "outcome": "held_notify_failed", "n_changes": n_changes}

    # === AUTO: SNAPSHOT_ALL ===
    snapshot = {"run_id": run_id, "created_at": now.isoformat(timespec="seconds"),
                "fx": fx, "fedex_fuel": fuel_res["fedex_pct"], "dhl_fuel": fuel_res["dhl_pct"],
                "tables": {tid: [{"rateId": c["rateId"], "old": c["old"], "intended": c["usd"], "zone": c["zone"]}
                                 for c in chs] for tid, chs in changes_by_table.items() if chs}}
    config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (config.SNAPSHOT_DIR / f"{run_id}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")

    # === APPLY_ALL ===
    applied_tables: list[str] = []
    apply_failed: str | None = None
    for table_id, chs in changes_by_table.items():
        if not chs:
            continue
        # 例外 (timeout/network/OAuth) も apply_failed に変換し、必ず PARTIAL_APPLIED 経路へ
        # 流す (プロセスが落ちて partial 記録/alert/復旧案内に届かない Q0 穴を塞ぐ、Codex)。
        try:
            res = ebay_api.update_shipping_cost(table_id, [{"rateId": c["rateId"], "usd": c["usd"]} for c in chs], token=token)
        except Exception as e:  # noqa: BLE001
            apply_failed = f"{table_id}: updateShippingCost 例外 {type(e).__name__}: {e}"
            break
        if not res["ok"]:
            apply_failed = f"{table_id}: updateShippingCost status={res['status']} {res['body'][:200]}"
            break
        applied_tables.append(table_id)

    if apply_failed:
        # PARTIAL_APPLIED: 即 alert。recovery は run_id snapshot ベースで別途。
        _mark_actions(details, applied_tables, changes_by_table, "applied")
        db.insert_details(run_id, details)
        db.insert_run(run_id, "auto", "partial_applied",
                      _inputs(fx_res, fuel_res, base_res),
                      {"warnings": warnings, "applied_tables": applied_tables, "apply_failed": apply_failed,
                       "recovery": f"snapshot {run_id}.json で rollback/再適用"})
        msg = (":rotating_light::rotating_light: 送料月次バッチ **PARTIAL_APPLIED** "
               f"(一部 table のみ適用、要手動復旧)\n適用済: {applied_tables}\n失敗: {apply_failed}\n"
               f"snapshot: {run_id}.json")
        logger.error(msg)
        _notify(msg)
        return {"run_id": run_id, "outcome": "partial_applied", "applied_tables": applied_tables,
                "apply_failed": apply_failed}

    # === VERIFY_ALL (readback、eventual consistency retry) ===
    # 長時間 run 中に access token が hard-expire し得る (過去事故多発)。verify/rollback は
    # mutation 後の重要フェーズなので token を取り直す (get_valid_access_token が内部 refresh)。
    try:
        token = ebay_api._token()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"verify 前 token 再取得失敗、既存 token で続行: {e}")
    verify_fail: list[str] = []
    for band, _ in config.BANDS:
        info = preflight[band]
        table_id = info["table_id"]
        if table_id not in applied_tables:
            continue
        expected = {z: computed[band][z] for z in zone_numbers}
        # readback 例外 (401/ネット断等) を握って verify_fail 化 — 適用成功済なのに例外が
        # 無記録で脱出 (監査ログ欠落 = 横断原則違反) するのを防ぐ。
        try:
            vr = ebay_api.readback_verify(table_id, expected, _match, token=token)
        except Exception as e:  # noqa: BLE001
            vr = {"ok": False, "mismatches": [f"readback 例外: {type(e).__name__}: {e}"]}
        if not vr["ok"]:
            verify_fail.append(f"{band}({table_id}): " + "; ".join(vr["mismatches"][:4]))

    if verify_fail:
        # rollback 試行 (snapshot の old へ)
        rollback_note = _attempt_rollback(snapshot, token)
        _mark_actions(details, applied_tables, changes_by_table, "applied")
        db.insert_details(run_id, details)
        db.insert_run(run_id, "auto", "aborted",
                      _inputs(fx_res, fuel_res, base_res),
                      {"warnings": warnings, "verify_fail": verify_fail, "rollback": rollback_note})
        msg = (":rotating_light: 送料月次バッチ 読戻し不一致 → rollback 試行。\n"
               f"不一致: {verify_fail}\nrollback: {rollback_note}\nsnapshot: {run_id}.json")
        logger.error(msg)
        _notify(msg)
        return {"run_id": run_id, "outcome": "verify_failed", "verify_fail": verify_fail, "rollback": rollback_note}

    # === SUCCESS ===
    _mark_actions(details, applied_tables, changes_by_table, "applied")
    db.insert_details(run_id, details)
    db.insert_run(run_id, "auto", "ok",
                  _inputs(fx_res, fuel_res, base_res),
                  {"warnings": warnings, "applied_tables": applied_tables, "n_changes": n_changes,
                   "fx_observed_days": fx_res.get("observed_days"),
                   "fuel_last_verified": fuel_res.get("last_verified_at"),
                   "base_fresh": base_res.get("fresh")})
    _notify(_build_message("auto", run_id, fx, fuel_res, base_res, details, [], [], n_changes))
    return {"run_id": run_id, "outcome": "auto_applied", "applied_tables": applied_tables, "n_changes": n_changes}


# ---- helpers ----
def _last_good_fx():
    try:
        st = json.loads(config.FX_STATE.read_text(encoding="utf-8"))
        return (st.get("last_good") or {}).get("fx")
    except (OSError, json.JSONDecodeError):
        return None


def _inputs(fx_res, fuel_res, base_res) -> dict:
    return {"fx": fx_res.get("fx"), "fx_period": fx_res.get("period"),
            "fedex_fuel": fuel_res.get("fedex_pct"), "dhl_fuel": fuel_res.get("dhl_pct"),
            "fuel_source": fuel_res.get("source"), "base_rates_source": base_res.get("source")}


def _mark_actions(details, applied_tables, changes_by_table, action):
    changed = {(tid, c["rateId"]) for tid, chs in changes_by_table.items() for c in chs}
    for d in details:
        if d["table_id"] in applied_tables and (d["table_id"], d["rate_id"]) in changed:
            d["action"] = action
        elif d["old_usd"] == d["new_usd"]:
            d["action"] = "skipped"  # 変更なし
        else:
            d["action"] = "held"     # 変更予定だが未適用 (失敗 table 等)


def _attempt_rollback(snapshot, token) -> str:
    """snapshot の old 値へ戻す。失敗時は手動復旧を促す文言を返す。"""
    failed = []
    for tid, rows in snapshot["tables"].items():
        ups = [{"rateId": r["rateId"], "usd": r["old"]} for r in rows if r.get("old") is not None]
        if not ups:
            continue
        # rollback API の例外も failed に集約し、必ず手動復旧文言を返す (Codex HIGH-2)。
        try:
            res = ebay_api.update_shipping_cost(tid, ups, token=token)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{tid}(exception={type(e).__name__})")
            continue
        if not res["ok"]:
            failed.append(f"{tid}(status={res['status']})")
    if failed:
        return (f"rollback 一部失敗: {failed} → **手動復旧必要** "
                f"(snapshot {snapshot['run_id']}.json の old 値を UI/API で再投入)")
    return "rollback 成功 (snapshot の old 値へ復元)"


def _build_message(mode, run_id, fx, fuel_res, base_res, details, guard_fires, auto_blockers, n_changes) -> str:
    head = ":truck: 送料 rate table 月次バッチ"
    mode_label = "**DRY-RUN (適用なし)**" if mode == "dry_run" else "**AUTO 適用完了**"
    lines = [
        f"{head} — {mode_label}  (run {run_id})",
        f"FX={fx}円/$ / FedEx燃料={fuel_res['fedex_pct']}% / DHL燃料={fuel_res['dhl_pct']}%"
        f" / 基本料金={base_res['source']}",
        f"変更予定 {n_changes} rate" + ("" if mode == "auto" else " (適用していません)"),
    ]
    # 主要な変更を抜粋 (old != new のみ、最大 20)
    diffs = [d for d in details if d["old_usd"] != d["new_usd"]]
    for d in diffs[:20]:
        lines.append(f"  {d['band']} z{d['zone']}: ${d['old_usd']}→${d['new_usd']}")
    if len(diffs) > 20:
        lines.append(f"  ... 他 {len(diffs) - 20} 件 (詳細は shipping_rate_batch_detail / run {run_id})")
    if not diffs:
        lines.append("  (現行と差分なし)")
    if guard_fires:
        lines.append(f":warning: 変動ガード {len(guard_fires)} 件:")
        lines.extend("   " + g for g in guard_fires[:10])
    if auto_blockers:
        lines.append(":lock: auto 不可の理由:")
        lines.extend("   " + b for b in auto_blockers[:8])
    if fuel_res.get("warnings"):
        lines.extend(":information_source: " + w for w in fuel_res["warnings"][:3])
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mode = sys.argv[1] if len(sys.argv) > 1 else "dry_run"
    result = run_batch(force_mode=mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
