# Section 232 関税 2026-04 改訂版 KB

**最終更新**: 2026-04-25
**情報源**: ホワイトハウス公式 Annexes I-A/I-B/II/III/IV PDF（2026-04-02 Proclamation）+ White & Case / Phillips Lytle / GHY / Greenberg Traurig 解説

## 1. 改訂サマリ（2026-04-06 発効）

| 区分 | 税率 | 対象 | HTSUS heading |
|------|------|------|---------------|
| **Annex I-A** | **50%** | 主要鉄/アルミ/銅製品（Chapter 72-74, 76 中心、280 コード） | 9903.82.02 |
| **Annex I-B** | **25%** | 鉄/アルミ/銅 derivatives（410 HTSUS コード、Chapter 84-87 含む） | 9903.82.04-17 |
| **Annex II** | 適用除外 | 食品、化学品、化粧品、motorcycle 部品など | — |
| **Annex III** | **15%** （transitional） | 産業用機械の一部、Dec 31 2027 まで | — |
| **Annex IV** | （技術付録） | I-A/I-B を HTSUS 9903.82.XX へ翻訳 | — |
| US-melted exemption | **10%** | 米国溶解金属 95%+ 含有品 | — |
| **重量閾値ルール** | **適用** | Chapter 72/73/74/76 以外は metal weight ≥15% で課税 | — |

## 2. eBay 物販で頻出する HTSUS の Annex 該当性

### 🔴 Annex I-B（25% 確定）対象の HTS（鉄/アルミ derivatives）

| HTS | 商品例 | 注意点 |
|-----|--------|--------|
| **8516.60.40** | 電気炊飯器、オーブン（家庭用） | NV-25 等、本件で確認 |
| **8516.60.60** | 電熱ホットプレート、グリラ等 | |
| **8516.29.00** | 電気スペースヒーター（蓄熱式以外） | |
| **8516.90.50 / .8050** | 電気調理器具の部品 | |
| **7321.xx** | 鋼鉄製ストーブ、レンジ、暖炉 | metal 比率高いため確実に課税 |
| **7322.xx** | 鋼鉄製ラジエータ | |
| **7323.xx** | 鋼鉄製食卓・台所用品 | 鍋、フライパン、保温ジャー等 |
| **7324.xx** | 鋼鉄製衛生陶器 | |
| **8418.10/21/29/30/40** | 冷蔵庫、冷凍庫 | |
| **8415.xx** | エアコン | |
| **8501.64** | 特定モーター | |
| **8504.31-33** | 変圧器 | |
| **8517.71** | 電気通信機器の部品 | |
| **8544.42/49/60** | 絶縁電線、ケーブル | |
| **8708.xx** | 自動車部品（多数） | バンパー、シャシー、ボディ、ホイール等 |
| **8716.xx** | トレーラー部品 | |

### 🟢 Annex II（除外）対象の HTS

motorcycle 製造専用で輸入される Chapter 84/85/87 部品のみ除外。一般輸入には影響なし。

### 🟡 Annex III（15% transitional、〜2027-12-31）

産業用機械中心：
- 8401.40（核反応炉部品）
- 8417.90（産業炉部品）
- 8421.29（液体ろ過装置）
- 8424.89.90（噴霧装置）
- 8428.32/33/39/60/70（コンベア、産業ロボット）
- 8431.39（持上げ機械部品）
※ 家電は対象外。

## 3. 重量閾値ルール（Annex IV §c 第2文）

> "For articles classified in the listed provisions that are not in chapters 72, 73, 74 or 76 of the HTSUS, headings 9903.82.02 and 9903.82.04–9903.82.17 only apply where the weight of the applicable metal is at least 15 percent of the weight of the imported article."

### 解釈
- Chapter 72-74, 76（純金属製品）：閾値なし、自動課税
- Chapter 84-87 等の derivatives：**金属の重量 ≥15% の場合のみ課税**
- 各金属（鉄/アルミ/銅）ごとに独立判定
- 例：8.5kg の家電で steel 4kg(47%) → steel 派生品として課税 / aluminum 0.5kg(6%) → aluminum 派生品としては不課税

## 4. IEEPA reciprocal との関係（重要）

### Section 232 が優先・IEEPA exempt
> "Products subject to Section 232 aluminium, steel, copper, or timber tariffs— but not semiconductors, automotive or automotive parts tariffs— are exempt from IEEPA tariffs."

つまり：
- Section 232 該当品 → **IEEPA reciprocal 15% は適用しない**
- 派生品 25% のみが追加コスト
- Section 232 非該当品（Annex II 等） → IEEPA reciprocal が適用される

