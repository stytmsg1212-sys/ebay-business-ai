"""商品画像を Gemini Vision で解析し、8 枚の plate library から上位 k 候補を選ぶ.

Gemini 2.5 Flash (text-only) は無料枠で使える (画像を入力に、text JSON を出力)。
コスト: ~$0.001/analysis (誤差レベル)。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

try:
    from google import genai
    _GENAI_OK = True
except ImportError:
    _GENAI_OK = False

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

from monitor.plate_library import (
    PLATE_LIBRARY, PlateSpec, AngleTag, active_plates, PINNED_PLATE_ID,
)

MODEL = "gemini-2.5-flash"  # primary: 無料枠 OK, text 出力
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # fallback: 503 時に自動切替


ANALYSIS_PROMPT = """You are analyzing a product photograph to recommend the best orientation
and camera-angle match for a brand plate that will be composited next to this product.

Return ONLY a valid JSON object with these fields, no prose:
{
  "product_shape": "flat_thin" | "tall_vertical" | "blocky_horizontal" | "blocky_cubic" | "irregular",
  "camera_elevation": "overhead" | "high_angle" | "mid_angle" | "eye_level" | "low_angle",
  "product_facing": "front" | "three_quarter_left" | "three_quarter_right" | "side" | "top_down",
  "recommended_orientation": "flat" | "stand",
  "reasoning": "one short sentence (max 20 words) justifying the choice"
}

Field definitions:
- product_shape:
  * flat_thin: CD/DVD, cable, cards, cassette tapes (thin in one dimension)
  * tall_vertical: speaker, mic, bottle, standing device taller than wide
  * blocky_horizontal: amplifier, receiver, wider than tall
  * blocky_cubic: roughly equal dimensions (cube-like electronics)
  * irregular: cannot classify
- camera_elevation:
  * overhead: pure top-down shot
  * high_angle: 60 degrees from horizontal (product lying flat, slight overhead)
  * mid_angle: ~45 degrees oblique
  * eye_level: camera at product mid-height
  * low_angle: camera below product center looking up
- product_facing: which face of product is primarily visible to the camera
- recommended_orientation:
  * "flat" (lying plate) when: flat_thin products, or any product shot from overhead/high_angle
  * "stand" (upright plate) when: tall_vertical/blocky products shot at eye_level or mid_angle

