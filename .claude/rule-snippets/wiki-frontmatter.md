# Wiki frontmatter 規約 (layer / sources / raw / updated)

出典: 2026-05-14 W123-W125 統合設計書 §3.4 Adopted (Astro-Han Karpathy LLM Wiki paradigm).

## 核心

memory / KB / reference / 設計書の frontmatter に **layer (raw|wiki) / sources / raw / updated** を追加し、stale 判定や lint を機械化可能化する.

## 適用範囲

**新規 / 編集時のみ追加** (K2 Surgical = 既存 file 一括変更禁止 / bulk migration NG).

既存 file への遡及適用は W125 codex-reviewer 着手後 (5/29〜) に **別 W** で議論. Phase A の今は新規・編集の起点のみ.

## 必須 field (新規ファイル)

```yaml
---
name: <kebab-case-slug>
description: <one-line>
layer: wiki                  # 必須: raw | wiki
updated: 2026-05-15          # 必須: YYYY-MM-DD (今日)
metadata:
  type: <feedback | project | reference | user>
---
```

## 推奨 field (該当する場合のみ)

```yaml
sources:                     # 出典 URL or path (複数可)
  - https://developer.ebay.com/...
  - https://www.cbp.gov/...
raw:                         # 元データ file path (raw layer への参照)
  - tools/ebay-manager/data/api_call_log_2026_05_15.jsonl
genre:                       # 知識ジャンル (1 語, kebab-case). graph 可視化・分類用
                             # 例: tariff / shipping / sku / supplier / rival / harness / codex
related:                     # 関連ページ slug (複数可). [[wikilink]] と二重管理せず補助索引として
  - reference-shipping-tariff-logic
  - feedback-ddp-shipping-policy
# ⚠️ metadata: は必須 field の `metadata.type` と **同一 map**. 下記 wiki_type は
#    その map に 1 key 追加するだけ (別 metadata: block を作らない = YAML duplicate key 事故防止).
metadata:
  type: feedback             # (必須、再掲) 我々の memory 分類: feedback/project/reference/user
  wiki_type: concept         # (推奨、C2 / 2026-05-16 Astro-Han 追加採用)
                             # concept | entity | source-summary | comparison | synthesis
                             # ⚠️ metadata.type とは別軸. type=memory 分類、wiki_type=Astro-Han 知識型.
                             # top-level type / metadata.type と衝突させない (Codex 2026-05-16 指摘)
```

**C2/genre/related の運用 (2026-05-16 Astro-Han 追加採用、Codex review 済)**:

