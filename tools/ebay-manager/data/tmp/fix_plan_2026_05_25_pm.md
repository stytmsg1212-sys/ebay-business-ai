# 5/25 PM 7 件即時修正 — Codex review 用 fix plan

## 背景

5/25 19:00 health check (W164-followup `af1a2c0` で Discord 通知 silent skip 修正後の初本物 fire) で以下 7 件の本番問題を検出. user 指示「全て今すぐ対応、Codex review 経由」.

## 7 件の root cause + 修正方針

### Problem #1: daily_codex_lint FileNotFoundError (今日 03:00)

- **根本原因**: 5/25 03:00 発火時、scheduler は旧 PID 67036 (`c746daf` mkdir fix 未適用) で起動していた。`c746daf` は朝の commit、scheduler restart は 17:16 まで実施されず
- **現状**: 新 PID 89344 (21:12 起動) は mkdir fix 適用済。次の 5/26 03:00 で成功するはず
- **修正案**: コード変更不要。今日の failed エントリは履歴として残す (削除すると Q2 違反 + 履歴歪曲)

### Problem #2: fuel_surcharge_check HTML 抽出失敗 (Monday 週次)

- **根本原因**: FedEx は bot 対策で値が HTML に埋め込まれていない (コード内コメント既述)、DHL は HTML 構造変化で regex マッチ失敗
- **DB**: 03:40 / 08:39 共に `HTMLから燃料サーチャージ値を抽出できませんでした (bot対策の可能性)`
- **修正案**:
  - **A.** 失敗時に raw HTML を `data/tmp/fuel_surcharge_failure_YYYY-MM-DD-{fedex|dhl}.html` に dump 保存し、次回の手動デバッグを容易化
  - **B.** Discord 通知に「現在値維持 + 30 日超で alert」既存ロジック (今朝の `7e5692a` で追加) で運用継続
  - **修正規模**: ~15 LOC (raw HTML 保存ロジックのみ)、再現性確保が目的
- **K1 Simplicity**: bot 対策回避は K1 過剰、現実用途は「値が取れた時だけ更新、取れない時は警告 + 手動 fallback」で十分

### Problem #3: research_morning_brief — `error_max_budget_usd`

- **根本原因**: `monitor/research_brain.py:140,164` で `max_budget_usd=0.50` がデフォルト、CLI に `--max-budget-usd 0.50` 渡している。今朝の 03:39 / 08:37 で claude が $0.5099675 で予算超過
- **修正案**: `task_research_morning_brief.py` が `ask_research_brain(...)` を呼ぶ箇所で `max_budget_usd=2.0` (Opus 4.7 の morning brief 1 回分の想定上限) を明示渡し
- **修正規模**: ~3 LOC
- **検証**: 直接呼出で $2 まで許容するか確認

### Problem #4: rival_detection — errors=1 (詳細不明)

- **根本原因**: 3 listings 中 1 件で内部 error 発生、success=false + errors=1 で集計表示するが **個別エラー詳細が message に乗っていない**
- **修正案**: `task_rival_detection.py` の error path で具体的 error message (listing id + 例外内容) を logger.warning + message JSON に含める
- **修正規模**: ~10-15 LOC
- **K3 verify**: 次回失敗時に DB message を見れば原因即特定

### Problem #5: orphan started 残骸 3 件 cleanup

- **対象**: 5/02 22:07 / 5/21 11:08 / 5/22 18:09 の inventory_check (started のまま finished_at NULL)
- **修正案**: Q2 6 step DB 直接書込:
  1. SELECT で 3 件 dump (rollback 用)
  2. UPDATE 1 件 WHERE id=対象 (`status='failed', finished_at=started_at + 12h, success=0, message='auto-cleanup W164-pm: scheduler crash/kill artifact'`) 試行
  3. 残り 2 件
  4. SELECT で再確認
  5. 24h retrospective code-reviewer (本 session 内なら同時)
- **修正規模**: ~10 LOC one-shot script
- **回避策**: scheduler 自身に「起動時に自分が知らない PID の started 残骸を failed 化」ロジック追加するか? → K2 Surgical 超え、別 W

### Problem #6: W139 coverable=1 false positive (timing 系)

