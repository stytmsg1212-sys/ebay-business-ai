---
title: GitHub × Obsidian × Codex 三位一体連携のメリット資料
date: 2026-05-15
author: Claude Code (Opus 4.7)
audience: user (eBay 越境 EC セラー、業務判断者)
sources:
  - .company/engineering/docs/2026-05-14-W123-W125_unified_design.md
  - https://github.com/multica-ai/andrej-karpathy-skills (Karpathy LLM Wiki paradigm)
  - https://github.com/kepano/obsidian-skills (kepano Obsidian Agent Skills)
  - https://github.com/Astro-Han/karpathy-llm-wiki (Astro-Han lint 9 種実装)
  - https://help.obsidian.md (Obsidian 1.12 公式)
  - https://github.com/denolehov/obsidian-git (Obsidian Git plugin)
layer: wiki
updated: 2026-05-15
---

# GitHub × Obsidian × Codex 三位一体連携のメリット資料

## 1. なぜ今この資料が必要か

5/14 統合設計書 (W123-W125) では `kepano/obsidian-skills` install や Obsidian vault 構築まで進めたが、**「連携で何が業務的に得られるのか」が技術用語で散らばっていて、user が判断材料として一覧できる資料がなかった**.

5/15 夜の W124 完走で Codex CLI も稼働開始、GitHub 同期着手が現実視野に入ったため、ここで一度全体像を業務メタファで整理する.

---

## 2. 全体像: 三位一体の連携イメージ

```
┌─────────────────────────────────────────────────────────────────┐
│  あなた (user)                                                  │
│   ↕ 編集・閲覧                                                  │
│  ┌──────────────────────┐                                       │
│  │  Obsidian (vault)    │ ← 見やすい editor、グラフ可視化、mobile│
│  │  C:\...obsidian-vault│                                       │
│  │  ├─ memory\ (junction)│ ← 業務ノウハウ・規約・経緯           │
│  │  └─ company\ (junction)│ ← 部署別業務メモ                    │
│  └──────────────────────┘                                       │
│   ↕ junction (実体は別 path)                                    │
│  ┌──────────────────────┐                                       │
│  │  実体ファイル群       │ ← AI agent が読む実物                 │
│  │  ~/.claude/.../memory │                                      │
│  │  .company/            │                                      │
│  └──────────────────────┘                                       │
│   ↕ git push / pull (将来)                                      │
│  ┌──────────────────────┐                                       │
│  │  GitHub (private)    │ ← 履歴・バックアップ・自動化           │
│  │  ebay-business-ai    │                                       │
│  └──────────────────────┘                                       │
│                                                                  │
│  各 AI ツールは同じファイルを読む:                              │
│  - Claude Code (Anthropic): memory + .company を全部 load        │
│  - Codex CLI (OpenAI):       review / lint / 矛盾検出に使う      │
│  - 将来 ChatGPT Web/Mobile: Obsidian Mobile 経由で読み書き可     │
└─────────────────────────────────────────────────────────────────┘
```

要点: **同じ知識ベース (memory + 業務 KB) を 1 箇所に置いて、複数 AI ツールが異なる得意分野で活用する** 構造.

---

## 3. なぜ Obsidian を入れるのか (5 つのメリット)

### 3.1 見やすく書きやすい editor (現状: メモ帳・VSCode・直接編集)

| 項目 | 現状 (テキストエディタ) | Obsidian |
|---|---|---|
| Markdown プレビュー | ❌ 別タブ or なし | ✅ Live preview (書きながら見える) |
| リンク `[[name]]` の navigation | ❌ 手動 ctrl+F | ✅ クリックで飛べる |
| バックリンク (どこから参照されてる?) | ❌ 全 file grep 必要 | ✅ 自動表示 |
| グラフ可視化 (memory 間の繋がり) | ❌ なし | ✅ Graph view で一望 |
| Mobile / iPad 編集 | ❌ なし | ✅ Obsidian app あり |

