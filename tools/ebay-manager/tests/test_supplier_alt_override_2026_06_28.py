"""#35 (2026-06-28): alt_only 候補の手動 override 採用 unit test.

テスト対象: tasks/task_supplier_apply.apply_supplier_candidate
- allow_alt_override=False (既定) → alt_only 候補はブロック (success=False)
- allow_alt_override=True  → alt_only ブロックをスキップして ReviseItem 経路へ進む
  (eBay 呼び出し自体は mock で止め、認証チェックより手前の段階まで確認)

既存挙動 (revive/replace 候補) は変更なし。
"""
from __future__ import annotations

import types
import unittest
from unittest.mock import MagicMock, patch

import sys
from pathlib import Path

# プロジェクト root を import path に追加
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tasks.task_supplier_apply import apply_supplier_candidate


# ---------------------------------------------------------------------------
# テスト用フィクスチャ
# ---------------------------------------------------------------------------

def _make_candidate(
    *,
    cid: int = 99,
    status: str = "accepted",
    match_score: int = 30,
    alt_listing_possible: int = 1,
    ebay_item_id: str = "111222333444",
    sku: str = "ebayyh_p9999999",
    candidate_url: str = "https://example.com/item/9999",
) -> dict:
    """テスト用 supplier_candidates 行 dict を返す。"""
    return {
        "id": cid,
        "status": status,
        "match_score": match_score,
        "alt_listing_possible": alt_listing_possible,
        "ebay_item_id": ebay_item_id,
        "sku": sku,
        "candidate_url": candidate_url,
        "candidate_title": "テスト商品",
        "availability_status": "available",
    }


def _make_listing(*, is_ended: int = 0) -> dict:
    return {
        "ebay_item_id": "111222333444",
        "is_ended": is_ended,
        "ended_at": None,
        "ended_reason": None,
        "current_price": 100.0,
    }


# ---------------------------------------------------------------------------
# テスト: alt_only ガード (allow_alt_override=False / 既定)
# ---------------------------------------------------------------------------

class TestAltOnlyBlock(unittest.TestCase):
    """allow_alt_override=False (既定) で alt_only 候補はブロックされる。"""

    @patch("tasks.task_supplier_apply.get_supplier_candidate_by_id")
    def test_alt_only_blocked_by_default(self, mock_get_cand):
        """score<60 + alt=1 の候補は既定でブロックされる。"""
        mock_get_cand.return_value = _make_candidate(match_score=30, alt_listing_possible=1)

        result = apply_supplier_candidate(candidate_id=99, config={})

        self.assertFalse(result["success"])
        self.assertIn("別SKU出品機会", result["message"])
        self.assertIn("新規出品フロー", result["message"])

    @patch("tasks.task_supplier_apply.get_supplier_candidate_by_id")
    def test_alt_only_blocked_explicit_false(self, mock_get_cand):
        """allow_alt_override=False を明示しても同様にブロック。"""
        mock_get_cand.return_value = _make_candidate(match_score=50, alt_listing_possible=1)

        result = apply_supplier_candidate(
            candidate_id=99, config={}, allow_alt_override=False
        )

        self.assertFalse(result["success"])
        self.assertIn("別SKU出品機会", result["message"])

    @patch("tasks.task_supplier_apply.get_supplier_candidate_by_id")
    def test_high_score_alt_not_blocked(self, mock_get_cand):
        """score>=60 + alt=1 は alt_only=False → ブロック対象外（通常 apply 経路）。

        注: このケースは実際には alt_listing_possible=1 でも score>=60 なら
        alt_only=False の扱いになる。ガード条件の正確な境界を確認。
        """
        # score=60 は alt_only=False (60 < 60 は False なので)
        mock_get_cand.return_value = _make_candidate(match_score=60, alt_listing_possible=1)

        # alt_only ブロックは通過するが、ebay_credentials_ok で失敗するはず
        with patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id") as mock_listing, \
             patch("tasks.task_supplier_apply.url_to_sku") as mock_url2sku, \
             patch("tasks.task_supplier_apply.get_ebay_credentials") as mock_creds, \
             patch("tasks.task_supplier_apply.ebay_credentials_ok") as mock_creds_ok:
            mock_listing.return_value = _make_listing()
            mock_url2sku.return_value = "ebayyh_p9999999"
            mock_creds.return_value = {}
            mock_creds_ok.return_value = False  # 認証失敗で止める

            result = apply_supplier_candidate(candidate_id=99, config={})

        # "別SKU出品機会" のブロックメッセージではなく、認証エラーになる
        self.assertFalse(result["success"])
        self.assertNotIn("別SKU出品機会", result["message"])
        self.assertIn("eBay 認証情報", result["message"])


# ---------------------------------------------------------------------------
# テスト: allow_alt_override=True で alt_only ブロックをスキップ
# ---------------------------------------------------------------------------

