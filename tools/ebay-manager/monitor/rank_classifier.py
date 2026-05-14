#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仕入先記述 → 8段階ランク自動推定 (W9 Phase 3)

仕入先の日本語 (condition + description + title) から、eBay 出品ランク
(N / S / A / B / C / D / PO / As-Is) を Claude Haiku で推定する。
ランク → eBay Condition ID は固定 map で変換。

設計方針:
  - Claude Haiku (低コスト・低レイテンシ) を採用。判定は有界なカテゴリ分類なので
    Sonnet 相当の推論は不要。
  - STABLE プロンプト (判定ルール表) を prompt cache 対象にする
    claude_summarizer.py のパターンを踏襲。
  - API 失敗 / 未設定時は regex キーワードマッチでフォールバック。
    最悪 As-Is + confidence=0.3 を返して UI を止めない。
  - api_call_log に operation='rank_classify' で記録。

正源仕様: .company/ebay-knowledge/topics/listing-description-template.md
および memory feedback_condition_rank_system.md
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# pythonw gotcha ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (ValueError, OSError):
        pass

# .env ロード (ebay-manager root の .env を明示ロード、CWD 非依存)
try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    pass

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

logger = logging.getLogger(__name__)

# ランク分類は有界なタスクなので Haiku で十分。
# 候補評価 (claude_evaluator) とは別モデルで運用し、コスト最小化。
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


# =========================================================================
# dataclass
# =========================================================================

@dataclass
class RankClassification:
    """仕入先記述から推定された出品ランク。"""
    rank_code: str           # 'N' / 'S' / 'A' / 'B' / 'C' / 'D' / 'PO' / 'As-Is'
    rank_label: str          # 'New (Unopened)' / 'Like New' / 'Excellent' 等 (英語)
    rank_jp: str             # 'Tested · Minor Wear' 等 (description の rank_jp に使う短文)
    ebay_condition_id: str   # '1000' / '1500' / '3000' / '7000'
    confidence: float        # 0.0-1.0
    reasoning: str           # 判定理由 (日本語)


# =========================================================================
# 定数テーブル (テンプレ正源と同期)
# =========================================================================

# ランク → (英語ラベル, JPヒント, eBay Condition ID)
_RANK_TABLE: dict[str, tuple[str, str, str]] = {
    "N":     ("New (Unopened)",  "Brand New Sealed",          "1000"),
    "S":     ("Like New",        "Opened \u00b7 No Wear",     "1500"),
    "A":     ("Excellent",       "Tested \u00b7 Minor Wear",  "3000"),
    "B":     ("Good",            "Tested \u00b7 Visible Wear", "3000"),
    "C":     ("Fair",            "Tested \u00b7 Heavy Wear",  "3000"),
    "D":     ("Issues",          "Working \u00b7 Limited Function", "3000"),
    "PO":    ("Power-On Only",   "Powers On \u00b7 Untested", "3000"),
    "As-Is": ("As-Is",           "Not Tested \u00b7 No Warranty", "7000"),
}

VALID_RANKS: tuple[str, ...] = tuple(_RANK_TABLE.keys())


# Fallback 用 regex パターン (優先度順)。
# 設計方針:
#   1. As-Is / ジャンク系を最優先 (安全側 = Defect 回避)
#   2. PO (通電確認のみ) を As-Is より先に判定するのは、仕入先表記で PO キーワードが
#      出た場合「通電はしている = 完全ジャンクではない」として格上げするため
#   3. その後 新品系 (N/S) を上位ランクとして判定
#   4. 続いて 美品 (A) を先に評価: 「美品 小キズあり」等の混在記述で
#      傷あり系 (D) より美品判定を優先する (ユーザーが「美品」と表記している以上、
#      小キズは織り込み済と解釈)
#   5. 最後に D / C / B の順 (D のキーワードは明確な訳あり表記のみ)
_FALLBACK_PATTERNS: list[tuple[str, str]] = [
    # As-Is (ジャンク・動作未確認・故障・部品取り)
    ("As-Is", r"ジャンク|動作未確認|動作しません|動作不可|故障|部品取り|壊れ|破損|通電しない"),
    # PO (通電確認のみ)
    ("PO",    r"通電確認のみ|通電のみ|電源のみ確認|Power\s*on\s*only"),
    # N (新品・未開封・シュリンク)
    ("N",     r"新品未開封|未開封|シュリンク|factory\s*sealed|Brand\s*New"),
    # S (新品同様・未使用・開封品)
    ("S",     r"新品同様|未使用に近い|ほぼ未使用|展示品|未使用(?:品)?|開封(?:済|品)"),
    # A (美品) — 傷あり・使用感より先に評価する
    ("A",     r"美品|ほぼ新品|極美品|ほとんど新品|ほぼ美品"),
    # D (全体的に状態が悪い・訳あり系)
    ("D",     r"全体的に状態が悪い|訳あり|難あり|ワケあり|へこみ|凹み"),
    # 2026-04-22 FIX (code-reviewer H1): Mercari/Yahoo 標準「やや傷や汚れあり」「目立った傷や
    # 汚れなし」を B パターンとして C より先に置く。C の `傷や汚れあり` が `やや傷や汚れあり` の
    # substring としてマッチしてしまう重大バグを防ぐ。
    # 「中古ですが」「中古です」も B に寄せる (単独「中古品」のみ拾えていた既存バグの補完)。
    # 「普通」は偽陽性リスク高のため除外 (A が先行判定するので実害なしと判断、より安全側に)。
    ("B",     r"目立った傷や汚れなし|やや傷や汚れあり|良品|並品|中古品|中古(?:です|ですが)"),
    # C (傷や汚れあり・使用感) — B に拾われなかったもののみ
    ("C",     r"傷や汚れあり|使用感あり|使用感|中古.*使用感|使用(?:に?よる)?スレ"),
]


