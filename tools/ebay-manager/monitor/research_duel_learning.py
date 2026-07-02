#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""リサーチ対戦アリーナ (W286) 完了 → 深層学習トリガー (Phase 1).

設計書: .company/engineering/docs/2026-06-27-research-duel-arena-system-design-v2.md §6 / §0-§1

役割:
  オーナーが AI 5品を 0-100 採点し終えた round (status=user_done) を入力に、
  **Opus 4.8 で深い思考** を 1 回回し、
    (a) 総括 (低得点の構造的理由 + オーナー選定理由の一般化可能ルール) を auto-memory に、
    (b) ルーブリック「候補」(rule/scope/由来round/support_count=1/status=candidate) を
        単一ファイル reference_research_rubric.md に追記する。
  最後に round を completed へ前進させる。

厳守する制約 (Codex/Fugu 査読確定、違反 = 品質事故):
  - ❌ 採点を claude_evaluator (evaluate_match / _build_past_judgments_block) へ配線しない。
       本番の仕入先同一性判定 = money-direct を汚染するため。Phase 1 は MD/memory への記録のみ。
  - ❌ multi-file cascade を呼ばない (単一 rubric ファイル)。候補は user 承認前提で自動昇格しない。
  - ✅ Q7 read-first: 生成前に既存 reference_research_rubric.md を読み、重複/矛盾を避ける
       (矛盾は出力に明記)。
  - ✅ Q0: Opus/API 失敗時は success=False + 明確な reason (偽装成功禁止)。
       round をクラッシュさせず、status も動かさない (再実行可能に残す)。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import research_brain
from . import research_duel_db

logger = logging.getLogger(__name__)

def _default_memory_dir() -> Path:
    """~/.claude/projects/<project-slug>/memory を環境非依存に解決する (設計書 / タスク指定).

    Claude Code の SessionStart hook が使う slug 規則 (repo root の ':' '\\' '/' を
    '-' に置換、`.claude/hooks/session-start-load-incantation.sh` L17 / test 済:
    `tests/test_session_start_hook_slug.py`) を repo root から再現し、Path.home() と
    組み合わせて絶対パスを導出する。ハードコードされたユーザ名/repo path に依存しない
    (総点検 LOW-b)。挙動は従来のハードコード値と同一パスに解決される。
    """
    # __file__ = <repo root>/tools/ebay-manager/monitor/research_duel_learning.py
    #   parents[0]=monitor / [1]=ebay-manager / [2]=tools / [3]=<repo root>
    repo_root = Path(__file__).resolve().parents[3]
    slug = str(repo_root).replace(":", "-").replace("\\", "-").replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / "memory"


# auto-memory の物理ディレクトリ (設計書 / タスク指定)。
MEMORY_DIR = _default_memory_dir()
RUBRIC_PATH = MEMORY_DIR / "reference_research_rubric.md"

# research_brain.ask の source タグ (research_qa.source は自由テキスト = 制約なし)。
LEARNING_SOURCE = "duel_learning"

# Opus 深思考の予算 (1 round の総括 + 候補抽出は時間を要する想定)。
_ASK_TIMEOUT_SEC = 240
_ASK_BUDGET_USD = 1.50


# ============================================================================
# 公開 API
# ============================================================================

