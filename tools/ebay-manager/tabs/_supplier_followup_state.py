#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""採用後フォローアップ session_state クリーンアップ ロジック (2026-06-11 H-1 抽出)。

streamlit 非依存の純関数として定義し、unit test から直接 import できるようにする。
tab_supplier_candidates.py の _close_supplier_followup は本関数を呼ぶ thin wrapper。
"""
from __future__ import annotations

from typing import Any, MutableMapping


def close_supplier_followup_state(
    session_state: MutableMapping[str, Any],
    cid: int,
) -> None:
    """cid に紐づく採用後フォローアップ欄の session_state キーを全消し。

    photo pipeline prefix: "sup_" (_SS in _supplier_photo_pipeline.py)
    desc pipeline prefix:  "sup_desc_pipeline_" (_SS in _supplier_description_pipeline.py)
    w158 image pipeline:   "sup_desc_pipeline_{cid}_w158_" (cid が中間位置)

    endswith(f"_{cid}") は直前が "_" 固定なので cid 11 vs 111 の誤爆なし。
    w158 キーは cid が中間なので startswith で別途捕捉する。
    """
    exact = [
        f"_sup_photo_prompt_{cid}", f"_sup_photo_open_inline_{cid}",
        f"_sup_desc_prompt_{cid}", f"_sup_desc_open_inline_{cid}",
        f"_sup_photo_meta_{cid}", f"_sup_msgs_{cid}",
    ]
    for k in exact:
        session_state.pop(k, None)

    suffix = f"_{cid}"
    w158_prefix = f"sup_desc_pipeline_{cid}_w158_"
    for k in list(session_state.keys()):
        # w158 キー: cid が中間位置のため endswith では一致しない → startswith で捕捉
        if k.startswith(w158_prefix):
            session_state.pop(k, None)
            continue
        if k.endswith(suffix) and (
            k.startswith("sup_hero_")
            or k.startswith("sup_additional_")
            or k.startswith("sup_apply_")
            or k.startswith("sup_all_image_urls_")
            or k.startswith("sup_btn_")
            or k.startswith("sup_desc_pipeline_")
        ):
            session_state.pop(k, None)