# =========================================================================
# Claude Haiku プロンプト
# =========================================================================

# STABLE 部 (prompt cache 対象)。判定ルール表は仕様改訂時のみ変わる。
_STABLE_SYSTEM_PROMPT = """あなたは eBay 越境EC セラーの品質判定アシスタントです。
日本の仕入先 (ヤフオク / メルカリ / PayPayフリマ) の日本語商品記述を読み、
eBay 出品時の 8段階ランク体系に分類します。

## 8段階ランク体系

| Rank  | EN Label         | JP Hint                      | eBay Cond ID |
|-------|------------------|------------------------------|--------------|
| N     | New (Unopened)   | Brand New Sealed             | 1000 |
| S     | Like New         | Opened \u00b7 No Wear        | 1500 |
| A     | Excellent        | Tested \u00b7 Minor Wear     | 3000 |
| B     | Good             | Tested \u00b7 Visible Wear   | 3000 |
| C     | Fair             | Tested \u00b7 Heavy Wear     | 3000 |
| D     | Issues           | Working \u00b7 Limited Function | 3000 |
| PO    | Power-On Only    | Powers On \u00b7 Untested    | 3000 |
| As-Is | As-Is            | Not Tested \u00b7 No Warranty | 7000 |

## 仕入先日本語キーワード → ランク推定ルール

| 仕入先日本語                                     | 推定ランク |
|--------------------------------------------------|-----------|
| 新品 / 未開封 / シュリンク付                     | N |
| 新品同様 / 未使用 / 未使用に近い / 開封品        | S |
| 美品 / 美品に近い / 極美品                       | A |
| **目立った傷や汚れなし** (Mercari/Yahoo 標準)    | **B** |
| 良品 / 並品 / 普通 / 中古品 / 中古ですが綺麗     | B |
| **やや傷や汚れあり** (Mercari/Yahoo 標準)        | B |
| **傷や汚れあり** (Mercari/Yahoo 標準) / 使用感あり | C |
| **全体的に状態が悪い** (Mercari/Yahoo 標準) / 難あり | D |
| 通電確認のみ (動作未確認明記)                    | PO |
| 動作未確認 / ジャンク / 部品取り / 故障          | As-Is |

## 判定方針 (2026-04-22 改訂)

1. **Mercari/ヤフオク の 6段階標準ラベルを最優先で認識**:
   新品 → S、未使用に近い → S、目立った傷や汚れなし → B、やや傷や汚れあり → B、
   傷や汚れあり → C、全体的に状態が悪い → D
   これらの定型ラベルは出品者が明示的に選択したものなので優先度が高い。
2. **動作確認 / 使用実績が記述にあるなら As-Is 禁止**:
   「日本の家電で使用実績」「動作確認済」「使用してました」等があれば、
   最低でも B 以上。As-Is は不適切。
3. **As-Is 必須条件**: 「動作未確認」「ジャンク」「部品取り」「故障」「通電しない」
   のいずれかを仕入先が**明示的に書いた**場合のみ As-Is。
   「年代物」「古い」「2年使用」だけでは As-Is にしない。
4. **PO と As-Is の判別**: 「通電確認のみ」は PO。「動作未確認」は As-Is。
   両方を含む場合は PO (通電は確認できているため)。
5. **confidence**: キーワードが明確に1つだけなら 0.9 以上。
   複数キーワード混在 / 曖昧なら 0.5-0.7。判断困難なら 0.3 以下。
6. **安全側判定の適用範囲**: 「美品だが動作未確認」等の**真に矛盾する**記述のみ
   As-Is/PO に寄せる。明らかに動作 OK な記述 + 使用感/軽微な傷は通常ランク
   (A/B/C) で判定する。

## 出力フォーマット (JSON のみ、コードブロック禁止)

{
  "rank_code": "N|S|A|B|C|D|PO|As-Is のいずれか",
  "rank_label": "英語ラベル (上表のEN Labelをそのまま)",
  "rank_jp": "短い英語ヒント (上表のJP Hintをそのまま)",
  "confidence": 0.0-1.0,
  "reasoning": "判定理由 (日本語、1〜2文)"
}
"""


