"""W23 Research 脳 中核 — Opus 4.8 ベース相談役エンドポイント.

設計 (Method A): Claude Code subagent + subprocess 呼出
  - `.claude/agents/research-brain.md` を Opus 4.8 subagent として登録
  - Streamlit / 他モジュールから ask() を呼ぶ
  - 内部で `claude -p --agent research-brain --model opus -p "<query>" ...`
  - Max plan で完結 → API 追加課金 $0
  - subprocess env から ANTHROPIC_API_KEY を除外して Max 認証強制

入出力:
  ask(query, source, ...) → ResearchAnswer(answer_md, citations, model_used, ...)
  research_qa テーブルに履歴を記録
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "monitor.db"

CLAUDE_CLI_DEFAULT_TIMEOUT = 120  # seconds. extended thinking で 60s+ 想定


@dataclass
class ResearchAnswer:
    """Research 脳の回答."""
    answer_md: str
    model_used: str
    source: str
    qa_id: int = 0
    citations: list[dict] = field(default_factory=list)
    thinking_md: Optional[str] = None
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: Optional[str] = None
    via: str = "subagent"  # 'subagent' | 'haiku_fallback' | 'error'


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(str(DB_PATH))


def _check_quota(model: str) -> tuple[bool, str]:
    """日次予算チェック. Method A (Max 内) でも一応 call 数を記録して暴走防止."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        row = c.execute(
            "SELECT opus_calls, haiku_calls FROM research_brain_quota WHERE date=?",
            (today,),
        ).fetchone()
        opus = row[0] if row else 0
        haiku = row[1] if row else 0
    if "opus" in model.lower() and opus >= 30:
        return False, f"Opus 日次上限 30 calls 超過 (今日 {opus} 件)"
    if "haiku" in model.lower() and haiku >= 200:
        return False, f"Haiku 日次上限 200 calls 超過 (今日 {haiku} 件)"
    return True, "OK"


def _record_qa(
    source: str,
    query: str,
    model: str,
    answer_md: str,
    citations: list[dict],
    thinking_md: Optional[str],
    duration_ms: int,
    cost_usd: float,
    via: str,
    context_keys: Optional[list[str]] = None,
) -> int:
    """research_qa テーブルに 1 行 INSERT. 戻り値 = qa_id"""
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO research_qa
                 (source, query, context_keys, model, answer_md, citations,
                  thinking_md, duration_ms, cost_usd, via)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                source, query[:5000],
                json.dumps(context_keys or [], ensure_ascii=False),
                model, answer_md, json.dumps(citations or [], ensure_ascii=False),
                thinking_md, duration_ms, cost_usd, via,
            ),
        )
        qa_id = cur.lastrowid
    # quota incrementer (UPSERT)
    today = datetime.now().strftime("%Y-%m-%d")
    is_opus = "opus" in model.lower()
    with _conn() as c:
        c.execute(
            """INSERT INTO research_brain_quota
                 (date, opus_calls, opus_cost_usd, haiku_calls, haiku_cost_usd)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 opus_calls = opus_calls + excluded.opus_calls,
                 opus_cost_usd = opus_cost_usd + excluded.opus_cost_usd,
                 haiku_calls = haiku_calls + excluded.haiku_calls,
                 haiku_cost_usd = haiku_cost_usd + excluded.haiku_cost_usd""",
            (
                today,
                1 if is_opus else 0,
                cost_usd if is_opus else 0.0,
                0 if is_opus else 1,
                0.0 if is_opus else cost_usd,
            ),
        )
    return qa_id


def _build_full_prompt(query: str, hints: Optional[dict] = None) -> str:
    """STABLE + DYNAMIC コンテキストを query と結合."""
    from monitor.research_context import build_stable_context, build_dynamic_context
    stable = build_stable_context()
    dynamic = build_dynamic_context(query, hints=hints)
    return (
        f"{stable}\n\n---\n\n{dynamic}\n\n---\n\n"
        f"# ユーザーからの質問\n\n{query}\n\n"
        f"上記の知識ベース (STABLE) + 関連知識 (DYNAMIC) を踏まえ、"
        f"研究脳ガイドライン (回答ガイドライン構造) に従って日本語で回答してください."
    )


