---
name: fedex-tsca
carrier: fedex
when_to_use:
  - TSCA (Toxic Substances Control Act) Certification が求められる化学品
  - ART markers / paints / chemical products 等
  - "TSCA Certification" キーワードで判定
priority: 2
version: 1.0
based_on:
  - 2026-02-07 AWB 888377212167 (アート markers TSCA 対応)
---

# FedEx TSCA 証明テンプレート (英文)

## Subject
`TRK#{{tracking_number}} - TSCA Certification`

## 本文

```
Dear FedEx Logistics Team,

Thank you for your email.
Please find attached the completed TSCA Certification for the shipment
under the tracking number {{tracking_number}}.

The shipped item is {{product_description_en}}, intended for drawing
and coloring purposes (finished consumer article).

It is considered a finished article and does not contain any chemical
substances intended for release. Accordingly, the shipment is
TSCA exempt (Negative Certification).

The Commercial Invoice has already been provided, and the information is
consistent with the TSCA certification.

Please let me know if you need any additional information or documentation
to proceed with customs clearance.

Best regards,
TOYOTASUMI
```

## 注意点

- TSCA Negative Certification は **finished consumer article** であることの証明
- 化学成分 (溶剤/顔料) の具体的組成は記載しない (finished article はその必要なし)
- user 側で「TSCA 証明書 PDF」を別途添付する必要あり (自動生成不可)
