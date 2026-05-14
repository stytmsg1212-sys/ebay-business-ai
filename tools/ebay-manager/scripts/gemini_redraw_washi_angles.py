"""Gemini nano-banana/edit で washi_gray_flat_top をベースに 4 角度を写真として再解釈.

PIL perspective transform では影/照明が角度に連動しないため、
Gemini 画像編集で「同じプレートを別視点から撮影した写真」として再描画させる。
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fal_client
import httpx
import numpy as np
from dotenv import load_dotenv
from PIL import Image
from scipy.ndimage import binary_fill_holes

from monitor.image_bg_remover import remove_background

load_dotenv(Path('.env'))

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "assets" / "plate_variants" / "gray_library" / "washi_gray_flat_top_raw.png"
OUT_DIR = ROOT / "assets" / "plate_variants" / "gray_library"

PRESERVE = (
    "Preserve exactly the same gray washi paper material (dark gray mulberry fiber "
    "paper), same deckle-edge torn paper edges, same sumi-ink enso brush-circle, "
    "and the same 'MONO' wordmark positioned directly below the enso. "
    "Do not alter the material color, the enso, or the MONO text. "
    "Output on a clean pure white studio background."
)

ANGLES = {
    "flat_top": (
        "Show this exact same gray washi plate from DIRECTLY OVERHEAD (pure top-down "
        "bird's-eye view, 90 degrees from horizontal). The plate lies flat on the "
        "surface and appears as a perfect square with no perspective distortion. "
        "Soft non-directional ambient shadow halo directly under the plate. "
        + PRESERVE
    ),
    "flat_high": (
        "Show this exact same gray washi plate from a gentle elevated angle "
        "(camera about 60 degrees above horizontal, plate lying flat on a white "
        "studio surface). The plate appears as a GENTLE TRAPEZOID where the top "
        "(far) edge is approximately 85 percent the length of the bottom (near) "
        "edge. Soft contact shadow spreads slightly behind the plate. "
        + PRESERVE
    ),
    "flat_mid": (
        "Show this exact same gray washi plate from a 45-degree oblique angle "
        "(camera 45 degrees above horizontal looking down, plate lying flat). "
        "The plate appears as a MODERATE TRAPEZOID where the top edge is about "
        "70 percent of the bottom edge, and the vertical extent is compressed. "
        "Soft contact shadow extends clearly behind the plate. "
        + PRESERVE
    ),
    "flat_low": (
        "Show this exact same gray washi plate from a LOW ANGLE (camera only 25 "
        "degrees above horizontal, almost at the level of the plate surface, "
        "looking across the plate). The plate appears as a STRONGLY COMPRESSED "
        "TRAPEZOID where the top (far) edge is about 45 percent of the bottom "
        "(near) edge and the vertical extent is dramatically compressed so the "
        "plate looks like a flat band with visible foreshortened top surface. "
        "Long soft contact shadow extends far BEHIND the plate into the depth of "
        "the frame. "
        + PRESERVE
    ),
}


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)


def fix_transparency(raw_path: Path, out_path: Path) -> None:
    raw = Image.open(raw_path).convert("RGBA")
    arr = np.array(raw)
    r = remove_background(raw_path)
    if not r.success or r.image is None:
        raise RuntimeError(f"rembg failed: {r.error}")
    alpha_rembg = np.array(r.image.convert("RGBA").split()[-1])
    alpha_filled = binary_fill_holes(alpha_rembg > 128).astype(np.uint8) * 255
    result = arr.copy()
    result[:, :, 3] = alpha_filled
    Image.fromarray(result, "RGBA").save(out_path, "PNG")


def redraw_one(key: str, prompt: str, base_url: str) -> tuple[str, bool, str]:
    raw_out = OUT_DIR / f"washi_gray_{key}_gemini_raw.png"
    trans_out = OUT_DIR / f"washi_gray_{key}_gemini_transparent.png"
    try:
        resp = fal_client.subscribe(
            "fal-ai/nano-banana/edit",
            arguments={
                "prompt": prompt,
                "image_urls": [base_url],
                "num_images": 1,
                "output_format": "png",
            },
            with_logs=False,
        )
        imgs = resp.get("images") or []
        if not imgs:
            return (key, False, "no images")
        download(imgs[0]["url"], raw_out)
        fix_transparency(raw_out, trans_out)
        return (key, True, str(trans_out))
    except Exception as e:
        return (key, False, str(e))


def main() -> None:
    if not BASE.exists():
        raise FileNotFoundError(f"base plate not found: {BASE}")
    base_url = fal_client.upload_file(str(BASE))
    print(f"base url: {base_url}")
    print(f"redrawing {len(ANGLES)} angles via Gemini nano-banana/edit...")
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(redraw_one, k, p, base_url) for k, p in ANGLES.items()]
        results = [f.result() for f in futures]
    print("\n=== RESULTS ===")
    for key, ok, msg in results:
        status = "OK" if ok else "FAIL"
        print(f"[{status}] washi_gray_{key}_gemini: {msg}")


if __name__ == "__main__":
    main()
