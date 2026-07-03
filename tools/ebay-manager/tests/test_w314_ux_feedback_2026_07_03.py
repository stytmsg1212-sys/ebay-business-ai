"""W314 Phase 2 user フィードバック 3 点 (2026-07-03) の回帰テスト。

対象:
  #1 採用ボタンの 3 択化 (SKUのみ / 編集あり / 不採用):
     `adopt_candidate(open_editor=False)` は followup フラグ (`_sup_photo_prompt_` /
     `_sup_photo_meta_`) を set しない (SKU 切替のみ完了、パネル非展開)。
     `open_editor=True` (default) は従来通り set する。

  #2 旧 description プロンプトの撤去:
     `_supplier_followup_section.py` から「📝 はい、description も生成 / いいえ、
     後でやる」ブロックが撤去され、`_sup_desc_prompt_` / `_sup_desc_open_inline_` を
     set/read するコード (adopt + section 両方) が消えている。

  #3 「対応を完了」の未反映 dirty ガード (2 段階確認):
     `_get_dirty_field_labels(eid)` が pf_{eid}_* baseline / 現在値の差分を検出。
     1 回目の「完了」ボタンで `_sup_followup_discard_confirm_{cid}` を立てる、
     2 回目で cid session_state + panel state (pf_{eid}_*) を破棄して閉じる。

Streamlit runtime を必要とする render 本体はテストせず、純関数 / ソース結線 /
session_state 遷移で検証する (既存 followup テスト群と同方針)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_TABS = _PROJECT_ROOT / "tabs"


# ─────────────────────────────────────────────────────────────
# #1 採用ボタンの 3 択化 (adopt_candidate open_editor kwarg)
# ─────────────────────────────────────────────────────────────

_EID = "111222333444"


def _candidate(cid: int = 42) -> dict:
    return {
        "id": cid,
        "ebay_item_id": _EID,
        "candidate_url": "https://example.com/item/9999",
        "candidate_title": "テスト商品",
    }


def _listing(qty: int) -> dict:
    return {"ebay_item_id": _EID, "quantity_ebay": qty}


class _FakeSessionState(dict):
    """streamlit.session_state を dict で模擬する薄い wrapper."""


class TestAdoptCandidateOpenEditorKwarg(unittest.TestCase):
    """`adopt_candidate(open_editor=...)` が followup フラグ set を制御することを検証。"""

    def setUp(self) -> None:
        self.session_state = _FakeSessionState()
        self._st_patch = patch("streamlit.session_state", self.session_state)
        self._st_patch.start()
        self._bump_patch = patch("ui_cache.bump_db_version")
        self.mock_bump = self._bump_patch.start()

    def tearDown(self) -> None:
        self._st_patch.stop()
        self._bump_patch.stop()

    def _run_adopt(self, *, open_editor: bool, qty: int = 3):
        from tabs._adopt_candidate import adopt_candidate

        with patch("monitor.database.get_supplier_candidate_by_id",
                   return_value=_candidate()), \
             patch("monitor.database.get_ebay_listing_by_item_id",
                   return_value=_listing(qty=qty)), \
             patch("monitor.database.update_ebay_listing_quantity"), \
             patch("tasks.task_supplier_apply.accept_supplier_candidate",
                   return_value={"success": True, "message": "accepted"}), \
             patch("tasks.task_supplier_apply.apply_supplier_candidate",
                   return_value={"success": True, "message": "applied"}), \
             patch("monitor.ebay_client.revise_inventory_quantity",
                   return_value={"success": True, "message": "ok"}):
            return adopt_candidate(
                42, {}, source_tab="inventory",
                open_editor=open_editor,
            )

    def test_open_editor_true_sets_followup_flags(self):
        """open_editor=True (default) は followup フラグを set する (従来動作)。"""
        res = self._run_adopt(open_editor=True)
        self.assertTrue(res["success"])
        self.assertIn("_sup_photo_prompt_42", self.session_state)
        self.assertTrue(self.session_state["_sup_photo_prompt_42"])
        self.assertIn("_sup_photo_meta_42", self.session_state)
        self.assertEqual(self.session_state["_sup_photo_meta_42"]["eid"], _EID)
        self.assertEqual(
            self.session_state["_sup_photo_meta_42"]["title"], "テスト商品",
        )

    def test_open_editor_false_skips_followup_flags(self):
        """open_editor=False は followup フラグを一切 set しない (SKUのみ経路)。"""
        res = self._run_adopt(open_editor=False)
        self.assertTrue(res["success"], "SKUのみ経路も adopt は成功する")
        self.assertNotIn(
            "_sup_photo_prompt_42", self.session_state,
            "SKUのみ経路では `_sup_photo_prompt_` を set しない",
        )
        self.assertNotIn(
            "_sup_photo_meta_42", self.session_state,
            "SKUのみ経路では meta も set しない (パネル情報不要)",
        )

    def test_default_is_open_editor_true(self):
        """kwarg 未指定は open_editor=True (既存呼出の下位互換)。"""
        from tabs._adopt_candidate import adopt_candidate

        with patch("monitor.database.get_supplier_candidate_by_id",
                   return_value=_candidate()), \
             patch("monitor.database.get_ebay_listing_by_item_id",
                   return_value=_listing(qty=3)), \
             patch("monitor.database.update_ebay_listing_quantity"), \
             patch("tasks.task_supplier_apply.accept_supplier_candidate",
                   return_value={"success": True, "message": "accepted"}), \
             patch("tasks.task_supplier_apply.apply_supplier_candidate",
                   return_value={"success": True, "message": "applied"}), \
             patch("monitor.ebay_client.revise_inventory_quantity",
                   return_value={"success": True, "message": "ok"}):
            adopt_candidate(42, {}, source_tab="inventory")
        self.assertIn("_sup_photo_prompt_42", self.session_state)


class TestSkuOnlyStillRestoresQty(unittest.TestCase):
    """SKUのみ経路 (open_editor=False) でも revive の qty 0→1 復元は走る。

    open_editor は UI (パネル展開) の on/off だけを制御する。money-direct な
    在庫復元ロジックは UI とは独立に走る (旧 revive の挙動維持)。
    """

    def setUp(self) -> None:
        self.session_state = _FakeSessionState()
        self._st_patch = patch("streamlit.session_state", self.session_state)
        self._st_patch.start()
        self._bump_patch = patch("ui_cache.bump_db_version")
        self._bump_patch.start()

    def tearDown(self) -> None:
        self._st_patch.stop()
        self._bump_patch.stop()

    def test_sku_only_revive_still_restores_qty(self):
        from tabs._adopt_candidate import adopt_candidate

        with patch("monitor.database.get_supplier_candidate_by_id",
                   return_value=_candidate()), \
             patch("monitor.database.get_ebay_listing_by_item_id",
                   return_value=_listing(qty=0)), \
             patch("monitor.database.update_ebay_listing_quantity") as mock_upd_qty, \
             patch("tasks.task_supplier_apply.accept_supplier_candidate",
                   return_value={"success": True, "message": "accepted"}), \
             patch("tasks.task_supplier_apply.apply_supplier_candidate",
                   return_value={"success": True, "message": "applied"}), \
             patch("monitor.credentials.get_ebay_credentials",
                   return_value={"app_id": "a", "dev_id": "d",
                                 "cert_id": "c", "user_token": "t"}), \
             patch("monitor.credentials.ebay_credentials_ok", return_value=True), \
             patch("monitor.ebay_client.revise_inventory_quantity",
                   return_value={"success": True, "message": "ok"}) as mock_revise:
            res = adopt_candidate(
                42, {}, source_tab="inventory", open_editor=False,
            )
        self.assertTrue(res["success"])
        self.assertTrue(res["qty_restored"], "SKUのみでも revive は qty 復元")
        mock_revise.assert_called_once()
        mock_upd_qty.assert_called_once_with(_EID, 1)
        # ただし followup フラグは set しない
        self.assertNotIn("_sup_photo_prompt_42", self.session_state)


# ─────────────────────────────────────────────────────────────
# #2 旧 description プロンプトの撤去 (source wiring test)
# ─────────────────────────────────────────────────────────────

class TestDescPromptRemoval(unittest.TestCase):
    """description prompt (📝 はい/いいえ) ブロックが撤去されていることを検証。"""

    def test_section_no_longer_renders_desc_prompt_buttons(self):
        """`_supplier_followup_section.py` に「はい/いいえ」desc プロンプトが無い。

        docstring の履歴文言に "description も生成する？" が残るので、button の
        widget key と render_supplier_description_section 呼出という「実際に描画
        コードが残っているか」で検証する (K3: measurable な残存判定)。
        """
        src = (_TABS / "_supplier_followup_section.py").read_text(encoding="utf-8")
        # Step 1 の「はい」ボタン widget key
        assert '_sup_desc_yes_' not in src, (
            "旧 description prompt (Step 1) の「はい」ボタン widget key が残存。"
        )
        # Step 1 の「いいえ」ボタン widget key
        assert '_sup_desc_no_' not in src, (
            "旧 description prompt (Step 1) の「いいえ」ボタン widget key が残存。"
        )
        # Step 2 で inline 展開していた description pipeline 呼出。コメント内の
        # 言及 (「〜は個別出品タブ側で引き続き使う」等) は許容し、実際の import 文と
        # 関数呼出のみ検出する (K3: measurable な残存判定)。
        assert 'from tabs._supplier_description_pipeline import' not in src, (
            "description pipeline の import が残存 = Step 2 の呼出が残っている可能性。"
        )
        # 呼出行: 空白付きだと inline 展開の残存
        assert 'render_supplier_description_section(' not in src, (
            "description pipeline の関数呼出行が残存。"
        )
        # 「対応を完了」自体は残る (dirty ガード付き)
        assert "対応を完了" in src

    def test_section_no_longer_writes_desc_open_inline(self):
        """`_sup_desc_open_inline_` の書込 (旧 Step 1 → Step 2 遷移) が撤去されている。"""
        src = (_TABS / "_supplier_followup_section.py").read_text(encoding="utf-8")
        assert '_sup_desc_open_inline_{_fcid}"] = True' not in src, (
            "`_sup_desc_open_inline_` を True に set するコードが残存。"
        )
        # 収集キーとしての read (`_followup_cids` 収集) はフォールバック用に残る。
        # これは K2 surgical で温存 (default False で影響なし)。

    def test_adopt_candidate_no_longer_sets_desc_prompt(self):
        """`_adopt_candidate.py` が `_sup_desc_prompt_` を set していない。"""
        src = (_TABS / "_adopt_candidate.py").read_text(encoding="utf-8")
        assert 'st.session_state[f"_sup_desc_prompt_{cid}"] = True' not in src, (
            "adopt_candidate が `_sup_desc_prompt_` を set しています。"
            "user フィードバック #2 で撤去されたはず。"
        )
        # `_sup_photo_prompt_` は followup section の cid 収集キーとして依然必要
        assert 'st.session_state[f"_sup_photo_prompt_{cid}"] = True' in src


# ─────────────────────────────────────────────────────────────
# #3 「対応を完了」の未反映 dirty ガード
# ─────────────────────────────────────────────────────────────

class TestFinishingPanelDirtyDetection(unittest.TestCase):
    """`_get_dirty_field_labels(eid)` が pf_{eid}_* dirty を検出することを検証。"""

    def setUp(self) -> None:
        self.session_state = _FakeSessionState()
        self._st_patch = patch("streamlit.session_state", self.session_state)
        self._st_patch.start()

    def tearDown(self) -> None:
        self._st_patch.stop()

    def test_no_baseline_returns_empty(self):
        """パネルが 1 度も render されていない (baseline なし) = dirty ゼロ。"""
        from tabs._supplier_followup_section import _get_dirty_field_labels
        self.assertEqual(_get_dirty_field_labels(_EID), [])

    def test_baseline_equal_current_returns_empty(self):
        """baseline と現在値が同じなら dirty ゼロ。"""
        from tabs._supplier_followup_section import _get_dirty_field_labels
        self.session_state[f"pf_{_EID}_title_initial"] = "Same Title"
        self.session_state[f"pf_{_EID}_title"] = "Same Title"
        self.assertEqual(_get_dirty_field_labels(_EID), [])

    def test_title_diff_detected(self):
        """title の baseline vs 現在値差分を検出。"""
        from tabs._supplier_followup_section import _get_dirty_field_labels
        self.session_state[f"pf_{_EID}_title_initial"] = "Old Title"
        self.session_state[f"pf_{_EID}_title"] = "New Title"
        labels = _get_dirty_field_labels(_EID)
        self.assertIn("タイトル", labels)

    def test_quantity_diff_detected(self):
        """quantity の baseline vs 現在値差分を検出。"""
        from tabs._supplier_followup_section import _get_dirty_field_labels
        self.session_state[f"pf_{_EID}_quantity_initial"] = 5
        self.session_state[f"pf_{_EID}_quantity"] = 10
        labels = _get_dirty_field_labels(_EID)
        self.assertIn("数量", labels)

    def test_multiple_dirty_fields(self):
        """複数フィールド dirty の順序 (DISPATCH_FIELD_ORDER に沿う)。"""
        from tabs._supplier_followup_section import _get_dirty_field_labels
        self.session_state[f"pf_{_EID}_title_initial"] = "Old"
        self.session_state[f"pf_{_EID}_title"] = "New"
        self.session_state[f"pf_{_EID}_rank_initial"] = "A"
        self.session_state[f"pf_{_EID}_rank"] = "B"
        self.session_state[f"pf_{_EID}_quantity_initial"] = 3
        self.session_state[f"pf_{_EID}_quantity"] = 5
        labels = _get_dirty_field_labels(_EID)
        # DISPATCH_FIELD_ORDER = ("title", "description", "rank", "quantity")
        self.assertEqual(labels, ["タイトル", "ランク", "数量"])


class TestClearFinishingPanelState(unittest.TestCase):
    """`_clear_finishing_panel_state(eid)` が pf_{eid}_* を全消しすることを検証。"""

    def setUp(self) -> None:
        self.session_state = _FakeSessionState()
        self._st_patch = patch("streamlit.session_state", self.session_state)
        self._st_patch.start()

    def tearDown(self) -> None:
        self._st_patch.stop()

    def test_clear_removes_all_pf_keys_for_eid(self):
        from tabs._supplier_followup_section import _clear_finishing_panel_state
        self.session_state[f"pf_{_EID}_title_initial"] = "X"
        self.session_state[f"pf_{_EID}_title"] = "Y"
        self.session_state[f"pf_{_EID}_quantity"] = 5
        self.session_state["pf_other_eid_title"] = "Z"
        _clear_finishing_panel_state(_EID)
        self.assertNotIn(f"pf_{_EID}_title_initial", self.session_state)
        self.assertNotIn(f"pf_{_EID}_title", self.session_state)
        self.assertNotIn(f"pf_{_EID}_quantity", self.session_state)
        # 他 eid は残存 (K2 surgical、eid 単位のクリア)
        self.assertIn("pf_other_eid_title", self.session_state)


class TestDirtyGuardWiringInSection(unittest.TestCase):
    """`_supplier_followup_section.py` に dirty ガード結線が入っている。"""

    def test_section_source_uses_dirty_guard(self):
        """section が dirty ラベル取得 + confirm フラグ + panel state clear を持つ。"""
        src = (_TABS / "_supplier_followup_section.py").read_text(encoding="utf-8")
        # 1 回目押下で立てる確認フラグ
        assert "_sup_followup_discard_confirm_" in src, (
            "破棄確認フラグ (`_sup_followup_discard_confirm_{cid}`) が section に無い。"
        )
        # dirty ラベル取得と warning 生成
        assert "_get_dirty_field_labels" in src
        assert "未反映の変更" in src, "1 回目押下時の warning 文言が section に無い。"
        # 2 回目押下でパネル state も破棄
        assert "_clear_finishing_panel_state" in src, (
            "パネル state 破棄 (`_clear_finishing_panel_state`) の呼出が section に無い。"
        )


# ─────────────────────────────────────────────────────────────
# 3-way ボタン結線 (source wiring test、ボタン増減の regression 検出)
# ─────────────────────────────────────────────────────────────

class TestThreeWayAdoptButtons(unittest.TestCase):
    """在庫監視 + 仕入先候補タブの採用ボタンが 3 択化されていること。"""

    def test_inventory_tab_has_sku_only_and_editor_buttons(self):
        src = (_TABS / "tab_inventory_monitor.py").read_text(encoding="utf-8")
        assert "SKUのみ" in src, "在庫監視タブに「SKUのみ」ボタンが無い"
        assert "編集あり" in src, "在庫監視タブに「編集あり」ボタンが無い"
        assert "open_editor=False" in src, (
            "在庫監視タブから open_editor=False を渡す経路が無い"
        )
        assert "open_editor=True" in src, (
            "在庫監視タブから open_editor=True (パネル展開) を渡す経路が無い"
        )

    def test_supplier_tab_has_sku_only_and_editor_buttons(self):
        src = (_TABS / "tab_supplier_candidates.py").read_text(encoding="utf-8")
        assert "SKUのみ" in src, "仕入先候補タブに「SKUのみ」ボタンが無い"
        assert "編集あり" in src, "仕入先候補タブに「編集あり」ボタンが無い"
        assert "open_editor=False" in src or 'open_editor=(_choice == "editor")' in src

    def test_supplier_tab_alt_only_confirm_passes_choice(self):
        """alt_only の 2 段確認が選択 (SKUのみ/編集あり) を引き継ぐ (K1)。"""
        src = (_TABS / "tab_supplier_candidates.py").read_text(encoding="utf-8")
        assert "_sup_confirm_alt_choice_" in src, (
            "alt-override 確認の choice 保存キーが無い"
        )


if __name__ == "__main__":
    unittest.main()
