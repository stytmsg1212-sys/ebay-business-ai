# システム開発

## 役割
eBay物販業務の自動化ツール、スクリプト、API連携を開発・管理する。

## ルール
- 技術ドキュメントは `docs/topic-name.md`
- デバッグログは `debug-log/YYYY-MM-DD-issue-name.md`
- デバッグのステータス: open → investigating → resolved → closed
- 設計書は必ず「概要」「設計・方針」「詳細」の構成にする
- バグ修正時は「再発防止」セクションを必ず記入
- 技術的な意思決定は secretary/notes/ に意思決定ログとして残す

## フォルダ構成
- `docs/` - 技術ドキュメント・設計書
- `debug-log/` - デバッグ・バグ調査ログ

## 主幹プロジェクト: eBay Manager
- パス: `tools/ebay-manager/`
- スタック: Python 3.11 / Streamlit / SQLite (WAL) / Claude API / Gemini 2.5 Flash / Playwright MCP
- エントリ: `streamlit run app.py` (Port 8501)
- 定時実行: `daily_scheduler.py` (APScheduler, pythonw.exe バックグラウンド)

## 主要機能と担当モジュール
| 機能 | 実装ファイル | 備考 |
|---|---|---|
| 在庫監視 | `tasks/task_inventory_check.py` | 定時 05:00 / 17:00 |
| 仕入先候補探索 | `tasks/task_supplier_candidate_search.py` + `monitor/claude_evaluator.py` | Phase 1 学習機能(Few-shot)実装済 2026-04-20 |
| SKU/URL自動変換 | `monitor/database.py::build_source_url` | ebayme_/ebayyh_/ebayMS_/ebayrm_ プレフィクス |
| 日次SEOブースト | `tasks/task_daily_relist.py` | End→Relist 7件/日 |
| メール三分類 | `tasks/task_email_classify.py` | Claude Haiku で要約＋優先度 |
| 動画学習 | `monitor/gemini_video_learner.py` | Gemini 2.5 Flash, media_resolution=LOW |

## 運用ルール（現場で確立したもの）
- **pythonw.exe gotcha**: `sys.stdout.reconfigure('utf-8')` 直呼びはクラッシュする。全タスクで以下パターン必須:
  ```python
  if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
      sys.stdout.reconfigure(encoding='utf-8')
  ```
- **マイグレーション**: `monitor/database.py::init_db` に追記。番号昇順で並べる。`ALTER TABLE` は `try/except sqlite3.OperationalError` で冪等化
- **daemon thread 禁止ケース**: 手動CLI実行（Python exit で死ぬ）→ `synchronous=True` 対応
- **定時実行の健全性監視**: セッション開始時に `logs/scheduler.log` を確認、クラッシュあれば自動修復＆補完実行（feedback_scheduler_health_check.md）

## コード変更後の必須ワークフロー

⚠️ 2026-07-02 Q4 リスク 3 段階化 (正典 = `.claude/rules/00-constitution.md` Q4): money-direct=三重 (code-reviewer HIGH=0 + Codex/Fugu 2 段 + live) / 通常ロジック=code-reviewer HIGH=0 / 軽微 (文言・docs)=self-review+テスト。以下は通常ロジック (T2) 以上の手順。

1. `code-reviewer` サブエージェントでレビュー → HIGH 0件 になるまで修正
2. 大幅修正後は `code-simplifier` も検討
3. ユーザー報告は「HIGH 0件=100点」到達後

## 連携先
- `secretary/notes/` に意思決定ログ
- `research/` で得られた新機能要件を受けて設計
- `ebay-knowledge/` の eBay API 仕様を参照（Business Policies, VeRO, Trading API）

## システム開発実践応用 (動画学習由来)

詳細は `.claude/rules/karpathy-principles.md` (K0-K3) および以下 memory 参照:
- `learning_L2_claudecode_love.md` (Git Guardrails / PRD workflow)
- `learning_L3_claude_code_best_practices.md` (Build 5 段階 / Simple Thing That Works / Understand Your Tools)
- `learning_L4_nobel_824.md` (Instinct v2 継続学習)

以下は engineering dept 固有応用例。

### Build 5 段階 (新機能実装フロー)

新機能は以下 5 段階を必ず踏む:
1. 関連ファイル読込
2. 計画提案
3. TODO リスト作成
4. ユーザー確認
5. 実装

think hard 投入: マイグレーション設計 / 非同期処理 / DB 整合性系で必ず。
Understand Your Tools: sqlite3 WAL / APScheduler daemon / Claude API cache TTL の挙動を表面理解で終わらせない。

### TDD + こまめなコミット

小変更ごとにテスト・コミット、機能単位ではなく **ステップ単位**。Simple Thing That Works (過剰抽象化禁止、3 回繰り返しから共通化)。

### Git Guardrails 即導入候補

`push --force` / `reset --hard` / `clean -f` などの破壊コマンドを hook 層でブロック (`.claude/hooks/`)。単独開発だが寝落ち事故対策として価値あり。

### PRD workflow (W37〜W42 pending 機能で活用)

`Write a PRD` → `PRD to Plan` → `Grill Me` で仕様詰め。W37 メーカー監視→自動出品 / W41 個別新規出品 / W42 画像加工 は特に PRD 先行が有効。

### Instinct v2 継続学習パターン

Phase 1 学習 (Few-shot 注入) は第一歩。将来は信頼度スコアで自動調整する `brand_risk_profile` テーブルへ発展予定。

### code-reviewer 100 点フロー

1. 新規作成 / 大幅修正 → `feature-dev:code-reviewer`
2. HIGH 0 件になるまで再修正
3. MEDIUM/LOW は優先度で対応
4. レビューログは `debug-log/YYYY-MM-DD-feature-review.md` に保存

### 並行実行パターン

- 複数機能の設計を同時進行 → `feature-dev:code-architect` x N 並行
- 探索と設計を並行 → `Explore` + `code-architect` 同時起動
