"""
eBay利益計算モジュール
コンサルツール (AI Research eBay) と同一の計算ロジックを実装
"""
import math
import json
import pandas as pd
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

DATA_DIR = Path(__file__).parent / "data"
SETTINGS_FILE = Path(__file__).parent / "settings.json"

# ---- データキャッシュ ----
_cache: dict = {}

def _load_data():
    if _cache:
        return
    _cache['shipping_rates'] = pd.read_csv(DATA_DIR / "ShippingRates.csv")
    _cache['shipping_services'] = pd.read_csv(DATA_DIR / "ShippingServices.csv")
    _cache['zone_mapping'] = pd.read_csv(DATA_DIR / "ZoneMapping.csv")
    _cache['additional_fees'] = pd.read_csv(DATA_DIR / "AdditionalFees.csv")
    _cache['ebay_fee_rates'] = pd.read_csv(DATA_DIR / "EbayFeeRates.csv")

def load_settings() -> dict:
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

# ---- 課金重量計算 ----
def get_chargeable_weight_kg(
    weight_g: float,
    length_cm: float,
    width_cm: float,
    height_cm: float,
    volumetric_divisor: int = 5000,
    weight_padding_g: float = 0,
    length_padding_cm: float = 0,
    width_padding_cm: float = 0,
    height_padding_cm: float = 0,
) -> float:
    """実重量と容積重量を比較し、大きい方を0.5kg単位で切り上げ"""
    actual_kg = (weight_g + weight_padding_g) / 1000
    l = length_cm + length_padding_cm
    w = width_cm + width_padding_cm
    h = height_cm + height_padding_cm

    if l > 0 and w > 0 and h > 0:
        vol_kg = (l * w * h) / volumetric_divisor
    else:
        vol_kg = 0.0

    raw_kg = max(actual_kg, vol_kg)
    # 0.5kg単位切り上げ、最低0.5kg
    charged_kg = max(0.5, math.ceil(raw_kg * 2) / 2)
    return charged_kg

# ---- 送料テーブル参照 ----
def get_zone_name(service_id: int, country_code: str) -> Optional[str]:
    _load_data()
    rows = _cache['zone_mapping'][
        (_cache['zone_mapping']['ServiceID'] == service_id) &
        (_cache['zone_mapping']['CountryCode'] == country_code)
    ]
    if rows.empty:
        return None
    return rows.iloc[0]['ZoneName']

def get_base_shipping_rate(service_id: int, zone_name: str, charged_weight_kg: float) -> Optional[float]:
    _load_data()
    charged_g = charged_weight_kg * 1000
    rows = _cache['shipping_rates'][
        (_cache['shipping_rates']['ServiceID'] == service_id) &
        (_cache['shipping_rates']['ZoneName'] == zone_name)
    ].sort_values('WeightGrams')

    if rows.empty:
        return None

    # 課金重量以上の最小エントリを返す
    valid = rows[rows['WeightGrams'] >= charged_g]
    if valid.empty:
        return float(rows.iloc[-1]['BaseRate'])
    return float(valid.iloc[0]['BaseRate'])

def get_service_info(service_id: int) -> Optional[dict]:
    _load_data()
    rows = _cache['shipping_services'][_cache['shipping_services']['ServiceID'] == service_id]
    if rows.empty:
        return None
    r = rows.iloc[0]
    return {
        'service_id': service_id,
        'carrier': r['Carrier'],
        'service_name': r['ServiceName'],
        'volumetric_divisor': int(r['VolumetricDivisor']),
    }

def get_all_services() -> list[dict]:
    _load_data()
    return _cache['shipping_services'].to_dict('records')

