"""W124 P1 G3 修復: bulk_frontmatter_migration の newline bug 修復.

bug: frontmatter 末尾に newline がない file で `<最終行末>layer: wiki` の形で結合される事故.

修復: 各 file の frontmatter section 内で `非改行 + layer:` パターンを `改行 + layer:` に split.
"""
from __future__ import annotations
import re
from pathlib import Path

MEMORY_DIR = Path(r"C:\Users\gucch\.claude\projects\C--Users-gucch-projects-claude\memory")


def repair_one(filepath: Path) -> tuple[str, str]:
    if not filepath.exists():
        return "missing", "file not found"

    content = filepath.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        return "skip", "no frontmatter"

    # frontmatter 終端
    end_match = re.search(r"\n---\n", content[4:])
    if not end_match:
        return "skip", "no frontmatter end"

    fm_end = 4 + end_match.start()
    frontmatter = content[4:fm_end]
    body = content[fm_end:]

    # 修復 pattern: `非改行 + layer: wiki` を `改行 + layer: wiki` に
    new_fm = re.sub(r"([^\n])layer: wiki", r"\1\nlayer: wiki", frontmatter)

    if new_fm == frontmatter:
        return "clean", "no bug pattern"

    filepath.write_text("---\n" + new_fm + body, encoding="utf-8")
    return "repaired", "split <X>layer → <X>\\nlayer"


def main():
    # 影響受けた可能性のある file 全件 (migration script の TARGET_FILES と同じ)
    targets = [
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
        "feedback_no_postponement_antipattern.md",
        "feedback_autonomous_work.md",
        "feedback_no_silent_skip_no_fake_success.md",
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
        "feedback_quality_principles_from_qiita.md",
        "reference_section_232_kb.md",
        "reference_shipping_method_vs_ddu_taxonomy.md",
        "reference_sku_naming_convention.md",
    ]

    repaired = 0
    clean = 0
    for fn in targets:
        status, reason = repair_one(MEMORY_DIR / fn)
        marker = {"repaired": "FIX", "clean": "ok ", "skip": "skp", "missing": "!! "}[status]
        if status == "repaired":
            print(f"  [{marker}] {fn}")
            repaired += 1
        else:
            clean += 1

    print()
    print(f"Repaired: {repaired} files")
    print(f"Clean (no bug): {clean} files")


if __name__ == "__main__":
    main()
