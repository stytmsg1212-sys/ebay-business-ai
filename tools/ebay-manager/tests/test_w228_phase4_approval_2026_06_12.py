#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 Phase 4 承認キュー + 下書き自動生成 テスト.

設計書: .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md §7

カバレッジ:
  (a) awaiting_approval → approved → draft_generated の遷移 + listing_draft_id 記録
  (b) W226 description 生成失敗時に needs_review 遷移 + reason 記録 (偽装成功なし)
  (c) 在庫0上限ガード: 上限以下 → watch 登録 + watch_ids_json 記録
  (d) 在庫0上限ガード: 上限超過 → watch スキップ + 警告痕跡 (logger.warning)
  (e) 見送り → gate_rejected

mock 方針:
  - W226 パイプライン / Discord / Playwright / Claude API は monkeypatch
  - DB は実際の SQLite (tmp DB) を使用 (本番 DB には書き込まない)
  - Streamlit session_state の副作用は _run_approval_logic のロジック層のみテスト
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# ヘルパ: テスト用候補行 (awaiting_approval)
# ---------------------------------------------------------------------------

def _make_approval_candidate(
    title_ja: str = 'テスト商品 Sony WH-1000XM5',
    found_url: str = 'https://jp.mercari.com/item/m12345',
    found_price_jpy: int = 18000,
    manual_weight_g: float = 280.0,
    found_condition_ja: Optional[str] = '美品',
) -> dict:
    """_run_approval_logic に渡す rc 辞書のモック."""
    return {
        'rc_id': 999,  # 呼出側で上書き
        'title_ja': title_ja,
        'found_url': found_url,
        'found_price_jpy': found_price_jpy,
        'manual_weight_g': manual_weight_g,
        'found_condition_ja': found_condition_ja,
        'profit_jpy_true': 4200,
        'profit_usd_true': 28.0,
        'keisuke_pass': 1,
        'keisuke_detail_json': '{"pass_600": true}',
        'section232_flag': 0,
        'weight_source': 'ai_estimate',
        'weight_confidence': 'medium',
        'harvest_pattern': None,
        'ebay_avg_sold_price_usd': 180.0,
        'ebay_total_sold': 5,
        'gate_inputs_json': '{"sold_1_2yr": 18}',
        'gate_decision': 'target_instock',
        'match_score': 82,
        'match_reason': '型番・色一致',
    }


def _default_config(max_oos: int = 20) -> dict:
    """_run_approval_logic に渡す config のモック."""
    return {
        'tasks_enabled': {
            'research_harvest': {
                'max_oos_active_listings': max_oos,
            },
        },
    }


# ---------------------------------------------------------------------------
# (a) awaiting_approval → approved → draft_generated の遷移 + listing_draft_id 記録
# ---------------------------------------------------------------------------

