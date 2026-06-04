# eBay 実手数料 vs 計算式予測 突合分析 (2026-06-03)

出典: eBay Finances API `/sell/finances/v1/transaction` (直近 180 日, scope=sell.finances, GET only). calculator.calculate (FVF / INTERNATIONAL / AD / PAYONEER / TXN_FEE) を sales_history + ebay_listings の category_id / weight_g で再計算し突合.

対象: SALE transaction. SALE=351, NON_SALE_CHARGE=672 (Promoted Listings等 別 entry), DB と紐付け成功 (sales_history.ebay_order_id 一致) = 119. 全 SALE 集約売上 $89469.07 / SALE 内 手数料 $14522.50 / 実効率 16.23% / NON_SALE_CHARGE 込み実効率 18.60%.

### transactionType 分布

| transactionType | 件数 |
|---|---:|
| NON_SALE_CHARGE | 672 |
| SALE | 351 |
| REFUND | 38 |
| ADJUSTMENT | 8 |
| TRANSFER | 2 |
| CREDIT | 1 |
| DISPUTE | 1 |

## 核心結論

- **matched 平均 実効率**: 16.70% (median 15.23%, range 8.52%–95.20%, n=119)
- **calculator 予測平均 (FVF+INTL+AD+Payoneer+TxnFee)** = 現在の settings (FVF カテゴリ別, INTL 1.2%, AD 2.0%, Payoneer 2.0%, TxnFee ¥62.8/件) で再計算.
- **component 別 gap (実 - 予測, USD, matched 合計)**:
  - FVF 系: $453.94 (実 $5781.18 vs 予測 $5327.24)
  - INTERNATIONAL: $-28.13 (実 $475.26 vs 予測 $503.39)
  - AD / PROMOTED: $-838.95 (実 $0.00 vs 予測 $838.95)
  - REGULATORY_OPERATING_FEE: $0.00 (calculator に該当 component なし = 全額 gap、新規追加候補)

## fetch 結果

- success: True
- fetched (transactions): 1073
- pages: 6
- truncated: False
- last_status: 200

## feeType 別実績 (全 SALE + NON_SALE_CHARGE 集約)

| feeType | USD 合計 (net) | 比率 (vs 売上) |
|---|---:|---:|
| FINAL_VALUE_FEE | $13221.29 | 14.78% |
| AD_FEE | $1896.16 | 2.12% |
| INTERNATIONAL_FEE | $1146.77 | 1.28% |
| FINAL_VALUE_FEE_FIXED_PER_ORDER | $154.44 | 0.17% |
| OTHER_FEES | $144.84 | 0.16% |
| PREMIUM_AD_FEES | $89.32 | 0.10% |
| INTERNATIONAL_LISTING_FEE | $-14.30 | -0.02% |

### うち NON_SALE_CHARGE 内訳 (net = debit - credit)

| feeType | USD 合計 (net) |
|---|---:|
| AD_FEE | $1896.16 |
| OTHER_FEES | $144.84 |
| PREMIUM_AD_FEES | $89.32 |
| INTERNATIONAL_LISTING_FEE | $-14.30 |

NON_SALE_CHARGE 合計 (net): $2116.02

## 実効率分布 (matched, sold_price_usd で割った値)

- n = 119
- 平均: 16.70%
- median: 15.23%
- stdev: 8.83%
- 範囲: 8.52% – 95.20%

## per-order 突合 (matched のみ, 上位 50 件 amount 降順)

| order_id | date | title | qty | sold_USD | 実fee | 予fee | gap | rate% |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 4381-63993 | 2026-03-23 | KEYENCE XG-X2700 Image Process | 1 | $1890.00 | $366.63 | $369.24 | $-2.61 | 19.40% |
| 4308-23836 | 2026-02-27 | Mitutoyo SJ-210 Surface Roughn | 1 | $1112.00 | $193.11 | $217.41 | $-24.30 | 17.37% |
| 4275-96319 | 2026-02-25 | Oriental Motor BMUD200-A Brush | 1 | $210.66 | $200.55 | $41.50 | $159.05 | 95.20% |
| 4406-99668 | 2026-03-27 | FUJI ELECTRIC FSCS10A1-00J POR | 1 | $889.00 | $161.37 | $173.90 | $-12.53 | 18.15% |
| 4285-09225 | 2026-02-26 | DENSO DST-i Scan Tool Diagnost | 1 | $860.00 | $128.99 | $168.23 | $-39.24 | 15.00% |
| 4309-36274 | 2026-03-06 | Tektronix TDS3054C Digital Pho | 1 | $980.00 | $149.99 | $191.65 | $-41.66 | 15.31% |
| 4365-72268 | 2026-03-13 | Razer HyperFlux V2 Hard Editio | 1 | $197.98 | $107.51 | $39.03 | $68.48 | 54.30% |
| 4492-40485 | 2026-04-09 | Netsuken NV-25 Electric Sushi  | 1 | $798.00 | $144.26 | $156.13 | $-11.87 | 18.08% |
| 4312-71522 | 2026-02-28 | Keithley 6485 Picoammeter pAmm | 1 | $895.00 | $134.83 | $175.06 | $-40.23 | 15.06% |
| 4318-52587 | 2026-03-02 | Netsuken NV-25 Electric Sushi  | 1 | $798.00 | $142.47 | $156.13 | $-13.66 | 17.85% |
| 4305-36804 | 2026-03-02 | FOSTEX R100T Attenuator Transf | 1 | $798.00 | $91.53 | $156.13 | $-64.60 | 11.47% |
| 4662-57695 | 2026-05-26 | KEYENCE SR-G100 Handheld Code  | 1 | $829.68 | $125.02 | $162.32 | $-37.30 | 15.07% |
| 4661-37902 | 2026-05-22 | Genuine Leica Single Prism Set | 1 | $398.58 | $121.34 | $78.18 | $43.16 | 30.44% |
| 4326-44083 | 2026-03-11 | Tektronix TDS754D Digital Osci | 1 | $780.00 | $121.01 | $152.62 | $-31.61 | 15.51% |
| 4398-29288 | 2026-03-20 | Mitsubishi Uni color 240 50th  | 1 | $690.00 | $132.54 | $135.05 | $-2.51 | 19.21% |
| 4435-82656 | 2026-03-31 | Keyence CA-LMHE20 Telecentric  | 1 | $608.25 | $104.79 | $119.10 | $-14.31 | 17.23% |
| 4259-77236 | 2026-02-25 | Keysight E3633A DC Power Suppl | 1 | $699.98 | $95.76 | $137.01 | $-41.25 | 13.68% |
| 4392-11400 | 2026-03-23 | Keithley 6485 Picoammeter pAmm | 1 | $700.00 | $105.55 | $137.01 | $-31.46 | 15.08% |
| 4320-83590 | 2026-03-05 | YOKOGAWA DL1740E Digital Oscil | 1 | $680.00 | $93.04 | $133.10 | $-40.06 | 13.68% |
| 4367-08728 | 2026-03-16 | Pioneer Lonesome Car-Boy KP-70 | 1 | $649.00 | $73.98 | $127.05 | $-53.07 | 11.40% |
| 4360-78132 | 2026-03-15 | TANGO FW-20S Hi-Fi Output Tran | 1 | $498.00 | $72.87 | $97.58 | $-24.71 | 14.63% |
| 4268-01735 | 2026-02-26 | TRAVELER'S notebook Limited Se | 1 | $598.00 | $81.88 | $117.10 | $-35.22 | 13.69% |
| 4394-66599 | 2026-03-19 | KEITHLEY 6485 Picoammeter For  | 1 | $498.00 | $75.21 | $97.58 | $-22.37 | 15.10% |
| 4486-94573 | 2026-04-08 | Oriental Motor BMUD200-A Brush | 1 | $210.66 | $81.43 | $41.50 | $39.93 | 38.65% |
| 4253-71474 | 2026-02-22 | GLSSWRKS Hana Premium Glass Mo | 1 | $360.00 | $66.66 | $70.65 | $-3.99 | 18.52% |
| 4298-88923 | 2026-03-04 | HIOKI DT4282 Digital Multimete | 1 | $360.00 | $59.40 | $70.65 | $-11.25 | 16.50% |
| 4366-12833 | 2026-03-18 | BEASTARS Season1 Vol.1 -4 Firs | 1 | $398.00 | $63.47 | $78.06 | $-14.59 | 15.95% |
| 4310-69901 | 2026-03-01 | For sun65218 AGILENT E1709A RE | 1 | $429.00 | $64.86 | $84.11 | $-19.25 | 15.12% |
| 4393-81786 | 2026-03-23 | Pioneer GEX-61 Lonesome Carboy | 1 | $398.00 | $42.27 | $78.06 | $-35.79 | 10.62% |
| 4364-49993 | 2026-03-14 | EchoTech ZO-41 II Hobby Ultras | 1 | $349.00 | $63.21 | $68.50 | $-5.29 | 18.11% |
| 4433-83210 | 2026-03-30 | Strymon Brigadier Delay Effect | 1 | $332.50 | $33.32 | $65.28 | $-31.96 | 10.02% |
| 4667-11501 | 2026-05-21 | Pioneer Lonesome Car-Boy CD-9  | 1 | $358.00 | $37.32 | $70.26 | $-32.94 | 10.42% |
| 4351-56604 | 2026-03-12 | The Art of the Iron Giant Hard | 1 | $368.00 | $78.92 | $72.22 | $6.70 | 21.45% |
| 4682-51160 | 2026-05-30 | Google Pixel Tablet Charging S | 1 | $360.00 | $41.23 | $70.65 | $-29.42 | 11.45% |
| 4302-48874 | 2026-02-26 | HIOKI DT4282 Digital Multimete | 1 | $369.00 | $50.70 | $72.41 | $-21.71 | 13.74% |
| 4684-59764 | 2026-05-27 | SNOOPY IN FASHION & Collection | 1 | $375.98 | $61.96 | $73.76 | $-11.80 | 16.48% |
| 4675-57099 | 2026-05-24 | PLOTTER 5001 Mini 5 Size 5-Rin | 2 | $377.80 | $62.83 | $74.13 | $-11.30 | 16.63% |
| 4279-21366 | 2026-02-24 | LEYBOLD NT10 TURBOTRONIK Turbo | 1 | $370.00 | $56.00 | $72.60 | $-16.60 | 15.14% |
| 4704-38679 | 2026-06-01 | HIOKI DT4282 Digital Multimete | 1 | $356.80 | $49.03 | $70.03 | $-21.00 | 13.74% |
| 4321-28130 | 2026-03-06 | Holbain Artist Colored Pencils | 1 | $296.00 | $57.64 | $58.16 | $-0.52 | 19.47% |
| 4308-72771 | 2026-03-05 | Holbain Artist Colored Pencils | 1 | $296.00 | $57.64 | $58.16 | $-0.52 | 19.47% |
| 4299-27257 | 2026-02-28 | Holbain Artist Colored Pencils | 1 | $296.00 | $57.64 | $58.16 | $-0.52 | 19.47% |
| 4424-93393 | 2026-03-29 | Bowl made of pottery 25 patter | 1 | $350.00 | $57.12 | $68.70 | $-11.58 | 16.32% |
| 4395-02514 | 2026-03-26 | Zojirushi NW-JE10 Pressure Ind | 1 | $319.50 | $57.24 | $62.74 | $-5.50 | 17.92% |
| 4360-14986 | 2026-03-14 | HP 53131A 225MHz Universal Cou | 1 | $348.00 | $57.92 | $68.31 | $-10.39 | 16.64% |
| 4712-69158 | 2026-05-30 | Zojirushi NW-JE10 Pressure Ind | 1 | $319.50 | $49.61 | $62.74 | $-13.13 | 15.53% |
| 4711-97510 | 2026-05-30 | Stax SRM-1/MK-2 Electrostatic  | 1 | $298.00 | $34.66 | $58.55 | $-23.89 | 11.63% |
| 4480-48980 | 2026-04-10 | Google Pixel Tablet Case Hazel | 2 | $306.00 | $42.12 | $60.11 | $-17.99 | 13.76% |
| 4446-67656 | 2026-04-04 | HP 53131A 225MHz Universal Cou | 1 | $298.00 | $41.02 | $58.55 | $-17.53 | 13.77% |
| 4367-85314 | 2026-03-15 | Razer THX Onyx Portable DAC He | 1 | $246.76 | $43.54 | $48.55 | $-5.01 | 17.64% |