class TestAltOverrideEnabled(unittest.TestCase):
    """allow_alt_override=True で alt_only ブロックをスキップし ReviseItem 経路へ。"""

    @patch("tasks.task_supplier_apply.get_supplier_candidate_by_id")
    def test_override_passes_alt_guard(self, mock_get_cand):
        """override=True の時は alt_only ブロック段階を通過する (次段 = listing check)。"""
        mock_get_cand.return_value = _make_candidate(match_score=30, alt_listing_possible=1)

        with patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id") as mock_listing:
            # is_ended=0 の listing を返してガード通過を確認
            mock_listing.return_value = _make_listing(is_ended=0)

            with patch("tasks.task_supplier_apply.url_to_sku") as mock_url2sku, \
                 patch("tasks.task_supplier_apply.get_ebay_credentials") as mock_creds, \
                 patch("tasks.task_supplier_apply.ebay_credentials_ok") as mock_creds_ok:
                mock_url2sku.return_value = "ebayyh_p9999999"
                mock_creds.return_value = {}
                # 認証チェックで止める（ReviseItem は呼ばない）
                mock_creds_ok.return_value = False

                result = apply_supplier_candidate(
                    candidate_id=99, config={}, allow_alt_override=True
                )

        # "別SKU出品機会" ブロックメッセージが出ていない = alt_only ガードを通過した
        self.assertFalse(result["success"])
        self.assertNotIn("別SKU出品機会", result["message"])
        # 認証エラーに進んでいる
        self.assertIn("eBay 認証情報", result["message"])

    @patch("tasks.task_supplier_apply.get_supplier_candidate_by_id")
    @patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id")
    @patch("tasks.task_supplier_apply.url_to_sku")
    @patch("tasks.task_supplier_apply.get_ebay_credentials")
    @patch("tasks.task_supplier_apply.ebay_credentials_ok")
    @patch("tasks.task_supplier_apply.revise_item_sku")
    @patch("tasks.task_supplier_apply.update_ebay_listing_sku")
    @patch("tasks.task_supplier_apply.upsert_item")
    @patch("tasks.task_supplier_apply.update_supplier_candidate_status")
    @patch("tasks.task_supplier_apply.get_conn")
    def test_override_executes_revise_and_returns_success(
        self,
        mock_conn,
        mock_update_status,
        mock_upsert,
        mock_update_sku,
        mock_revise,
        mock_creds_ok,
        mock_creds,
        mock_url2sku,
        mock_listing,
        mock_get_cand,
    ):
        """override=True で ReviseItem が呼ばれ success が返る。メッセージに override 痕跡あり。"""
        mock_get_cand.return_value = _make_candidate(match_score=30, alt_listing_possible=1)
        mock_listing.return_value = _make_listing(is_ended=0)
        mock_url2sku.return_value = "ebayyh_p8888888"
        mock_creds.return_value = {
            "app_id": "app", "dev_id": "dev", "cert_id": "cert", "user_token": "token"
        }
        mock_creds_ok.return_value = True
        mock_revise.return_value = {"success": True, "message": "OK"}
        # get_conn context manager mock
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock())
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = mock_cm

        result = apply_supplier_candidate(
            candidate_id=99, config={}, allow_alt_override=True
        )

        self.assertTrue(result["success"], f"expected success, got: {result}")
        self.assertIn("別SKU手動override採用", result["message"])
        self.assertIn("ebayyh_p8888888", result["message"])
        # ReviseItem が呼ばれた
        mock_revise.assert_called_once()
        # DB 追従が呼ばれた
        mock_update_sku.assert_called_once_with("111222333444", "ebayyh_p8888888")
        mock_update_status.assert_called_once_with(99, "applied")

    @patch("tasks.task_supplier_apply.get_supplier_candidate_by_id")
    @patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id")
    @patch("tasks.task_supplier_apply.url_to_sku")
    @patch("tasks.task_supplier_apply.get_ebay_credentials")
    @patch("tasks.task_supplier_apply.ebay_credentials_ok")
    @patch("tasks.task_supplier_apply.revise_item_sku")
    def test_override_revise_failure_returns_error(
        self,
        mock_revise,
        mock_creds_ok,
        mock_creds,
        mock_url2sku,
        mock_listing,
        mock_get_cand,
    ):
        """ReviseItem が失敗した場合は success=False が返る (偽装成功なし)。"""
        mock_get_cand.return_value = _make_candidate(match_score=30, alt_listing_possible=1)
        mock_listing.return_value = _make_listing(is_ended=0)
        mock_url2sku.return_value = "ebayyh_p7777777"
        mock_creds.return_value = {
            "app_id": "app", "dev_id": "dev", "cert_id": "cert", "user_token": "token"
        }
        mock_creds_ok.return_value = True
        mock_revise.return_value = {"success": False, "message": "eBay API error"}

        result = apply_supplier_candidate(
            candidate_id=99, config={}, allow_alt_override=True
        )

        self.assertFalse(result["success"])
        self.assertIn("ReviseItem 失敗", result["message"])


# ---------------------------------------------------------------------------
# テスト: 他のガードは override=True でも維持される
# ---------------------------------------------------------------------------

