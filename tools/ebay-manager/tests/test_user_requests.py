"""W266 依頼ボード (user_requests) のテスト。2026-06-12 新設。

カバレッジ:
  - migration v72 冪等性 (init_db 2 回でデータ保持)
  - add / get / 並び順
  - status 遷移 + awaiting_check の verify_steps 必須ガード (Q0)
  - 進捗ログ追記
  - done 遷移で confirmed_at 記録
"""
import json

import pytest

import monitor.database as db
from monitor.database import (
    USER_REQUEST_STATUSES,
    add_user_request,
    answer_user_request,
    append_user_request_log,
    ask_user_request,
    get_conn,
    get_user_request,
    get_user_requests,
    init_db,
    set_user_request_status,
)


def test_migration_v72_idempotent():
    """init_db 2 回連続でデータが消えない (Q2 冪等性)。"""
    init_db()
    rid = add_user_request("冪等性テスト", "desc")
    init_db()  # 再実行
    row = get_user_request(rid)
    assert row is not None
    assert row["title"] == "冪等性テスト"
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver >= 72


def test_add_and_get():
    init_db()
    rid = add_user_request(
        "タイトル", "詳細", kind="不具合", priority="高", related_w="W999"
    )
    row = get_user_request(rid)
    assert row["status"] == "open"
    assert row["kind"] == "不具合"
    assert row["priority"] == "高"
    assert row["related_w"] == "W999"
    assert row["created_at"]  # CURRENT_TIMESTAMP 付与


def test_add_empty_title_rejected():
    init_db()
    with pytest.raises(ValueError):
        add_user_request("   ")


def test_add_invalid_status_rejected():
    init_db()
    with pytest.raises(ValueError):
        add_user_request("t", status="bogus")


def test_get_user_requests_order_done_last():
    """done は末尾、それ以外は id 降順 (新しい依頼が上)。"""
    init_db()
    r1 = add_user_request("古い依頼")
    r2 = add_user_request("完了する依頼")
    r3 = add_user_request("新しい依頼")
    set_user_request_status(r2, "awaiting_check", verify_steps="手順")
    set_user_request_status(r2, "done")
    ids = [r["id"] for r in get_user_requests()]
    assert ids == [r3, r1, r2]  # done (r2) が最後


def test_get_user_requests_status_filter():
    init_db()
    r1 = add_user_request("a")
    r2 = add_user_request("b")
    set_user_request_status(r2, "in_progress")
    rows = get_user_requests(statuses=["in_progress"])
    assert [r["id"] for r in rows] == [r2]
    with pytest.raises(ValueError):
        get_user_requests(statuses=["bogus"])


def test_awaiting_check_requires_verify_steps():
    """確認待ち遷移は確認手順必須 (DB 層 Q0 ガード)。"""
    init_db()
    rid = add_user_request("確認手順なし")
    with pytest.raises(ValueError):
        set_user_request_status(rid, "awaiting_check")
    # verify_steps を渡せば通る
    assert set_user_request_status(rid, "awaiting_check", verify_steps="1. 開く 2. 押す")
    row = get_user_request(rid)
    assert row["status"] == "awaiting_check"
    assert row["verify_steps"] == "1. 開く 2. 押す"
    assert row["completed_at"]  # 対応完了時刻


def test_awaiting_check_with_existing_verify_steps():
    """行に verify_steps が既にあれば新規指定なしでも遷移可。"""
    init_db()
    rid = add_user_request("既存手順あり")
    set_user_request_status(rid, "awaiting_check", verify_steps="既存手順")
    set_user_request_status(rid, "in_progress", note="差し戻し")  # 一旦戻す
    assert set_user_request_status(rid, "awaiting_check")  # 既存 verify_steps で OK


def test_done_sets_confirmed_at():
    init_db()
    rid = add_user_request("完了フロー")
    set_user_request_status(rid, "awaiting_check", verify_steps="手順")
    set_user_request_status(rid, "done")
    row = get_user_request(rid)
    assert row["status"] == "done"
    assert row["confirmed_at"]


def test_done_requires_awaiting_check():
    """H1: done は awaiting_check 経由のみ (user 確認なし直接クローズの物理 BLOCK)。"""
    init_db()
    rid = add_user_request("直接done禁止")
    with pytest.raises(ValueError):
        set_user_request_status(rid, "done")  # open → done は拒否
    set_user_request_status(rid, "in_progress")
    with pytest.raises(ValueError):
        set_user_request_status(rid, "done")  # in_progress → done も拒否


def test_awaiting_check_empty_verify_steps_cannot_wipe():
    """H2: 空文字 verify_steps でガード通過後の手順消去を防ぐ。"""
    init_db()
    rid = add_user_request("wipe防止")
    set_user_request_status(rid, "awaiting_check", verify_steps="手順A")
    set_user_request_status(rid, "awaiting_check", verify_steps="")
    assert get_user_request(rid)["verify_steps"] == "手順A"  # 空文字で消えない
    # 非 awaiting_check 遷移でも空文字 wipe 不可
    set_user_request_status(rid, "in_progress", verify_steps="   ")
    assert get_user_request(rid)["verify_steps"] == "手順A"


def test_invalid_status_transition_rejected():
    init_db()
    rid = add_user_request("t")
    with pytest.raises(ValueError):
        set_user_request_status(rid, "bogus")


def test_set_status_missing_row_returns_false():
    init_db()
    assert set_user_request_status(99999, "in_progress") is False


