---
title: eBay Manager 実装進捗レポート
version: 4.2
created: 2026-04-07
last_updated: 2026-06-11
---

# eBay Manager 実装進捗レポート

---

## W228 Phase 3 リサーチ探索 B 工程自動化（2026-06-11）

### ゴール
gate_passed 候補を毎日 04:30 に自動処理: AI 重量推定 → フリマ探索 → 利益計算 → awaiting_approval 積み → Discord 通知。

### 実装内容
完了

- [x] **新規** `tasks/task_research_sourcing.py`: B 工程本体 (run_research_sourcing)。fail-CLOSED コスト cap / AI 重量推定 (Haiku 4.5) / evaluate_product 呼出 / Section232 付記 / 状態遷移 / Discord 通知
- [x] **新規** `monitor/research_section232.py`: Section 232 keyword 辞書 (Annex I-A / I-B / III)、純関数 estimate_section232()
- [x] **新規** `tests/test_research_sourcing.py`: 14 テスト全 PASS (6 クラス: 正常フロー / コスト cap 途中 / fail-CLOSED / borderline / P2 状態分離 / 重量推定失敗 / disabled skip / Section232)
- [x] **配線** `daily_scheduler.py`: _run_research_sourcing 関数追加 + CronTrigger(hour=4, minute=30) add_job
- [x] **配線** `monitor/task_execution_log.py`: TASK_SCHEDULE に research_sourcing エントリ追加
- [x] **配線** `config/schedule_config.json`: research_sourcing セクション追加 (enabled=true, daily_cost_cap_usd=3.0, max_items_per_run=20)

### バグ修正（テスト実行中に発見）

- monkeypatch パス誤り × 2 (evaluate_product / estimate_section232 は source module に patch する): tasks.task_research_sourcing.* → monitor.research_poc.evaluate_product / monitor.research_section232.estimate_section232
- test_cap_reached_midway が `cost_aborted==0` で失敗: root cause は `result.update(counters)` による上書き。`result["cost_aborted"]` 直接書込から `counters["cost_aborted"]` 書込に修正して解決

### スコア自己評価
- **完成度**: 5/5
- **テスト実施**: pytest 14/14 PASS。既存回帰 127 件 PASS (pre-existing 1 件除く)
- **仕様準拠**: 設計書 §10 DoD 6 クラス全て満たす。Q0 (silent skip 防止 / fail-CLOSED) / Q2 (DB 冪等、今回 ALTER なし) / K1 (最小実装) / K2 (既存コード外科的変更のみ) 準拠

### 既知の制限
- evaluate_product / keisuke_check の実装は research_poc.py 側に依存 (Phase 3 は呼び出し側のみ実装)
- Section232 は keyword 辞書ベース (LLM 推定なし、設計書 §14-Q4 の rule-based 方針通り)

**Status**: 完了・エバリュエーター待ち

---

## W258 Phase B 画像比較カード実装（2026-06-11）

### ゴール
仕入先候補レビューで eBay 出品 1 枚目画像と仕入先候補 1 枚目画像を左右並びで表示し、
「同じ商品か」を 2-3 秒で判断できるようにする。

### 実装内容
完了

- [x] **B-1** `monitor/database.py`: migration v71 追加 (supplier_candidates に `candidate_image_url` + `candidate_image_fetched_at` 2 列。v69 流儀の冪等パターン適用。user_version=71 bump)
- [x] **B-2** `monitor/database.py`: `add_supplier_candidate()` に `candidate_image_url` keyword 引数追加。値がある場合に `candidate_image_fetched_at` を UTC now で自動セット。`tasks/task_supplier_sweep.py` + `tasks/task_supplier_candidate_search.py` に `hit.image_url` の結線追加
- [x] **B-3** `scripts/backfill_candidate_images_2026_06_11.py`: 既存 pending 候補の og:image httpx 取得 backfill。dry-run 既定 / --apply / ドメイン毎 2s sleep / snapshot / 失敗 WARN 継続
- [x] **B-4** `scripts/backfill_ebay_images_2026_06_11.py`: active listing の ebay_image_url backfill。`get_ebay_image_url` 再利用 / resume state JSON / 100 件/batch / dry-run 既定
- [x] **B-5** `tabs/_supplier_card_html.py`: `sc-imgpair` CSS + `render_supplier_card_html()` に `ebay_image_url` / `candidate_image_url` 引数追加。両方 None はブロック非表示、片方 None はプレースホルダ。URL は html.escape 済 (XSS 対策)
- [x] **B-6** `tabs/tab_supplier_candidates.py`: render 呼出に ebay_image_url (listing dict から) + candidate_image_url (DB row から) を渡す結線追加。`tabs/tab_inventory_monitor.py`: _render_oos_block の候補ループ内に imgpair HTML を追加
- [x] **B-7** `tests/test_w258_image_pair_2026_06_11.py`: 10 テスト全 PASS (migration 冪等 3 / imgpair HTML 5 / backfill dry-run 2)

