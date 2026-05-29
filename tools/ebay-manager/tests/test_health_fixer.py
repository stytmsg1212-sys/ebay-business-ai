"""Phase 2 health_fixer の gate ユニット test (claude 実起動なし).

カバー:
- _extract_diff      … ```diff fenced / 言語指定なし fenced / 抽出不能
- _parse_and_normalize_diff … パス接頭辞 (a/ b/ tools/ebay-manager/) 正規化 + 行数解析
- _gate_size  (G4)   … 規模超過 (>80 行 / >3 ファイル) を弾く
- _gate_scope (G1)   … 業務中核 / migration / config / 秘密 / JSON / 範囲外を弾く
- _gate_safety(G3)   … 秘密値 / 危険コード (DROP/DELETE/ALTER/except:pass) を弾く
- _git_apply_check + _py_compile_on_copy (G2) … 構文エラー diff を弾く (本番 tree 不変更)
- propose_fix         … _invoke_fixer_subagent を monkeypatch して verdict 経路を検証

注意: gate は **本番 tree を一切書き換えない**。G2 系テストは存在しない新規ファイル
(`/dev/null` → `scripts/_hf_selftest_*.py`) を対象にし、`git apply --check` は適用可否の
確認のみ・py_compile は一時コピーで行うため、test 後も `git status` は clean のまま。
"""
from __future__ import annotations

import monitor.health_fixer as hf


