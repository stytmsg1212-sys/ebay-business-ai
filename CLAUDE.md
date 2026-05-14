# eBay物販ビジネスAI

本ファイルが他のすべての指示に優先する。本プロジェクトは金銭損失に直結するため、違反は **品質事故** として session memory に記録する。

## 思考姿勢: Karpathy 4 原則 (常時)

- **K0 Think Before Coding**: 仮定を明示、複数解釈を user に提示、混乱を抱えたまま進まない
- **K1 Simplicity First**: 最小コード、要求外機能 / 抽象化 / configurability 禁止 (3 回出てから共通化)
- **K2 Surgical Changes**: 関係ない code/comment/formatting を「ついでに直す」禁止
- **K3 Goal-Driven**: 抽象タスク → measurable goal、変更前後で outcome verify

詳細: `.claude/rules/karpathy-principles.md`

## 最優先ルール Q0-Q6 (違反 = 品質事故)

- **Q0** サイレントスキップ / 偽装成功 / 逃避修正は **絶対禁止**。詳細: `.claude/rules/silent-skip-prevention.md`
- **Q1** UI / 定時実行バグ修正 DoD = 11 ステップ Phase 0-3 全て (pytest PASS だけ NG、Streamlit + Playwright + DB クエリ + scheduler.log 必須)。詳細: `feedback_definition_of_done_protocol.md`
- **Q2** DB migration 冪等性必須 (try/except OperationalError、DROP/DELETE は別 one-shot script、本番直接書込時 24h retrospective review)。詳細: `.claude/rules/db-migration-rules.md`
- **Q3** 新機能 / 外部 API / 見積不確実な変更は **`/feature-dev` 必須**、Phase 3 Clarify 省略禁止。詳細: `feedback_feature_dev_usage.md`
- **Q4** コード変更後 code-reviewer agent で **HIGH=0** まで修正ループ。DB 直接書込後は retrospective review。詳細: `feedback_auto_review_after_changes.md`
- **Q5** 完了報告: 「使用したモデル」明示、多段パイプライン部分実装時は未実施フェーズ冒頭明記、Phase 0 発見併記
- **Q6** モデル選定: Opus 4.7 (業務判断 / 動画深掘り / Research、1 日 30 calls) / Sonnet 4.6 (多制約) / Haiku 4.5 (bulk / 短文、デフォルト) / Gemini 2.5 Flash (動画ネイティブ)。詳細: `feedback_model_selection_policy.md`

### Q5 完了報告 4 行テンプレ

```
- 使用モデル: <Gemini 2.5 Flash / Opus 4.7 / Sonnet 4.6 等>
- 検証経路: <pytest unit / Playwright UI / eBay VerifyAdd XML / DB SELECT>
- 実機ログ: <scheduler.log の抜粋 or "確認不要">
- 残リスク: <文章 or "なし">
```

## eBay 物販固有ルール (核心 3 項目)

- **Country of Origin / Country of Manufacture**: 出品文に絶対記載しない (関税リスク)
- **送料**: US 軸差分式 + 4 区分 primary_market 別運用 (詳細: `reference_shipping_tariff_logic.md` v1.0 必読). `<ShippingType>Flat</ShippingType>` 必須. 暫定: 既存出品は `price * 0.20` (β fix `<ShippingServiceCostOverrideList>` 経由、4 区分別出し分けは候補 D 待ち)
- **Manufacturer (通関書類)**: 日本代理店、End Use = 実用途のみ (`Resale` / 中国本社禁止)

詳細 (DDP / Section 232 / コンディションランク 8 段階 / SKU 規約 / VeRO / XML 制約 等): `tools/ebay-manager/CLAUDE.md` (`@import` で launch load 済)

## 役割分離: CLAUDE.md vs rule vs auto memory

| ファイル | 役割 |
|----------|------|
| 本 CLAUDE.md (project root) | プロジェクト構造・絶対ルール・規約 |
| `~/.claude/rules/*.md` (user global) | 全プロジェクト共通 coding / security |
| `.claude/rules/*.md` (project 横断) | Karpathy / DB migration / silent-skip / supplier-matching / **sku-rules** / **md-files-can-be-wrong** / **discord-notification** |
| `tools/ebay-manager/CLAUDE.md` (subdir) | eBay 規制 4 セクション (出品 / 通関 / DDP / ランク) |
| `USER_MANUAL.md` (project root) | **user (人間) が手で実行する手順** 集約 (scheduler 操作 / Phase 7 監視・緊急停止 / kill switch / メンテ / トラブル対処 / slash command 早見表) |
| `~/.claude/projects/.../memory/feedback_*.md` | 個別事故 / 学び |
| `~/.claude/projects/.../memory/session_*.md` | セッション総括・履歴 |
| `~/.claude/projects/.../memory/project_*.md` | 機能別状況 (W 番号) |

判断基準: 「全セッションで毎回参照?」 → CLAUDE.md / 「特定の事故 / 1 機能?」 → memory。CLAUDE.md は容赦なく編集 (Boris Tip 4)。

## 文脈管理ガイド (Boris Tip 18)

- **/clear**: 1 機能 (W 番号) のクローズ後、または話題切替時
- **/compact**: 同一機能の長大セッションで過去ログ詳細不要時 (事前に session_*.md 総括必須)
- **context_health**: session memory に turn_count / last_clear_at / last_compact_at / perceived_drift を毎回記録、major で強制 /clear

## Quality Gate (Boris Tip 24 機械強制)

PreToolUse hook (`.claude/hooks/quality-gate.sh`) で **物理 BLOCK**:
- `print(file=sys.stderr)` (pythonw [Errno 22])
- `except: pass` / `except Exception: pass`
- ALTER TABLE 無 try/except OperationalError
- migration 内 DROP TABLE / DELETE FROM

PostToolUse 警告: `success: True in except` / `INSERT OR IGNORE without rowcount` / eBay API 変更

## ビジネス概要

- 事業: eBay を通じた越境 EC 物販ビジネス
- 目標: 商品リサーチ・仕入れ・出品・売上管理・カスタマー対応の効率化

## 組織構成 (cc-company)

```
.company/
├── CLAUDE.md              組織ルール
├── secretary/             秘書室 (窓口・相談・タスク管理、常設)
├── finance/               経理 (売上 / 仕入れ / 経費 / 損益)
├── ebay-knowledge/        eBay 知識 (ポリシー / 規約 / KB)
├── engineering/           システム開発 (ツール / 自動化 / API)
├── daily-operations/      日々業務 (出品 / 在庫 / 注文 / CS)
└── research/              リサーチ (商品 / 競合 / トレンド)
```

## 運営ルール (cc-company)

- **秘書が窓口**: user との対話は常に秘書、部署作業は該当フォルダに書込
- **自動記録**: 意思決定 → `secretary/notes/YYYY-MM-DD-decisions.md` / 学び → `learnings.md` / アイデア → `secretary/inbox/YYYY-MM-DD.md`
- **同日 1 ファイル**: 既存なら追記
- **TODO 形式**: `- [ ] タスク | 優先度: 高/通常/低 | 期限: YYYY-MM-DD`
- **ROADMAP 自動登録**: 新機能口頭依頼即 `data/system_improvements.json` に W 番号登録。詳細: `feedback_roadmap_auto_add.md`

## Path-scoped 固有ルール (subdir CLAUDE.md @import)

Issue #24987 (closed as not planned) により subdir CLAUDE.md の Read tool lazy load が機能しないため、`@import` syntax で launch 時確実 load を強制。

@tools/ebay-manager/CLAUDE.md