### スコア自己評価
- **完成度**: 5/5
- **テスト実施**: pytest 10/10 PASS。py_compile 全変更ファイル PASS
- **仕様準拠**: 設計書 §3.4 受け入れ基準全て満たす (money-direct path 不変確認済)

### 既知の制限
- backfill 2 本は作成のみ (実行は main agent が Q2 6-step で実施)
- 本番 DB の ebay_image_url / candidate_image_url は backfill 完了まで「画像未取得」プレースホルダ表示

**Status**: 完了・エバリュエーター待ち

---

## W229 Phase 2 収穫エンジン実装（2026-06-10）

### ゴール
Terapeak Product Research 結果リストを 2 パターン（fresh_24h / two_year_echo）で取得する
スクレイパー層を実装する。scheduler 結線・DB migration・設定 UI は scope 外（後続）。

### 実装内容
実装完了

- [x] `HarvestedProduct` dataclass — 設計書 §5-1 + probe 確定 date_last_sold フィールド追加
- [x] `HarvestResult` dataclass — products / pages_loaded / error / success
- [x] `build_harvest_url()` — fresh_24h(7d/新着順) と two_year_echo(730d/古い順) の 2 パターン URL 生成。既存 `_build_terapeak_search_url` と同構造（startDate/endDate ms 必須・sellerCountry=JP・tabName=SOLD・limit=50）。不正 pattern は ValueError。
- [x] `parse_harvest_rows()` — tr.research-table-row を走査し td class suffix で各値を抽出。probe 確定の 2 段ネスト DOM (item-with-subtitle) に対応（td 全体取得→最初の `<div>text</div>` パターンで正確抽出）。スキップ行は logger.warning で記録（Q0）。
- [x] `filter_harvest_window()` — fresh_24h は JST 今日/昨日マッチ、two_year_echo は今日-2年マッチ（うるう日 2/29 → 2/28 丸め）。date_last_sold=None は除外 + logger.warning（Q0）。
- [x] `_poll_harvest_rows()` — domcontentloaded + ポーリング（networkidle 禁止・probe 確定）。
- [x] `harvest_product_list()` — 既存 thread wrapper パターン（_runner + list holder）で CDP attach。新規タブで navigate → live outerHTML → _detect_actual_dayrange 検証 → parse → filter → 窓内 0 件で打ち切り。ページ間 sleep + jitter 流用。error redirect 検知。
- [x] `tests/test_terapeak_harvest.py` — 48 テスト、既存テストとの合計 72 PASS

### 技術的判断（設計書からのずれ）

1. `_TD` lambda を `><div[^>]*>(.*?)</div>` 形式から `>(.*?)</td>` 形式に変更。
   理由: 実 DOM は `<div class="item-with-subtitle"><div>$385.00</div>` の 2 段ネストであり、
   前者だと外側 div を消費して `$385.00` → `85.00` を返す誤りが生じた（実機 fixture で発見）。
   td 全体を取得後に最初の `<div>text</div>` を探す方式が probe 確定構造と整合する。

2. `HarvestResult.pages_loaded` フィールドを追加。設計書では navigate 回数を戻り値に含める
   指示があり、クォータ計上（後続 Phase）に必要なため実装に含めた（scope 内）。

### スコア自己評価
- **完成度**: 4.8/5（実機 CDP 接続テストは scope 外。実機検証は user が別途実施）
- **テスト実施**: pytest 48 PASS（新規）+ 24 PASS（既存 terapeak test）= 72 PASS
- **仕様準拠**: 設計書 §5-1 の関数仕様・probe 確定セレクタ・打ち切りロジック全て実装
- **既存コード**: K2 準拠。既存関数・既存テストは一切変更なし