### 非該当品の IEEPA Japan 取扱い
- 日本産品の reciprocal IEEPA = **15%（MFN inclusive）**
- MFN < 15% の場合：top-up で 15% に
- MFN ≥ 15% の場合：top-up なし、MFN がそのまま
- WTO 民間航空機協定対象品 / 日本産医薬品 / 半導体は別ルール（IEEPA + Section 232 とも除外または特別レート）

## 5. 計算ワークフロー（DDP 価格設定用）

```
入力: HS コード, 商品価格 USD, 商品重量 kg, 主要材料の重量内訳
↓
Step 1: HS コードを Annex I-A/I-B/II/III と突合
  - I-A 該当 → Section 232 = 50%
  - I-B 該当 → Step 2 へ
  - III 該当 → Section 232 = 15%（〜2027-12-31）
  - II 該当 → Section 232 適用なし、Step 4 へ
  - 全リスト外 → Step 4 へ
Step 2: Chapter 確認
  - 72/73/74/76 → そのまま課税
  - 84/85/86/87 等 → Step 3 へ
Step 3: 金属重量比率を判定
  - steel/aluminum/copper のいずれかが ≥15% → Section 232 課税
  - すべて <15% → Section 232 不課税、Step 4 へ
Step 4: IEEPA reciprocal
  - 日本産で MFN < 15% → IEEPA top-up = (15% - MFN%) × 価格
  - 日本産で MFN ≥ 15% → IEEPA = 0
Step 5: MFN base duty を加算
合計 = Section 232 + IEEPA + MFN + FedEx Disbursement Fee (2-3%)
```

## 6. ケーススタディ：Netsuken NV-25（TRK#870480400096）

```
HS:        8516.60.4000
価格:      USD 798
重量:      8.5 kg（steel 4.0kg=47%, aluminum 0.8kg=9.4%, copper 0.05kg=0.6%）
MFN:       Free
原産国:    日本

Step 1: Annex I-B 該当 ✓
Step 2: Chapter 85 → 重量閾値ルール適用
Step 3: steel 47% ≥15% → Section 232 steel derivative 課税
        aluminum 9.4% <15% → 不課税
        copper 0.6% <15% → 不課税
Step 4: Section 232 該当 → IEEPA exempt
Step 5: MFN Free → 加算なし

合計追加関税: 25% × $798 = $199.50
FedEx Disbursement Fee 2-3%: ~$5-7
DDP 売主負担: 約 $205-207 (≈ ¥31,000)
```

## 7. 実務上のガイドライン

### 出品前チェック
1. 商品の HS code を推定（FedEx Trade Tools 等で）
2. このKBの「Annex I-B 対象 HTSUS」と突合
3. 該当する場合は重量と材料比率を確認
4. 価格設計に Section 232 25% buffer を組込

### 高リスク商品カテゴリ（Annex I-B 該当が多い）
- 家電・調理器具（炊飯器、オーブン、冷蔵庫、エアコン、ヒーター）
- 鋳鉄・ステンレス調理器具（鍋、フライパン、保温ジャー）
- 自動車部品（バンパー、ホイール、シャシー）
- 電線・ケーブル類
- 産業機械の部品（要 Annex III 確認、15% 軽減ありえる）

### 低リスク商品カテゴリ
- 精密電子機器（カメラ、e-reader、ガジェット）→ Chapter 85 内でも HS が異なる
- 衣類、玩具、書籍 → Section 232 対象外
- 非金属製品全般

### 「Section 232 派生品リスクスコアリング」を出品時自動算出する機能（W20 候補）
- HS code 入力 → Annex 該当判定 + 適用税率推定
- 商品重量・材料比率（写真から AI 推定 or seller 入力）→ 課税判定
- 推定追加コスト → 売価に reverse 計算で必要マージン算出

## 8. 公式情報源

- Annexes I-A/I-B/II/III/IV PDF: https://www.whitehouse.gov/wp-content/uploads/2026/04/ANNEXES-I-A-I-B-II-III-IV.pdf
- CBP CSMS # 68253075: https://content.govdelivery.com/accounts/USDHSCBP/bulletins/4117593
- White & Case 解説: https://www.whitecase.com/insight-alert/united-states-modifies-steel-aluminum-and-copper-section-232-tariffs
- Phillips Lytle 解説: https://phillipslytle.com/administration-restructures-section-232-tariffs-on-metal-and-derivative-products/
- GHY International: https://www.ghy.com/trade-compliance/us-adjusts-section-232-tariffs-on-aluminum-steel-and-copper-full-customs-value-now-applies/
- Greenberg Traurig（IEEPA refund + Section 232 重複の取扱い）: https://www.gtlaw.com/en/insights/2026/4/updates-for-importers-on-ieepa-refunds-and-section-232-metals