def get_additional_fees(service_id: int, country_code: str, charged_kg: float = 0.0) -> tuple[float, dict]:
    """
    追加費用の合計を取得。FIXED と PER_KG の両方を計算する。
    PER_KG は charged_kg (課金重量) × FeeValue で計算。
    """
    _load_data()
    rows = _cache['additional_fees'][
        (_cache['additional_fees']['ServiceID'] == service_id) &
        (_cache['additional_fees']['CountryCode'] == country_code)
    ]
    total = 0.0
    details = {}
    for _, r in rows.iterrows():
        calc_type = r['CalculationType']
        fee_value = float(r['FeeValue'])
        fee_name = r['FeeName']
        if calc_type == 'FIXED':
            total += fee_value
            details[fee_name] = fee_value
        elif calc_type == 'PER_KG':
            per_kg_total = round(fee_value * charged_kg)
            total += per_kg_total
            details[f"{fee_name}({charged_kg}kg)"] = per_kg_total
    return total, details

# ---- eBay手数料率取得 ----
def category_in_fee_table(category_id: int) -> bool:
    """W222 (2026-06-05): category_id が EbayFeeRates.csv に収録されているか。

    False の場合 get_ebay_fvf_rate は既定 12.7%/13.6% にフォールバックする
    (= 実レートと乖離し得る)。UI で「未収録のため既定レート」警告を出すのに使う。
    """
    _load_data()
    try:
        return bool((_cache['ebay_fee_rates']['CategoryID'] == int(category_id)).any())
    except (ValueError, TypeError):
        return False


def get_ebay_fvf_rate(category_id: int, total_sale_usd: float, store_plan: str = "Premium") -> float:
    """カテゴリとストアプランに応じたFVF実効レートを返す"""
    _load_data()
    cat = _cache['ebay_fee_rates'][_cache['ebay_fee_rates']['CategoryID'] == category_id]

    if store_plan == "None":
        prefix = "NoStore"
    else:
        prefix = "Store"

    if cat.empty:
        # デフォルト: 多くのカテゴリの一般的なレート
        rate1 = 0.127 if store_plan != "None" else 0.136
        return rate1

    row = cat.iloc[0]
    rate1 = float(row[f'{prefix}_Rate1']) / 100

    threshold1_raw = row.get(f'{prefix}_Threshold1', None)
    if pd.isna(threshold1_raw) or threshold1_raw == 0:
        return rate1

    threshold1 = float(threshold1_raw)
    rate2_raw = row.get(f'{prefix}_Rate2', None)
    rate2 = float(rate2_raw) / 100 if not pd.isna(rate2_raw) and rate2_raw != 0 else 0.0

    threshold2_raw = row.get(f'{prefix}_Threshold2', None)
    threshold2 = float(threshold2_raw) if not pd.isna(threshold2_raw) and threshold2_raw != 0 else float('inf')
    rate3_raw = row.get(f'{prefix}_Rate3', None)
    rate3 = float(rate3_raw) / 100 if not pd.isna(rate3_raw) and rate3_raw != 0 else 0.0

    # 階段型手数料額を計算
    if total_sale_usd <= threshold1:
        fee_usd = total_sale_usd * rate1
    elif total_sale_usd <= threshold2:
        fee_usd = threshold1 * rate1 + (total_sale_usd - threshold1) * rate2
    else:
        fee_usd = threshold1 * rate1 + (threshold2 - threshold1) * rate2 + (total_sale_usd - threshold2) * rate3

    return fee_usd / total_sale_usd if total_sale_usd > 0 else rate1

# ---- CPaSS追加費用計算 ----
def get_cpass_additional_fees(
    service_name: str,
    exchange_rate: float,
    settings: dict,
    duty_amount_jpy: float = 0.0,
) -> tuple[float, dict]:
    """CPaSS系サービスのUS向け追加費用（関税処理費・MPF）"""
    details = {}
    total = 0.0

    if "CPaSS" not in service_name:
        return total, details

    if "FICP" in service_name or "Economy" in service_name:
        # FICP / Economy: US processing fee (関税額×%) + MPF (fixed USD)
        processing_rate = settings.get("cpass_us_processing_rate", 2.10) / 100
        processing_fee = duty_amount_jpy * processing_rate
        mpf_fee = settings.get("cpass_fedex_mpf_simple_usd", 2.69) * exchange_rate
        details["米国関税処理手数料"] = round(processing_fee)
        details["MPF(簡易通関)"] = round(mpf_fee)
        total = round(processing_fee) + round(mpf_fee)
    elif "FedEx" in service_name:
        # IP系: import fee (JPY固定)
        import_fee = settings.get("cpass_fedex_import_fee_ip_jpy", 296)
        mpf_fee = settings.get("cpass_fedex_mpf_simple_usd", 2.69) * exchange_rate
        details["米国輸入手続き手数料"] = import_fee
        details["MPF/簡易通関"] = round(mpf_fee)
        total = import_fee + round(mpf_fee)
    elif "DHL" in service_name:
        reg_fee = settings.get("cpass_dhl_regulatory_simple_usd", 1.34) * exchange_rate
        details["規制手数料"] = round(reg_fee)
        total = round(reg_fee)

    return total, details