def _invoke_subagent(
    prompt: str,
    model: str,
    timeout: int = CLAUDE_CLI_DEFAULT_TIMEOUT,
    max_budget_usd: float = 0.50,
) -> tuple[str, dict]:
    """claude CLI を subprocess で呼び、subagent 経由で回答を得る (Method A).

    Max plan 認証を強制するため env から ANTHROPIC_API_KEY を除外.
    長大プロンプト (20KB+) は **stdin 経由** で渡す (Windows CLI 引数長制限回避).
    Returns: (answer_text, metadata_dict)
    """
    # Max 認証強制: ANTHROPIC_API_KEY だけ除外、他の env は維持 (Windows Node.js が
    # USERPROFILE / APPDATA / LOCALAPPDATA / NODE_PATH 等を必要とするため).
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDECODE", None)  # 親 Claude Code session の環境フラグ除外
    env.pop("CLAUDE_CODE_SSE_PORT", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_CODE_EXECPATH", None)

    cmd = [
        "claude", "-p",  # -p without arg = stdin から読む
        "--agent", "research-brain",
        "--model", "opus" if "opus" in model.lower() else "haiku",
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-mode", "default",
        "--max-budget-usd", f"{max_budget_usd:.2f}",
    ]

    started = time.time()
    try:
        result = subprocess.run(
            cmd, input=prompt,  # stdin pipe で長大プロンプトも OK
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return "", {
            "error": f"timeout ({timeout}s)", "duration_ms": int((time.time() - started) * 1000),
        }
    except FileNotFoundError:
        return "", {"error": "claude CLI not found in PATH", "duration_ms": 0}

    duration_ms = int((time.time() - started) * 1000)

    if result.returncode != 0:
        # 2026-05-25 強化: 5/19-5/25 で 5 日連続 "claude exit 1: " (stderr 空) が
        # 発生し原因不明だった. stderr だけでなく stdout / 解決 PATH / 実コマンドも
        # 保存して診断材料を増やす. ANTHROPIC_API_KEY を意図的に剥がしている点
        # (Max plan 強制) の影響可視化も目的.
        import shutil
        # WARNING: ここに API key の値 prefix を **絶対に追加しない**.
        # diag は task_execution_log.message → Discord 通知に流れるため漏洩リスク.
        # claude PATH は Windows ユーザー名 (PII) を含むため basename だけに削る.
        resolved = shutil.which("claude")
        claude_basename = Path(resolved).name if resolved else "NOT_FOUND_IN_PATH"
        api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))  # 親 process 側
        diag = (
            f"claude exit {result.returncode} | "
            f"stderr={(result.stderr or '')[:300]!r} | "
            f"stdout={(result.stdout or '')[:300]!r} | "
            f"claude_basename={claude_basename} | "
            f"parent_api_key_set={api_key_present}"
        )
        return "", {"error": diag, "duration_ms": duration_ms}

    # output-format=json should give us a JSON object with result text
    try:
        out = json.loads(result.stdout)
    except json.JSONDecodeError:
        # フォールバック: stdout 全部を回答とみなす
        return result.stdout.strip(), {"duration_ms": duration_ms, "raw": True}

    # Claude Code CLI JSON shape: {"type":"result", "result":"...", "total_cost_usd":..., "modelUsage":{...}}
    answer = (
        out.get("result")
        or out.get("text")
        or out.get("response")
        or out.get("message", {}).get("content")
        or ""
    )
    if isinstance(answer, list):
        # content blocks
        answer = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in answer)

    # cost / token usage (Max 内でも参考値として記録)
    cost_usd = float(out.get("total_cost_usd") or 0.0)
    usage = out.get("usage") or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    cache_r = int(usage.get("cache_read_input_tokens") or 0)
    cache_w = int(usage.get("cache_creation_input_tokens") or 0)

    return str(answer).strip(), {
        "duration_ms": int(out.get("duration_ms") or duration_ms),
        "cost_usd": cost_usd,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_read_tokens": cache_r,
        "cache_write_tokens": cache_w,
        "raw_json": out,
    }


