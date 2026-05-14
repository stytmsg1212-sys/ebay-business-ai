"""W115 v1.2 + v2 regression: mock 過多 test で signature mismatch を hide した事故防止.

経緯:
  - v1.2 (2026-05-10): `_upload_eps_and_revise` 内で `ebay_credentials_ok()` を引数なし呼出
    → TypeError でクラッシュ. user 報告で初めて発覚.
  - 既存 8 tests は `@patch("monitor.credentials.ebay_credentials_ok")` で mock 化、
    `MagicMock` が任意引数を受理するため signature mismatch を完全 hide.
  - 本ファイル: mock を **一切使わず**、実 import path で signature mismatch を catch.

戦略:
  - env 変数を unset → `get_ebay_credentials()` は空 dict → `ebay_credentials_ok(creds)` は False
  - この path を実 module で通せば、signature 不一致は TypeError で fail
  - 期待: success=False が返り、message に "credentials" を含む

H1 (2026-05-10 retrospective code-reviewer 指摘) の最小 fix.
追加 unit test (fetch_supplier_images_all / _do_supplier_additional_compose) は次 session 課題.
"""
from __future__ import annotations

import os
import pytest


@pytest.fixture
def clear_ebay_env(monkeypatch):
    """eBay 認証 env を全て unset (env-only fallback で空 creds になる)."""
    for var in ("EBAY_APP_ID", "EBAY_DEV_ID", "EBAY_CERT_ID", "EBAY_USER_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_upload_eps_and_revise_real_signature_no_typeerror(clear_ebay_env):
    """v1.2 regression: 実 import path で signature mismatch (TypeError) を catch.

    本テストの責務: `_upload_eps_and_revise` 内で `ebay_credentials_ok()` などの
    helper を呼ぶ際に signature mismatch が発生しないこと.

    本物の import path を通すため mock 一切なし.
    env 変数 setup 状態 (test 環境) によって 2 path のいずれか:
      (a) creds 揃ってる → EPS upload 試行 → file 不在 error (TypeError なし)
      (b) creds 不足  → credentials エラー (TypeError なし)
    どちらも success=False で dict 返却 (= signature OK).

    v1.2 bug 状態 (`ebay_credentials_ok()` 引数なし) なら TypeError が出て test fail.
    """
    from tabs._supplier_photo_pipeline import _upload_eps_and_revise

    # mock 一切なし、本物 import path
    # TypeError が出れば pytest が catch して test fail (= v1.2 regression 検出)
    result = _upload_eps_and_revise(
        candidate_id=99999,
        ebay_item_id="123456789012",
        hero_local_path="/tmp/nonexistent_path_for_test.png",
    )

    assert isinstance(result, dict), "戻り値は dict のはず"
    assert result.get('success') is False, (
        "存在しない file or creds 不足で必ず success=False"
    )
    assert 'message' in result, "message key が無いと UI エラー surface 不可"
    # 'credentials' OR 'EPS upload failed' OR 'file' OR 'ファイル' のいずれかを含むはず
    msg_lower = (result.get('message') or '').lower()
    assert any(kw in msg_lower for kw in ('credentials', 'eps upload', 'failed', 'file', 'ファイル')), (
        f"想定 error category 外: {result.get('message')!r}"
    )


def test_upload_eps_and_revise_v2_additional_paths_kwarg(clear_ebay_env):
    """v2 (multi-image) signature regression: additional_paths kwarg 受理確認.

    新 signature: (cid, eid, hero, additional_paths=None) → backward-compat 維持.
    旧 3-arg call が壊れていないこと + 4 番目 kwarg が TypeError にならないこと.
    """
    from tabs._supplier_photo_pipeline import _upload_eps_and_revise

    # 旧 3-arg call (backward-compat、TypeError なし)
    r1 = _upload_eps_and_revise(99999, "123456789012", "/tmp/nonexistent.png")
    assert isinstance(r1, dict)
    assert r1.get('success') is False  # file 不在 or credentials 不足

    # 新 4-arg call (additional_paths kwarg、TypeError なし)
    r2 = _upload_eps_and_revise(
        99999, "123456789012", "/tmp/nonexistent.png",
        additional_paths=["/tmp/add1.png", "/tmp/add2.png"],
    )
    assert isinstance(r2, dict)
    assert r2.get('success') is False


def test_fetch_supplier_images_all_invalid_url_returns_empty():
    """v2 fetch_supplier_images_all の境界: 無効 URL は [] を返す (silent skip 防止).

    Q0: 失敗時に logger.warning で痕跡保存 + 空 list 返却.
    """
    from tabs._supplier_photo_pipeline import fetch_supplier_images_all

    # invalid URL (scheme なし) → 空 list
    assert fetch_supplier_images_all("") == []
    assert fetch_supplier_images_all("not-a-url") == []
    assert fetch_supplier_images_all("ftp://example.com/x") == []
