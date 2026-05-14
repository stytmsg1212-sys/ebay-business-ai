# 自動ランク付けシステム実装完了（2026-04-06）

## 実装内容

### Context
ユーザーの498個のeBay出品を効率的に管理するため、Watch数・View数・販売数・伸び率をベースに自動ランク付けシステムを実装。

### 決定された実装方針
**v1.0（当面）**: 実装可能な方式で即時運用開始
- 優先度：View数（最重視）> Watch数 > 伸び率 > 販売数
- ランク体系：S-A-B-C-D-E（S≥90, A≥75, B≥60, C≥45, D≥30, E<30）
- View数を2倍、Watch数1倍、販売数1倍、伸び率0.5倍で加重合算

### 実装フェーズ

#### Phase 1: データベーススキーマ拡張 ✅
**File**: `monitor/database.py`
- 新しいカラム追加（マイグレーション実装）：
  - メトリクス（現在値）: `watch_count`, `view_count`, `sales_count_30d`
  - メトリクス（前回値）: `last_watch_count`, `last_view_count`, `last_sales_count_30d`
  - 伸び率（計算済み）: `watch_growth_rate`, `view_growth_rate`, `sales_growth_rate`
  - スコア: `metrics_score`, `last_metrics_updated_at`
- ALTER TABLE でidempotent対応（既存カラムはスキップ）

#### Phase 2: eBay APIデータ抽出拡張 ✅
**File**: `monitor/ebay_client.py`
- `get_active_listings()` を拡張
- 新フィールド抽出：
  - `WatchCount` → `watch_count`
  - `HitCount` → `view_count`
  - `QuantitySold` → `sales_count_30d`（取得不可時は0）

#### Phase 3: ランク計算ロジック実装 ✅
**New File**: `monitor/rank_calculator.py`
- `calculate_growth_rate()`: 伸び率計算（前回値0の場合は100%と扱う）
- `calculate_metrics_score()`: 複合スコア計算（0-100正規化）
  - 各メトリクスを最大値に基づいて0-100に正規化後、加重合算
  - View: 2倍, Watch: 1倍, 販売数: 1倍, View伸び率: 0.5倍, Watch伸び率: 0.3倍
- `assign_rank()`: スコアを固定スコア方式でランク割り当て
- `auto_rank_all_listings()`: 全出品の一括ランク計算
- `get_rank_stats_from_details()`: ランク別統計生成

#### Phase 4&5: eBay同期・ランク計算統合 ✅
**File**: `monitor/ebay_sync.py`
- `sync_listings_from_ebay()` を拡張：メトリクス保存処理を追加
- 新関数 `auto_rank_all_listings_in_db()`：
  - 全出品のメトリクスを取得
  - 伸び率を計算・DB保存
  - スコア・ランクを計算・DB保存
  - ランク分布詳細を返却

#### Phase 6: UI統合 ✅
**File**: `app.py`
- インポート拡張：`auto_rank_all_listings_in_db`, `get_rank_distribution_details`
- `rank_to_stars()` 更新：Sランク対応（✨✨✨✨✨ 最優先）
- UI要素追加：
  1. **⚡ 自動ランク更新ボタン**（ebay_col2）
     - 全出品を最新メトリクス再評価
     - 進捗表示
     - 完了後 UI自動更新

  2. **ランク統計拡張**（7カラム表示）
     - S/A/B/C/D/Eを各別表示

  3. **ランク分布詳細（expandable）**
     - ランク別の統計情報：
       - 件数
       - 平均Watch/View/販売数
       - 平均伸び率（Watch/View）

  4. **ランク編集セクション更新**
     - Sランク選択可能に
     - キャプション更新：S→A→B→C→D→E順

#### Phase 7: 既存ランク関数のSランク対応 ✅
**File**: `monitor/database.py`
- `get_rank_stats()`: Sランク追加
- `update_ebay_listing_rank()`: バリデーション更新（S,A,B,C,D,E）
- `get_ebay_listings_by_rank()`: ORDER BY CASE に S→0 追加
- `get_rank_distribution_details()`: 既にSランク対応

---

## 技術的な特徴

### データの伸び率計算
- **初回実行時**: 前回値がNULLの場合、現在値が存在すれば100%成長と扱う
- **複数回実行**: 前回値を自動更新。次回実行時の比較ベースになる
- **ゼロ除算対策**: `max(previous, 1)` で0分割を回避

### メトリクスの正規化
- 各メトリクスを設定された「最大値」（View:200, Watch:50, Sales:5）に基づいて0-100スケールに変換
- スケール外の高い値は上限100で固定

### スコア計算と境界値
```
normalized_score = (
    view 2.0倍 + watch 1.0倍 + sales 1.0倍 +
    view_growth 0.5倍 + watch_growth 0.3倍
) / (total_weight * 100) * 100

ランク境界：
- S >= 90
- A >= 75
- B >= 60
- C >= 45
- D >= 30
- E < 30
```

---

## 今後の拡張（v1.5, v2.0）

### v1.5（2-3週間後）
- 利益率データが蓄積されたら、スコア計算に追加
- `score × (利益率指数 0.5-2.0)` で加重

### v2.0（1-2ヶ月後）
- 販売実績データ（source inventory）からの過去30日販売数取得
- 競合数（detect_new_competitors）をランク判定に組込
- KPI統合：売上・利益率・販売数量・Defect率低減・競合対応

---

## 検証状況

✅ **構文チェック**: Python 全ファイル OK
✅ **インポート関係**: 新関数は全て正しくインポート
✅ **DB マイグレーション**: idempotent設計（既存DBとの互換性維持）
✅ **UIボタン動作**: 自動ランク更新 → 計算 → 表示フロー統合

---

## ユーザーへの使用手順

1. **初回**: 「🔄 eBay出品取得・同期」でメトリクスを取得・保存
2. **初回以降**: 「⚡ 自動ランク更新」でランク自動計算
3. **詳細確認**: 「📊 ランク分布詳細」でランク別統計を確認
4. **手動調整**: 「🔧 ランクを手動編集する」で個別ランク修正可能

---

## 記録
- **実装開始**: 2026-04-06
- **実装完了**: 2026-04-06
- **テスト状態**: 構文チェック完了、運用テスト待機
