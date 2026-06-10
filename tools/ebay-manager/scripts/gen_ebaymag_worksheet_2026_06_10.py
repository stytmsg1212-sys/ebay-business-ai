#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBaymag プランv2 出品ワークシート生成 (one-shot, 2026-06-10 B方式用)。

data/ebaymag_publish_groups_2026_06_09.json (26組合せ/258商品) を
人間が操作しやすい checklist markdown に変換する。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "ebaymag_publish_groups_2026_06_09.json"
DST = ROOT / "data" / "ebaymag_publish_worksheet_2026_06_10.md"

d = json.loads(SRC.read_text(encoding="utf-8"))
gs = d["groups"]

lines = [
    "# eBaymag プランv2 出品ワークシート (B方式)",
    "",
    "生成: 2026-06-10 / 元データ: data/ebaymag_publish_groups_2026_06_09.json"
    " (2026-06-09 設計、26組合せ/258商品/784国別出品)",
    "",
    "- 手順 (1件ずつ確実方式): eBaymag で対象 item の詳細パネルを開く →"
    " unarchive → 記載の国トグルだけ ON → 保存",
    "- 各バッチ完了後に assistant が eBay 実機検証 (公開ページ/API)",
    "- done 列: 操作済 = [x] / 検証済 = [v]",
    "",
]

# 小グループ → 大グループの順 (試行しやすい順)
order = sorted(gs.items(), key=lambda kv: len(kv[1]))
total = 0
for k, items in order:
    total += len(items)
    lines.append(f"## グループ [{k}] — {len(items)} 件")
    lines.append("")
    lines.append("| done | item_id | title | 区分 |")
    lines.append("|---|---|---|---|")
    for it in items:
        t = it["title"].replace("|", "/")[:70]
        lines.append(f"| [ ] | {it['item_id']} | {t} | {it.get('区分', '')} |")
    lines.append("")

DST.write_text("\n".join(lines), encoding="utf-8")
print(f"written: {DST.name} / groups={len(gs)} items={total}")
