"""W54 task_email_pickup INSERT silent skip 修正の regression test.

2026-04-30 W54: INSERT OR IGNORE rowcount 確認なし + broad except 握り潰し
で 3 日連続 0 件 silent skip 発生. rowcount ベースの inserted_count 集計と,
個別メール enrichment 失敗を許容する inner try/except を導入.
"""
from __future__ import annotations

import sqlite3
from unittest import mock

import pytest

from monitor import database as db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """クリーンな SQLite を一時作成して emails テーブルだけ用意する."""
    tmp_db = tmp_path / "monitor.db"
    monkeypatch.setattr(db, "DB_PATH", str(tmp_db))
    with sqlite3.connect(str(tmp_db)) as con:
        con.execute(
            """CREATE TABLE emails (
                gmail_id TEXT PRIMARY KEY,
                subject TEXT, sender TEXT, date TEXT,
                body_text TEXT, body_ja TEXT, category TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed INTEGER DEFAULT 0,
                summary_ja TEXT, action_ja TEXT, buyer_message_ja TEXT,
                priority_ai TEXT, category_ai TEXT, summarized_at TIMESTAMP
            )"""
        )
    return tmp_db


@pytest.fixture
def sample_emails():
    return [
        {
            "id": "gmail_001",
            "subject": "Item sold: ABC",
            "from": "ebay@ebay.com",
            "date": "Wed, 30 Apr 2026 11:00:00 +0900",
            "body": "An item has been sold.",
            "category": "sale",
        },
        {
            "id": "gmail_002",
            "subject": "Buyer message about item",
            "from": "ebay@ebay.com",
            "date": "Wed, 30 Apr 2026 11:01:00 +0900",
            "body": "Question from buyer",
            "category": "buyer_message",
        },
        {
            "id": "gmail_003",
            "subject": "Refund request",
            "from": "ebay@ebay.com",
            "date": "Wed, 30 Apr 2026 11:02:00 +0900",
            "body": "Refund requested",
            "category": "return",
        },
    ]


def _patch_external(monkeypatch):
    """Claude / 翻訳の外部依存を no-op に固定 (本 test は INSERT 動作のみ検証)."""
    from tasks import task_email_pickup as t
    monkeypatch.setattr(t, "_translate_to_ja", lambda s: "")
    # claude_summarizer はモジュール内で import される可能性があるため両側差替
    fake_summary = lambda subject, sender, body: None  # noqa: E731
    monkeypatch.setattr(
        "monitor.claude_summarizer.summarize_email",
        fake_summary,
        raising=False,
    )


def test_inserted_count_all_new(temp_db, sample_emails, monkeypatch):
    """全件新規: rowcount=1 を len(emails) 回 → inserted=len."""
    _patch_external(monkeypatch)
    from tasks.task_email_pickup import _save_emails_to_db

    inserted = _save_emails_to_db(sample_emails)
    assert inserted == len(sample_emails)

    with db.get_conn() as c:
        rows = c.execute("SELECT gmail_id FROM emails ORDER BY gmail_id").fetchall()
    assert [r["gmail_id"] for r in rows] == ["gmail_001", "gmail_002", "gmail_003"]


def test_inserted_count_all_duplicate(temp_db, sample_emails, monkeypatch):
    """全件重複: 2 回目呼出は rowcount=0 → inserted=0 (silent skip 検出可能化)."""
    _patch_external(monkeypatch)
    from tasks.task_email_pickup import _save_emails_to_db

    _save_emails_to_db(sample_emails)  # 初回 INSERT
    inserted = _save_emails_to_db(sample_emails)  # 全件 IGNORE
    assert inserted == 0


def test_inserted_count_mix(temp_db, sample_emails, monkeypatch):
    """1 件既存 + 2 件新規 → inserted=2."""
    _patch_external(monkeypatch)
    from tasks.task_email_pickup import _save_emails_to_db

    _save_emails_to_db([sample_emails[0]])  # 1 件先 INSERT
    inserted = _save_emails_to_db(sample_emails)  # 既存 1 + 新規 2
    assert inserted == 2


def test_enrichment_failure_does_not_break_insert(temp_db, sample_emails, monkeypatch):
    """regression: claude_summarizer / translate raise しても INSERT は通る.

    旧コードでは関数全体を try/except Exception でラップしていたため, 個別失敗が
    全件 silent skip に転化していた. 本 test で「個別 enrichment 失敗 +
    INSERT 成功 + 例外を上位に伝播しない」3 点を保証する.
    """
    from tasks import task_email_pickup as t

    monkeypatch.setattr(t, "_translate_to_ja", mock.Mock(side_effect=ValueError("translate down")))
    monkeypatch.setattr(
        "monitor.claude_summarizer.summarize_email",
        mock.Mock(side_effect=RuntimeError("claude down")),
        raising=False,
    )

    inserted = t._save_emails_to_db(sample_emails)
    assert inserted == len(sample_emails)