# ---- メイン利益計算 ----
@dataclass
class CalcInput:
    purchase_yen: float          # 仕入れ値（円）
    item_price_usd: float        # 販売価格（USD）
    weight_g: float              # 重量（g）
    length_cm: float = 0.0       # 長さ（cm）
    width_cm: float = 0.0        # 幅（cm）
    height_cm: float = 0.0       # 高さ（cm）
    category_id: int = 0         # eBayカテゴリID
    is_ddu: bool = False         # DDUモード（チェックでTrue）/ duty_pattern=None 時の解決根拠（後方互換）
    country_code: str = "US"     # 送付先国コード
    # 関税パターン: None=is_ddu由来(後方互換) / "included"(①商品価格内包) /
    #             "shipping"(②③送料に乗せる) / "ddu"(④US以外DDU)
    duty_pattern: Optional[str] = None
    # ②③専用: バイヤー徴収送料(=関税)を手動指定。None なら item_price × duty_rate で自動算出
    shipping_usd_override: Optional[float] = None
    # W212 washing 修正 (2026-06-02): 商品ごとの実関税率 (小数、例 0.30=I-B / 0.55=I-A)。
    # None = 従来の washing 挙動 (buyer 徴収送料 = 実関税と仮定し相殺、後方互換ゼロ変更)。
    # 設定時 = "shipping" パターンで buyer 徴収送料 (display) と seller 実負担関税 (actual)
    #   を分離計上 → 実関税 > buyer 徴収 (Section 232 等) で profit 過大計上を断つ。
    # ⚠️ 現状どの本番 caller も None (opt-in)。配線は別途 user 承認後。
    actual_duty_rate: Optional[float] = None
    # W220 (2026-06-04): per-listing ポイント実額(¥)。仕入先/カードで還元率が
    # 違うため listing ごとに実額を持つ。None = 従来の purchase_yen × point_rate
    # (global rate、後方互換ゼロ変更)。指定時は point_return = point_yen で確定。
    point_yen: Optional[float] = None

@dataclass
class ServiceResult:
    service_id: int
    service_name: str
    carrier: str
    charged_weight_kg: float
    zone_name: str
    base_rate: float
    fuel_surcharge_amount: float
    shipping_display: float       # 送料表示（base × fuel）
    additional_fees: dict         # 追加費用内訳
    additional_total: float
    total_shipping: float         # 合計送料コスト
    profit: float
    profit_rate: float
    tax_refund: float
    profit_with_refund: float
    profit_with_refund_rate: float
    is_listable: bool             # 出品推奨か

@dataclass
class CalcResult:
    # 売上
    revenue: float
    revenue_net: float           # 関税除く売上

    # 送料代
    shipping_usd: float          # バイヤー請求送料（関税分）

    # eBay費用内訳
    fvf: float
    fvf_rate: float
    intl_payment: float
    transaction_fee: float
    ad_fee: float
    payoneer: float
    point_return: float
    ebay_cost_subtotal: float    # 合計コスト（仕入れ除く）

    # 各送料サービス別結果
    service_results: list[ServiceResult] = field(default_factory=list)

