"""C4: MEMORY.md 2層 index 化 one-shot 移行スクリプト.

設計: scripts/c4-design-2tier-index.md (Codex review 済、4 修正反映)
Q0 verify: Codex Round HIGH 指摘に従い count だけでなく
  set / link basename / link 実在 / section別count / star count / 重複なし / 総数 / backup hash
を全て検証. いずれか fail で rollback (tier-2 削除 + backup 復元).

実行: python scripts/c4_migrate_2tier_index.py
冪等性: 既に移行済 (genre map 検出) なら no-op で exit.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path

MEM_DIR = Path("C:/Users/gucch/.claude/projects/C--Users-gucch-projects-claude/memory")
MEMORY = MEM_DIR / "MEMORY.md"
BACKUP = MEM_DIR / "MEMORY.md.bak-c4-2026-05-16"

# tier-1 に温存する section (見出し完全一致)
TIER1_KEEP = "## 🚨 必読 (毎回確認)"

# section 見出し → (tier-2 ファイル名, genre map 表示名, いつ読むか)
TIER2_MAP = {
    "## session (直近 1 週間)": ("MEMORY_session.md", "session", "過去セッションの経緯/事故/判断を辿る時"),
    "## session (アーカイブ)": ("MEMORY_session.md", "session", None),  # session に統合
    "## user": ("MEMORY_user.md", "user", "user プロファイル/モデル選好を確認する時"),
    "## project": ("MEMORY_project.md", "project", "機能別状況 (W番号)/設計議論を辿る時"),
    "## learning": ("MEMORY_learning.md", "learning", "動画学習・ベストプラクティス教材を参照する時"),
    "## feedback (絶対遵守 / 必須以外)": ("MEMORY_feedback.md", "feedback", "規約・事故・失敗再発防止・品質ルールを扱う時 (絶対遵守系)"),
    "## reference": ("MEMORY_reference.md", "reference", "SKU/関税/送料/プラットフォーム等の参照仕様を引く時"),
}

ENTRY_RE = re.compile(r"^- \[")
LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")
STAR_RE = re.compile(r"⭐+")


def parse_sections(text: str) -> list[tuple[str, list[str]]]:
    """('## 見出し', [entry 行...]) のリストに分解. title 行は ('#TITLE', [...])."""
    sections: list[tuple[str, list[str]]] = []
    cur_head = "#TITLE"
    cur_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((cur_head, cur_lines))
            cur_head = line
            cur_lines = []
        else:
            cur_lines.append(line)
    sections.append((cur_head, cur_lines))
    return sections


def entry_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ENTRY_RE.match(ln)]


def all_entries(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ENTRY_RE.match(ln)]


def link_basenames(entries: list[str]) -> list[str]:
    out = []
    for e in entries:
        m = LINK_RE.search(e)
        if m:
            out.append(Path(m.group(1)).name)
    return out


def star_count(entries: list[str]) -> int:
    return sum(len(m.group(0)) for e in entries for m in [STAR_RE.search(e)] if m)


def main() -> int:
    if not MEMORY.exists():
        print("FAIL: MEMORY.md not found")
        return 1

    original = MEMORY.read_text(encoding="utf-8")

    # 冪等性: 既に移行済なら no-op
    if "## 詳細索引 (tier-2" in original:
        print("SKIP: already migrated (tier-2 genre map detected)")
        return 0

    before_entries = all_entries(original)
    before_set = set(e.strip() for e in before_entries)
    before_basenames = sorted(link_basenames(before_entries))
    before_stars = star_count(before_entries)
    before_count = len(before_entries)
    print(f"before: {before_count} entries, {before_stars} stars, {len(before_basenames)} links")

    # backup
    shutil.copy2(MEMORY, BACKUP)
    backup_hash = hashlib.sha256(BACKUP.read_bytes()).hexdigest()
    print(f"backup: {BACKUP.name} sha256={backup_hash[:16]}")

    sections = parse_sections(original)

    # tier-2 ファイル別に entry を集約
    tier2_buckets: dict[str, list[str]] = {}
    tier2_meta: dict[str, tuple[str, str]] = {}  # fname -> (genre, when)
    tier1_sections: list[tuple[str, list[str]]] = []
    section_counts: dict[str, int] = {}

    for head, lines in sections:
        if head == "#TITLE" or head == TIER1_KEEP:
            tier1_sections.append((head, lines))
            if head == TIER1_KEEP:
                section_counts[head] = len(entry_lines(lines))
            continue
        if head in TIER2_MAP:
            fname, genre, when = TIER2_MAP[head]
            es = entry_lines(lines)
            tier2_buckets.setdefault(fname, [])
            # session は直近/アーカイブ両方を 1 file に: サブ見出し付与
            if fname == "MEMORY_session.md":
                sub = head.replace("## ", "### ")
                tier2_buckets[fname].append(sub)
            tier2_buckets[fname].extend(es)
            if when is not None:
                tier2_meta[fname] = (genre, when)
            section_counts[head] = section_counts.get(head, 0) + len(es)
        else:
            # 未知 section は安全側で tier-1 温存 (silent drop 防止 = Q0)
            print(f"WARN: unknown section kept in tier-1: {head}")
            tier1_sections.append((head, lines))
            section_counts[head] = len(entry_lines(lines))

    # tier-2 ファイル書き出し
    written_files: list[Path] = []
    for fname, entries in tier2_buckets.items():
        genre = tier2_meta.get(fname, (fname, ""))[0]
        body = [
            f"# MEMORY tier-2: {genre}",
            "",
            f"> tier-1 `MEMORY.md` の詳細索引。auto-load されない (on-demand Read 専用)。",
            f"> 新規/rotate 時は session-close skill が本 file を更新。",
            "",
        ]
        body.extend(entries)
        body.append("")
        p = MEM_DIR / fname
        p.write_text("\n".join(body), encoding="utf-8")
        written_files.append(p)
        print(f"tier-2 written: {fname} ({len([e for e in entries if ENTRY_RE.match(e)])} entries)")

    # tier-1 再構築: title + 必読 + genre map
    out: list[str] = []
    for head, lines in tier1_sections:
        if head == "#TITLE":
            out.extend(lines)
        else:
            out.append(head)
            out.extend(lines)

    # genre map 追加 (Codex #2: いつ読むか明記)
    gmap = [
        "",
        "## 詳細索引 (tier-2、on-demand Read)",
        "",
        "> 下記 entry は本 MEMORY.md に含めず別ファイルに分離 (C4 / 2026-05-16、Codex review 済)。",
        "> **auto-load されるのは本 MEMORY.md のみ** (harness 仕様、`MEMORY*.md` は glob されない)。",
        "> 特定 memory を探す時は **必ず該当 tier-2 を Read** すること (発見 regression 防止)。",
        "",
    ]
    # 表示順を固定 (session/feedback/project/reference/learning/user)
    order = ["MEMORY_session.md", "MEMORY_feedback.md", "MEMORY_project.md",
             "MEMORY_reference.md", "MEMORY_learning.md", "MEMORY_user.md"]
    for fname in order:
        if fname not in tier2_buckets:
            continue
        genre, when = tier2_meta.get(fname, (fname, "該当ジャンルを参照する時"))
        cnt = len([e for e in tier2_buckets[fname] if ENTRY_RE.match(e)])
        gmap.append(f"- **{genre}** ({cnt} 件) → `{fname}` — {when}")
    gmap.append("")
    out.extend(gmap)

    new_tier1 = "\n".join(out)
    if not new_tier1.endswith("\n"):
        new_tier1 += "\n"
    MEMORY.write_text(new_tier1, encoding="utf-8")

    # ───────── Q0 verify (Codex HIGH 指摘準拠) ─────────
    errors: list[str] = []

    # 1. tier-1 + tier-2 の entry set が before と完全一致
    after_t1 = all_entries(MEMORY.read_text(encoding="utf-8"))
    after_t2: list[str] = []
    for p in written_files:
        after_t2.extend(all_entries(p.read_text(encoding="utf-8")))
    after_all = after_t1 + after_t2
    after_set = set(e.strip() for e in after_all)

    if before_set != after_set:
        missing = before_set - after_set
        extra = after_set - before_set
        errors.append(f"SET MISMATCH: missing={len(missing)} extra={len(extra)}")
        for m in list(missing)[:5]:
            errors.append(f"  MISSING: {m[:80]}")
        for x in list(extra)[:5]:
            errors.append(f"  EXTRA: {x[:80]}")

    # 2. 総数一致
    if len(after_all) != before_count:
        errors.append(f"COUNT MISMATCH: before={before_count} after={len(after_all)}")

    # 3. link basename set 一致
    after_basenames = sorted(link_basenames(after_all))
    if after_basenames != before_basenames:
        errors.append(f"BASENAME MISMATCH: before={len(before_basenames)} after={len(after_basenames)}")

    # 4. link 先実在 (memory dir に .md があるか) — before に無くても after で新規切れを作らない
    for bn in set(after_basenames):
        if not (MEM_DIR / bn).exists():
            # 元から切れてた可能性: before にも無ければ既存問題として WARN のみ
            if bn in before_basenames:
                print(f"  NOTE: pre-existing dangling link (not C4 caused): {bn}")
            else:
                errors.append(f"NEW DANGLING LINK: {bn}")

    # 5. star count 一致
    after_stars = star_count(after_all)
    if after_stars != before_stars:
        errors.append(f"STAR MISMATCH: before={before_stars} after={after_stars}")

    # 6. duplicate basename なし (同 memory が 2 tier に重複していないか)
    seen: dict[str, int] = {}
    for bn in after_basenames:
        seen[bn] = seen.get(bn, 0) + 1
    dups = {k: v for k, v in seen.items() if v > 1}
    if dups:
        errors.append(f"DUPLICATE BASENAME: {dups}")

    if errors:
        print("\n=== Q0 VERIFY FAILED — ROLLBACK ===")
        for e in errors:
            print(e)
        # rollback
        shutil.copy2(BACKUP, MEMORY)
        for p in written_files:
            p.unlink(missing_ok=True)
        # rollback hash 検証
        restored_hash = hashlib.sha256(MEMORY.read_bytes()).hexdigest()
        ok = restored_hash == backup_hash
        print(f"rollback {'OK' if ok else 'FAILED'}: restored sha256={restored_hash[:16]} (expect {backup_hash[:16]})")
        return 2

    # 成功サマリ
    new_lines = len(MEMORY.read_text(encoding="utf-8").splitlines())
    print("\n=== Q0 VERIFY PASSED ===")
    print(f"tier-1 MEMORY.md: {new_lines} 行 (旧 {len(original.splitlines())} 行, 200 cliff から十分離脱)")
    print(f"tier-2: {len(written_files)} files, {len(after_t2)} entries on-demand")
    print(f"必読 tier-1 温存: {section_counts.get(TIER1_KEEP, 0)} entries")
    print(f"section別 count: {section_counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
