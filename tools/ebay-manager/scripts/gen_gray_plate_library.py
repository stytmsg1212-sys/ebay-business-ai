"""グレー和紙(直置き 4 アングル) + グレーアルミ(立て掛け 5 アングル) 計 9 バリアント生成."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.logo_plate_generator import PlateVariant, generate_all_plates

_COMMON_SUFFIX = (
    " Clean pure white background (#FFFFFF), soft key light from upper-left, "
    "subtle fill light, centered composition, plate occupies about 65% of frame. "
    "Extremely photorealistic, high detail material texture, product catalog quality, "
    "only the MONO enso and 'MONO' wordmark appear on the plate, no other text or "
    "logos, no watermark."
)

_WASHI_BASE = (
    "Create a photo-realistic brand plate. The plate is a SQUARE TAG shape (1:1 "
    "aspect ratio, approximately 8x8 cm) with natural deckle-edge torn paper edges. "
    "The plate is made of warm neutral gray washi paper (muted gray approximately "
    "#9b9891 with subtle warm undertone, clearly visible fibrous washi texture with "
    "natural fiber inclusions). Both the sumi-ink brush-stroke enso ring (upper "
    "portion) and the bold sans-serif 'MONO' wordmark (directly below the enso) are "
    "printed in deep matte sumi-black ink onto the gray paper. The 'MONO' text is "
    "legible, correctly spelled M-O-N-O, proportional to the enso above it. "
)

_ALUMINUM_BASE = (
    "Create a photo-realistic brand plate. The plate is a TALL VERTICAL PLAQUE "
    "(approximately 5 cm wide x 12 cm tall, 5:12 aspect ratio) with slightly rounded "
    "corners and a subtle beveled rim edge. The plate is made of brushed anodized "
    "aluminum with a warm neutral gray finish (approximately #9a9a9a with very "
    "subtle warm undertone, visible fine vertical hairline brushed metal texture). "
    "Both the enso brush-circle (upper portion) and the bold sans-serif 'MONO' "
    "wordmark (directly below the enso) are deeply laser-engraved into the metal, "
    "revealing darker anthracite tones inside the engraved grooves. The 'MONO' text "
    "is legible, correctly spelled M-O-N-O, proportional to the enso above it. "
)

_ANGLES_FLAT = {
    "flat_top": (
        "Viewed from directly overhead (90 degrees top-down bird's-eye view). "
        "The plate appears as a perfect square with no perspective distortion. "
        "The plate lies flat on the white surface. Very subtle soft contact "
        "shadow directly below the plate."
    ),
    "flat_high": (
        "Viewed from 60 degrees above horizontal (30 degrees off vertical). "
        "The plate lies flat on the white surface and appears as a slightly "
        "perspective-compressed square where the far edge is marginally shorter "
        "than the near edge. Soft contact shadow around the plate."
    ),
    "flat_mid": (
        "Viewed from 45 degrees above horizontal. The plate lies flat on the "
        "white surface and appears as a clear trapezoid with the far edge "
        "noticeably compressed. Soft contact shadow extends behind the plate."
    ),
    "flat_low": (
        "Viewed from 30 degrees above horizontal (low-angle view of the plate "
        "lying flat). The plate appears as a strongly compressed trapezoid with "
        "dramatic foreshortening. Contact shadow extends behind and slightly "
        "to the side of the plate."
    ),
}

_ANGLES_STAND = {
    "stand_front": (
        "The plaque stands upright on a flat white surface, self-supporting "
        "without any wall. The plaque front face directly faces the camera "
        "(0 degrees rotation, symmetrical composition). Soft contact shadow "
        "directly below the bottom edge of the plaque."
    ),
    "stand_L30": (
        "The plaque stands upright on a flat white surface, self-supporting. "
        "The plaque is rotated 30 degrees so its left edge recedes and its "
        "right edge is closer to the camera (gentle 3/4 angle view). "
        "Soft contact shadow below the bottom edge."
    ),
    "stand_R30": (
        "The plaque stands upright on a flat white surface, self-supporting. "
        "The plaque is rotated 30 degrees in the opposite direction: its right "
        "edge recedes and its left edge is closer to the camera (mirrored 3/4 "
        "angle view). Soft contact shadow below the bottom edge."
    ),
    "stand_L60": (
        "The plaque stands upright on a flat white surface, self-supporting. "
        "The plaque is rotated 60 degrees showing a strong side-profile view "
        "where the right edge is much closer to the camera and the left edge "
        "recedes. Soft contact shadow below the bottom edge."
    ),
    "stand_R60": (
        "The plaque stands upright on a flat white surface, self-supporting. "
        "The plaque is rotated 60 degrees in the opposite direction, strong "
        "side-profile view where the left edge is closer to the camera and "
        "the right edge recedes. Soft contact shadow below the bottom edge."
    ),
}


def build_catalog() -> list[PlateVariant]:
    variants: list[PlateVariant] = []
    for key, angle_desc in _ANGLES_FLAT.items():
        variants.append(PlateVariant(
            id=f"washi_gray_{key}",
            label_ja=f"グレー和紙・{key}",
            label_en=f"Gray Washi Flat ({key})",
            prompt=_WASHI_BASE + angle_desc + _COMMON_SUFFIX,
            recommended_for="直置き構図",
        ))
    for key, angle_desc in _ANGLES_STAND.items():
        variants.append(PlateVariant(
            id=f"alum_gray_{key}",
            label_ja=f"グレーアルミ・{key}",
            label_en=f"Gray Aluminum Standing ({key})",
            prompt=_ALUMINUM_BASE + angle_desc + _COMMON_SUFFIX,
            recommended_for="立て掛け構図",
        ))
    return variants


if __name__ == "__main__":
    variants = build_catalog()
    out_dir = Path(__file__).resolve().parent.parent / "assets" / "plate_variants" / "gray_library"
    print(f"generating {len(variants)} variants → {out_dir}")
    for v in variants:
        print(f"  {v.id:25s} | {v.label_ja}")
    results = generate_all_plates(
        variants=variants, max_parallel=3, remove_bg=True, output_dir=out_dir
    )
    print("\n=== RESULTS ===")
    for r in results:
        status = "OK" if r.success else "FAIL"
        print(f"[{status}] {r.material_id:28s} {r.elapsed_sec:5.1f}s")
        if not r.success:
            print(f"    error: {r.error}")
