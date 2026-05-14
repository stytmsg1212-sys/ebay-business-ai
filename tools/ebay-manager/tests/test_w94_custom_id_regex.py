"""W94 Phase 7 Day 1 incident pin: Anthropic Batch API custom_id regex compliance.

Anthropic Batch API rejects custom_id not matching ^[a-zA-Z0-9_-]{1,64}$ (HTTP 400).
production code が '|' 区切りで 5/3 02:30 batch を全件 reject させた事故 → '-' 区切りに修正.
本 test は production helper を直接呼んで実 platform 名・eid 形式で regex 適合を verify する.
"""
import re
from pathlib import Path

from tasks.task_supplier_sweep import _build_batch_custom_id

ANTHROPIC_BATCH_CUSTOM_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Production scrape platforms (tasks/task_supplier_sweep.py 内 platforms list と一致)
KNOWN_PLATFORMS = ("mercari", "yahoo_auctions", "paypay_furima")


def test_build_batch_custom_id_matches_anthropic_regex():
    """known platform x realistic eid x idx で regex match を verify."""
    eids = ["358212418625", "358485950991", "123456789012"]
    for eid in eids:
        for plat in KNOWN_PLATFORMS:
            for idx in (0, 5, 9, 99):
                cid = _build_batch_custom_id(eid, plat, idx)
                assert ANTHROPIC_BATCH_CUSTOM_ID_REGEX.match(cid), (
                    f"custom_id={cid!r} does not match Anthropic regex; "
                    "Batch API will reject the entire batch (400)."
                )


def test_build_batch_custom_id_within_64_char_limit():
    """Anthropic limit: 1-64 chars."""
    cid = _build_batch_custom_id("358212418625", "yahoo_auctions", 99)
    assert 1 <= len(cid) <= 64


def test_build_batch_custom_id_extreme_idx_safe():
    """idx が大きくなっても 64 char 以内に収まることを assert (H-2)."""
    # eid 12 char + plat 13 char + 区切り 2 char = 27 char ベース、idx に最大 ~37 char 余裕.
    cid = _build_batch_custom_id("123456789012", "paypay_furima", 999999)
    assert ANTHROPIC_BATCH_CUSTOM_ID_REGEX.match(cid)
    assert len(cid) <= 64


def test_build_batch_custom_id_unknown_platform_still_compliant():
    """将来 platform 名追加時のガード (英数 + underscore のみであれば適合) (H-2)."""
    for plat in ("rakuma", "amazon_jp", "ebay_jp"):
        cid = _build_batch_custom_id("358212418625", plat, 5)
        assert ANTHROPIC_BATCH_CUSTOM_ID_REGEX.match(cid), (
            f"future platform {plat!r} produces non-compliant custom_id={cid!r}"
        )


def test_no_pipe_separator_in_supplier_sweep_source():
    """W94 5/3 incident regression: production source が f-string with '|' 直書きしていない (H-3).

    helper bypass による直書き再発を grep で catch する低コスト investment.
    """
    project_root = Path(__file__).parent.parent
    src_file = project_root / "tasks" / "task_supplier_sweep.py"
    src = src_file.read_text(encoding="utf-8")
    pattern = re.compile(r'custom_id\s*=\s*f["\'][^"\']*\|')
    assert not pattern.search(src), (
        "tasks/task_supplier_sweep.py に '|' 区切りの custom_id 直書きが復活している. "
        "Anthropic Batch API regex 違反 (5/3 02:30 事故再発リスク). "
        "_build_batch_custom_id() helper を経由するか '-' 区切りを使うこと."
    )
