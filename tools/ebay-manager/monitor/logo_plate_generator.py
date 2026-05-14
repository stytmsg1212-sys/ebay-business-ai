"""MonoHonpo 立体ロゴプレート生成モジュール.

fal.ai Flux Pro Kontext で MONO 円相ロゴを参照画像として受け取り、
素材別 (和紙/真鍮/墨石/杉材/陶片/鉄錆) の正方形タグプレートを生成する。

出力は assets/plate_variants/{material_id}_raw.png (直接出力) と
{material_id}_transparent.png (背景除去版) の 2 ファイル。

使用例:
    from monitor.logo_plate_generator import generate_all_plates
    results = generate_all_plates()
    for r in results:
        print(f"{r.material_id}: success={r.success} path={r.transparent_path}")
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

# .env 経由の key 読込
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

# Flux Pro Kontext Max: 高品質 image-to-image edit (~$0.08/image)
# 通常 Kontext より微細な質感再現が良く、立体プレートのような
# 物理シミュレーション要素で効果が顕著。
MODEL_ID = "fal-ai/flux-pro/kontext/max"

ROOT = Path(__file__).resolve().parent.parent
LOGO_PATH = ROOT / "assets" / "monohonpo_logo_transparent.png"
OUTPUT_DIR = ROOT / "assets" / "plate_variants" / "v2"


@dataclass
class PlateVariant:
    """素材別プレートの仕様."""
    id: str
    label_ja: str
    label_en: str
    prompt: str
    recommended_for: str  # 想定用途カテゴリ


@dataclass
class PlateGenerationResult:
    """生成結果."""
    material_id: str
    success: bool
    raw_path: Optional[Path] = None
    transparent_path: Optional[Path] = None
    error: Optional[str] = None
    elapsed_sec: float = 0.0
    image_url: Optional[str] = None


# 共通プロンプト prefix/suffix (全素材で構図/照明/ロゴ構造を統一)
_COMMON_PREFIX = (
    "Create a physical, photo-realistic brand plate. The plate must reproduce "
    "BOTH elements from the reference logo image exactly: "
    "(1) the circular sumi-ink brush-stroke enso ring on the upper portion, and "
    "(2) the wordmark text 'MONO' in clean bold modern sans-serif capitals "
    "positioned directly below the enso. Both elements are rendered as surface "
    "features on the plate (engraved, embossed, branded, painted, or inlaid "
    "depending on the material). The 'MONO' text must be legible, correctly "
    "spelled M-O-N-O, and proportional to the enso above it. "
)

_COMMON_SUFFIX = (
    " Studio product photography, soft key light from upper-left at 45 degrees, "
    "soft fill, subtle rim light, natural contact shadow on surface below the plate. "
    "Shot straight-on with 5-degree downward tilt. Clean pure white background (#FFFFFF), "
    "centered composition, plate occupies about 70% of frame. "
    "Extremely photorealistic, high detail material texture, product catalog quality, "
    "only the MONO enso and the MONO wordmark appear on the plate, "
    "no other text or logos, no watermark."
)


_SHAPE_SQUARE = "The plate is a SQUARE tag (1:1 aspect ratio, about 8cm x 8cm) with slightly rounded corners. "
_SHAPE_CIRCLE = "The plate is a CIRCULAR COIN shape (about 8cm diameter), perfectly round, with a thin beveled rim edge. "
_SHAPE_BAR = "The plate is a HORIZONTAL RECTANGULAR NAMEPLATE (about 12cm wide x 6cm tall, 2:1 aspect ratio), with subtly rounded corners, reminiscent of an industrial equipment nameplate. "
_SHAPE_HANKO = "The plate is a HANKO-STYLE SQUARE (about 7cm x 7cm) with distinctly chamfered 45-degree beveled edges, like a traditional Japanese seal stone. "
_SHAPE_VERTICAL = "The plate is a VERTICAL RECTANGULAR TAG (about 6cm wide x 10cm tall, 3:5 aspect ratio) with softly rounded corners, like a museum specimen label. "
_SHAPE_HEX = "The plate is a HEXAGONAL BADGE (regular hexagon, about 8cm across flats), like an industrial parts badge. "
_SHAPE_VERTICAL_TALL = "The plate is a TALL VERTICAL PLAQUE (about 5cm wide x 12cm tall, 5:12 aspect ratio), like a book spine label. "
_SHAPE_ROUND_RECT = "The plate is a ROUNDED RECTANGLE (about 10cm wide x 7cm tall, 10:7 aspect ratio) with generously rounded corners, evoking a vintage museum tag. "


PLATE_CATALOG: list[PlateVariant] = [
    PlateVariant(
        id="washi_square",
        label_ja="和紙・正方形タグ",
        label_en="Washi Square Tag",
        prompt=(
            _COMMON_PREFIX + _SHAPE_SQUARE
            + "The plate is made of natural washi paper (unbleached off-white color #f6f2ea, "
            "visible fibrous paper texture with subtle fiber inclusions). "
            "Both the enso and 'MONO' wordmark are printed in matte sumi black ink, "
            "or blind-embossed (debossed) with visible depth shadow. "
            "Edges of the tag show natural deckle-edge torn paper character."
            + _COMMON_SUFFIX
        ),
        recommended_for="書籍/アート/文房具 (default brand)",
    ),
    PlateVariant(
        id="brass_circle",
        label_ja="真鍮・丸コイン",
        label_en="Aged Brass Circle Coin",
        prompt=(
            _COMMON_PREFIX + _SHAPE_CIRCLE
            + "The plate is solid aged brass with dark patina (antique brass color #8b7355, "
            "darker oxidation in the recessed areas, brushed hairline metal texture). "
            "Both the enso and 'MONO' wordmark are deeply engraved with visible tool marks "
            "and slight verdigris (green copper oxide) inside the engraving grooves. "
            "A thin polished beveled rim runs around the coin edge."
            + _COMMON_SUFFIX
        ),
        recommended_for="プレミアム一般/時計/貴金属",
    ),
    PlateVariant(
        id="sumi_bar",
        label_ja="墨石・横長ネームプレート",
        label_en="Sumi Stone Nameplate Bar",
        prompt=(
            _COMMON_PREFIX + _SHAPE_BAR
            + "The plate is carved from polished black slate stone (sumi-ink black #1a1817, "
            "subtle mineral grain, semi-matte satin finish). The enso sits on the left portion "
            "and 'MONO' wordmark is horizontally aligned to its right (or both stacked vertically). "
            "Both are laser-etched, revealing lighter matte gray tones inside the etched marks. "
            "Chamfered edges with a thin polished rim line. Industrial nameplate proportions."
            + _COMMON_SUFFIX
        ),
        recommended_for="機械/測定器/計測機器 (industrial)",
    ),
    PlateVariant(
        id="cedar_hanko",
        label_ja="杉材・印章型 (chamfered)",
        label_en="Cedar Hanko Chamfered Square",
        prompt=(
            _COMMON_PREFIX + _SHAPE_HANKO
            + "The plate is polished natural Japanese cedar wood (warm blonde tone, vertical "
            "grain pattern). Both the enso and 'MONO' wordmark are burned into the wood with a "
            "traditional yakin branding iron, creating charred dark-brown burn marks with slightly "
            "raised carbon ridges at the edges. The hanko-style chamfered 45-degree bevels show "
            "clean end-grain on the cut faces. Sanded matte finish."
            + _COMMON_SUFFIX
        ),
        recommended_for="生活用品/キッチン/手仕事",
    ),
    PlateVariant(
        id="ceramic_vertical",
        label_ja="陶片朱焼・縦長タグ",
        label_en="Ceramic Vertical Tag",
        prompt=(
            _COMMON_PREFIX + _SHAPE_VERTICAL
            + "The plate is fired ceramic (white porcelain body, subtle crackle glaze, slight "
            "off-white warm tone). Both the enso and 'MONO' wordmark are painted in vermillion "
            "red glaze (shu color #a8341b, traditional Japanese hanko red) and fired into the "
            "surface, giving a slightly raised glossy vermillion finish with subtle edge bleed. "
            "Hand-formed slightly uneven tall rectangular shape shows the handmade character."
            + _COMMON_SUFFIX
        ),
        recommended_for="文房具/アクセサリー/茶道具",
    ),
    PlateVariant(
        id="corten_hex",
        label_ja="鉄錆 corten・六角バッジ",
        label_en="Corten Steel Hexagonal Badge",
        prompt=(
            _COMMON_PREFIX + _SHAPE_HEX
            + "The plate is weathered corten steel (industrial rust patina #8b4a2a with darker "
            "crust variations and subtle texture mottling). Both the enso and 'MONO' wordmark are "
            "laser-cut-stamped into the steel, revealing cleaner darker metal beneath the rust "
            "layer inside the stamps, contrasting with the surrounding patina. Sharp machined stamp "
            "precision against the organic rust. Industrial wabi-sabi aesthetic."
            + _COMMON_SUFFIX
        ),
        recommended_for="機械/工具/自動車パーツ",
    ),
    PlateVariant(
        id="brass_vertical",
        label_ja="真鍮・縦長プラーク",
        label_en="Brass Vertical Book-Spine Plaque",
        prompt=(
            _COMMON_PREFIX + _SHAPE_VERTICAL_TALL
            + "The plate is polished aged brass (warm golden tone #c9a15f with patina in recessed "
            "areas, brushed vertical hairline). Both the enso (near the top) and 'MONO' wordmark "
            "(below the enso) are deeply engraved, dark patina settled inside the grooves. "
            "The tall vertical form resembles a book-spine label or library plaque. "
            "A thin polished rim runs around the plaque edge."
            + _COMMON_SUFFIX
        ),
        recommended_for="書籍/アート/図鑑",
    ),
    PlateVariant(
        id="slate_rounded",
        label_ja="黒石・丸角ミュージアムタグ",
        label_en="Dark Slate Rounded Museum Tag",
        prompt=(
            _COMMON_PREFIX + _SHAPE_ROUND_RECT
            + "The plate is polished dark charcoal slate (deep graphite #2b2825, soft matte "
            "finish, subtle mineral flecks). Both the enso (on the left) and 'MONO' wordmark "
            "(on the right) are precision-laser-etched with a delicate brushed silver color "
            "inlay, giving a refined museum exhibit plaque feel. Generously rounded corners "
            "give a soft elegant silhouette. Thin polished edge bevel."
            + _COMMON_SUFFIX
        ),
        recommended_for="カメラ/レンズ/骨董",
    ),
]


def _upload_logo() -> str:
    """ロゴ画像を fal CDN にアップロードして URL を返す.

    fal_client.upload_file はファイルパスを受け取り public URL を返す。
    同じファイルを複数回 upload しても dedup される想定。
    """
    if not LOGO_PATH.exists():
        raise FileNotFoundError(f"logo not found: {LOGO_PATH}")
    if not _FAL_OK:
        raise RuntimeError("fal_client not installed")
    url = fal_client.upload_file(str(LOGO_PATH))
    logger.info(f"logo uploaded: {url}")
    return url


def _download(url: str, dest: Path, timeout: float = 60.0) -> None:
    """生成結果を URL からダウンロードして保存."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)


