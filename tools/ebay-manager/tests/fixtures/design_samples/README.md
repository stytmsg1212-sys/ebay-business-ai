# W10 画像加工デザイン参照サンプル

MonoHonpo カバー画像合成の design reference。Phase C (image_composer / image_renderer) の実装/テスト時に「出力がこの雰囲気に近いか」を目視比較するために使用。

## ファイル

| ファイル | 商品 | 配置パターン |
|---------|------|-------------|
| `sample_01_pioneer_stack.HEIC` | Pioneer vintage audio 積重ね | ロゴカード立てて右後方、立体的配置 |
| `sample_02_maxell_flatlay.png` | maxell Bluetooth cassette + 付属品 | フラットレイ、ロゴカードはアクセサリ群の一部 |
| `sample_03_ohuhu_ontop.png` | Ohuhu / Sanrio 大箱 | ロゴカード商品の上に置く |
| `sample_04_cable_corner.png` | ケーブル類 | ロゴカード左上コーナー、商品中央 |

## 共通デザイン要素

- **背景**: 薄グレー〜薄ベージュのグラデーション (studio seamless paper)
- **ロゴカード**: 白い名刺サイズの紙カード、MONO ロゴ中央、影付き
- **床影**: 商品とカード両方から落ちる (drop shadow ではなく floor shadow)
- **Style**: Studio product photography like

## MVP (Phase C-1) 目標

sample_01_pioneer_stack をベースに、商品メイン + カード右後方の 1 パターン固定で再現する。
Phase C-2 で商品形状に応じた adaptive layout に拡張予定。
