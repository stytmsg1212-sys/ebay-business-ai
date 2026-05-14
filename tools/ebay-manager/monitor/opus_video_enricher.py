"""Opus 4.7 動画学習深掘り (W22 ハイブリッド方式の Phase B).

Gemini 2.5 Flash が抽出した構造化 JSON (videos_learned.gemini_response_raw) を
Opus 4.7 が MonoHonpo (eBay 越境EC) の業務目線で深く再解釈し、
- core_lesson: 最重要な学び (1-2 文)
- applicable_to_us: 弊社業務 (eBay 出品/仕入/価格/通関/システム開発) への適用案
- cross_video_links: 他の videos_learned との関連トピック
- red_flags: 弊社では当てはまらない点 / 注意点
- enriched_keywords: knowledge_index 用の追加キーワード

を生成して videos_learned に上書きする.

cost: 動画 1 本あたり ~$0.15-0.50 (input ~2K + output ~1.5K tokens).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# .env ロード
try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

OPUS_MODEL = "claude-opus-4-7"

# Opus 4.7 への深掘り指示プロンプト. MonoHonpo (eBay 越境EC) の業務文脈を明示.
ENRICH_PROMPT = """\
あなたは MonoHonpo (eBay 越境EC セラー) の Research 脳です. Opus 4.7 として深く考察してください.

以下は Gemini 2.5 Flash が動画から抽出した構造化結果です. これを読み、
MonoHonpo の実際の業務 (eBay 出品 / 仕入 / 価格設定 / 通関対応 / システム開発自動化) に
**どう活かせるか** を深く考察し、JSON で返してください.

## 動画タイトル
{title}

## 動画チャネル
{channel}

## Gemini 抽出 JSON (構造化済)
{gemini_json}

## 期待する出力 (厳密 JSON、```json フェンス禁止、前後テキスト禁止)

{{
  "core_lesson": "この動画の最重要な学びを 1-2 文で凝縮. MonoHonpo として何が一番大事か.",
  "applicable_to_us": [
    "MonoHonpo の業務にどう適用するかの具体案 (3-7 件、業務領域 [出品/仕入/価格/通関/システム開発] のいずれかを冒頭にタグ付け)",
    "例: [システム開発] CLAUDE.md にプロジェクトルールを記述してセッション間でコンテキストを保持する"
  ],
  "cross_video_links": [
    "他の動画で扱われていそうな関連トピックや、この動画と組み合わせると効果的な視点 (3-5 件)"
  ],
  "red_flags": [
    "この動画の主張のうち、弊社 (MonoHonpo) では当てはまらない点 / 注意点 / 適用前に検討すべきこと (1-5 件)",
    "例: 動画の言うベストプラクティスが越境EC の特殊事情と合わない場合、その理由"
  ],
  "enriched_keywords": [
    "knowledge_index 用の追加キーワード (5-15 件、Gemini が拾いきれていない業務適用視点のキーワード)"
  ]
}}

