"""W125 daily_codex_lint: 直近 7 日に編集された memory / KB / 設計書を Codex で lint.

cron 設計: 毎日 03:00 JST に発火 (主 batch 02:30 と Plus 5h 枠を分散).
出力: data/codex_lint_log/<YYYY-MM-DD>-lint.jsonl
Discord 通知: HIGH severity 3 件以上で 1 日 1 回 cap.

設計書: .company/engineering/docs/2026-05-15-w125-codex-reviewer-design.md §4
出典: 2026-05-15 W124 P3 G5+G6 実装.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logger = logging.getLogger(__name__)
TASK_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TASK_DIR.parent.parent.parent
LINT_LOG_DIR = TASK_DIR.parent / "data" / "codex_lint_log"

# 1 cron 実行あたりの対象 file 数の上限 (Plus 5h 枠の余裕担保)
MAX_FILES_PER_RUN = 30
# HIGH severity Discord 通知 threshold
DISCORD_HIGH_THRESHOLD = 3


def run(config: Optional[dict] = None) -> dict:
    """daily_codex_lint task entry point.

    Returns:
        {"success": bool, "findings_count": int, "high_count": int,
         "files_checked": int, "log_path": str, "message": str}
    """
    sys.path.insert(0, str(TASK_DIR.parent))
    from monitor.codex_lint_runner import (
        run_codex_lint,
        detect_cascade_gaps,
        list_recently_edited_files,
        summarize_findings,
    )

    today = datetime.now().strftime("%Y-%m-%d")
    # 2026-05-25: dir 未作成で FileNotFoundError が連日発生 (5/19-5/25 で 5 日失敗).
    # log_path.open("a") 直前で気付くと recent_files=0 早期 return で dir 作成すら
    # skip される構造だったため、entry 冒頭で確実に mkdir.
    LINT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LINT_LOG_DIR / f"{today}-lint.jsonl"

    # 直近 7 日に編集された file を対象
    recent_files = list_recently_edited_files(since_hours=168)
    if not recent_files:
        logger.info("daily_codex_lint: 直近 7 日に編集された file なし、skip")
        return {
            "success": True,
            "findings_count": 0,
            "high_count": 0,
            "files_checked": 0,
            "log_path": str(log_path),
            "message": "no recent edits",
        }

    # 上限を超える場合は最新優先で分割 (Plus 枠保護)
    if len(recent_files) > MAX_FILES_PER_RUN:
        logger.warning(
            f"daily_codex_lint: 対象 {len(recent_files)} 件 > 上限 {MAX_FILES_PER_RUN} 件, "
            f"最新優先で先頭 {MAX_FILES_PER_RUN} 件のみ実行"
        )
        recent_files = sorted(
            recent_files, key=lambda p: p.stat().st_mtime, reverse=True
        )[:MAX_FILES_PER_RUN]

    logger.info(f"daily_codex_lint: {len(recent_files)} files を Codex lint")

    # Codex lint 実行
    target_paths = [str(p) for p in recent_files]
    findings = run_codex_lint(
        target_files=target_paths,
        output_jsonl=log_path,
        timeout_sec=600,  # 大量 file 想定で 10 分
    )

    # cascade 検出 (G6)
    cascade = detect_cascade_gaps(recent_hours=24)
    if cascade:
        with log_path.open("a", encoding="utf-8") as f:
            import json
            for c in cascade:
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")

    all_findings = findings + cascade
    summary = summarize_findings(all_findings)
    high_count = summary["by_severity"]["HIGH"]

    # Discord 通知 (HIGH 3+ 件)
    if high_count >= DISCORD_HIGH_THRESHOLD:
        try:
            # board#22: codex lint は system ch (未設定なら既定 ch に fallback)
            from notifiers.discord_notifier import notifier_for
            notifier = notifier_for("system")
            top_3 = summary["high_top_3"]
            msg = (
                f"📋 **Codex Lint 結果 ({today})**\n"
                f"HIGH: {high_count} 件 / MED: {summary['by_severity']['MED']} / "
                f"LOW: {summary['by_severity']['LOW']}\n\n"
                f"**Top HIGH**:\n"
                + "\n".join(
                    f"- {h['file']}:{h.get('line') or '?'}: {h['description'][:80]}"
                    for h in top_3
                )
                + f"\n\n詳細: `{log_path.name}`"
            )
            notifier.send(msg)
            logger.info(f"daily_codex_lint: Discord 通知送信 (HIGH={high_count})")
        except Exception as e:
            logger.warning(f"daily_codex_lint: Discord 通知失敗: {e}")

    return {
        "success": True,
        "findings_count": len(all_findings),
        "high_count": high_count,
        "files_checked": len(recent_files),
        "log_path": str(log_path),
        "message": f"lint完了 {len(all_findings)} findings (HIGH={high_count})",
    }


if __name__ == "__main__":
    # Manual smoke test
    logging.basicConfig(level=logging.INFO)
    result = run()
    print(f"Result: {result}")
