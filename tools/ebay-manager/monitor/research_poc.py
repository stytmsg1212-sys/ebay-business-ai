#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 商品リサーチ自動化 フェーズB MVP PoC (ビルド順 step1-2).

スコープ:
  1 商品 手入力 (title_ja + 概算重量) → フリマ (メルカリ/ヤフオク/PayPay) で同一
  商品+同状態を探索 → claude_evaluator で同一性 judge → research_candidates
  テーブルに着地。

絶対にしないこと (out-of-scope):
  - eBay 出品 (verify/draft 含む)
  - キーワード新着監視への登録
  - 仕入購入の実行
  - 売れ行きゲート (Terapeak ACTIVE/SOLD 連携) ※フェーズA / W229

レビュー指摘の反映 (.company/engineering/docs/2026-06-07-product-research-automation-spec.md §8):
  - P0-1 「既存 task_supplier_candidate_search の流用は無理筋」: あの関数は
    `get_ebay_listing_by_item_id` で listing 必須 + `_estimate_profit_for_candidate`
    が current_price (eBay 売値) を必須にしているため、未出品 research_candidate を
    流すと 1 行目で None で抜ける。アダプタ層として薄い新関数を本ファイルに置く。
    実態: 流用できたのは「mercari_search / yahoo_search / paypay_search の検索 helper
    のみ」≈ ローレベル fetch だけ (全体ロジックの 1〜2 割)。
  - P0-2 状態機械: insert (new) → 探索 → sourced / not_found / needs_review に
    遷移。silent skip 防止のため reason 必須 (`research_candidates_db.update_status`)。
  - P1-1 利益: weight 欠落 = clip 0g で偽黒字を作らない。`compute_breakeven_price_usd`
    は weight<=0 で None を返す仕様 → ここで needs_review に落とす。

Phase 1 FIX-2 (2026-06-10):
  - keisuke_check 純関数: 還付抜き profit で率 6% または ¥600 の either-or 判定。
  - evaluate_product の利益算出を calculator.calculate 真値に差し替え。
  - weight 欠落時は profit_*_true = NULL のまま + needs_review (0 clip 禁止)。

Phase 1 FIX-3 (2026-06-10):
  - retry_sourcing: needs_review (技術失敗) 候補を sourcing に戻して再探索。

Phase 1 FIX-4 (2026-06-10):
  - evaluate_product が claude_evaluator の found_condition_ja を DB に保存。
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import research_candidates_db as rc_db

logger = logging.getLogger(__name__)

# 同一性判定の保存閾値。`task_supplier_candidate_search` の 60 と同水準だが、
# PoC では「最終一致判定は人間」(§2-B) なので閾値で reject せず、全件保存して
# 人間に提示する。本定数は将来の UI フィルタ用に置くだけ。
MATCH_SCORE_SUGGESTED_FLOOR = 60

# フリマ探索プラットフォーム (Amazon/楽天は対象外 / 設計書 §2-B)。
DEFAULT_PLATFORMS: tuple[str, ...] = ("mercari", "yahoo_auctions", "paypay_furima")


# ============================================================================
# HIGH-1: 仕入先タイトルからコンディション推定 (辞書マッチ、悪い状態優先)
# ============================================================================

# (key=キーワードリスト, value=表示文字列) — 先頭ほど悪い状態 (優先度高)
_CONDITION_KEYWORD_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("動作未確認", "ジャンク", "部品取り", "故障"), "ジャンク/動作未確認"),
    (("通電確認のみ", "通電のみ"), "通電のみ"),
    (("傷あり", "難あり", "訳あり"), "難あり"),
    (("使用感あり",), "使用感あり"),
    (("美品",), "美品"),
    (("良品", "並品"), "良品/並品"),
    # 「新品同様」は「新品」より先に評価する (「新品」が部分一致で勝たないように)
    (("新品同様", "未使用", "開封品"), "新品同様/未使用"),
    (("新品", "未開封", "シュリンク"), "新品"),
)


def _infer_condition_ja(title: str) -> Optional[str]:
    """仕入先タイトルから日本語コンディション文字列を推定する (純関数).

    ルール:
    - 悪い状態優先 (ジャンク系キーワードがあれば良い表記より優先して返す)
    - 複数カテゴリに一致する場合はリスト先頭 (= より悪い状態) を返す
    - 一致なし → None (推定を捏造しない)

    Args:
        title: 仕入先の商品タイトル (日本語混じり OK)。

    Returns:
        推定コンディション文字列、または None。
    """
    if not title:
        return None
    for keywords, label in _CONDITION_KEYWORD_MAP:
        if any(kw in title for kw in keywords):
            return label
    return None