**業務的に効くシーン**:
- 出張中・移動中に思いついた業務改善アイデアを iPad で memory に直書き
- 「あの関税ルールを参照してる他の memory どこだっけ?」 → backlink で即出る
- 「全 memory の繋がりを俯瞰したい」 → Graph view で蜘蛛の巣ビュー

### 3.2 OneDrive 同期事故からの脱出 (既経験済の苦労)

過去 W126 (5/14) で **OneDrive ↔ Git の競合 copy 量産事故** が発生. Obsidian は vault を **OneDrive 外** に置くことで物理的に再発防止.

| 課題 | OneDrive | Obsidian vault (OneDrive 外 + Git 同期) |
|---|---|---|
| 競合 copy "file - Copy.md" の量産 | ⚠️ 頻発 | ✅ 発生しない |
| `.git/FETCH_HEAD` の競合 | ⚠️ 頻発 | ✅ 発生しない |
| 「最新どっち?」の判別 | ⚠️ 手動 | ✅ git log で履歴明示 |

### 3.3 plugin エコシステム (kepano skill 既 install)

すでに `kepano/obsidian-skills` 5 種を install 済. これにより Claude Code から:
- vault のファイルを read/write
- daily note 生成
- template 適用
- tag 整理

を skill 経由で呼べる. 将来 W125 で `/codex-review` slash command を作る時にも同じ skill 基盤を再利用可能.

### 3.4 Karpathy "LLM が書き、人が読む" パラダイム

Karpathy 提唱の LLM Wiki paradigm の核心:
- **従来 (RAG)**: 読む時に AI が file 検索 → ヒット箇所だけ使う
- **新 (Compilation)**: 書く時に AI が file 群を統合 → 完成した wiki ページにする

Obsidian + Codex の組み合わせは Compilation 型に近く、**「user が業務知識を書いておけば、AI が定期的に矛盾・古さを lint してくれる」** 体制が組める.

5/15 夜の Codex review で実際に **私 (Claude) が見落とした cascade 漏れを 9 件発見** = この paradigm の効果が実証された.

### 3.5 業務ノウハウの "見える化"

eBay 越境 EC の業務ノウハウ (関税・送料・SKU・コンディション判定・サプライヤー基準・ライバル分析) は現在 **memory ファイル 50+ 件に散在**.

Obsidian の Graph view で関係性を俯瞰すると:
- どの memory が「ハブ」になっているか (= 重要度の可視化)
- どの memory が「孤島」になっているか (= 整理候補 = 削除 or リンク追加)
- ある topic (例: 「Section 232 関税」) を扱う memory が一覧で見える

判断: 「memory が増えすぎて自分でも何が何か分からない」状態を防ぐ.

---

## 4. なぜ GitHub を入れるのか (5 つのメリット)

### 4.1 完全なバックアップ + ディザスタリカバリ

| シナリオ | 現状 (Local + OneDrive) | GitHub 同期後 |
|---|---|---|
| PC 故障 / SSD 死亡 | OneDrive から復元 (運次第) | `git clone` で完全復元 |
| OneDrive サブスク停止 | データ全消失リスク | GitHub に残る |
| 誤って memory 大量削除 | OneDrive ゴミ箱 (30 日) | `git revert` でいつでも戻せる |
| ランサムウェア感染 | OneDrive も暗号化される | GitHub の過去 commit から復元可 |

業務データ (関税・売上 KB・サプライヤー判定基準) は **失うと数百時間かかる学び** が消える. 多重バックアップは生命線.

### 4.2 変更履歴の完全可視化

`git log` / `git blame` で:
- 「この関税率はいつ誰が変えた?」 → commit から特定 (今は session memory 漁る必要あり)
- 「この memory に半年で何回手が入った?」 → blame で頻度可視化 = メンテ負荷の判定材料
- 「先週の判断、今日と何が変わった?」 → diff で 1 秒
- 「W110(2) 着手前の状態に戻したい」 → revert で復元

### 4.3 commit 単位での "やったことの粒度" が見える

