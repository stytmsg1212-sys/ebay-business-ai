# Anthropic「Code execution with MCP」読解ノート (2026-06-11)

- 出典: https://www.anthropic.com/engineering/code-execution-with-mcp (公開 2025-11-04)
- 取得経路: Web 抜粋が JavaScript 無効化で取得不能 → user 提供 PDF (Downloads/Code execution with MCP_...pdf) を全文読解
- 関連: W209 ニュース AI アクション項目

## 記事の核心 (3 行)

1. MCP ツールを「直接ツール呼び出し」で使うと、全ツール定義の事前ロード + 中間結果の往復で context を浪費する (数千ツール接続なら読み出し前に数十万 token)
2. 解決策 = **MCP サーバをコード API として提示し、エージェントがコードを書いて呼ぶ** (例: `./servers/google-drive/getDocument.ts` のようなファイルツリー化)。必要なツール定義だけ on-demand で読む → 実測 150,000 → 2,000 token (98.7% 削減)
3. 中間データ (例: 1 万行スプレッドシート) は実行環境内で filter/transform してから返す → モデルは 5 行だけ見る。大データの「コピー転記ミス」も構造的に消える

## 主な利点 (記事の整理)

| 利点 | 内容 |
|---|---|
| Progressive disclosure | ツール定義をファイルシステム探索で必要分だけロード。`search_tools` (detail level 付き) でも可 |
| 結果の context 効率 | filter/集計/join を実行環境内で済ませ、要約だけモデルへ |
| 制御フロー | loop/条件分岐/polling をコードで実行 (agent loop で sleep を往復するより速くて安い) |
| プライバシー | 中間データは実行環境に留まる。harness が PII を tokenize ([EMAIL_1] 等) し、実データはモデルを通らず A→B へ流せる |
| 状態永続化 + Skills | 中間結果をファイル保存して resume / 動いたコードを `./skills/` に関数として保存 → SKILL.md を付けて再利用資産化 |

注意点: エージェント生成コードの実行には sandbox / リソース制限 / 監視が必要 = 運用コスト増。直接ツール呼び出しの単純さとのトレードオフ。

## 本プロジェクトへの適用評価

- **MonoDeck / scheduler は既にこのパターン**: 定時 task は Python コードが API を直接叩き、結果は DB/ファイルに保存、モデルには要約だけ渡す構造。記事の主張を裏付ける現行設計で、移行不要
- **Claude Code 本体も実装済み**: ToolSearch (deferred tool の on-demand ロード = progressive disclosure) / Workflow (コードで agent をオーケストレーション) / Skills がまさにこれ
- **効く可能性がある箇所**: claude.ai 相談エージェント (2026-06-02 構築、リモート MCP) にツールを増やす場合は、直接ツール呼び出しの token 膨張に注意。ツール数が 2 桁になったら code-API 化 or search_tools 方式を検討
- **新規アクション: 今は無し** (現行構成で問題が出ていないため、K1 Simplicity First で様子見)
