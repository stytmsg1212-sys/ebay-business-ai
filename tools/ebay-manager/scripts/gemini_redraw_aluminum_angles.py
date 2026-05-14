"""Gemini nano-banana/edit でアルミ立て掛けプレートの 4 角度を再描画."""
from __future__ import annotations

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
BASE = ROOT / "assets" / "plate_variants" / "gray_library" / "alum_gray_stand_front_raw.png"
OUT_DIR = ROOT / "assets" / "plate_variants" / "gray_library"

PRESERVE = (
    "Preserve exactly the same brushed gray aluminum material (warm neutral gray "
    "anodized finish with fine vertical hairline texture), the same tall vertical "
    "plaque shape with rounded corners and beveled rim edge, the same laser-engraved "
    "enso brush-circle (upper portion) and 'MONO' wordmark (below the enso). "
    "Do not alter the material color, the enso, or the MONO text. "
    "Output on a clean pure white studio background."
)

ANGLES = {
    "stand_front": (
        "Show this exact same aluminum plaque standing UPRIGHT on a flat white "
        "surface, self-supporting. VIEW: directly facing the camera at 0 degrees "
        "rotation - perfectly symmetrical front view where the face of the plaque "
        "is square-on to the camera. Both vertical edges are equally visible. "
        "Soft contact shadow directly below the bottom edge. "
        + PRESERVE
    ),
    "stand_L45": (
        "Show this exact same aluminum plaque standing UPRIGHT on a flat white "
        "surface, self-supporting. VIEW: strong 3/4 angle, the plaque is rotated "
        "so the LEFT edge recedes significantly into the distance while the RIGHT "
        "edge is closer to the camera. You can see both the front face AND the "
        "right edge thickness of the plaque. The enso and MONO are visible but "
        "noticeably foreshortened on the left side. "
        + PRESERVE
    ),
    "stand_R45": (
        "Show this exact same aluminum plaque standing UPRIGHT on a flat white "
        "surface, self-supporting. VIEW: strong 3/4 angle in the mirror direction "
        "- the plaque is rotated so the RIGHT edge recedes into the distance while "
        "the LEFT edge is closer to the camera. You can see both the front face "
        "AND the left edge thickness. The enso and MONO are foreshortened on the "
        "right side. "
        + PRESERVE
    ),
    "stand_low_front": (
        "Show this exact same aluminum plaque standing UPRIGHT on a flat white "
        "surface, self-supporting. VIEW: from a LOW camera angle near the surface "
        "looking slightly upward at the plaque. The plaque appears tall and "
        "imposing with its top edge slightly above and its bottom close to the "
        "camera. The bottom edge and contact shadow are clearly visible at the "
        "front of the frame. Heroic upward perspective. "
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
    raw_out = OUT_DIR / f"alum_gray_{key}_gemini_raw.png"
    trans_out = OUT_DIR / f"alum_gray_{key}_gemini_transparent.png"
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
        raise FileNotFoundError(f"base not found: {BASE}")
    base_url = fal_client.upload_file(str(BASE))
    print(f"base url: {base_url}")
    print(f"redrawing {len(ANGLES)} angles via Gemini...")
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(redraw_one, k, p, base_url) for k, p in ANGLES.items()]
        results = [f.result() for f in futures]
    print("\n=== RESULTS ===")
    for key, ok, msg in results:
        status = "OK" if ok else "FAIL"
        print(f"[{status}] alum_gray_{key}_gemini: {msg}")


if __name__ == "__main__":
    main()