現在のセッション (5/15 夜) でやった作業:
- shipping_tariff v2.0 → v2.1
- DB 11 record backfill
- Codex review usage 規約制定
- cascade fix 19 件
- 課金体系調査

これらを **1 commit ずつに分けて push** すると、「何を、なぜ、いつ」が GitHub 上で時系列で追える. session memory は「自然言語の要約」、git は「機械可読の事実」、両方残すと監査・振り返りが強力.

### 4.4 GitHub Actions で自動化の余地

将来できること (今すぐではない、Month 2 以降):
- memory に push がある度に Codex で自動 lint → PR コメント
- 毎日 03:00 に memory 全体を Codex で staleness check
- 関税率変更 keyword が新 commit にあれば、関連 KB 全部に reminder

### 4.5 multi-device 開発の道

将来:
- 自宅 PC / オフィス PC / iPad / iPhone から同じ vault にアクセス
- 移動中の電車で iPad で書いたメモが、自宅 PC で Claude Code 起動した時に既に load されてる

これが OneDrive で実現できなかったのは「conflict copy 事故 + 同期遅延」が原因. GitHub 経由なら conflict は git の merge で明示的に解決される.

---

## 5. 連携で新しく可能になること (Obsidian + GitHub + Codex)

### 5.1 「思いついた時に書く」 → 「書いたら自動 lint」 → 「整合性を保ったまま蓄積」

```
[user] iPad で「Section 232 関税が 25% → 30% に変わったらしい」と memo
   ↓
[Obsidian Mobile] vault の memory/temp_idea.md に保存
   ↓
[Obsidian Git plugin] 自動 commit + push (1h 毎)
   ↓
[GitHub Actions or 手動 trigger] Codex で全 memory cross-check
   ↓
[Codex] reference_section_232_kb.md L42 の 25% 記述と矛盾を検出
   ↓
[Claude Code] 次セッション開始時に通知 → user 承認 → 一斉 cascade update
```

現状: user が思いつきを書く先がない (memory に直書きは敷居が高い) → 記憶を頼る → 漏れる.

### 5.2 複数 AI ツールの "得意分野" 使い分け

| AI ツール | 得意 | 業務での使い所 |
|---|---|---|
| **Claude Code** (Opus 4.7) | コード生成・複雑業務判断・実行 | 出品自動化・関税計算・仕入先評価 |
| **Codex CLI** (GPT-5.3) | 文書 lint・矛盾検出・external 視点 | memory 監査・KB 鮮度チェック |
| **ChatGPT Web/Mobile** (将来) | 雑談ベースの相談・iPhone での質問 | 移動中の業務判断相談 |

同じ memory を 3 つのツールが参照 = **echo chamber 化を防ぎつつ、共通の知識ベースで判断統一**.

### 5.3 業務手順書 (USER_MANUAL.md) の進化

現在 USER_MANUAL.md は project root にあるが、Obsidian で **画像入りの章** にできる:
- "kill switch 押す手順" にスクショ付き
- "scheduler 再起動" の手順を flow chart で
- リンクで関連 troubleshoot memory に飛べる

→ 「Claude が動かない時、user が自分で復旧できる」局面が広がる.

### 5.4 cascade update の機械化 (5/15 夜実証済)

5/15 夜の事例: shipping_tariff v2.0 → v2.1 で 4 ファイル + コード + テスト + DB と修正点が広がった.

Obsidian + Codex 構成では:
1. user / Claude が 1 ファイルに修正を入れる
2. commit / push
3. Codex が「同 topic を扱う他の file」を grep し、波及未対応箇所を flag
4. Claude が user に提示し、一括修正

この体験を 5/15 夜に手動で実演したが、今後は **半自動化** (Codex の自発提案 5 場面に「cascade 未追従検出」を加える) が現実的.

---

## 6. Karpathy / Astro-Han 設計思想の取り込み (なぜこの paradigm が良いか)

### 6.1 3 層構造 (raw / wiki / schema)