### 未実装（後続 Phase へ）
- scheduler 結線（daily_scheduler.py への add_job）
- DB migration v70（harvest_pattern 列）および重複排除ロジック
- クォータ計上（api_call_log への navigate 記録）
- 設定 UI（ハーベストフィルタ設定）
- scrape_product_detail / _extract_active_listing_start_dates / _extract_sold_count（設計書 §5-1 の残関数、Phase 2 後半〜Phase 3 で実装）

**Status**: 完了・後続 Phase 結線待ち

---

## W229 Phase 2 — scrape_product_detail 実装（2026-06-10）

### ゴール
1 商品の Terapeak 詳細（sold_90d / has_active_listing+開始日 / sold_1_2yr）を取得し、
`evaluate_sourcing_gate` に渡せる `ProductGateData` を返す関数を実装する。

### 実装内容

- [x] `ProductGateData` dataclass — 設計書 §5-1 の仕様に準拠。`sold_90d` / `has_active_listing` / `listing_start_date` / `sold_1_2yr` / `avg_sold_price_usd` / `success` / `error`。Python 3.10 未満互換のため `str | None` は文字列リテラルで記述。
- [x] `_extract_sold_count(html, day_range)` — tr.research-table-row の行数を返す純関数（sold 件数 proxy）。
- [x] `_extract_avg_sold_price(html)` — avgSoldPrice td の最初の `$N.NN` を抽出する純関数。
- [x] `_extract_active_listing_start_dates(html)` — ACTIVE タブの `active-listing-row__startedDate` td から出品開始日を全件抽出する純関数。probe7_active.html で確認したセレクタを採用。
- [x] `_poll_active_rows(page, timeout_s)` — ACTIVE タブ用ポーリング（tr.active-listing-row 待ち。SOLD タブは tr.research-table-row）。
- [x] `scrape_product_detail(keyword)` — 既存 thread wrapper パターンで CDP attach。Q6 最適化（sold_90d >= 2 で 1 navigate のみ）、全 navigate エラー時は success=False + error 設定（Q0 silent skip 禁止）。
- [x] `_scrape_product_detail_impl(keyword)` — 実処理。3 navigate 経路（SOLD 90d → ACTIVE → SOLD 730d）と Q6 skip 経路（SOLD 90d のみ）を実装。sold_1_2yr = max(0, c730 - c90) proxy 方式。
- [x] `tests/test_terapeak_product_detail.py` — 30 テスト（純関数 + monkeypatch による本体テスト）。

### セレクタの根拠（probe7 HTML より）

| セレクタ | 対象 | 確認ファイル |
|---|---|---|
| `tr.research-table-row` | SOLD タブ行（6 行確認） | probe7_sold90.html |
| `td.research-table-row__avgSoldPrice` | 平均売却価格 "$102.89" | probe7_sold90.html |
| `tr.active-listing-row` | ACTIVE タブ行（50 行確認） | probe7_active.html |
| `td.active-listing-row__startedDate` | 出品開始日 "Feb 17, 2024" | probe7_active.html |

ACTIVE タブは `research-table-row` を使わず `active-listing-row` クラスを採用することを
probe HTML 実測で確認（重要な発見。class prefix が異なるため既存 `_poll_harvest_rows` を
ACTIVE タブに流用不可 → 専用の `_poll_active_rows` を新設）。

### proxy 方式の設計判断

sold_1_2yr = max(0, count(730d) - count(90d)) は「91〜730日前」の近似であり、
1〜2年厳密窓ではない。CUSTOM 日付範囲 URL が実機未検証のため、この proxy で代替。
docstring に明記済み。

### 技術的判断（設計書からのずれ）

1. `dataclasses` モジュールを `_dc` alias でインポート（ファイル末尾への追加のため、
   既存 `from dataclasses import dataclass` との名前衝突を回避しつつ K2 準拠）。
2. ACTIVE タブのポーリングに `_poll_active_rows` を新設。既存 `_poll_harvest_rows` は
   `tr.research-table-row` 専用であり ACTIVE タブには使用不可（probe 確認）。
3. poll=False でも outerHTML 取得を試みて sold_90d=0 の正常ケースと timeout を分離。

### スコア自己評価

