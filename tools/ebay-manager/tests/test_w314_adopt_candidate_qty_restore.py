"""W314 Phase 3 T1 (2026-07-03): adopt_candidate qty 復元経路の回帰テスト。

code-reviewer HIGH-1 対応: adopt_candidate の qty 0→1 復元判定は
`_qty_before == 0` の実測値に基づくが、旧実装との整合性を保つため
`allow_alt_override=True` (別SKU候補の手動採用) では復元しない。旧
`tab_supplier_candidates.py` sup_accept_alt_confirm 経路は apply 成功後
followup フラグ set のみで revise_inventory_quantity を呼ばなかった
(HEAD 逐語確認済、money-direct 挙動)。

カバレッジ:
  1. revive (apply 前 qty=0)         → 復元される  (qty_restored=True)
  2. replace (apply 前 qty>=1)        → 復元されない (revise_inventory_quantity 未呼出)
  3. alt_override (qty=0 + override)  → 復元されない (別商品リスク回避)
  4. apply 失敗                       → 早期 return、followup フラグ set も qty 復元も無し
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tabs._adopt_candidate import adopt_candidate  # noqa: E402


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
    """streamlit.session_state を dict で模擬する薄い wrapper.

    streamlit.session_state は attribute access もサポートするが、adopt_candidate
    実装は subscript のみを使うため dict で十分。
    """


class _AdoptCandidateTestBase(unittest.TestCase):
    """共通 monkey patch セット (streamlit / DB / apply / bump_db_version)。"""

    def setUp(self) -> None:
        self.session_state = _FakeSessionState()
        # streamlit.session_state を dict で置換 (adopt_candidate は import streamlit as st)
        self._st_patch = patch("streamlit.session_state", self.session_state)
        self._st_patch.start()
        # bump_db_version は副作用のみ (呼出は許容、DB キャッシュ無効化)
        self._bump_patch = patch("ui_cache.bump_db_version")
        self.mock_bump = self._bump_patch.start()

    def tearDown(self) -> None:
        self._st_patch.stop()
        self._bump_patch.stop()


class TestReviveQtyRestore(_AdoptCandidateTestBase):
    """apply 前 qty=0 の listing は 0→1 に自動復元される (revive)。"""

    def test_revive_restores_qty_to_one(self):
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
            res = adopt_candidate(42, {}, source_tab="inventory")

        self.assertTrue(res["success"])
        self.assertTrue(res["qty_restored"], "revive で qty_restored=True 必須")
        self.assertTrue(res["qty_restore_ok"])
        self.assertIn("0 → 1", res["qty_restore_message"])
        mock_revise.assert_called_once_with(_EID, 1, app_id="a", dev_id="d",
                                            cert_id="c", user_token="t")
        mock_upd_qty.assert_called_once_with(_EID, 1)
        # followup フラグ set 検証 (両タブが依存)
        self.assertTrue(self.session_state["_sup_photo_prompt_42"])
        self.assertTrue(self.session_state["_sup_desc_prompt_42"])


class TestReplaceNoQtyRestore(_AdoptCandidateTestBase):
    """apply 前 qty>=1 の listing は復元しない (replace 経路)。"""

    def test_replace_does_not_call_revise(self):
        with patch("monitor.database.get_supplier_candidate_by_id",
                   return_value=_candidate()), \
             patch("monitor.database.get_ebay_listing_by_item_id",
                   return_value=_listing(qty=3)), \
             patch("monitor.database.update_ebay_listing_quantity") as mock_upd_qty, \
             patch("tasks.task_supplier_apply.accept_supplier_candidate",
                   return_value={"success": True, "message": "accepted"}), \
             patch("tasks.task_supplier_apply.apply_supplier_candidate",
                   return_value={"success": True, "message": "applied"}), \
             patch("monitor.ebay_client.revise_inventory_quantity") as mock_revise:
            res = adopt_candidate(42, {}, source_tab="supplier")

        self.assertTrue(res["success"])
        self.assertFalse(res["qty_restored"])
        self.assertIsNone(res["qty_restore_ok"])
        self.assertIsNone(res["qty_restore_message"])
        mock_revise.assert_not_called()
        mock_upd_qty.assert_not_called()
        # followup フラグは set される
        self.assertTrue(self.session_state["_sup_photo_prompt_42"])


class TestAltOverrideNoQtyRestore(_AdoptCandidateTestBase):
    """alt_override=True + qty=0 でも復元しない (別商品リスク回避、旧挙動維持)。"""

    def test_alt_override_qty_zero_no_restore(self):
        with patch("monitor.database.get_supplier_candidate_by_id",
                   return_value=_candidate()), \
             patch("monitor.database.get_ebay_listing_by_item_id",
                   return_value=_listing(qty=0)), \
             patch("monitor.database.update_ebay_listing_quantity") as mock_upd_qty, \
             patch("tasks.task_supplier_apply.accept_supplier_candidate",
                   return_value={"success": True, "message": "accepted"}), \
             patch("tasks.task_supplier_apply.apply_supplier_candidate",
                   return_value={"success": True, "message": "applied"}), \
             patch("monitor.ebay_client.revise_inventory_quantity") as mock_revise:
            res = adopt_candidate(
                42, {}, source_tab="supplier", allow_alt_override=True,
            )

        self.assertTrue(res["success"])
        self.assertFalse(
            res["qty_restored"],
            "alt override では qty 復元しない (別商品リスク、旧挙動維持)",
        )
        self.assertIsNone(res["qty_restore_ok"])
        self.assertIsNone(res["qty_restore_message"])
        mock_revise.assert_not_called()
        mock_upd_qty.assert_not_called()
        # 採用成功なので followup フラグは set される (旧 alt-override も set していた)
        self.assertTrue(self.session_state["_sup_photo_prompt_42"])
        self.assertTrue(self.session_state["_sup_desc_prompt_42"])


class TestApplyFailureNoSideEffects(_AdoptCandidateTestBase):
    """apply 失敗時: 早期 return、followup フラグ set も qty 復元も呼ばれない。"""

    def test_apply_failure_returns_early(self):
        with patch("monitor.database.get_supplier_candidate_by_id",
                   return_value=_candidate()), \
             patch("monitor.database.get_ebay_listing_by_item_id",
                   return_value=_listing(qty=0)), \
             patch("monitor.database.update_ebay_listing_quantity") as mock_upd_qty, \
             patch("tasks.task_supplier_apply.accept_supplier_candidate",
                   return_value={"success": True, "message": "accepted"}), \
             patch("tasks.task_supplier_apply.apply_supplier_candidate",
                   return_value={"success": False, "message": "eBay API error"}), \
             patch("monitor.ebay_client.revise_inventory_quantity") as mock_revise:
            res = adopt_candidate(42, {}, source_tab="inventory")

        self.assertFalse(res["success"])
        self.assertEqual(res["stage"], "apply")
        self.assertIn("eBay API error", res["message"])
        self.assertFalse(res["qty_restored"])
        self.assertIsNone(res["qty_restore_ok"])
        self.assertIsNone(res["qty_restore_message"])
        mock_revise.assert_not_called()
        mock_upd_qty.assert_not_called()
        # followup フラグ set も走らない (session_state に残らない)
        self.assertNotIn("_sup_photo_prompt_42", self.session_state)
        self.assertNotIn("_sup_desc_prompt_42", self.session_state)
        self.assertNotIn("_sup_photo_meta_42", self.session_state)


if __name__ == "__main__":
    unittest.main()