def calculate(inp: CalcInput, settings: dict) -> CalcResult:
    _load_data()

    fx = settings["exchange_rate"]
    duty_rate = settings["duty_rate"] / 100
    pl_rate = settings["promoted_listing_rate"] / 100
    tax_rate = settings["consumption_tax_rate"] / 100
    point_rate = settings["point_reward_rate"] / 100
    payoneer_rate = settings["payoneer_fee_rate"] / 100
    intl_payment_rate = settings["intl_payment_rate"] / 100
    txn_fee = settings["transaction_fee_jpy"]
    store_plan = settings["store_plan"]
    fuel_fedex = settings["fuel_surcharge_fedex"] / 100
    fuel_dhl = settings["fuel_surcharge_dhl"] / 100

    # 関税パターン解決（duty_pattern 明示 > is_ddu 由来、後方互換）
    pattern = inp.duty_pattern
    if pattern is None:
        pattern = "ddu" if inp.is_ddu else "shipping"

    # 金銭直結の contract 検証: 未知パターンは silent な過少計上を生むため拒否
    # (typo 等で else→shipping 扱いになるが pattern=="shipping" gate を外れ
    #  CPaSS関税処理費が0に落ちて profit が過大化する。Q0: fail loud)
    if pattern not in ("included", "shipping", "ddu"):
        raise ValueError(
            f"duty_pattern must be one of included/shipping/ddu, got {pattern!r}"
        )

    # 金銭直結の contract 検証: 手入力送料(関税)の負値は revenue/FVF を壊すため拒否
    if inp.shipping_usd_override is not None and inp.shipping_usd_override < 0:
        raise ValueError(
            f"shipping_usd_override must be >= 0, got {inp.shipping_usd_override}"
        )

    # W212: 実関税率の負値は profit を不正に過大化するため拒否 (fail-loud、2 段レビュー指摘)
    if inp.actual_duty_rate is not None and inp.actual_duty_rate < 0:
        raise ValueError(
            f"actual_duty_rate must be >= 0, got {inp.actual_duty_rate}"
        )

    # パターン別: バイヤー徴収送料(shipping_usd) と seller実負担関税(duty_cost_jpy)
    if pattern == "ddu":
        # ④ US以外DDU: 関税はバイヤーが現地で支払い、seller負担なし
        shipping_usd = 0.0
        duty_cost_jpy = 0.0
    elif pattern == "included":
        # ① US向けDDP・US_Only: 関税を商品価格に内包。送料$0(Free)、関税はseller実負担
        shipping_usd = 0.0
        duty_cost_jpy = inp.item_price_usd * duty_rate * fx
    else:  # "shipping" (②③ US向けDDP 関税を送料に乗せる = US_only/US_only以外 共通)
        if inp.shipping_usd_override is not None:
            shipping_usd = inp.shipping_usd_override
        else:
            shipping_usd = inp.item_price_usd * duty_rate
        # バイヤー徴収送料=通関支払で相殺（profit上は washed）
        duty_cost_jpy = 0.0

    # W212 washing 修正 (2026-06-02): seller 実負担関税を分離計上。
    # buyer_shipping_jpy = バイヤーが実際に払う送料(=display関税)を income 化。
    # actual_duty_cost_jpy = seller が通関で実際に払う関税(= HS別 実関税率)。
    # legacy (actual_duty_rate=None) は従来挙動と数学的に完全一致:
    #   "shipping": actual_duty_cost = buyer_shipping_jpy → profit 式で相殺 = 従来 washed
    #   "included": actual_duty_cost = duty_cost_jpy (item×duty_rate×fx)
    #   "ddu":      両者 0
    # actual_duty_rate 設定時のみ "shipping"/"included" で実関税を採用し過大計上を断つ。
    buyer_shipping_jpy = shipping_usd * fx
    if inp.actual_duty_rate is not None:
        actual_duty_cost_jpy = (
            0.0 if pattern == "ddu"
            else inp.item_price_usd * inp.actual_duty_rate * fx
        )
    else:
        actual_duty_cost_jpy = (
            buyer_shipping_jpy if pattern == "shipping" else duty_cost_jpy
        )

    # 売上
    revenue = (inp.item_price_usd + shipping_usd) * fx
    revenue_net = inp.item_price_usd * fx  # 関税分(送料)を除いた純売上

    # eBay手数料（FVF は item+shipping の合計に課金）
    total_usd = inp.item_price_usd + shipping_usd
    fvf_rate = get_ebay_fvf_rate(inp.category_id, total_usd, store_plan)
    fvf = revenue * fvf_rate
    intl_payment = revenue * intl_payment_rate
    ad_fee = revenue * pl_rate
    # Payoneer手数料: eBayが全手数料を差し引いた後の入金額に対して2%
    # ad_fee も eBay 側で差し引かれるため、Payoneer基準額から控除する
    payoneer_base = revenue - fvf - intl_payment - txn_fee - ad_fee
    payoneer = payoneer_base * payoneer_rate
    # W220: per-listing ポイント実額があればそれを優先 (仕入先/カードで率が異なる)。
    # None = 従来の global rate 換算 (後方互換)。
    point_return = (
        inp.point_yen if inp.point_yen is not None
        else inp.purchase_yen * point_rate
    )

    # W212: 関税コストは actual_duty_cost_jpy に統一 (legacy=None では shipping→
    # shipping_usd*fx / included→duty_cost_jpy / ddu→0 と同値 = 表示集計も従来一致。
    # actual 設定時は profit と同じ実関税を集計に反映 = 表示の不整合を防ぐ)。
    ebay_cost_subtotal = (
        fvf + intl_payment + txn_fee + ad_fee + payoneer + actual_duty_cost_jpy
    )

    # 消費税還付: 仕入価格は税込み前提なので、税込÷(1+税率)×税率で内税額を算出
    # 例) 税込10万円、税率10% → 10万 × 10/110 ≒ 9,091円
    tax_refund = inp.purchase_yen * tax_rate / (1 + tax_rate)

    # 各サービス別送料・利益計算
    selected = settings.get("selected_services", [])
    services_df = _cache['shipping_services']

    service_results = []
    for _, svc in services_df.iterrows():
        svc_id = int(svc['ServiceID'])
        svc_name = str(svc['ServiceName'])

        # 選択されているサービスのみ計算
        if selected and svc_name not in selected:
            continue

        vol_div = int(svc['VolumetricDivisor'])
        carrier = str(svc['Carrier'])

        # 課金重量
        charged_kg = get_chargeable_weight_kg(
            inp.weight_g, inp.length_cm, inp.width_cm, inp.height_cm,
            vol_div,
            settings.get("weight_padding_g", 0),
            settings.get("length_padding_cm", 0),
            settings.get("width_padding_cm", 0),
            settings.get("height_padding_cm", 0),
        )

        # ゾーン
        zone_name = get_zone_name(svc_id, inp.country_code)
        if zone_name is None:
            continue

        # ベース送料
        base_rate = get_base_shipping_rate(svc_id, zone_name, charged_kg)
        if base_rate is None:
            continue

        # 燃料サーチャージ: service_nameに"DHL"を含むサービスはDHLレート
        # （carrier="CPaSS"でもサービス名が "CPaSS - DHL" ならDHLレートを使う）
        if "DHL" in svc_name:
            fuel_rate = fuel_dhl
        elif carrier in ("FedEx", "CPaSS", "eLogi"):
            fuel_rate = fuel_fedex
        else:
            fuel_rate = 0.0

        fuel_amount = base_rate * fuel_rate
        shipping_display = round(base_rate + fuel_amount)

        # 追加費用（AdditionalFees.csv）: FIXED と PER_KG の両方を計算
        add_total_csv, add_details_csv = get_additional_fees(svc_id, inp.country_code, charged_kg)

        # CPaSS専用追加費用（米国関税処理手数料の基準となる関税額JPY）
        # ①=商品価格内包の関税, ②③=送料経由の徴収関税, ④=0
        # W212 注: CPaSS 処理費は「buyer 徴収 (申告) 額」に対する手数料なので、
        # actual_duty_rate (HS別実関税) には意図的に連動させない (display 額が正)。
        if pattern == "included":
            duty_jpy = duty_cost_jpy
        elif pattern == "shipping":
            duty_jpy = shipping_usd * fx
        else:
            duty_jpy = 0.0
        cpass_total, cpass_details = get_cpass_additional_fees(svc_name, fx, settings, duty_jpy)

        additional_fees = {**add_details_csv, **cpass_details}
        additional_total = add_total_csv + cpass_total

        total_shipping = shipping_display + additional_total

        # 利益計算 (W212 washing 修正)
        # profit = (純売上 + buyer徴収送料income) - 仕入 - eBay費用 - 実送料 - seller実負担関税
        # buyer_shipping_jpy / actual_duty_cost_jpy は上流で算出 (legacy は従来式と完全一致)。
        ebay_fees_no_duty = fvf + intl_payment + txn_fee + ad_fee + payoneer
        profit = (
            revenue_net + buyer_shipping_jpy
            - inp.purchase_yen - ebay_fees_no_duty - total_shipping
            - actual_duty_cost_jpy
        )
        profit_rate = profit / revenue if revenue > 0 else 0.0

        profit_with_refund = profit + tax_refund + point_return
        profit_with_refund_rate = profit_with_refund / revenue if revenue > 0 else 0.0

        # 出品可否判定
        is_listable = _check_listable(inp.item_price_usd, profit, fx)

        service_results.append(ServiceResult(
            service_id=svc_id,
            service_name=svc_name,
            carrier=carrier,
            charged_weight_kg=charged_kg,
            zone_name=zone_name,
            base_rate=base_rate,
            fuel_surcharge_amount=round(fuel_amount),
            shipping_display=shipping_display,
            additional_fees=additional_fees,
            additional_total=additional_total,
            total_shipping=total_shipping,
            profit=round(profit),
            profit_rate=profit_rate,
            tax_refund=round(tax_refund),
            profit_with_refund=round(profit_with_refund),
            profit_with_refund_rate=profit_with_refund_rate,
            is_listable=is_listable,
        ))

    return CalcResult(
        revenue=round(revenue),
        revenue_net=round(revenue_net),
        shipping_usd=shipping_usd,
        fvf=round(fvf),
        fvf_rate=fvf_rate,
        intl_payment=round(intl_payment),
        transaction_fee=txn_fee,
        ad_fee=round(ad_fee),
        payoneer=round(payoneer),
        point_return=round(point_return),
        ebay_cost_subtotal=round(ebay_cost_subtotal),
        service_results=service_results,
    )