# =========================================================================
# Claude 呼び出しヘルパ
# =========================================================================

def _get_client() -> Optional["anthropic.Anthropic"]:
    """Anthropic クライアント取得。API キー未設定 / ライブラリ欠損時は None。"""
    if not _ANTHROPIC_OK:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _extract_json(text: str) -> Optional[str]:
    """Claude 出力から JSON 文字列候補を抽出 (claude_summarizer と同ロジック)。"""
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if fence:
        return fence.group(1)
    greedy = re.search(r'\{[\s\S]*\}', text)
    if greedy:
        return greedy.group(0)
    open_brace = re.search(r'\{[\s\S]*$', text)
    if open_brace:
        return open_brace.group(0).rstrip() + "}"
    return None


def _compose_user_prompt(
    supplier_condition_ja: str,
    supplier_description_ja: Optional[str],
    supplier_title_ja: Optional[str],
) -> str:
    """Claude 用の DYNAMIC 部を構築。"""
    lines = ["以下の仕入先商品情報を読み、8段階ランクを推定してください。\n"]
    if supplier_title_ja:
        lines.append(f"【商品タイトル】\n{supplier_title_ja[:300]}\n")
    lines.append(f"【商品の状態 (仕入先記載)】\n{(supplier_condition_ja or '(記載なし)')[:300]}\n")
    if supplier_description_ja:
        # description は長いので 2000 文字上限
        lines.append(f"【商品説明】\n{supplier_description_ja[:2000]}\n")
    lines.append("上記を JSON で判定してください。")
    return "\n".join(lines)


# =========================================================================
# Fallback (API 失敗時)
# =========================================================================

def _fallback_classify(
    supplier_condition_ja: str,
    supplier_description_ja: Optional[str],
    supplier_title_ja: Optional[str],
) -> RankClassification:
    """Claude 不使用 / 失敗時の regex ベース判定。

    安全側 (As-Is) 優先で判定し、合致なしなら As-Is + low confidence を返す。
    """
    # condition + description + title を統合してキーワードマッチ
    haystack = " ".join(
        (x or "") for x in [
            supplier_condition_ja,
            supplier_description_ja,
            supplier_title_ja,
        ]
    )

    matched_rank: Optional[str] = None
    matched_keyword: Optional[str] = None
    for rank, pattern in _FALLBACK_PATTERNS:
        m = re.search(pattern, haystack)
        if m:
            matched_rank = rank
            matched_keyword = m.group(0)
            break

    if matched_rank is None:
        # 2026-04-22 変更: As-Is 寄せ default が「目立った傷や汚れなし」等で
        # 憤慨バグの原因になった。動作確認/使用実績が記述にある場合は B (中古良品)
        # を default にして安全だが現実的な判定に倒す。
        # ジャンク系キーワードは先頭 pattern で検出済なのでここでは使える扱い。
        positive_signal = re.search(
            r"使用実績|使用して|動作(?:確認|OK|可|問題)?|正常|使える|使え(?:まし|ます)|稼働",
            haystack,
        )
        if positive_signal or "中古" in haystack:
            return _build_result(
                "B",
                confidence=0.55,
                reasoning=(
                    "キーワード不明瞭だが、動作/使用実績の記述が確認できたため "
                    "fallback B (Good)"
                ),
            )
        # それすらも無い場合のみ As-Is default に倒す (真に情報なし)
        return _build_result(
            "As-Is",
            confidence=0.3,
            reasoning="キーワード不明瞭かつ動作確認記述なしのため As-Is (fallback)",
        )

    return _build_result(
        matched_rank,
        confidence=0.7,
        reasoning=f"fallback regex match: '{matched_keyword}'",
    )


