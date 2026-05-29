"""W124 P1 G3: 既存 ⭐⭐⭐ memory の frontmatter 遡及拡張 (one-shot).

対象: MEMORY.md の ⭐⭐⭐ marked file (約 40 件).

追加項目:
- layer: wiki (memory 全体が wiki 層)
- updated: <file mtime の日付> (今日付けにはしない、staleness 判定の正しさ維持)

既存項目は保持. sources は file ごとに判断が必要なので本 script では追加しない.

Idempotent: 既に layer/updated がある file は skip (raise なし、行数 0 報告).
"""
from __future__ import annotations
import os
import re
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(r"C:\Users\gucch\.claude\projects\C--Users-gucch-projects-claude\memory")

# MEMORY.md から ⭐⭐⭐ marked file を抽出
TARGET_FILES = [
    # Sessions
    "session_2026_05_15_w124_codex_full_completion.md",
    "session_2026_05_15_w122_first_fire_verify.md",
    "session_2026_05_14_w122_implementation.md",
    "session_2026_05_13_windows_update_restart_recovery.md",
    "session_2026_05_12_power_recovery_w120_w121_verify.md",
    "session_2026_05_10_evening_w119_implementation.md",
    "session_2026_05_10_w183_w184_complete.md",
    "session_2026_05_10_morning_w108_w115_w183_planning.md",
    "session_2026_05_10_market_rollback_w108_w115.md",
    "session_2026_05_09_w112_w109_w110_w113_autonomous.md",
    "session_2026_05_08_api_quota_default500g_antibot.md",
    "session_2026_05_07_w7a_watchdog_w105_inventory.md",
    "session_2026_05_05_phase7_complete_silent_skip_sonnet.md",
    # Feedback (必読 ⭐⭐⭐)
    "feedback_no_postponement_antipattern.md",
    "feedback_explain_in_plain_language.md",
    "feedback_autonomous_work.md",
    "feedback_no_silent_skip_no_fake_success.md",
    "feedback_karpathy_principles.md",
    "feedback_check_clock_proactively.md",
    "feedback_dig_into_failures_immediately.md",
    "feedback_verify_numbers_before_reporting.md",
    "feedback_opus_price_watch.md",
    "feedback_sku_misuse_repeat_offense.md",
    "feedback_one_session_per_day.md",
    "feedback_handoff_staleness_check_2026_05_03.md",
    "feedback_sessionstart_recovery.md",
    "feedback_discord_visual_verify_required.md",
    "feedback_doc_placement_check_first.md",
    "feedback_session_handoff_zero_paste.md",
    "feedback_codex_review_usage.md",
    "feedback_memory_staleness_2026_04_30.md",
    "feedback_proactive_web_research.md",
    "feedback_competitor_jp_sellers_only.md",
    "feedback_definition_of_done_protocol.md",
    "feedback_db_migration_idempotency.md",
    "feedback_post_db_modification_review.md",
    "feedback_silent_skip_prevention.md",
    "feedback_customs_response_strategy.md",
    "feedback_roadmap_auto_add.md",
    "feedback_ddp_shipping_policy.md",
    "feedback_quality_principles_from_qiita.md",
    # Reference (⭐⭐⭐)
    "reference_shipping_tariff_logic.md",
    "reference_section_232_kb.md",
    "reference_shipping_method_vs_ddu_taxonomy.md",
    "reference_sku_naming_convention.md",
]


def migrate_one(filepath: Path) -> tuple[str, str]:
    """1 file の frontmatter 遡及拡張. Returns (status, reason)."""
    if not filepath.exists():
        return "missing", f"file not found"

    content = filepath.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        return "skip", "no frontmatter (--- delimiter not found at top)"

    # frontmatter 終端 (2 つ目の ---) の位置
    end_match = re.search(r"\n---\n", content[4:])
    if not end_match:
        return "skip", "frontmatter end (--- 2nd) not found"

    fm_end = 4 + end_match.start()
    frontmatter = content[4:fm_end]
    body = content[fm_end:]

    has_layer = bool(re.search(r"^layer:\s*\S", frontmatter, re.MULTILINE))
    has_updated = bool(re.search(r"^updated:\s*\S", frontmatter, re.MULTILINE))

    if has_layer and has_updated:
        return "already_done", "layer + updated 既存"

    # file mtime を YYYY-MM-DD で取得 (staleness 判定の正しさのため、今日付けにしない)
    mtime = datetime.fromtimestamp(filepath.stat().st_mtime).strftime("%Y-%m-%d")

    added = []
    if not has_layer:
        frontmatter += "layer: wiki\n"
        added.append("layer")
    if not has_updated:
        frontmatter += f"updated: {mtime}\n"
        added.append(f"updated={mtime}")

    new_content = "---\n" + frontmatter + body
    filepath.write_text(new_content, encoding="utf-8")
    return "updated", f"added {', '.join(added)}"


def main():
    updated_count = 0
    already_count = 0
    skip_count = 0
    missing_count = 0

    for filename in TARGET_FILES:
        filepath = MEMORY_DIR / filename
        status, reason = migrate_one(filepath)
        marker = {"updated": "+", "already_done": "=", "skip": "?", "missing": "!"}[status]
        print(f"  [{marker}] {filename}: {reason}")
        if status == "updated":
            updated_count += 1
        elif status == "already_done":
            already_count += 1
        elif status == "skip":
            skip_count += 1
        elif status == "missing":
            missing_count += 1

    print()
    print(f"Total: {len(TARGET_FILES)} files")
    print(f"  Updated: {updated_count}")
    print(f"  Already done: {already_count}")
    print(f"  Skip: {skip_count}")
    print(f"  Missing: {missing_count}")


if __name__ == "__main__":
    main()