def find_min_listable_price_usd(
    purchase_yen: float,
    weight_g: float,
    category_id: int,
    settings: dict,
    *,
    is_ddu: bool = False,
    country_code: str = "US",
    lo_usd: float = 10.0,
    hi_usd: float = 2000.0,
    step_usd: float = 0.50,
) -> Optional[float]:
    """利益が出る最低出品価格 (USD) を探索する (二分探索).

    Args:
        purchase_yen: 仕入れ価格 (円)
        weight_g: 重量 (g)
        category_id: eBay カテゴリID
        settings: settings.json 相当の dict
        is_ddu: DDU モード (default False = DDP 想定)
        country_code: 送付先国 (default "US")
        lo_usd / hi_usd: 探索範囲
        step_usd: 丸め単位 (0.50 = 50 cent 単位)

    Returns:
        最低 listable な USD 価格。見つからなければ None.

    探索ロジック:
        calculate() で全送料サービスのうち **1 つでも is_listable=True** なら OK。
        profit は USD 価格に対して単調増加する前提で二分探索。
    """
    if purchase_yen <= 0 or weight_g <= 0:
        return None

    def _is_listable_at(price: float) -> bool:
        inp = CalcInput(
            purchase_yen=purchase_yen,
            item_price_usd=price,
            weight_g=weight_g,
            category_id=category_id,
            is_ddu=is_ddu,
            country_code=country_code,
        )
        try:
            res = calculate(inp, settings)
        except Exception:  # noqa: BLE001
            return False
        return any(s.is_listable for s in res.service_results)

    # 上限で不採算なら None
    if not _is_listable_at(hi_usd):
        return None

    # 二分探索: 条件を満たす最小の price を求める
    lo, hi = lo_usd, hi_usd
    import math as _math
    while hi - lo > step_usd:
        mid = (lo + hi) / 2
        if _is_listable_at(mid):
            hi = mid
        else:
            lo = mid
    # step_usd 単位に切り上げ
    price = _math.ceil(hi / step_usd) * step_usd
    return float(price)


