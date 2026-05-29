"""Phase 2 health-fixer runner — Tier2 コードバグの修正案を **ドライランで** 生成する.

役割: 定時実行ヘルスチェックが検知した「再実行では直らないコードバグ」
(subprocess returncode≠0 = codex_lint 型) に対し、サブスク claude (Opus 4.8、
`--agent health-fixer`) に **read-only で原因を特定させ unified diff を 1 個だけ**
返させる。返ってきた diff を 4 つの gate (規模 / 範囲 / 安全 / 構文) で検証し、
全 pass した提案だけを保存する。**commit / 本番 tree への適用は一切しない**。

最重要の安全設計:
- 起動する claude は agent 定義で **Read/Grep/Glob のみ** = 本番コードを物理的に
  書き換えられない (diff をテキストで返すだけ)。
- gate 検証は **本番 tree を一切触らない**: `git apply --check` は適用可否のみ確認
  (書込なし)、構文チェックは touch ファイルの **一時コピー** に当てて py_compile し
  即破棄する。
- subprocess 起動は research_brain._invoke_subagent と同じ Max 認証強制パターン
  (ANTHROPIC_API_KEY 除外 → サブスク内 = 実課金 $0)。

verdict 語彙:
  proposed     … 全 gate pass。diff を data/health_fixes/ に保存、人間レビュー待ち。
  gate_failed  … 安全 (G3) / 構文 (G2) gate で弾かれた。
  escalated    … 規模超過 (G4) / 範囲外・業務中核 (G1)、または claude が ESCALATE。
  error        … claude 起動失敗 / diff 抽出不能。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import py_compile
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # tools/ebay-manager
HEALTH_FIXES_DIR = PROJECT_ROOT / "data" / "health_fixes"

CLAUDE_CLI_DEFAULT_TIMEOUT = 180  # コード調査 + diff 生成は research brief より長め

# --- gate 閾値 (G4 規模) ---
_MAX_CHANGED_LINES = 80
_MAX_TOUCHED_FILES = 3

# --- G1 範囲: allow (これに合致 *かつ* deny に非該当のみ通す) ---
# tasks/monitor/scripts の **直下** の *.py のみ (ネスト不可)。3 dir は実際フラットで、
# ネストを許すと deny relpath (monitor/database.py 等) を nested copy で回避され得るため。
_ALLOW_PATH_RE = re.compile(r"^(tasks|monitor|scripts)/[^/]+\.py$")

# --- G1 範囲: deny (業務中核 / 設定 / 秘密 / migration、deny 優先で escalate) ---
_DENY_BASENAMES = frozenset({
    "ebay_lister.py", "sku_mapping_manager.py", "ebay_client.py",
})
_DENY_RELPATHS = frozenset({
    "monitor/database.py", "monitor/ebay_client.py",
})

# --- G3 安全: 秘密値パターン (応答全文 + diff をスキャン) ---
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),        # Anthropic API key
    re.compile(r"sk-[A-Za-z0-9]{20,}"),               # OpenAI 系
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),              # GitHub PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),                  # AWS access key
    re.compile(r"discord(?:app)?\.com/api/webhooks/", re.IGNORECASE),  # Discord webhook
]

# --- G3 安全: diff の追加行に混入してはいけない危険コード ---
_DANGEROUS_ADDED = [
    (re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE), "DROP TABLE"),
    (re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE), "DELETE FROM"),
    (re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE), "ALTER TABLE"),
    (re.compile(r"except\s*:\s*pass\s*$"), "except: pass"),
    (re.compile(r"except\s+Exception\s*:\s*pass\s*$"), "except Exception: pass"),
]


def _scrub_secrets(text: str) -> str:
    """秘密値らしき文字列を [REDACTED] に置換する (sink へ出す診断文字列用).

    G3 (_gate_safety) は diff の安全性を検証するが、claude 起動失敗時の診断文字列
    (stderr/stdout 断片) や agent の ESCALATE 行は G3 を通らず直接
    proposal.reason → Discord/DB に流れる。万一それらに秘密値が混ざった場合の保険
    として、sink に出す前に既知パターン + 親 process の実 ANTHROPIC_API_KEY を伏字化。
    """
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    real_key = os.environ.get("ANTHROPIC_API_KEY")
    if real_key and len(real_key) >= 12:
        out = out.replace(real_key, "[REDACTED]")
    return out


@dataclass
class FixProposal:
    """Tier2 修正案の検証結果 (ドライラン)."""
    task_key: str
    verdict: str  # 'proposed' | 'gate_failed' | 'escalated' | 'error'
    reason: str = ""
    diff: str = ""              # 正規化後の diff (proposed 時)
    diff_path: str = ""         # 保存先 (proposed 時のみ)
    gates: dict = field(default_factory=dict)
    changed_lines: int = 0
    touched_files: list = field(default_factory=list)
    duration_ms: int = 0


# ──────────────────────────────────────────────────────────────────────
# claude CLI 起動 (research_brain._invoke_subagent の複製、--agent health-fixer)
# ──────────────────────────────────────────────────────────────────────

def _invoke_fixer_subagent(
    prompt: str,
    timeout: int = CLAUDE_CLI_DEFAULT_TIMEOUT,
    max_budget_usd: float = 0.50,
) -> tuple[str, dict]:
    """claude CLI を subprocess で呼び health-fixer subagent の応答を得る.

    Max plan 認証を強制 (ANTHROPIC_API_KEY 除外) = サブスク内、実課金 $0。
    長大プロンプトは stdin 経由 (Windows CLI 引数長制限回避)。
    Returns: (answer_text, metadata_dict)
    """
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_SSE_PORT", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_CODE_EXECPATH", None)

    cmd = [
        "claude", "-p",
        "--agent", "health-fixer",
        "--model", "opus",
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-mode", "default",
        "--max-budget-usd", f"{max_budget_usd:.2f}",
    ]

    started = time.time()
    try:
        result = subprocess.run(
            cmd, input=prompt,
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, env=env, cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return "", {"error": f"timeout ({timeout}s)",
                    "duration_ms": int((time.time() - started) * 1000)}
    except FileNotFoundError:
        return "", {"error": "claude CLI not found in PATH", "duration_ms": 0}

    duration_ms = int((time.time() - started) * 1000)

    if result.returncode != 0:
        # WARNING: ここに API key の値 prefix を **絶対に追加しない** (Discord に流れる).
        # claude PATH は Windows ユーザー名 (PII) を含むため basename だけに削る.
        resolved = shutil.which("claude")
        claude_basename = Path(resolved).name if resolved else "NOT_FOUND_IN_PATH"
        api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
        diag = (
            f"claude exit {result.returncode} | "
            f"stderr={(result.stderr or '')[:300]!r} | "
            f"stdout={(result.stdout or '')[:300]!r} | "
            f"claude_basename={claude_basename} | "
            f"parent_api_key_set={api_key_present}"
        )
        # diag は proposal.reason 経由で Discord/DB に流れるため秘密値を伏字化。
        return "", {"error": _scrub_secrets(diag), "duration_ms": duration_ms}

    try:
        out = json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip(), {"duration_ms": duration_ms, "raw": True}

    answer = (
        out.get("result")
        or out.get("text")
        or out.get("response")
        or out.get("message", {}).get("content")
        or ""
    )
    if isinstance(answer, list):
        answer = "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in answer)
    return str(answer).strip(), {
        "duration_ms": int(out.get("duration_ms") or duration_ms),
        "raw_json": out,
    }


# ──────────────────────────────────────────────────────────────────────
# diff 抽出 + 正規化 (パス接頭辞の揺れを吸収)
# ──────────────────────────────────────────────────────────────────────

_DIFF_FENCE_RE = re.compile(r"```diff\s*\n(.*?)\n```", re.DOTALL)
_GENERIC_FENCE_RE = re.compile(r"```\s*\n(.*?)\n```", re.DOTALL)


def _extract_diff(answer: str) -> Optional[str]:
    """応答から unified diff を 1 個抽出する (```diff fenced block 優先)."""
    m = _DIFF_FENCE_RE.search(answer)
    if m:
        return m.group(1).strip("\n")
    # フォールバック: 言語指定なし fenced で中身が diff っぽいもの
    for m in _GENERIC_FENCE_RE.finditer(answer):
        body = m.group(1)
        if body.lstrip().startswith(("--- ", "diff --git ")):
            return body.strip("\n")
    return None


def _strip_path_prefixes(token: str) -> str:
    """diff ヘッダのパスから a/ b/ と tools/ebay-manager/ 接頭辞を剥がす."""
    if token == "/dev/null":
        return token
    for pre in ("a/", "b/"):
        if token.startswith(pre):
            token = token[len(pre):]
            break
    if token.startswith("tools/ebay-manager/"):
        token = token[len("tools/ebay-manager/"):]
    return token


def _hdr_path(line: str) -> str:
    """'--- a/foo\t2020...' 形式からプロジェクト相対パス (or /dev/null) を得る."""
    token = line[4:].split("\t", 1)[0].strip()
    return _strip_path_prefixes(token)


def _parse_and_normalize_diff(diff: str) -> tuple[str, list[dict]]:
    """diff を正規化 (パスをプロジェクト相対 a//b/ に統一) しつつ file 変更を解析.

    Returns: (normalized_diff, files)
      files = [{"path": projrel, "added": n, "removed": m}, ...]
    """
    out_lines: list[str] = []
    files: list[dict] = []
    cur: Optional[dict] = None
    pending_minus: Optional[str] = None

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                a = _strip_path_prefixes(parts[2])
                b = _strip_path_prefixes(parts[3])
                out_lines.append(f"diff --git a/{a} b/{b}")
            else:
                out_lines.append(line)
            continue
        if line.startswith("--- "):
            p = _hdr_path(line)
            pending_minus = p
            out_lines.append("--- " + ("/dev/null" if p == "/dev/null" else f"a/{p}"))
            continue
        if line.startswith("+++ "):
            p = _hdr_path(line)
            out_lines.append("+++ " + ("/dev/null" if p == "/dev/null" else f"b/{p}"))
            tp = p if p != "/dev/null" else (pending_minus or "")
            cur = {"path": tp, "added": 0, "removed": 0}
            files.append(cur)
            continue
        if line.startswith("@@"):
            out_lines.append(line)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if cur:
                cur["added"] += 1
            out_lines.append(line)
            continue
        if line.startswith("-") and not line.startswith("---"):
            if cur:
                cur["removed"] += 1
            out_lines.append(line)
            continue
        out_lines.append(line)

    norm = "\n".join(out_lines).rstrip("\n") + "\n"  # 末尾 newline を 1 個に正規化
    # /dev/null だけの空 path を除外 (異常 diff 防御)
    files = [f for f in files if f["path"]]
    return norm, files


# ──────────────────────────────────────────────────────────────────────
# gate 群 (G4 規模 → G1 範囲 → G3 安全 → G2 構文 の順で適用、最初の fail で停止)
# ──────────────────────────────────────────────────────────────────────

def _gate_size(files: list[dict]) -> dict:
    """G4: 変更行 ≤80 かつ touch ファイル ≤3."""
    changed = sum(f["added"] + f["removed"] for f in files)
    nfiles = len(files)
    ok = changed <= _MAX_CHANGED_LINES and nfiles <= _MAX_TOUCHED_FILES
    return {
        "pass": ok, "changed_lines": changed, "touched_files": nfiles,
        "reason": "" if ok else
                  f"規模超過: {changed}行/{nfiles}ファイル "
                  f"(上限 {_MAX_CHANGED_LINES}行/{_MAX_TOUCHED_FILES}ファイル)",
    }


def _gate_scope(files: list[dict]) -> dict:
    """G1: 全 touch パスが allow (tasks/monitor/scripts の *.py) かつ deny 非該当."""
    for f in files:
        p = f["path"]
        # traversal / 絶対パス / backslash 区切りは正規化前に escalate (Codex review
        # 2026-05-29 HIGH-1)。allow regex は '/' 単一階層しか通さないため通常は弾けるが、
        # `tasks/..\monitor\database.py` のような backslash 経路は rsplit('/') が
        # 区切りを認識できず deny basename 判定を迂回する。ここで明示 reject する。
        if (
            "\\" in p
            or p.startswith("/")
            or re.match(r"^[A-Za-z]:", p)
            or any(seg in ("..", ".") for seg in p.split("/"))
        ):
            return {"pass": False, "reason": f"異常パス {p} (traversal/絶対/区切り、自動修正禁止)"}
        base = p.rsplit("/", 1)[-1]
        # deny 優先 (業務中核 / migration / 設定 / 秘密)
        if base in _DENY_BASENAMES or p in _DENY_RELPATHS:
            return {"pass": False, "reason": f"業務中核ファイル {p} (自動修正禁止)"}
        if "migrat" in p.lower():
            return {"pass": False, "reason": f"migration 関連 {p} (Q2、自動修正禁止)"}
        if p.startswith("config/") or p.startswith(".env") or "/.env" in p:
            return {"pass": False, "reason": f"設定/秘密 {p} (自動修正禁止)"}
        if p.startswith(".company/"):
            return {"pass": False, "reason": f"組織文書 {p} (自動修正禁止)"}
        if p.endswith(".json"):
            return {"pass": False, "reason": f"JSON 設定/データ {p} (自動修正禁止)"}
        # allow whitelist
        if not _ALLOW_PATH_RE.match(p):
            return {"pass": False, "reason": f"範囲外パス {p} (tasks/monitor/scripts の *.py のみ許可)"}
    return {"pass": True, "reason": ""}


def _gate_safety(full_text: str, files: list[dict], norm_diff: str) -> dict:
    """G3: 応答全文 + diff に秘密値・危険コードが混入していないか."""
    # 秘密値 (応答全文 + diff の双方をスキャン)
    haystack = full_text + "\n" + norm_diff
    for pat in _SECRET_PATTERNS:
        if pat.search(haystack):
            return {"pass": False, "reason": "秘密情報パターン検出 (応答 or diff に混入)"}
    # 親 process の ANTHROPIC_API_KEY 実値が混入していないか (値は記録しない)
    real_key = os.environ.get("ANTHROPIC_API_KEY")
    if real_key and len(real_key) >= 12 and real_key in haystack:
        return {"pass": False, "reason": "ANTHROPIC_API_KEY の実値が応答に混入"}
    # diff の **追加行** に危険コード
    for line in norm_diff.splitlines():
        if not (line.startswith("+") and not line.startswith("+++")):
            continue
        added = line[1:]
        for pat, label in _DANGEROUS_ADDED:
            if pat.search(added):
                return {"pass": False, "reason": f"危険コード混入: {label} (Q0/Q2)"}
    return {"pass": True, "reason": ""}


def _git_apply_check(norm_diff: str) -> dict:
    """G2-a: `git apply --check` で本番 tree への適用可否のみ確認 (書込なし)."""
    tmp_patch = None
    try:
        fd, tmp_patch = tempfile.mkstemp(suffix=".patch", text=True)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(norm_diff)
        try:
            r = subprocess.run(
                ["git", "apply", "--check", "--recount", tmp_patch],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                encoding="utf-8", timeout=30,
            )
        except FileNotFoundError:
            return {"pass": False, "reason": "git CLI not found"}
        except subprocess.TimeoutExpired:
            return {"pass": False, "reason": "git apply --check timeout"}
        if r.returncode != 0:
            return {"pass": False,
                    "reason": f"git apply --check 失敗: {(r.stderr or '')[:200]}"}
        return {"pass": True, "reason": ""}
    finally:
        if tmp_patch and os.path.exists(tmp_patch):
            os.remove(tmp_patch)


def _py_compile_on_copy(norm_diff: str, files: list[dict]) -> dict:
    """G2-b: touch ファイルの一時コピーに diff を当てて py_compile (本番 tree 不変更).

    一時 dir に touch ファイルをコピー → git apply (一時 dir 内のコピーのみ書換) →
    patch 後の *.py を py_compile → 一時 dir 破棄。本番 tree は一切触らない。
    """
    tmpdir = tempfile.mkdtemp(prefix="health_fixer_")
    try:
        # 既存ファイルを一時 dir に相対パス保持でコピー (新規作成 = /dev/null 元は skip)
        for f in files:
            rel = f["path"]
            src = PROJECT_ROOT / rel
            dst = Path(tmpdir) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                shutil.copy2(src, dst)
        # git apply が repo context を要求する場合に備え一時 dir を空 repo 化
        try:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir,
                           capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return {"pass": False, "reason": "git init (tmp) 失敗"}
        # patch を一時 dir のコピーに適用 (本番 tree ではない)
        fd, tmp_patch = tempfile.mkstemp(suffix=".patch", dir=tmpdir, text=True)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(norm_diff)
        try:
            r = subprocess.run(
                ["git", "apply", "--recount", tmp_patch],
                cwd=tmpdir, capture_output=True, text=True,
                encoding="utf-8", timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return {"pass": False, "reason": f"git apply (tmp) 失敗: {e}"}
        if r.returncode != 0:
            return {"pass": False,
                    "reason": f"git apply (tmp) 失敗: {(r.stderr or '')[:200]}"}
        # patch 後の *.py を構文チェック
        for f in files:
            rel = f["path"]
            if not rel.endswith(".py"):
                continue
            patched = Path(tmpdir) / rel
            if not patched.exists():  # 削除された場合は skip
                continue
            try:
                py_compile.compile(str(patched), doraise=True)
            except py_compile.PyCompileError as e:
                msg = str(e).replace(tmpdir, "<tmp>")
                return {"pass": False, "reason": f"構文エラー {rel}: {msg[:200]}"}
        return {"pass": True, "reason": ""}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────
# prompt 構築 + 公開 API
# ──────────────────────────────────────────────────────────────────────

def _build_fixer_prompt(task_key: str, error_message: str) -> str:
    """health-fixer subagent への指示プロンプト (task_key + エラー抜粋)."""
    return (
        f"# 修正対象\n\n"
        f"定時実行タスク `{task_key}` が起動した subprocess が returncode≠0 で失敗しました。"
        f"単純な再実行では直らないコードバグの可能性が高いです。\n\n"
        f"## subprocess エラーメッセージ (execution_log からの抜粋・末尾は切れている可能性あり)\n\n"
        f"```\n{error_message}\n```\n\n"
        f"## あなたへの依頼\n\n"
        f"1. 上記は task_execution_log に記録された **先頭部分の抜粋** です。"
        f"完全な traceback が必要なら `logs/scheduler.log` を Grep で調べ、"
        f"`{task_key}` 周辺のエラー全文を確認してください。\n"
        f"2. `tasks/` `monitor/` `scripts/` 配下のソースを Read/Grep/Glob で調べ、"
        f"この失敗の **根本原因 (再実行で直らないコードの欠陥)** を特定してください。\n"
        f"3. 起点モジュールは `task_key` から推定できます "
        f"(例: `{task_key}` → `tasks/task_{task_key}.py`)。不明なら Grep で探してください。\n"
        f"4. **最小の修正**を unified diff (```diff fenced block 1 個、プロジェクト相対パス) "
        f"で返すか、業務中核/秘密/config/規模超過/原因不明なら先頭行 `ESCALATE: <理由>` で回付してください。\n\n"
        f"出力規約・禁止事項・対象範囲は agent 定義 (health-fixer) の指示に厳密に従ってください。"
    )