# ---------------------------------------------------------------------------
# _extract_diff
# ---------------------------------------------------------------------------
def test_extract_diff_fenced():
    answer = (
        "原因はこれです。\n\n```diff\n"
        "--- a/tasks/x.py\n+++ b/tasks/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        "```\n以上です。"
    )
    d = hf._extract_diff(answer)
    assert d is not None and d.startswith("--- a/tasks/x.py")
    assert "+b" in d


def test_extract_diff_generic_fence_when_diff_like():
    answer = "```\ndiff --git a/monitor/y.py b/monitor/y.py\n@@ -1 +1 @@\n-a\n+b\n```"
    d = hf._extract_diff(answer)
    assert d is not None and d.startswith("diff --git ")


def test_extract_diff_none_when_prose_only():
    assert hf._extract_diff("ただの説明文。diff はありません。") is None


# ---------------------------------------------------------------------------
# _parse_and_normalize_diff
# ---------------------------------------------------------------------------
def test_normalize_strips_tools_prefix():
    raw = ("--- a/tools/ebay-manager/tasks/x.py\n"
           "+++ b/tools/ebay-manager/tasks/x.py\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n")
    norm, files = hf._parse_and_normalize_diff(raw)
    assert "--- a/tasks/x.py" in norm
    assert "+++ b/tasks/x.py" in norm
    assert "tools/ebay-manager" not in norm
    assert len(files) == 1
    assert files[0]["path"] == "tasks/x.py"
    assert files[0]["added"] == 1 and files[0]["removed"] == 1


def test_normalize_new_file_from_devnull():
    raw = "--- /dev/null\n+++ b/scripts/new.py\n@@ -0,0 +1,2 @@\n+def f():\n+    return 1\n"
    norm, files = hf._parse_and_normalize_diff(raw)
    assert "--- /dev/null" in norm
    assert "+++ b/scripts/new.py" in norm
    assert len(files) == 1
    assert files[0]["path"] == "scripts/new.py"
    assert files[0]["added"] == 2 and files[0]["removed"] == 0


def test_normalize_counts_multi_file():
    raw = (
        "--- a/tasks/x.py\n+++ b/tasks/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/monitor/y.py\n+++ b/monitor/y.py\n@@ -1 +1,2 @@\n-c\n+d\n+e\n"
    )
    _, files = hf._parse_and_normalize_diff(raw)
    assert {f["path"] for f in files} == {"tasks/x.py", "monitor/y.py"}


# ---------------------------------------------------------------------------
# G4 規模
# ---------------------------------------------------------------------------
def test_gate_size_pass():
    files = [{"path": "tasks/x.py", "added": 40, "removed": 39}]
    assert hf._gate_size(files)["pass"] is True


def test_gate_size_too_many_lines():
    files = [{"path": "tasks/x.py", "added": 50, "removed": 40}]  # 90 > 80
    g = hf._gate_size(files)
    assert g["pass"] is False and "規模超過" in g["reason"]


def test_gate_size_too_many_files():
    files = [{"path": f"tasks/x{i}.py", "added": 1, "removed": 0} for i in range(4)]
    assert hf._gate_size(files)["pass"] is False


# ---------------------------------------------------------------------------
# G1 範囲
# ---------------------------------------------------------------------------
def test_gate_scope_allows_tasks_monitor_scripts():
    for p in ("tasks/task_x.py", "monitor/foo.py", "scripts/fix_y.py"):
        assert hf._gate_scope([{"path": p, "added": 1, "removed": 0}])["pass"] is True


def test_gate_scope_denies_business_core():
    for p in ("monitor/ebay_lister.py", "monitor/sku_mapping_manager.py",
              "monitor/ebay_client.py", "monitor/database.py"):
        g = hf._gate_scope([{"path": p, "added": 1, "removed": 0}])
        assert g["pass"] is False, p


def test_gate_scope_denies_migration():
    g = hf._gate_scope([{"path": "scripts/migrate_v99.py", "added": 1, "removed": 0}])
    assert g["pass"] is False and "migration" in g["reason"]


def test_gate_scope_denies_config_env_company_json():
    for p in ("config/schedule_config.json", "scripts/.env",
              ".company/notes.md", "monitor/data.json"):
        assert hf._gate_scope([{"path": p, "added": 1, "removed": 0}])["pass"] is False


def test_gate_scope_denies_out_of_scope():
    for p in ("app.py", "tabs/tab_x.py", "tasks/sub/deep.py"):
        assert hf._gate_scope([{"path": p, "added": 1, "removed": 0}])["pass"] is False


def test_gate_scope_denies_backslash_traversal():
    """Windows backslash / .. / 絶対パスで deny basename を迂回できないこと.

    `tasks/..\\monitor\\database.py` は allow regex `[^/]+` が backslash を 1 階層と
    みなす + rsplit('/') が区切りを認識できないため、対策前は business-core の
    database.py を escalate せず通していた (Codex review 2026-05-29 HIGH-1)。
    """
    for p in (
        r"tasks/..\monitor\database.py",   # backslash traversal (元の bypass)
        "tasks/../monitor/database.py",    # forward-slash traversal
        "monitor/../monitor/ebay_lister.py",
        "/etc/passwd.py",                  # 絶対パス (POSIX)
        r"C:\Windows\system32\x.py",       # 絶対パス (Windows)
        r"monitor\database.py",            # 純 backslash 区切り
    ):
        g = hf._gate_scope([{"path": p, "added": 1, "removed": 0}])
        assert g["pass"] is False, p


# ---------------------------------------------------------------------------
# G3 安全
# ---------------------------------------------------------------------------
def test_gate_safety_pass_clean():
    diff = "--- a/tasks/x.py\n+++ b/tasks/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    assert hf._gate_safety("修正しました。", [], diff)["pass"] is True


def test_gate_safety_detects_secret_in_answer():
    diff = "--- a/tasks/x.py\n+++ b/tasks/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    answer = "key は sk-ant-api03-AAAABBBBCCCCDDDD1234 です"
    g = hf._gate_safety(answer, [], diff)
    assert g["pass"] is False and "秘密情報" in g["reason"]


def test_gate_safety_detects_dangerous_added_code():
    for danger in ("DROP TABLE foo", "DELETE FROM bar WHERE 1",
                   "ALTER TABLE baz ADD COLUMN x"):
        diff = f"--- a/scripts/x.py\n+++ b/scripts/x.py\n@@ -1 +1,2 @@\n a\n+    {danger}\n"
        g = hf._gate_safety("ok", [], diff)
        assert g["pass"] is False, danger


def test_gate_safety_detects_except_pass():
    diff = ("--- a/tasks/x.py\n+++ b/tasks/x.py\n@@ -1 +1,2 @@\n a\n"
            "+    except Exception: pass\n")
    g = hf._gate_safety("ok", [], diff)
    assert g["pass"] is False and "危険コード" in g["reason"]


def test_gate_safety_ignores_dangerous_in_context_line():
    """文脈行 (削除でも追加でもない) の DROP TABLE は弾かない (既存コードへの言及)."""
    diff = ("--- a/scripts/x.py\n+++ b/scripts/x.py\n@@ -1,2 +1,2 @@\n"
            "     conn.execute('DROP TABLE old')\n-    x = 1\n+    x = 2\n")
    assert hf._gate_safety("ok", [], diff)["pass"] is True


# ---------------------------------------------------------------------------
# G2 構文 (本番 tree 不変更、新規ファイル diff で検証)
# ---------------------------------------------------------------------------
_NEW_OK = ("--- /dev/null\n+++ b/scripts/_hf_selftest_ok.py\n"
           "@@ -0,0 +1,2 @@\n+def f():\n+    return 1\n")
_NEW_BROKEN = ("--- /dev/null\n+++ b/scripts/_hf_selftest_broken.py\n"
               "@@ -0,0 +1,2 @@\n+def f(:\n+    return 1\n")


def test_git_apply_check_pass_new_file():
    norm, _ = hf._parse_and_normalize_diff(_NEW_OK)
    assert hf._git_apply_check(norm)["pass"] is True


def test_py_compile_on_copy_pass():
    norm, files = hf._parse_and_normalize_diff(_NEW_OK)
    assert hf._py_compile_on_copy(norm, files)["pass"] is True


def test_py_compile_on_copy_fails_on_syntax_error():
    norm, files = hf._parse_and_normalize_diff(_NEW_BROKEN)
    g = hf._py_compile_on_copy(norm, files)
    assert g["pass"] is False and "構文エラー" in g["reason"]


# ---------------------------------------------------------------------------
# propose_fix (claude 起動を monkeypatch、gate 経路の verdict を検証)
# ---------------------------------------------------------------------------
def _patch_claude(monkeypatch, answer: str, meta: dict | None = None):
    monkeypatch.setattr(
        hf, "_invoke_fixer_subagent",
        lambda *a, **k: (answer, meta or {"duration_ms": 5}))


def test_propose_fix_error_on_empty(monkeypatch):
    _patch_claude(monkeypatch, "", {"error": "timeout (180s)", "duration_ms": 1})
    p = hf.propose_fix("daily_codex_lint", "boom")
    assert p.verdict == "error"


def test_propose_fix_escalate_first_line(monkeypatch):
    _patch_claude(monkeypatch, "ESCALATE: 業務中核 (price) に触れる必要があり対象外")
    p = hf.propose_fix("daily_codex_lint", "boom")
    assert p.verdict == "escalated" and "業務中核" in p.reason


def test_propose_fix_error_when_no_diff(monkeypatch):
    _patch_claude(monkeypatch, "原因は分かりましたが diff は出せません。")
    p = hf.propose_fix("daily_codex_lint", "boom")
    assert p.verdict == "error"


def test_propose_fix_escalated_on_scope(monkeypatch):
    """範囲外パス (app.py) の diff は G1 で escalated."""
    answer = ("```diff\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n```")
    _patch_claude(monkeypatch, answer)
    p = hf.propose_fix("daily_codex_lint", "boom")
    assert p.verdict == "escalated"
    assert p.gates["scope"]["pass"] is False


def test_propose_fix_gate_failed_on_syntax(monkeypatch):
    answer = f"```diff\n{_NEW_BROKEN}```"
    _patch_claude(monkeypatch, answer)
    p = hf.propose_fix("daily_codex_lint", "boom")
    assert p.verdict == "gate_failed"
    assert p.gates["syntax"]["pass"] is False


def test_propose_fix_proposed_happy_path(monkeypatch, tmp_path):
    """全 gate pass → proposed + diff 保存 (保存先は gitignore 済 data/health_fixes/)."""
    # 保存先を tmp に逃がして本番 data/ を汚さない
    monkeypatch.setattr(hf, "HEALTH_FIXES_DIR", tmp_path / "health_fixes")
    answer = f"説明。\n```diff\n{_NEW_OK}```"
    _patch_claude(monkeypatch, answer)
    p = hf.propose_fix("daily_codex_lint", "boom")
    assert p.verdict == "proposed", p.reason
    assert p.changed_lines == 2
    assert p.touched_files == ["scripts/_hf_selftest_ok.py"]
    assert p.diff_path  # 保存された
    assert (tmp_path / "health_fixes").exists()
    assert all(g["pass"] for g in p.gates.values())


# ---------------------------------------------------------------------------
# _scrub_secrets (sink へ出る診断文字列 / ESCALATE 行の伏字化)
# ---------------------------------------------------------------------------
def test_scrub_secrets_redacts_known_patterns():
    assert "sk-ant-" not in hf._scrub_secrets("key=sk-ant-api03-AAAABBBBCCCCDDDD1234")
    assert "ghp_" not in hf._scrub_secrets("token ghp_" + "A" * 36)
    assert "AKIA" not in hf._scrub_secrets("AKIA0123456789ABCDEF")
    assert "[REDACTED]" in hf._scrub_secrets("key=sk-ant-api03-AAAABBBBCCCCDDDD1234")


def test_scrub_secrets_redacts_real_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-REALVALUE-zzzz9999")
    out = hf._scrub_secrets("起動失敗: ...sk-ant-REALVALUE-zzzz9999... が露出")
    assert "REALVALUE" not in out and "[REDACTED]" in out


def test_scrub_secrets_noop_on_clean_text():
    assert hf._scrub_secrets("claude exit 1 | stderr='boom'") == "claude exit 1 | stderr='boom'"


def test_propose_fix_escalate_reason_scrubbed(monkeypatch):
    """ESCALATE 行に秘密値が混ざっても reason は伏字化される (G3 を通らない経路の保険)."""
    _patch_claude(monkeypatch, "ESCALATE: 設定に sk-ant-api03-LEAKLEAKLEAK0000 を発見")
    p = hf.propose_fix("daily_codex_lint", "boom")
    assert p.verdict == "escalated"
    assert "sk-ant-" not in p.reason and "[REDACTED]" in p.reason


def test_invoke_fixer_diag_scrubbed_on_nonzero_exit(monkeypatch):
    """claude 非0終了時の診断 (proposal.reason 経由で Discord/DB) は秘密値を伏字化."""
    class _FakeProc:
        returncode = 1
        stderr = "auth error: sk-ant-api03-DIAGLEAK111122223333"
        stdout = ""
    monkeypatch.setattr(hf.subprocess, "run", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(hf.shutil, "which", lambda _x: None)
    answer, meta = hf._invoke_fixer_subagent("prompt")
    assert answer == ""
    assert "sk-ant-" not in meta["error"] and "[REDACTED]" in meta["error"]
