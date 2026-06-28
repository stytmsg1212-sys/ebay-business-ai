# リサーチ対戦アリーナ — システム設計書 v2 (W286, 2026-06-27, Codex+Fugu査読反映)

> v1 (`2026-06-27-research-duel-arena-system-design.md`) を **Codex(HIGH4/MED4/LOW3) + Fugu(致命3/HIGH2/MED1, 実起動)** の査読で全面改訂。両者収束の致命指摘を反映し「**計測ハーネス先行・自己改善ループは相関実証後**」へ縮小(K1)。v1 は破棄せず履歴として残置。

## 0. 査読で覆った前提（実装前に必ず守る）

| 旧設計(v1) | 査読指摘 | 改訂(v2) |
|---|---|---|
| 「本丸=採点を `_build_past_judgments_block` へ注入」 | **category error**: 同一性判定(evaluate_match, 仕入先候補 money-direct)を汚染 (Codex H4/Fugu H1) | **evaluate_match へは一切入れない**。採点は別の「リサーチ選定」用に限定、相関実証まで evaluator 非接続 |
| 「翌朝も Terapeak で同一条件を開く」 | Terapeak は相対窓のみ=絶対日付 pin 不可 (Codex H1/Fugu M1) | **凍結 evidence bundle を UI 内表示**＋ top-N drift 検知でラウンド無効化。Terapeak リンク=参考 |
| 4テーブル+昇格エンジン+few-shot+ROI を一度に | ROI 相関は出品→売却で**8-12週後**=speculative (Fugu M3) | **Phase1=計測ハーネスのみ**。自己改善は相関実証後 |
| 「W290=actual_duty_rate 配線だけ」 | HS→率の決定器が無い (Codex H2/Fugu A1, money-direct) | 率を捏造せず **Section232該当フラグ+保守計上 / needs_review** |
| duel_ai_picks に profit 複製 | research_candidates と**2台帳分裂** (Fugu A2) | **rc_id 参照の薄い採点台帳**に |

## 1. 段階化（measurement-first）

### Phase 1 — 計測ハーネス（まずこれだけ）
目的: 「オーナー採点が**実利を予測するか**」を測れる土台 + 改良 sourcing の実投入。自己改善ループは作らない。

- **DB migration v82**(現行 user_version=81, Codex M1 確認): `duel_rounds` / `duel_ai_picks`(薄い) / `duel_user_picks`。状態遷移は `research_candidates_db._apply_status_in_conn` の **CAS + reason必須 + 許容遷移dict** を踏襲(Codex M4、素のUPDATE禁止)。冪等 try/except OperationalError + 2回 init_db テスト(Q2)。
- **夜間 `task_research_duel.py`**: 当日セル(6日ローテ)で AI 5品。sourcing は `research_poc.evaluate_product` → **research_candidates に着地(rc_id)**。`duel_ai_picks` は `rc_id` 参照 + `user_score(0-100)` / `user_fb_md` / `reject_tags_json` のみ(profit 複製しない/Fugu A2)。
- **凍結 evidence bundle**(Codex H1/Fugu M1): 取得 item_id/title/price/sold/url/top-N hash を `snapshot_json` 保存し UI でレンダリング。翌朝 top-N の重複率 < 閾値(例80%)ならそのラウンドを**採点無効**(drift 検知)。
- **UI `tab_research_duel.py`**: 条件ヘッダ(凍結) / ユーザー 1-5品+なぜ / ブラインドゲート(**サーバ側マスク**, DOM に値を出さない) / 0-100 スライダー + 減点タグ enum + **採点理由必須**(contrast bias 緩和/Fugu M2) / スコアボード(平均・0点率・中央値)。
- **完了 → Opus 深層学習**: 総括 + ルーブリック**候補**を `reference_research_rubric.md`(単一ファイル, user承認) + `system_improvements.json` 起票。**few-shot 配線・multi-file cascade は呼ばない**(K1/Fugu A3)。採点は offline label として蓄積(prompt version 記録)。
- **成功基準(K3)**: 主 = **採点 vs 実純利益の相関**(Phase2算出) / 監視 = 平均スコア6日移動平均・0点率。①②(主観)は**最適化目標にしない**(報酬ハッキング回避/Fugu H2)。

### Phase 2 — 自己改善ループ（30-60 picks 蓄積 + 相関実証後）
- **ROIパネル**: 出品確定時に `duel_ai_picks.ebay_item_id` を **backfill(ebay_item_id 識別のみ, supplier_url/title join 禁止/Codex H3)** → 売却同期(v81)/利益と join。NULL 行は「相関未確定」で明示除外(Q0)。
- **リサーチ選定専用 few-shot builder**(duel採点を読む, evaluate_match 非汚染/Codex H4)。
- **ルーブリック昇格**: support_count≥2 **かつ別 category_id 由来**(Codex L3 過学習耐性)。
- **6日ローテ自動化**。

## 2. エンジン修正（独立・money-direct・2段レビュー）
採点が過大利益に晒される前に W290 も**前倒し**(Codex L2 汚染ループ回避):
- **W288** ブランド+型番クエリ抽出(英語フルタイトル直投げ廃止 / 真因A, research_poc L761)
- **W287** カテゴリ/VeRO ゲート前段(`evaluate_sourcing_gate` に該当段無し=新規/Codex M2。category_id=0越境・`vero_brands.json` 再利用で重複回避 / 0点根治)
- **W291** `_best_hit` 最安→match優先(research_poc L648)。**コスト注(Codex M3)**: 全hit match=API N倍, cap$3超過 → token 事前フィルタで上位k件→match
- **W290** Section232: ①`actual_duty_rate` を CalcInput へ通す(research_poc L500 未配線) ②HS→率の決定経路(無い間は該当カテゴリ=保守計上 or needs_review, 率捏造禁止/Q0)。**sourcing群と同時投入**

## 3. レビュー・検証・ビルド順
- 各変更後 code-reviewer **HIGH=0** + Codex/Fugu 2段。Q1 DoD(Streamlit+Playwright+DB SELECT+scheduler.log 夜間fire)。
- 順序: **v82 migration → 改良sourcing(W288/287/291/290) → task_research_duel(凍結+5品) → tab(Claude Design確定後) → 完了学習(ルーブリック候補のみ)** → [Phase2: ROI/few-shot/昇格]

## 4. 残課題(実装中に確定、自走で潰す)
- 凍結 evidence bundle の取得粒度(harvest scraper 流用可否)。
- HS分類器の有無(無ければ保守フラグ運用に確定)。
- ブラインド contrast bias(採点理由必須 + anchor 商品で drift 検知)。
