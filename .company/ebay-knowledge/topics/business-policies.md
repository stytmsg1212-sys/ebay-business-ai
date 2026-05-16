---
topic: eBay Business Policies 一覧
source: GetUserPreferences API (ShowSellerProfilePreferences=true)
fetched_at: 2026-04-20
---

# eBay Business Policies

## Payment (1件)
| ID | 名前 |
|---|---|
| `359244671023` | Managed Payments |

→ **デフォルト確定**: 選択肢は1つのみ

## Return (3件)
| ID | 名前 | 推定用途 |
|---|---|---|
| `359243687023` | Return | **メイン（推定デフォルト）** |
| `369424758023` | Return Copy | 複製の予備 |
| `369226012023` | No Return Accepted | 返品不可（ジャンク向け） |

→ 要ユーザー確認: 新規出品のデフォルトはどれか？

## Shipping (44件 — 関税時代区分 × 重量で複雑)

### ① DDP 系（post-tariff時代、2025-11以降の想定）
**1day 出荷（在庫あり前提）**
| ID | 重量帯 | 料金 |
|---|---|---|
| `377279091023` | 0-0.5kg | $030 |
| `377279110023` | 0.5-1kg | $030 |
| `377279123023` | 1-2kg | $030 |
| `377279473023` | 2-3kg | $030 |
| `377279247023` | 3-4kg | $030 |
| `377279249023` | 4-5kg | $040 |
| `377279256023` | 5-6kg | $030 |
| `377279259023` | 6-8kg | $030 |
| `377279458023` | 8-10kg | $030 |
| `377279187023` | 10-20kg | $030 |

**7day 出荷（在庫なし・取り寄せ）**
| ID | 重量帯 | 料金 |
|---|---|---|
| `376828925023` | 0-0.5kg | $030 |
| `376856042023` | 0.5-1kg | $030 |
| `376856693023` | 1-2kg | $030 |
| `376857245023` | 2-3kg | $030 |
| `376930061023` | 3-4kg | $030 |
| `376931439023` | 4-5kg | $040 |
| `376952406023` | 5-6kg | $030 |
| `376953191023` | 6-8kg | $030 |
| `376954367023` | 8-10kg | $030 |
| `376958908023` | 10-20kg | $030 |

### ② pre-tariff 在庫あり (STOCK 1day)
| ID | 重量帯 | 名前 |
|---|---|---|
| `365199203023` | 0~0.5kg | 001 STOCK(1day) EXPEDITED 0~0.5kg |
| `365308147023` | 0.5~1.0kg | 002 STOCK(1day) EXPEDITED 0.5 ~ 1.0kg |
| `369226067023` | 0.5~1.0kg | 002 STOCK(1.0day) EXPEDITED 0.5 ~ 1.0kg Copy |
| `365308412023` | 1.0~1.5kg | 003 STOCK(1day) EXPEDITED 1.0 ~ 1.5kg |
| `365329085023` | 1.5~2.0kg | 004 STOCK(1day) EXPEDITED 1.5 ~ 2.0kg |
| `365330911023` | 2.0~2.5kg | 005 STOCK(1day) EXPEDITED 2.0 ~ 2.5kg |
| `365331219023` | 2.5~3.0kg | 006 STOCK(1day) EXPEDITED 2.5 ~ 3.0kg |
| `365332263023` | 3.0~4.0kg | 007 STOCK(1day) EXPEDITED 3.0 ~ 4.0kg |

### ③ pre-tariff 在庫なし (NO STOCK 7day)
| ID | 重量帯 | 名前 |
|---|---|---|
| `365332393023` | 0~0.5kg | 101 NO STOCK(7day) EXPEDITED 0~0.5kg |
| `365332406023` | 0.5~1.0kg | 102 NO STOCK(7day) EXPEDITED 0.5 ~ 1.0kg |
| `365332415023` | 1.0~1.5kg | 103 NO STOCK(7day) EXPEDITED 1.0 ~ 1.5kg |
| `366196461023` | 1.0~1.5kg | 103 Copy |
| `368260267023` | 1.0~1.5kg | 103 Copy (2) |
| `372319113023` | 1.0~1.5kg | 103 Copy (3) |
| `372319508023` | 1.0~1.5kg | 103 Copy (5) |
| `365332429023` | 1.5~2.0kg | 104 NO STOCK(7day) EXPEDITED 1.5 ~ 2.0kg |
| `365332431023` | 2.0~2.5kg | 105 NO STOCK(7day) EXPEDITED 2.0 ~ 2.5kg |
| `365332443023` | 2.5~3.0kg | 106 NO STOCK(7day) EXPEDITED 2.5 ~ 3.0kg |
| `365332454023` | 3.0~4.0kg | 107 NO STOCK(7day) EXPEDITED 3.0 ~ 4.0kg |

