"""health-check auto-fix Phase 0 の回帰 test.

カバー:
- v57 migration 冪等性 (init_db 2 回連続でデータ保持 / PRAGMA user_version=57)
  → `.claude/rules/db-migration-rules.md` Q2
- classify_finding が各 finding kind を正しい Tier に振り分ける (純関数)
- 監査ログ helper (record_attempt の status 検証 / count_attempts_today の
  skipped 除外ループガード / record_db_proposal の dedup)
"""
from __future__ import annotations

import pytest

# conftest.py の autouse fixture が monitor.database.DB_PATH を tmp に隔離する
# (本番 monitor.db 遮断)。各 test は init_db() で schema を生成する。


def _init():
    from monitor.database import init_db
    init_db()


# ---------------------------------------------------------------------------
# v57 migration 冪等性
# ---------------------------------------------------------------------------
def test_v57_user_version_bumped():
    _init()
    from monitor.database import get_conn
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver >= 57, f"user_version が 57 未満: {ver}"


def test_v57_tables_exist():
    _init()
    from monitor.database import get_conn
    with get_conn() as c:
        names = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('autofix_attempt_log', 'autofix_db_proposal')"
        ).fetchall()}
    assert names == {"autofix_attempt_log", "autofix_db_proposal"}


def test_v57_idempotent_preserves_data():
    """init_db を 2 回連続実行してもデータが消えない (冪等性 / Q2)."""
    _init()
    from monitor.health_autofix_log import record_attempt
    log_id = record_attempt(
        "hash_idempotent", "tier1", "missed_task", "rerun", "attempted",
        target_task_key="daily_relist")
    assert log_id >= 1
    _init()  # 再実行 = 起動毎呼び出しを模擬。DROP/wipe があればここで消える
    from monitor.database import get_conn
    with get_conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM autofix_attempt_log WHERE finding_hash=?",
            ("hash_idempotent",)).fetchone()[0]
    assert n == 1, "init_db 再実行でデータ消失 = 冪等性違反"


# ---------------------------------------------------------------------------
# classify_finding (純関数、DB 不要)
# ---------------------------------------------------------------------------
def _green() -> dict:
    """異常 0 件のヘルスチェック結果."""
    return {
        "success": True, "missed_count": 0, "missed": [],
        "coverage": {"coverable": 0, "dlq": 0, "dlq_skus": [],
                     "coverage_alert_sent": False},
        "url_divergence": {"divergence_count": 0, "divergent_ids": [],
                           "alert_sent": False},
        "phase_c": {"intermittent": [], "orphans": [], "db_locks": 0,
                    "subprocess_errors": [], "alert_sent": False},
    }


def _kinds(result: list[dict]) -> dict:
    """kind -> tier の dict に畳む (assert 用)."""
    return {f["kind"]: f["tier"] for f in result}


def test_classify_all_green_empty():
    from tasks.task_health_autofix import classify_finding
    assert classify_finding(_green()) == []


def test_classify_missed_task_tier1():
    from tasks.task_health_autofix import classify_finding, TIER1
    h = _green()
    h["missed"] = [{"task_key": "daily_relist", "expected_hour": 2}]
    out = classify_finding(h)
    assert len(out) == 1
    assert out[0]["tier"] == TIER1
    assert out[0]["kind"] == "missed_task"
    assert out[0]["target_task_key"] == "daily_relist"


def test_classify_orphan_tier1():
    from tasks.task_health_autofix import classify_finding, TIER1
    h = _green()
    h["phase_c"]["orphans"] = [
        {"task_key": "ebay_sync", "batch_id": "b1", "started_at": "2026-05-29 02:30:00"}]
    out = classify_finding(h)
    assert _kinds(out) == {"orphan_task": TIER1}
    assert out[0]["target_task_key"] == "ebay_sync"


