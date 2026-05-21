# LLM Wiki Compilation operating model (常時適用 / Q7)

出典: Astro-Han Karpathy LLM Wiki paradigm (https://zenn.dev/dely_jp/articles/8b55114cc0b958)、2026-05-16 完全実装 (Codex review HIGH=0)。本 rule は **operating model の単一権威**。細目は cascade-update / contradiction-annotation / wiki-frontmatter を参照 (ここで再定義しない)。

## 核心原則 (Compilation > RAG)

**毎回ゼロから検索・合成し直すのをやめる。知識を一度コンパイルして wiki 化し、次回以降は wiki を読むだけにする。**

- RAG (旧): query 時に raw を毎回 retrieve → その場で合成 → 揮発
- Compilation (本 paradigm): ingest 時に raw を **業務判断単位に再構成**して wiki に固定 → query 時は **読むだけ** → 新知見は wiki に **戻す**

## 3 操作の実体マッピング

| 操作 | 我々の実体 | 役割 |
|---|---|---|
| **INGEST (コンパイル)** | session-close skill + source-summary 作成 | raw → 業務判断単位へ再構成して wiki 固定 |
| **QUERY (読む+戻す)** | SessionStart auto-load + read-first 規律 + query-save | compiled wiki を読む、新知見を戻す |
| **LINT (維持)** | daily_codex_lint (03:00) + codex-reviewer 11 種 | wiki の健全性維持 |

## §1 read-first 規律 (行動の核心、最重要 = Q7)

調べ物・再導出・再合成・外部 web 調査・大規模 grep の **前に必ず** compiled wiki を先に確認する:

1. `MEMORY.md` tier-1 (必読 + genre map、auto-load 済)
2. 関連 `MEMORY_<section>.md` tier-2 (genre map の誘導に従い Read)
3. `reference_*.md` / 関連 KB

**存在すれば読んで使う。無い、または stale (`updated:` 6ヶ月超 / 一次情報が変化) の時だけ**再導出し、結果を §3 基準で wiki に戻す。

「既に compiled 済みの知識をゼロから再導出する」のは本 paradigm の最大の anti-pattern。

## §2 INGEST 再構成規律 (#44、記事の哲学的中核)

raw (eBay API 応答 / CBP PDF / 動画 transcript / scrape HTML / scheduler.log / FedEx 通関書類) を **そのまま wiki に写さない**。

- ✅ 「関税判定」「送料計算」「仕入先採否」「コンディションランク」等の **業務判断単位**に分解し、判断に直接使える形に再構成
- ❌ raw のコピペ蓄積 (= 後で読んでも判断に使えない劣化 wiki)
- raw 自体は不変 (wiki-frontmatter `layer: raw`、C8 raw 不変原則)。再構成物は `layer: wiki`

## §3 QUERY-save 基準 (欠落ループの復活、軽量3基準)

query / 作業で以下を得たら user に「wiki に保存しますか?」を **基準明示で提案**:

- **(i) 新しい業務判断 / 新概念 / 新分類軸** (新関税ルール解釈 / 新仕入先基準 / 新しい区分軸 等)
- **(ii) 再利用可能な分析** (複数 listing / 横断的に効く汎用知見)
- **(iii) 既存 wiki の矛盾解消** (contradiction-annotation 対象)

上記外 (個別具体・既存知識の組合せ・追記で済む) は **保存不要**。「保存しない」と明示判断する (黙ってスルーしない = 判断の透明性)。

保存先は session 以外なら該当 tier-2 / reference / KB (C4 2層構造、session-close §3 準拠)。

## §4 OPLOG schema (専用 log.md は C1 不採用、session_*.md に統一行)

以下の操作のみ session_*.md に 1 行記録 (全 query 義務化は noise なので **対象を限定**):

- ingest (raw → wiki 再構成した)
- query-save (§3 で wiki に戻した)
- lint-fix (codex-reviewer 指摘を反映した)
- 外部再調査した query (web/一次情報に当たり直した)
- 保存判断が分かれそうな query (保存 or 不保存を明示判断した)

形式: `[OPLOG] <op> | <対象> | <根拠/raw> | <wiki反映先 or "保存せず:理由">`

軽い参照だけの query は記録不要。

## §5 wikilink 規律 (#8、link lint 安定性)

- wiki 内リンクは `[[slug]]` 形式のみ
- 相対パス link (`](../foo.md)`) は本文ナビ用途では使わない (index/MEMORY.md の `[title](file.md)` は索引仕様なので別)
- 本文インライン `#tag` 禁止 (frontmatter で分類する = wiki_type/genre)

codex-reviewer lint #2 を「broken `[[link]]` + 形式逸脱」検出に拡張。

## §6 source-summary 層 (#3、raw と wiki の中間)

高 value domain (関税 / 配送 / eBay API / 動画学習) は raw と再構成済み wiki (reference_*.md) の **中間層**として `source_<topic>.md` を持つ:

- `layer: wiki` (LLM が作る要約 = 信頼境界上は wiki 側、raw ではない)
- `metadata.wiki_type: source-summary`
- `sources:` に一次情報 URL、`raw:` に原本 path (CBP PDF / eBay API doc / 動画)
- 内容 = 「この raw が何を言っているか」の薄い要約 (判断ロジックは reference_*.md 側)

raw 不変性と stale / cascade lint がこの層で強化される。

## 適用優先度 (Q7 の位置づけ)

Q0-Q6 = 品質・業務安全 (損失防止)。**Q7 = 知識利用順序** (再導出より wiki read-first)。軸が異なり衝突しない。Q7 違反 (compiled 済みを無視してゼロ再導出) は品質事故ではないが **時間浪費 + wiki 形骸化** を招くため常時意識する。

## 関連 rule

- `cascade-update.md` — 規約変更時の波及 (ingest/再構成と連動)
- `contradiction-annotation.md` — §3(iii) 矛盾解消の書式
- `wiki-frontmatter.md` — layer / wiki_type / sources / raw の定義
- `md-files-can-be-wrong.md` — wiki も誤り得る (read-first しても盲信しない)
- `karpathy-principles.md` — K0-K3
- session-close SKILL.md §3 — C4 2層への保存手順
