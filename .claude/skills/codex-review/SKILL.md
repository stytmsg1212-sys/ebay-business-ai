# Codex Review Skill (`/codex-review`)

`/codex-review <path>` で起動する slash command. user が任意の memory / KB / 設計書を **Codex CLI (GPT-5.3) で外部 lint** したい時に発火.

## トリガ

以下のいずれかで本 skill が呼ばれる:

- user が `/codex-review <path>` と入力
- user が「Codex で <path> をレビューして」「<path> に lint」と発話
- assistant が `feedback_codex_review_usage.md` の自発提案 5 場面で起動

## 引数

| 引数 | 必須 | 説明 |
|---|---|---|
| `<path>` | 必須 | lint 対象 file path (memory dir 相対 or 絶対). 複数指定可 (空白区切り). 省略時は「直近 24h 編集 file」を自動選択 |

## 実施手順

### Step 1: 対象 file 確定

引数が無ければ `codex_lint_runner.list_recently_edited_files(since_hours=24)` で直近 24h 編集 file を自動選択.

### Step 2: Codex lint 起動

```python
from tools.ebay-manager.monitor.codex_lint_runner import (
    run_codex_lint, detect_cascade_gaps, summarize_findings
)

findings = run_codex_lint(
    target_files=<path_list>,
    output_jsonl=f"data/codex_lint_log/{today}-lint.jsonl",
)
cascade = detect_cascade_gaps(recent_hours=24)
all_findings = findings + cascade
summary = summarize_findings(all_findings)
```

### Step 3: 2 段ループ (Claude 再評価)

`codex-reviewer` subagent を delegate するか、main agent 自身が:

1. 各 finding を実コード grep / 公式 web 確認 / cross-file 照合で検証
2. hallucination 候補を flag (例: 「eBay XML field 名違い」は実コード grep で却下)
3. accept / partial / reject を確定

### Step 4: user に提示

`feedback_explain_in_plain_language.md` の規範形式で報告:

```markdown
## Codex Lint 結果

Codex が <file> を読んで **N 件の指摘** を出しました.

| # | 場所 | 何が問題か | 業務的に効くか |
|---|---|---|---|
| 1 | reference_X.md L42 | "Section 232 25%" 残存 (現在 30%) | 関税計算 1 商品 $30 ズレ、要修正 |
| 2 | ... | ... | ... |

**Claude 再評価**: <accept N / reject M / hallucination H>

修正していい? それとも一旦保留?
```

### Step 5: user 承認後の修正

user が「修正」「直して」「OK」等で承認したら、main agent が Edit tool で修正実行. cascade-update.md 適用で関連 file も同時更新.

## 副作用

- `data/codex_lint_log/<date>-lint.jsonl` に finding を 1 件 1 行で append
- HIGH severity が 3 件以上で **Discord 通知** (R-11、cron 経由起動時のみ)

## コスト

- 1 file lint = Local Messages 1 件 (Plus 5h 枠の 1-7%)
- 月間想定 (1 日 5 file lint × 30 日) = 150 messages/月、Plus $20 ぴったりで収まる

## エラーハンドリング

- `codex login status` が "Not logged in" → user に「`codex login` (browser) してください」と通知して中断
- timeout (5 分) hit → 「Codex が遅い、対象 file を分割してください」と通知
- Plus 5h 枠 hit → 「上限到達、N 分後再試行」を提示

## 関連

- `feedback_codex_review_usage.md` (起動規約全文)
- `.claude/agents/codex-reviewer.md` (subagent 経由起動の場合)
- `tools/ebay-manager/monitor/codex_lint_runner.py` (実装本体)
- `.company/engineering/docs/2026-05-15-w125-codex-reviewer-design.md` (本 skill 設計)
