"""
自動ランク付けシステム（Watch数・View数・販売数・伸び率ベース）
複合スコア（0-100）を計算し、固定スコア方式でS-A-B-C-D-Eランクを割り当て
"""

from typing import Optional

# ---- 設定（ユーザー指定） ----

# メトリクスの正規化基準値（最大値と想定）
# 注: eBay API から View数（HitCount）が取得できないため、Watch数主体で計算
METRICS_MAX_WATCH = 20          # Watch数の最大値（元: 50 → 調整: 20）
#   理由: Watch 0 と 1 の差を大きくするため、基準値を小さくする
#   結果: watch=1 で正規化値 5.0（元は 2.0）、watch=20 で 100.0
METRICS_MAX_SALES = 5           # 販売数(30d)の最大値（今後実装）

# メトリクスの重み付け（Watch + 伸び率主体、v1.0）
# ※ eBay Trading API では HitCount が取得できないため、View数は除外
# ※ v2.0 で REST API を導入時に View数を追加予定
WEIGHT_WATCH = 3.0              # Watch数: 3倍（主要指標、強化）
WEIGHT_SALES = 1.0              # 販売数: 1倍（今後）
WEIGHT_WATCH_GROWTH = 0.5       # Watch伸び率: 0.5倍
WEIGHT_VIEW_GROWTH = 0.3        # View伸び率: 0.3倍（プレースホルダ）

# ランク境界（固定スコア方式、ユーザー指定）
RANK_BOUNDARIES = {
    'S': 90,    # スコア >= 90
    'A': 75,    # スコア >= 75
    'B': 60,    # スコア >= 60
    'C': 45,    # スコア >= 45
    'D': 30,    # スコア >= 30
    'E': 0,     # スコア >= 0
}

# 送料検証設定
SHIPPING_COST_RATIO = 0.20          # 商品価格の何%を送料とするか（20%）
SHIPPING_TOLERANCE = 0.15           # 許容誤差（±15%）
# 計算例: price=$100 → expected_shipping=$20
#        許容範囲: $17 - $23
#        ※ただし、$0 か $30 の場合はシステムバグの可能性


def calculate_growth_rate(current: int, previous: int) -> float:
    """
    伸び率を計算（%単位）
    (current - previous) / max(previous, 1) * 100
    """
    if previous == 0:
        # 前回値が0の場合、現在値が存在すれば100%成長と扱う
        return 100.0 if current > 0 else 0.0
    return ((current - previous) / previous) * 100


def _normalize_metric(value: int, max_value: int) -> float:
    """メトリクスを0-100スケールに正規化"""
    if max_value <= 0:
        return 0.0
    return min((value / max_value) * 100, 100)


def calculate_metrics_score(item: dict) -> float:
    """
    複合スコアを計算（0-100）
    v1.0: Watch数 + 伸び率主体（View数は取得不可のため除外）

    Watch 0 と 1 を区別するため、基準値を小さくし、Watch = 0 を特別扱い

    注: eBay Trading API では HitCount（View数）が取得できないため、
    Watch数と伸び率に重点を置いています。
    v2.0 で REST API 導入時に View数を追加予定。
    """
    # メトリクス抽出（存在しないキーはデフォルト値）
    watch_count = item.get('watch_count', 0)
    sales_count_30d = item.get('sales_count_30d', 0)
    watch_growth_rate = item.get('watch_growth_rate', 0.0)
    view_growth_rate = item.get('view_growth_rate', 0.0)  # 今後用

    # Watch = 0 の場合は最低スコア（E ランク確定）
    if watch_count == 0:
        return 0.0

    # メトリクスを正規化
    normalized_watch = _normalize_metric(watch_count, METRICS_MAX_WATCH)
    normalized_sales = _normalize_metric(sales_count_30d, METRICS_MAX_SALES)

    # 伸び率を0-100スケールに正規化（-100%～+∞を0-100に圧縮）
    normalized_watch_growth = max(0, min(watch_growth_rate, 100.0))
    normalized_view_growth = max(0, min(view_growth_rate, 100.0))

    # 加重合算（Watch + 伸び率主体）
    raw_score = (
        (normalized_watch * WEIGHT_WATCH) +
        (normalized_sales * WEIGHT_SALES) +
        (normalized_watch_growth * WEIGHT_WATCH_GROWTH) +
        (normalized_view_growth * WEIGHT_VIEW_GROWTH)
    )

    # スコア合計重み
    total_weight = (
        WEIGHT_WATCH + WEIGHT_SALES +
        WEIGHT_WATCH_GROWTH + WEIGHT_VIEW_GROWTH
    )

    # スコアを0-100に正規化
    final_score = (raw_score / (total_weight * 100)) * 100
    return min(final_score, 100.0)


