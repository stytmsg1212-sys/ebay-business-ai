---
name: ups-basic
carrier: ups
when_to_use:
  - UPS からの customs information 要求
  - UPS Brokerage Services 関連
priority: 1
version: 1.0
---

# UPS 基本回答テンプレート (英文)

## 宛先 (static)
- TO: importbrokerage@ups.com (or 受信メール送信元)
- CC: 受信メールの OSV / broker 担当者

## Subject
`Re: UPS Tracking {{tracking_number}} - Customs Information`

## 本文

```
Dear UPS Import Brokerage Team,

Thank you for your request regarding the following UPS shipment.
The requested customs clearance information is provided below.

Tracking Number: {{tracking_number}}
Shipper: TOYOTASUMI (from Japan)

Description of Goods:
{{product_description_en}}

End Use:
{{product_end_use_en}}

Manufacturer Information:
{{manufacturer_name}}
{{manufacturer_address}}

Composition:
{{composition_en}}

Suggested HTSUS Classification (for reference):
{{hts_code}} — {{hts_description}}
Please verify with your customs broker.

{{photo_attachment_note}}

The shipper is a retailer and is not the manufacturer.

Best regards,
TOYOTASUMI
```