def _remove_bg(raw_path: Path, transparent_path: Path) -> None:
    """背景除去のハイブリッド方式: 外形は rembg (shadow footprint も綺麗に除去)、
    内部穴 (enso ring 中央など) は scipy.ndimage.binary_fill_holes で塞ぐ.

    rembg 単独だと enso の中心も背景判定されて穴が空き、
    border-connected flood fill 単独だと plate 接地影の淡いグレーが残る。
    両方の弱点を相互に補完する。
    """
    import numpy as np
    from scipy.ndimage import binary_fill_holes
    from monitor.image_bg_remover import remove_background

    raw = Image.open(raw_path).convert("RGBA")
    raw_arr = np.array(raw)
    r = remove_background(raw_path)
    if not r.success or r.image is None:
        raise RuntimeError(f"rembg failed: {r.error}")
    alpha_rembg = np.array(r.image.convert("RGBA").split()[-1])
    alpha_bool = alpha_rembg > 128
    alpha_filled = binary_fill_holes(alpha_bool).astype(np.uint8) * 255
    result = raw_arr.copy()
    result[:, :, 3] = alpha_filled
    Image.fromarray(result, "RGBA").save(transparent_path, "PNG")


def generate_plate(
    variant: PlateVariant,
    logo_url: str,
    output_dir: Path = OUTPUT_DIR,
    remove_bg: bool = True,
) -> PlateGenerationResult:
    """1 素材の立体プレートを生成する."""
    start = time.time()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / f"{variant.id}_raw.png"
    transparent_path = output_dir / f"{variant.id}_transparent.png"

    try:
        logger.info(f"[{variant.id}] submitting to {MODEL_ID}...")
        result = fal_client.subscribe(
            MODEL_ID,
            arguments={
                "prompt": variant.prompt,
                "image_url": logo_url,
                "guidance_scale": 3.5,
                "num_images": 1,
                "output_format": "png",
                "aspect_ratio": "1:1",
                "safety_tolerance": "2",
            },
            with_logs=False,
        )

        images = result.get("images") or []
        if not images:
            return PlateGenerationResult(
                material_id=variant.id,
                success=False,
                error=f"no images in response: {result}",
                elapsed_sec=time.time() - start,
            )

        image_url = images[0].get("url")
        if not image_url:
            return PlateGenerationResult(
                material_id=variant.id,
                success=False,
                error="no image URL in first result",
                elapsed_sec=time.time() - start,
            )

        _download(image_url, raw_path)
        logger.info(f"[{variant.id}] raw saved: {raw_path}")

        if remove_bg:
            try:
                _remove_bg(raw_path, transparent_path)
                logger.info(f"[{variant.id}] transparent saved: {transparent_path}")
            except Exception as e:
                logger.warning(f"[{variant.id}] bg removal failed (keeping raw): {e}")
                transparent_path = None

        return PlateGenerationResult(
            material_id=variant.id,
            success=True,
            raw_path=raw_path,
            transparent_path=transparent_path,
            elapsed_sec=time.time() - start,
            image_url=image_url,
        )

    except Exception as e:
        logger.exception(f"[{variant.id}] generation failed")
        return PlateGenerationResult(
            material_id=variant.id,
            success=False,
            error=str(e),
            elapsed_sec=time.time() - start,
        )


def generate_all_plates(
    variants: Optional[list[PlateVariant]] = None,
    max_parallel: int = 3,
    remove_bg: bool = True,
    output_dir: Optional[Path] = None,
) -> list[PlateGenerationResult]:
    """全素材のプレートを並列生成する.

    max_parallel はデフォルト 3 (fal.ai のレート上限とサーバ負荷のバランス)。
    FAL_KEY 環境変数が必要。
    """
    if not _FAL_OK:
        raise RuntimeError("fal_client not installed; run `pip install fal-client`")
    if not os.environ.get("FAL_KEY"):
        raise RuntimeError("FAL_KEY env var not set")

    variants = variants or PLATE_CATALOG
    out_dir = output_dir or OUTPUT_DIR
    logo_url = _upload_logo()

    results: list[PlateGenerationResult] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(generate_plate, v, logo_url, out_dir, remove_bg): v
            for v in variants
        }
        for fut in as_completed(futures):
            v = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                results.append(
                    PlateGenerationResult(
                        material_id=v.id, success=False, error=str(e)
                    )
                )

    results.sort(key=lambda r: [v.id for v in variants].index(r.material_id))
    return results
