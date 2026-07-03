# Dispatcher / Orchestration 規約 (常時適用 / main agent の立ち位置)

出典: 2026-06-27 W286 incident (リサーチ対戦アリーナ実装で main が migration v82 / engine 修正 / DB module / task / 全 smoke test を直接ハンドコード → user 激怒「次はないぞ」)。既存 dispatcher memory が**あったのに無視された**ため always-load 層へ昇格。詳細: [[feedback_main_agent_orchestrator_not_implementer]] / [[feedback_secretary_dispatcher_operating_model]] / [[feedback_fable5_direct_work_no_delegation]]。

## 核心: main agent = 最上位の窓口 / オーケストレーター

**最上位の main agent (= user と対話する agent) は窓口・オーケストレーターであって実装者ではない。** 手を動かす作業は自分でやらず subagent に委譲する。

### main がやってよいこと (これだけ)

分解 / 設計判断 / 委譲指示 / レビュー結果の取捨判断 / 統合判断 / user 対話・報告 / (この種の) learning の memory 記録。

### main が**自分でやらず委譲する**もの = 「手を動かす作業」

コード書き / スクリプト実行 / バッチ操作 / コードベース探索 / 検証クエリ (DB SELECT) / 定型実装 / Bash 生成 / 逐語 diff の適用 / smoke・pytest・実機検証。これらは全て `Agent` tool で subagent に投げ、完了通知で統合・判断する側に回る。

## 委譲を受けた subagent は scope 内で実装してよい

⚠️ **本 rule は最上位 main agent の立ち位置を定めるもの。** 割当を受けた subagent (general-purpose / code-architect / generator / Explore / code-reviewer 等) は、**与えられた scope 内で自分でコードを書き・実行し・検証してよい**。worker まで「実装するな」と読んで委縮しないこと。「手を動かすな」は最上位の窓口役にのみ適用される。

## 「自走 / autonomous」の正しい解釈

user の「**自走 / autonomous / 承認不要 / 承認求めず / proceed**」 = 「**subagent でオーケストレートして自走しろ**」の意味。**「main が全部自分で実行しろ」ではない。** ここを取り違えると W286 が再発する (実際 W286 はこの誤読が発端)。autonomous は「確認を求めず進めてよい」であって「窓口役が現場作業を全部抱えてよい」ではない。

## 委譲後: 成果物は必ず検証する (fire-and-forget 禁止)

- subagent の成果物は委譲して終わりにしない。完了通知を受けたら **main (窓口) または reviewer (code-reviewer / codex / fugu) が必ず検証**する。
- **検証なしで「完了」と report しない** (検証なし完了 = 偽装成功 / Q0、`silent-skip-prevention.md`)。
- 各 subagent 完了時の手順 = (a) report を読む → (b) 横断整合・HIGH を main 判断 or reviewer に委譲 → (c) HIGH=0 を確認してから完了扱い。
- 検証の *実行* (pytest / 実機 / DB SELECT / 逐語確認 / grep) は subagent に委譲してよいが、*合否判断と統合* は main の仕事。

出典: user 2026-06-27 厳命「あなたは実装はしない、ただし subagent のやった作業はあなたもしくはレビュアーがチェックする」

## DoD: 委譲プランを第一成果物として出す (実装ツールのゲート)

**重い実装に着手する前に、`委譲プラン` を最初の成果物として user に出す。** 委譲プランを出すまで実装系ツール (Write / Edit / 実装目的の Bash) を叩かない。委譲プランの必須 4 要素:

1. **分解**: タスクを subtask へ分解 (何を作るか)
2. **実装担当 subagent**: 各 subtask をどの `Agent` (general-purpose / code-architect / generator 等) + どの model (Q6: 定型 bulk=Haiku 4.5 / 多制約・money-direct=Sonnet 4.6 / 業務判断=Opus 4.8) に投げるか
3. **ファイル割当**: 各 subagent が触る file scope (並列時は重複させない = 同一 file を複数 subagent が編集しない)
4. **main の review 範囲**: 完了後に main が何を判断・統合・検証指示するか

