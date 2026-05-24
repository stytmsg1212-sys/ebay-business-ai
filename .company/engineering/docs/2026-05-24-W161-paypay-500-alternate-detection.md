---
name: w161-paypay-500-alternate-detection
description: paypay フリマ仕入先 site の構造的 HTTP 500 error で在庫判定不能の 14 件 (22 件中 64%) に対し、headless ブラウザ or 代替経路で在庫判定を恒久化する設計
layer: wiki
updated: 2026-05-24
revision: v1 (Phase 4 設計、実装は別 session で着手予定)
genre: supplier-detection
metadata:
  type: design
  wiki_type: synthesis
---

# W161 paypay フリマ 在庫判定 代替検出方法 設計書

## 1. 業務背景

paypay フリマ (paypayfleamarket.yahoo.co.jp) は **SPA で構造的 HTTP 500 を返す** ケースが多発。現状の `monitor/paypay_search.py` は requests/httpx 経由で HTML を取得しているが、JS 未実行 = 商品情報取れず HTTP 500 (or 5xx) で last_status='不明' に蓄積。

**現状 5/24 時点のスコープ**:
- monitored_items 中 paypay 関連 = **22 件**
- last_check 直近 30 日: 不明 **14 件 (64%)** / 在庫有 6 / ページなし 2
- 「不明」の listing は inventory_check で `source_status` 判定不能 → W139 ensure_monitor_coverage が補完できない silent gap (履行不能 risk)

5/21 PM session memory 記載「OOS retry 168 件 (auto_rejected 1 / paypay HTTP 500 構造的 error 63 件 = retry 不能)」の延長案件。OOS retry queue が 14 件まで減少しているが、構造的 problem は未解消。

## 2. 問題分析

### 2.1 paypay 500 の根本原因 (推定)

paypay フリマ site は商品ページが SPA (React or similar) で render される。requests/httpx での GET は HTML shell のみ取得し、商品情報は JS 実行後に Ajax 経由で構成される。一部の商品 URL では server-side rendering が機能せず HTTP 500 を返す挙動が確認されている (バックエンド API の不安定性 or rate limit)。

### 2.2 影響範囲

- inventory_check の status 不明: 14 listing
- W139 ensure_monitor_coverage は last_status をベースに判定 → 不明状態は coverage 計算から除外 = 履行不能 listing の発見遅延
- supplier_sweep (朝 02:30 batch) で paypay sku の長期 OOS 判定が遅延

### 2.3 既存 monitor/paypay_search.py 調査結果

(別途 1 次情報照合必須、Phase 6 実装時)

## 3. 代替検出方法 (3 案)

### 案 A: Playwright headless ブラウザでレンダー後 DOM 取得 (推奨)

- inventory_checker_selenium.py / Playwright 既存 infrastructure 活用
- paypay 用に専用 inventory_checker_paypay.py 新設 (~150 LOC)
- JS render 完了待ち (商品情報 selector が出るまで) + 在庫 / OOS 判定
- pros: 確実性高い、site 改修にも追従しやすい
- cons: speed/cost (1 listing 5-15 秒、headless browser リソース)、Selenium / Playwright 依存

### 案 B: 公式 PayPay API / Yahoo Shopping API 利用

- paypay フリマは Yahoo グループ → 公式 Shopping API があるか調査
- pros: API ベースなら安定 / 高速 / 大規模 OK
- cons: paypay フリマ商品が公式 Shopping API に出るか不明 (確認必須、別途 R&D)

### 案 C: 代替経路 (Yahoo Auction / Mercari の同一商品) 検索 fallback

- paypay 500 の listing の title / SKU で Yahoo Auction / Mercari を検索
- 同一商品が出れば in_stock 判定の代替 (paypay 在庫の正確値ではないが「市場に存在する」signal)
- pros: 実装軽い、既存 yahoo_search / mercari_search infrastructure 流用可
- cons: paypay 在庫 ≠ 他 site 在庫、誤判定 risk

## 4. 採用案