def test_approval_happy_path_transitions_and_draft_id():
    """承認の正常系: awaiting_approval → approved → draft_generated + draft_id 記録."""
    from monitor.database import init_db, get_conn
    from monitor.research_candidates_db import (
        insert_research_candidate,
        update_status,
        get_research_candidate,
        STATUS_AWAITING_APPROVAL,
    )
    from tabs.tab_w228_research import _run_approval_logic

    init_db()

    # テスト候補を awaiting_approval で作成
    rc_id = insert_research_candidate(title_ja='Phase4-Test-A Sony WH-1000XM5')
    update_status(rc_id, 'gate_passed')   # new → gate_passed
    update_status(rc_id, 'sourcing')      # gate_passed → sourcing
    update_status(rc_id, 'sourced')       # sourcing → sourced
    update_status(rc_id, STATUS_AWAITING_APPROVAL)  # sourced → awaiting_approval

    rc = _make_approval_candidate()
    rc['rc_id'] = rc_id
    config = {}  # H-2: app.py config は max_oos に使用しない

    # W226 パイプラインと watch 登録を mock
    gen_ok = {
        'success': True,
        'description_html': '<p>Test description</p>',
        'rank_code': 'A',
        'title_en': 'Sony WH-1000XM5 Wireless Headphones',
        'message': '生成成功',
    }
    mock_watch_id = 77
    mock_watch_result = (mock_watch_id, True)

    # OOS count を 5 に固定 (上限 100 未満)
    with patch('tabs._supplier_description_pipeline.generate_supplier_description', return_value=gen_ok) as _p_gen, \
         patch('tabs.tab_w228_research._count_oos_active_listings', return_value=5), \
         patch('monitor.keyword_watch_db.add_watch', return_value=mock_watch_result) as _p_watch, \
         patch('tabs.tab_individual_listing._load_draft_into_form') as _p_prefill, \
         patch('tabs.tab_w228_research.st') as mock_st:
        # st.session_state を辞書で代替
        mock_st.session_state = {}
        # H-2: max_oos_limit で直接渡す (schedule_config.json 読み取りをバイパス)
        result = _run_approval_logic(rc_id=rc_id, rc=rc, config=config, max_oos_limit=100)

    assert result['success'] is True, f'失敗: {result["message"]}'
    assert result['draft_id'] is not None, 'draft_id が None'
    assert result['watch_registered'] is True
    assert mock_watch_id in result['watch_ids']
    assert result['needs_review_fallen'] is False

    # Step 4: W176 正規 prefill (_load_draft_into_form) が保存済み draft 全体で呼ばれること
    assert _p_prefill.call_count == 1, 'prefill (_load_draft_into_form) が呼ばれていない'
    prefill_draft = _p_prefill.call_args[0][0]
    assert prefill_draft['id'] == result['draft_id'], (
        f'prefill された draft id 不一致: {prefill_draft["id"]} != {result["draft_id"]}'
    )
    assert prefill_draft['supplier_url'] == rc['found_url']

    # DB 上の status と listing_draft_id を確認
    updated_rc = get_research_candidate(rc_id)
    assert updated_rc is not None
    assert updated_rc['status'] == 'draft_generated', (
        f'status が draft_generated でない: {updated_rc["status"]}'
    )
    assert updated_rc['listing_draft_id'] == result['draft_id'], (
        f'listing_draft_id 不一致: {updated_rc["listing_draft_id"]} != {result["draft_id"]}'
    )

    # M-2: draft の rank_code が found_condition_ja ではなく mock 生成結果 ('A') になること
    from monitor.database import get_conn
    with get_conn() as conn:
        draft_row = conn.execute(
            'SELECT rank_code FROM listing_drafts WHERE id = ?',
            (result['draft_id'],),
        ).fetchone()
    assert draft_row is not None
    assert draft_row[0] == 'A', (
        f'draft の rank_code が gen_result 由来でない: {draft_row[0]!r} (expected "A")'
    )


# ---------------------------------------------------------------------------
# (b) W226 生成失敗時に needs_review 遷移 + reason 記録 (偽装成功なし)
# ---------------------------------------------------------------------------

def test_approval_description_failure_falls_to_needs_review():
    """description 生成失敗 → needs_review 遷移、偽装成功なし."""
    from monitor.database import init_db
    from monitor.research_candidates_db import (
        insert_research_candidate,
        update_status,
        get_research_candidate,
        STATUS_AWAITING_APPROVAL,
    )
    from tabs.tab_w228_research import _run_approval_logic

    init_db()

    rc_id = insert_research_candidate(title_ja='Phase4-Test-B 生成失敗商品')
    update_status(rc_id, 'gate_passed')
    update_status(rc_id, 'sourcing')
    update_status(rc_id, 'sourced')
    update_status(rc_id, STATUS_AWAITING_APPROVAL)

    rc = _make_approval_candidate(title_ja='Phase4-Test-B 生成失敗商品')
    rc['rc_id'] = rc_id
    config = {}  # H-2: app.py config は max_oos に使用しない

    gen_fail = {
        'success': False,
        'description_html': '',
        'rank_code': '',
        'title_en': '',
        'message': 'Claude タイムアウト',
    }

    with patch('tabs._supplier_description_pipeline.generate_supplier_description', return_value=gen_fail), \
         patch('tabs.tab_w228_research.st') as mock_st:
        mock_st.session_state = {}
        result = _run_approval_logic(rc_id=rc_id, rc=rc, config=config, max_oos_limit=20)

    assert result['success'] is False
    assert result['needs_review_fallen'] is True
    assert 'Claude タイムアウト' in result['message']

    updated_rc = get_research_candidate(rc_id)
    assert updated_rc is not None
    assert updated_rc['status'] == 'needs_review', (
        f'status が needs_review でない: {updated_rc["status"]}'
    )
    assert updated_rc['needs_review_reason'] is not None
    assert 'description 生成失敗' in updated_rc['needs_review_reason']


