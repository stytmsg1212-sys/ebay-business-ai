---
name: dhl-basic
carrier: dhl
when_to_use:
  - DHL からの customs information 要求
  - DHL の AWB 番号指定返信
priority: 1
version: 1.0
---

# DHL 基本回答テンプレート (英文)

## 宛先 (static)
- TO: 受信メールの送信元 (OSV / customs agent)
- CC: 不要 (DHL は通常 OSV 単一窓口)

## Subject
`Re: DHL AWB {{tracking_number}} - Customs Clearance Information`

## 本文

```
Dear DHL Customs Team,

Thank you for your inquiry regarding the above DHL shipment.
Please find the requested information below.

AWB Number: {{tracking_number}}
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

## FedEx との差分

- AWB (Air Waybill) 番号形式 (10-11 桁数字)
- CC 不要 (OSV 単一窓口が多い)
- "TRK#" ではなく "AWB {{number}}"
