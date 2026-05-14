#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仕入先候補自動探索タスク（#9）

在庫切れになった商品に対して、メルカリ / ヤフオク等を横断検索し、
Claude API（スクショ＋テキスト精読）で元商品との類似度 match_score(0-100) を判定、
利益計算で採算可否を付けて supplier_candidates テーブルに登録する。

Pattern 1 (async): inventory_check が在庫切れを検出した直後、threading.Thread で起動
Pattern 2 (batch): daily_scheduler の朝バッチで、長期在庫切れ全件をスイープ

MVP（本ファイル）:
  - エントリポイント `run_supplier_candidate_search(ebay_item_id, sku, config, discovered_via)` の骨格
  - Claude API / web検索ブロックは stub。関数シグネチャと DB 書き込みまでを通す。
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calculator import (  # noqa: E402
    CalcInput,
    calculate,
    check_supplier_candidate_profitable,
    load_settings,
)
from monitor.database import (  # noqa: E402
    add_supplier_candidate, get_ebay_listing_by_item_id,
)
from monitor.claude_evaluator import evaluate_match, EvaluationResult  # noqa: E402

logger = logging.getLogger(__name__)

# 置換候補 (alt=0) の保存時 match_score 下限。settings.json で上書き可 (T6).
MATCH_SCORE_THRESHOLD = 60

# 別SKU出品機会 (alt=1) の保存時 match_score 下限。Q1=B/y 合意で 20 に制限.
# 旧: 制限なし → score<20 のゴミ 158 件が UI を圧迫
# 新: score<20 は保存スキップ
ALT_LISTING_SCORE_THRESHOLD = 20


def _get_threshold(settings: dict, key: str, default: int) -> int:
    """settings.json から閾値を取得、未設定時は default."""
    v = (settings or {}).get(key)
    try:
        if v is not None:
            return int(v)
    except (TypeError, ValueError):
        pass
    return default