def assign_rank(item: dict) -> str:
    """
    watch_count と sales_count_30d から直接ランクを決定 (Option C 直接マッピング).

    優先順位:
      - watch=0 かつ sales=0 → E
      - sales>=5 or watch>=30 → S
      - sales>=3 or watch>=15 → A
      - sales>=2 or watch>=8 → B
      - sales>=1 or watch>=4 → C
      - watch>=2 → D
      - watch>=1 → D  (watch=0 と watch=1 を明確に区別する user 要望)
      - その他 → E

    旧 score-base 判定 (calculate_metrics_score) は UI ランク分布表示で
    引き続き使用するため削除しないこと。本関数は score を参照しない。
    """
    watch = int(item.get('watch_count', 0) or 0)
    sales = int(item.get('sales_count_30d', 0) or 0)
    if watch == 0 and sales == 0:
        return 'E'
    if sales >= 5 or watch >= 30:
        return 'S'
    if sales >= 3 or watch >= 15:
        return 'A'
    if sales >= 2 or watch >= 8:
        return 'B'
    if sales >= 1 or watch >= 4:
        return 'C'
    if watch >= 2:
        return 'D'
    if watch >= 1:
        return 'D'
    return 'E'


def auto_rank_all_listings(all_listings: list[dict]) -> dict:
    """
    すべての出品にランクを自動割り当て
    入力: [{ebay_item_id, sku, ..., watch_count, view_count, ...}]
    出力: {
        rank_assigned: int,
        errors: int,
        summary: {rank: [items]},
        details: [{item_id, sku, score, rank, ...}]
    }
    """
    rank_assigned = 0
    errors = 0
    rank_summary = {'S': [], 'A': [], 'B': [], 'C': [], 'D': [], 'E': []}
    details = []

    for item in all_listings:
        try:
            # スコア計算 (UI 表示用、ランク判定には使わない)
            score = calculate_metrics_score(item)

            # ランク割り当て (Option C: watch/sales 直接マッピング)
            rank = assign_rank(item)

            # 詳細情報を記録
            item_detail = {
                'ebay_item_id': item.get('ebay_item_id'),
                'sku': item.get('sku'),
                'title': item.get('title', ''),
                'score': round(score, 2),
                'rank': rank,
                'watch_count': item.get('watch_count', 0),
                'view_count': item.get('view_count', 0),
                'sales_count_30d': item.get('sales_count_30d', 0),
                'watch_growth_rate': round(item.get('watch_growth_rate', 0.0), 1),
                'view_growth_rate': round(item.get('view_growth_rate', 0.0), 1),
                'sales_growth_rate': round(item.get('sales_growth_rate', 0.0), 1),
            }

            rank_summary[rank].append(item_detail)
            details.append(item_detail)
            rank_assigned += 1

        except Exception as e:
            errors += 1
            print(f"Error processing item {item.get('ebay_item_id', '?')}: {e}")

    return {
        'rank_assigned': rank_assigned,
        'errors': errors,
        'summary': rank_summary,
        'details': details,
    }