# ---------------------------------------------------------------------------
# (c) 在庫0上限ガード: 上限以下 → watch 登録 + watch_ids_json 記録
# ---------------------------------------------------------------------------

def test_oos_guard_under_limit_registers_watch():
    """OOS count < max_oos → watch 登録 + watch_ids_json に記録."""
    from monitor.database import init_db, get_conn
    from monitor.research_candidates_db import (
        insert_research_candidate,
        update_status,
        get_research_candidate,
        STATUS_AWAITING_APPROVAL,
    )
    from tabs.tab_w228_research import _run_approval_logic

    init_db()

    rc_id = insert_research_candidate(title_ja='Phase4-Test-C OOS上限内')
    update_status(rc_id, 'gate_passed')
    update_status(rc_id, 'sourcing')
    update_status(rc_id, 'sourced')
    update_status(rc_id, STATUS_AWAITING_APPROVAL)

    rc = _make_approval_candidate(title_ja='Phase4-Test-C OOS上限内')
    rc['rc_id'] = rc_id
    config = {}  # H-2: app.py config は max_oos に使用しない

    gen_ok = {
        'success': True,
        'description_html': '<p>OOS guard test</p>',
        'rank_code': 'B',
        'title_en': 'Test Product',
        'message': '生成成功',
    }
    watch_id_1, watch_id_2 = 101, 102

    # add_watch が 2 回呼ばれる (mercari, yahoo_auctions)
    call_count = {'n': 0}
    def mock_add_watch(**kwargs):
        call_count['n'] += 1
        wid = watch_id_1 if call_count['n'] == 1 else watch_id_2
        return (wid, True)

    with patch('tabs._supplier_description_pipeline.generate_supplier_description', return_value=gen_ok), \
         patch('tabs.tab_w228_research._count_oos_active_listings', return_value=10), \
         patch('monitor.keyword_watch_db.add_watch', side_effect=mock_add_watch), \
         patch('tabs.tab_individual_listing._load_draft_into_form'), \
         patch('tabs.tab_w228_research.st') as mock_st:
        mock_st.session_state = {}
        result = _run_approval_logic(rc_id=rc_id, rc=rc, config=config, max_oos_limit=20)

    assert result['success'] is True
    assert result['watch_registered'] is True
    assert result['watch_skipped_oos_limit'] is False
    assert watch_id_1 in result['watch_ids']
    assert watch_id_2 in result['watch_ids']

    updated_rc = get_research_candidate(rc_id)
    assert updated_rc is not None
    assert updated_rc['watch_ids_json'] is not None
    watch_ids_stored = json.loads(updated_rc['watch_ids_json'])
    assert watch_id_1 in watch_ids_stored
    assert watch_id_2 in watch_ids_stored


# ---------------------------------------------------------------------------
# (d) 在庫0上限ガード: 上限超過 → watch スキップ + 警告痕跡
# ---------------------------------------------------------------------------