# ============================================================================
# FIX-2: けいすけ基準 純関数 (設計書 §14-Q1)
# ============================================================================

def keisuke_check(profit_jpy: float, revenue_jpy: float) -> dict:
    """けいすけ基準 合否判定 (純関数、副作用なし).

    仕様書 §7-4 / 設計書 §14-Q1 準拠:
      合格 = profit_jpy >= 600 OR profit_jpy >= revenue_jpy * 0.06 (either-or)。
      入力は calculator.calculate の profit (= 還付抜き)。

    borderline 判定 (設計書 §14-Q2):
      基準ライン = min(600, revenue_jpy * 0.06)。
      profit がその ±20% 帯内なら borderline=True。
      (呼び出し側で needs_review に落とす材料 — Phase 1 では値の保存まで)

    Args:
        profit_jpy: 利益額 (円、還付抜き)。calculator.calculate の profit フィールド。
        revenue_jpy: 売上 (円)。calculator.calculate の revenue フィールド。

    Returns:
        {
            "pass": bool,         合否
            "profit_rate": float, 利益率 (profit / revenue)
            "pass_600": bool,     ¥600 条件単体の合否
            "pass_rate": bool,    6% 条件単体の合否
            "threshold_jpy": float,  min(600, revenue * 0.06)
            "borderline": bool,   ±20% 帯内
        }
    """
    # revenue が 0 以下 (異常値) のエッジケース: 率計算不能 → 額だけで判定
    if revenue_jpy <= 0:
        pass_600 = profit_jpy >= 600
        pass_rate = False  # 率計算不能
        profit_rate = 0.0
        threshold_jpy = 600.0
    else:
        profit_rate = profit_jpy / revenue_jpy
        pass_600 = profit_jpy >= 600
        pass_rate = profit_rate >= 0.06
        threshold_jpy = min(600.0, revenue_jpy * 0.06)

    keisuke_pass = pass_600 or pass_rate

    # borderline: profit が threshold の ±20% 帯内
    borderline_low = threshold_jpy * 0.80
    borderline_high = threshold_jpy * 1.20
    borderline = borderline_low <= profit_jpy <= borderline_high

    return {
        "pass": keisuke_pass,
        "profit_rate": round(profit_rate, 4),
        "pass_600": pass_600,
        "pass_rate": pass_rate,
        "threshold_jpy": threshold_jpy,
        "borderline": borderline,
    }


@dataclass
class FreemarketHit:
    """フリマ探索 1 件 (mercari/yahoo/paypay の正規化形式)."""
    source_platform: str
    url: str
    title: str
    price_jpy: Optional[int]
    image_url: Optional[str] = None


def _search_freemarket(
    platform: str, keyword: str, max_results: int = 5
) -> list[FreemarketHit]:
    """フリマ 1 platform 検索 (mercari/yahoo/paypay)。

    subprocess 経由で monitor.research_search_cli を呼び出す。

    理由 (2026-06-10 Q1 実機発見):
        Streamlit プロセス内では Windows の SelectorEventLoop が子プロセスを起動できず
        Playwright が NotImplementedError で即死する。直接 search_mercari 等を呼ぶと
        その例外が mercari_search.py の except Exception で握りつぶされて空リストが返り、
        evaluate_product が「0 件 = not_found (在庫なし)」と誤判定する。
        subprocess として起動することで ProactorEventLoop が正常に使われる。

    例外:
        RuntimeError: subprocess が非 0 exit または ok:false を返した場合。
        上位 evaluate_product の Step 3 except が search_errors に拾い
        needs_review に落とす既存機構がそのまま機能する。
    """
    _cwd = str(Path(__file__).resolve().parent.parent)
    _creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    proc = subprocess.run(
        [sys.executable, "-m", "monitor.research_search_cli", platform, str(max_results)],
        input=keyword,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        cwd=_cwd,
        creationflags=_creationflags,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"search subprocess failed (exit={proc.returncode}): {proc.stderr[-500:]}"
        )

    payload = json.loads(proc.stdout)  # JSONDecodeError はそのまま伝播 (上位が search_errors に拾う)

    if not payload.get("ok"):
        raise RuntimeError(f"search subprocess error: {payload.get('error')}")

    return [
        FreemarketHit(
            source_platform=platform,
            url=h["url"],
            title=h.get("title") or "",
            price_jpy=h.get("price_jpy"),
            image_url=h.get("image_url"),
        )
        for h in payload["hits"]
    ]


