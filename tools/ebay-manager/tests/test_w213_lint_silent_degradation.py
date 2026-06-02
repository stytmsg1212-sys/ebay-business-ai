"""W213: codex lint サイレント劣化修正のテスト (2026-06-02).

Codex CLI 失敗 (returncode≠0) 時に、空 list で silent success に見せず、
source='llm_error' の sentinel finding を jsonl に残すことを担保。
(gpt-5.3-codex モデル非対応で LLM レビューが死んでいたのに cascade 検知だけで
ログが埋まり成功偽装していた事故の再発防止。)
"""
import subprocess
from pathlib import Path

import monitor.codex_lint_runner as R


class _FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_codex_failure_writes_llm_error_sentinel(tmp_path, monkeypatch):
    """returncode≠0 で sentinel(source='llm_error', HIGH)を返し jsonl に書く."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeProc(1, stderr="model not supported"),
    )
    out = tmp_path / "lint.jsonl"
    findings = R.run_codex_lint(["dummy.md"], output_jsonl=out, working_dir=tmp_path)

    assert len(findings) == 1
    assert findings[0].source == "llm_error"
    assert findings[0].severity == "HIGH"
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "llm_error" in body, "失敗 sentinel が jsonl に残ること (silent 防止)"


def test_codex_success_zero_findings_no_sentinel(tmp_path, monkeypatch):
    """returncode==0 で 0 件なら sentinel を出さない(成功と失敗の区別)."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeProc(0, stdout=""),
    )
    out = tmp_path / "lint.jsonl"
    findings = R.run_codex_lint(["dummy.md"], output_jsonl=out, working_dir=tmp_path)
    # 0 件成功 = sentinel 無し (llm_error と区別できる)
    assert all(f.source != "llm_error" for f in findings)


def test_lintfinding_default_source_is_llm():
    """LintFinding の source デフォルトは 'llm'."""
    f = R.LintFinding(severity="LOW", file="x.md", line=None, description="d")
    assert f.source == "llm"