```
[Raw]   一次情報 (CBP PDF、eBay API 応答、動画 transcript、scheduler.log)
   ↓ Ingest (LLM が読み解く)
[Wiki]  LLM が維持する markdown (memory/ や .company/ebay-knowledge/)
   ↓ 参照
[Schema] 規則・契約 (CLAUDE.md、.claude/rules/、Q0-Q6)
```

我々は既に **80% この構造になっている**. 今回の Obsidian + Codex で「wiki 層の維持コスト」が下がり、結果として:
- raw → wiki の取り込み速度が上がる (Codex が consolidation を補助)
- wiki → schema (CLAUDE.md 更新) の判断が正確になる (Codex の lint で矛盾検出)

### 6.2 3 操作 (Ingest / Query / Lint)

| 操作 | 我々の現状 | 強化後 |
|---|---|---|
| Ingest (一次情報 → wiki) | session-close skill が手動でやってる | Codex が大規模な再 consolidation を補助 |
| Query (wiki から検索) | SessionStart hook + MEMORY.md auto-load で OK | 変更なし、既に十分 |
| Lint (wiki の品質維持) | R-9 (SUPERSEDED 検出) のみ | **W125 Codex reviewer で 9 種チェック実装** |

Lint の 9 種 (Astro-Han 由来) は:
1. index 整合性 (MEMORY.md と実 file)
2. internal link 検証
3. raw 参照検証 (source link 切れ)
4. See Also 補完
5. factual contradictions (両論併記欠落)
6. outdated / superseded claims (6 ヶ月以上未 update)
7. missing conflict annotations
8. orphan pages (どこからも link されない)
9. stale archive (raw 更新後 wiki 未追随)

> **2026-05-16 追補 (C3 / cascade-update 準拠)**: 上記 9 種に加え、growth suggestion 系 2 種を追加採用し **計 11 種** となった (Codex review 済):
> 10. missing concept pages (3+ file で言及されるのに独立ページ無し、report-only)
> 11. next-topic suggestion (KB 空白から次調査 topic 提案、evidence 必須・report-only・自動 KB 化禁止)
> 実装: `.claude/agents/codex-reviewer.md` (11 種化) + `tools/ebay-manager/monitor/codex_lint_runner.py` LINT_PROMPT_TEMPLATE に (8)(9) 追加。

これらは **私 Claude 単独では取りこぼす** ことが 5/15 夜に実証された (9 件の cascade 漏れ).

### 6.3 両論併記の文化 (contradiction-annotation.md)

5/14 制定の `contradiction-annotation.md` ルールが、Obsidian で **visual に supersession を表現** できる:
- 取消線で旧見解
- 太字で新見解
- リンクで決定経緯の session memory に飛べる

→ 「過去の判断を後から再評価する」業務継続性が担保される.

---

## 7. 想定リスクと対策

| リスク | 起きると何が困るか | 対策 |
|---|---|---|
| GitHub repo の private 漏洩 | 業務知識 (関税・サプライヤー・価格戦略) が外部流出 | repo を private に厳格設定 / 2FA 必須 / push key rotate |
| Obsidian Git plugin が壊れる | 自動同期が止まる、user 気付かない | Discord 通知 + 週次手動 commit 確認 (R-11 適用) |
| GitHub 障害 (実例 2026 年に複数回) | 同期 / push 一時停止 | Local + OneDrive バックアップ並走で当面しのぐ |
| Codex hallucination で誤 lint | 修正不要な箇所を「矛盾」と flag | 2 段ループ (Claude 再評価) で物理ガード、5/15 実例 1 件却下済 |
| junction が Windows update で壊れる | vault から memory/company が見えなくなる | `scripts/setup_obsidian_vault.ps1` で再構築可能化 |
| Obsidian app の breaking change | plugin 互換性失う | Obsidian 安定版 (1.12.x) で stay、メジャー更新前に backup |
| commit 履歴に secrets 混入 | API key 等が GitHub に push される | pre-commit hook (`secrets-scan`) 設定、`.gitignore` 強化 |

---