- **新規 / 編集時のみ追加** (K2 Surgical = 既存 file 一括変更禁止). wiki-frontmatter 本体規約と同じ漸進方針.
- `metadata.wiki_type` は **`metadata.type` と独立**. `metadata.type` は memory 種別 (feedback/project/reference/user)、`wiki_type` は Astro-Han 知識型 (concept/entity/source-summary/comparison/synthesis). 両方任意、混同禁止.
- `genre` / `related` は graph 可視化・分類の補助. `[[wikilink]]` が一次手段、`related:` は冗長にならない範囲の補助索引.
- **flat frontmatter file の例外 (2026-05-16、#1 移行で確定、md-files-can-be-wrong 準拠)**:
  既存 memory の多くは `metadata:` block を持たず top-level `type: reference` 等の flat 構造.
  この場合 `metadata.wiki_type` ではなく **top-level `wiki_type:` / `genre:` / `related:`** を使う
  (flat file に metadata block を新設すると top-level `type:` と二重概念化し Codex 指摘の collision を招くため).
  `metadata:` block が既にある file は `metadata.wiki_type`. **判定: その file の既存 `type:` が
  top-level なら wiki_type も top-level、`metadata.type` なら `metadata.wiki_type`** (同一階層に揃える).
  実例: reference_*.md 7 件は全 flat → top-level `wiki_type` で 2026-05-16 適用済 (#7 過大判定解消).

### ⚠️ harness auto-memory の 2-schema 並存 (2026-05-16 実測確定、md-files-can-be-wrong 自規約適用)

本 rule は当初「top-level field」を前提にしていたが、**実測で harness auto-memory の挙動が判明**:

| file の出自 | frontmatter schema | 挙動 |
|---|---|---|
| **Write tool で memory dir に新規作成** | `metadata:` block 配下に `node_type/type/wiki_type/genre/layer/updated/sources/raw/related` を **強制正規化** | auto-memory 管理 node 化。以後 **Edit しても再正規化で `metadata:` に戻る** (逆らえない) |
| 既存 file / Edit のみで触る file | flat top-level (`type:` / `layer:` 等が行頭) | auto-memory 管理外。Edit で正規化されない |

**帰結 (規約の訂正)**:
- 2 schema は **並存が正常**。どちらかへの統一は不可 (harness 仕様、Write 新規は必ず metadata: 化)
- **lint / stale 判定は top-level と `metadata.*` の両方を見る** こと (`layer` は `layer:` OR `metadata.layer`、`updated` も同様)
- 新規 source-summary 等を Write で作ると自動的に `metadata:` 化される = それが auto-memory canonical。**flat に戻そうとしない** (徒労 + harness と戦う = 禁止)
- 実例: `source_cbp_section232_tariff.md` / `source_ebay_api_shipping.md` は Write 作成のため `metadata:` canonical (2026-05-16、Codex MEDIUM#2 を本訂正で解決 = flat 化でなく規約を実態に合わせる)

## layer 区分

| layer | 性質 | 例 |
|---|---|---|
| `raw` | **immutable な一次情報. 原本は変更しない (C8 / 2026-05-16 明文化)**. 通常 `.md` でなく `.json` / `.txt` / `.pdf` | eBay API 応答 / 動画 transcript / FedEx PDF / CBP CSMS HTML / scheduler.log |
| `wiki` | LLM 維持の markdown. 大半がこちら | memory / KB / reference / 設計書 / CLAUDE.md |

**C8 raw 不変原則 (2026-05-16 Astro-Han 追加採用、Codex review 済)**: raw layer のファイルは **原本を直接編集しない**. 修正・要約・解釈が必要な場合は wiki layer 側に新規ページを作り、`raw:` で原本を参照する. raw を書き換えると一次情報の信頼性 (= 関税根拠 / API 実応答の証跡) が失われる.

## stale 判定 (W125 codex-reviewer 連携 = 5/29 以降)

- `updated:` が **6 ヶ月以上前** → stale candidate flag
- `sources:` の URL が **dead link** → cascade-update 候補 flag
- `raw:` の path が **削除済み** → broken reference flag

検出後の修正は 2-stage loop (Codex → Claude → user). 自動削除はしない (false positive リスク).

## 例: feedback memory の新規作成

```yaml
---
name: feedback-discord-visual-verify-required
description: R-11. 通知系 verify は user 実視認まで実施
layer: wiki
updated: 2026-05-14
sources:
  - https://discord.com/developers/docs/resources/webhook
metadata:
  type: feedback
---

# R-11: Discord 通知 verify は user 実視認まで

(本文)
```

## 例: KB topic file

```yaml
---
name: section-232-tariff-2026-04
description: Section 232 関税 HS リスト + 計算ワークフロー
layer: wiki
updated: 2026-04-30
sources:
  - https://www.cbp.gov/sites/default/files/...
  - https://www.federalregister.gov/...
metadata:
  type: ebay-knowledge
---
```

## 例: raw layer (参考、Phase A の対象外)

raw layer は `.md` 以外が主だが、もし `.md` で raw を扱う場合 (動画 transcript 等):

```yaml
---
name: video-transcript-2026-05-01-anthropic-cal-rueb
description: 動画 transcript (生データ)
layer: raw
updated: 2026-05-01
sources:
  - https://youtube.com/watch?v=...
---
```

## 適用しない場合

以下は frontmatter 拡張不要 (時系列・テンポラリ / 規約 doc):
- `session_*.md` (originSessionId は既存維持)
- `tools/ebay-manager/data/*.json` / `.jsonl` (raw layer だが frontmatter なし)
- `.company/secretary/inbox/YYYY-MM-DD.md` (日次ファイル、updated は file name で自明)
- `.company/secretary/notes/YYYY-MM-DD-*.md` (時系列 archival、cascade-update L22 延長)
- `.company/secretary/todos/YYYY-MM-DD.md` (時系列 TODO)
- `.claude/rules/*.md` / `~/.claude/rules/*.md` (rule 自身は規約 doc、本 wiki-frontmatter 規約の対象外)

## 遡及適用の許可範囲 (2026-05-15 user 指示で更新)

既存 memory への遡及適用は **重要 file 限定** (例: MEMORY.md で ⭐⭐⭐ marked の ~25 file) であれば K2 violation とみなさない. 業務クリティカルな file の frontmatter は 5/29 の W125 着手を待たずに揃えてよい.

**bulk migration 禁止** は維持: 148 file 全件の一括書き換えは依然 K2 違反. 重要度フィルタ (⭐⭐⭐ / 編集頻度 / 業務クリティカル度) で対象を絞る.

2026-05-15 W124 Phase 後の追加 G3 task として user 公認.

## 関連 rule

- `md-files-can-be-wrong.md` — .md staleness の必要性
- `cascade-update.md` — sources 変更時の波及
- `contradiction-annotation.md` — 矛盾 frontmatter (両論併記が必要な場合)
- `karpathy-principles.md` — K2 Surgical (bulk 禁止) / K0 仮定明示
