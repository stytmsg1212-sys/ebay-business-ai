---
topic: eBay出品 Description テンプレート (v4 完成版 · MonoHonpo Brand System)
version: 4.0
confirmed_at: 2026-04-20
supersedes: v3.0 (2026-04-20 午前)
brand_system: monohonpo-brand-v3.html (Claude Design handoff)
placeholder_syntax: "{{name}}"  # 二重波括弧。CSS braces と衝突しない
---

# eBay Description Template v4 — MonoHonpo

## 設計方針（v3 からの変更点）

- **絵文字全廃** — ⚠️🛡️🌐ℹ️📋🏪🌟 を全て unicode 幾何記号 (◯ ◇ ★ ▲) または text ラベル (CERT / JP) に置換
- **Hanko DDP シール** — 円形 (variant A, baseline) を採用。transform: rotate(-6deg) + shu vermillion
- **Enso brush rank** — SVG inline で円相を描画、中央に rank 文字を配置
- **和紙パレット** — #f6f2ea (washi) / #1a1817 (sumi) / #a8341b (shu朱) / #8b7355 (brass) / #6b7a5c (sage)
- **タイポ** — Cormorant Garamond (display) + Inter (body) + JetBrains Mono (spec/meta)
- **Gadget Mode** — 産業機器/電子機器向けの CSS クラス `.gadget` を追加（spec strip trio、dark precision panel 対応）
- **プレースホルダ構文** — `{{name}}` 二重波括弧。CSS の `{` `}` と衝突しないよう `re.sub(r'\{\{(\w+)\}\}', ...)` でPhase 3 generator が置換
- **VeRO / eBay UI 衝突回避** — CDN は Google Fonts のみ（fallback あり）、JavaScript なし、form 要素なし

## プレースホルダ一覧

| プレースホルダ | 意味 | 例 |
|---|---|---|
| `{{product_name}}` | 商品名（英語タイトル、SEO最適化） | `Sony WH-1000XM5 Wireless Noise Cancelling Headphones` |
| `{{product_sub}}` | 商品サブタイトル（italic） | `Black · Flagship model · Tested and documented` |
| `{{rank}}` | ランク記号 | `A` / `S` / `As-Is` 等 |
| `{{rank_label}}` | ランク英語ラベル | `Excellent` / `Like New` |
| `{{rank_jp}}` | ランク日本語説明 | `Tested · Minor Wear` |
| `{{quick_notes}}` | 個別商品の状態メモ（Claude 生成） | `Tested working. Minor scuffs on headband underside; ANC and Bluetooth 5.2 verified.` |
| `{{includes_rows}}` | 付属品リスト（事前フォーマット済み HTML） | `<div class="inc"><strong>Headphones</strong>Sony WH-1000XM5 (Black)</div>` ... |
| `{{specs_rows}}` | 仕様テーブル行（事前フォーマット済み `<tr>`） | `<tr><td>Brand</td><td>Sony</td></tr>` ... |
| `{{shipping_origin}}` | 発送元 | `Tokyo, Japan` |
| `{{shipping_carrier}}` | 運送会社 | `DHL SpeedPAK · tracked, insured` |
| `{{shipping_handling}}` | ハンドリング期間 | `1–3 business days` |
| `{{shipping_delivery_us}}` | US配送所要日数 | `6–10 business days typical` |
| `{{shipping_packaging}}` | 梱包 | `Double-boxed · bubble-wrapped · waterproof liner` |
| `{{shipping_notes}}` | 商品別発送注意（空文字可） | `Ships in original retail box.` or `""` |
| `{{mode_class}}` | レイアウトモード | `default` or `gadget`（電子/産業機器） |

## 8段階ランク体系

| Rank | EN Label | JP Hint | eBay Cond ID |
|---|---|---|---|
| N | New (Unopened) | Brand New Sealed | 1000 |
| S | Like New | Opened · No Wear | 1500 |
| A | Excellent | Tested · Minor Wear | 3000 |
| B | Good | Tested · Visible Wear | 3000 |
| C | Fair | Tested · Heavy Wear | 3000 |
| D | Issues | Working · Limited Function | 3000 |
| PO | Power-On Only | Powers On · Untested | 3000 + ItemSpecifics |
| As-Is | As-Is | Not Tested · No Warranty | 7000 |

