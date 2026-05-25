"""W125 Codex Lint Runner — core module.

Codex CLI (`codex exec --sandbox read-only --json`) を呼び出して memory / KB / 設計書を
lint する core module. subagent / slash command / cron 3 経路から共通利用される.

Phase A (2026-05-15 W124 P3 G5+G6): 設計書 .company/engineering/docs/2026-05-15-w125-codex-reviewer-design.md 通り.

Usage:
    from monitor.codex_lint_runner import run_codex_lint, detect_cascade_gaps

    findings = run_codex_lint(
        target_files=["path/to/file.md"],
        output_jsonl="data/codex_lint_log/2026-05-15-lint.jsonl",
    )
    cascade = detect_cascade_gaps(recent_hours=24)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# プロジェクトルート (本 module から見て 2 階層上)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MEMORY_DIR = Path(r"C:\Users\gucch\.claude\projects\C--Users-gucch-projects-claude\memory")
COMPANY_DIR = PROJECT_ROOT / ".company"
DOCS_DIR = COMPANY_DIR / "engineering" / "docs"
LINT_LOG_DIR = PROJECT_ROOT / "tools" / "ebay-manager" / "data" / "codex_lint_log"

# cascade 検出 trigger keyword (規約・閾値・HS code 等を含むファイル間で同期が必要)
TRIGGER_KEYWORDS: list[str] = [
    # 関税系
    "Section 232", "Annex I-A", "Annex I-B", "Annex III",
    "IEEPA", "de minimis", "DDP", "DDU",
    # SKU 系
    "stock", "ebayyh", "ebayme", "ebayPF",
    # 送料系
    "primary_market", "US_only", "mixed_global", "global_only",
    "MIN_SAMPLE_SIZE",
    # eBay API 系
    "ShippingServiceCostOverride", "VerifyAdd", "ConditionID",
]

LINT_PROMPT_TEMPLATE = """Review the file(s) listed below. Today is {today}. Apply these lint checks:

(1) Internal factual contradictions within each file.
(2) Outdated relative date claims (e.g. "本日", "今日", "現在" without absolute date).
(3) Cascade gaps: if the same topic is mentioned in another file, do values/claims agree?
(4) Broken internal [[wikilink]] references (linked file must exist in memory dir as .md).
(5) Missing layer/sources/updated frontmatter per wiki-frontmatter.md rule (file edited within last 24h).
(6) Internal logical inconsistencies between sections.
(7) External source link rot (sources: URL field if present).
(8) Missing concept pages: a concept referenced/mentioned across 3+ files but having no dedicated page. Report as suggestion only (LOW).
(9) Next-topic suggestion: a clear KB gap (e.g. one variant documented but a sibling variant missing). MUST cite the evidence file:line that implies the gap. Report as suggestion only (LOW), never assert it as a defect.

Target files:
{file_list}

