---
name: fedex-ja-domestic
carrier: fedex
when_to_use:
  - 日本の FedEx CS (Kanako 様、Hanae 様、Masae 様等) から日本語で来た場合
  - 英文回答を paperwork@fedex.com に送った後、日本側に短報を送る場合
priority: 3
version: 1.0
based_on:
  - 2025-12-10 FEDEX追跡番号 886578540186 (菊地様ル・クルーゼ案件で日本語短報)
---

# FedEx 日本 CS 向け 日本語短報テンプレート

## Subject
`Re: FedEx / アメリカ向け貨物につきまして TRK#{{tracking_number}}`

## 本文

```
{{japan_cs_contact_name}}様

お世話になっております。TOYOTASUMI です。
ご連絡ありがとうございました。

TRK#{{tracking_number}} の通関情報について、本日 paperwork@fedex.com 宛に
英文で提出いたしました。
CC: {{carrier_case_cc}} / {{sender_osv_email}}

提出内容の概要:
- 商品: {{product_description_ja}}
- 製造元: {{manufacturer_name}} ({{manufacturer_address_short}})
- 最終用途: {{product_end_use_ja}}
- 素材: {{composition_ja}}
- HTS 参考コード: {{hts_code}}

ご確認のほど、よろしくお願いいたします。

TOYOTASUMI
```

## 注意

- 日本 CS は **参考情報** なので短く済ませる (米国 CS が主対応窓口)
- 商品写真の再添付は不要 (paperwork@fedex.com で既に送付済み)