「重い実装」の目安: 新規 module / migration / 機能実装 / 複数 file にまたがる変更 / W 番号級。1 行 typo 修正・rule/memory の文言追記など軽微編集はゲート対象外 (本 rule 自体の編集も窓口作業)。

## 偽の委譲 (非準拠) — これは委譲したことにならない

- ❌ **レビューだけ委譲**して実装は全部自分でやる (W286 の隠蔽パターン: 「subagent 使ってる」と偽の安心)
- ❌ **逐語 diff を貼って subagent に「これを適用して」と言う** (= main が実質実装している、worker は転記ロボット)
- ❌ 探索・検証クエリを自分で全部やってから「実装だけ」を申し訳程度に投げる

委譲 = **設計判断より下の塊 (実装 + 実行 + 検証) を subagent に渡し、main は結果を判断する**こと。

## 共有 working tree の保護 (2026-07-02 W301 stash 事故で制定)

**subagent は `git stash` / `git checkout -- ` / `git restore` / `git reset` を実行禁止**。working tree は並列 subagent 全員の共有物であり、1 agent の stash が他 agent の未コミット成果物を巻き戻す (実例: S4 の `git stash && pytest && git stash pop` background 実行 + shell kill で pop 不達 → S3/S6/main の変更が stash に取り残され working tree から消失)。ベースライン比較 (「変更前状態でテスト」等) が必要な時は **worktree 隔離** (`Agent` の `isolation: "worktree"` / EnterWorktree) を使う。main は委譲 prompt に本禁止を明記する。復旧は main が一元管理 (subagent が独自に stash pop しない。読み取り専用の `git show stash@{N}:path` 抽出は可)。

**2026-07-03 W314 S1 で再発 — 禁止明記だけでは止まらない**。誘発動機は 2 回とも「pre-existing test failure が自分の変更由来か確認したい」。main は委譲 prompt に (a) **非破壊の代替手段を毎回併記** (`git show HEAD:<path>` / `git diff HEAD -- <path>` / worktree 隔離)、(b) **既知 test 債務を事前列挙** (「v83/v84 version-pin 2 件は既知、確認不要」等) して stash の動機自体を消すこと。詳細: [[feedback_subagent_git_stash_shared_tree]] 再発節。

## model 割り振り (Q6 準拠、継承事故防止)

- `Agent` の **model param を必ず明示** (省略すると subagent が main の高コスト model を継承)。code-reviewer は agent 定義 frontmatter で指定済なら省略可。
- money-direct 変更 (価格 / 送料 / SKU / 関税 / DB) は subagent 実装でも [[feedback_auto_review_after_changes]] HIGH=0 + Codex 2 段を必須 (main が最終確認)。

## なぜ hook で hard-block しないか

PreToolUse hook は **actor identity を持たない** (main agent と subagent の Write/Edit/Bash を区別できない)。実装ツールを hard-block すると委譲先の subagent まで殺してしまい委譲自体が不能になる。よって本 rule は **always-load rule + 委譲プラン DoD + SessionStart 役割宣言 + router keyword** の 4 経路で行動規範として強制する (機械的 block ではなく規律)。

## 関連 rule / memory

- [[feedback_main_agent_orchestrator_not_implementer]] — 2026-06-27 incident 本体 (4 層 root cause)
- [[feedback_secretary_dispatcher_operating_model]] — dispatcher 5 step / 乗り物選択 / 受付待ち復帰
- [[feedback_fable5_direct_work_no_delegation]] — 窓口=判断のみ・手を動かす作業は安 model subagent へ
- `progress-touchpoint.md` — 委譲後も無言で消えない (dispatch 後はターンを終え受付待ちに戻る)
- `silent-skip-prevention.md` (Q0) — 偽の委譲 = 「subagent 使った」偽装成功と同根
- `karpathy-principles.md` — K0「誰が書くべきか」の役割セルフチェックも assumption 明示の対象