def ask(
    query: str,
    *,
    source: Literal["ui_chat", "morning_brief", "supplier_escalation",
                    "feature_dev", "listing_review", "news_deep_dive",
                    "morning_discovery"] = "ui_chat",
    context_hints: Optional[dict] = None,
    force_model: Literal["opus", "haiku", "auto"] = "auto",
    enable_thinking: bool = False,  # UI 表示は Q3 で「非表示」決定
    save_history: bool = True,
    timeout: int = CLAUDE_CLI_DEFAULT_TIMEOUT,
    max_budget_usd: float = 0.50,
) -> ResearchAnswer:
    """Research 脳への問い合わせ (W23 中核 API).

    Args:
        query: ユーザーの問い (日本語)
        source: 呼出元タグ. router で model 自動決定の判断材料
        context_hints: {sku, ebay_item_id, news_id, video_ids[]} 等
        force_model: 'opus' / 'haiku' / 'auto' (default)
        enable_thinking: 真なら Opus extended thinking. UI 非表示でも DB に保存
        save_history: research_qa に履歴保存
        timeout: claude CLI timeout 秒

    Returns:
        ResearchAnswer
    """
    from monitor.research_router import choose_model
    model, auto_thinking = choose_model(query, source=source, force=force_model)
    use_thinking = enable_thinking or auto_thinking

    # Quota チェック
    ok, reason = _check_quota(model)
    if not ok:
        # フォールバックで Haiku に降格
        if "opus" in model.lower():
            logger.warning(f"Opus quota over: {reason}, fallback to Haiku")
            model = "claude-haiku-4-5-20251001"
            use_thinking = False
            ok, reason = _check_quota(model)
        if not ok:
            err = f"全 quota 超過: {reason}"
            qa_id = (_record_qa(source, query, model, err, [], None, 0, 0.0, "error")
                     if save_history else 0)
            return ResearchAnswer(
                answer_md=err, model_used=model, source=source,
                qa_id=qa_id, error=err, via="error",
            )

    # フルプロンプト構築
    full_prompt = _build_full_prompt(query, hints=context_hints)
    context_keys = []  # TODO: build_dynamic_context が返す video_ids を保存

    # subagent 呼出 (Method A)
    started = time.time()
    answer_text, meta = _invoke_subagent(
        full_prompt, model=model, timeout=timeout,
        max_budget_usd=max_budget_usd,
    )
    duration_ms = meta.get("duration_ms", int((time.time() - started) * 1000))

    if "error" in meta or not answer_text:
        err = meta.get("error", "empty answer")
        logger.error(f"subagent failed: {err}")
        qa_id = (_record_qa(source, query, model, f"[ERROR] {err}", [], None,
                            duration_ms, 0.0, "error")
                 if save_history else 0)
        return ResearchAnswer(
            answer_md=f"研究脳の呼出に失敗しました: {err}",
            model_used=model, source=source, qa_id=qa_id,
            error=err, via="error", duration_ms=duration_ms,
        )

    # citations は答えから簡易抽出 (動画 ID パターン)
    citations = _extract_citations(answer_text)

    # Method A subagent: **Max plan 内で完結**, 実課金 $0.
    # CLI が返す total_cost_usd は「もし API 経由だった場合の理論値」(参考値).
    # 2026-04-26 検証済: Console 表示と CLI 報告値が乖離、CLI 値は不採用.
    # research_qa.cost_usd には 0.0 を保存し、運用 visibility のため別カラム
    # (将来追加) で参考値を分離する案あり.
    cost_usd = 0.0  # Max 内で実課金されないため

    qa_id = (_record_qa(source, query, model, answer_text, citations, None,
                        duration_ms, cost_usd, "subagent", context_keys)
             if save_history else 0)

    return ResearchAnswer(
        answer_md=answer_text,
        model_used=model,
        source=source,
        qa_id=qa_id,
        citations=citations,
        thinking_md=None,  # Method A では取得不能
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        via="subagent",
    )


def _extract_citations(answer: str) -> list[dict]:
    """回答テキストから citation を簡易抽出. 動画 ID パターン + KB ファイル参照."""
    cites: list[dict] = []
    # 動画 ID (YouTube 11 桁 or x_<digits>)
    for m in re.finditer(r"\b([a-zA-Z0-9_-]{11}|x_\d+)\b", answer):
        vid = m.group(1)
        if vid not in [c.get("id") for c in cites]:
            cites.append({"type": "video", "id": vid})
    # feedback memory ファイル
    for m in re.finditer(r"feedback_[a-z_]+\.md", answer):
        f = m.group(0)
        if f not in [c.get("file") for c in cites]:
            cites.append({"type": "memory", "file": f})
    return cites[:20]


def get_recent_qa(source: Optional[str] = None, limit: int = 20) -> list[dict]:
    """UI 表示用. research_qa 直近 N 件."""
    sql = "SELECT id, asked_at, source, query, model, answer_md, cost_usd, duration_ms, user_rating FROM research_qa"
    params: list = []
    if source:
        sql += " WHERE source=?"
        params.append(source)
    sql += " ORDER BY asked_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def rate_qa(qa_id: int, rating: int, action_taken: bool = False) -> None:
    """W26 評価ループ用. user の 1-5 星 rating + action_taken を記録."""
    if not 1 <= rating <= 5:
        raise ValueError(f"rating must be 1-5, got {rating}")
    with _conn() as c:
        if action_taken:
            c.execute(
                "UPDATE research_qa SET user_rating=?, user_action_at=CURRENT_TIMESTAMP WHERE id=?",
                (rating, qa_id),
            )
        else:
            c.execute(
                "UPDATE research_qa SET user_rating=? WHERE id=?",
                (rating, qa_id),
            )


# CLI 動作確認用
if __name__ == "__main__":
    import sys
    # cp932 → UTF-8 (Windows 日本語回答用、Q0/feedback_no_silent_skip 違反防止)
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python -m monitor.research_brain <query>")
        sys.exit(1)
    q = " ".join(sys.argv[1:])
    print(f"=== Research 脳 への問い ===")
    print(f"Q: {q}\n")
    ans = ask(q, source="ui_chat")
    print(f"=== 回答 (model={ans.model_used} via={ans.via} {ans.duration_ms}ms) ===")
    print(ans.answer_md)
    if ans.citations:
        print(f"\n=== 引用 ({len(ans.citations)} 件) ===")
        for c in ans.citations:
            print(f"  - {c}")
    if ans.error:
        print(f"\n=== エラー ===\n{ans.error}")
