# 2026-04-25 意思決定ログ (W13/W14 セッション)

セッション期間: 2026-04-24 昼〜2026-04-25 早朝

## 本日の主要意思決定

### 1. W13 X/Reddit/HN AI ニュース機能の要件確定
- **Q1**: C (X + Reddit/HN 等複数ソース)
- **Q2**: B (AI/Claude + eBay 物販業務向け)
- **Q3**: A (朝バッチ 1 回 06:00)
- **Q4**: B (Grok/Claude による AI impact 判定)
- **Q5**: D (複合 dedupe: URL→Title→LLM)
- **Q6**: A (既存メール/ニュースエリア統合 + ソースタグ + 枠拡張)
- **Q7**: A (ダッシュボードのみ、Discord 通知なし)

### 2. W14 通関対応自動化の要件確定
- **Q1**: C (検知+ドラフト+MONO Deck 内確認送信、Gmail を開かない)
- **Q2**: B (FedEx + DHL + UPS 3 キャリア対応)
- **Q3**: D (ハイブリッド KB: JSON Tier1 + Web Tier2 + 承認制 Tier3)
- **Q4**: C (gmail.compose+modify+send 全 3 scope、MONO Deck で送信ボタン完結)
- **Q5**: D (ダッシュボードウィジェット + 専用タブ)
- **Q6**: E (動的多層対応 — 本文解析 + user テンプレ library + Claude 合成 + 特殊ケース手動フォールバック)
- **Q7**: A (朝バッチ 1 回、06:10 オフセット)
- **最終 E2E**: 過去 1 年 FedEx+DHL 全件 backfill 実施

### 3. W14 FedEx 回答の戦略ルール確定 (feedback_customs_response_strategy.md)
- **Manufacturer** = 日本代理店を第一選択 (中国本社自認を避け高関税リスク回避)
- **End Use** = 商品の**実用途のみ** (resale / commercial / eBay の禁句化)
- **素材** = アルミ/鉄不使用の**明示的宣言**
- **末尾定型句** = `The shipper is a retailer and is not the manufacturer.` 必須
- **HTS コード** = 根拠 Ruling 付きで提示、最終判断は通関士に委ねる旨注記

### 4. W14 セキュリティ要件確定 (code-reviewer 審査)
- **OneDrive 同期外に Gmail token 移動**: `%LOCALAPPDATA%\ebay-manager\gmail_token.json`
- **recipient allow-list** (CARRIER_DOMAINS): static map + suffix 厳格検証
- **送信 atomicity**: `drafted → sending → sent/failed` 楽観的ロック
- **プロンプトインジェクション対策**: `<untrusted_source>` XML 隔離 + JSON schema 強制 + recipient は Claude 外で決定
- **Kill switch**: `customs_automation.send_enabled=false` で scheduler 再起動不要停止
- **Audit log 独立**: `customs_send_audit` immutable テーブル
- **段階 scope 取得**: compose+modify 先行→ send は送信実装直前に追加

### 5. ROADMAP 追加 (W15-W18 、4 件)
- **W15**: 同商品マッチング score 精度向上 (人間の経験を AI 化)
- **W16**: プロダクトリサーチ AI 化 + 未出品商品自動発掘
- **W17**: 出品アラート機能
- **W18**: 有在庫管理機能 (仕入メール自動取込 + 滞留在庫コンサル)

### 6. video_learning の実行頻度変更
- 旧: 朝 02:30 のみ (5 件/日)
- 新: 朝 02:30 + 夕 18:00 (10 件/日、pending 27 件を ~3 日で消化)

### 7. qty=0 backfill の max_skus_per_run 一時拡大
- 30 → 100 (93 件を一気に処理) → 30 に復元
- 結果: pending 復活候補 0 → **86 件** (見込み利益 Top10 で ¥1.28M)

### 8. `/add_s` slash command 作成
- user が「実装したいシステム」を口頭で伝えた際、即 ROADMAP 追加
- 忘却防止の恒久ルール (feedback_roadmap_auto_add.md)

## 未着手 ROADMAP (優先度「高」)

| tag | title |
|---|---|
| W5 | メーカー監視→自動出品 |
| W6 | 販売履歴レコメンド (Opus 4.7) |
| W7 | 最安値保持 (プライスマッチ) |
| W12 | 競合監視タブ 作り直し |
| W15 | マッチング score 精度向上 |
| W16 | プロダクトリサーチ AI 化 |
| W17 | 出品アラート機能 |
| W18 | 有在庫管理機能 |

## 完了タスク

- W13 X/Reddit/HN AI ニュース (`data/system_improvements.json#112` 完了)
- W14 通関対応自動化 (`data/system_improvements.json#113` 完了)
- qty=0 復活候補 backfill (93 SKU、86 件生成)
- FedEx TRK#870904145187 通関対応ドラフト (v3) 作成
- scheduler 再起動 (W13 06:00 / W14 06:10 cron 登録)
- Gmail OAuth 4 scope 再認可 (user 承認)
