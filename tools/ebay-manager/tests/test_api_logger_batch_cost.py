"""W108 DB cost_usd 過大評価バグ fix の単体テスト.

経緯:
  - Anthropic Message Batches API は 50% 割引だが、api_logger.py が通常価格で計算
    していた (~ 月 $50-100 過大計上).
  - fix: _estimate_cost_usd に is_batch 引数追加 + log_api_call で operation 名末尾
    '_batch' で auto-detect (caller 渡し忘れ silent regression 防止).

検証観点:
  T1. is_batch=True で input/output/cache_r/cache_w 全 token が 50% になる
  T2. operation 末尾 '_batch' で is_batch auto-detect → cost 50%
  T3. 非 batch operation は通常価格 (auto-detect が誤動作しない)
  T4. init_db 2 回連続実行で is_batch カラムが消えない (Q2 冪等性)

注意: tests は production DB (data/monitor.db) を使う既存パターンに従う.
レコードは reviewer/operation で identification できる.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.api_logger import _estimate_cost_usd, log_api_call  # noqa: E402
from monitor.database import get_conn, init_db  # noqa: E402


# 既存 DB に test 行が紛れないよう test 専用 operation 名で識別
_TEST_OP_BATCH = "test_w108_candidate_evaluate_batch"
_TEST_OP_REALTIME = "test_w108_candidate_evaluate"
_TEST_MODEL = "claude-sonnet-4-6"


def _cleanup_test_rows():
    """前 run の残骸を削除. test 間の独立性確保."""
    with get_conn() as c:
        c.execute(
            "DELETE FROM api_call_log WHERE operation IN (?, ?)",
            (_TEST_OP_BATCH, _TEST_OP_REALTIME),
        )
        c.commit()


# T1
def test_estimate_cost_batch_halves_all_token_types():
    """is_batch=True は input/output/cache_read/cache_write 全 token に 50% を一律適用.
    公式 docs: 'These multipliers stack with the Batch API discount'."""
    kwargs = dict(
        model=_TEST_MODEL,
        in_tok=1_000_000, out_tok=1_000_000,
        cache_r=1_000_000, cache_w=1_000_000,
    )
    normal = _estimate_cost_usd(**kwargs, is_batch=False)
    batch = _estimate_cost_usd(**kwargs, is_batch=True)
    assert abs(batch - normal * 0.5) < 1e-9, (
        f"batch should be exactly 50% of normal, got {batch=} {normal=}"
    )


# T2
def test_log_api_call_auto_detects_batch_suffix_via_operation_name():
    """operation 名末尾 '_batch' で is_batch=True 自動判定 (silent regression 防止)."""
    init_db()
    _cleanup_test_rows()

    log_api_call(
        provider="anthropic",
        model=_TEST_MODEL,
        operation=_TEST_OP_BATCH,  # 末尾 '_batch'
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    with get_conn() as c:
        row = c.execute(
            "SELECT is_batch, cost_usd FROM api_call_log WHERE operation=? ORDER BY id DESC LIMIT 1",
            (_TEST_OP_BATCH,),
        ).fetchone()
    assert row is not None, "log_api_call が DB に書込まれていない"
    is_batch, cost = row
    assert is_batch == 1, f"operation 末尾 _batch で is_batch=1 を期待、実 {is_batch}"
    # 1M in + 1M out, sonnet $3 + $15 = $18, batch 50% = $9
    assert abs(cost - 9.0) < 1e-3, f"50% billed cost を期待 ($9), 実 ${cost}"

    _cleanup_test_rows()


# T3
def test_log_api_call_non_batch_operation_full_price():
    """末尾 '_batch' でない operation は通常価格 (auto-detect が誤動作しない)."""
    init_db()
    _cleanup_test_rows()

    log_api_call(
        provider="anthropic",
        model=_TEST_MODEL,
        operation=_TEST_OP_REALTIME,  # 末尾 '_batch' でない
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    with get_conn() as c:
        row = c.execute(
            "SELECT is_batch, cost_usd FROM api_call_log WHERE operation=? ORDER BY id DESC LIMIT 1",
            (_TEST_OP_REALTIME,),
        ).fetchone()
    assert row is not None
    is_batch, cost = row
    assert is_batch == 0, f"non-batch operation で is_batch=0 を期待、実 {is_batch}"
    # 1M in + 1M out, sonnet $3 + $15 = $18 (非 batch)
    assert abs(cost - 18.0) < 1e-3, f"通常価格 ($18) を期待、実 ${cost}"

    _cleanup_test_rows()


# T5 (2026-07-01 Sonnet 5 移行): sonnet-5 が _PRICING に登録され batch/通常とも正価格
def test_sonnet5_batch_and_realtime_cost():
    """claude-sonnet-5 ($3/$15) で 1M in + 1M out = $18、batch 50% = $9。
    _PRICING 未登録なら cost=0 で fail = $0 過小計上事故の検出。"""
    kwargs = dict(model="claude-sonnet-5", in_tok=1_000_000, out_tok=1_000_000)
    normal = _estimate_cost_usd(**kwargs, is_batch=False)
    batch = _estimate_cost_usd(**kwargs, is_batch=True)
    assert abs(normal - 18.0) < 1e-3, f"sonnet-5 通常価格 $18 を期待、実 ${normal}"
    assert abs(batch - 9.0) < 1e-3, f"sonnet-5 batch 価格 $9 を期待、実 ${batch}"


# T4
def test_init_db_idempotent_preserves_is_batch_column():
    """Q2 冪等性: init_db 2 回連続実行で is_batch カラム + データが残る."""
    init_db()
    _cleanup_test_rows()
    log_api_call(
        provider="anthropic",
        model=_TEST_MODEL,
        operation=_TEST_OP_BATCH,
        input_tokens=100,
        output_tokens=100,
    )
    init_db()  # 2 回目: ALTER TABLE は OperationalError catch で no-op
    with get_conn() as c:
        row = c.execute(
            "SELECT is_batch FROM api_call_log WHERE operation=? ORDER BY id DESC LIMIT 1",
            (_TEST_OP_BATCH,),
        ).fetchone()
    assert row is not None and row[0] == 1, (
        "init_db 2 回呼び出しで is_batch カラム / データが消失した (冪等性違反)"
    )

    _cleanup_test_rows()
