"""バグ3/4 修正 (2026-06-11): close_supplier_followup_state の unit test.

検証内容:
  1. 対象 cid の exact キーが消える
  2. 対象 cid の pipeline prefix キー (sup_hero_*, sup_desc_pipeline_* 等) が消える
  3. 対象 cid の w158 キー (sup_desc_pipeline_{cid}_w158_*) が消える
  4. 他 cid のキーは残る
  5. cid suffix 境界テスト (cid=1 を閉じても cid=11 / cid=21 は消えない)

H-1 fix (2026-06-11): _make_close_fn (手書き複製) を削除し、
本物の close_supplier_followup_state を import して検証する。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tabs._supplier_followup_state import close_supplier_followup_state  # noqa: E402


def _build_session_state(*cids: int) -> dict:
    """複数 cid 分の session_state を模倣する dict を返す."""
    ss: dict = {}
    for cid in cids:
        # tab_supplier_candidates.py が管理する exact キー
        ss[f"_sup_photo_prompt_{cid}"] = True
        ss[f"_sup_photo_open_inline_{cid}"] = True
        ss[f"_sup_desc_prompt_{cid}"] = True
        ss[f"_sup_desc_open_inline_{cid}"] = True
        ss[f"_sup_photo_meta_{cid}"] = {"url": "https://example.com", "eid": "123", "title": "Test"}
        ss[f"_sup_msgs_{cid}"] = []
        # photo pipeline キー (_SS = "sup_")
        ss[f"sup_hero_candidates_{cid}"] = []
        ss[f"sup_hero_studio_path_{cid}"] = "/tmp/studio.png"
        ss[f"sup_hero_source_url_{cid}"] = "https://example.com/img.jpg"
        ss[f"sup_hero_selected_path_{cid}"] = None
        ss[f"sup_additional_processed_{cid}"] = []
        ss[f"sup_apply_result_{cid}"] = {"success": True}
        ss[f"sup_all_image_urls_{cid}"] = []
        ss[f"sup_btn_compose_{cid}"] = False
        # desc pipeline キー (_SS = "sup_desc_pipeline_")
        ss[f"sup_desc_pipeline_gen_result_{cid}"] = None
        ss[f"sup_desc_pipeline_apply_result_{cid}"] = None
        ss[f"sup_desc_pipeline_prefetch_{cid}"] = None
        ss[f"sup_desc_pipeline_rank_override_{cid}"] = None
        # w158 image pipeline キー (cid が中間位置 — M-1 fix 対象)
        ss[f"sup_desc_pipeline_{cid}_w158_last_url"] = "https://example.com/item"
        ss[f"sup_desc_pipeline_{cid}_w158_studio_path"] = "/tmp/w158.png"
    return ss


def test_close_removes_exact_keys_for_target_cid():
    """exact キー 6 個が消える."""
    ss = _build_session_state(42)
    close_supplier_followup_state(ss, 42)

    exact_keys = [
        "_sup_photo_prompt_42", "_sup_photo_open_inline_42",
        "_sup_desc_prompt_42", "_sup_desc_open_inline_42",
        "_sup_photo_meta_42", "_sup_msgs_42",
    ]
    for k in exact_keys:
        assert k not in ss, f"exact key should be removed: {k}"


def test_close_removes_pipeline_keys_for_target_cid():
    """pipeline prefix キーが消える."""
    ss = _build_session_state(42)
    close_supplier_followup_state(ss, 42)

    pipeline_keys = [
        "sup_hero_candidates_42",
        "sup_hero_studio_path_42",
        "sup_hero_source_url_42",
        "sup_hero_selected_path_42",
        "sup_additional_processed_42",
        "sup_apply_result_42",
        "sup_all_image_urls_42",
        "sup_btn_compose_42",
        "sup_desc_pipeline_gen_result_42",
        "sup_desc_pipeline_apply_result_42",
        "sup_desc_pipeline_prefetch_42",
        "sup_desc_pipeline_rank_override_42",
    ]
    for k in pipeline_keys:
        assert k not in ss, f"pipeline key should be removed: {k}"


def test_close_removes_w158_keys_for_target_cid():
    """w158 キー (cid が中間位置) が消える — M-1 fix の回帰テスト."""
    ss = _build_session_state(42)
    close_supplier_followup_state(ss, 42)

    w158_keys = [
        "sup_desc_pipeline_42_w158_last_url",
        "sup_desc_pipeline_42_w158_studio_path",
    ]
    for k in w158_keys:
        assert k not in ss, f"w158 key should be removed: {k}"


def test_close_does_not_affect_other_cid():
    """他 cid のキーは残る (バグ4 干渉防止の前提)."""
    ss = _build_session_state(10, 20)
    close_supplier_followup_state(ss, 10)

    # cid=20 のキーが残っていること
    remaining_keys = [k for k in ss if str(20) in k]
    assert len(remaining_keys) > 0, "cid=20 keys should remain after closing cid=10"

    # 念のため個別確認
    assert "_sup_photo_prompt_20" in ss
    assert "sup_hero_candidates_20" in ss
    assert "sup_desc_pipeline_gen_result_20" in ss
    assert "sup_desc_pipeline_20_w158_last_url" in ss


def test_close_does_not_affect_other_cid_w158():
    """他 cid の w158 キーは残る."""
    ss = _build_session_state(10, 20)
    close_supplier_followup_state(ss, 10)

    assert "sup_desc_pipeline_20_w158_last_url" in ss, "cid=20 w158 key should survive closing cid=10"
    assert "sup_desc_pipeline_10_w158_last_url" not in ss, "cid=10 w158 key should be removed"


def test_close_no_cross_contamination_cid_suffix():
    """cid=1 を閉じても cid=11 / cid=21 は消えない (suffix 境界テスト)."""
    ss = _build_session_state(1, 11, 21)
    close_supplier_followup_state(ss, 1)

    # cid=1 は消えている
    assert "_sup_photo_prompt_1" not in ss

    # cid=11, cid=21 は生存
    assert "_sup_photo_prompt_11" in ss, "cid=11 should survive closing cid=1"
    assert "_sup_photo_prompt_21" in ss, "cid=21 should survive closing cid=1"
    assert "sup_hero_candidates_11" in ss
    assert "sup_desc_pipeline_gen_result_11" in ss
