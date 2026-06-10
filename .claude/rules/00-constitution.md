# Constitution — 常時遵守 (Layer 1 always-load)

本ファイルは eBay 物販プロジェクトの**核心 rule index**。CLAUDE.md と並んで毎セッション auto-load される。詳細は本 dir 内の各 rule file (always-load) または `.claude/rule-snippets/` (on-demand) を参照。

## Karpathy 4 原則 (K0-K3 verbatim、常時適用)

- **K0 Think Before Coding**: 仮定を明示、複数解釈を user に提示、混乱を抱えたまま進まない
- **K1 Simplicity First**: 最小コード、要求外機能 / 抽象化 / configurability 禁止 (3 回出てから共通化)
- **K2 Surgical Changes**: 関係ない code/comment/formatting を「ついでに直す」禁止
- **K3 Goal-Driven**: 抽象タスク → measurable goal、変更前後で outcome verify

優先度: K3 (最優先) → K0 → K2 → K1。詳細: `karpathy-principles.md`

## Q0-Q7 (品質事故防止、常時適用)

- **Q0** サイレントスキップ / 偽装成功 / 逃避修正は**絶対禁止** → `silent-skip-prevention.md`
- **Q1** UI / 定時実行バグ修正 DoD = 11 ステップ Phase 0-3 (pytest だけ NG、Streamlit + Playwright + DB + scheduler.log 必須)
- **Q2** DB migration 冪等性必須 (try/except OperationalError、DROP/DELETE 別 one-shot、本番直接書込 24h retrospective) → `db-migration-rules.md`
- **Q3** 新機能 / 外部 API / 不確実変更は構造化設計フロー必須 (Clarify → 設計 → 2 段 review → 実装 → Q1)
- **Q4** コード変更後 code-reviewer agent で HIGH=0 まで修正ループ
- **Q5** 完了報告: 「使用したモデル」明示、未実施フェーズ明記、Phase 0 発見併記
- **Q6** モデル選定: Opus 4.8 (業務判断・Research 1日30) / Sonnet 4.6 (多制約) / Haiku 4.5 (bulk・デフォルト)
- **Q7** 知識利用順序 (LLM Wiki): compiled wiki を read-first、ゼロ再導出禁止 → `.claude/rule-snippets/llm-wiki-compilation.md`
- **進捗 touchpoint**: マイルストーン完了=即報告 / 長時間待ちは事前宣言 / 無言区間上限 ~15 分 → `progress-touchpoint.md` (2026-06-10 制定、無応答事故 2 件で昇格)

## 金銭直結 rule (常時 always-load、違反 = 品質事故)

- **SKU 用途は 2 つだけ**: (1) 有/無在庫判定 prefix (`stock**` / `ebay**_*****`)、(2) 無在庫 SKU 変換 → 仕入先 URL。**listing 識別は `ebay_item_id`** (SKU 主キー禁止、`JOIN ON sku` / `GROUP BY sku` / `UNIQUE(sku)` 禁止) → `sku-rules.md`
- **DB 直接書込は原則禁止**、READ ONLY 例外。やむを得ず実行時は 6 step (snapshot → 1 件試行 → 全件 → SELECT 確認 → 24h retrospective review → 補正/rollback) → `db-migration-rules.md`
- **`.md ファイルも誤りを含み得る`** R-1: 実コード != .md 記述 で**コードの方が真実**。.md 記述を疑う癖を維持 → `md-files-can-be-wrong.md`
- **SQLite TIMESTAMP は UTC**、JST 直書き禁止 (`datetime('now', '-N hours')` 推奨) → `sqlite-timezone.md`
- **rule / CLAUDE.md / KB / 設計書 編集時は cascade scan 必須** (関連 file 全件 grep、同 session 更新、両論併記) → `cascade-update.md`

## eBay 物販核心 (詳細: tools/ebay-manager/CLAUDE.md @import)

- **Country of Origin / Country of Manufacture**: eBay 出品文に絶対記載しない (関税リスク)
- **送料**: US 軸差分式 + 4 区分 primary_market、`<ShippingType>Flat</ShippingType>` 必須
- **Manufacturer (通関書類)**: 日本代理店、End Use = 実用途のみ (resale / 中国本社禁止)
- **米国向け = DDP**、Section 232 派生品 25% 直撃で赤字化リスク (販売価格に関税 buffer 必須)

## Snippet index (on-demand、`.claude/rule-snippets/*.md`)

下記は **always-load 対象外**。assistant が該当 topic に触れる時に **Read on-demand** または **UserPromptSubmit router** で JIT inject される:

- `wiki-frontmatter.md` — memory / KB 新規・編集時の frontmatter schema (layer / updated / sources / metadata.wiki_type)
- `contradiction-annotation.md` — 同 topic に新旧矛盾が出た時の両論併記書式 (現状/過去/変更理由 3 block)
- `discord-notification.md` — Discord webhook (`eBay Manager`) 設定、R-11 user 実視認 verify 必須
- `supplier-matching-rules.md` — 仕入先候補 match_score < 60 除外、別 SKU 機会拾い、ジャンク 2 種類判別
- `llm-wiki-compilation.md` — Q7 read-first / INGEST 再構成 / QUERY-save 基準 3 軸 / OPLOG schema

## 完了報告 4 行テンプレ (Q5)

```
- 使用モデル: <Gemini 2.5 Flash / Opus 4.8 / Sonnet 4.6 / Haiku 4.5 等>
- 検証経路: <pytest unit / Playwright UI / eBay VerifyAdd XML / DB SELECT>
- 実機ログ: <scheduler.log の抜粋 or "確認不要">
- 残リスク: <文章 or "なし">
```
