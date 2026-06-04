"""W216: 関税ポリシーメール (tariff_policy) カテゴリ判定のテスト (2026-06-03)。

関税は利益計算 (settings.duty_rate, W215) に直結するため、率変更/追加請求/還付の
ニュースを専用カテゴリで漏れなく検知し毎朝 Discord 掲出する。実際に Gmail 全数調査
(2026-06-03) で観測した OrangeConnex / FedEx / eBay の件名で判定を担保。

旧 query に OrangeConnex が不在で率変更通知が未取得だった (利益直結の漏れ) のが
W216 の契機。本テストは tariff_policy への正分類 + 売上/通関要求/buyerメッセージを
誤分類しないこと (false positive 防止) を固定する。
"""
from tasks.task_email_pickup import _categorize_email


def test_orangeconnex_rate_change_is_tariff_policy():
    """OC の推定関税率変更通知 (15%→10% 等) を tariff_policy に分類。"""
    assert _categorize_email(
        "最新の米国関税政策に伴う米国向け推定関税・税金（Duty & Tax）率変更のお知らせ",
        "no-reply@orangeconnex.com",
    ) == "tariff_policy"


def test_orangeconnex_ieepa_refund_is_tariff_policy():
    """OC の IEEPA 関税還付 (返金) 進捗を tariff_policy に分類。"""
    assert _categorize_email(
        "米国 IEEPA 関税還付の進捗状況に関するご案内",
        "オレンジコネックスジャパン <cs.jp@orangeconnex.com>",
    ) == "tariff_policy"


def test_fedex_import_fee_bill_is_tariff_policy():
    """FedEx 直送分の輸入手数料 (関税立替) 請求を tariff_policy に分類。"""
    assert _categorize_email(
        "FedExの貨物に対する輸入手数料の支払金額 889911532475",
        "フェデックス <fedex@fedex.com>",
    ) == "tariff_policy"


def test_fedex_unpaid_duty_is_tariff_policy():
    """FedEx の未払い関税 (追加請求の警告) を tariff_policy に分類。"""
    assert _categorize_email(
        "米国およびEU輸入貨物の未払い関税およびその他税金 – FedEx荷送人アカウント *7197",
        "FedEx <noreply@fedex.com>",
    ) == "tariff_policy"


def test_ebay_tariff_policy_notice_is_tariff_policy():
    """eBay の米国関税政策変更のお知らせを tariff_policy に分類。"""
    assert _categorize_email(
        "【重要】米国関税政策変更のお知らせ",
        "イーベイ・ジャパン <no-reply@ebay.co.jp>",
    ) == "tariff_policy"


def test_fedex_ieepa_refund_is_tariff_policy():
    """FedEx の IEEPA 関税返金通知を tariff_policy に分類。"""
    assert _categorize_email(
        "米国IEEPA関税返金：重要なお知らせ",
        "FedEx <fedex@fedex.com>",
    ) == "tariff_policy"


def test_sale_email_not_tariff_policy():
    """eBay の売上通知は tariff_policy にしない (false positive 防止)。"""
    assert _categorize_email(
        "HIOKI DT4282 Digital Multimeter ...が売れました",
        "eBay <ebay@ebay.com>",
    ) == "sale"


def test_fedex_clearance_request_stays_customs_request():
    """FedEx の per-shipment 通関情報要求は customs_request を維持
    (tariff_policy = 制度/請求ニュース とは別物)。"""
    assert _categorize_email(
        "FedExより通関についてのご案内 / 製造業者の情報をご提供ください - 運送状番号：870480400096",
        "FedEx_Japan_Trace <trace@fedex.com>",
    ) == "customs_request"


def test_buyer_message_not_tariff_policy():
    """eBay の buyer メッセージは buyer_message を維持。"""
    assert _categorize_email(
        "crisalexe sent a message about HIOKI DT4282 ...",
        "eBay - crisalexe <member@ebay.com>",
    ) == "buyer_message"


def test_non_tariff_sender_with_keyword_not_tariff_policy():
    """tariff sender 以外 (例: 仕入先) からの『関税』語は tariff_policy にしない
    (sender AND subject の AND 条件を担保)。"""
    cat = _categorize_email(
        "関税についてのご案内",
        "rakuten <info@order.rakuten.co.jp>",
    )
    assert cat != "tariff_policy"
