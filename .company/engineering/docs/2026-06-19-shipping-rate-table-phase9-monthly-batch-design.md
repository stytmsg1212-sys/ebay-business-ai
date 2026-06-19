---
title: eBay SpeedPAK DDP Rate Table — Phase 9 月次自動更新バッチ 設計書
date: 2026-06-19
status: implemented (v2 / Codex VERDICT B 反映済 / code-reviewer HIGH=0 / pytest tests 2726 PASS + 25 unit / dry-run diff=0 実証)
owner: assistant (model claude-opus-4-8) / user 承認制
related_memory: project-shipping-rate-table-rebuild
related_design: 2026-06-19-shipping-rate-table-rebuild-phase3-design.md
---

# Phase 9 月次自動更新バッチ 設計書

## 1.5 Codex 協議反映 (v2, 2026-06-19 / VERDICT B → 拘束的修正)
Codex(gpt-5.5)レビューで VERDICT B(条件付き)。下記 6 点を **拘束条件** として本設計に反映。
実装はこの v2 に従う(§3-§12 の該当箇所も本節に整合させて更新済)。

- **F1 燃料は専用キー新設**: calculator 用 `fuel_surcharge_fedex/dhl`(CPaSS、現値 49.5/47.75)を **流用しない**。
  rate table 専用 `rate_table_fuel_fedex_pct` / `rate_table_fuel_dhl_pct` を settings に新設し、
  `source` / `effective_week` / `last_verified_at` / `verified_by` を併記。
  ⚠️ **現 live rate table は 41.50/45.25(6/19 web FICP)で焼かれており、calculator の CPaSS 値(49.5/47.75)とは別物**(実機確認済)。
  どちらが SpeedPAK 便の正しい燃料かは **初月 dry-run の diff を user が確認して確定**(本バッチが勝手に決めない)。
  未設定 / 30日超 stale / source 不明 なら **auto 不可(dry-run 強制)**。
- **F2 FX 取得失敗時は auto 停止**: 「前回 FX 据え置きで適用」は古い為替で全 table を動かす money-direct リスク。
  auto では FX 取得失敗・観測営業日不足・前月末未反映なら **更新せず dry-run 通知のみ**。前回値適用は user 明示フラグ + fallback 監査ログがある時だけ。
- **F3 全 table preflight 制 (部分適用の根絶)**: 実行を
  `preflight_all → diff_all → guard_all → snapshot_all → apply_all → verify_all` に分離。
  preflight/guard が **1 件でも失敗したら 10 table 全て未更新**。mutation 後の失敗は `PARTIAL_APPLIED` で即 alert、
  再実行は run_id/snapshot ベースの **idempotent recovery のみ**(通常 path で部分状態を上書きしない)。
- **F4 rollback の堅牢化**: apply 前 snapshot に `run_id, table_id, rate_id, old_usd, intended_usd` を保存。
  rollback も同じ API/OAuth に依存し必ず戻せる保証はない → rollback 失敗時は **手動復旧手順 + Discord escalation + listing 影響範囲** を必須化。
  readback は eBay eventual consistency 考慮で **短時間 retry 後に判定**。
- **F5 manifest 照合は ISO 正規化 set の bijection**: 表示名(`"Korea, South"`)照合は表記揺れ/順序/国名変更に弱い。
  canonical に **ISO country code(or eBay region ID)正規化セット**を持たせ、現 row の正規化国セットと manifest row を
  **一意 bijection match** → そこで得た **current rateId を動的採用**(manifest の rateId を盲信しない)。
  全 table で「9 row 完全一致・重複 signature なし・余剰 row なし」を満たすまで apply 禁止。
- **F6 変動ガードはハイブリッド閾値**: 固定 ±$30/±200% は不可。
  `$0 行`(0→正 / 正→0 を別扱いで必ず alert)/ 軽帯(絶対 $3-5)/ 通常帯(max($10, 30%))/ 重帯 Z8/Z9(max($20-30, 15-20%))。
  加えて「同一 carrier 入力変更から説明できない外れ値」を検出。

**横断原則 (Codex)**: 「**ログできないなら更新しない / 通知できないなら auto しない**」。
監査ログ書込失敗・Discord 送信失敗時は auto を中止(dry-run 扱い)。

**auto 昇格の合格基準 (確定)**: ①10 table 全て getRateTable 成功 ②各 9 row 完全一致 ③計 90 rate の計算再現
④ガード発火 0 ⑤Discord/DB 監査ログ成功 ⑥dry-run diff を user 承認 ⑦燃料/FX/PDF が fresh ⑧手動 rollback 手順確認済み。
**1 項目でも未充足なら auto 不可**。初月 dry-run の合格 = 「通知が届いた」ではなく **実 eBay 現行値との diff + Phase 3 再計算の一致**。