def run_completion_learning(round_id: int) -> dict:
    """user_done の round を深層学習に通し、memory 2 ファイルへ記録 → completed 化.

    Returns dict:
        success: bool
        reason: str (success=False 時の理由 / success=True 時は短い要約)
        round_id, jst_date
        summary_path: 総括 feedback md の path (str | None)
        rubric_path: ルーブリック md の path (str | None)
        rubric_candidates: 追記した候補数 (int)
        qa_id: research_qa の記録 id (int | None)
        completed: round を completed に遷移できたか (bool)
    """
    result: dict[str, Any] = {
        "success": False,
        "reason": "",
        "round_id": round_id,
        "jst_date": None,
        "summary_path": None,
        "rubric_path": None,
        "rubric_candidates": 0,
        "qa_id": None,
        "completed": False,
    }

    # ---- 1. round + picks 読込 + ガード ------------------------------------
    rnd = research_duel_db.get_round(round_id)
    if not rnd:
        result["reason"] = f"round_id={round_id} not found"
        logger.warning("[duel_learning] %s", result["reason"])
        return result
    result["jst_date"] = rnd.get("jst_date")
    status = rnd.get("status")

    if status == research_duel_db.STATUS_COMPLETED:
        # 冪等: 既に学習済。再走でクラッシュさせず no-op 成功扱い (memory 二重生成しない)。
        result["success"] = True
        result["completed"] = True
        result["reason"] = "already completed (no-op)"
        logger.info("[duel_learning] round_id=%s already completed — skip", round_id)
        return result
    if status != research_duel_db.STATUS_USER_DONE:
        # user_done 以外 (ai_pending/ai_done/invalidated) は採点が揃っていない = 学習不可。
        result["reason"] = (
            f"status={status!r} (expected {research_duel_db.STATUS_USER_DONE!r}); "
            "採点完了前 or 無効化済のため学習しない"
        )
        logger.warning("[duel_learning] round_id=%s %s", round_id, result["reason"])
        return result

    picks = research_duel_db.get_round_picks(round_id)
    ai_picks = picks.get("ai", [])
    user_picks = picks.get("user", [])
    scored = [p for p in ai_picks if p.get("user_score") is not None]
    if not scored:
        result["reason"] = "採点済の AI pick が 0 件 (user_score 全 NULL) — 学習 signal 無し"
        logger.warning("[duel_learning] round_id=%s %s", round_id, result["reason"])
        return result

    # ---- 2. Q7 read-first: 既存ルーブリックを読む ---------------------------
    existing_rubric = _read_existing_rubric()

    # ---- 3. Opus 4.8 で深層学習 -------------------------------------------
    prompt = _build_learning_prompt(rnd, ai_picks, user_picks, existing_rubric)
    try:
        ans = research_brain.ask(
            prompt,
            source=LEARNING_SOURCE,  # type: ignore[arg-type]  # research_qa.source は自由テキスト
            force_model="opus",       # 深い思考を保証 (router 任せにしない)
            enable_thinking=True,
            save_history=True,
            timeout=_ASK_TIMEOUT_SEC,
            max_budget_usd=_ASK_BUDGET_USD,
        )
    except Exception as e:  # noqa: BLE001 — round をクラッシュさせない (Q0)
        result["reason"] = f"research_brain.ask raised {type(e).__name__}: {str(e)[:300]}"
        logger.error("[duel_learning] round_id=%s %s", round_id, result["reason"])
        return result

    result["qa_id"] = ans.qa_id or None
    # Q0: API/quota 失敗を偽装成功にしない。via=='error' or error あり or 空回答 = 失敗。
    if ans.error or ans.via == "error" or not (ans.answer_md and ans.answer_md.strip()):
        result["reason"] = (
            f"Opus 呼出失敗 (via={ans.via}): {ans.error or 'empty answer'} — "
            "memory 未記録・round は user_done のまま (再実行可能)"
        )
        logger.error("[duel_learning] round_id=%s %s", round_id, result["reason"])
        return result

    answer_md = ans.answer_md.strip()
    summary_md, candidates = _parse_learning_output(answer_md)

    # ---- 4. 書き込み: 総括 → feedback md / 候補 → rubric md ----------------
    try:
        summary_path = _write_summary_memory(rnd, summary_md, candidates, ans)
        result["summary_path"] = str(summary_path)
        result["summary_md"] = summary_md  # HIGH-2 fix: tab が _res.get("summary_md") で読む
    except OSError as e:
        result["reason"] = f"総括 memory 書込失敗: {type(e).__name__}: {e}"
        logger.error("[duel_learning] round_id=%s %s", round_id, result["reason"])
        return result

    try:
        n_appended = _append_rubric_candidates(rnd, candidates, existing_rubric)
        result["rubric_candidates"] = n_appended
        result["rubric_path"] = str(RUBRIC_PATH)
    except OSError as e:
        # 総括は書けたが rubric が書けなかった = 部分成功。round は前進させず正直に報告 (Q0)。
        result["reason"] = (
            f"総括は記録済だが rubric 書込失敗: {type(e).__name__}: {e} "
            f"(summary={result['summary_path']})"
        )
        logger.error("[duel_learning] round_id=%s %s", round_id, result["reason"])
        return result

    # ---- 5. round を completed へ前進 --------------------------------------
    try:
        completed = research_duel_db.update_round_status(
            round_id, research_duel_db.STATUS_COMPLETED
        )
        result["completed"] = bool(completed)
    except ValueError as e:
        # 並行で invalidated 等になった場合。memory は記録済なので success は維持しつつ警告。
        logger.warning(
            "[duel_learning] round_id=%s completed 遷移不可: %s (memory は記録済)",
            round_id, e,
        )
        result["completed"] = False

    result["success"] = True
    result["reason"] = (
        f"学習完了: 総括 1 件 + ルーブリック候補 {result['rubric_candidates']} 件記録 "
        f"(completed={result['completed']})"
    )
    logger.info("[duel_learning] round_id=%s %s", round_id, result["reason"])
    return result