class TestOtherGuardsRemain(unittest.TestCase):
    """allow_alt_override=True でも alt_only 以外のガードは全て機能する。"""

    @patch("tasks.task_supplier_apply.get_supplier_candidate_by_id")
    def test_status_guard_still_blocks(self, mock_get_cand):
        """status != 'accepted' は override=True でもブロック。"""
        mock_get_cand.return_value = _make_candidate(
            status="pending", match_score=30, alt_listing_possible=1
        )
        result = apply_supplier_candidate(
            candidate_id=99, config={}, allow_alt_override=True
        )
        self.assertFalse(result["success"])
        self.assertIn("status='accepted'", result["message"])

    @patch("tasks.task_supplier_apply.get_supplier_candidate_by_id")
    @patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id")
    def test_ended_listing_guard_still_blocks(self, mock_listing, mock_get_cand):
        """退役済 listing は override=True でもブロック。"""
        mock_get_cand.return_value = _make_candidate(match_score=30, alt_listing_possible=1)
        mock_listing.return_value = _make_listing(is_ended=1)
        result = apply_supplier_candidate(
            candidate_id=99, config={}, allow_alt_override=True
        )
        self.assertFalse(result["success"])
        self.assertIn("退役済", result["message"])

    @patch("tasks.task_supplier_apply.get_supplier_candidate_by_id")
    @patch("tasks.task_supplier_apply.get_ebay_listing_by_item_id")
    @patch("tasks.task_supplier_apply.url_to_sku")
    def test_bad_url_guard_still_blocks(self, mock_url2sku, mock_listing, mock_get_cand):
        """候補 URL から SKU を生成できない場合は override=True でもブロック。"""
        mock_get_cand.return_value = _make_candidate(match_score=30, alt_listing_possible=1)
        mock_listing.return_value = _make_listing(is_ended=0)
        mock_url2sku.return_value = ""  # 生成失敗

        result = apply_supplier_candidate(
            candidate_id=99, config={}, allow_alt_override=True
        )
        self.assertFalse(result["success"])
        self.assertIn("新SKUを生成できませんでした", result["message"])

    @patch("tasks.task_supplier_apply.get_supplier_candidate_by_id")
    def test_missing_candidate_blocks(self, mock_get_cand):
        """候補が存在しない場合は override=True でもブロック。"""
        mock_get_cand.return_value = None
        result = apply_supplier_candidate(
            candidate_id=999, config={}, allow_alt_override=True
        )
        self.assertFalse(result["success"])
        self.assertIn("見つかりません", result["message"])


# ---------------------------------------------------------------------------
# テスト: confirm フラグのキー形式を AST で確認
# ---------------------------------------------------------------------------

class TestConfirmKeyInSourceCode(unittest.TestCase):
    """tab_supplier_candidates.py に 2段確認フロー実装があることを静的検証。"""

    _SRC_PATH = (
        Path(__file__).resolve().parent.parent
        / "tabs"
        / "tab_supplier_candidates.py"
    )

    def _src(self) -> str:
        return self._SRC_PATH.read_text(encoding="utf-8")

    def test_confirm_key_defined(self):
        """_sup_confirm_alt_adopt_{cid} フラグが実装されている。"""
        src = self._src()
        self.assertIn(
            "_sup_confirm_alt_adopt_",
            src,
            "2段確認フローの confirm フラグキー '_sup_confirm_alt_adopt_{cid}' がない",
        )

    def test_warning_message_present(self):
        """確認警告メッセージが含まれている。"""
        src = self._src()
        self.assertIn(
            "別SKU候補です",
            src,
            "2段確認フローの warning メッセージ '別SKU候補です' がない",
        )
        self.assertIn(
            "SKU をこの候補 URL に書き換えて",
            src,
            "warning に SKU 書換内容の説明がない",
        )

    def test_confirm_button_present(self):
        """「確定（SKU書換で採用）」ボタンが実装されている。"""
        src = self._src()
        self.assertIn(
            "確定（SKU書換で採用）",
            src,
            "確定ボタンのラベルが存在しない",
        )

    def test_cancel_button_present(self):
        """「やめる」ボタンが実装されている。"""
        src = self._src()
        self.assertIn(
            "やめる",
            src,
            "やめるボタンが実装されていない",
        )

    def test_allow_alt_override_in_apply_call(self):
        """タブ内の apply 呼び出しに allow_alt_override=True が渡されている。"""
        src = self._src()
        self.assertIn(
            "allow_alt_override=True",
            src,
            "tab 側の apply_supplier_candidate 呼び出しに allow_alt_override=True がない",
        )

    def test_fragment_rerun_on_cancel(self):
        """「やめる」ボタン後は fragment scope で rerun。"""
        src = self._src()
        self.assertIn(
            'st.rerun(scope="fragment")',
            src,
            "やめるボタン後の rerun が fragment scope でない",
        )


if __name__ == "__main__":
    unittest.main()