def test_oos_guard_over_limit_skips_watch(caplog):
    """OOS count >= max_oos → watch 登録スキップ、logger.warning が出る."""
    import logging
    from monitor.database import init_db
    from monitor.research_candidates_db import (
        insert_research_candidate,
        update_status,
        get_research_candidate,
        STATUS_AWAITING_APPROVAL,
    )
    from tabs.tab_w228_research import _run_approval_logic

    init_db()

    rc_id = insert_research_candidate(title_ja='Phase4-Test-D OOS上限超過')
    update_status(rc_id, 'gate_passed')
    update_status(rc_id, 'sourcing')
    update_status(rc_id, 'sourced')
    update_status(rc_id, STATUS_AWAITING_APPROVAL)

    rc = _make_approval_candidate(title_ja='Phase4-Test-D OOS上限超過')
    rc['rc_id'] = rc_id
    config = {}  # H-2: app.py config は max_oos に使用しない

    gen_ok = {
        'success': True,
        'description_html': '<p>OOS limit test</p>',
        'rank_code': 'A',
        'title_en': 'Test Product D',
        'message': '生成成功',
    }

    # OOS count を上限と同値 (= 超過) に設定
    with patch('tabs._supplier_description_pipeline.generate_supplier_description', return_value=gen_ok), \
         patch('tabs.tab_w228_research._count_oos_active_listings', return_value=5), \
         patch('tabs.tab_individual_listing._load_draft_into_form'), \
         patch('tabs.tab_w228_research.st') as mock_st, \
         patch('notifiers.discord_notifier.DiscordNotifier') as _p_discord, \
         caplog.at_level(logging.WARNING, logger='tabs.tab_w228_research'):
        mock_st.session_state = {}
        # M-1: Discord notifier の webhook_url を空にして送信をスキップさせる
        _p_discord.return_value.webhook_url = ''
        # H-2: max_oos_limit で直接渡す (上限 5 件)
        result = _run_approval_logic(rc_id=rc_id, rc=rc, config=config, max_oos_limit=5)

    assert result['success'] is True, f'失敗: {result["message"]}'
    assert result['watch_skipped_oos_limit'] is True
    assert result['watch_registered'] is False
    assert result['watch_ids'] == []
    # logger.warning が出ていること (Q0 痕跡)
    assert any('P0-3' in r.message or '上限' in r.message for r in caplog.records), (
        'OOS 上限超過時に logger.warning が出ていない'
    )
    # status は draft_generated に遷移済み (watch 失敗は status に影響しない)
    updated_rc = get_research_candidate(rc_id)
    assert updated_rc is not None
    assert updated_rc['status'] == 'draft_generated'


# ---------------------------------------------------------------------------
# (e) 見送り → gate_rejected
# ---------------------------------------------------------------------------

def test_rejection_transitions_to_gate_rejected():
    """見送りボタン: awaiting_approval → gate_rejected."""
    from monitor.database import init_db
    from monitor.research_candidates_db import (
        insert_research_candidate,
        update_status,
        get_research_candidate,
        STATUS_AWAITING_APPROVAL,
        STATUS_GATE_REJECTED,
    )

    init_db()

    rc_id = insert_research_candidate(title_ja='Phase4-Test-E 見送り商品')
    update_status(rc_id, 'gate_passed')
    update_status(rc_id, 'sourcing')
    update_status(rc_id, 'sourced')
    update_status(rc_id, STATUS_AWAITING_APPROVAL)

    # update_status で gate_rejected に遷移 (UI ボタンと同等)
    result = update_status(rc_id, STATUS_GATE_REJECTED)
    assert result is True

    updated_rc = get_research_candidate(rc_id)
    assert updated_rc is not None
    assert updated_rc['status'] == STATUS_GATE_REJECTED


# ---------------------------------------------------------------------------
# (f) record_listing_draft / record_watch_ids の単体テスト
# ---------------------------------------------------------------------------

def test_record_listing_draft_and_watch_ids():
    """record_listing_draft / record_watch_ids が DB に正しく書き込む."""
    from monitor.database import init_db
    from monitor.research_candidates_db import (
        insert_research_candidate,
        get_research_candidate,
        record_listing_draft,
        record_watch_ids,
    )

    init_db()

    rc_id = insert_research_candidate(title_ja='Phase4-Test-F helper検証')

    # record_listing_draft
    ok = record_listing_draft(rc_id, draft_id=999)
    assert ok is True
    rc = get_research_candidate(rc_id)
    assert rc is not None
    assert rc['listing_draft_id'] == 999

    # record_watch_ids
    ok2 = record_watch_ids(rc_id, watch_ids=[10, 20, 30])
    assert ok2 is True
    rc2 = get_research_candidate(rc_id)
    assert rc2 is not None
    stored = json.loads(rc2['watch_ids_json'])
    assert stored == [10, 20, 30]