# ============================================================================
# プロンプト構築
# ============================================================================

def _fmt_reject_tags(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        tags = json.loads(raw)
        if isinstance(tags, list) and tags:
            return ", ".join(str(t) for t in tags)
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


def _build_learning_prompt(
    rnd: dict,
    ai_picks: list[dict],
    user_picks: list[dict],
    existing_rubric: str,
) -> str:
    """Opus への深層学習プロンプトを組む.

    AI 採点 (低得点の構造的理由) + ユーザーの「なぜ」(一般化可能ルール) を渡し、
    総括 + ルーブリック候補 (JSON) を出させる。Q7: 既存ルーブリックを渡し重複/矛盾回避。
    """
    cat = rnd.get("category_label") or (
        f"category_id={rnd.get('category_id')}" if rnd.get("category_id") is not None
        else "カテゴリ指定なし"
    )
    header = (
        f"# リサーチ対戦アリーナ 完了ラウンドの深層学習 (round_id={rnd.get('round_id')})\n\n"
        f"- 日付 (JST): {rnd.get('jst_date')}\n"
        f"- パターン: {rnd.get('pattern')}  / カテゴリ: {cat}\n"
        f"- prompt_version: {rnd.get('prompt_version') or '(未記録)'}\n\n"
        "この round では AI がリサーチで仕入候補 5 品を選び、オーナー (eBay 越境EC のプロ) が "
        "それぞれを **0-100 で採点 + 理由** を付けた。さらにオーナー自身も「自分ならこれを選ぶ」"
        "という 1〜5 品とその理由 (なぜ) を提出している。\n"
    )

    # --- AI picks (採点 + 理由 + 減点タグ) ---
    ai_lines = ["## AI が選んだ 5 品 (オーナー採点)\n"]
    for p in sorted(ai_picks, key=lambda x: (x.get("rank") or 99)):
        score = p.get("user_score")
        score_s = "未採点" if score is None else f"{score}/100"
        fb = (p.get("user_fb_md") or "").strip() or "(理由なし)"
        tags = _fmt_reject_tags(p.get("reject_tags_json"))
        tags_s = f"  / 減点タグ: {tags}" if tags else ""
        title = (p.get("title_ja") or "(無題)").strip()
        ai_lines.append(
            f"- [rank {p.get('rank')}] {title}\n"
            f"    採点: {score_s}{tags_s}\n"
            f"    オーナーの評価理由: {fb}"
        )
    ai_block = "\n".join(ai_lines)

    # --- user picks (なぜ) ---
    if user_picks:
        u_lines = ["## オーナー自身が選んだ品 (なぜ選んだか)\n"]
        for p in sorted(user_picks, key=lambda x: (x.get("rank") or 99)):
            title = (p.get("title_ja") or "(無題)").strip()
            why = (p.get("why_md") or "").strip() or "(理由なし)"
            profit = p.get("profit_jpy_user")
            profit_s = f"  / オーナー利益見積: ¥{profit:,}" if isinstance(profit, int) else ""
            u_lines.append(f"- [rank {p.get('rank')}] {title}{profit_s}\n    なぜ: {why}")
        user_block = "\n".join(u_lines)
    else:
        user_block = "## オーナー自身が選んだ品\n\n(この round では未提出)"

    # --- Q7: 既存ルーブリック ---
    if existing_rubric.strip():
        rubric_block = (
            "## 既存のルーブリック候補 (重複・矛盾を避けるため必読)\n\n"
            "以下は過去 round で既に抽出済の候補。**同じ趣旨のルールを重複追加しない**。\n"
            "既存と矛盾する知見が今回出た場合は、新ルールの `note` に「既存ルール『〜』と矛盾。"
            "理由: 〜」と必ず明記すること。\n\n"
            f"```\n{existing_rubric.strip()[:6000]}\n```"
        )
    else:
        rubric_block = (
            "## 既存のルーブリック候補\n\n(まだ 1 件もない。今回が初回抽出。)"
        )

    # --- 指示 ---
    instructions = (
        "## あなたへの依頼\n\n"
        "上記を深く分析し、次の 2 つを生成してください。これは **オーナーの採点から AI のリサーチ眼を"
        "育てるためのコア学習** です。表層的な要約ではなく、構造を抽出してください。\n\n"
        "### (a) 総括 (Markdown 散文)\n"
        "- **低得点 (特に 60 未満 / 0 点) の AI pick に共通する構造的理由** は何か "
        "(例: 同一性判定が甘い・利益が薄い・VeRO/ブランドリスク・カテゴリ不適・送料/関税で赤字化 等)。\n"
        "- **オーナーの選定理由 (なぜ) に潜む、一般化可能な判断ルール** は何か "
        "(オーナーが暗黙に使っている『良い仕入候補』の条件)。\n"
        "- AI のリサーチがオーナーに近づくために次回意識すべき点。\n\n"
        "### (b) ルーブリック候補 (機械可読 JSON)\n"
        "総括から、再利用可能な判断ルールを **候補** として抽出する。各ルールは:\n"
        "- `rule`: ルール文 (1〜2 文、AI が次回リサーチ時に self-check できる具体的な文)\n"
        "- `scope`: `general` (全カテゴリ共通) / `owner` (このオーナー固有の好み) / "
        "`category` (このカテゴリ固有) のいずれか\n"
        "- `note`: 由来の補足 (任意。既存ルールとの矛盾はここに明記)\n\n"
        "**出力形式 (厳守)**: まず (a) の総括を散文で書き、最後に下記マーカーで囲った "
        "JSON 配列だけを 1 つ出力する。JSON 外に候補を書かない。候補が無ければ空配列 `[]`。\n\n"
        "<<<RUBRIC_JSON>>>\n"
        '[{"rule": "...", "scope": "general", "note": "..."}]\n'
        "<<<END_RUBRIC_JSON>>>\n"
    )

    return (
        f"{header}\n{ai_block}\n\n{user_block}\n\n{rubric_block}\n\n{instructions}"
    )


# ============================================================================
# 出力パース
# ============================================================================

_RUBRIC_RE = re.compile(
    r"<<<RUBRIC_JSON>>>\s*(?P<body>.*?)\s*<<<END_RUBRIC_JSON>>>",
    re.DOTALL,
)
_VALID_SCOPES = {"general", "owner", "category"}


def _parse_learning_output(answer_md: str) -> tuple[str, list[dict]]:
    """Opus 回答を (総括 Markdown, ルーブリック候補 list) に分解する.

    - 総括 = マーカーより前の本文 (マーカーが無ければ全文)。
    - 候補 = マーカー内 JSON。パース不能でも例外を上げず空配列 (総括は救う / Q0)。
    """
    m = _RUBRIC_RE.search(answer_md)
    if not m:
        # マーカー無し = 総括のみ救済。候補抽出は諦める (空)。
        logger.warning("[duel_learning] RUBRIC_JSON マーカー無し — 総括のみ記録")
        return answer_md.strip(), []

    summary = answer_md[: m.start()].strip() or answer_md.strip()
    body = m.group("body").strip()
    candidates: list[dict] = []
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        logger.warning("[duel_learning] ルーブリック JSON パース失敗 (%s) — 候補 0 件で続行", e)
        return summary, []

    if not isinstance(parsed, list):
        logger.warning("[duel_learning] ルーブリック JSON が配列でない — 候補 0 件")
        return summary, []

    for item in parsed:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule") or "").strip()
        if not rule:
            continue
        scope = str(item.get("scope") or "general").strip().lower()
        if scope not in _VALID_SCOPES:
            scope = "general"
        note = str(item.get("note") or "").strip()
        candidates.append({"rule": rule, "scope": scope, "note": note})
    return summary, candidates


# ============================================================================
# memory 書き込み
# ============================================================================

def _read_existing_rubric() -> str:
    """Q7 read-first: 既存 reference_research_rubric.md 本文 (無ければ空文字列)."""
    try:
        return RUBRIC_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_summary_memory(
    rnd: dict,
    summary_md: str,
    candidates: list[dict],
    ans: "research_brain.ResearchAnswer",
) -> Path:
    """総括を feedback_research_duel_<jst_date>.md に書く (同日既存なら追記).

    wiki-frontmatter 準拠 (metadata: block = Write 新規の auto-memory canonical schema)。
    同日に複数 round が完了する設計のため、新規時のみ frontmatter、以降は round 見出しを追記。
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    jst_date = rnd.get("jst_date") or datetime.now().strftime("%Y-%m-%d")
    path = MEMORY_DIR / f"feedback_research_duel_{jst_date}.md"
    round_id = rnd.get("round_id")

    cat = rnd.get("category_label") or (
        f"category_id={rnd.get('category_id')}" if rnd.get("category_id") is not None
        else "カテゴリ指定なし"
    )
    cand_lines = "\n".join(
        f"  - ({c['scope']}) {c['rule']}" for c in candidates
    ) or "  - (候補なし)"
    section = (
        f"\n## round_id={round_id} ({rnd.get('pattern')} / {cat})\n\n"
        f"- 学習実行: {datetime.now().strftime('%Y-%m-%d %H:%M')} (model={ans.model_used}, "
        f"qa_id={ans.qa_id}, cost=${ans.cost_usd:.4f})\n\n"
        f"### 総括\n\n{summary_md.strip()}\n\n"
        f"### 抽出したルーブリック候補 (→ reference_research_rubric.md に追記、user 承認待ち)\n\n"
        f"{cand_lines}\n"
    )

    if path.exists():
        with path.open("a", encoding="utf-8") as f:
            f.write(section)
        return path

    fm = (
        "---\n"
        f"name: feedback_research_duel_{jst_date}\n"
        f"description: リサーチ対戦アリーナ (W286) {jst_date} 完了ラウンドの深層学習総括。"
        "オーナー採点からの構造抽出 + ルーブリック候補。Phase1 は記録のみ (evaluate_match 非汚染)\n"
        "layer: wiki\n"
        f"updated: {datetime.now().strftime('%Y-%m-%d')}\n"
        "metadata:\n"
        "  type: feedback\n"
        "  wiki_type: synthesis\n"
        "  genre: research\n"
        "---\n\n"
        f"# リサーチ対戦アリーナ 深層学習総括 ({jst_date})\n\n"
        "オーナーが AI のリサーチ 5 品を採点した結果から、Opus 4.8 が抽出した「低得点の構造的理由」"
        "と「オーナーの暗黙判断ルール」の記録。**Phase1 = 記録のみ** で、採点は本番の仕入先同一性判定"
        "(`claude_evaluator.evaluate_match`) には一切配線しない (money-direct 汚染防止 / 設計書 §0)。\n"
        "ルーブリック候補の本体は `reference_research_rubric.md` (単一ファイル)。**昇格は user 承認後**。\n"
    )
    path.write_text(fm + section, encoding="utf-8")
    return path


def _append_rubric_candidates(
    rnd: dict,
    candidates: list[dict],
    existing_rubric: str,
) -> int:
    """ルーブリック候補を reference_research_rubric.md に追記する (無ければ新規作成).

    各候補: rule / scope / 由来round / support_count=1 / status=candidate。
    multi-file cascade は呼ばない (単一ファイル / 設計書制約)。自動昇格しない。
    戻り値 = 追記した候補数。
    """
    if not candidates:
        # 候補ゼロでも本ファイルだけは初期化しておく (次回 read-first 用)。
        if not RUBRIC_PATH.exists():
            RUBRIC_PATH.parent.mkdir(parents=True, exist_ok=True)
            RUBRIC_PATH.write_text(_rubric_header(), encoding="utf-8")
        return 0

    RUBRIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    round_id = rnd.get("round_id")
    jst_date = rnd.get("jst_date")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    block_lines = [f"\n### {jst_date} round_id={round_id} 由来 ({ts})\n"]
    for c in candidates:
        note = f" — {c['note']}" if c.get("note") else ""
        block_lines.append(
            f"- [ ] **({c['scope']})** {c['rule']}  "
            f"`support_count=1` `status=candidate` `from_round={round_id}`{note}"
        )
    block = "\n".join(block_lines) + "\n"

    if RUBRIC_PATH.exists():
        with RUBRIC_PATH.open("a", encoding="utf-8") as f:
            f.write(block)
    else:
        RUBRIC_PATH.write_text(_rubric_header() + block, encoding="utf-8")
    return len(candidates)


def _rubric_header() -> str:
    """reference_research_rubric.md の frontmatter + 説明 (新規作成時のみ)."""
    return (
        "---\n"
        "name: reference_research_rubric\n"
        "description: リサーチ対戦アリーナ (W286) でオーナー採点から抽出した仕入候補リサーチの"
        "判断ルーブリック候補。各候補は user 承認後に昇格 (自動昇格しない)。Phase1 は記録のみ\n"
        "layer: wiki\n"
        f"updated: {datetime.now().strftime('%Y-%m-%d')}\n"
        "metadata:\n"
        "  type: reference\n"
        "  wiki_type: concept\n"
        "  genre: research\n"
        "---\n\n"
        "# リサーチ判断ルーブリック (候補ボード)\n\n"
        "リサーチ対戦アリーナ (W286) の完了ラウンドで、Opus 4.8 がオーナーの採点・選定理由から"
        "抽出した「良い仕入候補の判断ルール」候補を蓄積する **単一ファイル**。\n\n"
        "## 運用ルール (厳守)\n\n"
        "- 各候補: `scope` (general=全カテゴリ / owner=オーナー固有 / category=カテゴリ固有) / "
        "`support_count` (同趣旨が出た回数) / `status` (candidate→ user 承認で昇格) / `from_round`。\n"
        "- **自動昇格しない**。`status=candidate` のまま蓄積し、昇格は user の承認 (チェック) 後。\n"
        "- **本番の仕入先同一性判定 (`claude_evaluator.evaluate_match`) には配線しない** "
        "(money-direct 汚染防止 / 設計書 §0)。Phase2 で相関実証後に『リサーチ選定専用』few-shot へ。\n"
        "- 同趣旨の候補が複数 round で出たら手動で `support_count` を加算 (multi-file cascade はしない)。\n\n"
        "---\n"
    )


# CLI 動作確認用
if __name__ == "__main__":
    import sys

    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if len(sys.argv) < 2:
        print("Usage: python -m monitor.research_duel_learning <round_id>")
        sys.exit(1)
    rid = int(sys.argv[1])
    out = run_completion_learning(rid)
    print(json.dumps(out, ensure_ascii=False, indent=2))