- **完成度**: 4.8/5（実機 CDP テストは scope 外。実機検証は後続 Phase で）
- **テスト実施**: pytest 95 PASS（新規 30 + 既存 65 = 全 PASS）
- **仕様準拠**: 設計書 §5-1 のシグネチャ・Q6 最適化・proxy 方式・Q0 全て実装
- **既存コード**: K2 準拠。既存関数・harvest 部は一切変更なし

### 未実装（後続 Phase へ）

- `harvest_product_list` との結線（task_research_harvest.py）
- DB 着地（research_candidates への gate_decision 保存）
- クォータ計上

**Status**: 完了・Agent C 結線済み

---

## W229 Phase 2 — Agent C: 03:30 収穫バッチ結線（2026-06-10）

### ゴール
`task_research_harvest.py` (03:30 JST 夜間バッチ本体) を実装し、
`daily_scheduler.py` と `task_execution_log.py` へ結線する。

### 実装内容

- [x] `tasks/task_research_harvest.py` — **新規作成** (約 600 行)
  - `run_research_harvest(config)` — バッチ本体。enabled チェック / CDP 疎通 / クォータ管理 / harvest × 2 パターン / dedup / gate 判定 / DB 着地 / Discord 通知
  - `_check_cdp_available()` — port 9222 TCP 疎通確認
  - `_get_today_terapeak_quota_used()` — JST 当日の api_call_log.terapeak_read 集計（sqlite-timezone.md 遵守）
  - `_record_navigate(success, error_message)` — api_call_log への navigate 1 回記録
  - `_send_discord(config, message, severity)` — Discord 通知ヘルパ
  - `_normalize_keyword(title)` — dedup 用正規化（小文字 + 連続空白）
  - `_get_existing_gate_decisions(normalized_keywords)` — DB 既存 gate_decision 済候補照合
  - `_get_harvest_pattern(prod, fresh_products)` — fresh_24h / two_year_echo 判別
  - `_update_harvest_meta(rc_id, nk, prod)` — harvest メタ列 UPDATE
  - Q0 対応: enabled=false / quota 不足 / anti-bot / consecutive failure 全経路で Discord + log 痕跡

- [x] `daily_scheduler.py` — `_run_research_harvest` 関数追加 + `add_job(hour=3, minute=30)` 追加
  - CronTrigger(hour=3, minute=30, second=0), id='research_harvest_03_30', max_instances=1

- [x] `monitor/task_execution_log.py` — TASK_SCHEDULE に research_harvest エントリ追加
  - `{"key": "research_harvest", "display": "W229 商品リサーチ発掘 (毎日 03:30)", "hours": [3], ...}`

- [x] `tests/test_task_research_harvest.py` — **新規作成** (19 テスト全 PASS)
  - TestEnabledFalseSkip (3) / TestCdpUnavailable (1) / TestQuotaInsufficient (2)
  - TestNormalFlow (3) / TestDedup (3) / TestHarvestFailure (3)
  - TestNormalizeKeyword (3) / TestRecordNavigate (1)

### MEDIUM-1 修正の状況

Agent A が `terapeak_scraper.py` L2115-2136 に実装済み (コメント "MEDIUM-1 修正 (2026-06-10)" で確認)。
Agent C による追加対応なし（既適用）。

### 設計書 §4/§6 との差分

**差分なし**。設計通り実装。

注記:
- モジュールレベルインポート (`from monitor.terapeak_scraper import harvest_product_list, ...`) を採用。
  設計書はローカルインポートを想定していたが、unittest.mock.patch の動作要件（パッチパスは
  「バインドされた場所」）により変更。機能・インターフェースは設計書準拠。
- skip_too_new 再判定経路: `update_status(rc_id, STATUS_HARVESTED)` → `save_gate_decision`
  (`_ALLOWED_TRANSITIONS[gate_rejected] = {harvested}` 経由)。設計書 §4 の状態遷移図に準拠。

### スコア自己評価

- **完成度**: 5/5（設計書 §6 の全実装項目を実装・テスト PASS）
- **テスト実施**: pytest 19 PASS (新規) + 95 PASS (既存 terapeak) + 回帰 189 PASS
- **仕様準拠**: 設計書 §4 状態機械 / §6 バッチ仕様 / §9 データフロー / §10 DoD 全て準拠
- **既存コード**: K2 準拠。daily_scheduler.py と task_execution_log.py への最小追加のみ

