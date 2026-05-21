# 矛盾アノテーション規約 (両論併記 / 常時適用)

出典: 2026-05-14 W123-W125 統合設計書 §3.4 Adopted (Astro-Han Karpathy LLM Wiki paradigm)、および既存 `md-files-can-be-wrong.md` R-1〜R-4 の自然拡張.

## 核心

同一 topic に **新旧異なる主張・矛盾する仕様** が判明した時、後勝ち式 supersession (新が古を上書き) で痕跡を消すのではなく、**両論併記** で経緯を残す.

理由: 後勝ち式は「なぜ古を捨てたか」根拠を失い、同じ判定変更を再発する温床になる. 両論併記は将来の自分・他 agent が「過去の判断と何が変わったか」を再現可能にする.

## 適用範囲

| ファイル種別 | 両論併記 | 後勝ち式 |
|---|---|---|
| `feedback_*.md` (memory) | ✅ 必須 | NG |
| `reference_*.md` (memory) | ✅ 必須 | NG |
| `.company/ebay-knowledge/topics/*.md` | ✅ 必須 | NG |
| `CLAUDE.md` / `.claude/rules/*.md` / 設計書 | ✅ 必須 | NG |
| `session_*.md` | NG (時系列記録、当然 1 視点) | 自然に後勝ち |
| `project_*.md` (W 番号進捗) | NG (現状最新で OK) | 自然に後勝ち |

## 書式 (3 block)

```markdown
## 現状の見解 (YYYY-MM-DD 〜)

(現在採用している判断 / 規約 / 数値)

## 過去の見解 (〜YYYY-MM-DD)

(以前採用していた判断、なぜ採用していたか)

## 矛盾点 / 変更理由

- 変更日: YYYY-MM-DD
- 契機: (事故 / 一次情報照合 / user 指摘 等)
- 何が違うか: (具体差分)
- 何が同じか: (適用範囲 / 例外 等で残った部分)
```

## 例 (SKU 一意キー)

```markdown
## 現状の見解 (2026-04-29 〜)

`ebay_listings` テーブルの listing 識別は `ebay_item_id` を使う. SKU は用途 2 つに限定 (有/無在庫判定 + URL 変換).

## 過去の見解 (〜2026-04-29)

SKU を listing 一意キー (主キー / 重複検出キー) として扱っていた. `WHERE sku=?` で 1 listing 特定、`GROUP BY sku` で重複検出.

## 矛盾点 / 変更理由

- 変更日: 2026-04-29
- 契機: W7-A SKU 主キー崩壊事故 (active 確定 SKU で stock:01 が 58 listing に並存)
- 何が違うか: 一意性の前提が崩れた. 有在庫は同 SKU を多数 listing が持つのが正常運用 (在庫種別フラグ、集約キーではない. 在庫/識別は ebay_item_id).
- 何が同じか: 無在庫の `ebay***_*****` 形式は引続き URL 変換用に使う.
```

## 適用しない場合の判定 (3 質問)

新 fact を memory に書く時、既存記述と矛盾しているか self-check:

1. 同じ topic を扱う既存 file はあるか? (grep で確認)
2. 既存記述と新 fact は **両立しないか**? (新が古を否定するか)
3. 否定する場合、古を消すか? **消さず両論併記**.

3 つ全部 yes なら両論併記の出番.

## 関連 rule

- `md-files-can-be-wrong.md` — .md は誤りを含み得る (本 rule の根因)
- `cascade-update.md` — 規約変更時の波及 scan (両論併記と連動して関連 file 一括 update)
- `karpathy-principles.md` — K0 仮定を明示

## W125 連携 (Phase 3 = 5/29 以降)

Codex non-source reviewer が以下を flag:
- 同 topic の異なる file 間で矛盾検出 (cross-file contradiction)
- 「過去の見解」block 欠落 (supersession の痕跡なし)

検出後の修正は 2-stage loop (Codex → Claude → user) で実施.
