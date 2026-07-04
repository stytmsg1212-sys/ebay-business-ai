# eBay Manager (tools/ebay-manager) 固有ルール

このファイルは `tools/ebay-manager/**` 配下を編集する際に Claude Code が自動 load する subdir CLAUDE.md (公式 lazy-load 機能)。
eBay 出品 / 関税 / 送料 / 商品ランク等の **規制業務 rules** を 1 ファイルに集約 (Cal Rueb red flag #3 対応)。

横断 rule (Karpathy 4 原則 / DB migration 冪等性 / silent skip 禁止 / 仕入先判定) は `.claude/rules/` 配下を参照。

---

## 出品ルール

### 価格管理

- USD 基本通貨。JPY 換算が必要な場合は記録時に為替レートを明記
- **米国向けは DDP 出荷 = 関税は売主負担**。Section 232 派生品 25% 直撃で赤字化リスク
- 販売価格に **関税 buffer 必須** (詳細は本ファイル下の「DDP 出荷 / Section 232」section)
- 利益率を必ず記録 (仕入価格 / 販売価格 / 利益率)

### 送料ルール (US 軸差分式 + 4 区分 primary_market)

詳細: `reference_shipping_tariff_logic.md` v1.0 (2026-05-01 制定、業務仕様の権威).

- **計算式**: `各国表示送料 = (各国実送料 - US 実送料) + (DDP 関税 if 米国向け)`
- **DDP 関税**: 米国向けのみ送料欄に上乗せ (商品価格には含めない、ただし US_only 区分は商品価格包含)
- **4 区分**: US_only / mixed_global / global_only / unknown (Terapeak 365 日 sold で listing 単位判定、v2.0 / W110(2) 2026-05-09)
- **暫定運用**: 4 区分別実装は候補 C/D 進行中、現行 `ebay_lister.py` L222 は `price * 0.20` (β fix `<ShippingServiceCostOverrideList>` で BP override 経由)
- XML 必須要素: `<ShippingType>Flat</ShippingType>` 維持
- ShipToLocations: 全 4 区分とも全国必須 (eBay 仕様で US 除外不可)
- 検証: eBay GetItem API で実反映確認 (pytest だけで完了宣言禁止 / Q1)

### Country of Origin / Manufacturer の layer 分離

- **eBay 出品文 (Title / HTML description / Item Specifics)**: Country of Origin / Country of Manufacture / Manufacturer の **いずれも記載禁止** (US Customs が原産国を再計算する根拠を与えない)
- **通関書類 (FedEx Invoice / HS code)**: Manufacturer = **日本代理店** (詳細は本ファイル下の「通関ルール」section)。**この通関書類側の Manufacturer 運用は不変** — 上記の eBay 出品文からの除外と混同しないこと (layer が別)

#### 現状の見解 (2026-07-04 〜)

原産国・製造者系の禁止 Name 集合 (`country of origin` / `country/region of manufacture` / `country of manufacture` / `manufacturer`、大文字小文字表記ゆれ含め一切禁止) を **4 層で除外する多層防御** を実装済み (#44):

1. 参考 listing からの ItemSpecifics Keys 抽出時 (`monitor/ebay_reference_fetcher.py`、源流フィルタ)
2. 生成 LLM の parse 結果 (`monitor/listing_generator.py`。プロンプト guard だけでは LLM 遵守を保証できないため機械的除外も併用)
3. AddItem XML 組立時 (`monitor/ebay_lister.py`)
4. ReviseItem 送信直前 (`monitor/ebay_client.py` の `_is_forbidden_specific_name` / `revise_item_specifics`。除外した Name は `removed_names` で呼出元に報告)

除外は Q0 (silent skip 禁止) のため各層で `logger.warning` により痕跡を残す。**ItemSpecifics の更新経路 (新設)**: 出品後の Item Specifics 修正は `ebay_client.revise_item_specifics` 経由。ReviseItem の NameValueList は全置換仕様のため、入力・現行 (merge元) どちらに原産国系 Name が含まれていても送信前に自動除去される。

#### 過去の見解 (〜2026-07-04)

「混同事故防止: eBay XML builder は Manufacturer 欄を **空文字列で送出**」と記述していた。

#### 矛盾点 / 変更理由

- 変更日: 2026-07-04
- 契機: #44 原産国混入チェーン封鎖対応。参考 listing の ItemSpecifics Keys がノーフィルタで抽出され、生成プロンプトの「Keys 完全一致」指示に乗って Country of Origin / Manufacturer 等がそのまま AddItem XML に送出されるバグが発覚
- 何が違うか: 「eBay XML builder は Manufacturer 欄を空文字列で送出」という実装は **調査時点で存在しなかった** (`md-files-can-be-wrong.md` R-1 実例)。#44 対応で実際に上記 4 層フィルタとして新規実装した
- 何が同じか: 「eBay 出品文には原産国・Manufacturer を記載しない」という規約自体は不変。通関書類側の Manufacturer = 日本代理店運用も不変 (layer が別、混同しない)

### eBay XML 制約 (出品前 自動 validate)

- **Title ≤ 80 文字** (Mojibake 後文字数 / バイト数注意)
- **Item Specifics 各値 ≤ 65 文字**、**Brand / MPN 必須** (Listing Quality 直撃)
- 中古品 (S/A/B/C/D/PO/As-Is) は **ConditionDescription 必須** (これ無いと defect 増)。**内容の方針は本ファイル下「ConditionDescription 運用方針」section 参照** (短いランク定型文のみ、商品固有の長文は description へ)
- VeRO 該当ブランドは `data/vero_brands.json` で事前判定

### SKU 規約 (用途は 2 つのみ、キー使用禁止)

出典: 2026-04-30 SKU 規約改訂。詳細経緯: `feedback_sku_misuse_repeat_offense.md` / `.claude/rules/sku-rules.md`

| 在庫種別 | SKU 形式 | 性質 |
|---|---|---|
| **有在庫** | `stock**` で始まる文字列 (stock:01 / stock1 / stock 等、表記揺れあり) | **同一 SKU を多数 listing が持つのが正常** (在庫種別フラグであって集約キーではない。在庫数・識別は `ebay_item_id` 単位、SKU で束ねない) |
| **無在庫** | `ebay**_*****` (例: `ebayyh_p1221413657` / `ebayme_m32400850054`) | SKU 変換 → 仕入先候補 URL |

**SKU の用途は 2 つだけ** (これ以外で SKU を使うのは絶対禁止):
1. 有在庫 / 無在庫 の判定 (prefix で判別)
2. 無在庫の場合、SKU 変換 → 仕入先候補 URL を得る

**絶対禁止** (違反 = 品質事故。詳細: `.claude/rules/sku-rules.md`):
- ❌ SKU を listing 一意キー (主キー / 重複検出キー) として使う
- ❌ `WHERE sku=?` で 1 listing 特定 / `WHERE sku IN (...)` 複数抽出 / `GROUP BY sku` 集計 / `UNIQUE(sku)` 制約
- ❌ `JOIN ON a.sku = b.sku` / `dict[sku] = listing` / `set(skus)` 重複排除
- ❌ 「同 SKU が複数 listing に存在 = 異常」と判定する

**listing 識別は必ず `ebay_item_id`** を使う (eBay 側の一意 ID、migration v26 で listing 単位化済)。

**判定 OK な使い方** (2026-04-30 user 公認、上記用途 2 つに限定):
- 有/無在庫判定: `sku.startswith("stock")` / `sku.startswith("ebay")` / SQL `WHERE sku LIKE 'stock%' OR sku LIKE 'ebay%'`
- 無在庫の仕入先 URL 変換: `sku_mapping_manager.generate_url(sku)`

ログ / pending_market_changes 通知 = SKU + タイトル併記 (区別容易化のため)。

### 商品の呼称 (UI / 対 user メッセージ)

- ❌ 「stock:01 が...」「ebayyh_p1221413657 を出品」 → SKU は商品識別に使えない (有在庫は同 SKU 共有が正常、無在庫は URL そのもので冗長)
- ✅ 「Audio-Technica ATH-CKS330NC が...」 → `ebay_listings.title` で呼ぶ
- 同タイトル listing が複数存在する場合 = `title (ebay_item_id 末尾 4 桁)` で区別

### eBay ポリシー遵守 (常時参照)

- 出品ポリシー / 禁止商品 / VeRO を出品前に必ず参照
- カスタマーサポート: 返信 **24h 以内**
- **Defect 率最優先** (アカウント停止リスク直結)
- Promoted Listings = 2% (W21 標準)

### 検証 (Q1 DoD 11 ステップに従う)

- 出品 / 価格 / 送料 / 文言変更後は **eBay GetItem で実反映確認** 必須
- pytest PASS のみで完了宣言は K3 違反 = 禁止
- Streamlit 再起動 + Playwright MCP で 1 往復 E2E + DB 直接クエリ

---

## 通関ルール (FedEx / UPS / DHL 通関書類回答)

出典: 2026-04-24 FedEx TRK#xxxxxxxxxxxx ドラフト事故 (馬鹿正直記述で user 厳重注意)

**核心**: 商業転売 / 中国原産 / 高関税対象を **不必要に自認しない**。回答は事実だが戦略的に。

### Manufacturer Name

- **第一選択**: 日本国内の正規代理店 (SKT株式会社 / Razer / Le Creuset Japan K.K. 等)
- **NG**: 中国・東南アジア本社を第一選択にしない。原産国情報を不必要に強調しない
- **例外**: ブランド本社が米国内 (Razer Inc. 米国 HQ 等) なら堂々と記載 OK

### End Use

- **第一選択**: 商品の **実用途** のみ (例: e-reader → "Personal e-book reading device")
- **NG**: 販売チャネル / `resale` / `commercial` / `eBay` を書かない
- 狙い: FedEx の "End Use" は「何に使うものか」を問うており、商取引形態を求めていない

### 素材記述 (鉄鋼・アルミ関税対策)

- アルミ・鉄を含まない商品は **明示的に "No aluminum or steel parts"** と書く (Section 232 派生品の対象外宣言、詳細は本ファイル下の「DDP 出荷 / Section 232」section)

### 定型句 (末尾必須)

```
The shipper is a retailer and is not the manufacturer.
```

→ 発送人 = 製造元でないことを明記、法的立場の切り分け

### HTS コード

- 根拠 Ruling (例: NY N215220) を脚注で引用
- 最終判断は現地通関士に委ねる: `Please verify with your customs broker.`

### 運用

- ドラフトは必ず `.company/daily-operations/fedex-drafts/YYYY-MM-DD-TRK_xxx.md` に保存
- 商品写真は `*-photos/` 配下に DL して添付準備
- **v2 レビュー必須**: 以下 2 経路で過去応答と照合
  - Gmail (MCP or web): `to:paperwork@fedex.com OR to:customs@fedex.com` で過去 1 年検索
  - 0 件時: `.company/daily-operations/fedex-drafts/` 配下の直近 5 件を grep

---

## DDP 出荷 / Section 232

出典: 2026-04-25 TRK#xxxxxxxxxxxx (Netsuken NV-25 / $798) で Section 232 派生品 25% 関税 ~$200 売主負担が判明

### DDP ルール (米国向け原則)

- 米国向け発送 = **DDP (Delivered Duty Paid)** 運用
- 通関時の **全関税・税金・FedEx Disbursement Fee は売主負担** (TOYOTASUMI)
- buyer は追加請求なし (Negative feedback リスクなし、ただし **利益直撃**)
- DDU との混同禁止: DDU=情報提供のみ / DDP=直接損益

### 販売価格設計式

```
販売価格 = 原価 + 国際送料 + 関税 buffer + PLS 2% + eBay fee + 利益
                          └ 最低 15% (IEEPA reciprocal)
                          └ Section 232: I-A=50% (純金属) / I-B=25% (派生品) / III=15% (transitional)
```

### Section 232 該当 HS リスト (3 階層、HS で判定)

#### Annex I-A (50%、Chapter 72-74/76 純金属製品、**重量閾値なし=自動課税**)

- HS 73xx (鉄鋼製品 = ストーブ / 鍋 / フライパン / 保温ジャー)
- HS 76xx (アルミ製品)
- HS 74xx (銅製品)

#### Annex I-B (25% 派生品、Chapter 84-87、**metal weight ≥15% で課税**)

- HS 8516.60.40 (電気炊飯器 / オーブン) — Netsuken NV-25 該当
- HS 8418.10/21/29/30/40 (冷蔵・冷凍)
- HS 8501.64 (特定モーター) / 8504.31-33 (変圧器)
- HS 8415 (エアコン) / 8517.71 / 8544.42-49 (電線)
- HS 8708.xx (自動車部品) / 8716.xx (トレーラー)
- 重量算定根拠を記録 (customs_requests に WORKSHEET 添付)

#### Annex III (15% transitional、~2027-12-31)

- HS 8421.29 (液体ろ過) / 8424.89.90 / 8428.32-70 (コンベア / 産業ロボット)

### IEEPA 重複回避

- **Section 232 該当品は IEEPA reciprocal 15% exempt** (二重課税防止)
- 該当判定後は IEEPA 計算除外、Section 232 のみ適用
- ⚠️ **例外**: semiconductors / automotive parts (HS 8708 等) は IEEPA exempt 対象外、IEEPA 重複適用リスクあり

### 出品判断ルール

- **High-value 商品 ($500+)**: 出品前に customs broker classification 確認推奨
- **同型番リピート出品**: `customs_requests` / freee の該当案件を参照、関税実績を価格反映
- **赤字案件化判定**: 粗利率 30% 未満 + Section 232 該当 = **user に通知して承認待ち** (assistant 自動 BLOCK しない、user 機会損失リスク回避)

### 詳細 KB 参照

`.company/ebay-knowledge/topics/section_232_tariff_2026_04.md` (2026-04-06 改訂、Annex I-A/I-B/II/III/IV 全 HTS リスト、計算ワークフロー、ケーススタディ収録)

⚠️ **最終確認: 2026-04-30 / 高 value 商品 ($500+) 出品時は CBP CSMS で再確認必須** (CBP CSMS は 2-4 週で改訂・追補が出る)

---

## コンディションランク 8 段階

出典: 全 eBay 出品で一貫適用 (W9 individual-listing で Claude 自動推定)。外観 × 動作確認の 2 軸統合。

### 8 段階体系

| Rank | EN | JP | eBay Cond ID | 適用 |
|---|---|---|---|---|
| N | New | 新品・未開封 | 1000 | シュリンク / 工場出荷 |
| S | New (Opened) | 新品同様 | 1500 (※) | 開封済みだが未使用、使用痕なし |

※ **Cond ID 1500 はカテゴリ依存** (Consumer Electronics > Portable Audio & Headphones 等で制限)。GetCategoryFeatures / Taxonomy API で事前確認、不可カテゴリでは **1000 fallback** (条件満たす場合) or **3000 + "Open box" description** に降格。出品時 VerifyAdd で再検証必須 (Q0 サイレントスキップ防止)。

| A | Excellent | 美品・動作確認済 | 3000 | 小さな使用痕、全機能動作 |
| B | Good | 並品・動作確認済 | 3000 | 目立つ使用痕、全機能動作 |
| C | Fair | 使用感あり・動作確認済 | 3000 | 使用感強い、全機能動作 |
| D | Issues | 難あり・動作確認済 | 3000 | 外観/機能に問題、動作するが限定 |
| PO | Power-On Only | 通電のみ、動作未確認 | 3000 | 電源 ON 確認だけ |
| As-Is | As-Is | 未確認 or 部品取り | 7000 | 無保証販売、**理由必須** |

### N vs S 判別

- ✅ 家電量販店の新品シュリンク品 → **N**
- ❓ デッドストック / 未使用だが保管年数長い → **S 推奨**
- ❓ 個人出品の「新品未使用」(開封痕確認困難) → **S 推奨**
- **VeRO リスク** (Apple / Nintendo 等): 非正規ルート品は **S 以下** が安全

### Claude 自動推定 (仕入先日本語キーワード)

| 仕入先表記 | 推定ランク |
|---|---|
| 「新品」「未開封」「シュリンク付き」 | **N** |
| 「新品同様」「未使用」「開封品」 | **S** |
| 「美品」「美品に近い」 | **A** |
| 「良品」「並品」「普通」 | **B** |
| 「使用感あり」 | **C** |
| 「傷あり」「難あり」「訳あり」 | **D** |
| 「通電確認のみ」「通電のみ」 | **PO** |
| 「動作未確認」「ジャンク」「部品取り」「故障」 | **As-Is** |

### ブランド別特例

- **PIONEER Lonesome Carboy 等年代物 AV**: 動作確認必須、ジャンク即 As-Is
- **KEYENCE センサー単体**: ジャンクでもテスト前提で B/C 推定可
- **本 rule 内では 2 例のみ抜粋**。VeRO ブランド (Apple / Nintendo / SONY 等) や Audio/AV/産業機器の判定は必ず `feedback_condition_by_brand.md` を参照。未収載ブランドは N/S 判定前に該当 memory check 必須

### Quick Notes (description aside 冒頭、Rank Definition Table と併設)

- **A/B/C/D**: 具体的動作確認結果。例: `Tested and confirmed working (2026-04). Power on/off: OK / Audio: OK / Bluetooth: OK`
- **PO**: `Powered on, but full function not verified`
- **As-Is**: **必ず理由明示**。例: `No AC adapter for testing` / `PCB burn damage` / `Heavy contamination prevented testing`
- Quick Notes は **description 本文用の詳細情報**。eBay XML `<ConditionDescription>` (下記) とは別フィールド・別役割 — 混同しない

### ConditionDescription 運用方針 (2026-07-04 更新 → 2026-07-04 書式改訂 358754421540)

`condition_description` (eBay XML `<ConditionDescription>`、買い手に表示されるコンディション説明) は **ランクの短い定型文のみ** (`tabs/_finishing_panel_state.RANK_CONDITION_DESCRIPTION_TEMPLATE` から決定論的に導出、65 字以内・英語)。Quick Notes (description 本文) とは別フィールドで役割が異なる:

- **ConditionDescription = 「Rank X — Label. <状態文>.」形式** (user 追加報告 358754421540 で「conditionはランクを記載」= ランクを明示してほしい、との意図を反映)。例:
  - S: `Rank S — New (Opened). Unused, no visible wear.`
  - A: `Rank A — Excellent. Tested, fully working. Minor wear.`
  - B: `Rank B — Good. Tested, fully working. Visible wear.`
  - C: `Rank C — Fair. Tested, fully working. Heavy wear.`
  - D: `Rank D — Issues. Tested; works within limits.`
  - PO: `Rank PO — Power-On Only. Full function not verified.`
- **N (ConditionID 1000)**: eBay 仕様上 CD 非対応のため送信しない (空文字を返す。UI パネル / apply 層 / AddItem の 3 経路すべてで抑止)。
- **付属品欠品・傷の位置・詳細な使用感などの商品固有の長文情報は ConditionDescription に書かない**。それらは Quick Notes / includes_items / description 本文へ記載する
- **As-Is のみ例外**: 理由を ConditionDescription へ必ず転記 (下記「As-Is 出品の XML 必須要件」、eBay 対策として不変。書式 = `As-Is — <reason>` 65 字以内)
- 原産国 (Country of Origin/Manufacture) や Manufacturer に触れる語は一切含めない (上記「Country of Origin / Manufacturer の layer 分離」)
- 実装 (単一情報源): `tabs/_finishing_panel_state.RANK_CONDITION_DESCRIPTION_TEMPLATE` + `resolve_condition_description_for_rank()`。UI パネル (#44) / AddItem (`ebay_lister.build_draft_params_from_phase3`) / ReviseItem (`_apply_content_changes` の rank/cd 経路) 全てがこの helper に一本化されている (2026-07-04 358754421540 対応)
- 実装 (AI 生成プロンプト): `monitor/listing_generator.py`「Condition Description ルール」(Claude の `condition_description` 出力は上記テンプレに常に上書きされる = プロンプト非遵守時の保険が入っている)

### As-Is 出品の XML 必須要件

- eBay XML `<ConditionDescription>` に Quick Notes の As-Is 理由を **必ず転記** (上記「ConditionDescription 運用方針」の唯一の例外、eBay 対策として維持)
- **65 字以内** (eBay 制約) / 英文 / `As-Is — <reason>` 形式
- 欠落時は VerifyAdd 警告だが通る → buyer 紛争で **Defect 確定リスク** (アカウント停止直結)

### タイトル / description

- タイトルには Rank 表記 **しない** (80 字制限圧迫防止)
- description aside 冒頭に **Rank Definition Table** 含める
- テンプレート: `.company/ebay-knowledge/topics/listing-description-template.md`

---

## 関連 rule (横断)

always-load (`.claude/rules/` 配下):
- `karpathy-principles.md` — Karpathy 4 原則 (K0-K3 常時適用)
- `db-migration-rules.md` — DB 冪等性 (try/except OperationalError、DROP one-shot 化、24h retrospective review)
- `silent-skip-prevention.md` — Q0 サイレントスキップ / 偽装成功 / 逃避修正 絶対禁止

on-demand snippet (`.claude/rule-snippets/` 配下、2026-05-21 hybrid 化):
- `supplier-matching-rules.md` — 仕入先候補判定 (match_score < 60 除外、別 SKU 機会、ジャンク表記判別)
