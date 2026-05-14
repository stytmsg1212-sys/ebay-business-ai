# Anthropic Claude Code ベストプラクティス (Cal Rueb 解説) — 秘書取り込み

**動画**: 25分47秒 / 2026-04-17公開 / @ClaudeCode_love
**video_id**: x_2045043180715012311
**ジャンル**: Claude Code / システム開発系
**取り込み日**: 2026-04-26 (`feedback_video_learning_role_separation.md` のルールに従い秘書にも保存)

## 秘書視点での核心

Claude Code = **エージェント駆動の運営基盤**. CLAUDE.md / permissions / hooks / slash command を組み合わせて「Claude に毎回ゼロから説明しない仕組み」を整備すれば、秘書ルーティン (TODO 管理 / メール対応 / 部署横断連携) も自律化できる.

## 秘書ルーティンへの応用

### 1. CLAUDE.md は「秘書のマニュアル」になる
- プロジェクト規約 / 役割分離 / 部署構成は CLAUDE.md に記載 → 全セッション共通
- 秘書がユーザーから受けるタスクを部署に振り分ける際、CLAUDE.md の組織構成 + 役割分離ルールを参照
- 2026-04-26 時点で K0-K3 (Karpathy) + Q0-Q5 (絶対ルール) + 文脈管理ガイドが統合済

### 2. /clear / /compact のタイミングを秘書が判断
- 1 機能 (W番号) クローズ後 → /clear 推奨
- 同一機能の長大セッションで context drift major → /clear 強制
- session memory に context_health を毎回記録 (turn_count / last_clear / drift)

### 3. permissions allowlist で作業中断を減らす
- 安全操作 (`pytest`, `git status`, `ls`) は allow → 確認ダイアログで秘書ルーティンが止まらない
- 危険操作 (本番出品, force push, DB DROP) は deny で物理ガード
- 2026-04-26 適用済 (`~/.claude/settings.json`)

### 4. ヘッドレス SDK で夜間バッチ自動化 (将来)
- 出品スクリプト / 在庫同期 / 価格改定 cron を `claude -p` で夜間実行 → 朝に PR 確認
- 秘書の「定時実行健全性チェック」は task_execution_log + Discord 通知 (本日 #1 daily_relist 事故対応で実装済)
- ROADMAP W37 として登録予定

### 5. 複数 Claude 並列 = git worktree (将来)
- 1 Claude が出品作業中、別 Claude が価格分析、3 つ目がテスト実行 — git worktree で分離
- 秘書はこの並列状態を統括する役割 (どの worktree が何を担当か把握)
- ROADMAP W38

## red flags (秘書が user に注意喚起すべき 5 点)

1. **eBay API レート制限**: 「並列 Claude が便利」と user に勧める時、本番 API 叩きは直列キュー必須を必ず添える
2. **permissions 過剰 allow リスク**: 「allow を増やしたい」と user が言ったら、本番 API 系 (Revise/EndItem) は手動承認維持を推奨
3. **規制業務の自動化禁止**: HS コード / 関税分類 / 輸出規制 / 知財侵害判定の自動化提案は秘書が **最終責任は人間** と明示
4. **CLAUDE.md 機密漏洩防止**: 仕入先名 / 利益率 / 社内ルール記載時、`.gitignore` 戦略を秘書から user に確認
5. **半年で陳腐化**: Claude Code 機能更新が早いため、CLAUDE.md に **バージョン明記 + 四半期見直し** routine を秘書が main­tain

## 秘書 TODO (今後 7 日)

- [ ] CLAUDE.md の最終更新日明記 (現状 2026-04-26 統合) | 優先度: 中
- [ ] 四半期見直し routine を `secretary/routines/` に追加検討 | 優先度: 中 | 期限: 2026-07-26
- [ ] W37-W42 (Cal Rueb 動画派生) を ROADMAP 登録確認 | 優先度: 高
- [ ] Claude Code SDK ヘッドレス検証 (W37 着手前の調査) | 優先度: 中

## 関連 memory

- `feedback_anthropic_video_cal_rueb_takeaways.md` (詳細 7 適用案 + 5 red flags)
- `feedback_video_learning_role_separation.md` (本ファイル作成の根拠ルール)
- `learning_L3_claude_code_best_practices.md` (前回 L3 学習結果)