## Gadget Mode 判定ルール（listing_generator 用）

以下のいずれかに該当する場合 `mode_class="gadget"`:

1. eBay Category が以下配下:
   - `Consumer Electronics` (293), `Cameras & Photo` (625), `Business & Industrial` (12576)
2. Item Specifics の `Brand` が以下を含む:
   - KEYENCE / Omron / Mitsubishi / Hitachi / Nikon / Canon / Sony / Panasonic / Zoom / Roland
3. Claude 生成の `{{specs_rows}}` が3行以上ある

Gadget mode 発動時は spec strip trio (3カラム測定値) が有効化、port diagram が差込可能。

## ランク自動推定ルール（v3 から継承）

| 仕入先日本語 | 推定ランク |
|---|---|
| 新品／未開封／シュリンク付 | N |
| 新品同様／未使用／開封品 | S |
| 美品／美品に近い | A |
| 良品／並品／普通 | B |
| 使用感あり | C |
| 傷あり／難あり／訳あり | D |
| 通電確認のみ（動作未確認明記） | PO |
| 動作未確認／ジャンク／部品取り／故障 | As-Is |

### Quick Notes 自動生成ポイント
- **As-Is** は理由必須（電源なし/基板破損/清掃困難）
- **PO** は「Powers on, but audio/communication/operation not verified」と明示
- **A/B/C/D** は動作確認項目をリスト化（「Power ON / Audio output / Charging / Bluetooth pairing」等）

---

## HTML テンプレート（v4 完成版）

Phase 3 listing_generator.py が `re.sub(r'\{\{(\w+)\}\}', replace, html)` で埋める。