def test_record_listing_draft_unknown_rc_id():
    """存在しない rc_id への record_listing_draft は False (ValueError でない)."""
    from monitor.database import init_db
    from monitor.research_candidates_db import record_listing_draft

    init_db()
    result = record_listing_draft(rc_id=99999, draft_id=1)
    assert result is False


def test_record_listing_draft_missing_rc_id():
    """rc_id=None の場合は ValueError."""
    from monitor.database import init_db
    from monitor.research_candidates_db import record_listing_draft

    init_db()
    with pytest.raises(ValueError, match='rc_id is required'):
        record_listing_draft(rc_id=None, draft_id=1)


# ---------------------------------------------------------------------------
# (f) watch-only 承認 (found_url 無し = not_found 再キュー監視候補)
#     2026-06-12 retrospective review H-1: found_url NULL 行が description 生成
#     失敗 → needs_review に必ず落ち、watch 登録 (唯一の目的) に到達できなかった
# ---------------------------------------------------------------------------

def _make_watch_candidate_in_db(title_ja: str) -> int:
    """not_found 再キュー経路で awaiting_approval に置いた候補を作る."""
    from monitor.database import init_db
    from monitor.research_candidates_db import (
        insert_research_candidate,
        update_status,
    )
    init_db()
    rc_id = insert_research_candidate(title_ja=title_ja)
    update_status(rc_id, 'sourcing')            # new → sourcing
    update_status(rc_id, 'not_found')           # sourcing → not_found
    update_status(rc_id, 'awaiting_approval')   # not_found → awaiting_approval (監視候補)
    return rc_id


def test_watch_only_approval_registers_watch_without_draft():
    """found_url=None 承認 → draft 生成なし + watch 登録 + watch_registered 終端.

    汚染防衛線: generate_supplier_description が呼ばれないこと =
    誤マッチ URL 由来の draft が二度と生まれないこと (rc 36 / draft #26 の再発防止)。
    """
    from monitor.research_candidates_db import get_research_candidate
    from tabs.tab_w228_research import _run_approval_logic

    rc_id = _make_watch_candidate_in_db('Phase4-Test-WatchOnly KEYENCE FS-N41N')
    rc = _make_approval_candidate(
        title_ja='Phase4-Test-WatchOnly KEYENCE FS-N41N',
        found_url=None, found_price_jpy=None, found_condition_ja=None,
    )
    rc['rc_id'] = rc_id
    rc['profit_jpy_true'] = None
    rc['profit_usd_true'] = None

    calls = []

    def mock_add_watch(**kwargs):
        calls.append(kwargs)
        return (700 + len(calls), True)

    with patch('tabs._supplier_description_pipeline.generate_supplier_description') as _p_gen, \
         patch('tabs.tab_w228_research._count_oos_active_listings', return_value=5), \
         patch('monitor.keyword_watch_db.add_watch', side_effect=mock_add_watch), \
         patch('tabs.tab_w228_research.st') as mock_st:
        mock_st.session_state = {}
        result = _run_approval_logic(rc_id=rc_id, rc=rc, config={}, max_oos_limit=20)

    assert result['success'] is True, f'失敗: {result["message"]}'
    assert result['draft_id'] is None, '監視候補で draft が生成された (虚偽原価 draft 汚染)'
    assert result['watch_registered'] is True
    assert len(result['watch_ids']) == 2  # mercari + yahoo_auctions
    assert result['needs_review_fallen'] is False
    # 汚染防衛線: description パイプラインは一切呼ばれない
    assert _p_gen.call_count == 0, 'found_url 無しで description 生成が呼ばれた'
    # price_max_jpy は found_price_jpy=None を引き継ぐ (上限は手動設定)
    assert all(c['price_max_jpy'] is None for c in calls)

    updated_rc = get_research_candidate(rc_id)
    assert updated_rc['status'] == 'watch_registered', (
        f'status が watch_registered でない: {updated_rc["status"]}'
    )
    assert json.loads(updated_rc['watch_ids_json'] or '[]') == result['watch_ids']


