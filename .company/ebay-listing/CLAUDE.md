---
type: department
name: eBay出品 (stub、agent 真実源化済)
created: "2026-04-02"
harness_status: stub-2026-04-30-w2-d7-s1
source_of_truth: .claude/agents/ebay-listing.md
---

# eBay出品部署 (stub、2026-04-30 W2-D7-S1 で agent 真実源化)

本部署のルール **本体は移行済**。本ファイルは部署フォルダ存在確保のための stub。

## ルール本体の所在 (本 stub からは再読込不要)

- 出品ルール本体: `.claude/agents/ebay-listing.md` (201 行、Step 1-2 商品調査 / 正確性原則 / Condition マッピング 8 段階自社規約 → eBay ConditionID 4 体系 (1000/1500/3000/7000) / HTML 4 構成 / XML 制約チェック表 / DoD 11 項目 / W27 Research 脳連携)
- 規制 4 セクション (出品 / 通関 / DDP-Section 232 / コンディションランク): `tools/ebay-manager/CLAUDE.md`
- 横断 rule (Karpathy / silent-skip / supplier-matching / db-migration): `.claude/rules/`

## 部署フォルダ役割 (維持)

- `drafts/` 出品ドラフト保存先 (agent が `YYYY-MM-DD-[商品略称].md` で書込)
- `drafts/_archive/` 過去ドラフト (2026-04-30: pre-customs-rule-fix 移動済 2 件)

## 連鎖参照ポイント (本 stub に到達したら)

- subagent `ebay-listing` 起動経路: `.claude/agents/ebay-listing.md` の L13「本 file 読み込み指示」→ 本 stub に到達 → 上記 source_of_truth で agent 内のルールに従って動作 (本 stub の再読込不要)
- skill `/listing` 経路: `.claude/commands/listing.md` の L1「本 file 読み込み指示」→ 本 stub に到達 → subagent `ebay-listing` 起動
- `.company/CLAUDE.md` L38 部署一覧 / `.company/daily-operations/CLAUDE.md` L33 参照: 本フォルダ存在確認のみ (stub で OK)

## 解体経緯 (W2-D7-S1)

- 2026-04-30: 196 行 → stub 化 (約 35 行)。dept ↔ agent の 60-70% 内容重複 + ルール矛盾 (eBay condition 7 段階 vs 自社規約 8 段階) を agent 真実源化で解消
- 移植先: `.claude/agents/ebay-listing.md` (Step 1-2 商品調査チェックリスト / 正確性原則 / Condition マッピング表 / HTML 4 構成 / 出品メタデータ概念 / Condition Description 250 字注記)
- 旧 196 行版: 過去 session memory (`session_2026_04_30_w2_d10_s3_clear_discipline.md` の W2-D7-S1 関連 section) で内容詳細を確認可能。本プロジェクトは非 git のため archive 物理保存なし