def test_classify_intermittent_tier1():
    from tasks.task_health_autofix import classify_finding, TIER1
    h = _green()
    h["phase_c"]["intermittent"] = [
        {"task_key": "inventory_check", "count": 4, "last_at": "2026-05-29 12:00:00"}]
    out = classify_finding(h)
    assert _kinds(out) == {"intermittent_failure": TIER1}


def test_classify_subprocess_error_tier2():
    from tasks.task_health_autofix import classify_finding, TIER2
    h = _green()
    h["phase_c"]["subprocess_errors"] = [
        {"task_key": "daily_codex_lint", "started_at": "2026-05-29 03:00:00",
         "message": "returncode=1 ..."}]
    out = classify_finding(h)
    assert _kinds(out) == {"subprocess_error": TIER2}


def test_classify_url_divergence_tier3():
    from tasks.task_health_autofix import classify_finding, TIER3
    h = _green()
    h["url_divergence"] = {"divergence_count": 3,
                           "divergent_ids": ["a", "b", "c"], "alert_sent": False}
    out = classify_finding(h)
    assert _kinds(out) == {"url_divergence": TIER3}


def test_classify_coverage_gap_tier3():
    from tasks.task_health_autofix import classify_finding, TIER3
    h = _green()
    h["coverage"]["coverable"] = 5
    out = classify_finding(h)
    assert _kinds(out) == {"coverage_gap": TIER3}


def test_classify_dlq_escalate():
    from tasks.task_health_autofix import classify_finding, ESCALATE
    h = _green()
    h["coverage"]["dlq"] = 2
    h["coverage"]["dlq_skus"] = ["ebayxx_1", "ebayxx_2"]
    out = classify_finding(h)
    assert _kinds(out) == {"coverage_dlq": ESCALATE}


def test_classify_db_lock_spike_escalate():
    from tasks.task_health_autofix import classify_finding, ESCALATE
    h = _green()
    h["phase_c"]["db_locks"] = 5
    out = classify_finding(h)
    assert _kinds(out) == {"db_lock_spike": ESCALATE}


def test_classify_db_lock_below_threshold_ignored():
    from tasks.task_health_autofix import classify_finding
    h = _green()
    h["phase_c"]["db_locks"] = 2  # 閾値 3 未満
    assert classify_finding(h) == []


def test_classify_self_errors_escalate_not_tier3():
    """監視 query 自体の失敗 (coverable/divergence_count == -1) は Tier3 でなく escalate."""
    from tasks.task_health_autofix import classify_finding, ESCALATE
    h = _green()
    h["coverage"] = {"coverable": -1, "dlq": -1, "dlq_skus": [],
                     "coverage_alert_sent": True, "coverage_error": "boom"}
    h["url_divergence"] = {"divergence_count": -1, "alert_sent": False,
                           "divergence_error": "join failed"}
    h["phase_c"]["db_query_error"] = "db locked"
    h["phase_c"]["log_scan_error"] = "io error"
    out = classify_finding(h)
    # 4 つの self-error 源 → 全て escalate / monitor_self_error。Tier3 は出ない
    assert all(f["tier"] == ESCALATE for f in out)
    assert all(f["kind"] == "monitor_self_error" for f in out)
    assert len(out) == 4
    # coverable==-1 / divergence_count==-1 なので coverage_gap / url_divergence は出ない
    assert "coverage_gap" not in _kinds(out)
    assert "url_divergence" not in _kinds(out)


def test_classify_multiple_findings_distinct_hash():
    """複数 task の同種異常は target_task_key で別 hash になる (独立ループガード)."""
    from tasks.task_health_autofix import classify_finding
    h = _green()
    h["missed"] = [{"task_key": "a", "expected_hour": 2},
                   {"task_key": "b", "expected_hour": 2}]
    out = classify_finding(h)
    hashes = {f["finding_hash"] for f in out}
    assert len(hashes) == 2, "別 task の missed が同一 hash に衝突"


