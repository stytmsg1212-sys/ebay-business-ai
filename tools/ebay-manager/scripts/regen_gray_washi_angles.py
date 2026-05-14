"""グレー和紙 4 アングル再生成: 色をより gray に、角度を shape-based で強化."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.logo_plate_generator import PlateVariant, generate_all_plates

_COMMON_SUFFIX = (
    " Clean pure white background (#FFFFFF), soft overhead diffuse studio light, "
    "centered composition, plate occupies about 60% of frame. Extremely "
    "photorealistic, product catalog quality, only the enso brush-circle and the "
    "'MONO' wordmark appear on the plate, no other text or logos, no watermark."
)

_WASHI_GRAY_BASE = (
    "Create a photo-realistic brand plate on a pure white studio background. "
    "The plate is a SQUARE TAG approximately 8x8 cm with natural deckle-edge "
    "torn paper edges. "
    "MATERIAL (CRITICAL): The plate is made of HAND-DYED DARK GRAY mulberry fiber "
    "paper (traditional Japanese kuro-washi dyed with sumi ink). The paper color "
    "is a DECISIVELY DARK GRAY tone approximately RGB #5a5a5a, like wet slate stone "
    "or charcoal ash. The paper is NOT white, NOT off-white, NOT cream-colored - "
    "it is unambiguously DARK GRAY, similar to the color of a slate roof tile. "
    "Visible fibrous texture from the mulberry fibers but the overall paper tone "
    "is clearly dark gray. "
    "LOGO: Both the sumi-ink brush-stroke enso ring (upper portion) and the bold "
    "sans-serif 'MONO' wordmark (directly below the enso, correctly spelled "
    "M-O-N-O, oriented right-side-up) are printed in PURE BLACK sumi-ink that is "
    "darker than the gray paper and strongly contrasted. "
)

_ANGLES_FLAT_V2 = {
    "flat_top": (
        "CAMERA POSITION: pure top-down overhead shot, camera directly above the "
        "plate looking straight down. "
        "PLATE APPEARANCE IN FRAME: a PERFECT SQUARE with all four edges equal "
        "in length, no trapezoid, zero perspective distortion. The 'MONO' text "
        "reads horizontally from left to right at the bottom. "
        "SHADOW: very subtle non-directional soft shadow halo directly under the "
        "plate."
    ),
    "flat_high": (
        "CAMERA POSITION: elevated 3/4 view from above, tilted about 30 degrees "
        "off vertical. "
        "PLATE APPEARANCE IN FRAME: a GENTLE TRAPEZOID where the top (far) edge "
        "is approximately 88 percent of the length of the bottom (near) edge. "
        "The plate lies FLAT on the surface with mild foreshortening. "
        "SHADOW: soft contact shadow spreads slightly behind the plate (away "
        "from camera)."
    ),
    "flat_mid": (
        "CAMERA POSITION: 45-degree oblique view from above. "
        "PLATE APPEARANCE IN FRAME: a MODERATE TRAPEZOID where the top (far) edge "
        "is approximately 70 percent of the length of the bottom (near) edge, "
        "and the vertical extent of the plate in the image is compressed to about "
        "75 percent of the horizontal width of the bottom edge. "
        "SHADOW: soft contact shadow extends clearly behind and slightly away "
        "from the plate."
    ),
    "flat_low": (
        "CAMERA POSITION: LOW oblique view, camera only about 20 degrees above "
        "horizontal, looking ACROSS at the plate lying flat on the surface. "
        "PLATE APPEARANCE IN FRAME: a STRONGLY COMPRESSED TRAPEZOID where the "
        "top (far) edge is only about 45 percent of the length of the bottom "
        "(near) edge, and the vertical extent of the plate is dramatically "
        "compressed so the plate appears almost like a flat horizontal band with "
        "visible top surface and minimal height. This is an extreme foreshortened "
        "view. "
        "SHADOW: long soft elongated contact shadow extends far BEHIND the plate "
        "(away from camera into the depth of the frame)."
    ),
}


def build_variants() -> list[PlateVariant]:
    return [
        PlateVariant(
            id=f"washi_gray_{key}",
            label_ja=f"グレー和紙・{key}",
            label_en=f"Gray Washi Flat ({key})",
            prompt=_WASHI_GRAY_BASE + angle_desc + _COMMON_SUFFIX,
            recommended_for="直置き構図",
        )
        for key, angle_desc in _ANGLES_FLAT_V2.items()
    ]


if __name__ == "__main__":
    variants = build_variants()
    out_dir = Path(__file__).resolve().parent.parent / "assets" / "plate_variants" / "gray_library"
    print(f"regenerating {len(variants)} washi gray variants → {out_dir}")
    results = generate_all_plates(
        variants=variants, max_parallel=3, remove_bg=True, output_dir=out_dir
    )
    print("\n=== RESULTS ===")
    for r in results:
        status = "OK" if r.success else "FAIL"
        print(f"[{status}] {r.material_id:28s} {r.elapsed_sec:5.1f}s")
