# .md ファイル (CLAUDE.md / rules / memory) も誤りを含み得る (常時適用)

出典: 2026-04-30 SKU 規約調査時に assistant が `tools/ebay-manager/CLAUDE.md` の `(連番)` `(一意キー)` 記述を権威として無批判参照 → 「stock:01 active 58 件 = SKU 崩壊」と誤判定。user 指摘で **CLAUDE.md 自体の記述ミス** が判明。

**.md 文書は権威ではなく、過去時点のスナップショット**。実コードと矛盾したら **コードの方が真実** であり、.md は訂正対象。

## 適用対象 (調査・設計・実装するエージェント全て)

- `research-brain` (Opus 4.8 業務判断)
- `code-architect` (機能設計)
- `code-reviewer` (レビュー)
- `generator` (コード実装)
- `planner` (仕様書設計)
- `Explore` (コードベース調査)
- `ebay-manager-qa` / `evaluator` (品質評価)
- general-purpose / 直接 Read している main agent も同様

## ルール R-1〜R-4

### R-1. 不具合調査時は .md ファイルも疑え

実コード ≠ .md 文書記述 で **コードの方が真実**。.md と現状コードが矛盾したら:

1. .md ファイルを **訂正対象としてフラグ** する (mental flag)
2. user に「.md 記述と現状コードが矛盾、どちらを正とするか」確認
3. 訂正後に再調査
4. 「.md にこう書かれているから」を結論の根拠にしない

### R-2. .md ファイルからの引用は分解して提示

「CLAUDE.md にこう書いてある」と発言する時:

- ✅ ファイル名 + 行番号 + **逐語引用** (` ``` ` で囲む)
- ❌ 「.md にこう書かれていたから」と推論を交えた要約
- ❌ 推論内容を「.md の引用」と混同して報告

特に推論を含む発言は **「これは私の解釈」と明示**。

### R-3. 過去事故関連の記述は memory も照合

`session_*.md` / `feedback_*.md` で過去事故を記録した場合、CLAUDE.md / rules の対応箇所が **未更新で残存** していないか定期確認。

例: 2026-04-29 W7-A SKU 主キー崩壊事故 → migration v26 で修正したが、`tools/ebay-manager/CLAUDE.md` の `(一意キー)` 記述は 2026-04-30 まで残存。これが新たな誤判断を生んだ。

### R-4. user 指摘で .md が誤りと判明したら 3 か所鏡像更新

「memory + rule + CLAUDE.md の 3 か所鏡像更新」が標準対応。1 か所だけ直すと再発する。

具体的な更新対象:
- `tools/ebay-manager/CLAUDE.md` (subdir 規約)
- `CLAUDE.md` (project root)
- `.claude/rules/<topic>.md` (横断 rule)
- memory `reference_*.md` / `feedback_*.md`
- memory `MEMORY.md` (インデックス)

## 違反例 (2026-04-30)

- assistant が `tools/ebay-manager/CLAUDE.md:39-47` の SKU 規約「`(連番)` `(一意キー)`」を疑わずに参照
- DB SELECT で `stock:01` active 58 件 → 「SKU 一意性崩壊」と誤判定
- 誤った W68 ROADMAP 提案 (SKU 崩壊調査) を生成
- user 指摘で初めて CLAUDE.md 自体の誤記述に気づく
- 結果: user 信頼度低下、調査時間ロス

## 関連 rule

- `silent-skip-prevention.md` — 困難な調査を回避修正で逃げない (Q0)
- `karpathy-principles.md` — K0 仮定を明示
- `sku-rules.md` — 本事故の対象ルール
