"""W23 Research 脳 中核 — Opus 4.8 ベース相談役エンドポイント.

設計 (Method B / 2026-06-11 切替): Anthropic API 直接呼出
  - 過去 (〜2026-06-11 Method A): `claude -p --agent research-brain` subprocess +
    env から ANTHROPIC_API_KEY を除外して Max plan 認証強制 (実課金 $0)
  - 変更理由: 2026-06-15 Claude 課金改定で `claude -p` / サブスク認証の
    headless 利用が別枠クレジット制になるため (reference_claude_code_billing_change_2026_06_15.md)
  - 現状 (Method B): anthropic SDK で messages.create を直接呼ぶ。
    system prompt は `.claude/agents/research-brain.md` 本文 (frontmatter 除去) を流用。
    モデルは Opus 4.8 のまま (Fable 5 は API 使用禁止 = user 指示 2026-06-10)。
    実課金が発生するため usage から実コストを research_qa.cost_usd に記録。
  - 機能差分: CLI subagent が持っていた Read/Glob/Grep/WebSearch tool は API 直呼びでは
    使えない。STABLE+DYNAMIC コンテキストをプロンプトに埋め込む既存設計で代替。

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
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import anthropic
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "monitor.db"

# ebay-manager root の .env を明示ロード (claude_evaluator と同パターン)
_ENV_PATH = PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

CLAUDE_CLI_DEFAULT_TIMEOUT = 120  # seconds. extended thinking で 60s+ 想定

# repo root の subagent 定義 (system prompt として流用)
AGENT_MD_PATH = PROJECT_ROOT.parent.parent / ".claude" / "agents" / "research-brain.md"

# USD per 1M tokens (input, output)。出典: feedback_opus_price_watch.md (Opus $5/$25)
_PRICING = {
    "opus": (5.0, 25.0),
    "haiku": (1.0, 5.0),
}


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
    via: str = "api"  # 'api' (2026-06-11〜) | 'subagent' (旧 Method A 履歴) | 'error'


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


_agent_system_prompt_cache: Optional[str] = None


def _load_agent_system_prompt() -> str:
    """`.claude/agents/research-brain.md` 本文を system prompt として読む.

    YAML frontmatter (--- ... ---) は CLI subagent 登録用メタデータなので除去。
    読めない場合も Q0 silent skip せず最小限の役割宣言で続行する。
    """
    global _agent_system_prompt_cache
    if _agent_system_prompt_cache is not None:
        return _agent_system_prompt_cache
    try:
        text = AGENT_MD_PATH.read_text(encoding="utf-8")
        m = re.match(r"\A---\n.*?\n---\n", text, flags=re.DOTALL)
        if m:
            text = text[m.end():]
        _agent_system_prompt_cache = text.strip()
    except OSError as e:
        logger.warning(f"research-brain.md 読込失敗 ({e}) — 最小 system prompt で続行")
        _agent_system_prompt_cache = (
            "あなたは MonoHonpo (eBay 越境EC セラー) の Research 脳です。"
            "必ず日本語で、核心→根拠→適用案 の構造で回答してください。"
        )
    return _agent_system_prompt_cache


def _estimate_cost_usd(model: str, in_tok: int, out_tok: int) -> float:
    """usage から実コスト概算 (USD)。pricing は _PRICING 参照."""
    key = "opus" if "opus" in model.lower() else "haiku"
    price_in, price_out = _PRICING[key]
    return (in_tok * price_in + out_tok * price_out) / 1_000_000


def _invoke_api(
    prompt: str,
    model: str,
    timeout: int = CLAUDE_CLI_DEFAULT_TIMEOUT,
    max_budget_usd: float = 0.50,
    use_thinking: bool = False,
) -> tuple[str, dict]:
    """Anthropic API を直接呼び回答を得る (Method B / 2026-06-11〜).

    model は choose_model が返すフル model ID (claude-opus-4-8 等) をそのまま使う。
    use_thinking=True なら extended thinking 有効 (thinking block は meta["thinking_md"])。
    Returns: (answer_text, metadata_dict)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "", {"error": "ANTHROPIC_API_KEY 未設定 (.env 確認)", "duration_ms": 0}

    # max_retries=1 明示: SDK デフォルト 2 だと timeout×3 試行で wall time が読めない
    client = anthropic.Anthropic(api_key=api_key, timeout=float(timeout), max_retries=1)
    kwargs: dict = {"max_tokens": 8000}
    if use_thinking:
        # thinking tokens は max_tokens に内数 → 回答分を確保するため引上げ。
        # Opus 4.8 は adaptive thinking のみ対応 ("enabled"+budget_tokens は 400 エラー、
        # 2026-06-11 実機確認)。思考量はモデルが質問の複雑さに応じて自動配分する。
        kwargs["max_tokens"] = 12000
        kwargs["thinking"] = {"type": "adaptive"}
    started = time.time()
    try:
        resp = client.messages.create(
            model=model,
            system=_load_agent_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
    except anthropic.APIError as e:
        # WARNING: error 文字列は task_execution_log → Discord に流れる経路あり。
        # API key の値を **絶対に含めない** (型名 + message のみ)。
        return "", {
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "duration_ms": int((time.time() - started) * 1000),
        }

    duration_ms = int((time.time() - started) * 1000)
    answer = "".join(b.text for b in resp.content if b.type == "text")
    thinking_md = "".join(
        b.thinking for b in resp.content if b.type == "thinking") or None

    # 出力上限到達 = 回答が途中で切れている (silent truncation 防止 / Q0)
    truncated = resp.stop_reason == "max_tokens"
    if truncated:
        logger.warning(
            f"research_brain 回答が max_tokens={kwargs['max_tokens']} で途中打切り "
            f"(model={model})。回答は不完全な可能性")
        answer += "\n\n---\n[警告] 回答が出力上限で途中打切りされています。"

    in_tok = int(resp.usage.input_tokens or 0)
    out_tok = int(resp.usage.output_tokens or 0)
    cost_usd = _estimate_cost_usd(model, in_tok, out_tok)
    if cost_usd > max_budget_usd:
        logger.warning(
            f"research_brain 1 call が予算超過: ${cost_usd:.3f} > ${max_budget_usd:.2f} "
            f"(in={in_tok} out={out_tok} tok)")

    return answer.strip(), {
        "duration_ms": duration_ms,
        "cost_usd": cost_usd,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "thinking_md": thinking_md,
        "truncated": truncated,
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
        timeout: Anthropic API timeout 秒

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

    # API 直接呼出 (Method B / 2026-06-11〜)
    started = time.time()
    answer_text, meta = _invoke_api(
        full_prompt, model=model, timeout=timeout,
        max_budget_usd=max_budget_usd, use_thinking=use_thinking,
    )
    duration_ms = meta.get("duration_ms", int((time.time() - started) * 1000))

    if "error" in meta or not answer_text:
        err = meta.get("error", "empty answer")
        logger.error(f"research_brain API call failed: {err}")
        # 空回答でも API 呼出が成功していれば課金は発生済 → 実コストを記録
        qa_id = (_record_qa(source, query, model, f"[ERROR] {err}", [],
                            meta.get("thinking_md"),
                            duration_ms, float(meta.get("cost_usd") or 0.0), "error")
                 if save_history else 0)
        return ResearchAnswer(
            answer_md=f"研究脳の呼出に失敗しました: {err}",
            model_used=model, source=source, qa_id=qa_id,
            error=err, via="error", duration_ms=duration_ms,
        )

    # citations は答えから簡易抽出 (動画 ID パターン)
    citations = _extract_citations(answer_text)

    # Method B (2026-06-11〜): API 直呼びは **実課金** が発生する。
    # usage から概算した実コストを記録 (過去 Method A は Max 内 $0 固定だった)。
    # research_brain_quota の opus_cost_usd / haiku_cost_usd が実金額の監視窓になる。
    cost_usd = float(meta.get("cost_usd") or 0.0)
    thinking_md = meta.get("thinking_md")

    qa_id = (_record_qa(source, query, model, answer_text, citations, thinking_md,
                        duration_ms, cost_usd, "api", context_keys)
             if save_history else 0)

    return ResearchAnswer(
        answer_md=answer_text,
        model_used=model,
        source=source,
        qa_id=qa_id,
        citations=citations,
        thinking_md=thinking_md,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        via="api",
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
