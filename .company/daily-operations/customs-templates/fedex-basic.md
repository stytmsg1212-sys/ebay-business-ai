---
name: fedex-basic
carrier: fedex
when_to_use:
  - FedEx からの一般的な customs clearance 情報要求
  - "Better description of the item / Manufacturer Name and Address / End Use / Composition" 系
  - TRK#/AWB 指定 の英文返信要求
priority: 1
version: 1.0
based_on:
  - 2026-04-24 TRK#870904145187 ONYX BOOX Leaf2 (今日の成功事例)
  - 2026-02-28 AWB 889064067660 Razer HyperFlux (過去 user 実績)
---

# FedEx 基本回答テンプレート (英文)

## 宛先 (static、Claude に決めさせない)
- TO: paperwork@fedex.com
- CC: {{carrier_case_cc}}, {{sender_osv_email}}

## Subject
`TRK#{{tracking_number}} - Customs Clearance Information`

## 本文スケルトン

```
Dear FedEx Team,

Thank you for your email regarding the above shipment.
Please find the requested manufacturer information below in order to
proceed with customs clearance.

Tracking Number: {{tracking_number}}

Description of Goods:
{{product_description_en}}

End Use:
{{product_end_use_en}}

Manufacturer Information:
{{manufacturer_name}}
{{manufacturer_address}}
{{manufacturer_tel_optional}}

Suggested HTSUS Classification (for reference):
{{hts_code}} — {{hts_description}}
{{hts_ruling_optional}}
Please verify with your customs broker.

{{photo_attachment_note}}

The shipper is a retailer and is not the manufacturer.

Please let me know if any additional information or documentation
is needed.

Best regards,
TOYOTASUMI
(Japanese eBay Seller)
```

## 戦略ルール (feedback_customs_response_strategy.md 反映)

- Manufacturer = 日本代理店 を第一選択 (中国本社は書かない)
- End Use = 商品の**実用途**のみ ("resale"/"eBay"/"commercial" は禁句)
- アルミ/鉄不使用なら明示的に宣言 ("No aluminum or steel parts...")
- 末尾定型句 `The shipper is a retailer and is not the manufacturer.` 必須
- HTS コードは根拠 Ruling 付きで提示、最終判断は通関士に委ねる旨注記
