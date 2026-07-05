"""DASHBOARD 緊急メール confirmed 除外の回帰テスト (2026-07-05 user 報告バグ).

user 報告: 「確認済み」ボタンを押しても緊急メール一覧から消えない。
root cause: `tabs.tab_dashboard._is_urgent_email` に emails.confirmed の
チェックが無く、confirmed=1 でも urgent 判定され続けた (既存バグ、
2026-07-05 の 8b574cc/f2e8c1c リファクタとは無関係)。

`_is_urgent_email` は純関数のため dict 入力だけで検証できる
(DB / Streamlit runtime 不要。ただし module import 時に streamlit を
読み込むため conftest の DB 隔離 fixture 下で安全に import できる)。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tabs.tab_dashboard import _is_urgent_email  # noqa: E402


def test_confirmed_urgent_email_is_excluded():
    """今回 fix の本丸: confirmed=1 なら urgent 条件を満たしても False。"""
    em = {
        'confirmed': 1,
        'priority_ai': 'urgent',
        'category': 'buyer_message',
        'category_ai': '',
    }
    assert _is_urgent_email(em) is False


def test_unconfirmed_urgent_email_is_included():
    """confirmed=0 (未確認) の urgent メールは従来通り True。"""
    em = {
        'confirmed': 0,
        'priority_ai': 'urgent',
        'category': 'buyer_message',
        'category_ai': '',
    }
    assert _is_urgent_email(em) is True


def test_missing_confirmed_key_treated_as_unconfirmed():
    """confirmed キー欠落 = 未確認扱いで True (get_recent_emails は
    COALESCE(confirmed,0) を返すが、防御的に欠落ケースも保証)。"""
    em = {
        'priority_ai': 'urgent',
        'category': 'buyer_message',
    }
    assert _is_urgent_email(em) is True


def test_unconfirmed_category_based_urgent_without_priority():
    """priority_ai 空の場合はカテゴリ判定 (buyer_message は urgent カテゴリ)。
    confirmed 除外がカテゴリ経路にも効くことの対照ケース。"""
    em_unconfirmed = {'confirmed': 0, 'priority_ai': '', 'category': 'buyer_message'}
    em_confirmed = {'confirmed': 1, 'priority_ai': '', 'category': 'buyer_message'}
    assert _is_urgent_email(em_unconfirmed) is True
    assert _is_urgent_email(em_confirmed) is False
