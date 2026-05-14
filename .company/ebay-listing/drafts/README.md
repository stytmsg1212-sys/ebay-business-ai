# `.company/ebay-listing/drafts/` について

## 目的

過去出品の通関書類 / FedEx 回答 draft / eBay 出品ドラフトを時系列保管。
**新規 listing のテンプレ流用元ではない**。

## 流用 NG (新規 listing で必ず再生成)

- **Item Specifics** → eBay Taxonomy API で都度再取得 (カテゴリ依存値が変わる)
- **価格 / 関税 buffer** → Section 232 KB (`reference_section_232_kb.md`) で都度再算定
- **HS コード** → 商品ごと CBP CSMS で再確認 (2-4 週で改訂・追補が出る)
- **Country of Origin / Country/Region of Manufacture** → 絶対記載しない (`feedback_customs_response_strategy.md`)

## 流用 OK

- **description aside HTML の構造** (Rank Definition Table テンプレートのみ、本文は商品固有)
- **HTML テンプレート全体構成** = `parts/htmltxt` を真実源とし drafts は参考程度

## archive 注記

- `_archive/2026-04-30-pre-customs-rule-fix/` 配下: customs strategy 制定前 (2026-04-24) のドラフト = ルール違反データ。各ファイル冒頭に違反警告ヘッダ + 行番号付き違反箇所列挙あり。HTML 部分のみ参考可、Item Specifics / 価格は流用 NG。

## 関連 KB

- 通関 draft 規約: `tools/ebay-manager/CLAUDE.md` 「通関ルール」section
- DDP / Section 232 buffer: `feedback_ddp_shipping_policy.md` / `reference_section_232_kb.md`
- 出品ドラフト生成: `.claude/agents/ebay-listing.md` (subagent、真実源 W2-D7-S1 で agent 化)
