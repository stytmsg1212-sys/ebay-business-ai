# C4: MEMORY.md 2層 index 化 設計 + Codex レビュー依頼

## 背景と前提条件の再評価 (K3 measurable goal)

eBay 越境 EC の AI 運用基盤 (Claude Code project)。`MEMORY.md` = 全 memory (業務ノウハウ/規約/経緯) の索引。SessionStart hook が **毎セッション開始時に全文 auto-load** (固有強み = 文脈ゼロから即把握)。

**実測 (2026-05-16)**:
- MEMORY.md: 156 行 / 20,347 文字 / 約 1 万トークン / 索引対象 149 ファイル
- SessionStart hook は **200 行超で truncation** (lines after ~200 silent drop)
- session-close skill にも「200 行 truncated 制限注意」と既に書かれている = 既知の未解決リスク
- 149 ファイルで増加中 → 早晩 200 行到達 → **索引サイレント欠落 (Q0 違反 latent)**

**過去の判定との矛盾アノテーション (両論併記)**:
- 過去 (2026-05-16 早朝、当初): C4 = 保留。Codex も保留。理由「token 圧迫の実測が無い、固有強み破壊リスク」
- 現状 (2026-05-16、実測後): 156/200 行 = 残 44 行、session-close で毎回 +1〜数行 = 数週間で cliff 到達。**proactive 対応の正当性が出た**。ただし固有強み (auto-load) 破壊リスクは依然存在 → 「auto-load 経路を変えない設計」で両立を狙う
- user 指示: 実施 (2026-05-16)。Codex 確認付き

## 設計目標 (K3)

1. auto-load される索引を 200 行 cliff から十分離す (目標: tier-1 ≤ 60 行)
2. ⭐⭐⭐ 必読 entry の「即文脈」便益は維持 (= tier-1 に温存)
3. 残り全 entry の発見可能性は失わない (tier-2 へ誘導)
4. **SessionStart hook のロジックは変更しない** (= 固有強み経路を触らない、最重要安全特性)
5. 149 entry の **1 件も消失させない** (Q0、移行 before/after count 一致で機械 verify)

## 設計

### Tier 1 = MEMORY.md (従来通り auto-load、消費経路 不変)

- `## 🚨 必読 (毎回確認)` section は **全文温存** (現状 ~40 行、毎回必要な critical context)
- 他 7 section (session直近/session-アーカイブ/user/project/learning/feedback/reference) の冗長な per-file 行を撤去し、**genre map (各 1 行: 件数 + tier-2 への pointer)** に置換
- 冒頭 header に明示: 「詳細索引は `MEMORY_<section>.md`。特定 memory を探す時はそれを Read」
- 想定: 156 行 → 約 55 行 (必読 ~40 + genre map ~8 + header ~7)、約 6K token/session 削減

### Tier 2 = MEMORY_<section>.md (on-demand、auto-load しない)

| ファイル | 内容 |
|---|---|
| `MEMORY_session.md` | session(直近1週間) + session(アーカイブ) の全行 |
| `MEMORY_feedback.md` | feedback section の全行 |
| `MEMORY_project.md` | project section |
| `MEMORY_reference.md` | reference section |
| `MEMORY_learning.md` | learning section |
| `MEMORY_user.md` | user section |

Claude は特定ジャンルの memory を探す時だけ該当 tier-2 を Read。

### SessionStart hook

**ロジック変更なし**。hook は今まで通り MEMORY.md を読むだけ。MEMORY.md が小さくなるだけ。
(hook の Python heredoc / Step 4.5 claude-loop check / 全 step に手を触れない)

### session-close skill (要更新、本設計で最もリスクのある部分)

- 新 session entry: 従来通り tier-1 `🚨 必読` 上部に 1 行追加 (session = critical recent、tier-1 維持)
- 旧「最新」session entry の rotation 先: tier-1 アーカイブ section ではなく `MEMORY_session.md` へ移動
- tier-1 genre map の件数: tier-2 増減時に同期更新
- zero-paste handoff (_NEXT_SESSION.md 生成) には**影響させない** (別 file、別経路)

### 移行 (one-time script、Q0 no silent loss)

1. MEMORY.md backup (`.bak-2026-05-16`)
2. 7 section の per-file 行を抽出 → 6 tier-2 file に分配
3. tier-1 を「必読 + genre map + header」に再構成
4. **verify: 移行前 `grep -c '^- \[' MEMORY.md` = 移行後 (tier1 必読 + 全 tier2) の entry 総数**。不一致なら rollback

## リスク評価 (K0 正直)

| リスク | severity | mitigation |
|---|---|---|
| 移行で entry 消失 | HIGH | before/after count 機械 verify、不一致 rollback |
| session-close skill 破損 (handoff 影響) | MEDIUM | skill 更新を最小差分、_NEXT_SESSION 経路は不変、更新後 dry-run |
| Claude 発見 regression (tier-2 読まず見落とし) | MEDIUM | tier-1 header に明示誘導 + 本設計を feedback memory 化 |
| SessionStart hook | LOW | ロジック不変 (設計の核心安全特性) |
| 既存 feedback memory の MEMORY.md 参照リンク切れ | MEDIUM | grep で `MEMORY.md` 参照箇所を cascade scan、誘導文修正 |

## Codex への質問

1. 「SessionStart hook を一切変更しない (MEMORY.md を小さくするだけ)」で固有強み (auto-load 即文脈) 破壊リスクは本当に LOW か? 落とし穴は?
2. tier-1 に「必読 全文 + genre map」、tier-2 に詳細、の分割は発見可能性を実質維持するか? Claude が tier-2 を読み忘れる構造的欠陥はないか?
3. session-close skill の更新 (rotation 先を tier-2 に) で zero-paste handoff (_NEXT_SESSION.md) が壊れる経路はあるか?
4. 移行の before/after entry count verify (Q0) はこの設計で十分か? もっと厳密な verify 方法は?
5. そもそも 156/200 行・149 file の現状で C4 を **今やる**のは妥当か、それとも依然 premature で別の軽量対処 (例: 単に 必読以外を圧縮) で十分か? 外部視点で率直に。

各回答 Severity (HIGH/MEDIUM/LOW) + 根拠 + 出典 URL (あれば)。設計に致命的欠陥があれば「実装前に再設計すべき」と明示してください。
