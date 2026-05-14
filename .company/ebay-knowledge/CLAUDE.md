# eBay知識

## 役割
eBayポリシー、規約、出品ノウハウ、トラブル対応の知識を蓄積・管理する。

## ルール
- 調査・学びは `topics/topic-name.md` に蓄積
- eBayポリシー変更情報は日付付きで記録
- VeROプログラム（知的財産権）関連情報は必ず記録
- トラブル事例と対応策を蓄積してナレッジベース化
- 出品ノウハウ（タイトル最適化・説明文・写真・価格設定）を体系化

## フォルダ構成
- `topics/` - ポリシー・ノウハウ・トラブル対応（1トピック1ファイル）

## 蓄積トピック（主要）
- `topics/speedpak-economy-rates.md` - 送料体系
- `topics/operation-rules.md` - 運用ルール全般

## API & ツール実装メモ
- **Trading API**: `EndItem` / `RelistFixedPriceItem` / `VerifyRelistItem` / `ReviseItem` / `AddItem`
- **Business Policies vs ShippingServiceCost override**: ベース Business Policy を持ちつつ個別上書きで柔軟性
- **SEOブースト**: 日次 End→Relist 7件 (`tasks/task_daily_relist.py`)
- **VerifyRelistFixedPriceItem は非サポート** → `VerifyRelistItem` を使う
- `INSERT OR IGNORE` はサイレント失敗しがち → `cur.rowcount == 0` をチェックして警告ログ

## 関税時代区分 (2025-10 デミニミス撤廃)
すべての送料・価格設定は 日付考慮必須:
- pre_tariff (~2025-09) / transition (2025-10) / post_tariff (2025-11~)
- 古いノウハウ投稿・動画を参照するときは必ず記載日付を確認すること

## VeRO (知的財産権) 注意
- 高リスクブランド: Disney, Nintendo, Sony ゲーム, Apple, Adobe, Lego
- 禁止商品リストは常に確認

## トラブル対応知識（現場で確立）
- **出品失敗 FailureType=Warning** → 警告だが出品は成立
- **SKU 変更時は `monitored_items.ebay_item_id` も同一トランザクションで更新**
- **daemon thread が pythonw.exe で死ぬ** → foreground 実行モード用意

## 学び運用
- eBayポリシー変更情報は日付付きで `topics/YYYY-MM-DD-policy-change.md`
- トラブルは `topics/trouble-案件名.md` に原因・対策・再発防止
- 新情報取得時は secretary/notes/ にも意思決定/学びとして追記

## eBay知識実践応用 (動画学習由来)

詳細は `.claude/rules/karpathy-principles.md` (K0-K3) および以下 memory 参照:
- `learning_L1_hayattiq.md` (Context Pack 3 層)
- `learning_L2_claudecode_love.md` (PRD workflow)
- `learning_L4_nobel_824.md` (183 skills / Skill 化)

以下は ebay-knowledge dept 固有応用例。

### Context Pack 3 層 (ポリシー調査)

ポリシー記事は 3 層で保存:
- **一次**: eBay 公式発表 / API Release Notes / Seller Hub 告知
- **反論**: フォーラムでのセラー苦情・対抗解釈
- **最新日付**: 発効日 / 遡及適用有無

think hard 投入: VeRO グレー事例 / ブランド抜け道商品 / 関税時代境界の扱い。

### CLAUDE.md shared context

部署 CLAUDE.md にポリシー要点インデックスを保持、詳細は `topics/*.md` に分離。Understand Your Tools (eBay API の挙動を表面理解で終わらせず、バージョン・廃止予定・レート制限まで押さえる)。

### Skill 化候補 (繰り返しトラブル対応)

183 skills 方式に倣い、繰り返し発生するトラブル対応を Skill 化:
- `vero-risk-check` / `defect-dispute-template` / `policy-diff-monitor`

繰り返し発生する問い合わせは Skill 化して即応可能に。

### PRD workflow (新ポリシー対応)

新ポリシー対応手順は「PRD → Plan → Implement」で体系化。`topics/PRD-2026-xx-policy-response.md` テンプレ整備予定。

### 並行実行パターン

ポリシー更新時は影響範囲分析を複数部署向けに並行生成:
- daily-operations 影響 / engineering 影響 / finance 影響 をエージェント x3 並行

### 関税時代区分のセルフチェック

新規調査時、記事日付を確認せずに採用するのは禁止。`pre_tariff` のノウハウを `post_tariff` に転用する場合は必ず再検証。