def test_classify_hash_stable_across_calls():
    """同一異常は呼び出しを跨いで同一 hash (件数の揺れに依存しない)."""
    from tasks.task_health_autofix import classify_finding
    h1 = _green()
    h1["phase_c"]["intermittent"] = [
        {"task_key": "x", "count": 3, "last_at": "2026-05-29 01:00:00"}]
    h2 = _green()
    h2["phase_c"]["intermittent"] = [
        {"task_key": "x", "count": 9, "last_at": "2026-05-29 13:00:00"}]  # 件数/時刻違い
    assert (classify_finding(h1)[0]["finding_hash"]
            == classify_finding(h2)[0]["finding_hash"])


def test_classify_non_dict_input():
    from tasks.task_health_autofix import classify_finding
    assert classify_finding(None) == []
    assert classify_finding("bad") == []


# ---------------------------------------------------------------------------
# 監査ログ helper
# ---------------------------------------------------------------------------
def test_record_attempt_invalid_status_raises():
    _init()
    from monitor.health_autofix_log import record_attempt
    with pytest.raises(ValueError):
        record_attempt("h", "tier1", "missed_task", "rerun", "bogus_status")


def test_count_attempts_excludes_skipped():
    """ループガードは status='skipped' を数えない (実対処のみ集計)."""
    _init()
    from monitor.health_autofix_log import record_attempt, count_attempts_today
    fh = "hash_loopguard"
    record_attempt(fh, "tier1", "missed_task", "rerun", "attempted")
    record_attempt(fh, "tier1", "missed_task", "rerun", "skipped")  # 数えない
    record_attempt(fh, "tier1", "missed_task", "rerun", "resolved")
    assert count_attempts_today(fh) == 2


def test_record_db_proposal_dedup():
    """同一 finding の pending 提案は重複作成せず既存 id を返す."""
    _init()
    from monitor.health_autofix_log import record_db_proposal, get_pending_proposals
    fh = "hash_proposal"
    id1 = record_db_proposal(fh, "url_divergence", "UPDATE monitored_items SET ...",
                             diagnosis_sql="SELECT ...", affected_rows_est=3)
    id2 = record_db_proposal(fh, "url_divergence", "UPDATE monitored_items SET ...")
    assert id1 == id2, "同一 finding で提案が重複作成された"
    pending = get_pending_proposals()
    assert sum(1 for p in pending if p["finding_hash"] == fh) == 1


# ---------------------------------------------------------------------------
# run_health_autofix orchestrator (Phase 1)
#   重い実 task / Discord / 診断 SELECT は monkeypatch で遮断し、分類→Tier 別
#   分岐 (再実行 / ループガード / killswitch / dispatch 欠如 / DB 提案 / escalate)
#   と監査ログ記録だけを検証する。
# ---------------------------------------------------------------------------
def _missed(task_key: str, expected_hour: int = 2) -> dict:
    h = _green()
    h["missed"] = [{"task_key": task_key, "expected_hour": expected_hour}]
    return h


def _statuses(finding_hash: str) -> list[str]:
    from monitor.database import get_conn
    with get_conn() as c:
        return [r[0] for r in c.execute(
            "SELECT status FROM autofix_attempt_log WHERE finding_hash=? "
            "ORDER BY id", (finding_hash,)).fetchall()]


def _patch_notify(monkeypatch) -> dict:
    """Discord 送信を network 無しに差し替え、渡された new_actions / fix_diffs を捕捉."""
    captured = {"actions": None, "fix_diffs": None}

    def fake_notify(config, new_actions, fix_diffs=None):
        captured["actions"] = list(new_actions)
        captured["fix_diffs"] = list(fix_diffs or [])
        return True

    monkeypatch.setattr(
        "tasks.task_health_autofix._notify_autofix_summary", fake_notify)
    return captured


def test_autofix_all_green_no_action(monkeypatch):
    _init()
    captured = _patch_notify(monkeypatch)
    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _green())
    assert s["classified"] == 0
    assert s["reran"] == [] and s["proposed"] == [] and s["escalated"] == []
    assert s["notified"] is False
    assert captured["actions"] is None  # 通知関数は呼ばれない


