# YouTube 動画学習パイプライン 実行手順

eBay コンサル動画などから学習内容を自動抽出し、リサーチ部門が活用可能な知識ベースを構築します。

---

## 🚀 クイックスタート

### Step 1: YouTube URL を提供

```bash
# 秘書に以下を依頼
秘書 Learning Pipeline:
「https://www.youtube.com/watch?v=Wfz-gdWcItM の動画から学習内容を抽出してください」
```

### Step 2: パイプラインが自動実行

```python
python youtube_processor.py
```

### Step 3: 学習内容が生成される

```
research/learning/2026-04-07-ebay-pricing-strategy.md
```

---

## 📋 ファイル構成

```
secretary/learning-pipeline/
├── youtube_processor.py        ← メイン処理スクリプト
├── README.md                   ← このファイル
└── requirements.txt            ← 依存ライブラリ

research/learning/
├── video_metadata.json         ← 処理済み動画の履歴
├── 2026-04-07-ebay-pricing-strategy.md    ← 抽出された学習内容
├── 2026-04-14-ebay-customer-service.md
└── INDEX.md                    ← 学習内容の目次
```

---

## 🔧 セットアップ

### 1. 依存ライブラリのインストール

```bash
pip install -r requirements.txt
```

### 2. 環境変数の設定

Claude API キー が必要な場合：
```bash
export ANTHROPIC_API_KEY="your-api-key-here"
```

### 3. リサーチ部門へのアクセス権限

秘書が生成したファイルをリサーチ部門が参照できるようにします：

```bash
# リサーチ部門に学習内容を通知
リサーチ Evaluator:
「新しい学習内容が利用可能です: research/learning/YYYY-MM-DD-{topic}.md」
```

---

## 📹 処理フロー

### YouTube 動画情報取得
```python
processor = YouTubeProcessor()
video_info = processor.get_video_info(url)

# 出力例:
{
  'video_id': 'Wfz-gdWcItM',
  'title': 'eBay Price Research Strategy 2024',
  'channel': 'eBay Seller Academy',
  'duration': 2540,  # 秒
}
```

### 字幕抽出
```python
subtitles = processor.extract_subtitles(video_info)

# 出力:
# 0:00 Introduction to eBay pricing
# 1:30 How to research competitor prices
# 5:00 Market analysis techniques
# ...
```

### 学習内容生成（Claude）
```python
content = processor.generate_learning_content(video_info, subtitles)

# Markdown 形式で生成：
# # eBay Price Research Strategy
# ## 概要
# ## 主要なポイント
# ## eBay ビジネスへの適用
# ...
```

### ファイル保存
```python
filepath = processor.save_learning_content(video_info, content)

# 出力: research/learning/2026-04-07-ebay-pricing-strategy.md
```

---

## 🎯 使用例

### Example 1: eBay 価格設定戦略動画

```bash
# 秘書への依頼
秘書 Learning Pipeline:
「https://www.youtube.com/watch?v=Wfz-gdWcItM から eBay 価格設定戦略の学習内容を抽出」

# 処理
→ youtube_processor.py が自動実行
→ Claude が動画内容を要約・構造化
→ research/learning/2026-04-07-ebay-pricing-strategy.md を生成

# 出力
# eBay 価格設定戦略
#
# ## 概要
# 2024年のeBayマーケットプレイスにおいて、効果的な価格設定は...
#
# ## 主要なポイント
# 1. 相場調査の3ステップ
# 2. 競合分析のコツ
# 3. 利益率の最適化
#
# ## eBay ビジネスへの適用
# - 毎週の市場調査スケジュール化
# - リサーチ部門での標準手順化
# - 出品戦略への反映
```

### Example 2: 複数の eBay 動画を週次で処理

```bash
# 毎週火曜日に実行（自動化可能）
秘書 Learning Pipeline（毎週実行）:
「以下の URL から最新の eBay コンサル動画を学習」
- https://www.youtube.com/watch?v=Wfz-gdWcItM
- https://www.youtube.com/watch?v=XXXXXXXXXXXX
- https://www.youtube.com/watch?v=YYYYYYYYYYYY

→ research/learning/YYYY-MM-DD-*.md が自動生成
→ リサーチ部門が新しい知識を活用開始
```

---

## 📊 生成される学習内容の例

