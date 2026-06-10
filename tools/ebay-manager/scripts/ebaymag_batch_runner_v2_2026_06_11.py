"""eBaymag プランv2 大グループ バッチ走行 v2 (itm 照合安全弁 + skip リトライ).

- worklist は data/ebaymag_publish_groups_2026_06_11_filtered.json から生成
  (query はタイトルから型番トークン自動導出、`plan` モードで全件目視可)
- 各 item: discover(skip=0..3) → itm 照合一致の productId で apply (expected_itm 渡し)
- apply rc2 (itm ABORT) も次 skip でリトライ
- rc0 で RESULT: OK なし = 「変更なし (全国既に ON)」 → ok_noop
- 3 連続失敗で全停止 (系統的問題の signal)
- 03:20-04:00 JST は W229 ハーベスト CDP 競合 → sleep 待機
- 結果は data/ebaymag_batch_log_2026_06_11b.json に逐次 flush

usage:
  python ebaymag_batch_runner_v2_2026_06_11.py plan   # worklist 目視 (mutation なし)
  python ebaymag_batch_runner_v2_2026_06_11.py run
"""
import datetime
import json
import re
import subprocess
import sys
import time

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DRIVER = r"scripts\ebaymag_publish_driver_2026_06_11.py"
GROUPS = r"data\ebaymag_publish_groups_2026_06_11_filtered.json"
LOG = r"data\ebaymag_batch_log_2026_06_11b.json"

# 実行順 (小 → 大)。小グループ 18 件分は実施済みのため除外
GROUP_ORDER = ["DE,FR", "AU", "CA,DE", "AU,DE", "DE", "AU,CA,DE,ES,FR,IT,UK"]

# タイトル → eBaymag name フィルタ query の手動上書き (型番導出が曖昧なもの)
# PLOTTER は同型番×5 サイズ展開のため「型番+サイズ」で絞る (skip 0-3 で届く粒度に)
QUERY_OVERRIDE: dict[str, str] = {
    "356700630309": "Car-Boy CD-9",
    "357040070021": "LANGOGO",
    # PLOTTER (型番 + サイズ)
    "356739344931": "5012 A5",
    "358309952721": "5001 A5",
    "356739326665": "5016 A5",
    "356821342530": "5003 A5",
    "356578914997": "5012 Narrow",
    "356739345367": "5012 Bible",
    "358309950949": "5001 Bible",
    "356821350283": "5003 A5",
    "356739310346": "5001 Narrow",
    "356739332851": "5016 Narrow",
    "356739329438": "5016 Bible",
    "358309960189": "5003 A5",
    "356739334208": "5016 Mini",
    "356739343901": "5012 Mini",
    "356739311462": "5001 Mini",
    "356739346076": "5012 Mini 5",
    "356739327753": "5016 Mini 5",
    "356739323708": "5003 Narrow",
    "358309947715": "5001 Mini 5",
    "356739323243": "5003 Bible",
    "356739322701": "5003 Mini",
    "356739321184": "5003 Mini 5",
    "356739350043": "5015",
}


def derive_query(title: str) -> str:
    """型番らしいトークン (英数字混在) を優先、なければ先頭 2 語."""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]{3,14}", title)
    models = [t for t in tokens
              if re.search(r"[A-Za-z]", t) and re.search(r"\d", t)
              and not re.fullmatch(r"\d+[A-Za-z]{0,2}", t)]
    if models:
        return models[0]
    words = title.split()
    return " ".join(words[:2])


def build_worklist() -> list[dict]:
    d = json.load(open(GROUPS, encoding="utf-8"))
    groups = d["groups"] if isinstance(d, dict) and "groups" in d else d
    wl = []
    for combo in GROUP_ORDER:
        for it in groups.get(combo, []):
            iid = str(it["item_id"])
            wl.append({
                "item_id": iid,
                "title": it["title"],
                "sites": combo,
                "query": QUERY_OVERRIDE.get(iid, derive_query(it["title"])),
                "qty": it.get("qty_available"),
            })
    return wl


