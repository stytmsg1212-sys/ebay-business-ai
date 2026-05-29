"""W23 Research 脳 — 軽量 vs 深思考のルーティング判定.

軽い問い (light): 単純な事実確認・カテゴリ ID 検索・短い計算 → Haiku で済ませる
重い問い (heavy): 戦略・設計・妥当性判断・複数文脈統合 → Opus 4.8 + extended thinking

設計方針 (Karpathy K1):
- パターンマッチで決め打ち、ML は使わない
- borderline は heavy 寄り (誤って Haiku 化して品質低下を避ける)
- force パラメータで明示指定可能
"""
from __future__ import annotations

import re
from typing import Literal

LIGHT_PATTERNS = [
    r"\bHS[\s-]*code\b",
    r"\bcategory[\s_]*id\b",
    r"^\s*\d+\s*g\s*$",  # "100g" 等の単純な単位
    r"^\s*\$\d+",  # 価格確認
    r"いくら",
    r"何円",
    r"^\s*[a-zA-Z0-9_-]+\s*とは\s*$",  # 用語確認
]

HEAVY_PATTERNS = [
    r"戦略", r"設計", r"妥当", r"判断", r"なぜ", r"どう考える",
    r"比較", r"トレードオフ", r"アーキテクチャ", r"設計方針",
    r"strategy", r"design", r"reason", r"trade.*off", r"architecture",
    # eBay 業務系で深い判断が必要
    r"値付け", r"出品判断", r"仕入判断", r"通関", r"VeRO",
    # システム開発系で深い判断
    r"hook", r"agent", r"subagent", r"CLAUDE\.md",
]

# Source 別のデフォルト model
SOURCE_MODEL_DEFAULTS = {
    "ui_chat": None,  # auto
    "morning_brief": "claude-opus-4-8",  # 朝の重点提案は深く考える
    "supplier_escalation": "claude-opus-4-8",  # 仕入判断 = 金銭損失リスク
    "feature_dev": "claude-opus-4-8",  # 設計判断 = 後続コスト大
    "listing_review": "claude-opus-4-8",  # 出品レビュー = eBay ポリシー責任
    "news_deep_dive": "claude-opus-4-8",  # ニュース深掘り = 業務影響評価
    "morning_discovery": "claude-opus-4-8",  # W122 朝の新商品発掘 = 5 階層深掘り
}


def choose_model(
    query: str,
    source: str = "ui_chat",
    force: Literal["opus", "haiku", "auto"] = "auto",
) -> tuple[str, bool]:
    """Returns: (model_id, enable_thinking)

    Rules (優先順):
      1. force == 'opus' → Opus + thinking
      2. force == 'haiku' → Haiku + no thinking
      3. SOURCE_MODEL_DEFAULTS で source に決め打ち model あれば採用
      4. HEAVY_PATTERN マッチ → Opus + thinking
      5. LIGHT_PATTERN マッチ かつ len(query) < 100 → Haiku
      6. デフォルト → Opus (安全側、品質低下を避ける)
    """
    if force == "opus":
        return "claude-opus-4-8", True
    if force == "haiku":
        return "claude-haiku-4-5-20251001", False

    # Source 別デフォルト
    src_model = SOURCE_MODEL_DEFAULTS.get(source)
    if src_model == "claude-opus-4-8":
        # ただし HEAVY_PATTERN でなければ thinking 不要 (cost 削減)
        is_heavy = any(re.search(p, query, re.IGNORECASE) for p in HEAVY_PATTERNS)
        return src_model, is_heavy

    # Pattern based
    is_heavy = any(re.search(p, query, re.IGNORECASE) for p in HEAVY_PATTERNS)
    if is_heavy:
        return "claude-opus-4-8", True

    is_light = any(re.search(p, query, re.IGNORECASE) for p in LIGHT_PATTERNS)
    if is_light and len(query) < 100:
        return "claude-haiku-4-5-20251001", False

    # デフォルト Opus (Karpathy K0 の "Don't hide confusion" — 不確実なら深く考える)
    return "claude-opus-4-8", False  # thinking off (cost 抑制)


if __name__ == "__main__":
    tests = [
        ("Section 232 の HS code 8516 該当?", "ui_chat"),
        ("Section 232 関税で家電の値付け戦略どう変える?", "ui_chat"),
        ("リサーチ脳の設計妥当性は?", "feature_dev"),
        ("PIONEER ジャンク 採用すべき?", "supplier_escalation"),
        ("100g", "ui_chat"),
        ("いくら?", "ui_chat"),
    ]
    for q, s in tests:
        model, thinking = choose_model(q, source=s)
        print(f"  query='{q[:40]}' source={s} → {model} thinking={thinking}")