def _normalize_url(u: str) -> str:
    """URL 比較用正規化 (2026-04-25 self-source bug 対策).

    吸収する揺れ:
      - scheme 違い (http/https) — host+path のみ比較
      - host のサブドメイン揺れ:
        * `page.auctions.yahoo.co.jp` ↔ `auctions.yahoo.co.jp`
        * `www.` 接頭辞
      - 大小文字
      - 末尾スラッシュ
      - クエリ (`?from=search` 等)
      - フラグメント (`#section`)
    """
    from urllib.parse import urlsplit
    u = (u or '').strip()
    if not u:
        return ''
    try:
        sp = urlsplit(u)
    except (ValueError, AttributeError):
        return u.lower()
    host = (sp.netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    # ヤフオク サブドメイン正規化
    if host == 'page.auctions.yahoo.co.jp':
        host = 'auctions.yahoo.co.jp'
    path = (sp.path or '').rstrip('/').lower()
    return f"{host}{path}"


@dataclass
class CandidateHit:
    """探索結果1件分。Claude評価前の生データ。"""
    source_platform: str
    url: str
    price_jpy: Optional[int]
    title: Optional[str]
    image_url: Optional[str] = None


@dataclass
class ScoredCandidate:
    """match_score 付き候補"""
    hit: CandidateHit
    match_score: int
    match_reasoning: str
    junk_likely_untested: bool = False
    alt_listing_possible: bool = False
    alt_listing_note: str = ""


def _estimate_profit_for_candidate(
    listing: dict,
    purchase_yen: int,
    settings: dict,
) -> Optional[float]:
    """
    ebay_listings の物理データ＋販売価格と仕入候補価格から、
    代表利益（profit_with_refund）を推定する。

    代表値の選び方:
      1. 送料サービス別に全計算を実行
      2. `is_listable=True` かつ profit_with_refund が最大のものを採用
      3. listable が1件もなければ全サービス中 profit_with_refund 最大

    推定不能条件（物理データ不足等）は None 返却。
    """
    current_price_usd = listing.get("current_price")
    if current_price_usd is None or current_price_usd <= 0:
        return None

    # 2026-04-22 変更: weight_g 未取得/負値で候補登録を止めていた ガードを撤去。
    # calculator が内部で最低 0.5kg 課金に clip してくれるため、weight_g=0 のまま
    # 通しても送料計算は可能。ユーザー要望: 「質量・サイズ誤りで利益候補から
    # 漏れるのを避けたい」→ weight_g=0 で計算して最小送料シナリオで利益を出す。
    weight_g = float(listing.get("weight_g") or 0)
    if weight_g < 0:
        weight_g = 0.0

    inp = CalcInput(
        purchase_yen=float(purchase_yen),
        item_price_usd=float(current_price_usd),
        weight_g=weight_g,
        length_cm=float(listing.get("length_cm") or 0),
        width_cm=float(listing.get("width_cm") or 0),
        height_cm=float(listing.get("height_cm") or 0),
        category_id=0,          # 未取得のためデフォルトFVFレート適用
        is_ddu=False,           # デフォルト DDP（eBay SpeedPAKはDDP基本）
        country_code="US",      # 最多販売先をデフォルト
    )

    try:
        result = calculate(inp, settings)
    except Exception as e:
        logger.warning(f"profit calculation failed for sku={listing.get('sku')}: {e}")
        return None

    if not result.service_results:
        return None

    listable = [s for s in result.service_results if s.is_listable]
    pool = listable or result.service_results
    best = max(pool, key=lambda s: s.profit_with_refund)
    return float(best.profit_with_refund)


# ─── 以下は次セッションで実装する stub ───

def search_candidates_on_platform(
    platform: str,
    keywords: str,
    max_results: int = 5,
) -> list[CandidateHit]:
    """
    プラットフォーム検索ディスパッチャ。

    対応プラットフォーム（2026-04-19時点）:
      - mercari: monitor.mercari_search.search_mercari (Playwright)
      - yahoo_auctions: monitor.yahoo_search.search_yahoo (Playwright)
      - paypay_furima: 未実装
    """
    if platform == "mercari":
        from monitor.mercari_search import search_mercari
        raw = search_mercari(keywords, max_results=max_results)
        return [
            CandidateHit(
                source_platform="mercari",
                url=h.url,
                price_jpy=h.price_jpy,
                title=h.title,
                image_url=h.image_url,
            )
            for h in raw
        ]

    if platform == "yahoo_auctions":
        from monitor.yahoo_search import search_yahoo
        raw = search_yahoo(keywords, max_results=max_results)
        return [
            CandidateHit(
                source_platform="yahoo_auctions",
                url=h.url,
                price_jpy=h.price_jpy,
                title=h.title,
                image_url=h.image_url,
            )
            for h in raw
        ]

    if platform == "paypay_furima":
        from monitor.paypay_search import search_paypay
        raw = search_paypay(keywords, max_results=max_results)
        return [
            CandidateHit(
                source_platform="paypay_furima",
                url=h.url,
                price_jpy=h.price_jpy,
                title=h.title,
                image_url=h.image_url,
            )
            for h in raw
        ]

    logger.debug(f"search_candidates_on_platform: platform {platform!r} not yet supported")
    return []


def evaluate_candidate_with_claude(
    hit: CandidateHit,
    ebay_title: str,
    ebay_image_url: Optional[str] = None,
    sku: Optional[str] = None,
    ebay_item_id: Optional[str] = None,
) -> ScoredCandidate:
    """
    Claude API でスクショ＋テキストを精読し match_score(0-100) を算出。
    判定基準プロンプトは monitor.claude_evaluator.STABLE_PROMPT_TEMPLATE を参照。

    Phase 1 学習: ebay_item_id を渡すと evaluator が過去の accept/reject 履歴を参照し、
    ユーザー個別の判断パターンを反映して match_score を決める (2026-05-01 W81 で
    sku → ebay_item_id 主導に変更、stock:01 等の同 SKU 多 listing で他 listing
    判定が混入する学習汚染を解消).
    """
    result: EvaluationResult = evaluate_match(
        ebay_title=ebay_title,
        candidate_title=hit.title or "",
        platform=hit.source_platform,
        price_jpy=hit.price_jpy,
        url=hit.url,
        ebay_image_url=ebay_image_url,
        candidate_image_url=hit.image_url,  # URL を渡す想定（スクレイパが http(s)://... をセット）
        sku=sku,
        ebay_item_id=ebay_item_id,
    )
    if result.error:
        logger.warning(
            f"evaluate failed for {hit.url}: {result.error} (reason={result.reasoning!r})"
        )
    return ScoredCandidate(
        hit=hit,
        match_score=result.match_score,
        match_reasoning=result.reasoning,
        junk_likely_untested=result.junk_likely_untested,
        alt_listing_possible=result.alt_listing_possible,
        alt_listing_note=result.alt_listing_note,
    )


# ─── エントリポイント ───

def run_supplier_candidate_search(
    ebay_item_id: str,
    sku: str,
    config: dict,
    discovered_via: str = "pattern_1_async",
    platforms: Optional[list[str]] = None,
) -> dict:
    """
    1 listing 分の仕入先候補探索フロー (W75 4b: signature を ebay_item_id 主導に変更).

    Args:
        ebay_item_id: eBay listing の一意 ID. listing 識別 canonical key (.claude/rules/sku-rules.md 準拠).
        sku: SKU 文字列. Claude 学習履歴 prompt / log 表示 / DB record の補助情報.
            **listing 識別 key としては使わない** (有/無在庫判定 + 仕入先 URL 変換のみ用途).
        config: settings.json dict.
        discovered_via: 発見経路 (DB record / log 用ラベル).
        platforms: 探索プラットフォーム list. None で default 3 platform.

    Returns: {'success': bool, 'found': int, 'persisted': int, 'message': str}
    """
    platforms = platforms or ["mercari", "yahoo_auctions", "paypay_furima"]

    listing = get_ebay_listing_by_item_id(ebay_item_id)
    if not listing:
        return {'success': False, 'found': 0, 'persisted': 0,
                'message': f'ebay_item_id not found: {ebay_item_id}'}

    ebay_title = listing.get('title') or ''

    settings = load_settings()
    fx = float(settings.get('exchange_rate', 155.0))

    # 2026-04-25 バグ修正: 候補に元仕入先 URL と同一のものが混入する問題.
    # 原因: 検索ロジックが title マッチで元商品自身をヒットさせ、その URL を candidate
    # として登録 → user が「在庫切れだから候補開いたのに同じ売り切れページに飛ばされる」
    # という体験を引き起こす.
    # 対策: source_url と完全一致する hit を除外 (host/path 正規化で揺れ吸収).
    listing_source_url = (listing.get('source_url') or '').strip()
    listing_url_norm = _normalize_url(listing_source_url)

    all_scored: list[ScoredCandidate] = []
    excluded_self = 0
    for plat in platforms:
        hits = search_candidates_on_platform(plat, ebay_title)
        for h in hits:
            # 元仕入先 URL と一致する候補は除外 (売り切れた元商品ページを再提示しない)
            if listing_url_norm and _normalize_url(h.url) == listing_url_norm:
                excluded_self += 1
                logger.info(
                    f"skip self-source candidate: sku={sku} url={h.url}"
                )
                continue
            # ebay_item_id を渡すことで同 listing の過去判断履歴が Claude プロンプトに
            # 注入される (Phase 1 学習). sku は brand 検索の自己除外と DB record で使用.
            all_scored.append(evaluate_candidate_with_claude(
                h, ebay_title, sku=sku, ebay_item_id=ebay_item_id,
            ))

    # settings.json から閾値を動的取得 (T6 で UI から変更可能)
    _alt0_threshold = _get_threshold(settings, "supplier_alt0_score_threshold", MATCH_SCORE_THRESHOLD)
    _alt1_threshold = _get_threshold(settings, "supplier_alt1_score_threshold", ALT_LISTING_SCORE_THRESHOLD)

    persisted = 0
    alt_listed = 0
    skipped_unprofitable = 0
    skipped_low_score = 0
    for sc in all_scored:
        # 保存条件: 仕入先として閾値越え、または別SKU出品機会として拾える
        if sc.match_score < _alt0_threshold and not sc.alt_listing_possible:
            skipped_low_score += 1
            continue

        # 2026-04-24 Q1=B/y: alt=1 (別SKU出品機会) にも score 下限を設定
        # 旧: alt=1 は score 無制限で保存 → score<20 のゴミ 158 件が DB を汚染
        # 新: alt=1 かつ score<ALT_LISTING_SCORE_THRESHOLD は skip
        if sc.alt_listing_possible and sc.match_score < _alt1_threshold:
            skipped_low_score += 1
            logger.debug(
                f"skip low-score alt_listing: sku={sku} score={sc.match_score} "
                f"(threshold={_alt1_threshold})"
            )
            continue

        profit_jpy: Optional[float] = None
        profitable = 0
        if sc.hit.price_jpy is not None:
            profit_jpy = _estimate_profit_for_candidate(
                listing=listing,
                purchase_yen=sc.hit.price_jpy,
                settings=settings,
            )
            if profit_jpy is not None:
                ok, _breakdown = check_supplier_candidate_profitable(
                    profit_with_refund=profit_jpy,
                    purchase_yen=sc.hit.price_jpy,
                )
                profitable = int(ok)

        # 2026-04-23 追加 (Q2 B): 利益が出ない置換候補は DB に保存しない
        # ただし alt_listing_possible=1 (別SKU出品機会) は計算対象外で残す (Q3 B)
        if not sc.alt_listing_possible and not profitable:
            skipped_unprofitable += 1
            logger.debug(
                f"skip unprofitable: sku={sku} url={sc.hit.url} "
                f"profit={profit_jpy} profitable={profitable}"
            )
            continue

        # 2026-04-25 Opus 4.7 切替: 評価 model を candidate に記録. UI で識別可能に.
        from monitor.claude_evaluator import CLAUDE_MODEL as _eval_model
        row_id = add_supplier_candidate(
            sku=sku,
            candidate_url=sc.hit.url,
            source_platform=sc.hit.source_platform,
            candidate_price_jpy=sc.hit.price_jpy,
            candidate_title=sc.hit.title,
            match_score=sc.match_score,
            match_reasoning=sc.match_reasoning,
            profit_jpy=profit_jpy,
            profitable=profitable,
            ebay_item_id=ebay_item_id,
            discovered_via=discovered_via,
            junk_likely_untested=int(sc.junk_likely_untested),
            alt_listing_possible=int(sc.alt_listing_possible),
            alt_listing_note=sc.alt_listing_note or None,
            eval_model=_eval_model,
        )
        if row_id:
            persisted += 1
            if sc.match_score < MATCH_SCORE_THRESHOLD and sc.alt_listing_possible:
                alt_listed += 1

    logger.info(
        f"仕入先候補探索: sku={sku} found={len(all_scored)} persisted={persisted} "
        f"alt_listed={alt_listed} skipped_unprofitable={skipped_unprofitable} "
        f"skipped_low_score={skipped_low_score} excluded_self={excluded_self}"
    )

    # W100 (2026-05-06): リサーチ完了 = grace 待機の役目終了 → yahoo_grace_until クリア
    # H-1 fix: 条件付き UPDATE (yahoo_grace_until <= now) で race condition 防止.
    # inventory_check が新規セットした未来の grace を、別経路 sweep が誤って
    # NULL 化してしまう silent regression を防ぐ.
    if ebay_item_id:
        try:
            from monitor.database import clear_yahoo_grace_if_due
            clear_yahoo_grace_if_due(ebay_item_id)
        except Exception as _e:
            logger.warning(f"[grace] item={ebay_item_id} grace_until クリア失敗: {_e}")

    return {
        'success': True,
        'found': len(all_scored),
        'persisted': persisted,
        'alt_listed': alt_listed,
        'skipped_unprofitable': skipped_unprofitable,
        'skipped_low_score': skipped_low_score,
        'excluded_self': excluded_self,
        'message': (
            f'{persisted}/{len(all_scored)} persisted '
            f'(alt0>={_alt0_threshold}, alt1>={_alt1_threshold}, '
            f'alt_listing={alt_listed}, skipped_low_score={skipped_low_score}, '
            f'skipped_unprofitable={skipped_unprofitable}, '
            f'excluded_self={excluded_self})'
        ),
    }