def get_rank_stats_from_details(details: list[dict]) -> dict:
    """詳細情報からランク別統計を作成"""
    stats = {}

    for rank in ['S', 'A', 'B', 'C', 'D', 'E']:
        items_in_rank = [d for d in details if d['rank'] == rank]
        count = len(items_in_rank)

        if count == 0:
            stats[rank] = {
                'count': 0,
                'avg_watch': 0,
                'avg_view': 0,
                'avg_sales': 0,
                'avg_watch_growth': 0,
                'avg_view_growth': 0,
            }
        else:
            stats[rank] = {
                'count': count,
                'avg_watch': round(sum(d['watch_count'] for d in items_in_rank) / count, 1),
                'avg_view': round(sum(d['view_count'] for d in items_in_rank) / count, 1),
                'avg_sales': round(sum(d['sales_count_30d'] for d in items_in_rank) / count, 1),
                'avg_watch_growth': round(
                    sum(d['watch_growth_rate'] for d in items_in_rank) / count, 1
                ),
                'avg_view_growth': round(
                    sum(d['view_growth_rate'] for d in items_in_rank) / count, 1
                ),
            }

    return stats


def check_shipping_cost(
    price: float,
    shipping_cost: float,
    primary_market: Optional[str] = None,
) -> dict:
    """W84 候補 D: 4 区分 primary_market 別 expected の送料整合チェック.

    `reference_shipping_tariff_logic.md` v1.0 § 4.2 マトリクス準拠. 関税額は
    post-tariff 期暫定として `price * 0.20` 近似値 (W89 で strict 化予定).

      - US_only      : expected = $0 (Free Shipping、関税は商品価格包含)
      - global_only  : expected = $0 (Free Shipping、自腹リスク許容)
      - mixed_global : expected = price * 0.20 (DDP 関税近似値が送料欄に上乗せ)
      - unknown      : mixed_global と同じ default
      - None / 旧 listing: 後方互換で従来 (price * 0.20) expected で警告

    Args:
        price: 商品価格 (USD).
        shipping_cost: 実際の eBay 送料 (USD).
        primary_market: 4 区分のいずれか. None で従来挙動 (後方互換).

    Returns: {
        'is_valid': bool, 'expected': float, 'actual': float, 'tolerance': float,
        'error_pct': float, 'message': str, 'status': str,
    }
    """
    market = (primary_market or "").strip().lower()
    if market in ("us_only", "global_only"):
        # 商品価格に関税包含 (us_only) or 自腹許容 (global_only) → 送料は $0 期待
        expected = 0.0
    else:
        # mixed_global / unknown / None (後方互換): 関税近似値が送料欄
        expected = price * SHIPPING_COST_RATIO

    if expected > 0:
        tolerance_min = expected * (1 - SHIPPING_TOLERANCE)
        tolerance_max = expected * (1 + SHIPPING_TOLERANCE)
        is_valid = tolerance_min <= shipping_cost <= tolerance_max
        error_pct = ((shipping_cost - expected) / expected) * 100
    else:
        # expected=$0 (us_only / global_only): 送料 $0 が正常、$0 以外は WARNING
        is_valid = (shipping_cost == 0.0)
        error_pct = 0.0 if is_valid else 100.0

    message = ""
    status = "OK"
    if not is_valid:
        status = "WARNING"
        if expected == 0.0 and shipping_cost > 0:
            message = (
                f"primary_market={market or 'default'} は送料 $0 が期待値ですが "
                f"${shipping_cost:.2f} が設定されています (商品価格に関税包含 or 自腹リスク許容方針)。"
            )
        elif shipping_cost == 0.0:
            message = "eBay送料が$0に設定されています。設定漏れの可能性があります。"
        elif shipping_cost == 30.0:
            message = "eBay送料がデフォルト$30のままです。設定が反映されていない可能性があります。"
        else:
            message = (
                f"送料が期待値（${expected:.2f}）と大きく異なります（誤差 {error_pct:+.1f}%）。"
                f"送料設定を確認してください。"
            )

    return {
        'is_valid': is_valid,
        'expected': round(expected, 2),
        'actual': round(shipping_cost, 2),
        'tolerance': SHIPPING_TOLERANCE * 100,
        'error_pct': round(error_pct, 1),
        'message': message,
        'status': status,
    }