def test_autofix_tier1_rerun_resolved(monkeypatch):
    _init()
    _patch_notify(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: calls.append(a) or {"success": True})
    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("daily_relist"))
    assert len(calls) == 1  # 対象 task を 1 回再実行
    assert s["reran"] == [{"task_key": "daily_relist"}]
    assert s["rerun_failed"] == []
    assert s["notified"] is True
    from monitor.health_autofix_log import make_finding_hash
    fh = make_finding_hash("missed_task", "daily_relist", None)
    assert _statuses(fh) == ["resolved"]


def test_autofix_tier1_rerun_attempted_on_failure(monkeypatch):
    _init()
    _patch_notify(monkeypatch)
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: {"success": False, "error": "boom"})
    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("daily_relist"))
    assert s["rerun_failed"] == [{"task_key": "daily_relist"}]
    assert s["reran"] == []
    from monitor.health_autofix_log import make_finding_hash
    fh = make_finding_hash("missed_task", "daily_relist", None)
    assert _statuses(fh) == ["attempted"]  # 失敗は resolved にしない (Q0)


def test_autofix_tier1_loop_guard(monkeypatch):
    """当日既に対処済の finding は再実行しない (暴走防止)."""
    _init()
    _patch_notify(monkeypatch)
    from monitor.health_autofix_log import make_finding_hash, record_attempt
    fh = make_finding_hash("missed_task", "daily_relist", None)
    record_attempt(fh, "tier1", "missed_task", "rerun", "attempted",
                   target_task_key="daily_relist")  # 当日 1 回対処済
    calls = []
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: calls.append(a) or {"success": True})
    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("daily_relist"))
    assert calls == []  # 再実行されない
    assert s["skipped"] == [{"task_key": "daily_relist", "reason": "loop_guard"}]
    assert s["notified"] is False


def test_autofix_tier1_killswitch(monkeypatch):
    """user が無効化した task は再実行しない (意図の尊重)."""
    _init()
    _patch_notify(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: calls.append(a) or {"success": True})
    from tasks.task_health_autofix import run_health_autofix
    config = {"tasks_enabled": {"daily_relist": {"enabled": False}}}
    s = run_health_autofix(config, _missed("daily_relist"))
    assert calls == []
    assert s["skipped"] == [{"task_key": "daily_relist", "reason": "killswitch"}]


def test_autofix_tier1_no_dispatch_escalate(monkeypatch):
    """再実行 dispatch 未定義 (CDP 依存等) → 暴走させず escalate."""
    _init()
    _patch_notify(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: calls.append(a) or {"success": True})
    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("market_analysis_refresh"))
    assert calls == []  # 再実行しない
    assert s["escalated"] == [
        {"task_key": "market_analysis_refresh", "reason": "no_dispatch"}]
    assert s["notified"] is True


def test_autofix_tier3_new_proposal(monkeypatch):
    """URL乖離 → READ-ONLY 診断 + 新規 DB 提案保存 + 承認待ち通知 (自動 write しない)."""
    _init()
    _patch_notify(monkeypatch)
    monkeypatch.setattr(
        "tasks.task_health_autofix._diagnose_url_divergence",
        lambda: {"sql": "SELECT ...", "rows": [{"ebay_item_id": "x"}], "count": 3})
    from tasks.task_health_autofix import run_health_autofix
    h = _green()
    h["url_divergence"] = {"divergence_count": 3, "divergent_ids": ["a", "b", "c"],
                           "alert_sent": False}
    s = run_health_autofix({}, h)
    assert len(s["proposed"]) == 1
    assert s["proposed"][0]["kind"] == "url_divergence"
    assert s["proposed"][0]["count"] == 3
    assert s["notified"] is True
    from monitor.health_autofix_log import get_pending_proposals
    assert any(p["kind"] == "url_divergence" for p in get_pending_proposals())


