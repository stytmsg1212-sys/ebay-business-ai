# 2026-04-19 学び・ノウハウ

## 発見した既存コードの問題（要対応）

### 1. `daily_scheduler.py` L436-437: busy-wait ループ

```python
while True:
    pass
```

CPU 13.8秒/壁時計8秒 → 1コア常時100%の無駄。正しくは：
```python
import time
while True:
    time.sleep(60)
```
PID 8864 に対する対応は**次回再起動時**にコード修正してから起動でOK（緊急ではないが電力の無駄）。

### 2. `config/schedule_config.json`: execution_times の設定ミス

`execution_schedule.times = [2, 11, 15, 18, 22]` に対して、個別タスクで：
- `email_pickup.execution_times = [11, 17, 22]` → **17 は発火しない**（15 ではない）
- `research.execution_times = [11, 17, 22]` → **同上**

新スケジュール（2026-04-19改定）に追従していない。修正すべき：
- `email_pickup`: `[11, 15, 18, 22]` が正しい（5:00=秘書ルーティンで実行済みなので除外）
- `research`: `[11, 15, 18, 22]` が正しい

### 3. `config/schedule_config.json`: eBay認証情報が平文コミット

`ebay.user_token` `ebay.cert_id` 等が平文記載。`.env` / 環境変数に移動すべき。
（セキュリティルール違反：global rules の `security.md` 参照）

## FedEx IP 料金表の構造変化（2026-04-05 発効）

FedEx FICP は全重量帯に一律 **+4.9%** の通常値上げ。
一方 FedEx IP (SID 14) は重量帯で非均一：
- 500g: **-7.4%**（値下げ）
- 1000g: +14.4%
- 1500g: +21.0%
- 2000-2500g: +34〜35%
- 3000g以上: +4.9% 均一

→ 2500g以下の商品をIPで出品していた場合、利益率に大きな影響あり。
→ 「FedEx値上げは一律」と思い込むと危険。IPの2500g以下は **要価格見直し**。

## 運送料PDF解析ロジックの堅牢性

`shipping_rate_manager.py` は初投入で本番PDF3テーブル（FedEx FICP, FedEx IP, DHL）を
**一発で全件正しく抽出**できた。特に以下が効いた：
- 重量の合理性チェック（0.1〜200kg）
- レート値の合理性チェック（100〜999999円）
- サービス名に「—」または「輸送料金」のみで `_detect_service_name` が分岐

次PDF更新時も動く可能性高い（PDF構造が変わらない限り）。
