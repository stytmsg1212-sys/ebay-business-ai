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


# ---- #43 業務外ドメイン (楽天等) 自動アーカイブ + N/A ヘッダ大文字小文字バグ regression ----


class TestHeaderValueCaseInsensitive:
    """2026-07-04 #43: 楽天の一括配信メールが小文字ヘッダ名 ('subject'/'from') を送るため,
    完全一致 (h['name'] == 'Subject') だと拾えず sender/subject が 'N/A' 固定になっていた."""

    def test_lowercase_header_name_is_found(self):
        from tasks.task_email_pickup import _header_value

        headers = [
            {"name": "subject", "value": "件名テスト"},
            {"name": "from", "value": '"楽天" <info@emagazine.rakuten.co.jp>'},
            {"name": "Date", "value": "Fri, 3 Jul 2026 16:39:39 +0900"},
        ]
        assert _header_value(headers, "Subject") == "件名テスト"
        assert _header_value(headers, "From") == '"楽天" <info@emagazine.rakuten.co.jp>'
        assert _header_value(headers, "Date") == "Fri, 3 Jul 2026 16:39:39 +0900"

    def test_missing_header_falls_back_to_default(self):
        from tasks.task_email_pickup import _header_value

        headers = [{"name": "From", "value": "a@example.com"}]
        assert _header_value(headers, "Subject") == "N/A"


class TestNoiseSenderDomain:
    def test_rakuten_subdomain_matches(self):
        from tasks.task_email_pickup import _is_noise_domain

        assert _is_noise_domain("emagazine.rakuten.co.jp")
        assert _is_noise_domain("pay.rakuten.co.jp")
        assert _is_noise_domain("rakuten.co.jp")

    def test_ebay_domain_does_not_match(self):
        from tasks.task_email_pickup import _is_noise_domain

        assert not _is_noise_domain("ebay.com")
        assert not _is_noise_domain("mail.yahoo.co.jp")

    def test_empty_domain_does_not_match(self):
        from tasks.task_email_pickup import _is_noise_domain

        assert not _is_noise_domain("")


class TestIsArchivableNoiseEmail:
    def test_rakuten_promo_other_category_is_archivable(self):
        from tasks.task_email_pickup import is_archivable_noise_email

        assert is_archivable_noise_email(
            "other", "promo", '"楽天カレンダー" <calendar-info@emagazine.rakuten.co.jp>'
        )
        assert is_archivable_noise_email(
            "other", "other", '"楽天ペイ" <no-reply@pay.rakuten.co.jp>'
        )

    def test_ebay_sender_is_not_archivable(self):
        """業務メール (eBay) はドメインがノイズリストに無いため対象外."""
        from tasks.task_email_pickup import is_archivable_noise_email

        assert not is_archivable_noise_email("other", "other", "eBay <ebay@ebay.com>")

    def test_supplier_purchase_category_is_never_archived(self):
        """money-direct guard: 楽天ドメインでも category='supplier_purchase' (入荷確認
        ワークフロー対象) は誤って confirmed=1 にしない."""
        from tasks.task_email_pickup import is_archivable_noise_email

        assert not is_archivable_noise_email(
            "supplier_purchase", "other", '"楽天ペイ" <order@checkout.rakuten.co.jp>'
        )

    def test_ai_category_return_blocks_archive(self):
        """rule 側は 'other' でも AI が return/buyer_message 等の重要カテゴリと判定したら
        対象外 (誤爆防止)."""
        from tasks.task_email_pickup import is_archivable_noise_email

        assert not is_archivable_noise_email(
            "other", "return", '"楽天市場" <order@rakuten.co.jp>'
        )

    def test_na_sender_is_not_archivable(self):
        """sender が 'N/A' (ドメイン抽出不能) の場合は fail-safe で対象外にする
        (誤って重要メールを隠すリスクより、未アーカイブのまま残すほうが安全)."""
        from tasks.task_email_pickup import is_archivable_noise_email

        assert not is_archivable_noise_email("other", "other", "N/A")


class TestSaveEmailsArchivesNoise:
    def test_rakuten_noise_email_inserted_as_confirmed(self, temp_db, monkeypatch):
        """楽天プロモメールは INSERT 時点で confirmed=1 になる (DASHBOARD 非表示)."""
        from tasks import task_email_pickup as t

        _patch_external(monkeypatch)
        # rule category も 'other' になるよう Claude fallback を明示的に固定
        monkeypatch.setattr(
            "monitor.claude_summarizer.summarize_email",
            lambda subject, sender, body: {"category": "promo", "priority": "low"},
            raising=False,
        )

        noise_email = {
            "id": "gmail_noise_1",
            "subject": "楽天カレンダーお得なニュース",
            "from": '"楽天カレンダー" <calendar-info@emagazine.rakuten.co.jp>',
            "date": "Fri, 3 Jul 2026 16:39:39 +0900",
            "body": "クーポンのお知らせ",
            "category": "other",
        }
        inserted = t._save_emails_to_db([noise_email])
        assert inserted == 1

        with db.get_conn() as c:
            row = c.execute(
                "SELECT confirmed FROM emails WHERE gmail_id='gmail_noise_1'"
            ).fetchone()
        assert row["confirmed"] == 1

    def test_ebay_email_inserted_as_unconfirmed(self, temp_db, sample_emails, monkeypatch):
        """業務メール (eBay) は従来通り confirmed=0 で INSERT される (regression guard)."""
        _patch_external(monkeypatch)
        from tasks.task_email_pickup import _save_emails_to_db

        _save_emails_to_db(sample_emails)
        with db.get_conn() as c:
            rows = c.execute(
                "SELECT gmail_id, confirmed FROM emails ORDER BY gmail_id"
            ).fetchall()
        assert all(r["confirmed"] == 0 for r in rows)

    def test_supplier_purchase_rakuten_email_not_archived(self, temp_db, monkeypatch):
        """money-direct guard の統合テスト: 楽天の購入確認メール (category=
        supplier_purchase) は INSERT 時に confirmed=1 にされない (入荷確認workflow温存)."""
        from tasks import task_email_pickup as t

        _patch_external(monkeypatch)
        monkeypatch.setattr(
            "monitor.claude_summarizer.summarize_email",
            lambda subject, sender, body: {"category": "other", "priority": "low"},
            raising=False,
        )

        purchase_email = {
            "id": "gmail_purchase_1",
            "subject": "楽天ペイ 注文受付（自動配信メール）",
            "from": '"楽天ペイ" <order@checkout.rakuten.co.jp>',
            "date": "Fri, 3 Jul 2026 21:35:17 +0900",
            "body": "ご注文ありがとうございます",
            "category": "supplier_purchase",
        }
        inserted = t._save_emails_to_db([purchase_email])
        assert inserted == 1

        with db.get_conn() as c:
            row = c.execute(
                "SELECT confirmed FROM emails WHERE gmail_id='gmail_purchase_1'"
            ).fetchone()
        assert row["confirmed"] == 0