def test_autofix_tier3_pending_dedup(monkeypatch):
    """既存 pending 提案がある finding は再通知せず提案も重複作成しない."""
    _init()
    _patch_notify(monkeypatch)
    monkeypatch.setattr(
        "tasks.task_health_autofix._diagnose_url_divergence",
        lambda: {"sql": "SELECT ...", "rows": [], "count": 3})
    from monitor.health_autofix_log import make_finding_hash, record_db_proposal
    fh = make_finding_hash("url_divergence", None, None)
    record_db_proposal(fh, "url_divergence", "既存 pending")
    from tasks.task_health_autofix import run_health_autofix
    h = _green()
    h["url_divergence"] = {"divergence_count": 3, "divergent_ids": ["a"],
                           "alert_sent": False}
    s = run_health_autofix({}, h)
    assert s["proposed"] == []
    assert s["skipped"] == [{"kind": "url_divergence", "reason": "proposal_pending"}]
    from monitor.health_autofix_log import get_pending_proposals
    assert sum(1 for p in get_pending_proposals() if p["finding_hash"] == fh) == 1


def test_autofix_escalate_first_then_skip(monkeypatch):
    """orphan (TIER1 だが自動再実行対象外) は本日初回のみ記録、2 回目は skip."""
    _init()
    _patch_notify(monkeypatch)
    from tasks.task_health_autofix import run_health_autofix
    h = _green()
    h["phase_c"]["orphans"] = [{"task_key": "ebay_sync", "batch_id": "b1",
                                "started_at": "2026-05-29 02:30:00"}]
    s1 = run_health_autofix({}, h)
    assert len(s1["escalated"]) == 1
    assert s1["escalated"][0]["kind"] == "orphan_task"
    s2 = run_health_autofix({}, h)  # 当日 2 回目
    assert s2["escalated"] == []
    assert s2["skipped"] == [
        {"kind": "orphan_task", "reason": "already_escalated_today"}]


def test_autofix_dedup_same_hash_single_rerun(monkeypatch):
    """同 task が複数 slot で missed → finding_hash dedupe で再実行は 1 回."""
    _init()
    _patch_notify(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "tasks.task_health_autofix._rerun_task",
        lambda *a, **k: calls.append(a) or {"success": True})
    from tasks.task_health_autofix import run_health_autofix
    h = _green()
    h["missed"] = [{"task_key": "daily_relist", "expected_hour": 2},
                   {"task_key": "daily_relist", "expected_hour": 11}]
    s = run_health_autofix({}, h)
    assert s["classified"] == 2   # 分類は 2 件
    assert len(calls) == 1        # dedupe で再実行は 1 回
    assert s["reran"] == [{"task_key": "daily_relist"}]


def test_autofix_error_isolated(monkeypatch):
    """1 件の処理が例外でも全体を止めず errors に記録する (Q0)."""
    _init()
    _patch_notify(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("rerun crashed")

    monkeypatch.setattr("tasks.task_health_autofix._rerun_task", boom)
    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _missed("daily_relist"))
    assert len(s["errors"]) == 1
    assert s["errors"][0]["kind"] == "missed_task"


# ---------------------------------------------------------------------------
# Phase 2: Tier2 修正案ドライラン dispatch
#   health_fixer.propose_fix (= claude 実起動) は monkeypatch で遮断し、
#   段階フラグ ON/OFF の分岐 + verdict 別の記録・通知だけを検証する。
# ---------------------------------------------------------------------------
def _subprocess_err(task_key: str = "daily_codex_lint") -> dict:
    h = _green()
    h["phase_c"]["subprocess_errors"] = [
        {"task_key": task_key, "started_at": "2026-05-29 03:00:00",
         "message": "returncode=1\nTraceback ... NameError: foo"}]
    return h


