"""ROADMAP id=236 (W153) を v3 PM 改修内容で update (one-shot)."""
import json
from pathlib import Path

PATH = Path(__file__).parent.parent / "data" / "system_improvements.json"

with open(PATH, encoding="utf-8") as f:
    data = json.load(f)

found = False
for entry in data:
    if entry.get("id") == 236:
        v3_append = (
            "\n\n[2026-05-22 PM v3 大改修] user 実画面 E2E 視認で重大欠陥 + 5 件業務要求発見 → "
            "v3 大改修完遂. (1) Black 単独 noise hit (改行区切り誤解で各行別 query union) → "
            "**空白区切り 1 query AND 検索** に統一 (Haiku prompt 書換 / list[str]→str / "
            "task_rival_detection 検索 loop 廃止 = 1 listing 1 call). "
            "(2) Economy 系 noise → seller block list 案は誤り (同 seller の高商品を巻き込む = "
            "reference_ebay_economy_shipping_seller_pattern.md 業務知識化) → **検索全件 record + "
            "UI hide** (delivery window > 10 日 OR shipping_service_code に 'Economy' 含む). "
            "(3) **DB v51 → v52** (listing_rival_discoveries に competitor_shipping_cost_usd / "
            "min_delivery_date / max_delivery_date / shipping_service_code 4 列追加). "
            "(4) **新規 rival のみ詳細 API enrich** (get_item_by_legacy_id で shipping_service_code "
            "取得、quota 新規分のみ ~5 calls/run). (5) UI 4 項目表示 (商品価格 + 送料 + 合計 + "
            "発送方法名 + 配達日数). (6) **UI 一括処理逆転** (☑=登録対象、未☑=自動却下、submit 1 回で "
            "全処理 = rerun 1 回). (7) cleanup script v3 強化 (--ebay-item-id 必須 / --max-id "
            "auto fence / JSONL dump / self-rename .executed). (8) top-level except errors++ "
            "(Codex HIGH: 旧実装は cron 集約で silent skip). 内部 reviewer Opus HIGH 3 + "
            "Codex GPT-5.5 HIGH 1 + MED 2 全解消. pytest 1441 PASS + W153 47/47 PASS. "
            "本番 DB user_version 50→52 適用 + MonoDeck/scheduler 再起動済. "
            "設計書 v3 改訂 (§20 改訂履歴 += FIX-1〜FIX-12). 学び: pytest + reviewer は "
            "user E2E 視認の代替にならない (UI label の mental model gap は automated test 不能)."
        )
        entry["progress_note"] = (entry.get("progress_note") or "") + v3_append
        # business critical fix を mark するためタグ追記
        if "v3" not in (entry.get("tag") or ""):
            entry["tag"] = (entry.get("tag") or "") + " v3"
        found = True
        break

if found:
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("[OK] id=236 W153 v3 progress_note appended")
else:
    print("[ERROR] id=236 not found")