**案 A (Playwright headless) を main path に採用**、案 C を fallback として併用 (案 A 失敗時に代替 site 検索 → 結果 unknown でも「市場存在」signal を確認)。

## 5. 実装スコープ (Phase 6 想定)

### 新規 file
- `monitor/inventory_checker_paypay.py` (~150 LOC): Playwright headless 経路の paypay 専用 checker
- `tests/test_inventory_checker_paypay.py` (~80 LOC): mock 経由 unit test

### 修正 file
- `monitor/paypay_search.py`: 500 受信時に Playwright checker への fallback 経路追加
- `tasks/task_inventory_check.py`: paypay sku で 500 / 不明 が出たら新 checker を呼ぶ条件分岐
- `tasks/task_supplier_sweep.py`: 同上 (供給スイープ時の paypay 救済)

### 既存 cache / DB
- `monitored_items.last_status`: 「不明」のまま (Playwright 経路で取れたら更新)
- `inventory_check_results.json`: 既存 schema 流用

## 6. リスク分析

- **Playwright headless リソース**: 1 listing 5-15 秒 = 22 件で 2-5 分追加。daily inventory_check 02:30 batch の全体時間に影響あり (現状 ~10-15 分 → ~15-20 分)
- **paypay site の更なる改修**: site UI が変わると selector が壊れる → 4 半期に 1 回 selector レビューが必要 (運用負担)
- **法的 / 規約 risk**: paypay フリマの robots.txt / 利用規約で headless scraping が禁止されている場合 → 別 W で確認必須

## 7. Q1 DoD

1. pytest test_inventory_checker_paypay 80 LOC PASS
2. Playwright headless で 14 件 paypay listing をすべて成功取得 (実機 verify)
3. monitored_items.last_status の「不明」14 件 → 「在庫有」/「在庫無」/「ページなし」のいずれかに収束
4. W139 ensure_monitor_coverage の coverage 計算が改善 (履行不能 risk 減)
5. inventory_check 全体時間 < 25 分維持 (regression なきこと)

## 8. ビルドシーケンス (Phase 6 実装順序、別 session で着手)

1. Step 1: paypay フリマの robots.txt / 利用規約確認 (別 W candidate or skip 判断)
2. Step 2: 公式 Yahoo Shopping API 調査 (案 B 可否確認、不可なら案 A confirm)
3. Step 3: monitor/inventory_checker_paypay.py 新規実装 (Playwright headless)
4. Step 4: test_inventory_checker_paypay.py 80 LOC
5. Step 5: paypay_search.py / task_inventory_check.py / task_supplier_sweep.py に fallback 統合
6. Step 6: code-reviewer Opus 4.7 HIGH=0 + Codex GPT-5.5 2 段 review
7. Step 7: Q1 DoD 実機 verify (Playwright で 14 件全て status 収束確認)
8. Step 8: commit + ROADMAP W161 完了

## 9. 質問リスト (Phase 5 で確認したい)

- Q-1: 案 A の Playwright headless 経路を採用するか? (cost: ~5 分追加、本セッションでは設計のみ起票)
- Q-2: 公式 Yahoo Shopping API 調査 (案 B) を別 W としても起票するか?
- Q-3: paypay site の利用規約確認は別 W か本 W に含めるか?

## 10. 完了報告テンプレ (Q5)

```
- 使用モデル: Opus 4.7 (設計) / Sonnet 4.6 (実装) / Haiku 4.5 (test 追加)
- 検証経路: pytest unit (80) / Playwright 実機で 14 件 status 収束確認 / DB SELECT
- 実機ログ: logs/scheduler.log で inventory_check duration 比較 (15→20 分以内)
- 残リスク: paypay site 改修への追従コスト / 法的 risk (規約確認結果次第)
```

---

*本設計書は Phase 4 成果物。実装 (Phase 6) は別 session で着手予定。本 W は前 session 5/21 PM で発見された OOS retry 168 件中 63 件 paypay 500 error の延長案件、5/24 時点で残 14 件 (64%) に影響。*