def _build_result(rank_code: str, confidence: float, reasoning: str) -> RankClassification:
    """ランクコードから RankClassification を組み立てる。"""
    if rank_code not in _RANK_TABLE:
        rank_code = "As-Is"  # 不明ランクは安全側に矯正
    label, jp_hint, cond_id = _RANK_TABLE[rank_code]
    # confidence は 0.0-1.0 にクランプ
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        c = 0.0
    c = max(0.0, min(1.0, c))
    return RankClassification(
        rank_code=rank_code,
        rank_label=label,
        rank_jp=jp_hint,
        ebay_condition_id=cond_id,
        confidence=c,
        reasoning=reasoning,
    )


# =========================================================================
# 公開 API
# =========================================================================

def classify_rank(
    supplier_condition_ja: str,
    supplier_description_ja: Optional[str] = None,
    supplier_title_ja: Optional[str] = None,
) -> RankClassification:
    """仕入先の日本語記述から 8段階ランクを推定する。

    Claude Haiku + prompt cache を使用。API キー未設定 / API 失敗時は
    regex フォールバックで As-Is 寄りの判定を返す。

    Args:
        supplier_condition_ja: 仕入先が記載した商品状態文字列 (例: "中古 美品")
        supplier_description_ja: 商品説明本文 (Optional)
        supplier_title_ja: 商品タイトル (Optional)

    Returns:
        RankClassification (常に有効、例外は投げない)
    """
    # Claude 未利用ケース: API キー欠損
    client = _get_client()
    if not client:
        logger.debug("Claude unavailable -> fallback regex")
        return _fallback_classify(
            supplier_condition_ja, supplier_description_ja, supplier_title_ja,
        )

    user_prompt = _compose_user_prompt(
        supplier_condition_ja or "",
        supplier_description_ja,
        supplier_title_ja,
    )

    # api_logger 呼び出し (claude_summarizer と同パターン)
    from monitor.api_logger import log_anthropic_response, _Timer

    msg = None
    try:
        with _Timer() as t:
            msg = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=400,
                system=[
                    {
                        "type": "text",
                        "text": _STABLE_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
            )
        log_anthropic_response(
            "rank_classify", CLAUDE_MODEL, msg,
            duration_ms=t.duration_ms, success=True,
        )
    except anthropic.APIError as e:
        logger.warning(f"Claude API error in classify_rank: {e}")
        log_anthropic_response(
            "rank_classify", CLAUDE_MODEL, None,
            success=False, error_message=str(e)[:500],
        )
        return _fallback_classify(
            supplier_condition_ja, supplier_description_ja, supplier_title_ja,
        )
    except Exception as e:  # noqa: BLE001 — UI を絶対に止めない
        logger.warning(f"rank_classify unexpected: {e}")
        log_anthropic_response(
            "rank_classify", CLAUDE_MODEL, None,
            success=False, error_message=str(e)[:500],
        )
        return _fallback_classify(
            supplier_condition_ja, supplier_description_ja, supplier_title_ja,
        )

    # 応答 parse
    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    cand = _extract_json(text)
    if not cand:
        logger.warning(f"rank_classify: no JSON in response: {text[:120]!r}")
        return _fallback_classify(
            supplier_condition_ja, supplier_description_ja, supplier_title_ja,
        )

    try:
        data = json.loads(cand)
    except json.JSONDecodeError as e:
        logger.warning(f"rank_classify JSON decode: {e}, raw={cand[:120]!r}")
        return _fallback_classify(
            supplier_condition_ja, supplier_description_ja, supplier_title_ja,
        )

    raw_rank = str(data.get("rank_code", "")).strip()
    if raw_rank not in _RANK_TABLE:
        logger.warning(f"rank_classify: invalid rank '{raw_rank}' -> fallback")
        return _fallback_classify(
            supplier_condition_ja, supplier_description_ja, supplier_title_ja,
        )

    return _build_result(
        raw_rank,
        confidence=data.get("confidence", 0.5),
        reasoning=str(data.get("reasoning", ""))[:500],
    )


if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO)

    # 手動テスト例
    samples = [
        ("美品", "動作確認済 使用に伴う小キズあり", "Sony WH-1000XM5 Black"),
        ("ジャンク", "通電せず。現状でのお渡し、ノークレームノーリターン", "KEYENCE FS-N18N"),
        ("新品未開封", "シュリンク付き、未開封品です", "Pioneer Lonesome Carboy"),
    ]
    for cond, desc, title in samples:
        r = classify_rank(cond, desc, title)
        print(_json.dumps({
            "title": title,
            "cond": cond,
            "rank_code": r.rank_code,
            "rank_label": r.rank_label,
            "rank_jp": r.rank_jp,
            "ebay_condition_id": r.ebay_condition_id,
            "confidence": r.confidence,
            "reasoning": r.reasoning,
        }, ensure_ascii=False, indent=2))
