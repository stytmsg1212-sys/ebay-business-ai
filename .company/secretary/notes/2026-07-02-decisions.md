# 2026-07-02 意思決定

- [決定] Fable 5 復帰に伴うモデル切替は **メインエージェントのみ** (`.claude/settings.json` model = `claude-fable-5` + `/model` で現行セッション切替済) とし、様子見する。
  - 据え置き (Opus 4.8 のまま): research 脳系 8 処理 (`research_router.py` SOURCE_MODEL_DEFAULTS / news_deep_dive / opus_video_enricher / task_generate_search_keywords) / subagent 定義 7 本 (`.claude/agents/*.md` frontmatter) / docs 文言 (CLAUDE.md Q6 等)
  - 全面移行する場合の洗い出しは本日 session で完了済 (レイヤー1: Python 実処理、レイヤー2: subagent frontmatter、レイヤー3: メイン既定 [済]、レイヤー4: docs cascade)。api_logger._PRICING への fable 単価追加 + effort 能力集合確認が移行時の必須項目 ([[feedback_model_migration_completeness]])

- [決定] **Fable 5 の運用方針 (user 指示)**: Fable サブスクは **2026-07-07 まで**、以降は高額従量課金 = 最上位モデルは Opus 4.8 に戻る。Fable は Opus 4.8 よりトークン消費が多い。
  - Fable (main) = 調査設計の要所 + 判断のみ。他は全て subagent へ割り振り (塩梅は assistant 裁量)
  - **7/7 までに `.claude/settings.json` model を `claude-opus-4-8` へ戻す** (戻し忘れ = 従量課金事故。MEMORY.md 索引に ⚠️ 付きで記録済)
  - Python 実処理 (research 脳系) / subagent frontmatter の Fable 化は**実施しない** (7/7 で無意味になるため)
  - 〜7/7 の Fable 枠は AI 店長設計レビュー等の難所に優先投下する

- [決定] **AI 店長の価格権限委譲ライン (user 回答 2026-07-02)**:
  - **値下げは 1 回 5% まで**
  - **同一商品が連続 3 回値下げされたらアラート** (ライバルも同種の自動値下げシステム使用を想定 = 値下げ合戦スパイラルの検知が目的)
  - ⚠️ ヒアリング議事録 §12「最大値下げ幅/ceiling 等の数値は設けない」(2026-06-23) を**上書きする改訂** → 議事録・Phase1 設計書に両論併記で cascade 反映 (本日 subagent 委譲)
- [記録] user の値付け・競合チェック作業 = **約 3 時間/日** (AI 店長の効果算定基礎: 月 ~90 時間)
- [実測] W183 自動値下げは稼働中: rival_pricing_refresh 本日 2 回 completed、価格変更 7 日 298 件 / 30 日 833 件、competitor_products active 178 件

- [決定] **AI 店長 Phase1 = GO 確定 (user 承認 2026-07-02、条件 3 つ全て合意)**:
  - 条件1: 既存採用ライバル 178 件は **eligible 温存 + 裏で AI 一括再判定し疑い分のみ user 提示** (設計書 §11 Q2 の (b)+(c) 折衷、値下げ停止期間ゼロ)
  - 条件2: **5% 上限 + 3 連続値下げアラートを W183 に先行実装** (Phase1 本体と独立、本日着手)
  - 条件3: Shadow 卒業基準 = **2 週間・would_be_eligible vs user 実採用の不一致率 5% 以下で昇格ボタン**
  - §11 残り: confidence 0.85/0.6 初期値 / cron 03:00 / DDU 手動リストのみ / 警告ブランド Holbein 第 1 号 — assistant 裁量で確定済
