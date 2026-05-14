"""
運送料PDF解析モジュール
eBay SpeedPAK (FedEx / DHL) の料金ガイドPDFから、
重量×ゾーン別の基本運送料テーブルを抽出する。

対応PDF:
  - "RATE GUIDE of eBay SpeedPAK Japan Ship via FedEx-JP"
  - "RATE GUIDE of eBay SpeedPAK Japan Ship via DHL-JP"

抽出対象サービス:
  - FedEx: International Connect Plus (FICP), International Priority (IP)
  - DHL: (ページ構造を検査後、追加)

MVP方針:
  - まずは FICP を確実に抽出
  - ゾーン文字「F」= USW (US West) を ShippingRates.csv の ZoneName="F" にマッピング
  - ユーザー確認UIで差分表示→承認で反映
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import pypdf

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SHIPPING_RATES_CSV = DATA_DIR / "ShippingRates.csv"


# ゾーン文字 → 主要国地域名のデフォルトマッピング（FedEx SpeedPAK基準）
# PDFヘッダー行「量 kg A D E F G H I J K M U」と
# 「主な国･地域名 MO SPAC IS USW USE/CA LAC EURO1 EU2/AE ME/AF SCN DE/GB AU/NZ」に対応
DEFAULT_ZONE_LETTER_MAP = {
    "A": "MO (Macao)",
    "D": "SPAC (South Pacific)",
    "E": "IS (Island)",
    "F": "USW (US West)",
    "G": "USE/CA (US East/Canada)",
    "H": "LAC (Latin America)",
    "I": "EURO1",
    "J": "EU2/AE",
    "K": "ME/AF",
    "M": "SCN (Scandinavia)",
    "U": "DE/GB (Germany/UK)",
}


@dataclass
class RateRow:
    """重量行: 1重量分の各ゾーン料金"""
    weight_kg: float
    rates_by_zone: dict[str, int]  # {"A": 2979, "D": 6428, ...}


@dataclass
class ServiceTable:
    """1サービス分のレートテーブル（複数ページをまとめた結果）"""
    service_name: str           # 例: "International Connect Plus (FICP)"
    zone_letters: list[str]     # 例: ["A", "D", "E", "F", "G", "H", "I", "J", "K", "M", "U"]
    region_labels: dict[str, str]  # 例: {"A": "MO", "F": "USW", ...}
    rows: list[RateRow]         # 重量昇順にソート済み
    source_pages: list[int] = field(default_factory=list)


@dataclass
class ExtractResult:
    """抽出全体結果"""
    tables: list[ServiceTable] = field(default_factory=list)
    error: Optional[str] = None
    carrier: Optional[str] = None   # "FedEx" or "DHL"
    effective_date: Optional[str] = None  # PDFから抽出した発効日
    raw_text_preview: str = ""


# ─── PDFテキスト抽出 ───

def _extract_all_pages(file_like) -> tuple[list[str], Optional[str]]:
    """全ページのテキストを配列で返す"""
    try:
        reader = pypdf.PdfReader(file_like)
        pages = [p.extract_text() or "" for p in reader.pages]
        return pages, None
    except Exception as e:
        return [], f"PDF読み込みエラー: {e}"


def _detect_carrier(full_text: str) -> Optional[str]:
    """PDF全体テキストからキャリア名を判定。タイトル頻度で優勢な方を採用"""
    fedex_count = full_text.count("FedEx") + full_text.count("フェデックス")
    dhl_count = full_text.count("DHL")
    if dhl_count > fedex_count:
        return "DHL"
    if fedex_count > 0:
        return "FedEx"
    if dhl_count > 0:
        return "DHL"
    return None


def _detect_effective_date(full_text: str) -> Optional[str]:
    """発効日を抽出（例: 発効日: 2026 年 4月 5 日）"""
    m = re.search(r'発効日[:：]?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', full_text)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def _detect_service_name(page_text: str) -> Optional[str]:
    """ページから「輸送料金— XXX」形式のサービス名を抽出。
    DHL PDFのように「輸送料金」のみの場合もある（その場合は None を返す）"""
    m = re.search(r'輸送料金[—–\-]+\s*([^\n]+)', page_text)
    if m:
        return m.group(1).strip()
    # サフィックスなしの「輸送料金」タイトル
    if re.search(r'^\s*輸送料金\s*$', page_text, flags=re.MULTILINE):
        return "輸送料金（主サービス）"
    return None


# ─── レートテーブル解析 ───

# ヘッダー行:
#   FedEx:  "量 kg A D E F G H I J K M U"
#   DHL:    "量 kg Zone 1 Zone 2 Zone 3 ... Zone 11"
_HEADER_PAT_FEDEX = re.compile(r'量\s*kg\s+([A-Z](?:\s+[A-Z/]+)*)')
_HEADER_PAT_DHL = re.compile(r'量\s*kg\s+((?:Zone\s*\d+\s*)+)', re.IGNORECASE)

# データ行: "12.5 10,838 26,443 ..." (weight float → 11個くらいの整数)
_RATE_ROW_PAT = re.compile(
    r'^\s*(\d+(?:\.\d+)?)\s+((?:\d{1,3}(?:,\d{3})*\s*){3,})$'
)


def _parse_zone_header(page_text: str) -> Optional[list[str]]:
    """
    ページからゾーンヘッダー行を探し、ゾーン識別子のリストを返す。
    FedEx: ["A", "D", "E", ...], DHL: ["1", "2", "3", ..., "11"]
    """
    for line in page_text.splitlines():
        stripped = line.strip()
        # FedEx形式を優先
        m = _HEADER_PAT_FEDEX.match(stripped)
        if m:
            letters = [t for t in m.group(1).split() if re.fullmatch(r'[A-Z]', t)]
            if len(letters) >= 3:
                return letters
        # DHL形式
        m = _HEADER_PAT_DHL.match(stripped)
        if m:
            zone_str = m.group(1)
            # "Zone 1 Zone 2 ..." から数字部分だけ取り出す
            nums = re.findall(r'Zone\s*(\d+)', zone_str, flags=re.IGNORECASE)
            if len(nums) >= 3:
                return nums
    return None


def _parse_region_labels(page_text: str, zone_letters: list[str]) -> dict[str, str]:
    """
    「主な国･地域名 MO SPAC IS USW ...」のようなラベル行から対応づけ。
    失敗時は DEFAULT_ZONE_LETTER_MAP を返す。
    """
    for line in page_text.splitlines():
        if "主な国" in line or "主な地域" in line:
            # "主な国･地域名" の後に区切られた単語
            parts = re.split(r'\s+', line.strip())
            # 先頭「主な国･地域名」を除去して残りをラベルとして扱う
            tokens = [t for t in parts if t and not re.match(r'^主な', t) and t not in ("国", "地域", "国･地域名", "国・地域名")]
            if len(tokens) >= len(zone_letters):
                return {letter: tokens[i] for i, letter in enumerate(zone_letters)}
    return {letter: DEFAULT_ZONE_LETTER_MAP.get(letter, "?") for letter in zone_letters}


def _parse_rate_rows(page_text: str, expected_zones: int) -> list[RateRow]:
    """
    ページテキストから「0.5 2,979 6,428 ...」形式の重量行を抽出。
    expected_zones 個の数値が取れる行のみ採用。
    """
    rows: list[RateRow] = []
    for line in page_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # まず先頭が数値かチェック（高速パス）
        first_tok = line.split(maxsplit=1)[0] if line.split() else ""
        try:
            _ = float(first_tok)
        except ValueError:
            continue

        # 全トークンを抽出: 重量 + 価格x11（のはず）
        # 価格はカンマ含みなので空白区切り単位で解析
        tokens = re.split(r'\s+', line)
        if len(tokens) < 1 + expected_zones:
            continue

        try:
            weight = float(tokens[0])
        except ValueError:
            continue

        # 重量として現実的な範囲のみ採用（0.5〜200kg）
        if not (0.1 <= weight <= 200):
            continue

        rate_tokens = tokens[1:1 + expected_zones]
        try:
            rates = [int(t.replace(',', '')) for t in rate_tokens]
        except ValueError:
            continue

        # 各レートが合理的範囲か軽くチェック（100円〜999999円）
        if any(r < 100 or r > 999999 for r in rates):
            continue

        rows.append(RateRow(weight_kg=weight, rates_by_zone={}))
        # 後でzone文字に紐付ける
        rows[-1]._raw_rates = rates  # type: ignore[attr-defined]

    return rows


def _attach_zones_to_rows(rows: list[RateRow], zone_letters: list[str]) -> None:
    """_raw_rates をzone_lettersで辞書化"""
    for r in rows:
        raw = getattr(r, '_raw_rates', None)
        if raw is None:
            continue
        r.rates_by_zone = {letter: raw[i] for i, letter in enumerate(zone_letters) if i < len(raw)}


def parse_pdf(file_like) -> ExtractResult:
    """
    PDF全体を解析してサービス別レートテーブル群を返す
    """
    result = ExtractResult()

    pages, err = _extract_all_pages(file_like)
    if err:
        result.error = err
        return result
    if not pages:
        result.error = "PDFが空でした"
        return result

    full_text = "\n".join(pages)
    result.carrier = _detect_carrier(full_text)
    result.effective_date = _detect_effective_date(full_text)
    result.raw_text_preview = full_text[:1500]

    # サービスごとにページをグルーピング
    service_pages: dict[str, list[int]] = {}
    current_service: Optional[str] = None

    for idx, page_text in enumerate(pages):
        svc = _detect_service_name(page_text)
        if svc:
            current_service = svc
        if current_service and _parse_zone_header(page_text):
            service_pages.setdefault(current_service, []).append(idx)

    # 各サービスごとにレート行をマージ
    for svc_name, page_indices in service_pages.items():
        all_rows: dict[float, RateRow] = {}
        zone_letters: Optional[list[str]] = None
        region_labels: Optional[dict[str, str]] = None

        for pi in page_indices:
            txt = pages[pi]
            letters = _parse_zone_header(txt)
            if letters is None:
                continue
            if zone_letters is None:
                zone_letters = letters
                region_labels = _parse_region_labels(txt, letters)

            rows = _parse_rate_rows(txt, expected_zones=len(letters))
            _attach_zones_to_rows(rows, letters)
            for r in rows:
                # 同じ重量が複数ページにあれば最初のを優先（通常は重複しない）
                if r.weight_kg not in all_rows:
                    all_rows[r.weight_kg] = r

        if zone_letters is None or not all_rows:
            continue

        sorted_rows = sorted(all_rows.values(), key=lambda x: x.weight_kg)
        table = ServiceTable(
            service_name=svc_name,
            zone_letters=zone_letters,
            region_labels=region_labels or {},
            rows=sorted_rows,
            source_pages=[p + 1 for p in page_indices],  # 1-indexed for display
        )
        result.tables.append(table)

    if not result.tables:
        result.error = "運送料テーブルが検出できませんでした（PDF形式が変わった可能性）"

    return result


# ─── CSVとの差分計算 ───

# PDFサービス名 → CSV ServiceID のデフォルトマッピング
# PDFに出るサービス名はキャリア側の名称、CSVは当社で使うServiceID
DEFAULT_SERVICE_MAPPING = {
    # FedEx SpeedPAK (CPaSS-FedEx に紐付け)
    "International Connect Plus (FICP)": 11,   # ServiceID 11 = CPaSS - FedEx - FICP
    "International Connect Plus(FICP)": 11,
    "International Priority (IP)": 14,         # ServiceID 14 = CPaSS - FedEx - IP Package
    "International Priority(IP)": 14,
    # DHL SpeedPAK
    "輸送料金（主サービス）": 15,               # ServiceID 15 = CPaSS - DHL
}

# PDFキャリア別、CSVのZoneName対応
# FedEx PDF は zone_letter="F"（USW）→ CSV ZoneName="F"
# DHL PDF は zone_letter="10" → CSV ZoneName="10"
DEFAULT_CARRIER_ZONE_MAPPING = {
    "FedEx": {"pdf_zone": "F", "csv_zone": "F"},
    "DHL": {"pdf_zone": "10", "csv_zone": "10"},
}


@dataclass
class RateDiff:
    """1レート分の差分"""
    service_id: int
    zone_name: str
    weight_grams: int
    old_rate: Optional[int]
    new_rate: int

    @property
    def delta_pct(self) -> Optional[float]:
        if self.old_rate is None or self.old_rate == 0:
            return None
        return (self.new_rate - self.old_rate) / self.old_rate * 100


@dataclass
class DiffReport:
    """1サービス分の差分レポート"""
    service_id: int
    service_name: str
    zone_name: str
    diffs: list[RateDiff] = field(default_factory=list)
    added: int = 0      # 新規追加行数
    updated: int = 0    # 更新行数
    unchanged: int = 0  # 変更なし


def compute_diff(
    table: ServiceTable,
    csv_path: Path = SHIPPING_RATES_CSV,
    service_id: Optional[int] = None,
    zone_letter: str = "F",
    zone_name_in_csv: str = "F",
) -> Optional[DiffReport]:
    """
    PDF抽出テーブル と CSV現行値の差分を計算する。

    Args:
        table: PDF抽出済みServiceTable
        csv_path: ShippingRates.csvパス
        service_id: 対象ServiceID（Noneなら DEFAULT_SERVICE_MAPPING から推論）
        zone_letter: PDFの何列目を使うか（"F" = USW）
        zone_name_in_csv: CSVのZoneName列の値（通常 "F"）
    """
    import pandas as pd

    if service_id is None:
        service_id = DEFAULT_SERVICE_MAPPING.get(table.service_name)
        if service_id is None:
            return None

    df = pd.read_csv(csv_path)
    existing = df[(df['ServiceID'] == service_id) & (df['ZoneName'] == zone_name_in_csv)].copy()
    existing_map = {int(r['WeightGrams']): int(r['BaseRate']) for _, r in existing.iterrows()}

    report = DiffReport(
        service_id=service_id,
        service_name=table.service_name,
        zone_name=zone_name_in_csv,
    )

    for row in table.rows:
        new_rate = row.rates_by_zone.get(zone_letter)
        if new_rate is None:
            continue
        weight_g = int(row.weight_kg * 1000)
        old_rate = existing_map.get(weight_g)

        diff = RateDiff(
            service_id=service_id,
            zone_name=zone_name_in_csv,
            weight_grams=weight_g,
            old_rate=old_rate,
            new_rate=int(new_rate),
        )
        report.diffs.append(diff)

        if old_rate is None:
            report.added += 1
        elif old_rate != new_rate:
            report.updated += 1
        else:
            report.unchanged += 1

    return report


# ─── CSVへ反映 ───

def apply_diff_to_csv(
    report: DiffReport,
    csv_path: Path = SHIPPING_RATES_CSV,
) -> tuple[int, int]:
    """
    差分レポートをCSVに反映
    Returns: (更新件数, 追加件数)
    """
    import pandas as pd

    df = pd.read_csv(csv_path)

    updated = 0
    added = 0

    for diff in report.diffs:
        if diff.old_rate == diff.new_rate:
            continue
        mask = (
            (df['ServiceID'] == diff.service_id)
            & (df['ZoneName'] == diff.zone_name)
            & (df['WeightGrams'] == diff.weight_grams)
        )
        if mask.any():
            df.loc[mask, 'BaseRate'] = diff.new_rate
            updated += 1
        else:
            next_id = int(df['RateID'].max()) + 1 if 'RateID' in df.columns else len(df) + 1
            new_row = {
                'RateID': next_id,
                'ServiceID': diff.service_id,
                'ZoneName': diff.zone_name,
                'WeightGrams': diff.weight_grams,
                'BaseRate': diff.new_rate,
            }
            # Unnamed列がある場合空値で埋める
            for col in df.columns:
                if col not in new_row:
                    new_row[col] = ''
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            added += 1

    df.to_csv(csv_path, index=False)
    return updated, added


# ─── メタ情報 ───

SHIPPING_RATE_WARNING_DAYS = 30


def get_shipping_rate_last_updated(settings: dict) -> Optional[str]:
    return settings.get("shipping_rate_last_updated")


def get_shipping_rate_days_since_update(settings: dict) -> Optional[int]:
    last_str = settings.get("shipping_rate_last_updated")
    if not last_str:
        return None
    try:
        last_dt = datetime.fromisoformat(last_str)
        return (datetime.now() - last_dt).days
    except (ValueError, TypeError):
        return None


def mark_shipping_rate_updated(settings: dict) -> dict:
    updated = dict(settings)
    updated["shipping_rate_last_updated"] = datetime.now().isoformat(timespec='seconds')
    return updated