## 含意 (money-direct)

1. **`sales_history.ebay_fee_usd = 0.0` ハードコード (task_order_alert.py:653) は実値で埋められる**. Finances API は scope=sell.finances で叩け、order_id 経由で sales_history に INSERT/UPDATE 可能 (本 W は分析のみ、書込は別段).
2. REGULATORY_OPERATING_FEE は本期間 0 (米国 DDP 主体ゆえ妥当).
3. **FVF 実 vs 予測 が 5% 超ズレ**. カテゴリ別 fee table (`data/EbayFeeRates.csv`) の rate / threshold が現行 eBay 課金体系と 一致していない可能性. EbayFeeRates.csv の再検証 + Top Rated Plus 10% 割引 (settings.seller_level="Top Rated") 適用有無を確認.
5. **AD/PROMOTED 実 $0.00 vs 予測 $838.95 が 10% 超ズレ**. campaign bid 2% override が一部 listing に 未適用 (キャンペーン rate 9% が直接かかっている) 可能性, または over-listing cap 追加 fee.

⚠️ 本 W219 段1-2 は分析のみ. calculator/settings/task_order_alert は ★ 触っていない ★. 較正は分析結果を user が確認後、別段で実施.

## 注: Payoneer 手数料は eBay Finances API には現れない

calculator の payoneer 2% は eBay → Payoneer 口座への送金時に Payoneer 側で控除される手数料で、eBay の totalFeeAmount には含まれない. 本分析の "calculator 予測" は Payoneer 込みで予測値が膨らむが、実 (actual_by_type) は eBay 控除分のみで Payoneer 分は含まない. gap を見るときは PAYONEER component を除外して FVF/INTL/AD のみで比較するのが正しい.

---

## 全 matched 行 (JSON 形式 backup, raw データ)

