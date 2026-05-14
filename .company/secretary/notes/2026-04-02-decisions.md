---
date: "2026-04-02"
type: decisions
---

# 作業ログ・意思決定 - 2026-04-02

## ✅ 今日完了したこと

### 1. 起動ルーティン
- メールチェック実施（Gmail MCP）
- 前日TODO繰越（ポケモン Pokopia等）
- デイリーリサーチ実施 → `research/topics/2026-04-02-new-product-research.md`

### 2. メールチェック結果（2026-04-02）

| 種別 | 内容 | 対応状況 |
|------|------|---------|
| eBay顧客メッセージ | khal-5019: DHL/FedEx問題クレーム（Sony ICD-TX660） | **未対応** |
| eBay顧客メッセージ | boe-rob: Kikkoman互換性質問 | **未対応** |
| eBay売上 | PLOTTER 5016 Bible Size HorseHair II 売れた | **発送未処理** |
| 入金 | Payoneer $289.46（eBay Payout ID: 7427935178） | 確認済み |
| 仕入れ | 楽天市場でPLOTTER ネイビー ¥15,345購入 | 古物台帳対象外（新品）|

### 3. eBay出品エージェント構築（新規）

以下を新規作成：
- `.company/ebay-listing/CLAUDE.md` — 出品エージェント部署ルール
- `.company/ebay-listing/drafts/2026-04-02-plotter-horsehair2-navy.md` — PLOTTER出品文（HTML完成版）
- `.claude/agents/ebay-listing.md` — サブエージェント定義（model: haiku）
- `.claude/commands/listing.md` — `/listing` スラッシュコマンド

### 4. 確立したエージェント起動方法
```bash
cd "C:\Users\gucch\OneDrive\work\claude"
claude
# 起動後：
/listing
```

---

## ❌ 未対応タスク（次回最優先）

| タスク | 優先度 | 期限 |
|--------|--------|------|
| khal-5019への返信（DHL→FedEx経緯説明＋謝罪） | 🔴 高 | 即日 |
| boe-rob への返信（Kikkoman非互換を正直に伝える） | 🔴 高 | 即日 |
| PLOTTER 5016 Bible Size の発送処理 | 🔴 高 | 即日 |
| ポケモン Pokopia 在庫確認（PokémonCenter Online） | 🟡 高 | 2026-04-03 |
| Pilot Kire-Na 3本バンドル出品検討 | 🟢 通常 | 未定 |

---

## ⚠️ 今日の失敗・修正点（次回への教訓）

### 失敗1: 起動ルーティンの省略
- 最初にメールチェックだけしてリサーチと繰越を省略した
- **→ 次回：/company起動時は必ずメールチェック＋繰越＋リサーチの3点セット**

### 失敗2: 古物台帳の適用範囲誤り
- 楽天市場の新品購入を古物台帳に記載しようとした
- **→ 古物台帳は中古品仕入れ（メルカリ・ヤフオク・ラクマ）のみ対象**

### 失敗3: `--agent` フラグの誤情報
- `claude --agent ebay-listing` というフラグを誤って案内した
- このフラグは存在しない（サブエージェント直接起動は不可）
- **→ 正しくは `cd プロジェクトDir && claude` 後に `/listing`**

### 失敗4: 起動ディレクトリのミス
- `C:\Users\gucch` から `claude` を起動すると `.claude/agents/` が読めない
- **→ 必ず `C:\Users\gucch\OneDrive\work\claude` から起動すること**

---

## 新設ルール・方針

| 内容 | 理由 |
|------|------|
| eBay出品にCountry of Originを記載しない | 関税リスク |
| 不確かな情報は空欄（無理に埋めない） | 誤記載→返品・Defect |
| 出品エージェントmodel: haiku | 速度優先（品質問題あれば sonnet に戻す） |

---

## 現在のファイル構成（重要ファイル）

```
.company/
├── ebay-listing/          ← 今日新規作成
│   ├── CLAUDE.md
│   └── drafts/
│       └── 2026-04-02-plotter-horsehair2-navy.md  ← HTML出品文完成

.claude/
├── agents/
│   └── ebay-listing.md    ← 今日新規作成（model: haiku）
└── commands/
    └── listing.md         ← 今日新規作成（/listingコマンド）
```

---

## 復活の呪文（次回セッション再開手順）

### 秘書セッション（通常業務）
```bash
cd "C:\Users\gucch\OneDrive\work\claude"
claude
# 起動後：
/company でメールチェックと昨日の続きをお願いします
```

### eBay出品エージェント（専用セッション）
```bash
cd "C:\Users\gucch\OneDrive\work\claude"
claude
# 起動後：
/listing
```

### 両方同時に使う場合
- ターミナル1: 秘書（上記1番目）
- ターミナル2: 出品エージェント（上記2番目）
- **どちらも必ず `C:\Users\gucch\OneDrive\work\claude` から起動すること**

---

## 次回セッションで真っ先にやること

1. **khal-5019 返信** — DHL/FedEx問題の謝罪＋現状報告
2. **boe-rob 返信** — Kikkoman非互換を丁寧に伝える
3. **PLOTTER 5016 発送** — 発送手続き開始
4. **ポケモン Pokopia 確認** — 4/3期限
