---
title: Obsidian Web Clipper セットアップ手順 (W124 P2 G2)
date: 2026-05-15
author: Claude Code (Opus 4.7)
audience: user (実行担当)
sources:
  - https://obsidian.md/clipper
  - https://github.com/obsidianmd/obsidian-clipper
  - https://obsidian.md/help/web-clipper
layer: wiki
updated: 2026-05-15
---

# Obsidian Web Clipper セットアップ手順 (~5 分 user 作業)

## 何が嬉しいか (1 行で)

Web 記事を **ブラウザの 1 クリック** で Obsidian vault に Markdown 保存. YAML frontmatter (title / url / 著者 / 日付) は自動付与.

## 何をどこに保存するか

- **保存先**: `C:\Users\gucch\obsidian-vault\clipped\` (新規作成)
- **理由**: vault 配下だが `memory/` (我々の curated 知識) と `company/` (業務記録) とは分離した raw layer 領域
- 必要に応じて後で memory / company に「ingest」(= 整理して取り込み) する流れ

## Step 1: ブラウザ拡張インストール (user 操作、~2 分)

### Chrome / Edge の場合

1. Chrome Web Store を開く: <https://chromewebstore.google.com/detail/obsidian-web-clipper/cnjifjpddelmedmihgijeibhnjfabmlf>
2. 「Chromeに追加」(Edge は「インストール」) をクリック
3. ブラウザ右上に Obsidian アイコン (紫の宝石) が追加される

### Firefox / Safari の場合

公式ページから案内: <https://obsidian.md/clipper>

## Step 2: Vault 接続 (user 操作、~1 分)

1. ブラウザ右上の Obsidian アイコンをクリック → 設定 (⚙)
2. 「Vault」セクションで `obsidian-vault` を選択 (Obsidian app で既に開いてる vault が自動で見える)
3. 「Save to folder」を `clipped/` に設定 (フォルダが無ければ作る)

## Step 3: テンプレート設定 (推奨、~2 分)

「Templates」タブで以下のテンプレート設定推奨:

### Note name (ファイル名形式)

```
{{date|YYYY-MM-DD}}-{{title|slug}}
```

例: `2026-05-15-llm-wiki-obsidian-claude-code.md`

### Properties (YAML frontmatter)

```yaml
title: "{{title}}"
url: {{url}}
author: {{author}}
published: {{published}}
clipped: {{date}}
layer: raw
genre: clipped
tags:
  - clipped
```

→ 我々の `wiki-frontmatter.md` 規約とも整合 (`layer: raw` で raw layer に分類).

### Body template

```
{{content}}
```

(記事本文を Markdown に変換して保存)

## Step 4: 動作テスト (~1 分)

今日読んだ Zenn 記事をテスト clip:

1. <https://zenn.dev/dely_jp/articles/8b55114cc0b958> を開く
2. ブラウザの Obsidian アイコンをクリック
3. プレビューで内容確認 → 「Save」
4. Obsidian app で `C:\Users\gucch\obsidian-vault\clipped\2026-05-15-llm-wiki-obsidian-claude-code.md` が作成されているか確認

## Step 5: 動作確認後 (assistant 側で対応)

user が Step 1-4 完了したら、assistant に「Web Clipper セットアップ完了、テスト clip OK」と一言伝える. 私が:

1. `obsidian-vault/clipped/` の最初の clip を確認
2. 必要なら template の微調整提案
3. 今後 user が「この記事 ingest して」と言ったら assistant が `clipped/` の最新 clip を memory / company に整理取り込み (= ingest 操作)

## 注意点 (記事の作者「たろう眼鏡」氏も指摘)

- **clipped/ は raw layer = 不変扱い** にする. 直接編集しない. 直接編集が要る場合は memory に ingest してから.
- **Web Clipper のテンプレートは vault 横串で 1 つ**. clip するサイトごとに template を増やすのは複雑度上がるので、当面は 1 つの共通テンプレで運用.
- 大量 clip すると vault が肥大化. **月 1 回の lint で「ingest 済 → 削除 OK」を判定** する運用. (W125 codex-reviewer の役割候補)

## 関連

- [[2026-05-14-W123-W125_unified_design]] (W123 Obsidian 連携の親設計)
- [[2026-05-15-github-obsidian-integration-benefits]] (連携メリット資料、本日制定)
- `feedback_codex_review_usage.md` (clipped/ の lint も将来 Codex に任せる)