def estimate_profit_usd_for_research(
    *,
    terapeak_avg_price_usd: Optional[float],
    purchase_yen: int,
    manual_weight_g: Optional[float],
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    settings: Optional[dict] = None,
) -> tuple[Optional[float], Optional[str]]:
    """PoC 専用 利益見込み: (estimated_profit_usd, needs_review_reason).

    P0-1 / P1-1 反映:
      - 既存 `_estimate_profit_for_candidate` (task_supplier_candidate_search) は
        ebay_listings dict + current_price 前提で動かない。本関数は terapeak 平均
        + 手入力 weight だけで計算する薄いアダプタ。
      - 利益計算は **`compute_breakeven_price_usd` の存在前提仕様** (weight<=0 で
        None を返す = 仕入不能/物理不明) を逆用する: 損益分岐 USD を求めて
        「terapeak 平均 - 損益分岐」を粗い利益見込みとする。weight 欠落で None が
        返ったら needs_review_reason 付きで上位に渡す (P1-1: 0 clip 禁止)。
      NOTE: この関数は「旧来の簡易推定」。evaluate_product では FIX-2 として
      calculator.calculate を呼ぶ compute_profit_true_for_research に差し替え済み。
      後方互換のため本関数は残す。

    Returns:
      (profit_usd, None) 計算成功
      (None, reason)     計算不能 → needs_review に落とす根拠付き
    """
    if not terapeak_avg_price_usd or terapeak_avg_price_usd <= 0:
        return None, "terapeak_avg_price_usd 未入力 (Terapeak 平均が無いと利益計算不可)"
    if not purchase_yen or purchase_yen <= 0:
        return None, "purchase_yen が 0 以下 (フリマ価格未取得 = 仕入価格不明)"
    if not manual_weight_g or manual_weight_g <= 0:
        # P1-1: weight=0 clip 常用は禁止 (送料過小→偽黒字→誤仕入)。
        # 設計書「Terapeak には weight 無し → MVP は人手で概算重量入力」。
        return (
            None,
            "manual_weight_g 未入力 (送料計算不能。0 clip = 偽黒字防止のため "
            "needs_review)",
        )

    if settings is None:
        # K1: 本 PoC は単発計算なので settings 未指定なら calculator のデフォルトを
        # 読む (load_settings)。caller は test で dict 直渡しできる。
        from calculator import load_settings as _load_settings
        try:
            settings = _load_settings()
        except Exception as e:
            return None, f"settings load 失敗: {e}"

    from .lowest_price import compute_breakeven_price_usd

    try:
        breakeven_usd = compute_breakeven_price_usd(
            purchase_yen=float(purchase_yen),
            weight_g=float(manual_weight_g),
            length_cm=float(length_cm or 0),
            width_cm=float(width_cm or 0),
            height_cm=float(height_cm or 0),
            settings=settings,
        )
    except Exception as e:
        # RuntimeError (setup error / settings 不正) や型不正等。silent に黒字にしない。
        return None, f"breakeven 計算失敗: {e}"

    if breakeven_usd is None:
        # `compute_breakeven_price_usd` は weight<=0 / 仕入が極端高 (上限でも赤字)
        # / 計算経路の None で None を返す。「赤字判定」と「物理不明」を区別する
        # 情報は同 helper からは得られないので、両方を needs_review に倒す
        # (P2: 後者は技術失敗、前者は業務判断だが PoC では人間 review に統合)。
        return None, "breakeven 計算が None (仕入価格過大 or 物理データ不足)"

    profit_usd = float(terapeak_avg_price_usd) - float(breakeven_usd)
    return round(profit_usd, 2), None