```html
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500;600;700&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">

<style>
.mh-wrap{background:#f6f2ea;color:#1a1817;font-family:'Inter','Helvetica Neue',Arial,sans-serif;font-size:15px;line-height:1.6;max-width:860px;margin:0 auto;padding:0;}
.mh-wrap *{box-sizing:border-box;}
.mh-wrap h1,.mh-wrap h2,.mh-wrap h3,.mh-wrap h4{font-family:'Cormorant Garamond','Times New Roman',serif;color:#1a1817;margin:0;}
.mh-wrap .mh-jp{font-family:'Cormorant Garamond','Times New Roman',serif;letter-spacing:4px;}
.mh-wrap .mh-mono{font-family:'JetBrains Mono','Courier New',monospace;letter-spacing:1.5px;}

/* ==== Masthead ==== */
.mh-mast{text-align:center;padding:44px 40px 32px;border-bottom:1px solid #d8cdb5;background:#fbf9f3;}
.mh-mast .mh-origin{font-family:'Cormorant Garamond','Times New Roman',serif;font-size:11px;letter-spacing:8px;color:#a8341b;margin-bottom:14px;font-weight:500;}
.mh-mast h1{font-size:34px;font-weight:500;line-height:1.2;margin-bottom:10px;color:#1a1817;}
.mh-mast .mh-sub{font-family:'Cormorant Garamond','Times New Roman',serif;font-style:italic;font-size:17px;color:#6b6157;margin:0;}

/* ==== Hanko DDP Seal ==== */
.mh-hanko{display:flex;gap:24px;align-items:center;padding:26px 32px;background:#fbf9f3;border:1px solid #d8cdb5;border-left:4px solid #a8341b;margin:32px 40px;}
.mh-hanko-seal{width:104px;height:104px;border-radius:50%;background:#a8341b;color:#fbf9f3;display:flex;flex-direction:column;align-items:center;justify-content:center;flex-shrink:0;box-shadow:inset 0 0 0 2px #fbf9f3,0 0 0 3px #a8341b;position:relative;transform:rotate(-6deg);font-family:'Cormorant Garamond','Times New Roman',serif;}
.mh-hanko-seal .mh-hs-t{font-size:10px;letter-spacing:3px;font-weight:500;}
.mh-hanko-seal .mh-hs-m{font-size:22px;font-weight:700;line-height:1;margin:3px 0;letter-spacing:1px;}
.mh-hanko-seal .mh-hs-b{font-family:'JetBrains Mono','Courier New',monospace;font-size:8px;letter-spacing:2px;margin-top:2px;}
.mh-hanko-body h3{font-family:'Cormorant Garamond','Times New Roman',serif;font-size:22px;font-weight:600;margin-bottom:8px;}
.mh-hanko-body p{margin:0;color:#3a332c;font-size:14px;line-height:1.55;}
.mh-hanko-body .mh-strong{color:#a8341b;font-weight:600;}

/* ==== Rank Block (Enso brush) ==== */
.mh-rank{padding:32px;background:#fbf9f3;border:1px solid #d8cdb5;margin:0 40px 32px;text-align:center;}
.mh-rank .mh-kicker{font-family:'Cormorant Garamond','Times New Roman',serif;font-size:11px;letter-spacing:6px;color:#6b6157;margin-bottom:14px;text-transform:uppercase;}
.mh-rank-brush{width:120px;height:120px;margin:0 auto 16px;position:relative;}
.mh-rank-brush .mh-rb-letter{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-family:'Cormorant Garamond','Times New Roman',serif;font-size:48px;font-weight:600;color:#1a1817;}
.mh-rank h3{font-size:28px;font-weight:500;margin-bottom:6px;}
.mh-rank .mh-rank-jp{font-family:'Cormorant Garamond','Times New Roman',serif;font-size:13px;color:#6b6157;margin-bottom:16px;letter-spacing:3px;}
.mh-rank .mh-quick{max-width:520px;margin:0 auto;padding:16px 20px;background:#f6f2ea;border-left:2px solid #1a1817;text-align:left;font-family:'Cormorant Garamond','Times New Roman',serif;font-style:italic;font-size:16px;color:#3a332c;line-height:1.5;}

/* ==== Section head ==== */
.mh-sec{padding:0 40px;margin-bottom:36px;}
.mh-sec-head{display:flex;align-items:baseline;gap:16px;margin-bottom:20px;padding-bottom:8px;border-bottom:1px solid #d8cdb5;}
.mh-sec-head h2{font-size:24px;font-weight:500;}
.mh-sec-head .mh-jp{font-size:11px;color:#6b6157;}
.mh-sec-head .mh-num{margin-left:auto;font-family:'JetBrains Mono','Courier New',monospace;font-size:10px;color:#9a8f82;letter-spacing:2px;}

/* ==== Includes grid ==== */
.mh-inc-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.mh-inc{padding:14px 16px 14px 36px;background:#fbf9f3;border:1px solid #d8cdb5;position:relative;font-size:14px;color:#3a332c;}
.mh-inc:before{content:'';position:absolute;left:14px;top:18px;width:10px;height:10px;border:1.5px solid #a8341b;border-radius:50%;box-sizing:border-box;}
.mh-inc:after{content:'';position:absolute;left:17px;top:21px;width:4px;height:4px;background:#a8341b;border-radius:50%;}
.mh-inc strong{display:block;color:#1a1817;font-family:'Cormorant Garamond','Times New Roman',serif;font-size:15px;font-weight:600;margin-bottom:2px;}

/* ==== Specs table ==== */
.mh-specs{width:100%;border-collapse:collapse;}
.mh-specs tr{border-bottom:1px solid #d8cdb5;}
.mh-specs tr:last-child{border-bottom:0;}
.mh-specs td{padding:14px 0;font-size:14px;vertical-align:top;}
.mh-specs td:first-child{font-family:'JetBrains Mono','Courier New',monospace;font-size:10px;letter-spacing:2px;color:#6b6157;text-transform:uppercase;width:180px;padding-top:17px;}
.mh-specs td:last-child{color:#1a1817;font-family:'Cormorant Garamond','Times New Roman',serif;font-size:17px;}

/* ==== Gadget mode spec strip (optional, activates when .gadget is on wrap) ==== */
.mh-wrap.gadget .mh-spec-strip{display:grid;grid-template-columns:1fr 1fr 1fr;margin:0 40px 32px;border-top:1px solid #d8cdb5;border-bottom:1px solid #d8cdb5;background:#fbf9f3;}
.mh-wrap:not(.gadget) .mh-spec-strip{display:none;}
.mh-spec-strip > div{padding:16px 0;text-align:center;border-right:1px solid #d8cdb5;}
.mh-spec-strip > div:last-child{border-right:0;}
.mh-spec-strip .k{font-family:'JetBrains Mono','Courier New',monospace;font-size:9px;letter-spacing:2px;color:#9a8f82;text-transform:uppercase;margin-bottom:4px;}
.mh-spec-strip .v{font-family:'Cormorant Garamond','Times New Roman',serif;font-size:16px;font-weight:600;color:#1a1817;}

/* ==== Shipping block (dark) ==== */
.mh-ship{padding:28px 32px;background:#1a1817;color:#f6f2ea;margin:0 40px 32px;}
.mh-ship h3{font-family:'Cormorant Garamond','Times New Roman',serif;font-size:22px;color:#fbf9f3;margin-bottom:14px;font-weight:500;}
.mh-ship h3 .mh-jp{font-family:'Cormorant Garamond','Times New Roman',serif;font-size:11px;color:#9a8f82;letter-spacing:4px;margin-left:12px;}
.mh-ship-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px 28px;}
.mh-ship-grid strong{display:block;font-family:'JetBrains Mono','Courier New',monospace;font-size:9px;color:#d8cdb5;letter-spacing:2px;text-transform:uppercase;margin-bottom:3px;}
.mh-ship-grid span{color:#f6f2ea;font-size:14px;}

/* ==== Standard aside sections ==== */
.mh-aside{padding:0 40px;margin-bottom:32px;}
.mh-aside .mh-box{padding:20px 24px;background:#fbf9f3;border:1px solid #d8cdb5;margin-bottom:14px;}
.mh-aside h3{font-family:'Cormorant Garamond','Times New Roman',serif;font-size:18px;font-weight:600;color:#1a1817;margin-bottom:8px;}
.mh-aside p{margin:0;font-size:13px;color:#3a332c;line-height:1.6;}

/* ==== Rank definitions table ==== */
.mh-rankdef{padding:22px 28px;background:#f6f2ea;border:1px solid #d8cdb5;}
.mh-rankdef table{width:100%;border-collapse:collapse;font-size:13px;}
.mh-rankdef td{padding:8px 10px;border-bottom:1px solid #d8cdb5;vertical-align:top;color:#3a332c;}
.mh-rankdef td:first-child{font-family:'Cormorant Garamond','Times New Roman',serif;font-weight:700;color:#a8341b;width:4em;font-size:15px;}

/* ==== About / Footer ==== */
.mh-about{text-align:center;padding:40px 40px 24px;border-top:1px solid #d8cdb5;}
.mh-about .mh-mark{font-family:'Cormorant Garamond','Times New Roman',serif;font-size:22px;font-weight:600;letter-spacing:6px;color:#1a1817;margin-bottom:4px;}
.mh-about .mh-mark-jp{font-family:'Cormorant Garamond','Times New Roman',serif;font-size:11px;letter-spacing:6px;color:#6b6157;margin-bottom:16px;}
.mh-about p{max-width:520px;margin:0 auto 10px;font-size:13px;color:#6b6157;line-height:1.65;}
.mh-about .mh-contact{font-family:'JetBrains Mono','Courier New',monospace;font-size:10px;letter-spacing:2px;color:#9a8f82;text-transform:uppercase;margin-top:16px;}

@media (max-width:640px){
  .mh-mast{padding:32px 20px 24px;}
  .mh-mast h1{font-size:26px;}
  .mh-hanko{flex-direction:column;text-align:center;margin:24px 20px;padding:22px;}
  .mh-rank,.mh-ship{margin-left:20px;margin-right:20px;}
  .mh-sec,.mh-aside,.mh-about{padding-left:20px;padding-right:20px;}
  .mh-inc-grid{grid-template-columns:1fr;}
  .mh-ship-grid{grid-template-columns:1fr;}
  .mh-specs td:first-child{width:110px;}
}
</style>

<div class="mh-wrap {{mode_class}}">

  <!-- Masthead -->
  <div class="mh-mast">
    <div class="mh-origin">S H I P P E D  F R O M  J A P A N</div>
    <h1>{{product_name}}</h1>
    <p class="mh-sub">{{product_sub}}</p>
  </div>

  <!-- Hanko DDP Seal -->
  <div class="mh-hanko">
    <div class="mh-hanko-seal">
      <span class="mh-hs-t">C E R T</span>
      <span class="mh-hs-m">DDP</span>
      <span class="mh-hs-b">PREPAID</span>
    </div>
    <div class="mh-hanko-body">
      <h3>No surprise customs fees. Ever.</h3>
      <p>
        Since October 2025, the US eliminated the $800 de minimis exemption &mdash; most sellers now ship DDU, meaning buyers receive unexpected customs invoices after delivery.
        <br><br>
        <span class="mh-strong">MonoHonpo ships every US order DDP (Delivered Duty Paid).</span> Your listed price includes the item, international shipping, US customs duties, and import taxes. When the package arrives, you pay nothing more.
      </p>
    </div>
  </div>

  <!-- Optional Spec Strip (gadget mode) -->
  <div class="mh-spec-strip">
    {{spec_strip_rows}}
  </div>

  <!-- Condition Rank -->
  <div class="mh-rank">
    <div class="mh-kicker">C O N D I T I O N &nbsp; R A N K</div>
    <div class="mh-rank-brush">
      <svg viewBox="0 0 120 120" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M60 14 C 28 14, 14 38, 14 60 C 14 86, 36 106, 62 106 C 88 106, 106 86, 106 60 C 106 46, 98 30, 82 22"
              stroke="#1a1817" stroke-width="5" stroke-linecap="round" fill="none"/>
      </svg>
      <div class="mh-rb-letter">{{rank}}</div>
    </div>
    <h3>Rank {{rank}} &mdash; {{rank_label}}</h3>
    <div class="mh-rank-jp">{{rank_jp}}</div>
    <div class="mh-quick">{{quick_notes}}</div>
  </div>

  <!-- Includes -->
  <div class="mh-sec">
    <div class="mh-sec-head">
      <h2>In the box</h2>
      <div class="mh-jp">I N C L U D E D</div>
    </div>
    <div class="mh-inc-grid">
      {{includes_rows}}
    </div>
  </div>

  <!-- Specifications -->
  <div class="mh-sec">
    <div class="mh-sec-head">
      <h2>Specifications</h2>
      <div class="mh-jp">S P E C S</div>
    </div>
    <table class="mh-specs">
      {{specs_rows}}
    </table>
  </div>

  <!-- Shipping -->
  <div class="mh-ship">
    <h3>Shipping &amp; handling<span class="mh-jp">S H I P P I N G</span></h3>
    <div class="mh-ship-grid">
      <div><strong>Origin</strong><span>{{shipping_origin}}</span></div>
      <div><strong>Carrier</strong><span>{{shipping_carrier}}</span></div>
      <div><strong>Handling</strong><span>Ships within {{shipping_handling}}</span></div>
      <div><strong>Delivery (US)</strong><span>{{shipping_delivery_us}}</span></div>
      <div><strong>Packaging</strong><span>{{shipping_packaging}}</span></div>
      <div><strong>Duties</strong><span>DDP &mdash; US import duties prepaid by seller</span></div>
    </div>
  </div>

  <!-- Aside standard sections -->
  <div class="mh-aside">

    <!-- Rank definitions -->
    <div class="mh-rankdef">
      <h3 style="margin-bottom:12px;">Condition Rank Definitions</h3>
      <table>
        <tr><td>N</td><td>New (Unopened) &mdash; Brand new, factory sealed</td></tr>
        <tr><td>S</td><td>Like New &mdash; Opened but unused, no visible wear</td></tr>
        <tr><td>A</td><td>Excellent &mdash; Minor wear, tested and fully working</td></tr>
        <tr><td>B</td><td>Good &mdash; Visible use marks, tested and fully working</td></tr>
        <tr><td>C</td><td>Fair &mdash; Heavy use marks, tested and fully working</td></tr>
        <tr><td>D</td><td>Issues &mdash; Cosmetic or function issues, working with limits</td></tr>
        <tr><td>PO</td><td>Power-On Only &mdash; Powers on, full function not verified</td></tr>
        <tr><td>As-Is</td><td>As-Is &mdash; Not fully tested, sold without warranty</td></tr>
      </table>
    </div>

    <div class="mh-box">
      <h3>Payment</h3>
      <p>We accept eBay Managed Payments (credit cards, PayPal, Apple Pay, Google Pay). Import duties and taxes for US buyers are prepaid (DDP). International (non-US) buyers are responsible for their country's import duties. To cancel after purchase, please contact us immediately.</p>
    </div>

    <div class="mh-box">
      <h3>Voltage notice</h3>
      <p>Some electronics from Japan are designed for AC 100V. For use in regions with 200&ndash;240V, a step-down transformer may be required. Please confirm voltage compatibility before purchase. Damage caused by incorrect voltage is not covered by warranty.</p>
    </div>

    <div class="mh-box">
      <h3>Physical store notice</h3>
      <p>This item may also be listed in our physical workshop in Tokyo. In rare cases it may sell in-store before an online order is processed. If this occurs we will notify you immediately and issue a full refund.</p>
    </div>

    <div class="mh-box" style="border-left:3px solid #a8341b;">
      <h3>If you need anything</h3>
      <p>Questions before purchase are always welcome &mdash; messages answered within 24 hours on Tokyo business days. If something about the item doesn't match the description on arrival, contact us first: we cover return shipping on misdescribed items.{{shipping_notes}}</p>
    </div>

  </div>

  <!-- About MonoHonpo -->
  <div class="mh-about">
    <div class="mh-mark">M O N O H O N P O</div>
    <div class="mh-mark-jp">M O N O &nbsp; H O N P O</div>
    <p>One workbench in Tokyo. Every item sits on the bench for a full session before it is photographed, graded, and packed. If it doesn't test clean, it doesn't list.</p>
    <p>We specialize in industrial sensors &amp; test gear, vintage Japanese audio, and hand-selected specialist tools. US buyers get DDP on every order.</p>
    <div class="mh-contact">eBay Messages &middot; Response &lt; 24h &middot; Tokyo JST</div>
  </div>

</div>
```

