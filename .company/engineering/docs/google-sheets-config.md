---
type: config
---

# Google Sheets 古物台帳 設定

## サービスアカウント
- ファイル: `C:/Users/gucch/.config/google/service-account.json`
- メール: `claude-sheets@rising-parser-491621-i9.iam.gserviceaccount.com`
- プロジェクトID: `rising-parser-491621-i9`

## スプレッドシートID一覧

### 2025年（参照のみ）
| 台帳 | スプレッドシートID |
|------|------------------|
| ヤフオク | `1mSzOxy6VtPTVDO1aRM4oyfkqxstJGQwyEROdbmvwR2E` |
| メルカリ | `116Kp6JqFyE7HDg7MjcvxdwGHNKzLHbnTawkPptbedzA` |
| ラクマ | `1PG-lMIKM0d28Mid19i9nJXUS4DyHav-QUXF7MibyhxM` |
| その他 | `1H-9eKOtsfAR0V-8Hqdulud5TUHqAn-AJN01PZ9QqXhg` |

### 2026年（書き込み先・現在アクティブ）
| 台帳 | スプレッドシートID |
|------|------------------|
| ヤフオク | `18zZyQPSKWqfi60-q7uDlvtOM-Jn_mov3v1JLCBMGIhA` |
| メルカリ | `1rWXlGYojbJm6kMepgjJDsrVA68TNLjltaaG6r5yu2As` |
| ラクマ | `1iqKBr1uYVupF5iTjs-1gpM_AcuGpeTquRjIbWEf_8lA` |
| その他 | `1w3nug7ucZPG9iFDItst81YBOO2--8UN6UnD8qG7PXnw` |

## シート構成

### ヤフオク・メルカリ共通
- Paypayカード
- 楽天カード
- セゾンカード
- 銀行振込

### ラクマ
- Paypayカード

### その他
- 銀行

## カラム構成（共通）
| 列 | 項目 |
|----|------|
| B | 管理番号 |
| C | 取引日 |
| D | 品名（古物） |
| E | 数量 |
| F | 仕入金額（税込） |
| G | 仕入先氏名・名称 |
| H | 商品ID |
| I | 仕入先住所 |
| J | 本人確認方法 |
| K | 備考 |

## 管理番号フォーマット
- ヤフオク Paypay: `Y-P-YYYY-NN`
- ヤフオク 楽天: `Y-R-YYYY-NN`
- ヤフオク セゾン: `Y-S-YYYY-NN`
- ヤフオク 銀行: `Y-B-YYYY-NN`
- メルカリ Paypay: `M-P-YYYY-NN`（推定）
- ラクマ Paypay: `R-P-YYYY-NN`（推定）