## 原則
- 動画の表面的な要約ではなく、**MonoHonpo が今すぐ実行できる粒度**まで翻訳する
- Claude Code / システム開発系の動画なら「自動化システム改善」視点で適用案を出す
- eBay 物販系の動画なら「出品・仕入・価格・通関」視点で適用案を出す
- 動画内容が弊社と無関係でも、無理に当てはめず red_flags にその旨を記す (捏造禁止)
"""


def _get_anthropic_client():
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic SDK が未インストール") from e
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY が .env に未設定")
    return anthropic.Anthropic(api_key=api_key)


def enrich_video(
    video_id: str,
    *,
    db_path: str = "data/monitor.db",
    save_to_db: bool = True,
) -> Optional[dict]:
    """指定 video_id の Gemini 抽出結果を Opus 4.7 で深掘り、enriched JSON を返す.

    save_to_db=True なら videos_learned 該当行に書き戻す.
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        "SELECT video_id, title, channel, summary_ja, gemini_response_raw "
        "FROM videos_learned WHERE video_id=? AND status='done'",
        (video_id,),
    )
    row = cur.fetchone()
    if not row:
        con.close()
        logger.warning(f"video_id={video_id} not found or status!=done")
        return None

    gemini_raw = row["gemini_response_raw"] or ""
    if not gemini_raw or len(gemini_raw) < 100:
        con.close()
        logger.warning(f"video_id={video_id} gemini_response_raw が空/短すぎる")
        return None

    title = row["title"] or "(no title)"
    channel = row["channel"] or "(unknown)"

    prompt = ENRICH_PROMPT.format(
        title=title[:200],
        channel=channel[:100],
        gemini_json=gemini_raw[:30000],  # 最大 30KB に制限 (token 過大化防止)
    )

    client = _get_anthropic_client()

    try:
        from monitor.api_logger import log_anthropic_response, _Timer
        with _Timer() as t:
            msg = client.messages.create(
                model=OPUS_MODEL,
                max_tokens=2500,
                messages=[{"role": "user", "content": prompt}],
            )
        log_anthropic_response("video_opus_enrich", OPUS_MODEL, msg,
                               duration_ms=t.duration_ms, success=True)
    except Exception as e:
        logger.error(f"Opus call failed: {e}")
        con.close()
        try:
            from monitor.api_logger import log_anthropic_response
            log_anthropic_response("video_opus_enrich", OPUS_MODEL, None,
                                   success=False, error_message=str(e)[:500])
        except Exception:
            pass
        return None

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )

    # JSON 抽出 (```json フェンス対応)
    import re
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    json_str = fence.group(1) if fence else text.strip()

    try:
        enriched = json.loads(json_str)
    except json.JSONDecodeError:
        # truncation 対応
        try:
            from monitor.gemini_video_learner import _repair_truncated_json
            enriched = json.loads(_repair_truncated_json(json_str))
        except Exception as e:
            logger.error(f"JSON parse failed: {e}")
            con.close()
            return None

    # コスト計算
    usage = msg.usage
    input_tokens = getattr(usage, "input_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", 0)
    # Opus 4.7 pricing: input $15/Mtok, output $75/Mtok
    cost_usd = (input_tokens / 1_000_000) * 15.0 + (output_tokens / 1_000_000) * 75.0

    enriched["_meta"] = {
        "model": OPUS_MODEL,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost_usd, 4),
        "enriched_at": datetime.now().isoformat(),
    }

    if save_to_db:
        cur.execute(
            """
            UPDATE videos_learned
               SET opus_enriched_at = ?,
                   opus_model = ?,
                   opus_cost_usd = ?,
                   core_lesson = ?,
                   applicable_to_us = ?,
                   cross_video_links = ?,
                   red_flags = ?,
                   enriched_keywords = ?,
                   opus_raw_response = ?
             WHERE video_id = ?
            """,
            (
                datetime.now().isoformat(),
                OPUS_MODEL,
                round(cost_usd, 4),
                enriched.get("core_lesson", ""),
                json.dumps(enriched.get("applicable_to_us", []), ensure_ascii=False),
                json.dumps(enriched.get("cross_video_links", []), ensure_ascii=False),
                json.dumps(enriched.get("red_flags", []), ensure_ascii=False),
                json.dumps(enriched.get("enriched_keywords", []), ensure_ascii=False),
                text[:5000],
                video_id,
            ),
        )
        # enriched_keywords を knowledge_index にも追加 (既存重複チェック)
        for kw in enriched.get("enriched_keywords", []):
            if not kw or len(kw) > 100:
                continue
            try:
                cur.execute(
                    "INSERT INTO knowledge_index (keyword, video_id, weight, context) "
                    "VALUES (?, ?, ?, ?)",
                    (kw, video_id, 1.5,  # weight=1.5 で Opus 由来を優遇
                     enriched.get("core_lesson", "")[:200]),
                )
            except sqlite3.IntegrityError:
                pass
        con.commit()
        logger.info(f"Opus enriched saved: video_id={video_id} cost=${cost_usd:.4f}")

    con.close()
    return enriched


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python -m monitor.opus_video_enricher <video_id>")
        sys.exit(1)
    vid = sys.argv[1]
    result = enrich_video(vid)
    if not result:
        print("FAILED")
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))
