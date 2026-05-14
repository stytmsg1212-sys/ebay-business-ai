"""
燃料サーチャージPDF管理モジュール
FedEx/DHLから定期配布される料金表PDFから燃料サーチャージ値を抽出し、
settings.json に反映する。

処理フロー:
1. PDFファイルを受け取りテキスト抽出（pypdf）
2. FedEx / DHL の燃料サーチャージ値を正規表現で検出
3. 検出結果を候補として返す（ユーザー確認後に反映）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import pypdf

SETTINGS_FILE = Path(__file__).parent / "settings.json"

# 更新通知の閾値（最終更新からの日数）
UPDATE_WARNING_DAYS = 30


@dataclass
class ExtractedSurcharge:
    """PDF抽出結果1件分"""
    carrier: str         # "FedEx" or "DHL"
    rate_pct: float      # 燃料サーチャージ率（%）
    context: str         # 該当箇所の前後テキスト（確認用）


@dataclass
class ExtractionResult:
    """PDF抽出結果全体"""
    fedex_candidates: list[ExtractedSurcharge] = field(default_factory=list)
    dhl_candidates: list[ExtractedSurcharge] = field(default_factory=list)
    raw_text_preview: str = ""
    error: Optional[str] = None


def extract_text_from_pdf(file_like) -> tuple[str, Optional[str]]:
    """
    PDFからテキストを抽出
    Args:
        file_like: Streamlit UploadedFile または Path 相当のファイルライクオブジェクト
    Returns:
        (抽出テキスト, エラーメッセージ or None)
    """
    try:
        reader = pypdf.PdfReader(file_like)
        pages_text = []
        for page in reader.pages:
            pages_text.append(page.extract_text() or "")
        return "\n".join(pages_text), None
    except Exception as e:
        return "", f"PDF読み込みエラー: {e}"


def _find_percentages_near_keyword(text: str, keywords: list[str], window: int = 200) -> list[tuple[float, str]]:
    """
    キーワード周辺で %値 を検出
    Args:
        text: 抽出済みテキスト全体
        keywords: 検索キーワード（"FedEx", "DHL" 等）
        window: キーワード前後何文字を走査するか
    Returns:
        [(rate_pct, context), ...]
    """
    results: list[tuple[float, str]] = []
    pct_pattern = re.compile(r'(\d{1,3}(?:[.,]\d{1,3})?)\s*%')
    seen: set[tuple[float, str]] = set()

    lower_text = text.lower()
    for kw in keywords:
        kw_lower = kw.lower()
        start = 0
        while True:
            idx = lower_text.find(kw_lower, start)
            if idx == -1:
                break
            chunk_start = max(0, idx - window)
            chunk_end = min(len(text), idx + len(kw) + window)
            chunk = text[chunk_start:chunk_end]

            for m in pct_pattern.finditer(chunk):
                raw = m.group(1).replace(',', '.')
                try:
                    val = float(raw)
                except ValueError:
                    continue
                # 燃料サーチャージとして妥当な範囲のみ採用（0〜80%）
                if 0 < val < 80:
                    # 前後30文字を contextとして抽出
                    mstart = max(0, m.start() - 30)
                    mend = min(len(chunk), m.end() + 30)
                    ctx = chunk[mstart:mend].replace('\n', ' ').strip()
                    key = (round(val, 2), ctx[:60])
                    if key not in seen:
                        seen.add(key)
                        results.append((val, ctx))
            start = idx + len(kw)
    return results


def parse_fuel_surcharges(text: str) -> ExtractionResult:
    """
    抽出テキストからFedEx/DHLの燃料サーチャージ候補を抽出
    """
    result = ExtractionResult()

    if not text.strip():
        result.error = "PDFからテキストが抽出できませんでした（スキャン画像PDFかもしれません）"
        return result

    # プレビューは先頭800文字
    result.raw_text_preview = text[:800]

    fedex_hits = _find_percentages_near_keyword(text, ["FedEx", "フェデックス"])
    dhl_hits = _find_percentages_near_keyword(text, ["DHL", "Deutsche Post"])

    for val, ctx in fedex_hits:
        result.fedex_candidates.append(ExtractedSurcharge("FedEx", val, ctx))
    for val, ctx in dhl_hits:
        result.dhl_candidates.append(ExtractedSurcharge("DHL", val, ctx))

    return result


def get_days_since_last_update(settings: dict) -> Optional[int]:
    """
    最終更新日から何日経過したかを返す
    Returns:
        日数（未設定ならNone）
    """
    last_str = settings.get("fuel_surcharge_last_updated")
    if not last_str:
        return None
    try:
        last_dt = datetime.fromisoformat(last_str)
        return (datetime.now() - last_dt).days
    except (ValueError, TypeError):
        return None


def is_update_needed(settings: dict, threshold_days: int = UPDATE_WARNING_DAYS) -> bool:
    """
    最終更新から閾値日数以上経過しているか（未設定の場合もTrue）
    """
    days = get_days_since_last_update(settings)
    return days is None or days >= threshold_days


def apply_surcharge_update(
    settings: dict,
    fedex_pct: Optional[float],
    dhl_pct: Optional[float],
) -> dict:
    """
    settings辞書に燃料サーチャージ値と更新日時を反映（辞書返却のみ、保存は呼出側）
    """
    updated = dict(settings)
    if fedex_pct is not None:
        updated["fuel_surcharge_fedex"] = round(fedex_pct, 2)
    if dhl_pct is not None:
        updated["fuel_surcharge_dhl"] = round(dhl_pct, 2)
    updated["fuel_surcharge_last_updated"] = datetime.now().isoformat(timespec='seconds')
    return updated
