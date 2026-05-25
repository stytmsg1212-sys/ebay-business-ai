# W175 fix plan — 個別出品 wizard 完了後の session_state cleanup (HTTP 431 対応)

## 背景

2026-05-25 user 報告: 個別出品 タブで `保存&ACTIVE出品` ボタン押下 (eBay AddItem 成功、`ebay_item_id=358597336018` listing_drafts id=16 status='applied' 確定) 後、Streamlit が **HTTP 431 (Request Header Fields Too Large)** で全 page 不達.

原因仮説: 個別出品 wizard が session_state に蓄積する大型データ (scraped HTML / AI 生成 listing / 画像 URL list / Photoroom 中間 path / eBay 応答 XML / EPS 公開 URL list 等) が、Streamlit Tornado server の Cookie/header limit (~8KB) を超過. 連続出品 (1 listing 完了→次の listing 開始) のたびに過去 session_state が残存し、累積で header overflow.

## 修正範囲

`tools/ebay-manager/tabs/tab_individual_listing.py` の AddItem 成功 path で大型 session_state を auto-cleanup.

## 現状の session_state 構造 (`_SS = "il_"` prefix)

`_init_session_state` (L99-152) で初期化される keys:

| key | 内容 | サイズ目安 |
|---|---|---|
| `il_supplier_url` | str | <1KB |
| `il_reference_url` | str | <1KB |
| `il_scraped_product` | dict (商品名/価格/説明/画像 URL list) | **5-20KB** |
| `il_reference_listing` | eBay GetItem 応答 dict | **3-10KB** |
| `il_selected_image_urls` | list[str] | **1-5KB** |
| `il_rank_classification` | dict | <1KB |
| `il_generated_listing` | AI 生成 (title/description/specifics) | **5-15KB** |
| `il_hero_candidates` | list[{plate_id, path, reasoning}] | **2-8KB** |
| `il_hero_studio_path` | Path str | <1KB |
| `il_hero_source_url` | str | <1KB |
| `il_additional_processed` | list[{source_url, path}] | **1-5KB** |
| `il_processed_image_urls` | list[str] EPS 公開 URL | **1-3KB** |
| `il_verify_result` | dict (VerifyAddFixedPriceItem 応答) | **2-8KB** |
| `il_add_result` | dict (AddFixedPriceItem 応答 + raw_xml) | **5-20KB** |
| `il_pl_result` | Promoted Listings 応答 | **1-3KB** |
| `il_current_draft_id` | int | <1KB |

**累積見積**: 1 listing で **20-80KB**、複数 listing 連続で線形増加 → Cookie/header overflow.

## 設計案 (3 候補)

### 候補 A: AddItem 成功直後 auto-cleanup (heavy items のみ)

`tabs/tab_individual_listing.py` L2033 (status.update(state="complete") 直後) に追加:

```python
# W175 (2026-05-25): HTTP 431 対応. 大型 session_state を auto-cleanup.
# 残す: add_result / pl_result / current_draft_id (success display 用)
_HEAVY_KEYS_TO_CLEAR_AFTER_ADD = [
    f"{_SS}scraped_product",
    f"{_SS}reference_listing",
    f"{_SS}generated_listing",
    f"{_SS}rank_classification",
    f"{_SS}hero_candidates",
    f"{_SS}hero_studio_path",
    f"{_SS}hero_source_url",
    f"{_SS}additional_processed",
    f"{_SS}processed_image_urls",
    f"{_SS}selected_image_urls",
    f"{_SS}verify_result",
]
for k in _HEAVY_KEYS_TO_CLEAR_AFTER_ADD:
    st.session_state.pop(k, None)
logger.info("W175 auto-cleanup: removed %d heavy session_state keys after AddItem",
            len(_HEAVY_KEYS_TO_CLEAR_AFTER_ADD))
```

- **LOC**: ~18 (list + loop + log)
- **メリット**: 成功 listing の summary 表示 (add_result の ebay_item_id / fees / scheduled_time) は維持
- **デメリット**: user が同じ URL で再 scrape したい (rare case) ときに再取得が必要

### 候補 B: 完全 reset (新規出品 button 追加)

Step 5 結果表示エリアに **「新しい出品を開始」** button を追加. user が手動で `_clear_from_step(1)` 相当を呼ぶ.

```python
if st.session_state.get(f"{_SS}add_result", {}).get("success"):
    if st.button("🆕 新しい出品を開始", key="il_start_new_listing"):
        # _clear_from_step(1) で Step 1 含む全 state を初期値に戻す
        _clear_from_step(1)
        st.session_state[f"{_SS}supplier_url"] = ""
        st.session_state[f"{_SS}reference_url"] = ""
        st.rerun()
```

- **LOC**: ~8
- **メリット**: user 操作明示、誤クリックリスク低
- **デメリット**: user が忘れて連続出品すると HTTP 431 再発

### 候補 C: 両方 (auto + manual button)

候補 A の自動 cleanup + 候補 B の button. 自動で heavy items は片付け、user は明示 button で完全 reset.

- **LOC**: ~26
- **メリット**: 多層防御 (silent skip 防止 + UX 明示)
- **デメリット**: K1 Simplicity 越境気味

## 推奨候補

**候補 A** が K1 Simplicity 範囲内で HTTP 431 根本対策として最小. user は自然に新規出品を開始でき、summary 表示も維持.

候補 B/C は将来 user 要望次第で別 W (W175.2) で追加可能.

## 検証経路

1. **静的 verify**: AST parse + import OK
2. **pytest 追加**: session_state cleanup 後に heavy keys が消えていることを check
3. **Streamlit 実機**: 連続 2 件出品で HTTP 431 が再発しないか
4. **R-11 user 実視認**: 出品成功 toast + summary 表示が消えないこと

## Codex review で確認したい点

1. **root cause 判定** (Cookie/header overflow 仮説) は正しいか? 他の HTTP 431 トリガーがないか
2. **clear する key list** が漏れなくかつ過剰でないか (削除すべきでない key を消していないか)
3. **タイミング** (AddItem 成功直後) が適切か? Promoted Listings 処理 (L2038+) の前/後で差が出るか
4. **回帰リスク**: 同じ URL で再 scrape したい user case はあるか? あれば候補 B の button 必須
5. **draft 機能との整合性**: 「保存済みドラフト」タブで draft load (L228) する path で支障ないか
6. **erstatlas (Codex 仮説)**: session_state 以外で Cookie/header を肥大化させる Streamlit 機能 (例: AuthState, cache_data 等) を見落としていないか

## ROADMAP

W175 (id=259、新規): 個別出品 wizard 完了後 session_state auto-cleanup (HTTP 431 対応)
- priority: 高 (本番出品ブロックの可能性)
- source: 2026-05-25 W174-pm session で発覚