class _FakeProposal:
    """health_fixer.FixProposal の最小スタブ (dispatch test 用)."""
    def __init__(self, verdict, *, reason="", diff="", diff_path="",
                 gates=None, changed_lines=0, touched_files=None, duration_ms=10):
        self.verdict = verdict
        self.reason = reason
        self.diff = diff
        self.diff_path = diff_path
        self.gates = gates or {}
        self.changed_lines = changed_lines
        self.touched_files = touched_files or []
        self.duration_ms = duration_ms


def test_tier2_flag_off_escalates(monkeypatch):
    """段階フラグ未設定 (既定 False) では Tier2 は従来通り escalate (記録のみ)."""
    _init()
    _patch_notify(monkeypatch)
    called = []
    import monitor.health_fixer as hf
    monkeypatch.setattr(hf, "propose_fix",
                        lambda *a, **k: called.append(a) or _FakeProposal("proposed"))
    from tasks.task_health_autofix import run_health_autofix
    s = run_health_autofix({}, _subprocess_err())  # health_autofix ブロック無し = False
    assert called == []  # claude は起動しない
    assert s["fix_dryrun"] == []
    assert len(s["escalated"]) == 1
    assert s["escalated"][0]["kind"] == "subprocess_error"


def test_tier2_flag_on_proposed(monkeypatch):
    """フラグ ON + verdict=proposed → fix_dryrun 記録 + diff を fix_diffs へ + 通知."""
    _init()
    captured = _patch_notify(monkeypatch)
    import monitor.health_fixer as hf
    monkeypatch.setattr(hf, "propose_fix", lambda *a, **k: _FakeProposal(
        "proposed", reason="全 gate pass", diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b",
        diff_path="data/health_fixes/2026-05-29_daily_codex_lint_abcd1234.diff",
        gates={"size": {"pass": True}}, changed_lines=2,
        touched_files=["tasks/task_daily_codex_lint.py"]))
    from tasks.task_health_autofix import run_health_autofix
    config = {"health_autofix": {"tier2_dryrun_enabled": True}}
    s = run_health_autofix(config, _subprocess_err())
    assert len(s["fix_dryrun"]) == 1
    assert s["fix_dryrun"][0]["verdict"] == "proposed"
    assert s["notified"] is True
    assert len(captured["fix_diffs"]) == 1  # diff 本文が別メッセージ用に渡る
    from monitor.health_autofix_log import make_finding_hash
    fh = make_finding_hash("subprocess_error", "daily_codex_lint", None)
    assert _statuses(fh) == ["proposed"]


def test_tier2_verdict_status_map(monkeypatch):
    """verdict → autofix_attempt_log status の対応付け (proposed以外も痕跡を残す Q0)."""
    _init()
    _patch_notify(monkeypatch)
    import monitor.health_fixer as hf
    from monitor.health_autofix_log import make_finding_hash
    from tasks.task_health_autofix import run_health_autofix
    # 各ケースで別 task_key を使い finding_hash 衝突 (= loop guard 干渉) を避ける。
    # init_db は冪等でデータを保持するため、同一 hash を再投入すると 2 回目が
    # loop guard で skipped になり status が混ざる。
    cases = [
        ("codex_case_gate", "gate_failed", "gate_failed"),
        ("codex_case_esc", "escalated", "escalated"),
        ("codex_case_err", "error", "aborted"),
    ]
    config = {"health_autofix": {"tier2_dryrun_enabled": True}}
    for task_key, verdict, expected_status in cases:
        monkeypatch.setattr(hf, "propose_fix",
                            lambda *a, _v=verdict, **k: _FakeProposal(_v, reason="r"))
        run_health_autofix(config, _subprocess_err(task_key))
        fh = make_finding_hash("subprocess_error", task_key, None)
        assert _statuses(fh) == [expected_status], f"{verdict}→{expected_status}"


