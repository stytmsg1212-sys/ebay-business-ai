#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W119 Step 4 一括 Browse API 検索 CLI script.

全 active listing の search_keyword で Browse API を loop call、各 listing の top 10 候補を
JSON に保存. UI Step 4 一括モード側で「📁 JSON から読込」ボタンで session_state に注入し、
user は curation + register に集中できる.

使い方:
    python scripts/run_w119_bulk_browse.py             # 全件 (~420 listing, ~6 分)
    python scripts/run_w119_bulk_browse.py --limit 10  # 上限指定 (test 用)

出力: data/w119_bulk_results.json
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import get_conn  # noqa: E402
from monitor.credentials import get_ebay_credentials  # noqa: E402
from tabs.tab_research_wizard import (  # noqa: E402
    _process_browse_items,
    _BROWSE_API_LIMIT,
    _BULK_BROWSE_SLEEP_SEC,
)

logger = logging.getLogger(__name__)
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "w119_bulk_results.json"


def fetch_listings_with_keyword() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ebay_item_id, title, search_keyword
            FROM ebay_listings
            WHERE (is_ended IS NULL OR is_ended=0)
              AND title IS NOT NULL AND title != ''
              AND search_keyword IS NOT NULL AND search_keyword != ''
            ORDER BY ebay_item_id
            """
        ).fetchall()
    return [
        {"ebay_item_id": r[0], "title": r[1], "search_keyword": r[2]} for r in rows
    ]


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="処理 listing 上限 (test 用)")
    parser.add_argument("--no-getitem", action="store_true",
                        help="getItem call を skip (shipping_service_code 取得しない、速い)")
    parser.add_argument("--saturated-only", type=int, default=None, metavar="N",
                        help="既存 JSON で候補数が N 件以上の listing のみ再実行. "
                             "例: --saturated-only 10 で 10 件 cap 達成 listing だけ再 search.")
    parser.add_argument("--force", action="store_true",
                        help="errorId 2001 (daily quota) 24h 抑制窓を無視して強制実行.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    # H-NEW-2 (review #2): 24h 抑制窓 guard.
    # 直近の bulk run で errorId 2001 (daily quota saturation) を観測している場合、
    # 24h 経過するまで自動 retry を skip (cron 二重発火で quota を更に圧迫しないため).
    # --force で bypass 可能.
    if OUTPUT_PATH.exists() and not args.force:
        try:
            _existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) or {}
            _last_2001 = (_existing.get("meta") or {}).get("last_quota_2001_at")
            if _last_2001:
                from datetime import datetime, timedelta, timezone
                try:
                    _ts = datetime.fromisoformat(_last_2001)
                    _now = datetime.now(timezone.utc) if _ts.tzinfo else datetime.now()
                    if (_now - _ts) < timedelta(hours=24):
                        _hrs_remain = 24 - int((_now - _ts).total_seconds() / 3600)
                        print(
                            f"🚫 errorId 2001 (eBay daily quota saturation) を "
                            f"{_last_2001} に観測. 残 ~{_hrs_remain}h は自動 retry を skip. "
                            f"強制実行は `--force` 付きで再実行してください."
                        )
                        sys.exit(3)
                except ValueError:
                    pass  # 不正な timestamp は無視して通常 flow
        except (json.JSONDecodeError, OSError):
            pass  # 読み込み失敗は通常 flow に流して既存挙動維持

    # config + credentials
    cfg = json.loads(
        (Path(__file__).resolve().parent.parent / "config" / "schedule_config.json")
        .read_text(encoding="utf-8")
    )
    creds = get_ebay_credentials(cfg)
    app_id = creds.get("app_id", "")
    cert_id = creds.get("cert_id", "")
    if not (app_id and cert_id):
        print("ERROR: eBay API credentials 不在 (config/schedule_config.json 確認)")
        sys.exit(1)

    from tasks.ebay_browse_api import BrowseAPIClient
    client = BrowseAPIClient(app_id, cert_id)

    listings = fetch_listings_with_keyword()
    if args.limit:
        listings = listings[: args.limit]

    # --saturated-only: 既存 JSON で候補数 >= N の listing + 前回失敗 listing を対象に絞る.
    # H6 (Wave B): 既存 JSON 不在 / 該当 0 件で fallback せず明示 exit (silent skip 解消).
    # H7 (Wave B): 前回失敗 listing (results[eid]=None) も自動 retry 対象に含める.
    existing_results: dict = {}
    if args.saturated_only is not None:
        if not OUTPUT_PATH.exists():
            print(
                f"ERROR: --saturated-only 指定だが {OUTPUT_PATH.name} 不在. "
                f"先に全件 bulk search (--saturated-only 抜き) を実行してください."
            )
            sys.exit(2)
        try:
            old_data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(
                f"ERROR: 既存 JSON 読込失敗 ({e}). "
                f"silent fallback せず終了 (data corruption 疑い、JSON を再生成してください)."
            )
            sys.exit(2)
        existing_results = old_data.get("results") or {}
        threshold = args.saturated_only
        saturated_ids = {
            eid for eid, items in existing_results.items()
            if items and len(items) >= threshold
        }
        # H7: 前回 API 失敗 listing も同時 retry
        failed_ids = {
            eid for eid, items in existing_results.items() if items is None
        }
        target_ids = saturated_ids | failed_ids
        listings = [l for l in listings if l["ebay_item_id"] in target_ids]
        print(
            f"--saturated-only {threshold}: saturated {len(saturated_ids)} 件 + "
            f"前回失敗 {len(failed_ids)} 件 = {len(listings)} listing 対象"
        )
        if not listings:
            print(f"対象 0 件: 候補 {threshold} 件以上 / 前回失敗 listing は存在しません. 終了.")
            sys.exit(0)

    n = len(listings)
    print(f"対象 listing: {n} 件 (推定 ~{n * (0.7 + 0.5 * 20) / 60:.0f} 分、getItem 込)")

    # 既存結果を merge base にする (saturated 以外は前回結果を維持)
    results: dict = dict(existing_results) if args.saturated_only is not None else {}
    n_with = 0
    n_zero = 0
    n_failed = 0
    last_failed_id = ""

    # 2026-05-12 復旧 retry 用: 429 連発時の adaptive backoff state.
    # - 連続 429 で sleep を線形に伸ばす (5s → 15s → 30s → 60s, cap 60s)
    # - 1 件成功で sleep をリセット
    # - 連続 N 件 429 で完全 abort して quota 回復を待つ
    consecutive_429 = 0
    backoff_extra_sec = 0
    ABORT_AFTER_CONSECUTIVE_429 = 8  # 8 連続 429 で abort (rate limit hit 確定)

    # H-NEW-2 (review #2): errorId 2001 (resource limit = daily quota saturation) 検出 flag.
    # True なら UI / 後続 cron に「本日中の自動 retry は無効」signal を渡す.
    quota_2001_observed = False

    import httpx as _httpx

    start = time.time()
    for idx, it in enumerate(listings, start=1):
        ebay_item_id = it["ebay_item_id"]
        keyword = it["search_keyword"]
        if idx > 1:
            time.sleep(_BULK_BROWSE_SLEEP_SEC + backoff_extra_sec)
        try:
            items = client.search_items(
                query=keyword,
                limit=_BROWSE_API_LIMIT,
                item_location_country="JP",
                delivery_country="US",
                sort="price",
            )
        except Exception as e:
            # H10 副作用 fix (2026-05-12): search_items が raise するように改めた結果、
            # 前回成功 result がある listing でも 429 等で None 上書きされる経路が出た.
            # 既存有効データ (None でなく非空) があれば **温存** し、失敗カウントだけ進める.
            logger.warning(
                f"[w119_bulk] Browse API failed {ebay_item_id}: {type(e).__name__}: {e}"
            )
            prev = results.get(ebay_item_id)
            if prev is None or prev == []:
                # 前回も失敗 or 真の 0 件 → 失敗 sentinel として None を書込
                results[ebay_item_id] = None
            # 前回成功 (非空 list) なら results[ebay_item_id] は変更しない (前回結果温存)
            n_failed += 1
            last_failed_id = ebay_item_id

            # 429 連発 adaptive backoff
            is_429 = (
                isinstance(e, _httpx.HTTPStatusError)
                and getattr(e.response, "status_code", None) == 429
            )
            if is_429:
                consecutive_429 += 1
                # H-NEW-2: errorId 2001 (daily quota / resource limit) を response body から検出
                try:
                    body = e.response.json()
                    err_id = body.get("errors", [{}])[0].get("errorId")
                    if err_id == 2001:
                        quota_2001_observed = True
                except (ValueError, KeyError, IndexError, AttributeError):
                    pass
                # 線形に sleep を増やす (0→5→15→30→60s cap)
                next_backoff = min(60, 5 * (2 ** (consecutive_429 - 1)))
                backoff_extra_sec = next_backoff
                print(
                    f"  ⚠ 429 連続 {consecutive_429} 回目: backoff extra sleep "
                    f"{backoff_extra_sec}s で継続 (短期 rate limit hit"
                    f"{' / errorId 2001 = daily quota saturation' if quota_2001_observed else ''})",
                    flush=True,
                )
                if consecutive_429 >= ABORT_AFTER_CONSECUTIVE_429:
                    print(
                        f"\n  ❌ 429 が {ABORT_AFTER_CONSECUTIVE_429} 回連続. "
                        f"quota 回復待ちのため abort. "
                        f"残 {n - idx} listing は次回 retry で復旧可能 (前回データ温存済).",
                        flush=True,
                    )
                    break
            continue

        # 成功した listing で backoff state をリセット
        consecutive_429 = 0
        backoff_extra_sec = 0

        top_items = _process_browse_items(items, ebay_item_id)

        # W119 (2026-05-12): 各 top item に対して getItem call で 配送方法 + 関税ポリシー を取得.
        # 2 軸独立: 配送方法 (carrier: SpeedPAK Economy / FedEx 等) + 関税ポリシー (DDU/DDP).
        # 詳細: `reference_shipping_method_vs_ddu_taxonomy.md`.
        # cost: 1 listing あたり最大 20 件 × 0.3s ≒ 6 秒追加.
        if not args.no_getitem and top_items:
            for it in top_items:
                legacy = it.get("legacy_item_id")
                if not legacy or not legacy.isdigit():
                    continue
                try:
                    detail = client.get_item_pricing(legacy)
                    if detail:
                        it["shipping_service_code"] = detail.get("shipping_service_code")
                        it["shipping_type"] = detail.get("shipping_type")
                        it["is_ddu_policy"] = detail.get("is_ddu_policy")
                except Exception as e:
                    logger.warning(
                        f"[w119_bulk] getItem failed for {legacy}: {type(e).__name__}: {e}"
                    )
                time.sleep(0.5)  # API rate-limit 緩和 (2026-05-12: 0.3→0.5、429 burst 防御)

        results[ebay_item_id] = top_items
        if top_items:
            n_with += 1
        else:
            n_zero += 1

        if idx % 20 == 0 or idx == n:
            elapsed = time.time() - start
            print(
                f"  [{idx}/{n}] 競合あり {n_with} / 0 件 {n_zero} / 失敗 {n_failed} "
                f"(elapsed {elapsed:.0f}s, eta {(elapsed / idx) * (n - idx):.0f}s)",
                flush=True,
            )

    # 保存 (H-NEW-3: atomic write で partial-state read を防ぐ)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 既存 meta があれば quota_2001_observed timestamp を引き継ぐ (24h 抑制窓に使用)
    existing_meta: dict = {}
    if OUTPUT_PATH.exists():
        try:
            existing_meta = (json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) or {}).get("meta") or {}
        except (json.JSONDecodeError, OSError):
            existing_meta = {}

    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_listings": n,
        "n_with_competitors": n_with,
        "n_zero_competitors": n_zero,
        "n_failed": n_failed,
        "duration_sec": round(time.time() - start, 1),
    }
    # H-NEW-2: errorId 2001 観測時刻を記録. UI と後続 cron がこれを見て「24h 抑制窓」を判定.
    if quota_2001_observed:
        meta["last_quota_2001_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    elif existing_meta.get("last_quota_2001_at"):
        # 今回観測なしでも、既存 24h 以内の記録は引き継ぐ (途中保存後にも guard が効く)
        meta["last_quota_2001_at"] = existing_meta["last_quota_2001_at"]

    # atomic write: tmp に書込 → os.replace で rename (POSIX も Win32 も atomic)
    tmp_path = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    import os as _os
    _os.replace(tmp_path, OUTPUT_PATH)

    print(f"\n=== DONE ===")
    print(f"  {meta}")
    print(f"  output: {OUTPUT_PATH}")
    if n_failed > 0:
        print(f"  ⚠ 失敗 {n_failed} 件 (例: {last_failed_id}) は再検索推奨")
    if quota_2001_observed:
        print(f"  🚫 errorId 2001 (daily quota saturation) 観測. "
              f"本日中の自動 retry は 24h 抑制窓で skip されます.")


if __name__ == "__main__":
    main()