def test_watch_only_approval_oos_over_limit_falls_to_needs_review():
    """found_url=None + 在庫0上限超過 → watch 未登録 + needs_review 可視化 (Q0)."""
    from monitor.research_candidates_db import get_research_candidate
    from tabs.tab_w228_research import _run_approval_logic

    rc_id = _make_watch_candidate_in_db('Phase4-Test-WatchOnly-OOS 上限超過商品')
    rc = _make_approval_candidate(
        title_ja='Phase4-Test-WatchOnly-OOS 上限超過商品',
        found_url=None, found_price_jpy=None, found_condition_ja=None,
    )
    rc['rc_id'] = rc_id

    with patch('tabs.tab_w228_research._count_oos_active_listings', return_value=25), \
         patch('monitor.keyword_watch_db.add_watch') as _p_watch, \
         patch('tabs.tab_w228_research.st') as mock_st:
        mock_st.session_state = {}
        result = _run_approval_logic(rc_id=rc_id, rc=rc, config={}, max_oos_limit=20)

    assert result['success'] is False
    assert result['watch_skipped_oos_limit'] is True
    assert result['needs_review_fallen'] is True
    assert _p_watch.call_count == 0, '上限超過で watch 登録が走った'

    updated_rc = get_research_candidate(rc_id)
    assert updated_rc['status'] == 'needs_review'
    assert '上限' in (updated_rc['needs_review_reason'] or '')


def test_watch_only_approval_partial_failure_records_registered_ids():
    """M-1: mercari 成功 + yahoo 失敗 → needs_review + 登録済 watch_id は記録される."""
    from monitor.research_candidates_db import get_research_candidate
    from tabs.tab_w228_research import _run_approval_logic

    rc_id = _make_watch_candidate_in_db('Phase4-Test-WatchOnly-Partial 部分失敗商品')
    rc = _make_approval_candidate(
        title_ja='Phase4-Test-WatchOnly-Partial 部分失敗商品',
        found_url=None, found_price_jpy=None, found_condition_ja=None,
    )
    rc['rc_id'] = rc_id

    with patch('tabs.tab_w228_research._count_oos_active_listings', return_value=5), \
         patch('monitor.keyword_watch_db.add_watch',
               side_effect=[(701, True), RuntimeError('yahoo down')]), \
         patch('tabs.tab_w228_research.st') as mock_st:
        mock_st.session_state = {}
        result = _run_approval_logic(rc_id=rc_id, rc=rc, config={}, max_oos_limit=20)

    assert result['success'] is False
    assert result['needs_review_fallen'] is True
    assert result['watch_ids'] == [701]  # mercari 分は呼出元に返る

    updated_rc = get_research_candidate(rc_id)
    assert updated_rc['status'] == 'needs_review'
    assert 'yahoo_auctions' in (updated_rc['needs_review_reason'] or '')
    # M-1: 部分成功分の対応関係が DB に残る (orphan watch 痕跡保全)
    assert json.loads(updated_rc['watch_ids_json'] or '[]') == [701]


# ---------------------------------------------------------------------------
# import guard (import エラーを早期発見)
# ---------------------------------------------------------------------------

def test_imports_work():
    """Phase 4 関連モジュールが import エラーなし."""
    from tabs.tab_w228_research import (
        _run_approval_logic,
        _count_oos_active_listings,
        _render_section_d,
        STATUS_APPROVED,
        STATUS_DRAFT_GENERATED,
        STATUS_AWAITING_APPROVAL,
        STATUS_GATE_REJECTED,
    )
    assert callable(_run_approval_logic)
    assert callable(_count_oos_active_listings)
    assert callable(_render_section_d)