- **検出**: 19:00 で `ebayyh_v1206136150` が coverable と判定、現在 DB は monitored_items に id=243 で登録済 (last_check=18:28:39)
- **find_coverage_gaps 仕様**: `NOT EXISTS (m.ebay_item_id=l.ebay_item_id AND COALESCE(m.is_active,1)=1)` で判定
- **仮説**: 18:28:39 時点で last_check は更新されたが、ebay_item_id か is_active カラムが NULL のままだった可能性。または 18:08:25 の ensure_monitor_coverage で row 作成 → 18:28:39 で last_check 更新の流れ、19:00 時点では row はあるが ebay_item_id が NULL?
- **検証要**: monitored_items row の created_at と ebay_item_id 充填タイミングを実 DB で確認
- **修正案**: 確認結果次第。**現時点で false positive と断定不能**。投機修正は K0 違反 = Codex 助言要

### Problem #7: rival_pricing_refresh 18:45 misfire

- **根本原因**: scheduler.log で `Run time of job "W183 ライバル価格 refresh (18:45)..." was missed by 0:00:01.503857` 確認
- **設計**: `daily_scheduler.py:756-760` で `job_defaults={'misfire_grace_time': 600}` 設定済 (10 分 grace) のはず
- **疑問**: 600s grace あれば 1.5s 遅延は run されるはず。job_defaults の override 不在を再確認も、L902-911 の add_job は misfire_grace_time 個別指定なし = defaults 継承のはず。なぜ skip?
- **仮説**: APScheduler v3 の挙動誤解、または coalesce=True + 1.5s delay の組合せで「missed and coalesce skipped」になる別 path
- **修正案**: rival_pricing_refresh の add_job に `misfire_grace_time=300` を **明示指定** (defaults 上書きで保険、原因不明でも実害ゼロ)。本 session で時間切れなら別 W で根本調査
- **修正規模**: ~3 LOC

## 修正優先度 + 推定 LOC

| # | Problem | 修正 LOC | 検証 | 緊急度 |
|---|---------|---------|-----|--------|
| 1 | daily_codex_lint | 0 | 5/26 03:00 観察 | Low (既に解決) |
| 2 | fuel_surcharge | ~15 | 5/25 raw HTML dump 確認 + 6/1 月曜 fire 観察 | Medium |
| 3 | morning_brief budget | ~3 | 手動 invoke で $2 budget 動作確認 | High (毎朝の盲点) |
| 4 | rival_detection error log | ~15 | 次回 failure 時 message 確認 | Medium |
| 5 | orphan 3 件 cleanup | ~10 (one-shot) | 修正後 _check_phase_c_health で orphan=0 verify | Low |
| 6 | W139 coverable timing | 0 (要調査) | Codex 助言後判断 | Medium (false positive 可能性) |
| 7 | rival_pricing misfire | ~3 | 次の 0:45 / 6:45 fire 観察 + scheduler.log 確認 | High |

## 設計上の検討事項 (Codex 要 review)

1. **Problem #1**: 履歴 failed 記録を残すか、orphan 5/02 と一緒に削除するか? K2 Surgical 観点で「履歴は残す」が正論だが、user 報告の見やすさには干渉
2. **Problem #3 budget**: $2 で適切か? Opus 4.7 で morning brief 1 回 = 大規模 context. $5 にすべきか? CLAUDE.md Q6 (Opus 4.7 1 日 30 calls) と整合性
3. **Problem #5 orphan cleanup**: one-shot script vs scheduler 自動 cleanup ロジック? 後者は K2 越境 + 別 W だが、再発防止としては魅力的
4. **Problem #6 W139**: 投機修正禁止 (K0 違反防止)、何を verify すべきか
5. **Problem #7 misfire**: 個別 misfire_grace_time 上書きで「保険」する vs 根本調査? 後者は時間コスト大、保険で運用継続可

## 実装順

1. Problem #6 を実 DB で再 verify (現状 ebay_item_id の有無確認)
2. Problem #5 one-shot script 作成 + 実行
3. Problem #3 morning_brief budget 修正
4. Problem #7 misfire_grace_time 明示
5. Problem #4 rival_detection error log 強化
6. Problem #2 fuel_surcharge raw HTML dump
7. Problem #1 履歴は残す (no-op)
8. pytest + scheduler restart + verify

## Codex review で確認したいポイント

- 上記 7 件の root cause 判定が正しいか
- 修正方針の優先度・LOC 配分は K1 Simplicity 違反していないか
- 投機的修正 (#6) を含めていないか
- one-shot DB cleanup の Q2 6 step 抜けがないか
- 別 W に逃すべき item (例: misfire の根本調査) を含めていないか
