#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sakana Fugu 外部レビュー/助言ヘルパ (OpenAI 互換 API)。

責務:
  - コード変更 / 文書 / 業務質問を Sakana Fugu (orchestration model) に渡し、
    Claude lineage とは独立した第 3 視点の講評を得る。
  - 出力はそのまま採用せず、呼び出し元 (fugu-reviewer agent) が 2 段ループで
    再評価する前提 (codex-reviewer と同じ思想)。

設計:
  - Fugu は OpenAI 互換 (`https://api.sakana.ai/v1`、model=`fugu-ultra`/`fugu`)。
    既存 openai SDK に base_url を差すだけで叩ける (新依存なし)。
  - API キーは `.env` の FUGU_API_KEY を env 経由で読む (security rule、ハードコード禁止)。
  - Fugu Ultra は遅い (Sakana 公式が timeout 延長を案内) ため timeout=600s。
  - Q0: キー欠落 / API エラーは黙って成功扱いにせず、明示的に例外を上げる。

使い方 (CLI):
    python -m monitor.fugu_review --mode code --diff-from-git
    python -m monitor.fugu_review --mode doc --files a.md b.md
    python -m monitor.fugu_review --mode advisory --question "..."

使い方 (import):
    from monitor.fugu_review import run_fugu_review
    r = run_fugu_review(mode="code", diff=my_diff_text)
    print(r.text)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ebay-manager root の .env を明示ロード (CWD 非依存、claude_evaluator.py と同方針)。
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

FUGU_BASE_URL = "https://api.sakana.ai/v1"
DEFAULT_MODEL = "fugu-ultra"  # レビュー品質優先。速度優先なら "fugu"。
_TIMEOUT_SEC = 600  # Fugu Ultra は多段編成で遅い (Sakana 公式が延長を推奨)。

# モード別 system prompt。業務文脈 (eBay 越境 EC・money-direct) を与えて的を絞る。
_SYSTEM_PROMPTS: dict[str, str] = {
    "code": (
        "You are a senior code reviewer for a money-critical eBay cross-border "
        "e-commerce automation system (Python / SQLite / Streamlit). Review the "
        "provided diff or code for: correctness bugs, logic errors, security "
        "issues, and money-direct risks (shipping cost, price, SKU handling, DB "
        "writes). Report ONLY high-confidence findings, each with a file:line "
        "reference, a severity (HIGH/MED/LOW), and a concrete fix. Be terse. If "
        "you find nothing high-confidence, say so explicitly."
    ),
    "doc": (
        "You are a documentation linter for an LLM knowledge base (memory files, "
        "KB topics, design docs, .claude rules, CLAUDE.md). Detect: internal "
        "factual contradictions, outdated or relative date claims, missing source "
        "citations, broken [[wikilinks]], and cascade inconsistencies where the "
        "same fact differs across files. Report concrete findings with file:line. "
        "Be terse. Do not invent issues; cite the exact text you flag."
    ),
    "advisory": (
        "You are a senior business and systems advisor for an eBay cross-border "
        "commerce business shipping from Japan. Consider tariffs (DDP, US Section "
        "232 derivative duties), international shipping economics, listing "
        "strategy, account-health/defect risk, and implementation trade-offs. "
        "Give an honest, decisive recommendation with reasoning. Flag assumptions "
        "and risks explicitly. Be concrete, not generic."
    ),
}


@dataclass
class FuguReview:
    """Fugu レビュー結果。text を呼び出し元が 2 段ループで再評価する。"""

    mode: str
    model: str
    text: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]


class FuguReviewError(RuntimeError):
    """キー欠落 / API エラー / 入力不備 (Q0: 黙って成功にしない)。"""


def _get_api_key() -> str:
    key = os.environ.get("FUGU_API_KEY")
    if not key:
        raise FuguReviewError(
            f"FUGU_API_KEY が未設定です。{_ENV_PATH} に "
            "FUGU_API_KEY=<console.sakana.ai で取得したキー> を追記してください。"
        )
    return key