Output ONLY the JSON, nothing else."""


@dataclass
class ProductAnalysis:
    product_shape: str
    camera_elevation: str
    product_facing: str
    recommended_orientation: str
    reasoning: str
    raw_response: str


@dataclass
class PlateRecommendation:
    plate: PlateSpec
    score: float
    match_reasons: list[str]


def _analyze_with_claude(img: Image.Image) -> str:
    """Claude Haiku Vision で商品解析 (Gemini 503 fallback)."""
    import base64
    import io
    if not _ANTHROPIC_OK:
        raise RuntimeError("anthropic SDK not installed")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    image_b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": image_b64,
                }},
                {"type": "text", "text": ANALYSIS_PROMPT},
            ],
        }],
    )
    return msg.content[0].text if msg.content else ""


def analyze_product_image(image_source, max_retries: int = 2) -> ProductAnalysis:
    """画像を AI Vision (Gemini 優先 / Claude fallback) で解析。

    Gemini 2.5 Flash が 503 の場合は Claude Haiku に自動切替。
    """
    import time
    if isinstance(image_source, (str, Path)):
        img = Image.open(image_source)
    elif isinstance(image_source, Image.Image):
        img = image_source
    else:
        raise TypeError(f"unsupported image source: {type(image_source)}")

    text = ""
    gemini_available = _GENAI_OK and bool(os.environ.get("GOOGLE_API_KEY"))

    # Gemini 優先試行
    if gemini_available:
        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        for attempt in range(max_retries):
            try:
                resp = client.models.generate_content(
                    model=MODEL, contents=[ANALYSIS_PROMPT, img],
                )
                text = (resp.text or "").strip()
                if text:
                    break
            except Exception as e:
                msg = str(e)
                if "503" in msg or "UNAVAILABLE" in msg or "overloaded" in msg.lower():
                    wait = 3 * (2 ** attempt)
                    logger.warning(f"Gemini 503 (attempt {attempt+1}/{max_retries}), retry after {wait}s")
                    time.sleep(wait)
                    continue
                logger.warning(f"Gemini error (non-503), falling back to Claude: {msg[:100]}")
                break

    # Claude fallback
    if not text:
        logger.info("Falling back to Claude Haiku Vision")
        text = _analyze_with_claude(img)
    if not text:
        raise RuntimeError("both Gemini and Claude returned empty")
    # コードフェンス剥がし (```json ... ```)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"no JSON in response: {text[:300]}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e} / {m.group(0)[:300]}")

    return ProductAnalysis(
        product_shape=data.get("product_shape", "irregular"),
        camera_elevation=data.get("camera_elevation", "mid_angle"),
        product_facing=data.get("product_facing", "front"),
        recommended_orientation=data.get("recommended_orientation", "stand"),
        reasoning=data.get("reasoning", ""),
        raw_response=text,
    )


# camera_elevation → 推奨 flat angle の優先順位
_ELEV_TO_FLAT_ANGLE: dict[str, list[AngleTag]] = {
    "overhead":   ["top_down", "high_angle", "mid_angle", "low_angle"],
    "high_angle": ["high_angle", "mid_angle", "top_down", "low_angle"],
    "mid_angle":  ["mid_angle", "high_angle", "low_angle", "top_down"],
    "eye_level":  ["low_angle", "mid_angle", "high_angle", "top_down"],
    "low_angle":  ["low_angle", "mid_angle", "high_angle", "top_down"],
}

# product_facing → 推奨 stand angle の優先順位
_FACING_TO_STAND_ANGLE: dict[str, list[AngleTag]] = {
    "front":              ["front", "low_front", "three_quarter_L", "three_quarter_R"],
    "three_quarter_left": ["three_quarter_L", "front", "three_quarter_R", "low_front"],
    "three_quarter_right":["three_quarter_R", "front", "three_quarter_L", "low_front"],
    "side":               ["three_quarter_L", "three_quarter_R", "front", "low_front"],
    "top_down":           ["front", "low_front", "three_quarter_L", "three_quarter_R"],
}


def score_plates(analysis: ProductAnalysis) -> list[PlateRecommendation]:
    """採用中の plate のみスコアリングして降順で返す (現状 W1-W4 の flat 4 種).

    立て掛け S1-S4 は 2026-04-22 検証で違和感が出たため不採用
    (plate_library.ACTIVE_PLATE_IDS で制御).
    """
    recs: list[PlateRecommendation] = []
    for plate in active_plates():
        score = 0.0
        reasons: list[str] = []

        # camera elevation とのマッチで angle をスコアリング
        pref_order = _ELEV_TO_FLAT_ANGLE.get(analysis.camera_elevation, [])
        if plate.angle in pref_order:
            rank = pref_order.index(plate.angle)
            angle_score = 10.0 - (rank * 2.0)  # 0位=10, 1位=8, 2位=6, 3位=4
            score += max(angle_score, 0.0)
            reasons.append(f"camera={analysis.camera_elevation}, angle rank {rank+1}/{len(pref_order)}")
        else:
            reasons.append(f"no angle match for camera={analysis.camera_elevation}")

        recs.append(PlateRecommendation(plate=plate, score=score, match_reasons=reasons))

    recs.sort(key=lambda r: -r.score)
    return recs


def select_top_k(
    analysis: ProductAnalysis,
    k: int = 3,
    pinned_plate_id: Optional[str] = PINNED_PLATE_ID,
) -> list[PlateRecommendation]:
    """AI スコアリング上位 k を返す。pinned 指定時は必ず先頭に固定.

    Args:
        analysis: Vision 分析結果.
        k: 返す候補数.
        pinned_plate_id: 必ず 1 位に入れる plate ID (None で pinning 無効).
    """
    scored = score_plates(analysis)
    if not pinned_plate_id:
        return scored[:k]

    # pinned を先頭に差し込み、残りは AI 順位で埋める
    pinned_rec = next((r for r in scored if r.plate.id == pinned_plate_id), None)
    if pinned_rec is None:
        # 不明な pinned → 無視して通常処理
        return scored[:k]
    pinned_rec.match_reasons = ["PINNED (fixed top candidate)"] + pinned_rec.match_reasons
    others = [r for r in scored if r.plate.id != pinned_plate_id]
    return [pinned_rec] + others[: k - 1]


def analyze_and_select(
    image_source,
    k: int = 3,
    pinned_plate_id: Optional[str] = PINNED_PLATE_ID,
) -> tuple[ProductAnalysis, list[PlateRecommendation]]:
    analysis = analyze_product_image(image_source)
    return analysis, select_top_k(analysis, k, pinned_plate_id=pinned_plate_id)