## 1. 目的
Phase 6-8 で全 10 DDP rate table を **DHLゾーン 9 行/テーブル** 構造に再構築し eBay 反映済み。
本 Phase 9 は、その **金額のみ** を毎月自動で実費に追従させる月次バッチを実装する。
**行構造(ゾーン 9 行・国セット)は不変** = `updateShippingCost` API(金額のみ)で完結し、UI 手作業ゼロ運用。

## 2. 確定パラメータ (Clarify 2026-06-19 + 既存制約)
| 項目 | 確定値 | 出典 |
|---|---|---|
| 基本料金PDF 入手 | **user が手動配置** (`C:\work\claude\eBaySpeedPAK\`)。滅多に変わらない。バッチは「あれば再パース、無ければ前回の基本料金キャッシュを据え置き+アラート」 | Clarify |
| 実行タイミング | **毎月1日 早朝**(前月為替確定後)。03:00 JST 想定(inventory 02:30 と非衝突) | Clarify |
| 適用モード | **初月は dry-run**(計算→Discord diff通知→user 確認)→ 検証後に **完全自動適用**へ昇格 | Clarify |
| 為替 | rate table 専用 = **前月平均 × (1−1%) ≈ 157円/$**。calculator 用 `settings.json::exchange_rate` とは別管理(触らない) | Phase 3 設計 / K2 |
| 燃料 | **`settings.json::fuel_surcharge_fedex/dhl`(CPaSS値・user 手動維持)を読む**。自動スクレイプ禁止(retail≠CPaSS、2026-05-31 money-safety 確定) | task_fuel_surcharge_check.py |
| 計算式 | `surcharge = max(0, ceil[(DHL実費×(1+DHL燃料) − FedEx_US実費×(1+FedEx燃料)) / FX])` | Phase 3 設計 §3 |
| 対象 | DDP 10 table / 22 policy(1day+7day 共有 + Pre-order/Flat$50 相乗り 2) | Phase 3 設計 §2 |

## 3. 入力と取得方法 (3 入力、各々 fail-closed)

### 3.1 基本料金 (DHL/FedEx SpeedPAK PDF)
- **源**: `C:\work\claude\eBaySpeedPAK\` の 2 PDF。user が手動更新(年 1-2 回)。
- **取得**: `parse_rates.py` / `phase3_calc.py` の PDF パースロジックを `scripts/` 昇格して再利用。
  - DHL p12 の 3 層([Envelope][書類≤2kg][荷物=非書類]) の **荷物(非書類)** 層を使う(書類料金誤用バグ既知)。
  - FedEx 列E (USW)。
- **fail-closed**: PDF が無い/パース失敗/**アンカー検証不一致**(FedEx 0.5kg E=2082 等の既知値)なら **前回パース結果(キャッシュ JSON)を据え置き + アラート**。基本料金は据え置きでも、燃料/為替の変動だけは反映される。
- **キャッシュ**: 最後に成功したパース結果を `data/shipping_rate_batch/base_rates_cache.json` に保存(PDF mtime + アンカー pass を併記)。

### 3.2 燃料率 (CPaSS、自動取得しない)
- **源**: `settings.json::fuel_surcharge_fedex` / `fuel_surcharge_dhl`(% 値、user が MonoDeck 全体設定で手動維持、CPaSS login PDF 由来)。
- **取得**: settings.json を読むだけ。**スクレイプ禁止**(2026-05-31: 小売値≠CPaSS値の誤上書き money-direct リスク)。
- **fail-closed (stale ガード)**: `fuel_surcharge_last_updated` が **30 日超**なら、古い燃料で rate table を作ると過小/過大になるため **適用せず警告**(dry-run 相当で停止、user に「燃料を先に更新せよ」)。
- ⚠️ **Codex 協議ポイント #1**: Phase 3 で使った FedEx FICP=41.50% / DHL=45.25%(6/19 web 取得)が、settings.json の CPaSS 値と一致するか要確認。**rate table 用燃料 = SpeedPAK 適用便の燃料** であるべき。もし settings.json の CPaSS 値が SpeedPAK 便と別物なら、`rate_table_fuel_fedex/dhl` 専用キーを settings に新設して user 維持に回す。dry-run の通知に「使用した燃料率」を明示し user が初月に検証する。

### 3.3 為替 (前月平均、外部 FX API)
- **源**: 外部 FX API(**httpx・非ブラウザ**、例 frankfurter.app: `GET /YYYY-MM-01..YYYY-MM-末?from=USD&to=JPY`)。
- **計算**: 前月の全営業日 USD→JPY の **単純平均 × (1−1%)** → 小数切り上げ整数(円/$)。
- **fail-closed**: API 失敗 / 平均が **[120, 200] 円/$ の常識帯外** なら **前回 FX 据え置き + アラート**(適用は継続するが FX は前回値)。
- **calculator は触らない**: `settings.json::exchange_rate`(spot ~160、利益計算用)とは別。rate table FX は `data/shipping_rate_batch/` 内に保持。
- **監査**: 取得した日次レート列 + 平均 + 適用値を audit ログに残す。

## 4. 計算 (phase3_calc.py の汎化)
- `phase3_calc.py` のコア式を関数化:
  ```python
  def compute_surcharge_usd(dhl_base_jpy, fedex_us_base_jpy,
                            dhl_fuel_pct, fedex_fuel_pct, jpy_per_usd) -> int:
      dhl = dhl_base_jpy * (1 + dhl_fuel_pct / 100)
      fed = fedex_us_base_jpy * (1 + fedex_fuel_pct / 100)
      return max(0, math.ceil((dhl - fed) / jpy_per_usd))
  ```
- 入力: §3 の 3 入力 + canonical manifest の (band, zone)。
- 出力: `{table_id: {rate_id: new_usd}}`(全 10 table × 9 zone)。
- band 代表重量 = 帯上限(過小=赤字回避)。DHL 非書類/帯上限/ゾーン、FedEx 列E/帯上限。

## 5. 適用 (canonical manifest → updateShippingCost)

### 5.1 canonical manifest
- `data/shipping_rate_batch/manifest/` に `phase6_manifest_<band>.json` を昇格(現 `data/tmp/`)。
- 形式: `{table_id, band, policies[], rows:[{rate_id, zone, usd, countries[]}]}`(実物確認済)。
- これが **唯一の正典**: zone↔rate_id↔countries の対応。

### 5.2 更新フロー (1 table ずつ)
1. `getRateTable(table_id)` で現構造を読む。
2. **manifest 整合検証 (fail-closed)**:
   - 行数 = manifest の行数(9 or $0行省略時の実数)と一致。
   - 各 rate_id の国セットが manifest と一致(human UI 編集で rateId 再採番された場合に検出)。
   - 不一致 → **その table は適用せず alert + skip 記録**(Q0: silent skip 禁止、task_execution_log + Discord)。
3. 計算済み new_usd と現値を diff。
4. **per-rate 変動ガード**: 1 rate の変動が **±$X (例 $30) 超** or 現値の **±N% (例 200%) 超** なら異常値候補 → dry-run では通知のみ、auto では **当該 table を hold + alert**(計算バグ/入力異常の捕捉)。
5. **mode 分岐**:
   - **dry-run**: updateShippingCost は呼ばない。diff を蓄積。
   - **auto**: 変更行のみ `POST /sell/account/v2/rate_table/{id}/update_shipping_cost` `{"rates":[{rateId, shippingCost:{value,currency:"USD"}}]}`(rateId 昇順)。204 確認。
6. **auto 時 読戻し検証**: 直後に `getRateTable` 再取得、全 rate_id の金額が new_usd と一致を確認。不一致 → alert + 当該 table を rollback(元値再 POST)。
7. **shipToLocations 不変の保証**: 本バッチは updateShippingCost(金額)のみ。配送可能国(shipToLocations)は **一切触らない**。これは API 仕様上自明だが、念のため適用前後で `getFulfillmentPolicy` の shipToLocations diff=0 を 1 policy サンプルで確認(Phase 6 安全ルール踏襲)。

### 5.3 blast radius 明示
- 各 table は 1day+7day policy が共有(10 table=20 policy)+ 5284247010(Pre-order)/5284249010(Flat$50)相乗り。
- updateShippingCost は table 単位 = 共有 policy 全てに即波及。通知に影響 policy 数を明記。

## 6. mode 管理と dry-run→auto 昇格
- `config/schedule_config.json` に `rate_table_batch: {enabled, mode: "dry_run"|"auto", last_run, ...}`。
- 初回は `mode="dry_run"`。user が初月の diff 通知を確認 → 問題なければ `mode="auto"` に手動切替(or MonoDeck トグル)。
- **kill switch**: `tasks_enabled.rate_table_monthly_update.enabled=false` で停止。

## 7. fail-closed サニティガード一覧 (Q0)
| ガード | 条件 | 動作 |
|---|---|---|
| PDF アンカー | 既知値不一致 | 基本料金 据え置き + alert |
| 燃料 stale | last_updated > 30日 | 適用せず警告(燃料更新要求) |
| 燃料 範囲 | fedex/dhl が [10,70]% 外 | 適用せず alert |
| FX 範囲 | 平均 が [120,200] 外 | FX 据え置き + alert |
| manifest 整合 | 行数/国セット不一致 | 当該 table skip + alert |
| per-rate 変動 | ±$30 or ±200% 超 | dry-run=通知 / auto=hold + alert |
| 読戻し不一致 | getRateTable ≠ new_usd | rollback + alert |
- 全ガード発火は **task_execution_log + Discord('pricing')** に記録(silent skip 絶対禁止)。

## 8. 監査ログ (DB / money-direct トレーサビリティ)
- 新テーブル `shipping_rate_batch_log`(migration、冪等):
  `run_at, mode, fx_used, fedex_fuel, dhl_fuel, table_id, rate_id, old_usd, new_usd, action(applied/held/skipped/dryrun), note`。
- 毎月の old→new を全件残す = 後から「なぜこの月この送料に上がったか」を再現可能。

## 9. ファイル構成 (data/tmp → scripts 昇格)
- `scripts/shipping_rate_batch/` 新設:
  - `parse_base_rates.py`(PDF パース、parse_rates.py 昇格 + アンカー検証 + キャッシュ)
  - `compute_surcharge.py`(差額式 関数、phase3_calc 昇格)
  - `fetch_fx.py`(前月平均 FX、httpx)
  - `ebay_rate_table_api.py`(getRateTable / updateShippingCost ラッパー、OAuth は `monitor.ebay_oauth_refresh.get_valid_access_token`)
  - `run_monthly_batch.py`(オーケストレータ: 取得→計算→manifest検証→diff→(auto)適用→読戻し→監査ログ→Discord)
  - `manifest/phase6_manifest_<band>.json`(canonical 昇格)
- `data/tmp/` の `phase6_apply_band.py`(Playwright UI 駆動)は **昇格しない**(構造変更は UI 限定・月次では不要)。`dhl_country_zone.json` の junk "PO" はクリーン化。

## 10. スケジュール登録 (silent-skip-prevention 必須 4 点)
1. `task_key='rate_table_monthly_update'`。
2. `monitor/task_execution_log.TASK_SCHEDULE` に登録(月次 = `weekdays`/`hours` モデルに乗らないため、独立 CronTrigger + task_execution_log への手動 start/finish 記録で代替)。
3. `daily_scheduler.setup_scheduler` に `CronTrigger(day=1, hour=3, minute=0)` で add_job、`args=[config]`、`id='rate_table_monthly_update'`、`max_instances=1`、`replace_existing=True`。
4. 月次は既存 daily batch dispatcher に乗らない独立 job のため、**job 内で log_task_start/finish を明示呼び出し**(skip/fail も必ず記録)。

## 11. Codex 協議ポイント (open questions)
1. **燃料率の源**(§3.2): settings.json CPaSS 値 = SpeedPAK 便燃料か? 別なら専用キー新設。
2. **FX API の選定**: frankfurter.app(ECB)で USD/JPY 前月平均が取れるか、営業日のみか暦日か、休日補間。代替 API。
3. **per-rate 変動ガード閾値**($30 / 200%)の妥当性。重帯($318 等)では絶対額閾値が緩すぎないか。
4. **月次タイミング**: 前月 FX 確定は月初で十分か(ECB は前営業日公示)。1日が休日でも cron は動くか。
5. **manifest 整合の rateId 再採番耐性**: getRateTable が国セットで照合できる粒度か(API レスポンス構造の再確認)。
6. **auto 昇格の判定**: 初月 dry-run の「合格」基準を何で測るか(全 table 読戻し diff=設計値、per-rate ガード未発火 等)。

## 12. ロールアウト
1. 本設計を Codex 協議(§11)→ 反映。
2. `scripts/shipping_rate_batch/` 実装 + 単体テスト(計算式・manifest検証・fail-closed 分岐・FXパース)。
3. code-reviewer HIGH=0 + Codex 2 段。
4. **dry-run を手動実行(Q1 検証)**: 実 getRateTable と照合、Discord に diff 通知が出ることを実機確認。
5. 月次 cron 登録 + kill switch。`mode="dry_run"` で初回 7/1 を待つ(or 手動 trigger で前倒し検証)。
6. 初月 diff を user が確認 → `mode="auto"` 昇格。
7. 24h retrospective(初回 auto 適用後)。