def test_append_log():
    init_db()
    rid = add_user_request("ログテスト")
    assert append_user_request_log(rid, "1 行目", author="assistant")
    assert append_user_request_log(rid, "2 行目", author="user")
    row = get_user_request(rid)
    log = row["progress_log"]
    assert "1 行目" in log and "2 行目" in log
    assert "(assistant)" in log and "(user)" in log
    assert "JST" in log  # タイムスタンプ付き
    assert append_user_request_log(99999, "missing") is False


def test_status_transition_appends_note():
    init_db()
    rid = add_user_request("note 遷移")
    set_user_request_status(rid, "in_progress", note="対応開始します")
    row = get_user_request(rid)
    assert "対応開始します" in row["progress_log"]


def test_statuses_constant_matches_labels():
    from monitor.database import USER_REQUEST_STATUS_LABELS

    assert set(USER_REQUEST_STATUS_LABELS) == set(USER_REQUEST_STATUSES)


# ---- W267 (依頼ボード#13): 回答待ち双方向化 ----


def test_migration_v73_idempotent():
    """init_db 2 回で pending_question 列が存在しデータ保持 (Q2 冪等性)。"""
    init_db()
    rid = add_user_request("v73テスト")
    init_db()  # 再実行
    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(user_requests)")}
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert "pending_question" in cols
    assert ver >= 73
    assert get_user_request(rid)["title"] == "v73テスト"


def test_ask_user_request_sets_question_and_status():
    init_db()
    rid = add_user_request("質問フロー")
    assert ask_user_request(rid, "再現手順を教えてください")
    row = get_user_request(rid)
    assert row["status"] == "waiting_user"
    assert row["pending_question"] == "再現手順を教えてください"
    assert "質問: 再現手順を教えてください" in row["progress_log"]


def test_ask_user_request_empty_question_rejected():
    init_db()
    rid = add_user_request("空質問")
    with pytest.raises(ValueError):
        ask_user_request(rid, "   ")


def test_ask_user_request_missing_row_returns_false():
    init_db()
    assert ask_user_request(99999, "質問") is False


def test_answer_user_request_full_flow():
    """回答受領: ログ追記 + 質問クリア + in_progress 復帰 + 検知イベント発行。"""
    init_db()
    rid = add_user_request("回答フロー")
    ask_user_request(rid, "AとBどちらにしますか?")
    assert answer_user_request(rid, "Bでお願いします")
    row = get_user_request(rid)
    assert row["status"] == "in_progress"
    assert row["pending_question"] is None
    assert "回答: Bでお願いします" in row["progress_log"]
    assert "(user)" in row["progress_log"]
    # 検知イベント JSONL (conftest で tmp に隔離済)
    events_path = db.BOARD_ANSWER_EVENTS_PATH
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    event = json.loads(lines[-1])
    assert event["id"] == rid
    assert event["preview"].startswith("Bでお願いします")
    assert event["answered_at_utc"]


def test_answer_user_request_requires_waiting_user():
    """waiting_user 以外からの回答は拒否 (UI と状態の race 防止)。"""
    init_db()
    rid = add_user_request("非回答待ち")
    with pytest.raises(ValueError):
        answer_user_request(rid, "回答")  # open のまま
    set_user_request_status(rid, "in_progress")
    with pytest.raises(ValueError):
        answer_user_request(rid, "回答")


def test_answer_user_request_empty_answer_rejected():
    init_db()
    rid = add_user_request("空回答")
    ask_user_request(rid, "質問")
    with pytest.raises(ValueError):
        answer_user_request(rid, "")


def test_answer_user_request_missing_row_returns_false():
    init_db()
    assert answer_user_request(99999, "回答") is False


def test_set_status_away_from_waiting_user_clears_pending_question():
    """H2 回帰: answer 以外の経路で waiting_user を離脱しても stale 質問が残らない。"""
    init_db()
    rid = add_user_request("stale質問")
    ask_user_request(rid, "旧質問")
    set_user_request_status(rid, "in_progress")  # legacy 直接遷移 (回答なし)
    assert get_user_request(rid)["pending_question"] is None


def test_ask_user_request_done_rejected():
    """MED 回帰: クローズ済み (done) の依頼への質問は拒否 (status 逆行防止)。"""
    init_db()
    rid = add_user_request("クローズ済み")
    set_user_request_status(rid, "awaiting_check", verify_steps="手順")
    set_user_request_status(rid, "done")
    with pytest.raises(ValueError):
        ask_user_request(rid, "質問")
    assert get_user_request(rid)["status"] == "done"


def test_answer_event_write_failure_keeps_db_update(monkeypatch, tmp_path):
    """MED 回帰: 検知イベント JSONL 書込失敗でも DB 更新は維持 + True 返却 (Q0 保険設計)。

    BOARD_ANSWER_EVENTS_PATH をディレクトリに差し替え → open(..., 'a') が
    OSError 系 (IsADirectoryError / PermissionError) を出す状況を再現。
    """
    events_dir = tmp_path / "events_as_dir"
    events_dir.mkdir()
    monkeypatch.setattr("monitor.database.BOARD_ANSWER_EVENTS_PATH", events_dir)
    init_db()
    rid = add_user_request("イベント書込失敗")
    ask_user_request(rid, "質問")
    assert answer_user_request(rid, "回答") is True  # OSError でも巻き戻さない
    row = get_user_request(rid)
    assert row["status"] == "in_progress"
    assert row["pending_question"] is None
    assert "回答: 回答" in row["progress_log"]