```json
[
  {
    "order_id": "06-14724-56167",
    "txn_date": "2026-06-02T13:20:06.839Z",
    "amount_usd": 162.36,
    "sold_price_usd": 195.0,
    "total_fee_usd": 32.64,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.24,
      "FINAL_VALUE_FEE": 29.96,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 38.45095541401274,
    "predicted_by_type": {
      "FVF": 27.49044585987261,
      "INTERNATIONAL": 2.5987261146496814,
      "AD": 4.3312101910828025,
      "PAYONEER": 3.6305732484076434,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5003 A5 Size 6-Ring Shrink Leather Binder Genuine Le",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "09-14718-32834",
    "txn_date": "2026-06-02T06:03:58.698Z",
    "amount_usd": 231.43,
    "sold_price_usd": 226.0,
    "total_fee_usd": 39.57,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 3.0,
      "FINAL_VALUE_FEE": 36.13,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 44.50191082802548,
    "predicted_by_type": {
      "FVF": 31.85987261146497,
      "INTERNATIONAL": 3.0127388535031847,
      "AD": 5.019108280254777,
      "PAYONEER": 4.210191082802548,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Panasonic AU-XPD1 USB 3.0 Express P2 Card Reader Used Tested",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "18-14698-03859",
    "txn_date": "2026-06-01T00:33:04.604Z",
    "amount_usd": 213.78,
    "sold_price_usd": 244.0,
    "total_fee_usd": 34.22,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.6,
      "FINAL_VALUE_FEE": 31.18,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 48.005095541401275,
    "predicted_by_type": {
      "FVF": 34.394904458598724,
      "INTERNATIONAL": 3.248407643312102,
      "AD": 5.414012738853503,
      "PAYONEER": 4.547770700636943,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Ohuhu illustration Marker Pen 320 Colors Brush & Fine Type w",
    "buyer_country": "IL",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "14-14704-38679",
    "txn_date": "2026-06-01T00:27:02.322Z",
    "amount_usd": 307.77,
    "sold_price_usd": 356.8,
    "total_fee_usd": 49.03,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 3.73,
      "FINAL_VALUE_FEE": 44.86,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 70.03057324840763,
    "predicted_by_type": {
      "FVF": 50.29936305732484,
      "INTERNATIONAL": 4.751592356687898,
      "AD": 7.923566878980892,
      "PAYONEER": 6.656050955414012,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "HIOKI DT4282 Digital Multimeter 10A Slightly New from Japan",
    "buyer_country": "RO",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "06-14711-97510",
    "txn_date": "2026-05-30T17:47:27.025Z",
    "amount_usd": 267.34,
    "sold_price_usd": 298.0,
    "total_fee_usd": 34.66,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 31.06,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 3.16
    },
    "predicted_total_usd": 58.546496815286616,
    "predicted_by_type": {
      "FVF": 42.00636942675159,
      "INTERNATIONAL": 3.968152866242038,
      "AD": 6.617834394904459,
      "PAYONEER": 5.554140127388535,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Stax SRM-1/MK-2 Electrostatic Headphone Amplifier SRM-1 MK-2",
    "buyer_country": "SE",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "05-14712-69158",
    "txn_date": "2026-05-30T11:21:31.053Z",
    "amount_usd": 277.89,
    "sold_price_usd": 319.5,
    "total_fee_usd": 49.61,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 3.42,
      "FINAL_VALUE_FEE": 45.75,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 62.74394904458598,
    "predicted_by_type": {
      "FVF": 45.038216560509554,
      "INTERNATIONAL": 4.254777070063694,
      "AD": 7.095541401273885,
      "PAYONEER": 5.955414012738854,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Zojirushi NW-JE10 Pressure Induction Heating Rice Cooker & W",
    "buyer_country": "AT",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "05-14712-59797",
    "txn_date": "2026-05-30T10:39:41.132Z",
    "amount_usd": 171.23,
    "sold_price_usd": 198.0,
    "total_fee_usd": 30.77,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.11,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 28.22
    },
    "predicted_total_usd": 39.03057324840764,
    "predicted_by_type": {
      "FVF": 27.910828025477706,
      "INTERNATIONAL": 2.6369426751592355,
      "AD": 4.3949044585987265,
      "PAYONEER": 3.6878980891719744,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "TRIFIELD TF2 Digital EMF Meter Electromagnetic Field Radiati",
    "buyer_country": "GR",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "11-14701-72033",
    "txn_date": "2026-05-30T05:06:59.928Z",
    "amount_usd": 169.53,
    "sold_price_usd": 200.0,
    "total_fee_usd": 30.47,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 27.94,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 2.09
    },
    "predicted_total_usd": 39.419108280254775,
    "predicted_by_type": {
      "FVF": 28.191082802547772,
      "INTERNATIONAL": 2.662420382165605,
      "AD": 4.439490445859873,
      "PAYONEER": 3.7261146496815285,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "HP 8447D OPT 010 RF Amplifier 0.1-1300MHz Tested Used Amateu",
    "buyer_country": "TW",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "23-14682-51160",
    "txn_date": "2026-05-30T02:44:37.110Z",
    "amount_usd": 318.77,
    "sold_price_usd": 360.0,
    "total_fee_usd": 41.23,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 36.65,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 4.14
    },
    "predicted_total_usd": 70.65477707006369,
    "predicted_by_type": {
      "FVF": 50.7515923566879,
      "INTERNATIONAL": 4.796178343949045,
      "AD": 7.993630573248407,
      "PAYONEER": 6.713375796178344,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Google Pixel Tablet Charging Speaker Dock GA03944-US Hazel G",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "27-14667-46652",
    "txn_date": "2026-05-28T00:45:14.606Z",
    "amount_usd": 157.72,
    "sold_price_usd": 148.0,
    "total_fee_usd": 20.28,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.01,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 17.83
    },
    "predicted_total_usd": 29.285350318471338,
    "predicted_by_type": {
      "FVF": 20.866242038216562,
      "INTERNATIONAL": 1.9745222929936306,
      "AD": 3.286624203821656,
      "PAYONEER": 2.7579617834394905,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Google Pixel Tablet Speaker Dock Charging Holder White Teste",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "14-14684-59764",
    "txn_date": "2026-05-27T09:09:24.377Z",
    "amount_usd": 318.02,
    "sold_price_usd": 375.98,
    "total_fee_usd": 61.96,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 3.97,
      "FINAL_VALUE_FEE": 57.55,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 73.76305732484076,
    "predicted_by_type": {
      "FVF": 53.0,
      "INTERNATIONAL": 5.006369426751593,
      "AD": 8.343949044585987,
      "PAYONEER": 7.012738853503185,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "SNOOPY IN FASHION & Collection Photo Book Set Softcover Good",
    "buyer_country": "ES",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "22-14670-93080",
    "txn_date": "2026-05-27T01:51:21.924Z",
    "amount_usd": 130.79,
    "sold_price_usd": 154.07,
    "total_fee_usd": 23.28,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 21.09,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 1.75
    },
    "predicted_total_usd": 30.457324840764333,
    "predicted_by_type": {
      "FVF": 21.719745222929937,
      "INTERNATIONAL": 2.050955414012739,
      "AD": 3.4203821656050954,
      "PAYONEER": 2.8662420382165603,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Baccarat 2025 Aria Tumblers Set of 2 Rock Glass NEW In Box f",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "27-14662-57695",
    "txn_date": "2026-05-26T22:10:50.419Z",
    "amount_usd": 704.66,
    "sold_price_usd": 829.68,
    "total_fee_usd": 125.02,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 115.91,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 8.67
    },
    "predicted_total_usd": 162.3171974522293,
    "predicted_by_type": {
      "FVF": 116.96178343949045,
      "INTERNATIONAL": 11.050955414012739,
      "AD": 18.420382165605094,
      "PAYONEER": 15.48407643312102,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "KEYENCE SR-G100 Handheld Code Reader New Unused in Original ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "12-14675-57099",
    "txn_date": "2026-05-24T15:12:40.682Z",
    "amount_usd": 314.97,
    "sold_price_usd": 377.8,
    "total_fee_usd": 62.83,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 4.34,
      "FINAL_VALUE_FEE": 58.05,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 74.12611464968153,
    "predicted_by_type": {
      "FVF": 53.261146496815286,
      "INTERNATIONAL": 5.031847133757962,
      "AD": 8.388535031847134,
      "PAYONEER": 7.044585987261146,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5001 Mini 5 Size 5-Ring Pueblo Leather Binder Genuin",
    "buyer_country": "AU",
    "qty": 2,
    "matched": true
  },
  {
    "order_id": "26-14652-30520",
    "txn_date": "2026-05-24T06:15:36.147Z",
    "amount_usd": 174.2,
    "sold_price_usd": 205.5,
    "total_fee_usd": 31.3,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.15,
      "FINAL_VALUE_FEE": 28.71,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 40.49554140127389,
    "predicted_by_type": {
      "FVF": 28.96815286624204,
      "INTERNATIONAL": 2.738853503184713,
      "AD": 4.560509554140127,
      "PAYONEER": 3.828025477707006,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5001 Narrow Size Pueblo Leather Binder Genuine Leath",
    "buyer_country": "GB",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "22-14657-33460",
    "txn_date": "2026-05-23T20:38:47.883Z",
    "amount_usd": 110.13,
    "sold_price_usd": 96.0,
    "total_fee_usd": 15.87,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 14.01,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 1.42
    },
    "predicted_total_usd": 19.132484076433123,
    "predicted_by_type": {
      "FVF": 13.535031847133759,
      "INTERNATIONAL": 1.2802547770700636,
      "AD": 2.1337579617834397,
      "PAYONEER": 1.78343949044586,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Pioneer Rayz Plus SE-LTC5R-T Lightning-Powered Noise Canceli",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "16-14661-37902",
    "txn_date": "2026-05-22T13:03:10.224Z",
    "amount_usd": 683.82,
    "sold_price_usd": 398.58,
    "total_fee_usd": 121.34,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 112.49,
      "INTERNATIONAL_FEE": 8.41
    },
    "predicted_total_usd": 78.17707006369426,
    "predicted_by_type": {
      "FVF": 56.18471337579618,
      "INTERNATIONAL": 5.312101910828026,
      "AD": 8.847133757961783,
      "PAYONEER": 7.43312101910828,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Genuine Leica Single Prism Set GPR121 + GZR103 + GDF121 with",
    "buyer_country": "CZ",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "11-14667-11501",
    "txn_date": "2026-05-21T22:24:58.965Z",
    "amount_usd": 320.68,
    "sold_price_usd": 358.0,
    "total_fee_usd": 37.32,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 3.74,
      "FINAL_VALUE_FEE": 33.14
    },
    "predicted_total_usd": 70.25987261146496,
    "predicted_by_type": {
      "FVF": 50.46496815286624,
      "INTERNATIONAL": 4.770700636942675,
      "AD": 7.949044585987261,
      "PAYONEER": 6.67515923566879,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Pioneer Lonesome Car-Boy CD-9 Vintage 9-Band Graphic Equaliz",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "24-14659-13424",
    "txn_date": "2026-05-21T11:05:27.675Z",
    "amount_usd": 81.81,
    "sold_price_usd": 98.0,
    "total_fee_usd": 16.19,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.1,
      "FINAL_VALUE_FEE": 14.65,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 19.521019108280257,
    "predicted_by_type": {
      "FVF": 13.815286624203821,
      "INTERNATIONAL": 1.305732484076433,
      "AD": 2.178343949044586,
      "PAYONEER": 1.821656050955414,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5015 Mini 5 Size 5-Ring Bridle Leather Binder Genuin",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "05-14496-41109",
    "txn_date": "2026-04-11T13:22:31.225Z",
    "amount_usd": 202.24,
    "sold_price_usd": 238.5,
    "total_fee_usd": 36.26,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.5,
      "FINAL_VALUE_FEE": 33.32,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 46.941401273885354,
    "predicted_by_type": {
      "FVF": 33.62420382165605,
      "INTERNATIONAL": 3.178343949044586,
      "AD": 5.292993630573249,
      "PAYONEER": 4.445859872611465,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5016 A5 6-Ring Horse Hair II Leather Binder Genuine ",
    "buyer_country": "GB",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "12-14485-60544",
    "txn_date": "2026-04-11T13:16:59.588Z",
    "amount_usd": 202.24,
    "sold_price_usd": 238.5,
    "total_fee_usd": 36.26,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.5,
      "FINAL_VALUE_FEE": 33.32,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 46.941401273885354,
    "predicted_by_type": {
      "FVF": 33.62420382165605,
      "INTERNATIONAL": 3.178343949044586,
      "AD": 5.292993630573249,
      "PAYONEER": 4.445859872611465,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5016 A5 6-Ring Horse Hair II Leather Binder Genuine ",
    "buyer_country": "GB",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "13-14480-48980",
    "txn_date": "2026-04-10T14:48:17.330Z",
    "amount_usd": 263.88,
    "sold_price_usd": 306.0,
    "total_fee_usd": 42.12,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 38.48,
      "INTERNATIONAL_FEE": 3.2,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 60.11337579617834,
    "predicted_by_type": {
      "FVF": 43.13375796178344,
      "INTERNATIONAL": 4.076433121019108,
      "AD": 6.796178343949045,
      "PAYONEER": 5.707006369426751,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Google Pixel Tablet Case Hazel GA04462-WW Hazel Genuine New ",
    "buyer_country": "PH",
    "qty": 2,
    "matched": true
  },
  {
    "order_id": "14-14472-69515",
    "txn_date": "2026-04-09T04:58:52.756Z",
    "amount_usd": 158.86,
    "sold_price_usd": 153.0,
    "total_fee_usd": 28.14,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 25.58,
      "INTERNATIONAL_FEE": 2.12
    },
    "predicted_total_usd": 30.247133757961784,
    "predicted_by_type": {
      "FVF": 21.56687898089172,
      "INTERNATIONAL": 2.038216560509554,
      "AD": 3.394904458598726,
      "PAYONEER": 2.8471337579617835,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Google Pixel Tablet Case Hazel GA04462-WW Hazel Genuine New ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "02-14490-55246",
    "txn_date": "2026-04-09T04:05:45.211Z",
    "amount_usd": 163.6,
    "sold_price_usd": 196.5,
    "total_fee_usd": 32.9,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 2.26,
      "FINAL_VALUE_FEE": 30.2
    },
    "predicted_total_usd": 38.74394904458599,
    "predicted_by_type": {
      "FVF": 27.70063694267516,
      "INTERNATIONAL": 2.6178343949044587,
      "AD": 4.3630573248407645,
      "PAYONEER": 3.662420382165605,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PFU HHKB Professional Classic Keyboard English Layout - Whit",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "01-14492-40485",
    "txn_date": "2026-04-09T03:14:17.164Z",
    "amount_usd": 813.74,
    "sold_price_usd": 798.0,
    "total_fee_usd": 144.26,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 132.79,
      "INTERNATIONAL_FEE": 11.03
    },
    "predicted_total_usd": 156.13248407643312,
    "predicted_by_type": {
      "FVF": 112.4968152866242,
      "INTERNATIONAL": 10.630573248407643,
      "AD": 17.713375796178344,
      "PAYONEER": 14.89171974522293,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Netsuken NV-25 Electric Sushi Rice Warmer 2.5-Sho 100V 50W S",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "13-14472-99996",
    "txn_date": "2026-04-08T20:58:53.093Z",
    "amount_usd": 185.73,
    "sold_price_usd": 198.54,
    "total_fee_usd": 32.81,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 29.88,
      "INTERNATIONAL_FEE": 2.49
    },
    "predicted_total_usd": 39.13885350318471,
    "predicted_by_type": {
      "FVF": 27.987261146496817,
      "INTERNATIONAL": 2.643312101910828,
      "AD": 4.407643312101911,
      "PAYONEER": 3.700636942675159,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Sony PlayStation SCPH-1050 Offcial RGB SCART Cable PS1 PS2 N",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "03-14486-94573",
    "txn_date": "2026-04-08T19:22:04.740Z",
    "amount_usd": 416.89,
    "sold_price_usd": 210.66,
    "total_fee_usd": 81.43,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 75.36,
      "INTERNATIONAL_FEE": 5.63
    },
    "predicted_total_usd": 41.50191082802548,
    "predicted_by_type": {
      "FVF": 29.694267515923567,
      "INTERNATIONAL": 2.8089171974522293,
      "AD": 4.67515923566879,
      "PAYONEER": 3.9235668789808917,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Oriental Motor BMUD200-A Brushless DC Motor Driver Single-Ph",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "11-14468-29448",
    "txn_date": "2026-04-07T08:27:48.979Z",
    "amount_usd": 88.16,
    "sold_price_usd": 99.7,
    "total_fee_usd": 11.54,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.12,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 9.98
    },
    "predicted_total_usd": 19.845859872611467,
    "predicted_by_type": {
      "FVF": 14.05732484076433,
      "INTERNATIONAL": 1.3248407643312101,
      "AD": 2.210191082802548,
      "PAYONEER": 1.8535031847133758,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Questyle QCC Dongle Pro Lossless Bluetooth Transmitter Mfi C",
    "buyer_country": "CH",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "26-14444-91809",
    "txn_date": "2026-04-06T22:45:29.231Z",
    "amount_usd": 202.24,
    "sold_price_usd": 238.5,
    "total_fee_usd": 36.26,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.5,
      "FINAL_VALUE_FEE": 33.32,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 46.941401273885354,
    "predicted_by_type": {
      "FVF": 33.62420382165605,
      "INTERNATIONAL": 3.178343949044586,
      "AD": 5.292993630573249,
      "PAYONEER": 4.445859872611465,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5016 A5 6-Ring Horse Hair II Leather Binder Genuine ",
    "buyer_country": "DE",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "13-14463-61857",
    "txn_date": "2026-04-06T20:12:21.067Z",
    "amount_usd": 71.27,
    "sold_price_usd": 86.0,
    "total_fee_usd": 14.73,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.1,
      "FINAL_VALUE_FEE": 13.19,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 17.17707006369427,
    "predicted_by_type": {
      "FVF": 12.121019108280255,
      "INTERNATIONAL": 1.1464968152866242,
      "AD": 1.910828025477707,
      "PAYONEER": 1.5987261146496816,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Heavy Unit Mega Drive Special MD Genesis Used Japan Import B",
    "buyer_country": "PT",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "13-14461-62755",
    "txn_date": "2026-04-06T13:29:50.562Z",
    "amount_usd": 154.45,
    "sold_price_usd": 179.3,
    "total_fee_usd": 24.85,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 22.54,
      "INTERNATIONAL_FEE": 1.87
    },
    "predicted_total_usd": 35.38089171974522,
    "predicted_by_type": {
      "FVF": 25.273885350318473,
      "INTERNATIONAL": 2.388535031847134,
      "AD": 3.9808917197452227,
      "PAYONEER": 3.337579617834395,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Sony ICD-TX660 Digital Voice Recorder 16GB Built-in Memory B",
    "buyer_country": "DE",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "13-14457-05126",
    "txn_date": "2026-04-05T14:04:53.067Z",
    "amount_usd": 159.85,
    "sold_price_usd": 153.0,
    "total_fee_usd": 27.15,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 24.66,
      "INTERNATIONAL_FEE": 2.05
    },
    "predicted_total_usd": 30.247133757961784,
    "predicted_by_type": {
      "FVF": 21.56687898089172,
      "INTERNATIONAL": 2.038216560509554,
      "AD": 3.394904458598726,
      "PAYONEER": 2.8471337579617835,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Google Pixel Tablet Case Porcelain GA04446-WW Genuine New fr",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "17-14446-67656",
    "txn_date": "2026-04-04T11:37:18.253Z",
    "amount_usd": 256.98,
    "sold_price_usd": 298.0,
    "total_fee_usd": 41.02,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 37.47,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 3.11
    },
    "predicted_total_usd": 58.546496815286616,
    "predicted_by_type": {
      "FVF": 42.00636942675159,
      "INTERNATIONAL": 3.968152866242038,
      "AD": 6.617834394904459,
      "PAYONEER": 5.554140127388535,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "HP 53131A 225MHz Universal Counter Frequency Counter Power O",
    "buyer_country": "TW",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "12-14453-55023",
    "txn_date": "2026-04-04T06:15:58.870Z",
    "amount_usd": 167.13,
    "sold_price_usd": 193.98,
    "total_fee_usd": 26.85,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 2.02,
      "FINAL_VALUE_FEE": 24.39
    },
    "predicted_total_usd": 38.247133757961784,
    "predicted_by_type": {
      "FVF": 27.343949044585987,
      "INTERNATIONAL": 2.5859872611464967,
      "AD": 4.305732484076433,
      "PAYONEER": 3.611464968152866,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "YUKI AIM Polar 65 KATANA Collection Keyboard AS-KB165WHUS-YU",
    "buyer_country": "HK",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "20-14433-33109",
    "txn_date": "2026-04-02T07:21:50.383Z",
    "amount_usd": 165.27,
    "sold_price_usd": 195.0,
    "total_fee_usd": 29.73,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.04,
      "FINAL_VALUE_FEE": 27.25,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 38.45095541401274,
    "predicted_by_type": {
      "FVF": 27.49044585987261,
      "INTERNATIONAL": 2.5987261146496814,
      "AD": 4.3312101910828025,
      "PAYONEER": 3.6305732484076434,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5016 Bible Size 6-Ring Horse Hair II Leather Binder ",
    "buyer_country": "GB",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "10-14444-04521",
    "txn_date": "2026-04-01T12:31:47.352Z",
    "amount_usd": 129.64,
    "sold_price_usd": 153.0,
    "total_fee_usd": 23.36,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 21.16,
      "INTERNATIONAL_FEE": 1.76
    },
    "predicted_total_usd": 30.247133757961784,
    "predicted_by_type": {
      "FVF": 21.56687898089172,
      "INTERNATIONAL": 2.038216560509554,
      "AD": 3.394904458598726,
      "PAYONEER": 2.8471337579617835,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Google Pixel Tablet Case Hazel GA04462-WW Hazel Genuine New ",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "16-14435-45066",
    "txn_date": "2026-04-01T11:59:23.166Z",
    "amount_usd": 208.61,
    "sold_price_usd": 197.98,
    "total_fee_usd": 29.37,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 26.26,
      "INTERNATIONAL_FEE": 2.67,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 39.03057324840764,
    "predicted_by_type": {
      "FVF": 27.910828025477706,
      "INTERNATIONAL": 2.6369426751592355,
      "AD": 4.3949044585987265,
      "PAYONEER": 3.6878980891719744,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Razer HyperFlux V2 Hard Edition Gaming Mouse Pad Wireless Ch",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "06-14448-47273",
    "txn_date": "2026-03-31T23:47:56.099Z",
    "amount_usd": 238.29,
    "sold_price_usd": 228.0,
    "total_fee_usd": 29.71,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.97,
      "FINAL_VALUE_FEE": 26.3,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 44.890445859872614,
    "predicted_by_type": {
      "FVF": 32.140127388535035,
      "INTERNATIONAL": 3.038216560509554,
      "AD": 5.063694267515924,
      "PAYONEER": 4.248407643312102,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "SHARP PC-G850VS Pocket Computer Built-in Speaker Box & Manua",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "14-14435-82656",
    "txn_date": "2026-03-31T19:51:25.956Z",
    "amount_usd": 623.46,
    "sold_price_usd": 608.25,
    "total_fee_usd": 104.79,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 96.34,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 8.01
    },
    "predicted_total_usd": 119.10063694267515,
    "predicted_by_type": {
      "FVF": 85.7452229299363,
      "INTERNATIONAL": 8.101910828025478,
      "AD": 13.503184713375797,
      "PAYONEER": 11.35031847133758,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Keyence CA-LMHE20 Telecentric Macro Lens 21MP High Resolutio",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "21-14422-66803",
    "txn_date": "2026-03-31T07:11:55.486Z",
    "amount_usd": 209.45,
    "sold_price_usd": 228.95,
    "total_fee_usd": 19.5,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 16.66,
      "INTERNATIONAL_FEE": 2.4,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 45.07515923566879,
    "predicted_by_type": {
      "FVF": 32.27388535031847,
      "INTERNATIONAL": 3.050955414012739,
      "AD": 5.082802547770701,
      "PAYONEER": 4.267515923566879,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Sony DPT-RP1 Digital Paper A4 13.3 inch Used Tested from Jap",
    "buyer_country": "DE",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "10-14433-83210",
    "txn_date": "2026-03-30T07:41:07.339Z",
    "amount_usd": 329.18,
    "sold_price_usd": 332.5,
    "total_fee_usd": 33.32,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 4.08,
      "FINAL_VALUE_FEE": 28.8
    },
    "predicted_total_usd": 65.27898089171974,
    "predicted_by_type": {
      "FVF": 46.87261146496815,
      "INTERNATIONAL": 4.426751592356688,
      "AD": 7.382165605095541,
      "PAYONEER": 6.197452229299363,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Strymon Brigadier Delay Effect Pedal Used Tested from Japan",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "17-14421-92853",
    "txn_date": "2026-03-29T21:17:55.820Z",
    "amount_usd": 101.71,
    "sold_price_usd": 115.2,
    "total_fee_usd": 13.49,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 11.73,
      "INTERNATIONAL_FEE": 1.32,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 22.877707006369427,
    "predicted_by_type": {
      "FVF": 16.24203821656051,
      "INTERNATIONAL": 1.535031847133758,
      "AD": 2.5605095541401273,
      "PAYONEER": 2.140127388535032,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player Black MXCP-P100BK ",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "13-14424-93393",
    "txn_date": "2026-03-29T04:00:08.409Z",
    "amount_usd": 292.88,
    "sold_price_usd": 350.0,
    "total_fee_usd": 57.12,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 3.66,
      "FINAL_VALUE_FEE": 53.02,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 68.69936305732483,
    "predicted_by_type": {
      "FVF": 49.33757961783439,
      "INTERNATIONAL": 4.662420382165605,
      "AD": 7.770700636942675,
      "PAYONEER": 6.528662420382165,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Bowl made of pottery 25 patterns of Nerikomi Eiji Murofushi ",
    "buyer_country": "HK",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "21-14409-70920",
    "txn_date": "2026-03-28T12:58:04.439Z",
    "amount_usd": 117.72,
    "sold_price_usd": 108.99,
    "total_fee_usd": 21.27,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.6,
      "FINAL_VALUE_FEE": 19.23,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 21.661146496815288,
    "predicted_by_type": {
      "FVF": 15.363057324840764,
      "INTERNATIONAL": 1.4522292993630572,
      "AD": 2.4203821656050954,
      "PAYONEER": 2.0254777070063694,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "DENSO 2m USB Cable For DST-010 (Green) / 95171-13780  Genuin",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "02-14436-42292",
    "txn_date": "2026-03-28T00:34:28.045Z",
    "amount_usd": 118.26,
    "sold_price_usd": 110.0,
    "total_fee_usd": 20.74,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.56,
      "FINAL_VALUE_FEE": 18.74,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 21.85859872611465,
    "predicted_by_type": {
      "FVF": 15.509554140127388,
      "INTERNATIONAL": 1.464968152866242,
      "AD": 2.4394904458598727,
      "PAYONEER": 2.0445859872611467,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "DENSO 5m USB Cable For DST-010 (Green) / 95171-14021 - Genui",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "22-14406-99668",
    "txn_date": "2026-03-27T23:52:30.864Z",
    "amount_usd": 945.63,
    "sold_price_usd": 889.0,
    "total_fee_usd": 161.37,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 12.35,
      "FINAL_VALUE_FEE": 148.58,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 173.8968152866242,
    "predicted_by_type": {
      "FVF": 125.32484076433121,
      "INTERNATIONAL": 11.840764331210192,
      "AD": 19.738853503184714,
      "PAYONEER": 16.59235668789809,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "FUJI ELECTRIC FSCS10A1-00J PORTAFLOW-C Ultrasonic Flow Meter",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "06-14430-30286",
    "txn_date": "2026-03-27T23:14:11.514Z",
    "amount_usd": 118.26,
    "sold_price_usd": 110.0,
    "total_fee_usd": 20.74,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.56,
      "FINAL_VALUE_FEE": 18.74,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 21.85859872611465,
    "predicted_by_type": {
      "FVF": 15.509554140127388,
      "INTERNATIONAL": 1.464968152866242,
      "AD": 2.4394904458598727,
      "PAYONEER": 2.0445859872611467,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "DENSO 5m USB Cable For DST-010 (Green) / 95171-14021 - Genui",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "06-14428-54529",
    "txn_date": "2026-03-27T15:48:48.115Z",
    "amount_usd": 120.83,
    "sold_price_usd": 115.2,
    "total_fee_usd": 14.37,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 12.52,
      "INTERNATIONAL_FEE": 1.41,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 22.877707006369427,
    "predicted_by_type": {
      "FVF": 16.24203821656051,
      "INTERNATIONAL": 1.535031847133758,
      "AD": 2.5605095541401273,
      "PAYONEER": 2.140127388535032,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player White MXCP-P100WH ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "07-14424-78451",
    "txn_date": "2026-03-27T01:12:50.412Z",
    "amount_usd": 119.81,
    "sold_price_usd": 115.2,
    "total_fee_usd": 15.39,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 13.43,
      "INTERNATIONAL_FEE": 1.52,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 22.877707006369427,
    "predicted_by_type": {
      "FVF": 16.24203821656051,
      "INTERNATIONAL": 1.535031847133758,
      "AD": 2.5605095541401273,
      "PAYONEER": 2.140127388535032,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player White MXCP-P100WH ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "25-14395-02514",
    "txn_date": "2026-03-26T07:29:29.703Z",
    "amount_usd": 292.26,
    "sold_price_usd": 319.5,
    "total_fee_usd": 57.24,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 3.95,
      "FINAL_VALUE_FEE": 52.85,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 62.74394904458598,
    "predicted_by_type": {
      "FVF": 45.038216560509554,
      "INTERNATIONAL": 4.254777070063694,
      "AD": 7.095541401273885,
      "PAYONEER": 5.955414012738854,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Zojirushi NW-JE10 Pressure Induction Heating Rice Cooker & W",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "01-14428-42212",
    "txn_date": "2026-03-25T19:31:23.649Z",
    "amount_usd": 131.49,
    "sold_price_usd": 128.0,
    "total_fee_usd": 16.51,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 14.44,
      "INTERNATIONAL_FEE": 1.63,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 25.374522292993632,
    "predicted_by_type": {
      "FVF": 18.044585987261147,
      "INTERNATIONAL": 1.7070063694267517,
      "AD": 2.840764331210191,
      "PAYONEER": 2.3821656050955413,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player Black MXCP-P100BK ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "22-14395-73560",
    "txn_date": "2026-03-25T13:26:31.192Z",
    "amount_usd": 128.23,
    "sold_price_usd": 152.95,
    "total_fee_usd": 28.72,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.97,
      "FINAL_VALUE_FEE": 26.31,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 30.240764331210194,
    "predicted_by_type": {
      "FVF": 21.56050955414013,
      "INTERNATIONAL": 2.038216560509554,
      "AD": 3.394904458598726,
      "PAYONEER": 2.8471337579617835,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "YOKOGAWA TY720 Digital Multimeter Tester Voltage Meter  Test",
    "buyer_country": "FR",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "07-14414-36278",
    "txn_date": "2026-03-24T18:48:25.733Z",
    "amount_usd": 193.97,
    "sold_price_usd": 220.05,
    "total_fee_usd": 31.08,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 28.29,
      "INTERNATIONAL_FEE": 2.35
    },
    "predicted_total_usd": 43.33630573248408,
    "predicted_by_type": {
      "FVF": 31.019108280254777,
      "INTERNATIONAL": 2.929936305732484,
      "AD": 4.885350318471337,
      "PAYONEER": 4.101910828025478,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "TRAVELER’S Notebook Limited Set MOOMIN Dad's Memories Moomin",
    "buyer_country": "NL",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "09-14409-71910",
    "txn_date": "2026-03-24T12:42:04.124Z",
    "amount_usd": 186.64,
    "sold_price_usd": 223.7,
    "total_fee_usd": 37.06,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.55,
      "FINAL_VALUE_FEE": 34.07,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 44.04968152866242,
    "predicted_by_type": {
      "FVF": 31.53503184713376,
      "INTERNATIONAL": 2.9808917197452227,
      "AD": 4.968152866242038,
      "PAYONEER": 4.165605095541402,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5001 Bible Size 6-Ring Pueblo Leather Binder Genuine",
    "buyer_country": "SG",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "08-14411-00189",
    "txn_date": "2026-03-24T12:31:03.906Z",
    "amount_usd": 113.06,
    "sold_price_usd": 128.0,
    "total_fee_usd": 14.94,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 13.03,
      "INTERNATIONAL_FEE": 1.47,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 25.374522292993632,
    "predicted_by_type": {
      "FVF": 18.044585987261147,
      "INTERNATIONAL": 1.7070063694267517,
      "AD": 2.840764331210191,
      "PAYONEER": 2.3821656050955413,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player Black MXCP-P100BK ",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "16-14399-09278",
    "txn_date": "2026-03-24T08:30:00.928Z",
    "amount_usd": 189.64,
    "sold_price_usd": 220.05,
    "total_fee_usd": 30.41,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 27.67,
      "INTERNATIONAL_FEE": 2.3
    },
    "predicted_total_usd": 43.33630573248408,
    "predicted_by_type": {
      "FVF": 31.019108280254777,
      "INTERNATIONAL": 2.929936305732484,
      "AD": 4.885350318471337,
      "PAYONEER": 4.101910828025478,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "TRAVELER’S Notebook Limited Set MOOMIN Dad's Memories Moomin",
    "buyer_country": "GB",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "02-14417-60139",
    "txn_date": "2026-03-23T20:26:43.388Z",
    "amount_usd": 82.76,
    "sold_price_usd": 93.84,
    "total_fee_usd": 11.08,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 9.56,
      "INTERNATIONAL_FEE": 1.08,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 18.705732484076435,
    "predicted_by_type": {
      "FVF": 13.229299363057326,
      "INTERNATIONAL": 1.2484076433121019,
      "AD": 2.082802547770701,
      "PAYONEER": 1.7452229299363058,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player White MXCP-P100WH ",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "26-14381-63993",
    "txn_date": "2026-03-23T17:37:59.578Z",
    "amount_usd": 1923.37,
    "sold_price_usd": 1890.0,
    "total_fee_usd": 366.63,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 340.7,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 25.49
    },
    "predicted_total_usd": 369.2407643312102,
    "predicted_by_type": {
      "FVF": 266.4331210191083,
      "INTERNATIONAL": 25.171974522292995,
      "AD": 41.955414012738856,
      "PAYONEER": 35.28025477707006,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "KEYENCE XG-X2700 Image Processing Controller High-Speed Sens",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "18-14392-11400",
    "txn_date": "2026-03-23T13:22:25.066Z",
    "amount_usd": 594.45,
    "sold_price_usd": 700.0,
    "total_fee_usd": 105.55,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 7.32,
      "FINAL_VALUE_FEE": 97.79,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 137.01146496815286,
    "predicted_by_type": {
      "FVF": 98.68152866242038,
      "INTERNATIONAL": 9.32484076433121,
      "AD": 15.54140127388535,
      "PAYONEER": 13.063694267515924,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Keithley 6485 Picoammeter pAmmeter 5-1/2 Digit Resolution Po",
    "buyer_country": "CN",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "12-14400-65179",
    "txn_date": "2026-03-23T12:26:00.241Z",
    "amount_usd": 137.39,
    "sold_price_usd": 164.0,
    "total_fee_usd": 26.61,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.83,
      "FINAL_VALUE_FEE": 24.34,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 32.4,
    "predicted_by_type": {
      "FVF": 23.121019108280255,
      "INTERNATIONAL": 2.1847133757961785,
      "AD": 3.643312101910828,
      "PAYONEER": 3.050955414012739,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "HOKUYO UTM-30LX-EW LIDAR Sensor 270° 30m Ethernet Tested Jap",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "27-14379-00097",
    "txn_date": "2026-03-23T12:22:41.868Z",
    "amount_usd": 237.51,
    "sold_price_usd": 280.0,
    "total_fee_usd": 42.49,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 39.12,
      "INTERNATIONAL_FEE": 2.93,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 55.03694267515923,
    "predicted_by_type": {
      "FVF": 39.47133757961783,
      "INTERNATIONAL": 3.732484076433121,
      "AD": 6.2165605095541405,
      "PAYONEER": 5.2165605095541405,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "KYOWA CDV-700A Signal Conditioner Dynamic Strain Amplifier T",
    "buyer_country": "KR",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "14-14396-99721",
    "txn_date": "2026-03-23T08:10:08.722Z",
    "amount_usd": 210.31,
    "sold_price_usd": 248.0,
    "total_fee_usd": 37.69,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 34.65,
      "INTERNATIONAL_FEE": 2.6,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 48.79490445859873,
    "predicted_by_type": {
      "FVF": 34.961783439490446,
      "INTERNATIONAL": 3.305732484076433,
      "AD": 5.503184713375796,
      "PAYONEER": 4.624203821656051,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "KYOWA DPM-712B Dynamic Strain Meter Tester Measuring Instrum",
    "buyer_country": "KR",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "16-14393-81786",
    "txn_date": "2026-03-23T05:26:57.507Z",
    "amount_usd": 363.73,
    "sold_price_usd": 398.0,
    "total_fee_usd": 42.27,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 4.25,
      "FINAL_VALUE_FEE": 37.58,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 78.0624203821656,
    "predicted_by_type": {
      "FVF": 56.10828025477707,
      "INTERNATIONAL": 5.2993630573248405,
      "AD": 8.834394904458598,
      "PAYONEER": 7.420382165605096,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Pioneer GEX-61 Lonesome Carboy Digital Tuner Vintage Car Aud",
    "buyer_country": "MT",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "20-14384-96773",
    "txn_date": "2026-03-22T16:51:33.904Z",
    "amount_usd": 100.68,
    "sold_price_usd": 93.84,
    "total_fee_usd": 13.16,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 11.43,
      "INTERNATIONAL_FEE": 1.29,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 18.705732484076435,
    "predicted_by_type": {
      "FVF": 13.229299363057326,
      "INTERNATIONAL": 1.2484076433121019,
      "AD": 2.082802547770701,
      "PAYONEER": 1.7452229299363058,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player Black MXCP-P100BK ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "04-14406-26477",
    "txn_date": "2026-03-22T02:31:25.079Z",
    "amount_usd": 82.76,
    "sold_price_usd": 93.84,
    "total_fee_usd": 11.08,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 9.56,
      "INTERNATIONAL_FEE": 1.08,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 18.705732484076435,
    "predicted_by_type": {
      "FVF": 13.229299363057326,
      "INTERNATIONAL": 1.2484076433121019,
      "AD": 2.082802547770701,
      "PAYONEER": 1.7452229299363058,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player Black MXCP-P100BK ",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "01-14406-65841",
    "txn_date": "2026-03-20T23:06:29.843Z",
    "amount_usd": 118.52,
    "sold_price_usd": 110.0,
    "total_fee_usd": 20.48,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.54,
      "FINAL_VALUE_FEE": 18.5,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 21.85859872611465,
    "predicted_by_type": {
      "FVF": 15.509554140127388,
      "INTERNATIONAL": 1.464968152866242,
      "AD": 2.4394904458598727,
      "PAYONEER": 2.0445859872611467,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "DENSO 5m USB Cable For DST-010 (Green) / 95171-14021 - Genui",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "23-14373-18210",
    "txn_date": "2026-03-20T18:16:12.855Z",
    "amount_usd": 100.63,
    "sold_price_usd": 93.84,
    "total_fee_usd": 13.21,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 11.47,
      "INTERNATIONAL_FEE": 1.3,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 18.705732484076435,
    "predicted_by_type": {
      "FVF": 13.229299363057326,
      "INTERNATIONAL": 1.2484076433121019,
      "AD": 2.082802547770701,
      "PAYONEER": 1.7452229299363058,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player Black MXCP-P100BK ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "04-14398-29288",
    "txn_date": "2026-03-20T03:48:11.831Z",
    "amount_usd": 665.46,
    "sold_price_usd": 690.0,
    "total_fee_usd": 132.54,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 122.9,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 9.2
    },
    "predicted_total_usd": 135.0496815286624,
    "predicted_by_type": {
      "FVF": 97.26751592356688,
      "INTERNATIONAL": 9.19108280254777,
      "AD": 15.318471337579618,
      "PAYONEER": 12.872611464968152,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Mitsubishi Uni color 240 50th anniversary Color Pencil 5000 ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "06-14394-66599",
    "txn_date": "2026-03-19T22:50:32.176Z",
    "amount_usd": 422.79,
    "sold_price_usd": 498.0,
    "total_fee_usd": 75.21,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 5.2,
      "FINAL_VALUE_FEE": 69.57,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 97.57834394904458,
    "predicted_by_type": {
      "FVF": 70.20382165605096,
      "INTERNATIONAL": 6.630573248407643,
      "AD": 11.05732484076433,
      "PAYONEER": 9.286624203821656,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "KEITHLEY 6485 Picoammeter For Parts or Repair As-Is from Jap",
    "buyer_country": "CN",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "07-14390-70956",
    "txn_date": "2026-03-19T12:24:31.555Z",
    "amount_usd": 175.13,
    "sold_price_usd": 198.0,
    "total_fee_usd": 22.87,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 20.36,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 2.07
    },
    "predicted_total_usd": 39.03057324840764,
    "predicted_by_type": {
      "FVF": 27.910828025477706,
      "INTERNATIONAL": 2.6369426751592355,
      "AD": 4.3949044585987265,
      "PAYONEER": 3.6878980891719744,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Google Pixel Tablet Charging Speaker Dock GA03944-US Hazel G",
    "buyer_country": "TW",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "09-14385-15541",
    "txn_date": "2026-03-18T20:38:40.954Z",
    "amount_usd": 81.13,
    "sold_price_usd": 93.84,
    "total_fee_usd": 12.71,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 11.03,
      "INTERNATIONAL_FEE": 1.24,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 18.705732484076435,
    "predicted_by_type": {
      "FVF": 13.229299363057326,
      "INTERNATIONAL": 1.2484076433121019,
      "AD": 2.082802547770701,
      "PAYONEER": 1.7452229299363058,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player White MXCP-P100WH ",
    "buyer_country": "HU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "20-14366-12833",
    "txn_date": "2026-03-18T03:07:59.259Z",
    "amount_usd": 364.53,
    "sold_price_usd": 398.0,
    "total_fee_usd": 63.47,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 4.84,
      "FINAL_VALUE_FEE": 58.19,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 78.0624203821656,
    "predicted_by_type": {
      "FVF": 56.10828025477707,
      "INTERNATIONAL": 5.2993630573248405,
      "AD": 8.834394904458598,
      "PAYONEER": 7.420382165605096,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "BEASTARS Season1 Vol.1 -4 First Limited Edition DVD Disc Com",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "27-14354-21370",
    "txn_date": "2026-03-17T18:06:49.585Z",
    "amount_usd": 211.75,
    "sold_price_usd": 198.0,
    "total_fee_usd": 27.25,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 24.09,
      "INTERNATIONAL_FEE": 2.72
    },
    "predicted_total_usd": 39.03057324840764,
    "predicted_by_type": {
      "FVF": 27.910828025477706,
      "INTERNATIONAL": 2.6369426751592355,
      "AD": 4.3949044585987265,
      "PAYONEER": 3.6878980891719744,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Beams Limited KM5 Instant Disk Audio CP1 CLEAR CD Bluetooth ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "15-14367-08728",
    "txn_date": "2026-03-16T19:50:38.044Z",
    "amount_usd": 575.02,
    "sold_price_usd": 649.0,
    "total_fee_usd": 73.98,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 6.79,
      "FINAL_VALUE_FEE": 66.75,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 127.04968152866242,
    "predicted_by_type": {
      "FVF": 91.49044585987261,
      "INTERNATIONAL": 8.643312101910828,
      "AD": 14.40764331210191,
      "PAYONEER": 12.10828025477707,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Pioneer Lonesome Car-Boy KP-707G  CD-5  GM-4 x2 Maintained T",
    "buyer_country": "MT",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "07-14373-96640",
    "txn_date": "2026-03-15T21:01:15.113Z",
    "amount_usd": 159.78,
    "sold_price_usd": 153.0,
    "total_fee_usd": 27.22,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 24.72,
      "INTERNATIONAL_FEE": 2.06
    },
    "predicted_total_usd": 30.247133757961784,
    "predicted_by_type": {
      "FVF": 21.56687898089172,
      "INTERNATIONAL": 2.038216560509554,
      "AD": 3.394904458598726,
      "PAYONEER": 2.8471337579617835,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Google Pixel Tablet Case Hazel GA04462-WW Hazel Genuine New ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "10-14367-85314",
    "txn_date": "2026-03-15T16:02:25.716Z",
    "amount_usd": 253.22,
    "sold_price_usd": 246.76,
    "total_fee_usd": 43.54,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 3.31,
      "FINAL_VALUE_FEE": 39.79,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 48.54649681528662,
    "predicted_by_type": {
      "FVF": 34.78343949044586,
      "INTERNATIONAL": 3.286624203821656,
      "AD": 5.477707006369426,
      "PAYONEER": 4.598726114649682,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Razer THX Onyx Portable DAC Headphone Amplifier NEW from Jap",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "27-14342-27608",
    "txn_date": "2026-03-15T06:12:30.662Z",
    "amount_usd": 106.19,
    "sold_price_usd": 99.84,
    "total_fee_usd": 13.65,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 11.87,
      "INTERNATIONAL_FEE": 1.34,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 19.877707006369427,
    "predicted_by_type": {
      "FVF": 14.07643312101911,
      "INTERNATIONAL": 1.3312101910828025,
      "AD": 2.21656050955414,
      "PAYONEER": 1.8535031847133758,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player White MXCP-P100WH ",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "24-14360-78132",
    "txn_date": "2026-03-15T02:35:29.529Z",
    "amount_usd": 524.53,
    "sold_price_usd": 498.0,
    "total_fee_usd": 72.87,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 6.68,
      "FINAL_VALUE_FEE": 65.75
    },
    "predicted_total_usd": 97.57834394904458,
    "predicted_by_type": {
      "FVF": 70.20382165605096,
      "INTERNATIONAL": 6.630573248407643,
      "AD": 11.05732484076433,
      "PAYONEER": 9.286624203821656,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "TANGO FW-20S Hi-Fi Output Transformer Set of 2 LCR Tested fr",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "24-14360-14986",
    "txn_date": "2026-03-14T23:01:01.324Z",
    "amount_usd": 290.08,
    "sold_price_usd": 348.0,
    "total_fee_usd": 57.92,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 53.48,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 4.0
    },
    "predicted_total_usd": 68.3108280254777,
    "predicted_by_type": {
      "FVF": 49.05732484076433,
      "INTERNATIONAL": 4.6369426751592355,
      "AD": 7.726114649681529,
      "PAYONEER": 6.490445859872612,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "HP 53131A 225MHz Universal Counter Frequency Counter Power O",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "09-14364-49993",
    "txn_date": "2026-03-14T11:52:03.111Z",
    "amount_usd": 354.79,
    "sold_price_usd": 349.0,
    "total_fee_usd": 63.21,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 58.4,
      "INTERNATIONAL_FEE": 4.37
    },
    "predicted_total_usd": 68.50191082802547,
    "predicted_by_type": {
      "FVF": 49.197452229299365,
      "INTERNATIONAL": 4.649681528662421,
      "AD": 7.745222929936306,
      "PAYONEER": 6.509554140127388,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "EchoTech ZO-41 II Hobby Ultrasonic Cutter NEW from Japan",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "07-14365-72268",
    "txn_date": "2026-03-13T21:40:51.232Z",
    "amount_usd": 844.41,
    "sold_price_usd": 197.98,
    "total_fee_usd": 107.51,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 96.21,
      "INTERNATIONAL_FEE": 10.86,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 39.03057324840764,
    "predicted_by_type": {
      "FVF": 27.910828025477706,
      "INTERNATIONAL": 2.6369426751592355,
      "AD": 4.3949044585987265,
      "PAYONEER": 3.6878980891719744,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Razer HyperFlux V2 Hard Edition Gaming Mouse Pad Wireless Ch",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "21-14343-26473",
    "txn_date": "2026-03-13T14:30:56.547Z",
    "amount_usd": 91.87,
    "sold_price_usd": 112.4,
    "total_fee_usd": 20.53,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.4,
      "FINAL_VALUE_FEE": 18.69,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 22.329936305732485,
    "predicted_by_type": {
      "FVF": 15.847133757961783,
      "INTERNATIONAL": 1.4968152866242037,
      "AD": 2.4968152866242037,
      "PAYONEER": 2.089171974522293,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5001 3-Ring Pueblo Leather Binder Genuine Leather JP",
    "buyer_country": "DE",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "02-14365-55452",
    "txn_date": "2026-03-12T06:21:04.431Z",
    "amount_usd": 154.45,
    "sold_price_usd": 179.3,
    "total_fee_usd": 24.85,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 22.54,
      "INTERNATIONAL_FEE": 1.87
    },
    "predicted_total_usd": 35.38089171974522,
    "predicted_by_type": {
      "FVF": 25.273885350318473,
      "INTERNATIONAL": 2.388535031847134,
      "AD": 3.9808917197452227,
      "PAYONEER": 3.337579617834395,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Sony ICD-TX660 Digital Voice Recorder 16GB Built-in Memory B",
    "buyer_country": "BE",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "11-14351-56604",
    "txn_date": "2026-03-12T01:12:32.548Z",
    "amount_usd": 319.08,
    "sold_price_usd": 368.0,
    "total_fee_usd": 78.92,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 4.59,
      "FINAL_VALUE_FEE": 73.89,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 72.21528662420381,
    "predicted_by_type": {
      "FVF": 51.87898089171974,
      "INTERNATIONAL": 4.904458598726115,
      "AD": 8.171974522292993,
      "PAYONEER": 6.859872611464968,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "The Art of the Iron Giant Hardcover Ramin Zahed Brand Used E",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "14-14346-45111",
    "txn_date": "2026-03-11T20:54:51.819Z",
    "amount_usd": 173.2,
    "sold_price_usd": 208.0,
    "total_fee_usd": 34.8,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.39,
      "FINAL_VALUE_FEE": 31.97,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 40.9859872611465,
    "predicted_by_type": {
      "FVF": 29.32484076433121,
      "INTERNATIONAL": 2.770700636942675,
      "AD": 4.617834394904459,
      "PAYONEER": 3.872611464968153,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "PLOTTER 5003 A5 6-Ring Shrink Leather Binder with Accessorie",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "24-14344-48734",
    "txn_date": "2026-03-11T11:45:41.489Z",
    "amount_usd": 189.64,
    "sold_price_usd": 220.05,
    "total_fee_usd": 30.41,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 27.67,
      "INTERNATIONAL_FEE": 2.3
    },
    "predicted_total_usd": 43.33630573248408,
    "predicted_by_type": {
      "FVF": 31.019108280254777,
      "INTERNATIONAL": 2.929936305732484,
      "AD": 4.885350318471337,
      "PAYONEER": 4.101910828025478,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "TRAVELER’S Notebook Limited Set MOOMIN Dad's Memories Moomin",
    "buyer_country": "AT",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "26-14326-44083",
    "txn_date": "2026-03-11T09:22:00.720Z",
    "amount_usd": 681.99,
    "sold_price_usd": 780.0,
    "total_fee_usd": 121.01,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 8.39,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 112.18
    },
    "predicted_total_usd": 152.61656050955415,
    "predicted_by_type": {
      "FVF": 109.95541401273886,
      "INTERNATIONAL": 10.388535031847134,
      "AD": 17.318471337579616,
      "PAYONEER": 14.554140127388536,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Tektronix TDS754D Digital Oscilloscope 500MHz 2GS/s 4CH Test",
    "buyer_country": "IN",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "21-14328-94252",
    "txn_date": "2026-03-10T10:39:27.384Z",
    "amount_usd": 172.76,
    "sold_price_usd": 199.8,
    "total_fee_usd": 31.04,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 28.47,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 2.13
    },
    "predicted_total_usd": 39.38089171974522,
    "predicted_by_type": {
      "FVF": 28.1656050955414,
      "INTERNATIONAL": 2.662420382165605,
      "AD": 4.43312101910828,
      "PAYONEER": 3.7197452229299364,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "HILTI SMD 57 Screw Magazine Attachment for SD 5000-22 SMD57 ",
    "buyer_country": "PL",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "16-14332-88717",
    "txn_date": "2026-03-09T16:25:06.015Z",
    "amount_usd": 144.77,
    "sold_price_usd": 139.98,
    "total_fee_usd": 25.21,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 22.87,
      "INTERNATIONAL_FEE": 1.9
    },
    "predicted_total_usd": 27.712101910828025,
    "predicted_by_type": {
      "FVF": 19.73248407643312,
      "INTERNATIONAL": 1.8662420382165605,
      "AD": 3.1082802547770703,
      "PAYONEER": 2.605095541401274,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "SONY ICD-ST25 Compact Voice Recorder Digital Handheld IC Rec",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "16-14326-88713",
    "txn_date": "2026-03-08T12:19:05.601Z",
    "amount_usd": 107.2,
    "sold_price_usd": 99.0,
    "total_fee_usd": 21.8,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 1.49,
      "FINAL_VALUE_FEE": 19.87,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 19.712101910828025,
    "predicted_by_type": {
      "FVF": 13.955414012738853,
      "INTERNATIONAL": 1.3184713375796178,
      "AD": 2.1974522292993632,
      "PAYONEER": 1.8407643312101911,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "APA Hotel Original Toothbrush Set (50 pcs) – Black & White O",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "06-14339-30616",
    "txn_date": "2026-03-07T21:18:12.161Z",
    "amount_usd": 161.44,
    "sold_price_usd": 151.99,
    "total_fee_usd": 20.55,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.04,
      "FINAL_VALUE_FEE": 18.07,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 30.056050955414015,
    "predicted_by_type": {
      "FVF": 21.426751592356688,
      "INTERNATIONAL": 2.0254777070063694,
      "AD": 3.375796178343949,
      "PAYONEER": 2.828025477707006,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "CRYPTON VOCALOID4 Hatsune Miku V4X  incl. ENGLISH PACKAGE So",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "24-14327-45832",
    "txn_date": "2026-03-07T18:54:52.870Z",
    "amount_usd": 184.63,
    "sold_price_usd": 179.3,
    "total_fee_usd": 31.67,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 28.83,
      "INTERNATIONAL_FEE": 2.4
    },
    "predicted_total_usd": 35.38089171974522,
    "predicted_by_type": {
      "FVF": 25.273885350318473,
      "INTERNATIONAL": 2.388535031847134,
      "AD": 3.9808917197452227,
      "PAYONEER": 3.337579617834395,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Sony ICD-TX660 Digital Voice Recorder 16GB Built-in Memory B",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "23-14313-71476",
    "txn_date": "2026-03-07T17:04:47.269Z",
    "amount_usd": 111.74,
    "sold_price_usd": 128.0,
    "total_fee_usd": 20.26,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 18.44,
      "INTERNATIONAL_FEE": 1.38
    },
    "predicted_total_usd": 25.374522292993632,
    "predicted_by_type": {
      "FVF": 18.044585987261147,
      "INTERNATIONAL": 1.7070063694267517,
      "AD": 2.840764331210191,
      "PAYONEER": 2.3821656050955413,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "HIOKI 9694 Clamp on Sensor for 3169/3196/8800 Used 2 unit Se",
    "buyer_country": "HU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "15-14323-86528",
    "txn_date": "2026-03-07T10:38:28.967Z",
    "amount_usd": 88.1,
    "sold_price_usd": 99.84,
    "total_fee_usd": 11.74,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 10.16,
      "INTERNATIONAL_FEE": 1.14,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 19.877707006369427,
    "predicted_by_type": {
      "FVF": 14.07643312101911,
      "INTERNATIONAL": 1.3312101910828025,
      "AD": 2.21656050955414,
      "PAYONEER": 1.8535031847133758,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "maxell MXCP-P100 Portable Cassette Player Black MXCP-P100BK ",
    "buyer_country": "AU",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "13-14326-70896",
    "txn_date": "2026-03-07T07:12:47.689Z",
    "amount_usd": 178.68,
    "sold_price_usd": 166.24,
    "total_fee_usd": 21.01,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 18.48,
      "INTERNATIONAL_FEE": 2.09,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 32.83312101910828,
    "predicted_by_type": {
      "FVF": 23.43312101910828,
      "INTERNATIONAL": 2.21656050955414,
      "AD": 3.6878980891719744,
      "PAYONEER": 3.0955414012738856,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "CRYPTON VOCALOID4 Megurine LUKA V4X Music Software Win Mac E",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "20-14314-74880",
    "txn_date": "2026-03-06T20:56:03.427Z",
    "amount_usd": 211.0,
    "sold_price_usd": 197.98,
    "total_fee_usd": 26.98,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 23.85,
      "INTERNATIONAL_FEE": 2.69,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 39.03057324840764,
    "predicted_by_type": {
      "FVF": 27.910828025477706,
      "INTERNATIONAL": 2.6369426751592355,
      "AD": 4.3949044585987265,
      "PAYONEER": 3.6878980891719744,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Razer HyperFlux V2 Hard Edition Gaming Mouse Pad Wireless Ch",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "22-14309-36274",
    "txn_date": "2026-03-06T08:35:08.879Z",
    "amount_usd": 846.01,
    "sold_price_usd": 980.0,
    "total_fee_usd": 149.99,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 10.41,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 139.14
    },
    "predicted_total_usd": 191.64840764331208,
    "predicted_by_type": {
      "FVF": 138.15286624203821,
      "INTERNATIONAL": 13.050955414012739,
      "AD": 21.75796178343949,
      "PAYONEER": 18.286624203821656,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Tektronix TDS3054C Digital Phosphor Oscilloscope 500MHz 5GS/",
    "buyer_country": "IT",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "13-14321-28130",
    "txn_date": "2026-03-06T00:06:55.953Z",
    "amount_usd": 298.36,
    "sold_price_usd": 296.0,
    "total_fee_usd": 57.64,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 3.98,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 53.22
    },
    "predicted_total_usd": 58.15796178343948,
    "predicted_by_type": {
      "FVF": 41.72611464968153,
      "INTERNATIONAL": 3.9426751592356686,
      "AD": 6.573248407643312,
      "PAYONEER": 5.515923566878981,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Holbain Artist Colored Pencils Set of 150 Colors New Package",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "11-14320-83590",
    "txn_date": "2026-03-05T10:52:30.555Z",
    "amount_usd": 586.96,
    "sold_price_usd": 680.0,
    "total_fee_usd": 93.04,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 7.11,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 85.49
    },
    "predicted_total_usd": 133.10063694267515,
    "predicted_by_type": {
      "FVF": 95.85987261146497,
      "INTERNATIONAL": 9.05732484076433,
      "AD": 15.095541401273886,
      "PAYONEER": 12.687898089171975,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "YOKOGAWA DL1740E Digital Oscilloscope 500MHz 1GS/s Tested In",
    "buyer_country": "SG",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "19-14308-72771",
    "txn_date": "2026-03-05T04:35:27.610Z",
    "amount_usd": 298.36,
    "sold_price_usd": 296.0,
    "total_fee_usd": 57.64,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 3.98,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 53.22
    },
    "predicted_total_usd": 58.15796178343948,
    "predicted_by_type": {
      "FVF": 41.72611464968153,
      "INTERNATIONAL": 3.9426751592356686,
      "AD": 6.573248407643312,
      "PAYONEER": 5.515923566878981,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Holbain Artist Colored Pencils Set of 150 Colors New Package",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "22-14298-88923",
    "txn_date": "2026-03-04T00:52:08.467Z",
    "amount_usd": 373.6,
    "sold_price_usd": 360.0,
    "total_fee_usd": 59.4,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 4.52,
      "FINAL_VALUE_FEE": 54.44,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 70.65477707006369,
    "predicted_by_type": {
      "FVF": 50.7515923566879,
      "INTERNATIONAL": 4.796178343949045,
      "AD": 7.993630573248407,
      "PAYONEER": 6.713375796178344,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "HIOKI DT4282 Digital Multimeter 10A Slightly New from Japan",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "14-14305-36804",
    "txn_date": "2026-03-02T22:56:53.858Z",
    "amount_usd": 712.47,
    "sold_price_usd": 798.0,
    "total_fee_usd": 91.53,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 8.4,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 82.69
    },
    "predicted_total_usd": 156.13248407643312,
    "predicted_by_type": {
      "FVF": 112.4968152866242,
      "INTERNATIONAL": 10.630573248407643,
      "AD": 17.713375796178344,
      "PAYONEER": 14.89171974522293,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "FOSTEX R100T Attenuator Transformer Pair 100W Tested Excelle",
    "buyer_country": "IT",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "05-14318-52587",
    "txn_date": "2026-03-02T22:16:32.246Z",
    "amount_usd": 755.53,
    "sold_price_usd": 798.0,
    "total_fee_usd": 142.47,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 132.14,
      "INTERNATIONAL_FEE": 9.89
    },
    "predicted_total_usd": 156.13248407643312,
    "predicted_by_type": {
      "FVF": 112.4968152866242,
      "INTERNATIONAL": 10.630573248407643,
      "AD": 17.713375796178344,
      "PAYONEER": 14.89171974522293,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Netsuken NV-25 Electric Sushi Rice Warmer 2.5-Sho 100V 50W S",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "25-14287-07361",
    "txn_date": "2026-03-02T14:35:59.624Z",
    "amount_usd": 243.55,
    "sold_price_usd": 241.05,
    "total_fee_usd": 47.5,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 43.78,
      "INTERNATIONAL_FEE": 3.28,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 47.43184713375796,
    "predicted_by_type": {
      "FVF": 33.98089171974522,
      "INTERNATIONAL": 3.210191082802548,
      "AD": 5.350318471337579,
      "PAYONEER": 4.490445859872612,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Mitutoyo Surface Roughness Standard Specimen 178-604 Brand N",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "05-14310-69901",
    "txn_date": "2026-03-01T13:18:36.666Z",
    "amount_usd": 364.14,
    "sold_price_usd": 429.0,
    "total_fee_usd": 64.86,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 4.49,
      "FINAL_VALUE_FEE": 59.93,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 84.11337579617835,
    "predicted_by_type": {
      "FVF": 60.47770700636943,
      "INTERNATIONAL": 5.713375796178344,
      "AD": 9.522292993630574,
      "PAYONEER": 8.0,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "For sun65218 AGILENT E1709A REMOTE HIGH PERFORMANCE RECEIVER",
    "buyer_country": "KR",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "01-14312-71522",
    "txn_date": "2026-02-28T15:18:07.862Z",
    "amount_usd": 760.17,
    "sold_price_usd": 895.0,
    "total_fee_usd": 134.83,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 9.35,
      "FINAL_VALUE_FEE": 125.04,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 175.056050955414,
    "predicted_by_type": {
      "FVF": 126.1656050955414,
      "INTERNATIONAL": 11.92356687898089,
      "AD": 19.866242038216562,
      "PAYONEER": 16.70063694267516,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Keithley 6485 Picoammeter pAmmeter 5-1/2 Digit Resolution Po",
    "buyer_country": "CN",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "09-14299-27257",
    "txn_date": "2026-02-28T05:30:08.719Z",
    "amount_usd": 298.36,
    "sold_price_usd": 296.0,
    "total_fee_usd": 57.64,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 3.98,
      "FINAL_VALUE_FEE": 53.22
    },
    "predicted_total_usd": 58.15796178343948,
    "predicted_by_type": {
      "FVF": 41.72611464968153,
      "INTERNATIONAL": 3.9426751592356686,
      "AD": 6.573248407643312,
      "PAYONEER": 5.515923566878981,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Holbain Artist Colored Pencils Set of 150 Colors New Package",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "02-14308-23836",
    "txn_date": "2026-02-27T21:18:20.969Z",
    "amount_usd": 1138.89,
    "sold_price_usd": 1112.0,
    "total_fee_usd": 193.11,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 177.89,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 14.78
    },
    "predicted_total_usd": 217.4063694267516,
    "predicted_by_type": {
      "FVF": 156.7579617834395,
      "INTERNATIONAL": 14.80891719745223,
      "AD": 24.687898089171973,
      "PAYONEER": 20.751592356687897,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Mitutoyo SJ-210 Surface Roughness Tester Surftest 178-601/17",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "24-14285-09225",
    "txn_date": "2026-02-26T16:10:39.574Z",
    "amount_usd": 911.01,
    "sold_price_usd": 860.0,
    "total_fee_usd": 128.99,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 116.78,
      "INTERNATIONAL_FEE": 11.77
    },
    "predicted_total_usd": 168.228025477707,
    "predicted_by_type": {
      "FVF": 121.23566878980891,
      "INTERNATIONAL": 11.452229299363058,
      "AD": 19.089171974522294,
      "PAYONEER": 16.05095541401274,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "DENSO DST-i Scan Tool Diagnostic Tester Main Unit DN-VIM-003",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "15-14283-12107",
    "txn_date": "2026-02-26T16:01:17.071Z",
    "amount_usd": 190.56,
    "sold_price_usd": 228.0,
    "total_fee_usd": 37.44,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.57,
      "FINAL_VALUE_FEE": 34.43,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 44.890445859872614,
    "predicted_by_type": {
      "FVF": 32.140127388535035,
      "INTERNATIONAL": 3.038216560509554,
      "AD": 5.063694267515924,
      "PAYONEER": 4.248407643312102,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Kikkoman LuciPac A3 Water ATP Test Swabs 100 pcs for Lumites",
    "buyer_country": "CH",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "25-14268-01735",
    "txn_date": "2026-02-26T11:30:05.654Z",
    "amount_usd": 516.12,
    "sold_price_usd": 598.0,
    "total_fee_usd": 81.88,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 75.19,
      "INTERNATIONAL_FEE": 6.25
    },
    "predicted_total_usd": 117.10063694267515,
    "predicted_by_type": {
      "FVF": 84.29936305732484,
      "INTERNATIONAL": 7.968152866242038,
      "AD": 13.273885350318471,
      "PAYONEER": 11.159235668789808,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "TRAVELER'S notebook Limited Set TRAVELER'S DINER NEW from Ja",
    "buyer_country": "GB",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "01-14302-48874",
    "txn_date": "2026-02-26T08:53:01.196Z",
    "amount_usd": 318.3,
    "sold_price_usd": 369.0,
    "total_fee_usd": 50.7,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 3.86,
      "FINAL_VALUE_FEE": 46.4,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 72.40636942675158,
    "predicted_by_type": {
      "FVF": 52.01910828025478,
      "INTERNATIONAL": 4.917197452229299,
      "AD": 8.19108280254777,
      "PAYONEER": 6.8789808917197455,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "HIOKI DT4282 Digital Multimeter 10A Slightly New from Japan",
    "buyer_country": "AT",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "27-14259-77236",
    "txn_date": "2026-02-25T05:01:15.951Z",
    "amount_usd": 604.22,
    "sold_price_usd": 699.98,
    "total_fee_usd": 95.76,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 88.01,
      "INTERNATIONAL_FEE": 7.31,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 137.00509554140126,
    "predicted_by_type": {
      "FVF": 98.67515923566879,
      "INTERNATIONAL": 9.32484076433121,
      "AD": 15.54140127388535,
      "PAYONEER": 13.063694267515924,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Keysight E3633A DC Power Supply 0-8V/20A 0-20V/10A Tested Ve",
    "buyer_country": "MY",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "15-14275-96319",
    "txn_date": "2026-02-25T00:55:51.199Z",
    "amount_usd": 1045.25,
    "sold_price_usd": 210.66,
    "total_fee_usd": 200.55,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 186.18,
      "INTERNATIONAL_FEE": 13.93
    },
    "predicted_total_usd": 41.50191082802548,
    "predicted_by_type": {
      "FVF": 29.694267515923567,
      "INTERNATIONAL": 2.8089171974522293,
      "AD": 4.67515923566879,
      "PAYONEER": 3.9235668789808917,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Oriental Motor BMUD200-A Brushless DC Motor Driver Single-Ph",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "25-14261-17551",
    "txn_date": "2026-02-24T21:51:43.227Z",
    "amount_usd": 211.74,
    "sold_price_usd": 197.98,
    "total_fee_usd": 26.24,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 23.18,
      "INTERNATIONAL_FEE": 2.62,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 39.03057324840764,
    "predicted_by_type": {
      "FVF": 27.910828025477706,
      "INTERNATIONAL": 2.6369426751592355,
      "AD": 4.3949044585987265,
      "PAYONEER": 3.6878980891719744,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Razer HyperFlux V2 Hard Edition Gaming Mouse Pad Wireless Ch",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "15-14274-93700",
    "txn_date": "2026-02-24T20:13:47.370Z",
    "amount_usd": 199.76,
    "sold_price_usd": 193.98,
    "total_fee_usd": 34.22,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "INTERNATIONAL_FEE": 2.6,
      "FINAL_VALUE_FEE": 31.18
    },
    "predicted_total_usd": 38.247133757961784,
    "predicted_by_type": {
      "FVF": 27.343949044585987,
      "INTERNATIONAL": 2.5859872611464967,
      "AD": 4.305732484076433,
      "PAYONEER": 3.611464968152866,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "YUKI AIM Polar 65 KATANA Collection Keyboard AS-KB165WHUS-YU",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "10-14279-21366",
    "txn_date": "2026-02-24T04:59:55.152Z",
    "amount_usd": 314.0,
    "sold_price_usd": 370.0,
    "total_fee_usd": 56.0,
    "fees_by_type": {
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44,
      "FINAL_VALUE_FEE": 51.69,
      "INTERNATIONAL_FEE": 3.87
    },
    "predicted_total_usd": 72.60382165605095,
    "predicted_by_type": {
      "FVF": 52.15923566878981,
      "INTERNATIONAL": 4.929936305732484,
      "AD": 8.21656050955414,
      "PAYONEER": 6.898089171974522,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "LEYBOLD NT10 TURBOTRONIK Turbo Molecular Pump Controller Tes",
    "buyer_country": "CN",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "20-14260-48359",
    "txn_date": "2026-02-23T08:35:32.132Z",
    "amount_usd": 196.83,
    "sold_price_usd": 231.8,
    "total_fee_usd": 38.97,
    "fees_by_type": {
      "INTERNATIONAL_FEE": 2.96,
      "FINAL_VALUE_FEE": 35.57,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 45.62929936305733,
    "predicted_by_type": {
      "FVF": 32.67515923566879,
      "INTERNATIONAL": 3.089171974522293,
      "AD": 5.146496815286624,
      "PAYONEER": 4.318471337579618,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "Ohuhu illustration Marker Pen 320 Colors Brush & Fine Type w",
    "buyer_country": "FR",
    "qty": 1,
    "matched": true
  },
  {
    "order_id": "23-14253-71474",
    "txn_date": "2026-02-22T19:26:16.035Z",
    "amount_usd": 383.34,
    "sold_price_usd": 360.0,
    "total_fee_usd": 66.66,
    "fees_by_type": {
      "FINAL_VALUE_FEE": 61.14,
      "INTERNATIONAL_FEE": 5.08,
      "FINAL_VALUE_FEE_FIXED_PER_ORDER": 0.44
    },
    "predicted_total_usd": 70.65477707006369,
    "predicted_by_type": {
      "FVF": 50.7515923566879,
      "INTERNATIONAL": 4.796178343949045,
      "AD": 7.993630573248407,
      "PAYONEER": 6.713375796178344,
      "TXN_FEE": 0.39999999999999997
    },
    "title": "GLSSWRKS Hana Premium Glass Mouse Pad Smooth Precision Gamin",
    "buyer_country": "US",
    "qty": 1,
    "matched": true
  }
]
```