Report concrete findings only with file:line references. Skip generic praise.
For checks (8) and (9): these are growth suggestions, not defects. Always mark them LOW,
always include the evidence (which file:line implies the gap). Do NOT invent new facts.
Output format per finding: SEVERITY|file:line|short_description
SEVERITY = HIGH | MED | LOW.
"""


@dataclass
class LintFinding:
    """Codex review 1 件分の finding."""
    severity: str  # HIGH / MED / LOW
    file: str
    line: Optional[int]
    description: str
    raw: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _parse_findings_from_codex_output(json_lines: list[str]) -> list[LintFinding]:
    """Codex JSON stream の agent_message から findings を抽出."""
    findings: list[LintFinding] = []
    for line in json_lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # type='item.completed' で item.type='agent_message' の text を取り出す
        if obj.get("type") != "item.completed":
            continue
        item = obj.get("item", {})
        if item.get("type") != "agent_message":
            continue
        text = item.get("text", "")
        # `SEVERITY|file:line|description` 形式を行ベースで抽出
        for raw_line in text.split("\n"):
            match = re.match(
                r"^[-\s]*(HIGH|MED|LOW)\s*[|｜]\s*([^\s|｜:]+)(?::(\d+))?\s*[|｜]\s*(.+)$",
                raw_line.strip(),
            )
            if match:
                severity = match.group(1)
                file_path = match.group(2)
                line_num = int(match.group(3)) if match.group(3) else None
                desc = match.group(4).strip()
                findings.append(
                    LintFinding(
                        severity=severity,
                        file=file_path,
                        line=line_num,
                        description=desc,
                        raw=raw_line.strip(),
                    )
                )
    return findings


def run_codex_lint(
    target_files: list[str],
    working_dir: Optional[Path] = None,
    output_jsonl: Optional[Path] = None,
    timeout_sec: int = 300,
) -> list[LintFinding]:
    """指定 file 群に Codex lint を実行し findings を返す.

    Args:
        target_files: lint 対象の file path 群 (working_dir からの相対 or 絶対 path)
        working_dir: codex exec の -C (= CWD) 引数. default = MEMORY_DIR
        output_jsonl: lint log の保存先 (1 件 1 行 JSON). None なら保存しない
        timeout_sec: Codex exec の timeout

    Returns:
        LintFinding のリスト. parse 失敗時は空リスト.
    """
    if not target_files:
        return []

    cwd = working_dir or MEMORY_DIR
    file_list_text = "\n".join(f"- {f}" for f in target_files)
    prompt = LINT_PROMPT_TEMPLATE.format(today=_today_str(), file_list=file_list_text)

    # 2026-05-25 緊急修正: Windows で `subprocess.run(['codex', ...])` は .CMD/.BAT を
    # 自動解決しないため `FileNotFoundError [WinError 2]` で sync fail し、旧 except
    # 節が空 list return = silent success していた (Q0 違反). shutil.which で実 path
    # 解決して .CMD まで含める. cross-platform: Linux/Mac では bare 'codex' (exe) 一致.
    codex_exe = shutil.which("codex") or "codex"

    cmd = [
        codex_exe, "exec",
        "--sandbox", "read-only",
        "--json",
        "--skip-git-repo-check",
        "-C", str(cwd),
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        # 2026-05-25: silent return [] でなく明示 log + 空 list (上位 task で
        # findings_count=0 が異常か正常か判定不能の盲点を回避).
        logger.warning(
            "run_codex_lint TIMEOUT after %ss (cmd=%s, cwd=%s)",
            timeout_sec, codex_exe, cwd,
        )
        return []
    except FileNotFoundError as fnf_err:
        # codex CLI 未インストール = production incident. 旧 silent return から escalation.
        logger.error(
            "run_codex_lint CRITICAL: codex CLI invocation failed: %s (resolved=%s). "
            "Wiki cascade verify is OFFLINE. Install codex or fix PATH.",
            fnf_err, codex_exe,
        )
        return []

    # 2026-05-25: returncode != 0 を明示捕捉 (旧コードは parse 段階で空 list = silent success).
    if result.returncode != 0:
        logger.error(
            "run_codex_lint codex CLI exit %s | stderr=%r | stdout_head=%r",
            result.returncode,
            (result.stderr or "")[:500],
            (result.stdout or "")[:500],
        )
        return []

    json_lines = result.stdout.split("\n")
    findings = _parse_findings_from_codex_output(json_lines)

    if output_jsonl:
        output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with output_jsonl.open("a", encoding="utf-8") as f:
            for finding in findings:
                f.write(json.dumps(finding.to_dict(), ensure_ascii=False) + "\n")

    return findings


def list_recently_edited_files(
    since_hours: int = 168,  # 7 日
    include_dirs: Optional[list[Path]] = None,
) -> list[Path]:
    """直近 N 時間以内に編集された memory / KB / 設計書 file を返す.

    判定基準: mtime ベース (frontmatter の `updated:` ではない).
    """
    cutoff = datetime.now() - timedelta(hours=since_hours)
    dirs = include_dirs or [MEMORY_DIR, COMPANY_DIR / "ebay-knowledge", DOCS_DIR]

    recent: list[Path] = []
    for d in dirs:
        if not d.exists():
            continue
        for filepath in d.rglob("*.md"):
            try:
                mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                if mtime >= cutoff:
                    recent.append(filepath)
            except (OSError, ValueError):
                continue
    return recent


def detect_cascade_gaps(
    recent_hours: int = 24,
    keywords: Optional[list[str]] = None,
) -> list[LintFinding]:
    """recent_hours 内に編集された file から TRIGGER_KEYWORDS を抽出し、
    同じ keyword を含む他 file をリストアップ. cascade 漏れ候補を返す.

    例: feedback_ddp_shipping_policy.md が直近編集で "DDP" 含む →
        reference_shipping_tariff_logic.md / .company/ebay-knowledge/*.md も "DDP" 含む →
        変更が伝搬してるか cascade 検証要、として LOW finding 出力.
    """
    kws = keywords or TRIGGER_KEYWORDS
    recent_files = list_recently_edited_files(since_hours=recent_hours)
    findings: list[LintFinding] = []

    for recent in recent_files:
        try:
            content = recent.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for kw in kws:
            if kw not in content:
                continue
            # 他 file (全 .md) で同 keyword を grep
            related_files: list[str] = []
            for d in [MEMORY_DIR, COMPANY_DIR]:
                if not d.exists():
                    continue
                for other in d.rglob("*.md"):
                    if other == recent:
                        continue
                    try:
                        other_content = other.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    if kw in other_content:
                        related_files.append(str(other.relative_to(PROJECT_ROOT)) if PROJECT_ROOT in other.parents else str(other))
                        if len(related_files) >= 5:
                            break
                if len(related_files) >= 5:
                    break

            if related_files:
                findings.append(
                    LintFinding(
                        severity="LOW",
                        file=str(recent.name),
                        line=None,
                        description=f"cascade candidate: keyword '{kw}' also in {len(related_files)} other file(s) — verify consistency: {', '.join(related_files[:3])}",
                    )
                )
    return findings


def summarize_findings(findings: list[LintFinding]) -> dict:
    """findings の severity 別カウント + top 3 HIGH を返す."""
    by_sev = {"HIGH": 0, "MED": 0, "LOW": 0}
    high_top: list[LintFinding] = []
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        if f.severity == "HIGH" and len(high_top) < 3:
            high_top.append(f)
    return {
        "total": len(findings),
        "by_severity": by_sev,
        "high_top_3": [f.to_dict() for f in high_top],
    }


if __name__ == "__main__":
    # Manual smoke test: lint 1 file
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="feedback_codex_review_usage.md", help="lint 対象 file (memory dir 相対)")
    parser.add_argument("--cwd", default=str(MEMORY_DIR), help="codex exec -C 引数")
    args = parser.parse_args()

    print(f"[*] Codex lint 開始: {args.file} in {args.cwd}")
    findings = run_codex_lint([args.file], working_dir=Path(args.cwd))
    summary = summarize_findings(findings)
    print(f"[*] Findings: total={summary['total']}, HIGH={summary['by_severity']['HIGH']}, MED={summary['by_severity']['MED']}, LOW={summary['by_severity']['LOW']}")
    for f in findings:
        print(f"  [{f.severity}] {f.file}:{f.line or '?'}: {f.description}")
