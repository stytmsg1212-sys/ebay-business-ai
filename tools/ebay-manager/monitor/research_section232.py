#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Section 232 該当フラグ推定 (ルールベース純関数).

設計書: .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md §14-Q4
KB:    .company/ebay-knowledge/topics/section_232_tariff_2026_04.md

商品タイトル (日本語) のキーワード辞書で Annex I-A / I-B / III フラグを推定する。
最終 HS コード分類は人間 (スコープ外 / 設計書 §1)。

戻り形式:
    {
        "flag": bool,                 # Section 232 該当の可能性あり
        "annex": "I-A"|"I-B"|"III"|None,   # 該当 Annex (flag=False なら None)
        "rate": float|None,           # 推定税率 (0.50/0.25/0.15 or None)
        "matched_keyword": str|None,  # 最初にマッチしたキーワード
    }
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# キーワード辞書 (KB の HS リストから実用的なものを抽出)
# 評価順: I-A (最高税率) → I-B → III → 対象外
# 各エントリ: (キーワードリスト, Annex, 税率)
# ---------------------------------------------------------------------------

_ANNEX_IA_KEYWORDS: tuple[str, ...] = (
    # HS 73xx: 鉄鋼製品 (ストーブ/鍋/フライパン/保温ジャー等)
    "鋳鉄",
    "鉄鋳物",
    "南部鉄器",
    "ステンレス鍋",
    "ホーロー鍋",
    "フライパン",
    "ストーブ",
    "鉄瓶",
    "スキレット",
    "鉄板",
    "プレスパン",
    # HS 76xx: アルミ製品
    "アルミ鍋",
    "アルミパン",
    "アルミダイキャスト",
    # HS 74xx: 銅製品
    "銅鍋",
    "銅製",
)

_ANNEX_IB_KEYWORDS: tuple[str, ...] = (
    # HS 8516.60: 電気炊飯器/オーブン
    "炊飯器",
    "炊飯ジャー",
    "電気鍋",
    "電気オーブン",
    "オーブンレンジ",
    "トースター",
    "ホットプレート",
    # HS 8418: 冷蔵/冷凍
    "冷蔵庫",
    "冷凍庫",
    "冷温庫",
    "ワインセラー",
    "チラー",
    # HS 8415: エアコン
    "エアコン",
    "空調",
    "クーラー",
    # HS 8504: 変圧器
    "変圧器",
    "トランス",
    "トランスフォーマー",
    "昇圧器",
    "降圧器",
    # HS 8501: 特定モーター
    "インダクションモーター",
    "サーボモーター",
    # HS 8708: 自動車部品
    "バンパー",
    "シャシー",
    "ホイール",
    "マフラー",
    "エキゾーストマニホールド",
    # HS 8544: 絶縁電線/ケーブル (鉄芯・アルミ芯)
    "鋼心アルミより線",
    "鋼心線",
)

_ANNEX_III_KEYWORDS: tuple[str, ...] = (
    # HS 8421.29: 液体ろ過装置
    "液体フィルタ",
    "産業用フィルタ",
    "液体ろ過",
    # HS 8428: コンベア/産業ロボット
    "コンベア",
    "産業ロボット",
    "スカラロボット",
    "多関節ロボット",
)


def estimate_section232(title_ja: str) -> dict:
    """商品タイトルから Section 232 該当フラグを推定する (純関数、副作用なし).

    Args:
        title_ja: 商品タイトル (日本語混じり可)。

    Returns:
        {
            "flag": bool,
            "annex": "I-A"|"I-B"|"III"|None,
            "rate": float|None,
            "matched_keyword": str|None,
        }
    """
    if not title_ja:
        return {"flag": False, "annex": None, "rate": None, "matched_keyword": None}

    # Annex I-A (50%) — 最高税率なので最優先
    for kw in _ANNEX_IA_KEYWORDS:
        if kw in title_ja:
            return {
                "flag": True,
                "annex": "I-A",
                "rate": 0.50,
                "matched_keyword": kw,
            }

    # Annex I-B (25%)
    for kw in _ANNEX_IB_KEYWORDS:
        if kw in title_ja:
            return {
                "flag": True,
                "annex": "I-B",
                "rate": 0.25,
                "matched_keyword": kw,
            }

    # Annex III (15% transitional)
    for kw in _ANNEX_III_KEYWORDS:
        if kw in title_ja:
            return {
                "flag": True,
                "annex": "III",
                "rate": 0.15,
                "matched_keyword": kw,
            }

    return {"flag": False, "annex": None, "rate": None, "matched_keyword": None}
