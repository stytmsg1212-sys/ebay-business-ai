# Anthropic Claude Code ベストプラクティス (Cal Rueb 解説) — リサーチ脳取り込み

**動画**: 25分47秒 / 2026-04-17公開 / @ClaudeCode_love
**video_id**: x_2045043180715012311
**Opus 4.7 深掘り済** ($0.19) — DB videos_learned + knowledge_index 33 件登録
**ジャンル**: Claude Code / システム開発系 (= research + secretary 両方取り込み対象)

## 核心 (core_lesson)

Claude Code は単なるコーディング補助ではなく、**CLAUDE.md コンテキスト永続化 + 権限自動承認 + ヘッドレス SDK 実行** を組み合わせた **エージェント駆動の自動化基盤**。MonoHonpo の eBay 出品〜仕入〜価格改定〜通関対応の全パイプラインに適用可能。

最重要原則: **「Claude に毎回ゼロから説明しない仕組み」** (CLAUDE.md + slash command + 計画→TDD→コミット のループ)

## MonoHonpo 適用 7 案 (research 視点)

### A. 出品系 (Item Specifics / タイトル生成)
- **マルチモーダル出品**: 仕入候補スクショ + 競合 eBay listing 画像を同時投入 → タイトル/Item Specifics/カテゴリ一括生成 (W9 既存を強化、ROADMAP W39)

### B. 価格系 (TDD 改定)
- **価格改定ロジック TDD**: 期待利益率テスト先行 → Claude 実装 → pass → commit. 為替・eBay 手数料・送料テーブル変更で活用 (ROADMAP W40)

### C. 通関系 (関税/HS判定)
- HS コード判定 / 禁制品チェックは Claude に作らせる際、`/clear` で前タスク context 切る + CLAUDE.md に **「2025 関税改定の影響範囲」** 明記してから着手
- 規制業務は **Claude 出力の自動採用禁止**. 人間が最終責任 (red flag #3)

### D. リサーチ脳 (Research) との直結
- 動画 cross_video_links: subagents/multi-agent オーケストレーション → **MonoHonpo Research脳/出品脳/価格脳/通関脳 の責務分離設計** と完全一致
- MCP eBay API 連携 → Trading API/在庫DB/為替API を MCP 化して Claude 直接呼出 (ROADMAP W41)
- プロンプトキャッシュ + Opus/Sonnet 使い分け → 月次 API コスト削減 (ROADMAP W42)

## red flags (eBay 業務固有 5 件)

1. **eBay API レート制限**: 並列 Claude 推奨だが、Trading API はアカウント単位制限. **本番 API 叩きは直列キュー必須**
2. **permissions 過剰 allow → 誤本番出品**: Revise/EndItem 系は手動承認維持、dry-run 既定
3. **規制業務の最終責任は人間**: HS コード/輸出規制/知財侵害判定の自動採用は重大リスク
4. **CLAUDE.md 機密漏洩**: 仕入先名/利益率/社内ルール記載時の `.gitignore` 戦略
5. **Claude Code 機能更新の早さ**: 半年で陳腐化するため CLAUDE.md にバージョン明記 + 四半期見直し

## 既存実装との関係

| 動画提案 | MonoHonpo 現状 |
|---------|----------------|
| CLAUDE.md コンテキスト永続化 | ✅ 完備 (2026-04-26 K0-K3 + Q0-Q5 統合) |
| permissions allowlist | ✅ 完備 (2026-04-26 適用) |
| ヘッドレス SDK CI/CD | ⏳ W37 |
| git worktree 並列 | ⏳ W38 |
| マルチモーダル出品強化 | ⏳ W39 (W9 拡張) |
| 価格改定 TDD | ⏳ W40 |
| MCP eBay API 連携 | ⏳ W41 |
| プロンプトキャッシュ運用 | ⏳ W42 |

## 詳細 (memory feedback)

`feedback_anthropic_video_cal_rueb_takeaways.md` を参照. enriched_keywords 15 件で knowledge_index 検索可能.