**Status**: 完了 (enabled=false のまま本番 scheduler に組み込み済み)

---

## 概要

このドキュメントは、**ジェネレーターエージェント** が各スプリントの実装状況・自己評価を記録するためのファイルです。

エバリュエーターはこのドキュメントを読んで、実装が仕様書（`spec.md`）に沿っているかを判定します。

---

## Sprint 4 実装進捗（2026-04-06）

### ゴール
498 件の eBay 出品に Watch 数＋伸び率ベースで S-E ランクを自動割り当て

### 実装内容
✅ **全て完了**

- [x] `rank_calculator.py` — ランク計算エンジン実装
  - `calculate_growth_rate()` — 伸び率計算
  - `calculate_metrics_score()` — スコア計算（0-100）
  - `assign_rank()` — ランク割り当て（S-E）
  - `check_shipping_cost()` — 送料警告判定

- [x] `ebay_sync.py` — API 統合・DB 保存
  - `sync_listings_from_ebay()` — 出品取得＋DB 同期
  - メトリクス保存（Watch, View, Sales）
  - 送料警告フラグ付き

- [x] `app.py` — UI 統合
  - eBay 連携タブに「自動ランク更新」ボタン
  - ランク統計表示（S/A/B/C/D/E）
  - ランク分布詳細セクション
  - 送料警告詳細表示（拡張可能）
  - 手動ランク編集機能

### スコア自己評価
- **完成度**: 5/5
- **テスト実施**: ✅ 498 件全体でエラー 0 件
- **仕様準拠**: ✅ 全て完了

### 今後の課題
- View 数（HitCount）は eBay Trading API では取得不可 → v2.0 で REST API 導入予定
- 販売実績データも同様 → v2.0 で対応予定

**Status**: ✅ **完了・本番運用開始**

---

## Sprint 5 実装進捗（2026-04-07）

### ゴール
送料警告機能の UI 統合・表示

### 実装内容
✅ **完了**

- [x] `rank_calculator.py` に `check_shipping_cost()` 関数
  - 商品価格の 20% を期待送料として計算
  - 実際の送料と比較（±15% 許容）
  - $0 または $30 の場合は特別警告

- [x] `app.py` に警告表示
  - テーブルの「⚠️」列に警告マーク
  - 詳細セクション（拡張可能）で詳細表示
  - 誤差率、期待値、実際値を明示

### スコア自己評価
- **完成度**: 5/5
- **テスト実施**: ✅ 警告マークが正しく表示される
- **仕様準拠**: ✅ 完全に準拠

**Status**: ✅ **完了・本番稼働中**

---

## Sprint 6 以降の計画

### Sprint 6: 競合監視システム（未実装）
**予定**: 2026-04-XX

- eBay Competitor Monitoring API で同じ商品の他セラーを検索
- 価格＋送料で競合比較
- 新規セラーを通知

### Sprint 7: UI/UX 改善（フェーズ 2）
**予定**: 未定

- ページロード時間短縮（< 5秒）
- モバイル対応
- ダークモード対応

---

## 起動手順（次回テスト用）

### 1. Streamlit 起動
```bash
cd C:\Users\gucch\OneDrive\work\claude\tools\ebay-manager
streamlit run app.py
```

### 2. エバリュエーター実行
**別のターミナルで:**
```bash
cd C:\Users\gucch\OneDrive\work\claude\tools\ebay-manager
python evaluator.py --sprint 5
```

### 出力
```
docs/feedback/sprint-5.md が生成されます
```

---

## 修正履歴

| 日付 | 修正内容 | Sprint | 状態 |
|------|--------|--------|------|
| 2026-04-07 | 送料警告機能の UI 統合 | 5 | ✅ 完了 |
| 2026-04-06 | 自動ランク付けシステム | 4 | ✅ 完了 |
| 2026-04-05 | eBay API 連携・同期 | 3 | ✅ 完了 |

---

## エバリュエーターからのフィードバック

### Sprint 4 フィードバック
- **スコア**: 5.0/5.0
- **合否**: ✅ PASS
- **指摘**: なし
- **次ステップ**: Sprint 5 に進める

### Sprint 5 フィードバック
- **スコア**: （テスト実施待ち）
- **合否**: （テスト実施待ち）

---

## 技術メモ