def test_tier2_loop_guard(monkeypatch):
    """当日対処済の Tier2 finding は claude を再起動しない (稀 + コスト)."""
    _init()
    _patch_notify(monkeypatch)
    from monitor.health_autofix_log import make_finding_hash, record_attempt
    fh = make_finding_hash("subprocess_error", "daily_codex_lint", None)
    record_attempt(fh, "tier2", "subprocess_error", "fix_dryrun", "proposed",
                   target_task_key="daily_codex_lint")
    called = []
    import monitor.health_fixer as hf
    monkeypatch.setattr(hf, "propose_fix",
                        lambda *a, **k: called.append(a) or _FakeProposal("proposed"))
    from tasks.task_health_autofix import run_health_autofix
    config = {"health_autofix": {"tier2_dryrun_enabled": True}}
    s = run_health_autofix(config, _subprocess_err())
    assert called == []  # 再起動しない
    assert s["skipped"] == [{"task_key": "daily_codex_lint", "reason": "loop_guard"}]


def test_tier2_killswitch(monkeypatch):
    """user が無効化した task は修正案も作らない."""
    _init()
    _patch_notify(monkeypatch)
    called = []
    import monitor.health_fixer as hf
    monkeypatch.setattr(hf, "propose_fix",
                        lambda *a, **k: called.append(a) or _FakeProposal("proposed"))
    from tasks.task_health_autofix import run_health_autofix
    config = {"health_autofix": {"tier2_dryrun_enabled": True},
              "tasks_enabled": {"daily_codex_lint": {"enabled": False}}}
    s = run_health_autofix(config, _subprocess_err())
    assert called == []
    assert s["skipped"] == [{"task_key": "daily_codex_lint", "reason": "killswitch"}]


def test_tier2_no_error_message_escalates(monkeypatch):
    """subprocess message が引けない時は claude を起動せず escalate (要人手)."""
    _init()
    _patch_notify(monkeypatch)
    called = []
    import monitor.health_fixer as hf
    monkeypatch.setattr(hf, "propose_fix",
                        lambda *a, **k: called.append(a) or _FakeProposal("proposed"))
    from tasks.task_health_autofix import run_health_autofix
    config = {"health_autofix": {"tier2_dryrun_enabled": True}}
    h = _green()
    # subprocess_errors に message を載せない (空) → classify は拾うが message 引けず
    h["phase_c"]["subprocess_errors"] = [
        {"task_key": "daily_codex_lint", "started_at": "2026-05-29 03:00:00",
         "message": ""}]
    s = run_health_autofix(config, h)
    assert called == []
    assert len(s["escalated"]) == 1
    assert s["escalated"][0]["reason"] == "no_error_message"


def test_tier2_dryrun_enabled_helper():
    """段階フラグ helper の既定 False + 明示 True 判定."""
    from tasks.task_health_autofix import _tier2_dryrun_enabled
    assert _tier2_dryrun_enabled({}) is False
    assert _tier2_dryrun_enabled({"health_autofix": {}}) is False
    assert _tier2_dryrun_enabled(
        {"health_autofix": {"tier2_dryrun_enabled": False}}) is False
    assert _tier2_dryrun_enabled(
        {"health_autofix": {"tier2_dryrun_enabled": True}}) is True


def test_result_ok_mirrors_run_task_semantics():
    """_result_ok の success 既定は **True** であること (run_task と同一規約).

    意図のロックイン: daily_scheduler.run_task は dict が "success" キーを持たない
    場合 success=True とみなして execution_log に completed を記録する
    (run_task: `result.get("success", True)`)。_result_ok を既定 False に変えると、
    run_task が completed と記録した再実行を autofix だけ「失敗」と報告し、Discord に
    赤通知が出る = **逆向きの fake-failure** が生じる。両者の既定は一致させる。
    """
    from tasks.task_health_autofix import _result_ok
    # success キー無しの dict → run_task と同じく成功扱い (既定 True)
    assert _result_ok({}) is True
    assert _result_ok({"foo": "bar"}) is True
    # 明示 success キーはそのまま反映
    assert _result_ok({"success": True}) is True
    assert _result_ok({"success": False}) is False
    # dict 以外は truthy 判定
    assert _result_ok(None) is False
    assert _result_ok("ok") is True
