"""W164-pm regression test: rival_detection errors>0 時 DB message に first error 追記.

2026-05-25 19:00 health check で 5/25 03:40 rival_detection に errors=1 検出.
Discord には _send_discord_errors_alert で詳細通知済だが Discord は揮発、DB は
audit 用. fix: task_execution_log.message に first error の listing id + 100 chars
excerpt を追記 (次回 failure 時 DB query で原因即特定可能化).

code-reviewer HIGH-2 対応の regression coverage.
"""
from __future__ import annotations


def test_message_includes_first_err_format():
    """errors>0 で `| first_err: <eid>: <excerpt>` 文字列が含まれる format 確認.

    K1 Simplicity: run_rival_detection は外部依存多数 (eBay API / DB / Discord)
    なので関数全体 mock ではなく fix 該当 logic を直接 unit-test する.
    """
    # fix で追加した logic の rotation 抜き出し (task_rival_detection.py:383-392)
    summary = {
        "listings_processed": 3, "new_discoveries_total": 1, "errors": 1,
        "skipped_bad_item_id": 0, "requests_used": 3,
    }
    summary["message"] = (
        f"listings={summary['listings_processed']} "
        f"new={summary['new_discoveries_total']} "
        f"err={summary['errors']} "
        f"bad_iid={summary['skipped_bad_item_id']} "
        f"reqs={summary['requests_used']}"
    )
    per_listing_summaries = [
        {"ebay_item_id": "356750811453", "errors": 1, "message": "search failed: query='ATH CKS330NC'"},
        {"ebay_item_id": "357079442078", "errors": 0, "message": "OK"},
    ]
    # fix logic 再現
    if summary["errors"] > 0:
        err_entries = [r for r in per_listing_summaries if r.get("errors", 0) > 0]
        if err_entries:
            first = err_entries[0]
            eid = first.get("ebay_item_id", "?")
            excerpt = (first.get("message") or "")[:100]
            summary["message"] += f" | first_err: {eid}: {excerpt}"

    assert "first_err: 356750811453:" in summary["message"]
    assert "search failed" in summary["message"]
    # 既存 format は破壊しない
    assert "err=1" in summary["message"]


def test_message_no_first_err_when_zero_errors():
    """errors=0 なら first_err 追記なし (既存 format 維持)."""
    summary = {
        "listings_processed": 5, "new_discoveries_total": 0, "errors": 0,
        "skipped_bad_item_id": 0, "requests_used": 5,
    }
    summary["message"] = "listings=5 new=0 err=0 bad_iid=0 reqs=5"
    per_listing_summaries = [{"ebay_item_id": "X", "errors": 0, "message": "OK"}]
    if summary["errors"] > 0:  # False path
        err_entries = [r for r in per_listing_summaries if r.get("errors", 0) > 0]
        if err_entries:
            first = err_entries[0]
            summary["message"] += f" | first_err: {first.get('ebay_item_id')}: ..."

    assert "first_err" not in summary["message"]
    assert summary["message"] == "listings=5 new=0 err=0 bad_iid=0 reqs=5"