def test_save_emails_skips_claude_for_existing(temp_db, sample_emails, monkeypatch):
    """W66: 事前 SELECT で gmail_id 既存メールは Claude API call されない (課金抑制).

    subject filter 撤去で取得件数が増えても, 重複メールは事前 SELECT で skip され
    Claude summarize は呼ばれない. 通常運用で大半が既存メールになるため必須.
    """
    from tasks import task_email_pickup as t

    summarize_calls: list[str] = []

    def _fake_summarize(subject, sender, body):
        summarize_calls.append(subject)
        return None

    monkeypatch.setattr(
        "monitor.claude_summarizer.summarize_email", _fake_summarize, raising=False
    )
    monkeypatch.setattr(t, "_translate_to_ja", lambda s: "")

    # 1 回目: 全件新規 → 全件 Claude call
    inserted_1 = t._save_emails_to_db(sample_emails)
    assert inserted_1 == len(sample_emails)
    assert len(summarize_calls) == len(sample_emails), "初回は全件 Claude call が走るべき"

    # 2 回目: 全件既存 → 事前 SELECT で skip → Claude call ゼロ
    summarize_calls.clear()
    inserted_2 = t._save_emails_to_db(sample_emails)
    assert inserted_2 == 0
    assert len(summarize_calls) == 0, (
        "事前 SELECT で全件 skip されるはず. Claude API 課金浪費を防止"
    )


def test_save_emails_claude_call_outside_db_connection(temp_db, sample_emails, monkeypatch):
    """W66 hotfix regression: Claude API call が DB connection 内で行われないこと.

    旧実装は単一 with get_conn() 内で Claude call (~10s/件) をループ → transaction
    が数十分継続し他 UPDATE (supplier_apply 等) が busy_timeout 超過で fail.
    本 test は summarize_email 呼出時点で DB connection が open でないことを保証する.
    将来の refactor で `with get_conn()` ブロックを Claude call 全体に再拡大する事故を防ぐ.
    """
    from tasks import task_email_pickup as t
    from monitor import database as db_mod

    open_during_claude_call: list[bool] = []
    active = {"count": 0}
    real_get_conn = db_mod.get_conn

    class _Tracker:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            active["count"] += 1
            return self._conn.__enter__()

        def __exit__(self, *a):
            active["count"] -= 1
            return self._conn.__exit__(*a)

    def _wrapped_get_conn():
        return _Tracker(real_get_conn())

    monkeypatch.setattr(db_mod, "get_conn", _wrapped_get_conn)

    def _fake_summarize(subject, sender, body):
        # Claude call 時点では DB connection が open であってはならない
        open_during_claude_call.append(active["count"] > 0)
        return None

    monkeypatch.setattr(
        "monitor.claude_summarizer.summarize_email", _fake_summarize, raising=False
    )
    monkeypatch.setattr(t, "_translate_to_ja", lambda s: "")

    t._save_emails_to_db(sample_emails)

    assert open_during_claude_call, "Claude summarize が呼ばれていない (test setup 不備)"
    assert not any(open_during_claude_call), (
        "DB lock 衝突 regression: Claude API call 中に DB connection が open. "
        "Phase 1/2 分離が崩れている (tasks/task_email_pickup.py:_save_emails_to_db)"
    )


def test_run_email_pickup_returns_inserted_count(temp_db, sample_emails, monkeypatch):
    """run_email_pickup の return dict に inserted_count が含まれ, message にも反映される."""
    from tasks import task_email_pickup as t

    fake_service = object()
    monkeypatch.setattr(t, "get_gmail_service", lambda cfg: fake_service)
    monkeypatch.setattr(t, "extract_ebay_emails", lambda svc: (sample_emails, 2))

    result = t.run_email_pickup({"gmail": {"enabled": True}})

    assert result["success"] is True
    assert result.get("inserted_count") == 2
    assert "INSERT 2" in result["message"]
    # 後方互換: task_company_secretary.get_email_summary が参照するため emails も保持
    assert isinstance(result.get("emails"), list)
    # truncation 対策: dict のキー順で inserted_count / message が emails より先に来ること
    keys = list(result.keys())
    assert keys.index("inserted_count") < keys.index("emails")
    assert keys.index("message") < keys.index("emails")
