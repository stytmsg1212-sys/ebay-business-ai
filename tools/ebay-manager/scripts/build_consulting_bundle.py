#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eBay 相談用ナレッジバンドル生成 (claude.ai プロジェクトへのアップロード用).

蓄積したノウハウ (コンサルKB + eBay規制ルール + 送料/関税リファレンス) を
1 つの markdown に束ねて出力する。出力ファイルを claude.ai の「プロジェクト」の
ナレッジにアップロードすれば、Web/スマホの手軽なチャットでこの知識に基づいた
相談ができる。

使い方:
    python tools/ebay-manager/scripts/build_consulting_bundle.py
出力:
    .company/ebay-knowledge/exports/ebay-consulting-bundle.md

ノウハウを更新したら本スクリプトを再実行 → 出力を claude.ai に再アップロード。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # tools/ebay-manager/scripts -> repo root
MEM = Path.home() / ".claude" / "projects" / "C--Users-gucch-projects-claude" / "memory"

# (表示見出し, ソースファイルパス) — 上ほど優先度高
SOURCES: list[tuple[str, Path]] = [
    ("コンサル知見 コンパイル版 (時間軸統合・最重要)",
     REPO / ".company/ebay-knowledge/topics/consultant-kb-compiled.md"),
    ("eBay 規制業務ルール (出品/通関/DDP/Section232/コンディションランク)",
     REPO / "tools/ebay-manager/CLAUDE.md"),
    ("送料・関税ロジック (US軸差分式 + 4区分 primary_market + DDP)",
     MEM / "reference_shipping_tariff_logic.md"),
    ("配送方法 vs DDU/DDP 分類 (混同禁止)",
     MEM / "reference_shipping_method_vs_ddu_taxonomy.md"),
    ("Section 232 関税 詳細KB (Annex I-A/I-B/III HTSリスト)",
     REPO / ".company/ebay-knowledge/topics/section_232_tariff_2026_04.md"),
]

OUT = REPO / ".company/ebay-knowledge/exports/ebay-consulting-bundle.md"

PREAMBLE = """\
# eBay 越境EC 相談ナレッジバンドル (MonoHonpo / TOYOTASUMI)

> このファイルは claude.ai の「プロジェクト」ナレッジにアップロードして使う、
> eBay 物販相談エージェント用の知識束です。コンサル相談ログの蒸留 + 自社の
> eBay 規制ルール + 送料/関税リファレンスを 1 ファイルに統合しています。
>
> **時間軸の鉄則**: 新しい情報が古い情報に優先。関税率/送料仕様/eBayポリシー/
> 手数料/ツール挙動は時限性が高い (⏰ マーク)。回答時は発言月を確認し、⏰ 項目は
> 「○○年○月時点の情報なので最新を要確認」と添えること。
> **規制業務 (HSコード分類/通関/VeRO知財判定) の最終責任は人間**。断定せず、
> 必要なら CBP CSMS / eBay公式 / 配送会社 / 通関士への確認を促すこと。
"""


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = [PREAMBLE]
    parts.append(f"\n> 生成日時: {datetime.now().strftime('%Y-%m-%d %H:%M')} / "
                 f"本スクリプト: tools/ebay-manager/scripts/build_consulting_bundle.py\n")
    included, missing = [], []
    for title, path in SOURCES:
        if not path.exists():
            missing.append(str(path))
            continue
        body = path.read_text(encoding="utf-8")
        parts.append(f"\n\n{'='*78}\n# 【{title}】\n# source: {path}\n{'='*78}\n\n{body}")
        included.append(f"{title} ({len(body)//1024}KB)")
    OUT.write_text("\n".join(parts), encoding="utf-8")

    print(f"OK: {OUT}")
    print(f"  総サイズ: {OUT.stat().st_size // 1024}KB")
    print("  含めたソース:")
    for s in included:
        print(f"    - {s}")
    if missing:
        print("  ⚠️ 見つからなかったソース (スキップ):")
        for m in missing:
            print(f"    - {m}")


if __name__ == "__main__":
    main()
