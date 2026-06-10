"""eBaymag プランv2 小グループ バッチ走行 (1 件ずつ discover→apply、逐次).

- 各 item: driver discover で productId 回収 → driver apply (安全弁 2 重) → RESULT 記録
- 3 連続失敗で全停止 (系統的問題の signal)
- 結果は data/ebaymag_batch_log_2026_06_11.json に逐次 flush
- 03:20 JST を過ぎたら新規 item を開始しない (W229 ハーベスト CDP 競合回避)
"""
import datetime
import json
import re
import subprocess
import sys

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DRIVER = r"scripts\ebaymag_publish_driver_2026_06_11.py"
LOG = r"data\ebaymag_batch_log_2026_06_11.json"
STOP_AT = datetime.time(3, 20)  # JST、これ以降は新規 item 開始しない

# (query, sites_csv, item_id, label) — 小グループ 18 件 (UK 2 件は完了済みで除外)
WORKLIST = [
    ("DT4261", "AU,CA", "358626622317", "HIOKI DT4261"),                # CA 初観察
    ("X310", "AU,FR", "357414236596", "Leica Disto X310"),
    ("Keithley 237", "FR", "358352049570", "Keithley 237 SMU"),
    ("AR-3000A", "AU,IT", "358207319749", "AOR AR-3000A"),
    ("DISTO D2", "IT,UK", "357418890043", "Leica DISTO D2"),
    ("PC-G850VS", "DE,IT", "357839289258", "SHARP PC-G850VS"),
    ("PJ-723", "DE,IT", "357374753803", "Brother PJ-723"),
    ("PM8006", "CA", "358333799417", "Marantz PM8006"),
    ("AKP846", "CA", "357387217824", "Ajazz AKP846"),
    ("KP-707G", "IT", "357200863085", "Pioneer KP-707G"),
    ("DSP-FTA440", "IT", "358377470398", "Fluke DSP-FTA440"),
    ("KP-717G", "DE,UK", "357944436089", "Pioneer KP-717G"),
    ("LUKA V4X", "DE,UK", "357065276999", "CRYPTON LUKA V4X"),
    ("Alice Madness", "DE,ES", "357418184869", "Alice Madness Art Book"),
    ("LuciPac", "DE,ES", "358228793891", "Kikkoman LuciPac"),
    ("SP-004", "CA,FR", "357190920884", "Wallhack SP-004"),
    ("Car Eye", "CA,FR", "358377346781", "BMW MINI Car Eye 3.0"),
    ("DST-010", "CA,FR", "358046729862", "DENSO DST-010 Cable"),
]


def run(args: list[str], timeout: int = 180) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, DRIVER, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> None:
    results = []
    consecutive_fail = 0
    skip_ca = False  # CA 枠エラー検出後は CA 含みを丸ごとスキップ

    for query, sites, item_id, label in WORKLIST:
        now = datetime.datetime.now().time()
        if now >= STOP_AT and now < datetime.time(4, 0):
            results.append({"item_id": item_id, "label": label, "status": "deferred_w229_window"})
            print(f"[{label}] 03:20-04:00 窓 → 後回し")
            continue

        if skip_ca and "CA" in sites.split(","):
            results.append({"item_id": item_id, "label": label, "sites": sites,
                            "status": "skipped_ca_quota"})
            print(f"[{label}] CA 枠エラー検出済 → スキップ")
            _flush(results)
            continue

        print(f"\n=== {label} ({sites}) item={item_id} ===")
        # --- discover ---
        try:
            rc, out = run(["discover", query])
        except subprocess.TimeoutExpired:
            rc, out = 99, "TIMEOUT"
        m = re.search(r"productId[:=](\d+)", out)
        if rc != 0 or not m:
            print(f"[{label}] discover 失敗 rc={rc}\n{out[-400:]}")
            results.append({"item_id": item_id, "label": label, "sites": sites,
                            "status": "discover_failed", "out_tail": out[-400:]})
            consecutive_fail += 1
            _flush(results)
            if consecutive_fail >= 3:
                print("3 連続失敗 → 全停止")
                break
            continue
        product_id = m.group(1)
        print(f"[{label}] productId={product_id}")

        # --- apply ---
        try:
            rc, out = run(["apply", query, product_id, sites], timeout=240)
        except subprocess.TimeoutExpired:
            rc, out = 99, "TIMEOUT"
        ok = rc == 0 and "RESULT: OK" in out
        print(out[-700:])
        rec = {"item_id": item_id, "label": label, "sites": sites,
               "productId": product_id,
               "status": "ok" if ok else f"apply_failed_rc{rc}",
               "out_tail": out[-500:]}
        results.append(rec)
        _flush(results)

        if ok:
            consecutive_fail = 0
        else:
            consecutive_fail += 1
            # CA 含みで画面エラー語 or 未定着 → CA 枠とみなし以後の CA をスキップ
            if "CA" in sites.split(",") and ("エラー" in out or "未定着" in out):
                skip_ca = True
                print(f"[{label}] CA でエラー → 以後 CA 含みグループをスキップ")
            if consecutive_fail >= 3:
                print("3 連続失敗 → 全停止")
                break

    _flush(results)
    n_ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\n==== 完了: OK {n_ok} / 全 {len(results)} (log: {LOG}) ====")


def _flush(results: list) -> None:
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.datetime.now().isoformat(timespec="seconds"),
                   "results": results}, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
