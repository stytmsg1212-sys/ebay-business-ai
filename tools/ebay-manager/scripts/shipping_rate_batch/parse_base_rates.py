"""SpeedPAK PDF から DHL/FedEx 基本料金を抽出 + アンカー検証 + キャッシュ (§3.1)。

phase3_calc.py の PDF パースを昇格。基本料金は年 1-2 回しか変わらないため、
成功したパース結果を base_rates_cache.json に保存し、PDF 欠落/アンカー失敗時は
キャッシュへ fail-closed する (ただし cache 使用は dry-run 限定、Codex MEDIUM)。

戻り base_rates 形式:
  {"dhl": {weight_str: {zone_str: jpy}}, "fedex_us": {weight_str: jpy}}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger(__name__)


# ---- PDF 行抽出ヘルパ (phase3_calc.py 由来) ----
def _cluster_rows(words, tol=5):
    ws = sorted(words, key=lambda w: w["top"])
    rows, cur, cur_top = [], [], None
    for w in ws:
        if cur_top is None or (w["top"] - cur_top) <= tol:
            cur.append(w)
            cur_top = cur[0]["top"]
        else:
            rows.append(cur)
            cur, cur_top = [w], w["top"]
    if cur:
        rows.append(cur)
    out = []
    for grp in rows:
        toks = sorted(grp, key=lambda w: w["x0"])
        out.append((min(t["top"] for t in toks), [(t["x0"], t["text"]) for t in toks]))
    return out


def _num(s: str) -> Optional[int]:
    s = s.replace(",", "")
    return int(s) if s.lstrip("-").isdigit() else None


def _parse_rate_page(pdf, page_idx, divider_top=None, want_below=True):
    p = pdf.pages[page_idx]
    rows = _cluster_rows(p.extract_words())
    out = {}
    for top, toks in rows:
        if divider_top is not None:
            if want_below and top <= divider_top:
                continue
            if (not want_below) and top > divider_top:
                continue
        texts = [t for _, t in toks]
        try:
            cand = float(texts[0])
        except (ValueError, IndexError):
            continue
        nums = [n for n in (_num(t) for t in texts[1:]) if n is not None]
        if len(nums) >= 11:
            out.setdefault(cand, nums[:11])
    return out


def _find_divider(pdf, page_idx, label="荷物"):
    for w in pdf.pages[page_idx].extract_words():
        if label in w["text"]:
            return w["top"]
    return None


def _parse_pdfs() -> dict:
    """両 PDF をパースして base_rates を構築 (PDF 必須)。"""
    import pdfplumber

    band_weights = sorted({w for _, w in config.BANDS})

    with pdfplumber.open(str(config.DHL_PDF)) as pdf:
        div = _find_divider(pdf, 11, "荷物")
        dhl_below_p12 = _parse_rate_page(pdf, 11, divider_top=div, want_below=True)
        dhl_p13 = _parse_rate_page(pdf, 12)

    def dhl_rate(weight, zone):
        src = dhl_below_p12 if weight in dhl_below_p12 else dhl_p13
        return src[weight][zone - 1]

    with pdfplumber.open(str(config.FEDEX_PDF)) as pdf:
        fx = {}
        for pi in range(12, 18):
            for wt, nums in _parse_rate_page(pdf, pi).items():
                fx.setdefault(wt, nums)

    def fedex_us(weight):
        return fx[weight][config.FEDEX_US_COL_IDX]

    # 完全性チェック
    miss = []
    for w in band_weights:
        if w not in dhl_below_p12 and w not in dhl_p13:
            miss.append(f"DHL {w}")
        if w not in fx:
            miss.append(f"FedEx {w}")
    if miss:
        raise ValueError(f"PDF に必要な帯上限重量が無い: {miss}")

    base = {"dhl": {}, "fedex_us": {}}
    for w in band_weights:
        base["fedex_us"][str(w)] = fedex_us(w)
        base["dhl"][str(w)] = {str(z): dhl_rate(w, z) for z in range(1, 12)}
    return base


def _check_anchors(base: dict) -> list[str]:
    """PDF アンカー検証。失敗説明のリストを返す (空 = OK)。"""
    fails = []
    for name, kind, weight, zone, expected in config.PDF_ANCHORS:
        try:
            if kind == "fedex_us":
                got = base["fedex_us"][str(weight)]
            else:
                got = base["dhl"][str(weight)][str(zone)]
        except KeyError:
            fails.append(f"{name}: 値が無い (parse 欠落)")
            continue
        if got != expected:
            fails.append(f"{name}: got={got} expected={expected}")
    return fails


def _save_cache(base: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_rates": base,
        "parsed_at": datetime.now().isoformat(timespec="seconds"),
        "dhl_pdf_mtime": _mtime(config.DHL_PDF),
        "fedex_pdf_mtime": _mtime(config.FEDEX_PDF),
        "anchors_passed": True,
    }
    config.BASE_RATES_CACHE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _mtime(p: Path) -> Optional[float]:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def load_base_rates() -> dict:
    """基本料金を取得 (fail-closed)。

    Returns:
        {
            "base_rates": {...},
            "source": "pdf" | "cache",
            "fresh": bool,            # PDF 由来 = True / cache fallback = False
            "warnings": [str, ...],
        }
    PDF があってアンカー OK なら fresh=True でキャッシュ更新。
    PDF 欠落 / アンカー失敗 / parse 例外 なら cache へ fallback (fresh=False)。
    cache も無ければ例外 (バッチは起動不可)。
    """
    warnings: list[str] = []
    try:
        if not config.DHL_PDF.exists() or not config.FEDEX_PDF.exists():
            raise FileNotFoundError(
                f"PDF 欠落 (DHL={config.DHL_PDF.exists()}, FedEx={config.FEDEX_PDF.exists()})"
            )
        base = _parse_pdfs()
        anchor_fails = _check_anchors(base)
        if anchor_fails:
            raise ValueError("アンカー検証失敗: " + "; ".join(anchor_fails))
        _save_cache(base)
        return {"base_rates": base, "source": "pdf", "fresh": True, "warnings": []}
    except Exception as e:
        warnings.append(f"PDF パース失敗 → キャッシュ fallback: {type(e).__name__}: {e}")
        logger.warning(warnings[-1])
        if not config.BASE_RATES_CACHE.exists():
            raise RuntimeError(
                "基本料金 PDF パース失敗 + キャッシュ無し = 計算不能。"
                f" PDF を {config.PDF_DIR} に配置せよ。元エラー: {e}"
            ) from e
        cached = json.loads(config.BASE_RATES_CACHE.read_text(encoding="utf-8"))
        warnings.append(f"キャッシュ使用 (parsed_at={cached.get('parsed_at')})")
        return {"base_rates": cached["base_rates"], "source": "cache", "fresh": False, "warnings": warnings}