---

## Phase 3 listing_generator.py 実装ヒント

### プレースホルダ置換
```python
import re

def render_description(template: str, values: dict[str, str]) -> str:
    """Replace {{placeholder}} markers. CSS braces {..} survive untouched."""
    def _replace(match: re.Match) -> str:
        key = match.group(1)
        return values.get(key, "")  # missing key → empty string
    return re.sub(r"\{\{(\w+)\}\}", _replace, template)
```

### includes_rows 生成
```python
def build_includes_rows(items: list[dict]) -> str:
    """items = [{'label': 'Headphones', 'detail': 'Sony WH-1000XM5'}, ...]"""
    return "\n".join(
        f'<div class="mh-inc"><strong>{escape(it["label"])}</strong>{escape(it["detail"])}</div>'
        for it in items
    )
```

### specs_rows 生成
```python
def build_specs_rows(specs: list[tuple[str, str]]) -> str:
    return "\n".join(
        f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>" for k, v in specs
    )
```

### spec_strip_rows 生成（gadget mode のみ）
```python
def build_spec_strip(trio: list[tuple[str, str]]) -> str:
    """最大3項目。Gadget 以外は空文字で空カラム扱い。"""
    return "".join(
        f'<div><div class="k">{escape(k)}</div><div class="v">{escape(v)}</div></div>'
        for k, v in trio[:3]
    )
```

### mode_class 決定ロジック
```python
GADGET_CATEGORIES = {"293", "625", "12576"}  # Consumer Electronics, Cameras, Business & Industrial
GADGET_BRANDS = {"KEYENCE", "Omron", "Mitsubishi", "Hitachi",
                 "Nikon", "Canon", "Sony", "Panasonic", "Zoom", "Roland"}

def detect_mode(category_id: str, brand: str, specs_count: int) -> str:
    cat_root = category_id.split("/")[0] if category_id else ""
    if cat_root in GADGET_CATEGORIES:
        return "gadget"
    if brand and brand.strip() in GADGET_BRANDS:
        return "gadget"
    if specs_count >= 3:
        return "gadget"
    return "default"
```

### HTML escape
```python
from xml.sax.saxutils import escape
# 全ての動的値は escape() 通すこと
```

---

## プレビュー確認

`design/listing-template-v4-preview.html` に Sony WH-1000XM5 のサンプルデータを埋めた HTML あり。ブラウザで開けば最終見た目が確認できる。