### ④ 特殊
| ID | 名前 | 用途 |
|---|---|---|
| `361448076023` | IN STOCK EXPEDITED-FedEx-USAonly-Free(2day)+(1～5day) | 在庫あり FedEx無料 (US only) |
| `361448229023` | NO STOCK EXPEDITED-FedEx-USAonly-Free(7day)+(1～5day) | 在庫なし FedEx無料 (US only) |
| `369424757023` | Flat: US_ExpeditedSppedPAK free, 1 business | フラットレート無料 |
| `378277682023` | Flat: US_ExpeditedSppedPAK $50.00, 7 business | フラットレート $50 |
| `379661692023` | Pre-order 2-3kg | 予約注文 |

## W9 実装上の観点

### 推奨運用パターン (post-tariff時代=2025-11以降)
- **デフォルト適用**: DDP系（重量帯・在庫有無で自動選択）
- 在庫あり = `DDP_Xkg_$030_1day` 系
- 在庫なし/取り寄せ = `DDP_Xkg_$030_7day` 系

### W9 UI 提案
重量を入力 → ShippingPolicy を自動推定:
```
重量 < 0.5kg  → DDP_0-0.5kg (1day or 7day)
0.5-1kg       → DDP_0.5-1kg
1-2kg         → DDP_1-2kg
...
```

※ Stock/NoStock の切替は UI 上でチェックボックス（「在庫あり：1day出荷」「在庫なし：7day出荷」）

## Out-of-Stock Control

`OutOfStockControlPreference` 状態履歴（contradiction-annotation: 値の時系列変化）:
- **2026-04-20**: 未設定（None）= **OFF**（GetUserPreferences API）
- **2026-05-16**: user が **ON 化（Seller Hub 手動確認）** → W133 前提クリア。API での再確認は未実施（Phase 0 で GetUserPreferences 取得を足す時に自動 verify 予定）

### 挙動（一次情報照合 2026-05-16, eBay Developers Program / 複数 SaaS KB 一致）

- **OFF（現状）**: GTC 固定価格 listing の数量が 0 になると eBay が listing を **自動 End**（item_id / ウォッチャー / 販売履歴を喪失、再出品が必要）。
- **ON**: 数量0でも listing は **active のまま保持**、検索結果からは除外され、再入荷で数量を戻すと同一 item_id で復帰。
- 適用対象: **GTC（Good 'Til Cancelled）固定価格 listing のみ**。ON にすると以後**全 GTC listing に一括適用**。

⚠️ 旧想定「OFF でも End されない」は**誤り**（2026-05-16 W133 設計時に判明、`md-files-can-be-wrong`）。OFF=End が正。

### ON 化手順（2 ルート、ラベルは英語 UI 基準）

1. **Seller Hub 経由**: Seller Hub → Overview → Shortcuts → **Site Preferences** → 「Multi-quantity listings」セクション → **「Listings stay active when you're out of stock」を ON**
2. **My eBay 経由**: My eBay → Account → Site Preferences → **Selling Preferences** → out-of-stock オプションを編集して有効化

### W133 依存

W133（有在庫管理）の「在庫0で数量0化・listing 保持」は **本設定 ON が前提**。ON 未確認の間は数量0自動 revise を行わず Discord アラートのみ（誤 End による資産喪失防止セーフガード）。

出典: https://developer.ebay.com/api-docs/user-guides/static/trading-user-guide/out-of-stock.html / https://developer.ebay.com/api-docs/user-guides/static/trading-user-guide/out-of-stock-enable.html

## Copy/重複ポリシーの整理
"Copy", "Copy (2)", "Copy (3)", "Copy (5)" が複数存在。テスト用と思われる。本番利用前に削除 or アーカイブ推奨。