## 8. 実装手順 (今夜 or 明日以降に取れる路線)

設計書 §3.1 Decision C の詳細化:

### Step 1: Obsidian Git plugin install (5 分、assistant + user)

```
Obsidian → Settings → Community plugins → Browse → "Obsidian Git" install
→ Enable → ⚙ で設定:
   - Commit message: "vault: {{date}} {{hostname}}"
   - Auto commit interval: 60 分
   - Auto push interval: 360 分 (= 6h)
   - Pull on startup: ON
```

### Step 2: リポジトリ方針決定 (議論 5 分、user 判断)

| 方針 | おすすめ度 | 理由 |
|---|---|---|
| **A) 既存 `ebay-business-ai` repo の subdir として同梱** | ⭐⭐⭐ | リポジトリ管理 1 個、コード変更と memory 更新が同 commit 履歴に乗る (visible) |
| B) 別 private repo (例: `claude-memory`) を新設 | ⭐⭐ | 分離が綺麗、ただし管理コスト 2 倍 |

A 推奨理由の補強: 既に `ebay-business-ai` repo は `.company/` / memory 系も含めて push 済 (W126 で 4368 files 移行済). vault の junction が指す先がそのまま既存 repo 配下なので、追加作業少.

### Step 3: 動作確認 (10 分)

- 1 ファイル編集 → 60 分後に commit が走る確認
- 6h 後に push 走るか確認 (or 手動 push で test)
- 別マシン or `git clone` で復元できるか dry-run

### Step 4: Discord 通知連動 (Optional、Month 2 以降)

- push 失敗時に Discord 通知 (R-11 適用)
- 1 日 1 回 commit 0 件なら「同期止まってる」warning

---

## 9. 何を期待してよくて、何を期待してはいけないか

### 期待してよい (確実に得られる)

- ✅ memory バックアップの多重化 (Local + GitHub)
- ✅ memory 編集の利便性向上 (Obsidian の Live preview / Backlink / Graph)
- ✅ Codex lint で cascade 漏れ検出 (5/15 夜実証済)
- ✅ 履歴の完全可視化 (git log / blame)
- ✅ 複数 AI ツールの共通知識基盤化

### 期待してはいけない (誤解しがちな点)

- ❌ Obsidian の AI 機能で何かが自動化される (Obsidian 単独では AI 機能弱、別の plugin 必要)
- ❌ GitHub に push しただけで世界中で読める (private repo 設定なら user しか見えない)
- ❌ Codex が完全自律で間違いを直してくれる (2 段ループ必須、user 承認が要)
- ❌ Mobile で書いたものが即座に Claude に反映 (commit + push 後に次セッションで load = ~ 数時間遅延)

---

## 10. 次のアクション (今夜 or 明日以降)

3 択 (再掲):

| 案 | 内容 | 時間 |
|---|---|---|
| **D) 今夜は session-close で締め、明日朝 W122 verify 後で着手** | 今夜は静かに終了 | 明日 30 分 |
| **E) リポジトリ方針 (A/B) だけ決めて session memory に書き残す → 明日着手** | 5 分議論 + 記録 | 今夜 5 分 + 明日 25 分 |
| **F) 今夜 Step 1-3 を全部やる** | 一気通貫 | 今夜 30 分 |

設計書では「W124 完了後」と書いてあるので、5/16 以降が "正規" タイミング. 5/15 夜既に大量作業終わってるので **D / E 推奨**.

---

## 11. 関連ドキュメント

- `.company/engineering/docs/2026-05-14-W123-W125_unified_design.md` (5/14 統合設計、本資料の親)
- `.claude/rules/cascade-update.md` (cascade 規約)
- `.claude/rules/contradiction-annotation.md` (両論併記)
- `.claude/rules/wiki-frontmatter.md` (frontmatter 規約)
- `memory/feedback_codex_review_usage.md` (5/15 制定、本連携の起動規約)
- `memory/session_2026_05_15_w124_codex_full_completion.md` (5/15 夜セッション、本資料の作業文脈)
