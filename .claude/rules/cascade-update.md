# 波及 update 規約 (cascade update / 常時適用)

出典: 2026-05-14 W123-W125 統合設計書 §3.4 Adopted (Astro-Han Karpathy LLM Wiki paradigm)、既存 `md-files-can-be-wrong.md` R-4 (3 か所鏡像更新) の機械化版.

## 核心

規約 / 仕様 / 閾値 / API 仕様を変更する時、**関連する全ファイル** (memory / rule / KB / 設計書 / CLAUDE.md) を grep で発掘し、**同 session で同時更新** する.

理由: 1 か所だけ更新して残りを放置すると、古い情報が残存して将来の自分・他 agent が misjudge を生む. md-files-can-be-wrong.md R-4 の「3 か所鏡像更新」を、機械的な scan + 同 session 完結 で標準化する.

## 触発契機 (以下を編集する時必須)

| ファイル | 編集 → cascade 必須 |
|---|---|
| `CLAUDE.md` (project root) | ✅ |
| `.claude/rules/*.md` | ✅ |
| `~/.claude/rules/*.md` | ✅ |
| `reference_*.md` (memory) | ✅ |
| `.company/ebay-knowledge/topics/*.md` | ✅ |
| `tools/ebay-manager/CLAUDE.md` | ✅ |
| `feedback_*.md` (rule 性質を持つもの = Q0-Q6 / R-1〜R-12) | ✅ |
| `session_*.md` / `project_*.md` | 不要 (時系列・W 番号進捗) |

## 必須 step (5 段)

1. **core キーワード抽出**: 編集対象の topic を 1-3 語で特定 (例: `"Section 232"`, `"stock SKU"`, `"SpeedPAK Economy"`, `"DDP"`).
2. **grep で全件発掘**: `Grep` ツールで `.company/` / `.claude/` / memory / `tools/ebay-manager/CLAUDE.md` を全 scan.
3. **要否判定**:
   - **touching**: 直接該当箇所を含む → 同 session で更新必須
   - **informative**: 関連言及のみ → 内容次第で更新 (両論併記なら追記、訂正なら修正)
   - **unrelated**: 文脈無関係 → 触らない (K2 Surgical)
4. **同 session で同時更新**: touching と informative の必要箇所を 1 つの session 内で全て直す. 「次セッションで」と先送りしない.
5. **session memory に痕跡記録**: 「cascade scan 実施: keyword=X, 影響 N file, 更新 M file」を session_*.md に 1 行記録. 監査可能化.

## 例: 「Section 232 関税率 25% → 30%」改訂時

```bash
# Step 1: keyword = "Section 232" or "25%" or "Annex I-B"
# Step 2: grep
Grep "Section 232|Annex I-B" --include "*.md"
# 想定 hit:
#   - .company/ebay-knowledge/topics/section_232_tariff_2026_04.md (touching)
#   - tools/ebay-manager/CLAUDE.md (touching: DDP section)
#   - memory/reference_section_232_kb.md (touching)
#   - memory/feedback_ddp_shipping_policy.md (touching)
#   - memory/feedback_tariff_era.md (informative)
#   - memory/session_2026_04_25_close.md (unrelated, 時系列記録)

# Step 3-4: 5 file 同時更新 (両論併記 規約に従い、「過去 25%」「現状 30%」を併記)
# Step 5: session_2026_05_15_*.md に "cascade scan: Section 232 25→30%, 5 file 更新" 記録
```

## 違反例 (2026-04-29 SKU 規約改訂)

W7-A SKU 主キー崩壊事故で `tools/ebay-manager/CLAUDE.md` の `(連番)` `(一意キー)` 記述を 1 か所だけ修正 → `.claude/rules/sku-rules.md` 新設で **CLAUDE.md (project root)** / `feedback_sku_misuse_repeat_offense.md` も更新したが、2026-04-30 まで残存箇所があり assistant が誤判定を再生.

防止策 = 本 rule. 修正時に keyword 全 scan で取りこぼし防止.

## K2 Surgical Changes との両立

- 編集対象の topic に **直接関連する箇所のみ** 更新
- 「ついでに format 揃え」「ついでに古い記述を削除」 = K2 違反 (本 rule の scope 外)
- 各 file の更新は **最小差分** に保つ

## ページ分割時の発見可能性保持 (C5 / 2026-05-16 Astro-Han 追加採用)

memory / KB ページが肥大化して分割する時、**分割元に要約スタブと `[[新ページ]]` リンクを残す**. 分割元を空にして移動するだけだと、旧 path を参照していた他 file / 過去 session memory から辿れなくなり (orphan 化)、知識が事実上消失する.

- 分割元: 1-2 行の要約 + `→ 詳細は [[new-page-slug]]` を残す
- 新ページ: `related:` frontmatter or 本文冒頭で分割元を逆参照
- これは cascade scan の touching 対象 (分割は topic の物理移動 = 波及確定)

出典: Zenn 記事 (Astro-Han LLM Wiki) の「ページ分割時は分割元に要約を残し `[[新ページ]]` でリンク」、Codex review 済 (2026-05-16).

## W125 連携 (Phase 3 = 5/29 以降)

Codex non-source reviewer が以下を flag:
- 同 topic の言及が file 間で矛盾 (cascade 漏れの signal)
- 数値・閾値・HS code 等の値が file 間で食い違い

検出後の修正は 2-stage loop (Codex → Claude → user) で実施.

## 関連 rule

- `md-files-can-be-wrong.md` R-4 (3 か所鏡像更新) — 本 rule の原型
- `.claude/rule-snippets/contradiction-annotation.md` — 値変更時の書式 (on-demand snippet、2026-05-21 移動)
- `karpathy-principles.md` — K2 Surgical / K3 Goal-Driven
- `silent-skip-prevention.md` — 取りこぼし防止