def run(args: list[str], timeout: int = 240) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, "-u", DRIVER, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def wait_out_w229_window() -> None:
    now = datetime.datetime.now()
    if datetime.time(3, 20) <= now.time() < datetime.time(4, 0):
        resume = now.replace(hour=4, minute=0, second=30, microsecond=0)
        secs = (resume - now).total_seconds()
        print(f"[W229 窓] 03:20-04:00 → {secs:.0f}s sleep して 04:00:30 再開")
        time.sleep(secs)


def process_item(item: dict) -> dict:
    rec = {"item_id": item["item_id"], "label": item["title"][:60],
           "sites": item["sites"], "query": item["query"]}
    expected = item["item_id"]
    for skip in range(4):
        # --- discover (itm 照合) ---
        try:
            rc, out = run(["discover", item["query"], "archived", str(skip)])
        except subprocess.TimeoutExpired:
            rc, out = 99, "TIMEOUT"
        if "TITLE_NOT_FOUND" in out:
            rec["status"] = f"not_found(skip={skip})"
            rec["out_tail"] = out[-300:]
            return rec
        m = re.search(r"productId[:=](\d+)", out)
        if rc != 0 or not m:
            rec["status"] = "discover_failed"
            rec["out_tail"] = out[-300:]
            return rec
        product_id = m.group(1)
        m_itm = re.search(r"itm: (\d{12})", out)
        itm = m_itm.group(1) if m_itm else None
        if itm and itm != expected:
            print(f"  skip={skip}: productId={product_id} itm={itm} 不一致 → 次候補")
            continue
        # --- apply (expected_itm 安全弁つき) ---
        print(f"  skip={skip}: productId={product_id} itm={itm} → apply")
        try:
            rc, out = run(["apply", item["query"], product_id, item["sites"], expected])
        except subprocess.TimeoutExpired:
            rc, out = 99, "TIMEOUT"
        rec["productId"] = product_id
        rec["out_tail"] = out[-500:]
        if rc == 0 and "RESULT: OK" in out:
            rec["status"] = "ok"
            return rec
        if rc == 0:
            rec["status"] = "ok_noop"  # 変更なし (全国既に ON)
            return rec
        if rc == 2 and "ABORT: itm=" in out:
            print(f"  skip={skip}: apply で itm ABORT → 次候補")
            continue
        rec["status"] = f"apply_failed_rc{rc}"
        return rec
    rec["status"] = "no_matching_itm(skip0-3)"
    return rec


def main() -> None:
    wl = build_worklist()
    if len(sys.argv) > 1 and sys.argv[1] == "plan":
        for i, it in enumerate(wl):
            print(f"{i+1:3} [{it['sites']:<22}] q={it['query']!r:<22} "
                  f"qty={it['qty']} {it['item_id']} | {it['title'][:70]}")
        print(f"\n計 {len(wl)} 件")
        return

    results = []
    consecutive_fail = 0
    for i, item in enumerate(wl):
        wait_out_w229_window()
        print(f"\n=== {i+1}/{len(wl)} [{item['sites']}] {item['title'][:60]} "
              f"(q={item['query']!r}) ===")
        rec = process_item(item)
        results.append(rec)
        print(f"  => {rec['status']}")
        _flush(results)
        if rec["status"].startswith("ok"):
            consecutive_fail = 0
        else:
            consecutive_fail += 1
            if consecutive_fail >= 3:
                print("3 連続失敗 → 全停止")
                break
    _flush(results)
    n_ok = sum(1 for r in results if r["status"].startswith("ok"))
    print(f"\n==== 完了: OK {n_ok} / 試行 {len(results)} / 全 {len(wl)} (log: {LOG}) ====")


def _flush(results: list) -> None:
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.datetime.now().isoformat(timespec="seconds"),
                   "results": results}, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
