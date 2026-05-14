"""eBay 1 枚目 hero image 生成パイプライン.

商品画像 → Photoroom (studio化) → plate_selector (top-k 選定)
  → Gemini nano-banana/edit で top-k 候補の合成画像を並列生成。

UI 側で top-k 候補を並べて user が 1 枚選択する想定。
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

try:
    import fal_client
    _FAL_OK = True
except ImportError:
    _FAL_OK = False

from monitor.plate_library import PlateSpec, get_plate, PINNED_PLATE_ID
from monitor.plate_selector import (
    ProductAnalysis,
    PlateRecommendation,
    analyze_and_select,
)

MODEL_ID = "fal-ai/nano-banana/edit"  # Gemini 2.5 Flash Image multi-image


@dataclass
class CompositionResult:
    plate_id: str
    success: bool
    score: float
    reasoning: str
    output_path: Optional[Path] = None
    error: Optional[str] = None
    elapsed_sec: float = 0.0
    image_url: Optional[str] = None


@dataclass
class HeroGenerationResult:
    analysis: ProductAnalysis
    candidates: list[CompositionResult] = field(default_factory=list)
    product_studio_path: Optional[Path] = None


_COMPOSE_PROMPT = (
    "You are compositing two images into one realistic product photograph. "
    "Image 1 is a product on a studio backdrop (keep the product exactly as shown). "
    "Image 2 is a physical gray washi paper tag you will place into the scene. "
    "\n\n"
    "TASK: Place the gray washi tag from Image 2 lying flat on the studio surface. "
    "\n\n"
    "**CRITICAL PLACEMENT RULES** (MUST FOLLOW):\n"
    "- The tag MUST be placed in the BOTTOM-LEFT CORNER of the frame, entirely "
    "within the bottom-left 35% of the image (x: 0%-35% from left, y: 60%-95% "
    "from top).\n"
    "- The tag MUST NOT overlap, touch, or obscure any part of the product body.\n"
    "- There MUST be at least 15% of clear empty floor space (gap) between the "
    "tag and the nearest edge of the product.\n"
    "- The tag must be entirely BELOW the product's horizontal midline (i.e., "
    "positioned in the lower portion of the frame, closer to the camera).\n"
    "- The tag size: its longest edge ~25% of final image width (smaller than "
    "the product, so the product remains the dominant subject).\n"
    "- If the product already occupies the bottom-left area, shift the tag to the "
    "bottom-right corner instead (still maintaining no overlap).\n"
    "\n"
    "TAG FIDELITY (critical): Reproduce the tag from Image 2 with FULL FIDELITY. "
    "The tag must be a SQUARE SHAPE (not circular, not oval, not rounded-corner "
    "rectangle - a square with natural torn deckle-edge paper borders). The tag "
    "color must be DARK GRAY (slate gray, approximately RGB #5a5a5a) - NOT white, "
    "NOT off-white, NOT cream. The tag is made of handmade washi paper with "
    "visible fibrous texture. On the tag surface: (A) a circular sumi-ink brush "
    "stroke emblem (enso ring) positioned in the upper half of the tag, and "
    "(B) directly below the enso, the four capital letters M-O-N-O printed in "
    "bold black sans-serif font. Both the circle and 'MONO' text must be clearly "
    "legible and sharp against the dark gray paper. "
    "\n\n"
    "PRODUCT FIDELITY (critical): Preserve every visible detail of the product from "
    "Image 1 exactly as shown - do not redesign, simplify, or regenerate the "
    "product. Keep all model numbers, display text, button labels, indicator LEDs, "
    "ports, connectors, brand logos, warning stickers, and physical details "
    "pixel-accurate. The product remains the focal point. "
    "\n\n"
    "SCENE UNIFICATION: Both the product and the tag share the same soft overhead "
    "diffuse studio lighting. The tag casts a soft natural contact shadow onto the "
    "surface that matches the product's own shadow direction and softness. "
    "Background is a clean seamless light neutral gray studio backdrop. "
    "\n\n"
    "OUTPUT: A single cohesive 1:1 square product photograph, photorealistic, "
    "high detail, no watermark, no text overlays."
)


def _compose_single(
    product_url: str,
    rec: PlateRecommendation,
    output_dir: Path,
    output_size: str = "1:1",
) -> CompositionResult:
    import time
    start = time.time()
    plate = rec.plate
    out_path = output_dir / f"hero_{plate.id}.png"
    try:
        plate_url = fal_client.upload_file(str(plate.transparent_path))
        resp = fal_client.subscribe(
            MODEL_ID,
            arguments={
                "prompt": _COMPOSE_PROMPT,
                "image_urls": [product_url, plate_url],
                "num_images": 1,
                "output_format": "png",
            },
            with_logs=False,
        )
        imgs = resp.get("images") or []
        if not imgs:
            return CompositionResult(
                plate_id=plate.id, success=False, score=rec.score,
                reasoning="; ".join(rec.match_reasons),
                error="no images in response", elapsed_sec=time.time()-start,
            )
        url = imgs[0].get("url")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=60, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            out_path.write_bytes(r.content)
        return CompositionResult(
            plate_id=plate.id, success=True, score=rec.score,
            reasoning="; ".join(rec.match_reasons),
            output_path=out_path, elapsed_sec=time.time()-start, image_url=url,
        )
    except Exception as e:
        logger.exception(f"[{plate.id}] compose failed")
        return CompositionResult(
            plate_id=plate.id, success=False, score=rec.score,
            reasoning="; ".join(rec.match_reasons),
            error=str(e), elapsed_sec=time.time()-start,
        )


def generate_hero_candidates(
    studio_product_path: Path,
    output_dir: Path,
    k: int = 3,
    max_parallel: int = 3,
    pinned_plate_id: Optional[str] = PINNED_PLATE_ID,
) -> HeroGenerationResult:
    """Photoroom 済の商品画像を入力に、AI 判定→top-k 合成を実行する.

    Args:
        studio_product_path: Photoroom /v2/edit 後の商品画像 (背景除去+studio化済)
        output_dir: 合成結果の保存先
        k: Gemini 合成する候補数 (default 3)
        max_parallel: 並列度 (default 3)

    Returns:
        HeroGenerationResult with analysis + k 個の CompositionResult
    """
    if not _FAL_OK:
        raise RuntimeError("fal_client not installed")
    if not os.environ.get("FAL_KEY"):
        raise RuntimeError("FAL_KEY not set")
    if not os.environ.get("GOOGLE_API_KEY"):
        raise RuntimeError("GOOGLE_API_KEY not set")

    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Gemini Vision で商品分析 + top-k plate 選定 (pinned があれば top-1 固定)
    logger.info("analyzing product image...")
    analysis, recs = analyze_and_select(
        studio_product_path, k=k, pinned_plate_id=pinned_plate_id,
    )
    logger.info(f"analysis: {analysis.product_shape} / {analysis.camera_elevation} "
                f"/ {analysis.product_facing} / rec_orient={analysis.recommended_orientation}")
    pinned_tag = f" [pinned={pinned_plate_id}]" if pinned_plate_id else ""
    logger.info(f"top-{k} plates{pinned_tag}: {[r.plate.id for r in recs]}")

    # 2. 商品画像を fal にアップロード (1 回のみ)
    product_url = fal_client.upload_file(str(studio_product_path))

    # 3. top-k を並列合成
    results: list[CompositionResult] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(_compose_single, product_url, r, output_dir): r
            for r in recs
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    # 順序を score 降順に戻す
    order = {r.plate.id: i for i, r in enumerate(recs)}
    results.sort(key=lambda x: order.get(x.plate_id, 999))

    return HeroGenerationResult(
        analysis=analysis,
        candidates=results,
        product_studio_path=studio_product_path,
    )