def compute_profit_true_for_research(
    *,
    terapeak_avg_price_usd: Optional[float],
    purchase_yen: int,
    manual_weight_g: Optional[float],
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    settings: Optional[dict] = None,
) -> tuple[Optional[int], Optional[float], Optional[str], Optional[float]]:
    """FIX-2: calculator.calculate を使った真値利益計算.

    設計書 §14-Q1 / FIX-2 準拠:
      - calculator.calculate の profit フィールド (還付抜き) を使う。
      - weight 欠落時は (None, None, reason, None) を返す (0 clip 禁止 / P1-1)。
      - 旧 estimate_profit_usd_for_research (compute_breakeven 逆用) より精度が高い。

    Args:
        terapeak_avg_price_usd: Terapeak 平均売値 (USD)。
        purchase_yen: フリマ仕入価格 (円)。
        manual_weight_g: 重量 (g)。None または 0 以下 = 計算不能 → needs_review。
        length_cm / width_cm / height_cm: 寸法 (cm)。省略可 (0 扱い)。
        settings: calculator の settings dict。None = load_settings() 呼び出し。

    Returns:
        (profit_jpy_true, profit_usd_true, None, revenue_jpy)  — 計算成功
        (None, None, needs_review_reason, None)                 — 計算不能
    """
    if not terapeak_avg_price_usd or terapeak_avg_price_usd <= 0:
        return None, None, "terapeak_avg_price_usd 未入力 (利益計算不可)", None
    if not purchase_yen or purchase_yen <= 0:
        return None, None, "purchase_yen が 0 以下 (仕入価格不明)", None
    if not manual_weight_g or manual_weight_g <= 0:
        # P1-1: weight 欠落 = needs_review。0 clip で偽黒字を作らない。
        return None, None, (
            "manual_weight_g 未入力 (送料計算不能。0 clip = 偽黒字防止のため needs_review)"
        ), None

    if settings is None:
        from calculator import load_settings as _load_settings
        try:
            settings = _load_settings()
        except Exception as e:
            return None, None, f"settings load 失敗: {e}", None

    # calculator.calculate を呼ぶ (CalcInput を構築)
    try:
        from calculator import calculate, CalcInput
        inp = CalcInput(
            purchase_yen=float(purchase_yen),
            item_price_usd=float(terapeak_avg_price_usd),
            weight_g=float(manual_weight_g),
            length_cm=float(length_cm or 0),
            width_cm=float(width_cm or 0),
            height_cm=float(height_cm or 0),
        )
        result = calculate(inp, settings)
    except Exception as e:
        return None, None, f"calculator.calculate 失敗: {e}", None

    if not result.service_results:
        return None, None, "calculator.calculate: service_results が空 (送料データ不足)", None

    # 最良 (最高利益) のサービス結果を採用
    best_svc = max(result.service_results, key=lambda s: s.profit)
    profit_jpy = best_svc.profit  # 還付抜き真値

    fx = settings.get("exchange_rate", 150)
    profit_usd = round(profit_jpy / fx, 2) if fx and fx > 0 else None

    return int(profit_jpy), profit_usd, None, float(result.revenue)


