#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依頼ボード#24: リサーチ由来のキーワード監視を区別表示するための
get_research_watch_ids() 導出ヘルパーのテスト.

watch_ids_json (research_candidates) から研究承認由来の watch_id 集合を導出し、
キーワード監視 UI のバッジ/フィルタに使う (スキーマ変更なし)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.database import init_db, get_conn  # noqa: E402
from monitor.research_candidates_db import (  # noqa: E402
    insert_research_candidate, record_watch_ids, get_research_watch_ids,
)


def test_collects_and_dedupes_watch_ids():
    init_db()
    rc1 = insert_research_candidate(title_ja="board24 A", terapeak_avg_price_usd=100.0)
    rc2 = insert_research_candidate(title_ja="board24 B", terapeak_avg_price_usd=100.0)
    record_watch_ids(rc1, [101, 102])
    record_watch_ids(rc2, [102, 103])  # 102 は重複
    ids = get_research_watch_ids()
    assert {101, 102, 103} <= ids


def test_ignores_null_and_malformed_json():
    init_db()
    rc = insert_research_candidate(title_ja="board24 malformed", terapeak_avg_price_usd=100.0)
    # 不正 JSON を直接書いても例外を投げず無視されること (UI を止めない)
    with get_conn() as c:
        c.execute(
            "UPDATE research_candidates SET watch_ids_json=? WHERE rc_id=?",
            ("not-a-json", rc),
        )
    ids = get_research_watch_ids()  # 例外なく返る
    assert isinstance(ids, set)


def test_candidate_without_watch_not_included():
    init_db()
    rc = insert_research_candidate(title_ja="board24 no watch", terapeak_avg_price_usd=100.0)
    record_watch_ids(rc, [777])
    rc_nowatch = insert_research_candidate(title_ja="board24 none", terapeak_avg_price_usd=100.0)
    ids = get_research_watch_ids()
    assert 777 in ids
    # watch 未登録の候補は何も寄与しない (watch_ids_json が NULL)
    with get_conn() as c:
        row = c.execute(
            "SELECT watch_ids_json FROM research_candidates WHERE rc_id=?", (rc_nowatch,)
        ).fetchone()
    assert row[0] is None