def _save_diff(task_key: str, norm_diff: str) -> str:
    """proposed diff を data/health_fixes/<JSTdate>_<task_key>_<hash8>.diff に保存."""
    HEALTH_FIXES_DIR.mkdir(parents=True, exist_ok=True)
    jst_date = datetime.now().strftime("%Y-%m-%d")  # JST naive (Windows local)
    h8 = hashlib.sha256(norm_diff.encode("utf-8")).hexdigest()[:8]
    safe_key = re.sub(r"[^A-Za-z0-9_]", "_", task_key)[:40]
    path = HEALTH_FIXES_DIR / f"{jst_date}_{safe_key}_{h8}.diff"
    path.write_text(norm_diff, encoding="utf-8")
    # PROJECT_ROOT 相対で返す (ログ/通知用、絶対 path = PII 回避)
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return path.name


def propose_fix(
    task_key: str,
    error_message: str,
    *,
    config: Optional[dict] = None,
    timeout: int = CLAUDE_CLI_DEFAULT_TIMEOUT,
    max_budget_usd: float = 0.50,
) -> FixProposal:
    """Tier2 コードバグの修正案をドライランで生成・検証する (commit/適用なし).

    claude (health-fixer, read-only) に diff を出させ、G4→G1→G3→G2 の順で gate 検証。
    全 pass で verdict='proposed' + diff 保存。1 つでも fail で対応する verdict を返す。
    """
    started = time.time()
    prompt = _build_fixer_prompt(task_key, error_message)
    answer, meta = _invoke_fixer_subagent(
        prompt, timeout=timeout, max_budget_usd=max_budget_usd)
    duration_ms = meta.get("duration_ms", int((time.time() - started) * 1000))

    if meta.get("error") or not answer:
        return FixProposal(
            task_key=task_key, verdict="error",
            reason=meta.get("error", "empty answer"), duration_ms=duration_ms)

    # ESCALATE (先頭行が ESCALATE:)
    # この reason は agent 出力そのままで G3 を通らず Discord/DB に流れるため伏字化。
    first_line = answer.lstrip().splitlines()[0] if answer.strip() else ""
    if first_line.strip().upper().startswith("ESCALATE:"):
        return FixProposal(
            task_key=task_key, verdict="escalated",
            reason=_scrub_secrets(first_line.strip())[:300], duration_ms=duration_ms)

    raw_diff = _extract_diff(answer)
    if not raw_diff:
        return FixProposal(
            task_key=task_key, verdict="error",
            reason="diff 抽出不能 (```diff block なし、ESCALATE でもない)",
            duration_ms=duration_ms)

    norm_diff, files = _parse_and_normalize_diff(raw_diff)
    if not files:
        return FixProposal(
            task_key=task_key, verdict="error",
            reason="diff にファイル変更が無い (不正形式)", duration_ms=duration_ms)

    changed_lines = sum(f["added"] + f["removed"] for f in files)
    touched = [f["path"] for f in files]
    gates: dict = {}

    # G4 規模 → escalated
    g4 = _gate_size(files)
    gates["size"] = g4
    if not g4["pass"]:
        return FixProposal(
            task_key=task_key, verdict="escalated", reason=g4["reason"],
            gates=gates, changed_lines=changed_lines, touched_files=touched,
            duration_ms=duration_ms)

    # G1 範囲 → escalated
    g1 = _gate_scope(files)
    gates["scope"] = g1
    if not g1["pass"]:
        return FixProposal(
            task_key=task_key, verdict="escalated", reason=g1["reason"],
            gates=gates, changed_lines=changed_lines, touched_files=touched,
            duration_ms=duration_ms)

    # G3 安全 → gate_failed
    g3 = _gate_safety(answer, files, norm_diff)
    gates["safety"] = g3
    if not g3["pass"]:
        return FixProposal(
            task_key=task_key, verdict="gate_failed", reason=g3["reason"],
            gates=gates, changed_lines=changed_lines, touched_files=touched,
            duration_ms=duration_ms)

    # G2 構文 (a: git apply --check 本番 tree / b: 一時コピー py_compile) → gate_failed
    g2a = _git_apply_check(norm_diff)
    gates["apply_check"] = g2a
    if not g2a["pass"]:
        return FixProposal(
            task_key=task_key, verdict="gate_failed", reason=g2a["reason"],
            gates=gates, changed_lines=changed_lines, touched_files=touched,
            duration_ms=duration_ms)
    g2b = _py_compile_on_copy(norm_diff, files)
    gates["syntax"] = g2b
    if not g2b["pass"]:
        return FixProposal(
            task_key=task_key, verdict="gate_failed", reason=g2b["reason"],
            gates=gates, changed_lines=changed_lines, touched_files=touched,
            duration_ms=duration_ms)

    # 全 pass → proposed + 保存
    diff_path = _save_diff(task_key, norm_diff)
    return FixProposal(
        task_key=task_key, verdict="proposed",
        reason="全 gate pass (規模/範囲/安全/構文)",
        diff=norm_diff, diff_path=diff_path, gates=gates,
        changed_lines=changed_lines, touched_files=touched,
        duration_ms=duration_ms)