def compute_max_purchase_jpy(
    *,
    terapeak_avg_price_usd: Optional[float],
    manual_weight_g: Optional[float],
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    settings: Optional[dict] = None,
) -> tuple[Optional[int], Optional[str]]:
    """W262: けいすけ基準 PASS が成立する上限仕入価格 (損益分岐仕入価格、円) を逆算.

    用途 (board #1 / W262):
      仕入先未発見の監視候補について「同等品が ¥X 以下で出品されれば利益が取れる」
      目標仕入価格を Terapeak 平均売値 + 推定重量から逆算する。
      この値を keyword watch の price_max_jpy に渡すことで、in_price_range 通知が
      そのまま「利益が出る価格で出品された」通知になる。

    アルゴリズム:
      利益は仕入価格に対して単調減少 (calculator.calculate は仕入を線形コスト計上)
      のため、けいすけ基準の PASS/FAIL 境界は一意。¥1 で PASS を確認した後、
      hi を FAIL になるまで倍々拡張し、二分探索で境界を求める (~25 回の calculate)。

    Returns:
        (max_purchase_jpy, None)  — 逆算成功 (この価格以下の仕入で基準 PASS)
        (None, reason)            — 逆算不能 (terapeak/weight 欠落、¥1 でも不達 等)
    """
    if not terapeak_avg_price_usd or terapeak_avg_price_usd <= 0:
        return None, "terapeak_avg_price_usd 未入力 (逆算不可)"
    if not manual_weight_g or manual_weight_g <= 0:
        # P1-1 と同根: weight 0 clip で偽の上限価格を作らない
        return None, "manual_weight_g 未入力 (送料計算不能のため逆算不可)"

    if settings is None:
        from calculator import load_settings as _load_settings
        try:
            settings = _load_settings()
        except Exception as e:
            return None, f"settings load 失敗: {e}"

    def _probe(purchase_yen: int) -> tuple[Optional[bool], Optional[str]]:
        """指定仕入価格でのけいすけ基準合否。(None, reason) = 計算不能."""
        profit_jpy, _usd, reason, revenue_jpy = compute_profit_true_for_research(
            terapeak_avg_price_usd=terapeak_avg_price_usd,
            purchase_yen=purchase_yen,
            manual_weight_g=manual_weight_g,
            length_cm=length_cm,
            width_cm=width_cm,
            height_cm=height_cm,
            settings=settings,
        )
        if profit_jpy is None:
            return None, reason
        kc = keisuke_check(float(profit_jpy), float(revenue_jpy or 0))
        return bool(kc["pass"]), None

    ok, reason = _probe(1)
    if ok is None:
        return None, reason
    if not ok:
        return None, "¥1 仕入でも けいすけ基準 不達 (Terapeak 売値が低すぎ or 送料負け)"

    _CAP = 10_000_000
    fx = settings.get("exchange_rate", 150) or 150
    lo = 1  # PASS 確認済
    hi = max(2, int(float(terapeak_avg_price_usd) * float(fx)))

    # hi を FAIL になるまで倍々拡張 (売値全額相当の仕入なら通常 FAIL のはず)
    while True:
        ok_hi, reason_hi = _probe(hi)
        if ok_hi is None:
            return None, reason_hi
        if not ok_hi:
            break
        lo = hi
        if hi >= _CAP:
            return None, f"仕入 ¥{_CAP:,} でも基準成立 (入力異常の疑い)"
        hi = min(hi * 2, _CAP)

    # 二分探索: 不変条件 lo=PASS / hi=FAIL
    while hi - lo > 1:
        mid = (lo + hi) // 2
        ok_mid, reason_mid = _probe(mid)
        if ok_mid is None:
            return None, reason_mid
        if ok_mid:
            lo = mid
        else:
            hi = mid

    return lo, None


# ============================================================================
# FIX-3: needs_review (技術失敗) 候補の再探索経路
# ============================================================================

def retry_sourcing(rc_id: int) -> bool:
    """FIX-3: needs_review (技術失敗) 候補を sourcing に戻して再探索を可能にする.

    設計書 §実装スコープ FIX-3 準拠:
      - status を CAS で needs_review → sourcing に遷移させる。
      - 遷移後に evaluate_product を呼ぶかどうかは caller が判断する
        (UI の「再探索」ボタン押下時に呼ぶ)。
      - 技術失敗 (needs_review) のみ対象。業務 0 件 (not_found) は対象外
        (技術失敗と業務判断の分離 / P2)。

    Returns:
        True = sourcing に遷移成功。
        False = rc_id 不在 / 状態が needs_review でない (遷移対象外)。
    """
    if rc_id is None:
        raise ValueError("rc_id is required for retry_sourcing")
    try:
        return rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)
    except ValueError:
        # 不正遷移 (not_found や sourced 等から sourcing) = 対象外
        logger.info(
            f"[research_poc] retry_sourcing: rc_id={rc_id} 状態が needs_review でないため "
            "再探索対象外 (ValueError は業務的に正常)"
        )
        return False


def _best_hit(hits: list[FreemarketHit]) -> Optional[FreemarketHit]:
    """探索結果の代表 1 件を選ぶ. PoC では「価格が分かっている中で最安」.

    K1: 同一性スコアでソートするロジックは本 PoC では人間 review 前提のため不要。
    最安 1 件を保存し、人間が UI で複数比較する形に統合するのは将来。
    """
    priced = [h for h in hits if h.price_jpy is not None and h.price_jpy > 0]
    if not priced:
        return None
    return min(priced, key=lambda h: h.price_jpy)