```markdown
# eBay 価格設定戦略

## 動画情報
- タイトル: eBay Seller Academy - Pricing Strategies 2024
- チャンネル: eBay Seller Academy
- 長さ: 42 分 24 秒

## 概要
eBay での効果的な価格設定は売上向上の鍵。本動画では、プロセラーの視点から
実践的な価格調査方法と競合分析テクニックを解説。

## 主要なポイント

1. **相場調査の3ステップ**
   - Advanced Search で同一商品を検索
   - Sold Listings でフィルタして成約価格を確認
   - 過去30日のデータを平均値・中央値・最高値で集計

2. **競合分析のコツ**
   - TOP 5-10 のライバルセラーを特定
   - 価格だけでなく説明文・画像数・送料設定を比較
   - 差別化ポイント（新品状態、付属品、送料込み価格など）を洗出す

3. **利益率の最適化**
   - 市場相場 - 仕入原価 - 送料 - 手数料 = 純利益
   - 目標利益率 15-20% を基準に出品価格決定
   - 回転率と利益率のバランスが重要

4. **価格改定の周期**
   - 新商品は週1回の価格調査
   - 既存商品は月1回で十分
   - トレンド商品は需要変動を考慮して臨機応変に対応

## eBay ビジネスへの適用

### リサーチ部門への適用
- 新商品リサーチ時に「3ステップ相場調査」を標準手順として組み込む
- 競合分析テンプレートを作成（5社以上のデータ自動集計）
- 月次の「市場相場レポート」を生成

### 出品部門への適用
- 推奨出品価格を「相場平均 ± 5%」の範囲で自動計算
- 競合セラーより高い場合は説明文・画像での差別化を強調
- 利益率 20% 未満の商品は「見直し対象」フラグを立てる

### eBay 知識部門への適用
- 「価格設定のベストプラクティス」として知識化
- Defect 防止：適正価格での出品がカスタマー満足度向上につながることを周知

## キーワード
`eBay`, `価格設定`, `市場調査`, `競合分析`, `相場`, `利益率`, `Sold Listings`

## 関連する施策
- 毎週金曜日：市場相場レポート生成（リサーチ部門）
- 月次：利益率が低い商品の見直し（出品部門）
- 四半期ごと：eBay ポリシー変更の学習（eBay知識部門）

## スコア（品質評価）
- **内容完全性**: 4.8/5 — 実践的な事例が豊富
- **実践性**: 4.9/5 — すぐに出品戦略に反映可能
- **ビジネス価値**: 4.7/5 — 利益率向上に直結
- **総合**: 4.8/5 ✅ **高品質・即座に活用開始推奨**

---
処理日: 2026-04-07
処理エージェント: YouTube Learning Pipeline
```

---

## ✅ 品質チェック

秘書が生成した学習内容は、以下の基準で自動評価されます：

| 基準 | 目標 | 合格条件 |
|------|------|---------|
| ポイント数 | 5個以上 | 4個以上 |
| 実践性 | 「すぐに実装可能」 | スコア 4.0/5 以上 |
| ビジネス価値 | 売上向上に貢献 | スコア 4.0/5 以上 |
| 完全性 | 動画内容を網羅 | スコア 4.0/5 以上 |

合格した学習内容は、リサーチ部門へ即座に通知されます。

---

## 🔄 定期運用

### 週次処理（毎週火曜日）
```
秘書 Learning Pipeline: 「新しい eBay 動画を処理してください」
  ↓
youtube_processor.py 実行
  ↓
research/learning/YYYY-MM-DD-*.md 生成
  ↓
リサーチ部門 Evaluator が品質確認
  ↓
✅ 合格 → リサーチ Generator が活用開始
```

### 月次総括（毎月末）
```
research/learning/INDEX.md を更新

## 月間学習内容
- [2026-04-07] eBay 価格設定戦略
- [2026-04-14] eBay カスタマーサービス
- [2026-04-21] eBay 配送最適化
- ...
```

---

## 🆘 トラブルシューティング

### Q: 字幕が取得できない場合
A: `extract_subtitles()` が自動的に動画説明欄を参照します。説明が詳細でない場合は、Claude が推論で補完します。

### Q: Claude API が使えない場合
A: テンプレート生成モードで Markdown を自動作成します。ユーザーが手動で内容を記入することで対応可能。

### Q: 処理時間が長い場合
A: YouTube メタデータ取得に 10-30 秒、Claude 処理に 30-60 秒かかります。複数動画を処理する場合は、並列実行は避け、順序実行をお勧めします。

---

## 📚 参考資料

- [eBay Seller Academy](https://www.youtube.com/channel/UCvRjdJZEYSdwqJ4pNRSCYLw)
- [yt_dlp Documentation](https://github.com/yt-dlp/yt-dlp)
- [Claude API Documentation](https://docs.anthropic.com/)

---

**更新日**: 2026-04-07
**バージョン**: 1.0
**ステータス**: ✅ 本格運用準備完了