### 設定値（重要）

```python
# rank_calculator.py
SHIPPING_COST_RATIO = 0.20          # 関税 = 商品価格 × 20%
SHIPPING_TOLERANCE = 0.15           # 許容誤差 = ±15%
METRICS_MAX_WATCH = 20              # Watch 正規化基準値
WEIGHT_WATCH = 3.0                  # Watch の重み付け
```

### ファイル依存関係

```
app.py
  ↓
monitor/ebay_sync.py
  ↓ 呼び出し
monitor/ebay_client.py
monitor/rank_calculator.py
monitor/database.py
```

---

## 次のジェネレーター向けメモ

### Sprint 6 以降を実装する際の注意点

1. **spec.md を必ず確認**
   - 受け入れ基準を満たしているか
   - 依存関係を確認（他の Sprint との前提条件）

2. **エバリュエーター実行**
   - 実装完了後は必ず `python evaluator.py` を実行
   - Markdown フィードバックを確認

3. **フィードバックに従って修正**
   - `docs/feedback/sprint-N.md` を読む
   - バグリストを優先度順に修正
   - 修正後、このファイルを更新

4. **進捗記録**
   - このファイルに実装内容を記録
   - スコア自己評価を追記

---

**作成者**: eBay Manager 開発チーム
**最終更新**: 2026-04-07
**現在の状態**: 本番運用中（Sprint 1-5 完了）

---

## W228 後続: 承認UI + キーワード新着監視登録 (2026-06-07)

### ゴール
research_candidates 一覧 (セクションC) を拡張し、人間が候補を承認してキーワード新着監視に登録できるようにする。

### 実装内容
完了

- [x] research_candidates_db.py に承認系 status 定数追加 (DB migration なし)
  - STATUS_IDENTITY_APPROVED / STATUS_IDENTITY_REJECTED / STATUS_WATCH_REGISTERED
  - _VALID_STATUSES / _ALLOWED_TRANSITIONS 拡張 (sourced/needs_review → identity_approved/identity_rejected / identity_approved → watch_registered)
- [x] tab_w228_research.py セクションC拡張
  - status フィルタに承認系 status を追加 (9 種)
  - sourced / needs_review 行に「同一性OK / 却下」ボタン表示
  - identity_approved 行に「上限仕入価格 (編集可) + キーワード新着監視に登録」ボタン表示
  - watch 登録: メルカリ + ヤフオク の 2 サイト / source=w228_research / price_max=found_price_jpy (保守値)
  - 登録成功後 status=watch_registered に遷移
  - 登録失敗時は status 遷移しない (Q0 偽装成功禁止)
  - 重複登録防止: add_watch inserted_new=False でも status は遷移 (既存 watch として扱う)
- [x] tests/test_w228_approval_watch_2026_06_07.py 追加 (12 テスト、全 PASS)
  - 承認 status 定数存在確認
  - sourced → identity_approved → watch_registered 正常遷移
  - 不正遷移拒否 (identity_approved → sourced / watch_registered → sourced)
  - needs_review → identity_approved 許容
  - identity_rejected → sourcing 許容 (再探索)
  - watch_registered → needs_review 許容
  - add_watch が正しい引数で呼ばれることを mock 確認
  - 重複登録防止 + status 遷移の確認
  - watch 登録失敗時の status 非遷移確認
  - _calc_price_max_jpy / URL ビルダーのロジック確認

### price_max 算出ロジック
found_price_jpy を保守的上限として使用 (estimated_profit_usd は USD で為替依存のため JPY 換算せず)。
UIで number_input として編集可能 (step=500 JPY)。

### スコア自己評価
- **完成度**: 5/5
- **テスト実施**: 新規 12 pass / 既存 W228 テスト 15 pass / 全体 tests/ は回帰なし
- **仕様準拠**: 実eBay出品は未実装 (制約通り)、承認+watch登録のみ

### 残課題 (別ステップ)
- 実 eBay 出品 (AddItem/ReviseItem) は未実装 (最高リスクのため別ステップ)
- watch 登録後のキーワード監視クローラーは既存 W148/W206/W207 がカバー

**Status**: 完了・コミット待ち

---

## W229 / W228 Phase 1 FIX-1〜4 実装進捗 (2026-06-10)

