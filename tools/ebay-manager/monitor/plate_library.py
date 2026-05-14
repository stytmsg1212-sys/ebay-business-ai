"""MonoHonpo 立体ロゴプレートライブラリ (8 枚固定).

直置き (flat) 4 アングル + 立て掛け (standing) 4 アングル = 計 8 枚。
各 plate は Gemini nano-banana/edit で再描画済で、角度に応じた影/照明が baked in。

命名規約:
  W1-W4: Washi (グレー和紙) 直置き
  S1-S4: Aluminum (グレーアルミ) 立て掛け

利用側は PLATE_LIBRARY["W3"] のように ID でアクセス。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

Orientation = Literal["flat", "stand"]
AngleTag = Literal[
    "top_down",       # W1: 真俯瞰
    "high_angle",     # W2: 軽い俯瞰 (60°)
    "mid_angle",      # W3: 45° 斜め
    "low_angle",      # W4: 強い foreshortening
    "front",          # S1: 正面対称
    "three_quarter_L",# S2: 3/4 左向き (強)
    "three_quarter_R",# S3: 3/4 右向き (弱め)
    "low_front",      # S4: 低角度正面
]

ROOT = Path(__file__).resolve().parent.parent
GRAY_LIB = ROOT / "assets" / "plate_variants" / "gray_library"


@dataclass(frozen=True)
class PlateSpec:
    id: str
    label_ja: str
    orientation: Orientation
    angle: AngleTag
    transparent_path: Path
    raw_path: Path
    description: str


PLATE_LIBRARY: dict[str, PlateSpec] = {
    # --- 直置き (Gray Washi, flat lying) ---
    "W1": PlateSpec(
        id="W1",
        label_ja="和紙・真俯瞰",
        orientation="flat",
        angle="top_down",
        transparent_path=GRAY_LIB / "washi_gray_flat_top_gemini_transparent.png",
        raw_path=GRAY_LIB / "washi_gray_flat_top_gemini_raw.png",
        description="商品を真上から撮った写真向け",
    ),
    "W2": PlateSpec(
        id="W2",
        label_ja="和紙・軽俯瞰",
        orientation="flat",
        angle="high_angle",
        transparent_path=GRAY_LIB / "washi_gray_flat_high_gemini_transparent.png",
        raw_path=GRAY_LIB / "washi_gray_flat_high_gemini_raw.png",
        description="60°程度の俯瞰写真向け",
    ),
    "W3": PlateSpec(
        id="W3",
        label_ja="和紙・45°斜め",
        orientation="flat",
        angle="mid_angle",
        transparent_path=GRAY_LIB / "washi_gray_flat_mid_gemini_transparent.png",
        raw_path=GRAY_LIB / "washi_gray_flat_mid_gemini_raw.png",
        description="45°俯瞰の標準商品写真向け",
    ),
    "W4": PlateSpec(
        id="W4",
        label_ja="和紙・低角度",
        orientation="flat",
        angle="low_angle",
        transparent_path=GRAY_LIB / "washi_gray_flat_low_gemini_transparent.png",
        raw_path=GRAY_LIB / "washi_gray_flat_low_gemini_raw.png",
        description="ほぼ目線高さから撮った商品写真向け",
    ),
    # --- 立て掛け (Gray Aluminum, standing upright) ---
    "S1": PlateSpec(
        id="S1",
        label_ja="アルミ・正面",
        orientation="stand",
        angle="front",
        transparent_path=GRAY_LIB / "alum_gray_stand_front_gemini_transparent.png",
        raw_path=GRAY_LIB / "alum_gray_stand_front_gemini_raw.png",
        description="商品が正面向きの写真向け",
    ),
    "S2": PlateSpec(
        id="S2",
        label_ja="アルミ・3/4左",
        orientation="stand",
        angle="three_quarter_L",
        transparent_path=GRAY_LIB / "alum_gray_stand_L45_gemini_transparent.png",
        raw_path=GRAY_LIB / "alum_gray_stand_L45_gemini_raw.png",
        description="商品が左向き 3/4 angle の写真向け",
    ),
    "S3": PlateSpec(
        id="S3",
        label_ja="アルミ・3/4右",
        orientation="stand",
        angle="three_quarter_R",
        transparent_path=GRAY_LIB / "alum_gray_stand_R45_gemini_transparent.png",
        raw_path=GRAY_LIB / "alum_gray_stand_R45_gemini_raw.png",
        description="商品が右向き 3/4 angle の写真向け",
    ),
    "S4": PlateSpec(
        id="S4",
        label_ja="アルミ・低角度",
        orientation="stand",
        angle="low_front",
        transparent_path=GRAY_LIB / "alum_gray_stand_low_front_gemini_transparent.png",
        raw_path=GRAY_LIB / "alum_gray_stand_low_front_gemini_raw.png",
        description="低い位置から商品を仰角で撮った写真向け",
    ),
}


# 実運用で採用する plate ID. 2026-04-23 確定:
#   - S1-S4 (立て掛け) は「横商品×縦板」の違和感で不採用
#   - W3 (mid 45°), W4 (low 強foreshortening) が最重要角度 (ユーザー検証で確定)
#   - W2 (軽俯瞰) は 3 候補目の variety 確保のため残す
#   - W1 (真俯瞰) のみ unused (真俯瞰は不自然なことが多い)
ACTIVE_PLATE_IDS: list[str] = ["W3", "W4", "W2"]

# pinned にすると AI 推奨に関わらず毎回 top-1 として採用される.
# W3 (mid) がユーザー検証で一番商品写真と調和すると確定.
# None にすれば AI が W3/W4 から自動選択.
PINNED_PLATE_ID: Optional[str] = "W3"


def get_plate(plate_id: str) -> PlateSpec:
    if plate_id not in PLATE_LIBRARY:
        raise KeyError(f"unknown plate_id: {plate_id}")
    return PLATE_LIBRARY[plate_id]


def active_plates() -> list[PlateSpec]:
    """実運用で採用中の plate のみ返す."""
    return [PLATE_LIBRARY[pid] for pid in ACTIVE_PLATE_IDS if pid in PLATE_LIBRARY]


def plates_by_orientation(orientation: Orientation) -> list[PlateSpec]:
    return [p for p in PLATE_LIBRARY.values() if p.orientation == orientation]


def validate_library() -> list[str]:
    """すべての plate ファイル存在確認. 欠落 ID を返す."""
    missing: list[str] = []
    for pid, spec in PLATE_LIBRARY.items():
        if not spec.transparent_path.exists():
            missing.append(f"{pid}: {spec.transparent_path}")
    return missing


if __name__ == "__main__":
    missing = validate_library()
    if missing:
        print("MISSING FILES:")
        for m in missing:
            print(f"  {m}")
    else:
        print(f"all {len(PLATE_LIBRARY)} plates present:")
        for pid, spec in PLATE_LIBRARY.items():
            print(f"  {pid}: {spec.label_ja:20s} [{spec.orientation:5s}/{spec.angle}]")
