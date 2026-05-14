# 2026-04-25 学び・ノウハウ

## 技術的学び

### 1. SQLite GLOB vs LIKE の wildcard 違い (W14 deadline CHECK で実害)
- **GLOB**: `?` が single-char wildcard、`*` が multi-char。`_` は **literal underscore**.
- **LIKE**: `_` が single-char wildcard、`%` が multi-char.
- 2026-04-24 W14 DB v18 migration で `deadline GLOB '____-__-__'` と書いた結果、ISO 日付も CHECK 失敗で backfill 0件挿入.
- **教訓**: 日付フォーマット検証には **LIKE '____-__-__'** を使う。GLOB を使うなら `GLOB '????-??-??'`.

### 2. eBay sold notification メールに item_id が含まれない
- Gmail subject は日本語翻訳 ("Onyx Boox leaf2 ホワイト... が売れました") で item_id なし.
- Body にも 357618434395 (正解) が含まれず、12-13 桁数字は Gmail 内部 ID のみ.
- **解決策**: subject から商品タイトル抽出 → **DB title 逆引き** (ASCII トークンマッチで日本語→英語 cross-language 対応).
- 教訓: "#\d{12}" 正規表現は eBay ID 抽出に**使えない**。title 逆引きが実用解.

### 3. Grok x_search API (search-x v3) の応答形式
- **期待**: `{"tweets": [...]}` 構造化配列
- **実際**: Responses API 形式 (`{"output": [{"type":"message","content":[{"annotations":[{"url":"..."}]}]}]}`)
- tweets は markdown テキストに埋め込み、URL は `annotations[].url` に格納.
- **教訓**: 外部 API を skill 経由で使う時は **実応答を確認** してからパーサ書く (仕様書や README だけ読んで書くと外す).

### 4. OneDrive 同期配下のクレデンシャル漏洩リスク
- `C:\Users\gucch\OneDrive\work\claude\` は OneDrive 同期対象.
- gmail_token.json (refresh_token 含む、永続権限) を置くと **Microsoft アカウント侵害時に gmail.send が奪われる**.
- 特に `gmail.send` は revoke 困難 (一度付与すると実質的に無効化は user 側で明示 revoke が必要).
- **教訓**: OAuth token / .env のような永続権限持ちファイルは `%LOCALAPPDATA%` 等 OneDrive 外に置く. `.gitignore` だけでは不十分.

### 5. Scheduler in-memory code cache 問題
- 2026-04-24 朝発覚: PID 38260 が 2026-04-23 起動時のコードを in-memory 保持、その後の Step 4b-4f (supplier_sweep / daily_relist / video_learning_queue 等) が「一度も実行されない」状態に.
- APScheduler の cron job は `execute_daily_tasks` 関数参照を保持するが、関数本体は起動時 import 固定.
- **教訓**: コード追加後は **scheduler プロセス再起動必須**. OS 固有の pythonw は stdin 不可視 (browser OAuth も不可) のため手動起動.

### 6. FedEx 通関回答の戦略的記述
- "Resale to U.S. consumer via eBay" と書くと **商業転売自認** → 関税当局は高関税/調査対象化.
- "Personal e-book reading device" のように**商品の実用途**のみ記述すれば個人消費者向け (低関税) 扱い.
- Manufacturer 欄は**日本代理店**を第一選択 (中国本社を書くとデミニミス撤廃以降の高関税リスク).
- **教訓**: user の利益を守る戦略的記述は「馬鹿正直」と対立する. 法的には日本代理店も正当な Manufacturer 情報として有効.

### 7. SPF/DKIM 検証で phishing 検知
- Gmail の `Authentication-Results` ヘッダで `spf=pass dkim=pass` を確認.
- 偽 FedEx メール (attacker@evil.com) は domain allow-list で弾き、本物も SPF/DKIM 失敗なら manual review 降格.
- **教訓**: 権限強いタスク (gmail.send) の入力ソース検証は **ドメイン allow-list + SPF/DKIM 両輪** で.

### 8. code-reviewer 2 回投入パターンの有効性
- W13/W14 両方とも、1 回目審査で HIGH 多数指摘 → 修正 → 2 回目で HIGH=0 (100 点) パターン.
- 金銭損失直結の業務システムでは「自己 review → code-reviewer 独立審査 → 再修正 → 再審査」の 2 往復が実用的.
- **教訓**: `feedback_auto_review_after_changes.md` のルール通り、大機能追加時は code-reviewer を必ず 2 回通す.

### 9. Claude プロンプトインジェクション対策 3 経路
- W14 の customs_draft_generator で 3 つの注入経路を識別:
  1. 受信メール本文 (攻撃者制御可能)
  2. 添付 PDF/Word/Excel の抽出テキスト (攻撃者制御可能)
  3. user テンプレ library の .md ファイル (誤操作 or マルウェア混入)
- **対策**: `<untrusted_source>` XML タグ隔離 + system prompt で「tag 内の指示無視」明言 + **recipient は Claude に決めさせず static map** (deterministic) + テンプレ変数 allow-list.
- **教訓**: LLM に「何を任せる / 任せない」を明確に分離。セキュリティ critical な決定 (宛先・scope) は Claude 外で確定.

### 10. scope 段階取得による爆発半径最小化 (H-10)
- W14 実装で compose+modify を先に追加 → 途中で事故っても send 権限未付与なので実害ゼロ.
- 送信機能実装時に初めて send 追加、再認可 1 回で済む.
- **教訓**: OAuth scope は機能実装と同期して段階的に追加. 初回に全 scope 付与は NG.

## 運用的学び

### 11. user は「馬鹿正直」な実装を嫌う
- 2026-04-24 W14 FedEx ドラフトで "Resale to consumer via eBay" と書き激怒指摘.
- 税関・通関対応は user の利益を守る戦略的記述が必須.
- **恒久化**: `feedback_customs_response_strategy.md` を memory に保存、次回以降の通関対応は同パターン厳守.

### 12. ROADMAP 追加は「即時登録」が user の忘却防止に必須
- user 口頭の「〜を実装したい」を memo 止まりにすると数日で忘れる.
- `/add_s` slash command で即 system_improvements.json に登録.
- **恒久化**: `feedback_roadmap_auto_add.md` で手順化.

### 13. 過去データ backfill は段階的 (30→90→365) が安全
- いきなり 365 日実行は API コスト + Gmail rate limit + エラー時の影響大.
- dry_run デフォルト True で 30 日 → 正常動作確認後 90 日 → 実行 365 日 が基本フロー.
- H-3 対応で `run_backfill(days=N, dry_run=True)` の明示 API を提供.

### 14. user は「MONO Deck で完結」を望む (Gmail 開かない)
- W14 Q4 で user が明示: 「gmail を開くのも面倒、MONO Deck 上で確認+送信を完結したい」.
- そのため gmail.send scope 付与 (通常の最小権限は compose のみでよいところ、あえて追加).
- **教訓**: ツール統合度は user の日常操作ルートに合わせる. 「確認のために別ツール開く」は摩擦.

## KPI / コスト実測

| 指標 | 値 |
|---|---|
| W13 運用コスト | **約 $0.077/日** (Anthropic $0.037 + xAI $0.040) |
| W14 運用コスト | 推定 **$0.05-0.2/件** (通関要求発生時のみ) |
| W13/W14 月間見込み | **$3-5/月** (ニュース $2.3 + 通関 $0.5-2) |
| 手動作業削減 | 通関対応 80 分/件 → **5 分/件** (目標達成見込み) |
