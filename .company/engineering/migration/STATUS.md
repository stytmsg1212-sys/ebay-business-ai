---
status: 保留 (2026-05-27)
decision_by: user
reason: Phase 0 実機検証で Anthropic Claude モデルが Codex ChatGPT account で使用不可と確定。業務判断 agent (6 個) を GPT-5.5 化することの業務影響が大きいため、移行は当面見送り。
---

# Codex CLI 全面移行: 保留

## 保留時点での到達点

- ✅ harness 全体 scan 完了 (agents 6 / hooks 11 / skills 4 / commands 3 / scripts 5 / rules 8 / rule-snippets 5)
- ✅ 設計書 3 視点完成: Claude v1 / Codex full / Claude v2 (公式 docs ベース訂正版)
- ✅ Phase 0 検証 2/3 完了:
  - 検証 1 (features list): `child_agents_md=under development`, `memories=experimental`, `remote_control=under development`, `hooks=stable`, `plugins=stable`, `multi_agent=stable`
  - 検証 2 (Claude model 動作): **`claude-opus-4-7` not supported when using Codex with a ChatGPT account** が明確に確定
  - 検証 3 (hook exit 2 BLOCK): **未実施** (検証 2 で blocker 確定したため中断)

## 確定した 3 大 blocker

1. **業務判断 agent 6 個が全部 GPT-5.5 化** (Claude モデル不可)。Karpathy K3 がモデル依存、Opus 4.7 → GPT-5.5 で業務判断力が同等か未測定
2. **24h 自走 (`claude --remote-control`) の Codex 同等機能 (`remote_control`) は under development**。外部 cron 必須
3. **MEMORY.md auto-load (Codex `memories`) は experimental**。手動 resume 化必要

## 保留中の資産 (将来再開時に参照)

| ファイル | 内容 |
|---|---|
| `codex-migration-design-claude.md` | Claude v1 視点設計書 (Codex ヒアリング経由、誤り 5 件あり / 参考保管) |
| `codex-migration-design-codex.md` | Codex 視点設計書 (224 行、`codex features list` 実機反映 / 権威性最高) |
| `codex-migration-design-v2-diff.md` | Claude v2 視点 (公式 docs 直接 fetch ベースの訂正版) |
| `codex-alternatives.md` | Codex 提案の代替案 (3 blocker 対策、bg job `b1z3ubnq2` の出力先) |
| `_codex_prompt.txt` / `_codex_alternatives_prompt.txt` | Codex 起動時の prompt (再開時の再実行用) |
| `../../../.codex/agents/test-claude.toml` | Phase 0 検証 2 で使った test agent (Claude モデル指定確認用) |
| `../../../.codex/hooks/test-block.sh` | Phase 0 検証 3 で使う予定だった test hook |

## 再開判断材料

以下の条件が満たされた場合、移行検討再開:
- Codex CLI の `remote_control` / `memories` が under development → stable に昇格
- Codex で multi-provider 設定 (`-c provider=anthropic`) で Claude モデル使用が公式サポート
- GPT-5.5 系が業務判断 (VeRO / Section 232 / 出品 / 仕入先) で Opus 4.7 同等の品質と実証

## 現状維持で良い理由

- Anthropic Claude Code は安定動作中
- 業務影響なく現業務 (eBay 物販 + AI 自動化) は全機能稼働
- Codex は `codex-reviewer` agent として **2 軍運用** で引き続き利用 (文書 lint / 外部視点 review)
