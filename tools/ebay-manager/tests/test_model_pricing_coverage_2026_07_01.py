"""本番モデル定数と価格表の整合性 機械検証 (Sonnet 5 移行 / doc §3 HIGH #7).

経緯:
  - モデル移行 (sonnet-4-6 → sonnet-5) で本番の CLAUDE_MODEL 定数を切替える際、
    api_logger._PRICING への価格登録を漏らすと cost_usd=0 で silent 過小計上
    ($0 事故) になる。過去 W108 の batch 過大計上と対の再発リスク。
  - このテストは「本番で使うモデル id が必ず _PRICING に存在」+「_PRICING /
    _TIER1 の全 id が billable token>0 で cost>0 を返す」を物理 block する。

検証観点:
  T1. 本番モデル定数 (claude_evaluator.CLAUDE_MODEL / listing_generator.CLAUDE_MODEL)
      が _PRICING に存在する。
  T2. _PRICING の全キーが _estimate_cost_usd(id, 1, 1) > 0 ($0 黙殺の検出)。
  T3. _TIER1_INPUT_TOKENS_PER_MIN の全キーが _PRICING で cost>0 (rate limit 対象
      モデルの価格漏れ検出)。
  T4. A/B test スクリプト (run_supplier_ab_test_*) の MODELS 全 id が _PRICING に
      存在する。A/B にモデルを足した時の $0 漏れも CI で捕捉。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from monitor.api_logger import _PRICING, _estimate_cost_usd  # noqa: E402
from monitor.claude_evaluator import (  # noqa: E402
    CLAUDE_MODEL as EVALUATOR_MODEL,
    _TIER1_INPUT_TOKENS_PER_MIN,
)
from monitor.listing_generator import CLAUDE_MODEL as GENERATOR_MODEL  # noqa: E402


# T1
def test_production_model_constants_exist_in_pricing():
    """本番で API に投げるモデル id は必ず _PRICING に登録されていること。
    未登録だと _estimate_cost_usd が 0.0 を返し cost が silent に過小計上される。"""
    for name, model_id in (
        ("claude_evaluator.CLAUDE_MODEL", EVALUATOR_MODEL),
        ("listing_generator.CLAUDE_MODEL", GENERATOR_MODEL),
    ):
        assert model_id in _PRICING, (
            f"{name}={model_id!r} が api_logger._PRICING に存在しない "
            f"($0 過小計上事故)。_PRICING に価格行を追加すること。"
        )


# T2
def test_all_pricing_keys_yield_positive_cost():
    """_PRICING の全モデルが billable token>0 で cost>0 を返す (cost 0 = 即 fail)。"""
    for model_id in _PRICING:
        cost = _estimate_cost_usd(model_id, 1, 1)
        assert cost > 0, (
            f"{model_id!r} は input=1/output=1 で cost>0 を期待、実 {cost}。"
            f"_PRICING の単価が 0 か欠落。"
        )


# T3
def test_all_tier1_ratelimit_keys_have_pricing():
    """rate limit 対象 (_TIER1_INPUT_TOKENS_PER_MIN) の全モデルが _PRICING で cost>0。"""
    for model_id in _TIER1_INPUT_TOKENS_PER_MIN:
        cost = _estimate_cost_usd(model_id, 1, 1)
        assert cost > 0, (
            f"rate limit 対象 {model_id!r} が _PRICING 未登録 (cost={cost})。"
            f"価格漏れ = $0 過小計上事故。"
        )


def _load_ab_test_models() -> list:
    """A/B test スクリプトの MODELS list を file path から動的ロード (scripts は
    非パッケージのため importlib で読む)。返値は (model_id, label) の list。"""
    ab_path = _PROJECT_ROOT / "scripts" / "run_supplier_ab_test_2026_05_01.py"
    assert ab_path.exists(), f"A/B test スクリプトが見つからない: {ab_path}"
    spec = importlib.util.spec_from_file_location("_ab_test_module", ab_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MODELS


# T4
def test_ab_test_models_exist_in_pricing():
    """A/B test スクリプトの MODELS 全 id が _PRICING に存在 (A/B にモデル追加時の
    $0 漏れ検出)。evaluate_match(model=...) に渡され課金対象になるため。"""
    models = _load_ab_test_models()
    assert models, "A/B test の MODELS が空。スクリプト構造変更を確認。"
    for entry in models:
        model_id = entry[0] if isinstance(entry, (tuple, list)) else entry
        cost = _estimate_cost_usd(model_id, 1, 1)
        assert cost > 0, (
            f"A/B test MODELS の {model_id!r} が _PRICING 未登録 (cost={cost})。"
            f"A/B で評価に投げると $0 過小計上。_PRICING に価格行を追加すること。"
        )
