#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W75 4b: run_supplier_candidate_search signature change regression.

旧: `run_supplier_candidate_search(sku, config, ...)` で内部 `get_ebay_listing_by_sku(sku)` lookup
新: `run_supplier_candidate_search(ebay_item_id, sku, config, ...)` で `get_ebay_listing_by_item_id(eid)` lookup

SKU rule (.claude/rules/sku-rules.md) 準拠 = listing 識別 canonical key を ebay_item_id に統一.
8 callers (app.py x2 / task_inventory_check / task_supplier_sweep / test x4) 全て移行済.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def test_signature_requires_ebay_item_id_first():
    """関数 signature の第一引数が ebay_item_id である (旧 sku 主導から変更済)."""
    from tasks.task_supplier_candidate_search import run_supplier_candidate_search
    sig = inspect.signature(run_supplier_candidate_search)
    params = list(sig.parameters.keys())
    assert params[0] == "ebay_item_id", (
        f"first param must be 'ebay_item_id' (canonical listing key), got {params!r}"
    )
    assert "sku" in params, "sku は依然として必要 (Claude prompt / log 表示用)"


def test_lookup_uses_get_ebay_listing_by_item_id():
    """内部 lookup が canonical 関数 get_ebay_listing_by_item_id を呼ぶ."""
    from tasks import task_supplier_candidate_search as t
    # module 内に canonical 関数が import されている
    assert hasattr(t, "get_ebay_listing_by_item_id"), (
        "get_ebay_listing_by_item_id が module level に import されていない"
    )
    # 旧 deprecated 関数の import は削除済
    assert not hasattr(t, "get_ebay_listing_by_sku"), (
        "get_ebay_listing_by_sku の import が残っている (削除すべき)"
    )


def test_lookup_failure_mentions_ebay_item_id_not_sku(monkeypatch):
    """listing 不在時のエラーメッセージが ebay_item_id を提示 (sku ではない)."""
    from tasks import task_supplier_candidate_search as t
    monkeypatch.setattr(t, "get_ebay_listing_by_item_id", lambda eid: None)
    r = t.run_supplier_candidate_search(
        ebay_item_id="999999", sku="any_sku", config={},
        platforms=[], discovered_via="test",
    )
    assert r["success"] is False
    assert "ebay_item_id" in r["message"], (
        f"error message should reference ebay_item_id, got: {r['message']!r}"
    )
    assert "999999" in r["message"], "提示された eid 値がメッセージに含まれること"


def test_old_sku_only_call_pattern_breaks_loudly():
    """旧 sku-only 呼出 `run_supplier_candidate_search(sku=...)` は TypeError で fail.

    silent skip (ebay_item_id を default にされて誤動作) 防止. caller 移行漏れを
    pytest で検出可能にする.
    """
    from tasks.task_supplier_candidate_search import run_supplier_candidate_search
    with pytest.raises(TypeError):
        # 旧 caller pattern: sku のみ keyword 指定 (ebay_item_id 不在)
        run_supplier_candidate_search(sku="test", config={}, discovered_via="test")


def test_callers_pass_ebay_item_id_static_check():
    """主要 caller (app.py / sweep / inventory_check) が ebay_item_id= 指定で呼んでいる static check."""
    files_to_check = [
        "app.py",
        "tasks/task_supplier_sweep.py",
        "tasks/task_inventory_check.py",
    ]
    for rel_path in files_to_check:
        src = (_PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
        if "run_supplier_candidate_search" not in src:
            continue
        # 旧 sku 単独呼出 pattern が残っていない
        assert "run_supplier_candidate_search(sku=" not in src, (
            f"{rel_path}: old `run_supplier_candidate_search(sku=...)` 呼出が残存. "
            "ebay_item_id= も併せて指定してください."
        )
        # _run_cs_bulk / _run_cs alias 経由の場合も同様に check
        assert "_run_cs_bulk(sku=" not in src, (
            f"{rel_path}: `_run_cs_bulk(sku=...)` 旧呼出が残存"
        )
        assert "_run_cs(sku=" not in src, (
            f"{rel_path}: `_run_cs(sku=...)` 旧呼出が残存"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