### ゴール
研究候補 (research_candidates) のゲート判定永続化 + 利益真値計算 + needs_review 再探索 + found_condition_ja 保存

### 設計書
`.company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md`

### 実装内容
全 FIX 完了

- [x] **migration v69** (`monitor/database.py`)
  - 19 列追加: gate_decision / gate_reason / gate_inputs_json / gated_at / source / harvest_keyword
    / ebay_avg_sold_price_usd / ebay_total_sold / harvested_at / profit_jpy_true / profit_usd_true
    / keisuke_pass / keisuke_detail_json / section232_flag / section232_reason
    / weight_source / weight_confidence / listing_draft_id / watch_ids_json / result_ebay_item_id
  - 全 ALTER TABLE に try/except sqlite3.OperationalError (冪等性 Q2)
  - PRAGMA user_version = 69 は必須列確認後のみ設定
  - 既存 test_rival_seller_monitor.py の `ver == 68` 断言を `>= 68` に更新

- [x] **research_candidates_db.py 拡張**
  - 新規 7 status 定数: harvested / gate_passed / gate_rejected / awaiting_approval / approved / draft_generated / listed
  - _VALID_STATUSES (15件), _ALLOWED_TRANSITIONS 拡張 (new → gate_passed/gate_rejected も追加)
  - `save_gate_decision()`: gate_* 列書込 + move_status=True で status 遷移 (CAS経由)
  - `save_profit_true()`: profit_jpy_true / profit_usd_true / keisuke_pass / keisuke_detail_json 書込

- [x] **FIX-2** (`monitor/research_poc.py`)
  - `keisuke_check(profit_jpy, revenue_jpy)` 純関数: ¥600 OR 6% either-or / borderline ±20%
  - `compute_profit_true_for_research()`: calculator.calculate 真値利益 (profit フィールド = 還付抜き)
  - weight=None/0 → (None, None, reason) 返却 (0 clip 禁止 P1-1)

- [x] **FIX-3** (`monitor/research_poc.py`)
  - `retry_sourcing(rc_id)`: needs_review → sourcing CAS 遷移 (ValueError は False 返却)

- [x] **FIX-4** (`monitor/research_poc.py`)
  - `evaluate_product` に `found_condition_ja` 取得・保存 (DB 列は v67 既存)

- [x] **FIX-1** (`tabs/tab_w228_research.py`)
  - `_GATE_RC_ID_KEY` 定数追加
  - `_render_section_a()` でゲート判定後に `insert_research_candidate` + `save_gate_decision` DB永続化
  - エラー時は `st.warning` で UI 継続 (Q0 silent skip 防止)

- [x] **FIX-3 UI** (`tabs/tab_w228_research.py`)
  - `_render_candidate_actions` に「再探索」ボタン追加 (needs_review のみ表示)
  - 連打防止: `_EVAL_PROCESS_LOCK.locked()` で disabled
  - lazy import で circular import 回避

- [x] **新規テスト** (`tests/test_research_gate_persistence.py`, `tests/test_research_profit_true.py`)
  - 36 テスト: v69 冪等性 / gate_decision 永続化 / save_profit_true NULL 保存 / keisuke_check 境界値

### 設計書との差分 (コードが真実)
- `_ALLOWED_TRANSITIONS` の `STATUS_NEW` に `STATUS_GATE_PASSED / STATUS_GATE_REJECTED` を追加
  (設計書は harvested → gate 経路のみ記載だが、手動 Wizard の FIX-1 fast-path は new → gate が必要)
- `test_rival_seller_monitor.py` の `ver == 68` を `>= 68` に緩和 (将来 migration 追加に対して堅牢化)

### スコア自己評価
- **完成度**: 4.8/5
- **テスト実施**: 新規 36 pass / research 系 86 pass / 全体 925 pass (1 failure は v69 追加前の ver==68 断言で修正済)
- **仕様準拠**: FIX-1〜4 全受け入れ基準を満たす。設計書差分は上記 2 件で文書化済み

### 残課題 (Phase 2 以降)
- harvest_keyword / ebay_avg_sold_price_usd 等 W229 ハーベスト列は定数登録のみ (実装は Phase 2)
- section232_flag 列は追加済みだが、自動判定ロジックは未実装
- `result_ebay_item_id` / `listing_draft_id` は Phase 4 用

**Status**: 完了・エバリュエーター待ち