def evaluate_product(
    title_ja: str,
    *,
    rc_id: Optional[int] = None,
    manual_weight_g: Optional[float] = None,
    terapeak_avg_price_usd: Optional[float] = None,
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    platforms: Optional[tuple[str, ...]] = None,
    max_results_per_platform: int = 5,
    settings: Optional[dict] = None,
) -> dict:
    """PoC エントリポイント. 1 商品の手入力からフリマ探索 + 同一性提示までを実行.

    FIX-A: rc_id を指定した場合は INSERT をスキップして既存行を再利用する。
      - gate 経由の行 (status=gate_passed) をそのまま sourcing → sourced に遷移させる。
      - rc_id が存在しない場合は ValueError (silent 新規作成しない / Q0)。
      - rc_id=None (既定) の場合は従来どおり新規 INSERT。

    フロー:
      1. rc_id=None → research_candidates に new で 1 行 INSERT (status=new)
         rc_id 指定 → INSERT スキップ、指定行を使用 (存在しない場合は ValueError)
      2. status を sourcing に遷移
         (gate_passed → sourcing は _ALLOWED_TRANSITIONS に登録済み)
      3. 各 platform でフリマ探索. 例外は 1 つでも捕まえ needs_review に落とす
         (P2: 技術失敗と業務判断 0 件を区別)。
      4. 全 platform 合算で 0 件 → not_found
      5. 最有力 1 件で claude_evaluator.evaluate_match を呼び match_score 取得
      6. terapeak 平均 + 手入力 weight + フリマ実価格で利益見込み計算
         (利益計算不能 = needs_review_reason 付きで落とす)
      7. 全部成功 → sourced

    Returns:
      {
        "rc_id": <int>,
        "status": <str>,
        "match_score": <int|None>,
        "match_reason": <str|None>,
        "estimated_profit_usd": <float|None>,
        "needs_review_reason": <str|None>,
        "found_url": <str|None>,
        "found_price_jpy": <int|None>,
        "source_platform": <str|None>,
        "search_errors": <list[str]>,  # 取得 error が出た platform 名
        "hits_count_total": <int>,
      }
    """
    if not title_ja or not title_ja.strip():
        raise ValueError("title_ja is required")
    platforms = platforms or DEFAULT_PLATFORMS

    # Step 1: INSERT or 既存行再利用 (FIX-A)
    if rc_id is not None:
        # FIX-A: gate 経由の rc_id を引き継ぐ。存在しない場合は ValueError (Q0)。
        existing = rc_db.get_research_candidate(rc_id)
        if existing is None:
            raise ValueError(
                f"evaluate_product: rc_id={rc_id} が research_candidates に存在しない "
                "(gate 経由の rc_id のみ渡すこと / Q0 silent 新規作成禁止)"
            )
        # HIGH-1 (4巡目 defense-in-depth): rc_id の DB 行 title_ja と入力 title_ja が
        # 一致しない場合は別商品行への書込を防ぐために ValueError を raise する (Q0)。
        if (existing.get("title_ja") or "").strip() != title_ja.strip():
            raise ValueError(
                f"evaluate_product: rc_id={rc_id} の title_ja={existing.get('title_ja')!r} と "
                f"入力 title={title_ja.strip()!r} が不一致 (別商品行への書込防止 / Q0)"
            )
        logger.info(
            "[research_poc] FIX-A: rc_id=%s を再利用 (INSERT スキップ) status=%s",
            rc_id, existing.get("status"),
        )
        # FIX-A 追補 (2026-06-10 Q1 rc_id=10 で発覚): 再利用パスは INSERT を
        # 通らないため、今回の入力スナップショット (terapeak / 重量 / 寸法) を
        # 既存行に書き戻す。承認キュー「Terapeak と利益額を見て承認」の前提。
        rc_db.update_input_snapshot(
            rc_id,
            manual_weight_g=manual_weight_g,
            length_cm=length_cm,
            width_cm=width_cm,
            height_cm=height_cm,
            terapeak_avg_price_usd=terapeak_avg_price_usd,
        )
    else:
        rc_id = rc_db.insert_research_candidate(
            title_ja=title_ja.strip(),
            manual_weight_g=manual_weight_g,
            length_cm=length_cm,
            width_cm=width_cm,
            height_cm=height_cm,
            terapeak_avg_price_usd=terapeak_avg_price_usd,
        )

    # Step 2: sourcing 遷移 (new → sourcing, gate_passed → sourcing)
    rc_db.update_status(rc_id, rc_db.STATUS_SOURCING)

    # Step 3: フリマ探索 (P2: error と 0 件を区別)
    all_hits: list[FreemarketHit] = []
    search_errors: list[str] = []
    for plat in platforms:
        try:
            hits = _search_freemarket(
                plat, title_ja.strip(), max_results=max_results_per_platform
            )
            all_hits.extend(hits)
        except Exception as e:
            # 取得エラー = 後で needs_review に落とす根拠。silent skip しない。
            search_errors.append(f"{plat}: {type(e).__name__}: {e}")
            logger.warning(
                f"[research_poc] search error rc_id={rc_id} platform={plat}: {e}"
            )

    if search_errors:
        # 1 platform でも取得失敗 = 全結果が信頼できない可能性 (中断 / 検閲 /
        # anti-bot 等)。Q0: needs_review で人間に再試行させる。
        reason = "フリマ探索で取得エラー: " + " / ".join(search_errors)
        rc_db.update_research_candidate_result(
            rc_id,
            new_status=rc_db.STATUS_NEEDS_REVIEW,
            needs_review_reason=reason,
        )
        return {
            "rc_id": rc_id,
            "status": rc_db.STATUS_NEEDS_REVIEW,
            "match_score": None,
            "match_reason": None,
            "estimated_profit_usd": None,
            "needs_review_reason": reason,
            "found_url": None,
            "found_price_jpy": None,
            "source_platform": None,
            "search_errors": search_errors,
            "hits_count_total": len(all_hits),
        }

    # Step 4: ヒット 0 件 = 在庫なし (= 仕入先実在せず) → not_found (業務判断)。
    # Codex 2段指摘#1: ヒットはあるが全件価格が取れない (価格欄パース失敗) のを
    # not_found に畳むと「実在せず」と「取得不完全」を混同し候補を silent に失う。
    # ヒットがあるのに代表が選べない場合は needs_review (取得不完全) に落とす。
    best = _best_hit(all_hits)
    if best is None:
        if all_hits:
            reason = (
                f"フリマ {len(all_hits)} 件ヒットしたが全件 価格が取得できず "
                "(価格欄パース失敗 = 取得不完全)。再探索 / 手動確認が必要。"
            )
            rc_db.update_research_candidate_result(
                rc_id,
                new_status=rc_db.STATUS_NEEDS_REVIEW,
                needs_review_reason=reason,
            )
            return {
                "rc_id": rc_id,
                "status": rc_db.STATUS_NEEDS_REVIEW,
                "match_score": None,
                "match_reason": None,
                "estimated_profit_usd": None,
                "needs_review_reason": reason,
                "found_url": None,
                "found_price_jpy": None,
                "source_platform": None,
                "search_errors": [],
                "hits_count_total": len(all_hits),
            }
        rc_db.update_status(rc_id, rc_db.STATUS_NOT_FOUND)
        return {
            "rc_id": rc_id,
            "status": rc_db.STATUS_NOT_FOUND,
            "match_score": None,
            "match_reason": None,
            "estimated_profit_usd": None,
            "needs_review_reason": None,
            "found_url": None,
            "found_price_jpy": None,
            "source_platform": None,
            "search_errors": [],
            "hits_count_total": len(all_hits),
        }

    # Step 5: 同一性判定 (claude_evaluator)。保存のみ、最終確定は人間 (§2-B)。
    # API エラーは EvaluationResult.error にメッセージが入る (match_score=0)。
    from .claude_evaluator import evaluate_match

    eval_result = evaluate_match(
        ebay_title=title_ja.strip(),
        candidate_title=best.title or "",
        platform=best.source_platform,
        price_jpy=best.price_jpy,
        url=best.url,
        # ebay_image_url は未出品 research_candidate なので None (= 比較画像なし)。
        ebay_image_url=None,
        candidate_image_url=best.image_url,
        # sku / ebay_item_id は持たないので渡さない (Few-shot 学習は出品済 entity のみ)。
        sku=None,
        ebay_item_id=None,
    )
    eval_error_reason: Optional[str] = (
        eval_result.error if getattr(eval_result, "error", None) else None
    )

    # HIGH-1 (FIX-4): claude_evaluator.EvaluationResult に condition 属性は存在しないため
    # getattr では常に None になる。代わりに仕入先タイトルのキーワード辞書で推定する。
    found_condition_ja: Optional[str] = _infer_condition_ja(best.title or "")

    # Step 6: 利益計算 (FIX-2: calculator.calculate 真値に差し替え / P1-1)
    purchase_yen = int(best.price_jpy) if best.price_jpy else 0
    profit_jpy_true, profit_usd_true, profit_reason, revenue_jpy_true = compute_profit_true_for_research(
        terapeak_avg_price_usd=terapeak_avg_price_usd,
        purchase_yen=purchase_yen,
        manual_weight_g=manual_weight_g,
        length_cm=length_cm,
        width_cm=width_cm,
        height_cm=height_cm,
        settings=settings,
    )

    # 後方互換: estimated_profit_usd は profit_usd_true で埋める
    profit_usd = profit_usd_true

    # けいすけ基準判定 (FIX-2 / HIGH-A: revenue は compute_profit_true_for_research の
    # 第4要素で返す。settings=None 経路でも必ず calculator.revenue を使う。
    # 二重 calculate / fallback 分岐を廃止し監査ラベル虚偽を構造的に排除する。)
    keisuke_result: Optional[dict] = None
    if profit_jpy_true is not None and revenue_jpy_true is not None and revenue_jpy_true > 0:
        keisuke_result = keisuke_check(float(profit_jpy_true), float(revenue_jpy_true))
        keisuke_result["revenue_jpy"] = round(float(revenue_jpy_true), 2)
        keisuke_result["revenue_basis"] = "calculator_revenue"

    # Step 7: 着地 status 決定
    # needs_review 条件: AI エラー or 利益計算不能。両者ある場合は両方併記。
    needs_review_reason_parts: list[str] = []
    if eval_error_reason:
        needs_review_reason_parts.append(
            f"claude_evaluator エラー: {eval_error_reason}"
        )
    if profit_reason:
        needs_review_reason_parts.append(profit_reason)

    if needs_review_reason_parts:
        final_status = rc_db.STATUS_NEEDS_REVIEW
        needs_review_reason = " / ".join(needs_review_reason_parts)
    else:
        final_status = rc_db.STATUS_SOURCED
        needs_review_reason = None

    # FIX-B: save_profit_true を先に実行し、status 確定 (update_research_candidate_result)
    # を commit point にする。先に status を sourced にすると、後続の profit 書込失敗時に
    # 「sourced なのに profit_jpy_true=NULL」の行が残る。順序を逆転することで、
    # status 書込が失敗した場合でも行は sourcing のまま利益値だけ持つ = 再実行可能。
    if profit_jpy_true is not None or keisuke_result is not None:
        keisuke_detail = json.dumps(keisuke_result, ensure_ascii=False) if keisuke_result else "{}"
        # FIX-C: save_profit_true の戻り値 False は warning ログで痕跡を残す (Q0)
        saved_profit = rc_db.save_profit_true(
            rc_id=rc_id,
            profit_jpy_true=profit_jpy_true,
            profit_usd_true=profit_usd_true,
            keisuke_pass=bool(keisuke_result.get("pass")) if keisuke_result else False,
            keisuke_detail_json=keisuke_detail,
        )
        if not saved_profit:
            logger.warning(
                "[research_poc] save_profit_true failed: rc_id=%s not found", rc_id
            )

    # FIX-B: status 確定は profit 書込の後 (commit point)
    rc_db.update_research_candidate_result(
        rc_id,
        found_url=best.url,
        found_price_jpy=best.price_jpy,
        found_condition_ja=found_condition_ja,  # FIX-4
        match_score=eval_result.match_score,
        match_reason=eval_result.reasoning,
        estimated_profit_usd=profit_usd,
        new_status=final_status,
        needs_review_reason=needs_review_reason,
    )

    return {
        "rc_id": rc_id,
        "status": final_status,
        "match_score": eval_result.match_score,
        "match_reason": eval_result.reasoning,
        "estimated_profit_usd": profit_usd,
        "profit_jpy_true": profit_jpy_true,       # FIX-2: 真値を返す
        "profit_usd_true": profit_usd_true,        # FIX-2: 真値を返す
        "keisuke_pass": keisuke_result.get("pass") if keisuke_result else None,  # FIX-2
        "keisuke_detail": keisuke_result,          # HIGH-A: 監査用 dict をそのまま返す
        "needs_review_reason": needs_review_reason,
        "found_url": best.url,
        "found_price_jpy": best.price_jpy,
        "found_condition_ja": found_condition_ja,  # FIX-4
        "source_platform": best.source_platform,
        "search_errors": [],
        "hits_count_total": len(all_hits),
    }