def _git_run(args: list[str], root: Path) -> subprocess.CompletedProcess:
    """git をローカル文字化け耐性つきで実行 (errors=replace で UnicodeDecodeError 回避)。"""
    return subprocess.run(
        args, cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


# --diff-from-git の対象を code dir (tools/ebay-manager) に限定し、業務/秘匿パスを除外。
# 3者レビュー合議 HIGH: Codex が「root=repo 全体で .company/ 業務データ・untracked
# 機密まで外部 Sakana API へ egress」を構造的に指摘。pathspec の :(exclude) で防ぐ。
_DIFF_SCOPE = "tools/ebay-manager"
_DIFF_PATHSPEC: list[str] = [
    _DIFF_SCOPE,
    ":(exclude)tools/ebay-manager/data",
    ":(exclude)*.env*",
    ":(exclude)*secret*",
    ":(exclude)*token*",
    ":(exclude)*credential*",
    ":(exclude)*.pid",
]
_SECRET_SUBSTR: tuple[str, ...] = (".env", "secret", "token", "credential", ".pid")


def _is_egress_denied(rel: str) -> bool:
    """外部 API へ送ってはいけないパスか (code dir 外・data・秘匿名)。"""
    low = rel.replace("\\", "/").lower()
    if not low.startswith(_DIFF_SCOPE + "/"):
        return True  # tools/ebay-manager 外 (.company/ data/ 等) は送らない
    if low.startswith(_DIFF_SCOPE + "/data/"):
        return True
    return any(s in low for s in _SECRET_SUBSTR)


def _git_diff() -> str:
    """tracked 変更 + 新規ファイルを取得 (mode=code/doc 用)。code dir に scope。

    Q0: git 失敗を黙って空に落とさず FuguReviewError。skip は stderr に痕跡化。
    旧 HEAD~1 silent fallback は false coverage を生むため撤廃 (fugu-reviewer MED-1)。
    untracked も含めるが (取りこぼし防止 MED-2)、外部 egress 対策で tools/ebay-manager
    に scope + 業務/秘匿パス deny (3者合議 HIGH、Codex の repo 全体 egress 指摘)。
    """
    root = Path(__file__).resolve().parent.parent.parent.parent  # repo root
    tracked = _git_run(["git", "diff", "HEAD", "--", *_DIFF_PATHSPEC], root)
    if tracked.returncode != 0:
        raise FuguReviewError(f"git diff 失敗: {tracked.stderr.strip()[:200]}")
    parts: list[str] = []
    if tracked.stdout.strip():
        parts.append(tracked.stdout)
    untracked = _git_run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", _DIFF_SCOPE], root)
    if untracked.returncode != 0:  # Q0: git 失敗を握り潰さない (codex finding 2)
        raise FuguReviewError(f"git ls-files 失敗: {untracked.stderr.strip()[:200]}")
    skipped: list[str] = []
    for rel in untracked.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        if _is_egress_denied(rel):
            skipped.append(rel)
            continue
        target = root / rel
        if not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:  # 痕跡を残す (codex finding 3、silent skip 禁止)
            skipped.append(f"{rel}(読込失敗 {type(e).__name__})")
            continue
        parts.append(f"===== NEW FILE: {rel} =====\n{content}")
    if skipped:
        head = ", ".join(skipped[:8]) + ("..." if len(skipped) > 8 else "")
        print(f"[fugu_review] 外部送信から除外 {len(skipped)} 件: {head}", file=sys.stderr)
    return "\n".join(parts)


def _read_files(paths: list[str]) -> str:
    chunks: list[str] = []
    for p in paths:
        path = Path(p)
        if not path.is_file():  # ディレクトリ/不在を FuguReviewError 化 (型違い例外の escape 防止)
            raise FuguReviewError(f"対象ファイルが存在しないかディレクトリです: {p}")
        chunks.append(f"===== FILE: {p} =====\n{path.read_text(encoding='utf-8', errors='replace')}")
    return "\n\n".join(chunks)


def run_fugu_review(
    mode: str,
    *,
    diff: Optional[str] = None,
    files: Optional[list[str]] = None,
    question: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> FuguReview:
    """Fugu に1リクエスト送り、講評テキストを返す。

    Args:
        mode: "code" / "doc" / "advisory"。
        diff: コード差分テキスト (mode=code)。
        files: 対象ファイルパス (mode=code/doc)。
        question: 助言を求める質問 (mode=advisory)。
        model: "fugu-ultra" (既定) / "fugu"。

    Raises:
        FuguReviewError: モード不正 / 入力欠落 / キー欠落 / API 失敗。
    """
    if mode not in _SYSTEM_PROMPTS:
        raise FuguReviewError(f"未知の mode: {mode!r} (有効: {sorted(_SYSTEM_PROMPTS)})")

    # 入力本文を組み立て (モード別)。
    if mode == "advisory":
        if not question:
            raise FuguReviewError("mode=advisory には --question が必要です。")
        user_content = question
    else:
        parts: list[str] = []
        if diff:
            parts.append(f"=== DIFF ===\n{diff}")
        if files:
            parts.append(_read_files(files))
        if not parts:
            raise FuguReviewError(f"mode={mode} には --diff-from-git か --files が必要です。")
        user_content = "\n\n".join(parts)

    # openai SDK に Fugu の base_url を差して叩く (OpenAI 互換 chat completions)。
    try:
        from openai import OpenAI  # 遅延 import (未導入環境を ImportError で素通りさせない)
    except ImportError as e:  # Q0: 非構造化失敗 (生 traceback) を防ぎ明示的に上げる
        raise FuguReviewError(f"openai パッケージ未導入: pip install openai ({e})") from e

    client = OpenAI(api_key=_get_api_key(), base_url=FUGU_BASE_URL, timeout=_TIMEOUT_SEC)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPTS[mode]},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as e:  # openai は多様な例外型を投げる → 集約して明示的に上げる
        raise FuguReviewError(f"Fugu API 呼び出し失敗: {type(e).__name__}: {e}") from e

    if not resp.choices:
        raise FuguReviewError("Fugu 応答に choices がありません (空応答)。")
    text = resp.choices[0].message.content or ""
    if not text.strip():
        raise FuguReviewError("Fugu 応答が空文字です。")

    usage = getattr(resp, "usage", None)
    return FuguReview(
        mode=mode,
        model=model,
        text=text,
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
    )


def _main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Sakana Fugu 外部レビュー/助言")
    ap.add_argument("--mode", required=True, choices=sorted(_SYSTEM_PROMPTS))
    ap.add_argument("--diff-from-git", action="store_true", help="git diff HEAD を対象に")
    ap.add_argument("--files", nargs="*", help="対象ファイルパス")
    ap.add_argument("--question", help="mode=advisory の質問")
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=["fugu-ultra", "fugu"])
    args = ap.parse_args(argv)

    # git diff は code/doc モードのみで取得 (advisory で無駄な git 実行・git エラー面を作らない)。
    diff = None
    if args.diff_from_git:
        if args.mode == "advisory":
            print("[FUGU ERROR] --diff-from-git は mode=code/doc 専用です。", file=sys.stderr)
            return 2
        try:
            diff = _git_diff()
        except FuguReviewError as e:
            print(f"[FUGU ERROR] {e}", file=sys.stderr)
            return 2
        if not (diff or "").strip():
            print("[FUGU ERROR] レビュー対象の変更がありません (git diff 空)。", file=sys.stderr)
            return 2

    try:
        r = run_fugu_review(
            args.mode,
            diff=diff,
            files=args.files,
            question=args.question,
            model=args.model,
        )
    except FuguReviewError as e:
        print(f"[FUGU ERROR] {e}", file=sys.stderr)
        return 2

    print(f"=== Fugu Review (mode={r.mode}, model={r.model}, "
          f"tokens in={r.prompt_tokens}/out={r.completion_tokens}) ===\n")
    print(r.text)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
