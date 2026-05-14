# 2026-04-19 意思決定ログ

## 1. 運送料CSV更新（実行済）

PDF解析→差分適用で `data/ShippingRates.csv` を更新。

| ServiceID | キャリア/サービス | ゾーン | 更新 | 新規 | 平均変化率 |
|---|---|---|---|---|---|
| 11 | FedEx FICP | F (USW) | 88行 | 0 | **+4.9%**（全行一律） |
| 14 | FedEx IP Package | F (USW) | 88行 | 0 | **+5.7%**（-7.4%〜+35.2%、重量階層再編） |
| 15 | DHL 主サービス | 10 | 174行 | 1 | **+21.4%**（+21.0〜+26.4%） |

- **反映根拠**: PDF `RATE GUIDE of eBay SpeedPAK Japan Ship via {FedEx,DHL}-JP.pdf`（発効日 FedEx=2026-04-05 / DHL=2026-03-15）
- **影響**: DHL出品は運送料を20%以上少なく見積もっていた状態 → 利益計算が実態より甘かった状態を正した
- **バックアップ**: `data/ShippingRates.csv.bak.20260419_064755`
- **settings.json**: `shipping_rate_last_updated = 2026-04-19T06:48:36` を記録

## 2. 運送料PDF UI 設定タブ統合（実行済）

- `app.py` 設定タブから **旧 fuel_surcharge PDF UI を撤去**
- 新規 **運送料 PDF自動反映** セクションを追加
  - 差分プレビュー: サービス別に avg/min/max% を表示
  - **安全策**: 15%以上の変動行数を検出して警告表示
  - 詳細差分は checkbox（expander禁止ルール遵守）で opt-in 表示
- ダッシュボードに **運送料PDF更新 30日超警告** を追加（既存の燃料サーチャージ警告と並列）

## 3. daily_scheduler 再起動（実行済）

- 旧プロセス: 停止状態（前セッション中に終了した模様、Port 8502 も Listener 無）
- 新プロセス: `pythonw.exe daily_scheduler.py` (PID 8864)
- 新スケジュール **02:30 / 11:00 / 15:00 / 18:00 / 22:00** 反映済み（ログ確認済）

## 4. 仕入先候補機能 #9 基盤構築（実行済、本番ロジックは次セッション）

### DB 拡張（マイグレーション v4）
- `ebay_listings.yahoo_grace_until TIMESTAMP` カラム追加（24h猶予ルール用）
- `supplier_candidates` テーブル新設（UNIQUE制約: sku + candidate_url で重複防止）
  - match_score, match_reasoning, profitable, status(pending/accepted/rejected/applied), discovered_via

### スケルトン作成
- `tasks/task_supplier_candidate_search.py` 作成
- エントリポイント `run_supplier_candidate_search(sku, config, discovered_via)`
- `search_candidates_on_platform()` と `evaluate_candidate_with_claude()` は stub
- `MATCH_SCORE_THRESHOLD = 60`（ヒアリング閾値、採用基準）
- DB I/O・profit判定・UNIQUE抵触時のスキップは実装済

### 未着手（次セッション以降）
- Claude API 評価ロジック本体（プロンプト設計含む）
- platform 別スクレイパ（メルカリ、ヤフオク、PayPayフリマ）
- Pattern 1 async 起動（`task_inventory_check` 成功時に threading.Thread）
- Pattern 2 バッチ `task_supplier_sweep.py`
- app.py 仕入先候補タブ（採用/不採用→利益再計算→反映→SKU変換→ReviseItem）
- ヤフオク24h猶予特別ルール実装