def _check_listable(item_price_usd: float, profit: float, fx: float) -> bool:
    """販売価格帯別の出品可否判定（為替レートは呼び出し側から渡す）"""
    tiers = [
        (30,     500,  0.25),
        (100,   1000,  0.20),
        (300,   2000,  0.15),
        (999999, 5000, 0.12),
    ]
    for price_limit, min_profit_jpy, min_rate in tiers:
        if item_price_usd <= price_limit:
            rate = profit / (item_price_usd * fx) if item_price_usd > 0 else 0
            return profit >= min_profit_jpy and rate >= min_rate
    return False


def check_supplier_candidate_profitable(
    profit_with_refund: float,
    purchase_yen: float,
    profit_without_refund: Optional[float] = None,
) -> tuple[bool, dict]:
    """
    仕入先候補の採用判定（2026-04-19決定ロジック / 2026-06-22 600円床を還付抜き化）

    - 最低利益額 600円（**還付抜き利益で判定** = profit_without_refund）
      消費税還付込み利益で判定すると、還付分だけ下駄を履いて本来不採用の薄利候補を
      採用してしまう（仕入れ=money-direct）ため、user 判断で還付抜きに是正（2026-06-22）。
    - 仕入額に応じたスライド利益率: 5,000円以下=10%、100,000円以上=20%、間は線形補間
      ※スライド率の分母は現状 **還付込み利益（profit_with_refund）** のまま（従来挙動を維持）。
        還付抜き化すべきかは user 判断待ちの論点（2026-06-22）。
    - 両方の条件を満たすときのみ採用可

    Args:
        profit_with_refund: 消費税還付込み利益（円）。スライド率の floor 判定に使用。
        purchase_yen: 仕入価格（円、税込）
        profit_without_refund: 消費税還付抜き利益（円）。600円絶対床の判定に使用。
            None の場合は後方互換のため profit_with_refund を代用するが、
            その場合 600円床は還付込みで判定される（呼び出し元は必ず還付抜き値を渡すこと）。

    Returns:
        (採用可否, 内訳dict: floor_profit, required_rate, pass_600, pass_floor,
         profit_with_refund, profit_without_refund)
    """
    absolute_floor = 600

    # 600円絶対床は還付抜き利益で判定（2026-06-22 money-direct 是正）。
    # 後方互換: profit_without_refund 未指定なら還付込みを代用（呼び出し元は渡すべき）。
    profit_for_floor600 = (
        profit_without_refund if profit_without_refund is not None else profit_with_refund
    )

    if purchase_yen <= 5000:
        required_rate = 0.10
    elif purchase_yen >= 100000:
        required_rate = 0.20
    else:
        required_rate = 0.10 + (purchase_yen - 5000) * 0.10 / 95000

    # 境界比較の一貫性のためfloor値はint化（円単位で判定）
    floor_by_rate = round(purchase_yen * required_rate)
    floor_profit = max(absolute_floor, floor_by_rate)

    # 600円絶対床 = 還付抜き / スライド率床 = 還付込み（率の基準は論点、上記 docstring 参照）。
    pass_600 = profit_for_floor600 >= absolute_floor
    pass_floor = profit_with_refund >= floor_profit
    ok = pass_600 and pass_floor

    return ok, {
        "floor_profit": floor_profit,
        "required_rate": required_rate,
        "pass_600": pass_600,
        "pass_floor": pass_floor,
        "profit_with_refund": round(profit_with_refund),
        "profit_without_refund": (
            round(profit_without_refund) if profit_without_refund is not None else None
        ),
    }
