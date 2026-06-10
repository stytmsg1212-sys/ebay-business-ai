"""description='test' 16 件の修復 one-shot (2026-06-11 user 承認済).

損傷: Description / ConditionDescription とも 'test'、Brand/MPN が欠落 or 'NA'。
      (注: 当初「ItemSpecifics ゼロ」と判定したのは GetItem に
       IncludeItemSpecifics=true を付けない読取りアーティファクト。実際は
       既存 specifics が存在するため、replace-all の ReviseItem で消さないよう
       既存を取得してマージする。既存値は保持、Brand/MPN のみ実値で上書き。)
修復: MonoHonpo v4 系テンプレで description 再生成 + ConditionDescription +
      ItemSpecifics (既存マージ + Brand/MPN 上書き) を 1 回の ReviseItem で反映。

方針 (正直 description 原則):
- 状態主張はタイトル由来のみ (working pull / as-is / calibrated / tested)。
  タイトルに主張が無い品は「Pre-owned · 写真参照」に留め、ランク章は使わない
  (動作確認情報が無いのに Rank B 等を名乗ると defect リスク)。
- ConditionID は変更しない (3000 Used 維持、K2 surgical)。

usage:
  python fix_testdesc16_2026_06_11.py preview <item_id>   # HTML を data/ に書出し (mutation なし)
  python fix_testdesc16_2026_06_11.py apply <item_id>     # 1 件反映
  python fix_testdesc16_2026_06_11.py apply-all           # ログで未完了の全件反映
  python fix_testdesc16_2026_06_11.py verify              # GetItem で全 16 件検証
"""
import json
import sys
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from monitor.inventory_sync import _get_credentials
from monitor.ebay_client import _call_trading_api

LOG_PATH = r"data\testdesc16_fix_log_2026_06_11.json"

# 状態主張の種別 → (condition 見出し, JP ヒント, quick notes, ConditionDescription<=65 字)
CLAIMS = {
    "working_pull": (
        "Used &mdash; Working Pull",
        "Removed from working installation",
        "Removed from a working installation (working pull). Condition is as "
        "pictured &mdash; please review the photos carefully and message us "
        "with any questions before purchase.",
        "Working pull from a live installation. See photos for details.",
    ),
    "as_is": (
        "As-Is",
        "Untested · Sold as pictured",
        "Sold as-is, untested &mdash; priced accordingly. Please review the "
        "photos carefully; only what is pictured is included.",
        "Sold as-is, untested. See photos; pictured items only.",
    ),
    "calibrated": (
        "Used &mdash; Calibrated",
        "Calibrated · See title",
        "Calibrated unit (see title for reference). Condition is as pictured "
        "&mdash; message us for calibration details before purchase.",
        "Calibrated unit. See photos; pictured items only.",
    ),
    "tested": (
        "Used &mdash; Tested",
        "Tested · See notes",
        "Tested (see title). Condition is as pictured &mdash; please review "
        "the photos carefully and message us for test details.",
        "Tested. See photos; pictured items only.",
    ),
    "none": (
        "Used",
        "Pre-owned · See photos",
        "Pre-owned unit in the condition shown in the photos &mdash; please "
        "review them carefully. We are happy to provide additional photos or "
        "details on request.",
        "Pre-owned. See photos; pictured items only.",
    ),
}

# 16 件の修復データ (全て タイトル由来の事実のみ)
ITEMS = [
    dict(item_id="358027482174", brand="KEYENCE", mpn="MK-P4", claim="working_pull",
         name="KEYENCE MK-P4 Touch Panel Console",
         sub="Industrial touch panel console · Working pull",
         specs=[("Type", "Touch panel console")]),
    dict(item_id="358042514439", brand="GE Panametrics", mpn="PT878", claim="as_is",
         name="GE Panametrics TransPort PT878",
         sub="Portable ultrasonic liquid flow meter · Sold as-is",
         specs=[("Type", "Portable ultrasonic liquid flow meter")]),
    dict(item_id="358046641356", brand="HACH", mpn="DR3900", claim="none",
         name="HACH DR3900 VIS Spectrophotometer",
         sub="Water analysis lab instrument",
         specs=[("Type", "VIS spectrophotometer (water analysis)")]),
    dict(item_id="358062291688", brand="Thermo Scientific", mpn="RadEye B20-ER", claim="none",
         name="Thermo Scientific RadEye B20-ER",
         sub="Radiation survey meter",
         specs=[("Type", "Radiation survey meter")]),
    dict(item_id="358120580440", brand="Anritsu", mpn="S331E", claim="calibrated",
         name="Anritsu Site Master S331E",
         sub="Compact cable &amp; antenna analyzer · Calibrated",
         specs=[("Type", "Cable & antenna analyzer")]),
    dict(item_id="358120654421", brand="Testo", mpn="350", claim="tested",
         name="Testo 350 Emission Analyzer Kit",
         sub="Control unit + analyzer box + software",
         specs=[("Type", "Emission analyzer kit")],
         includes=[("Control unit", "Testo 350"),
                   ("Analyzer box", "As pictured"),
                   ("Software", "2022 (per title)")]),
    dict(item_id="358147979341", brand="Hitachi", mpn="L700-185LFF-0R", claim="working_pull",
         name="HITACHI L700-185LFF-0R Inverter Drive",
         sub="Removed from a working machine",
         specs=[("Type", "Inverter drive")]),
    dict(item_id="358158853598", brand="AKAI", mpn="RC-21", claim="none",
         name="AKAI RC-21 Wired Remote Control",
         sub="For reel-to-reel tape recorders · With box",
         specs=[("Type", "Wired remote control (reel-to-reel)")],
         includes=[("Remote control", "AKAI RC-21"),
                   ("Original box", "As pictured")]),
    dict(item_id="358166322333", brand="Thorlabs", mpn="MDT694", claim="none",
         name="Thorlabs MDT694 Piezo Controller",
         sub="High-voltage PZT driver · 0&ndash;150 V",
         specs=[("Type", "High-voltage piezo controller"),
                ("Output", "0-150 V")]),
    dict(item_id="358207286305", brand="Tektronix", mpn="TDS3034B", claim="none",
         name="Tektronix TDS 3034B Oscilloscope",
         sub="4-channel color digital phosphor · 300 MHz",
         specs=[("Type", "Digital phosphor oscilloscope"),
                ("Bandwidth", "300 MHz"), ("Channels", "4")]),
    dict(item_id="358223832012", brand="Agilent", mpn="N2795A", claim="none",
         name="Agilent N2795A Active Probe",
         sub="Single-ended · DC&ndash;1 GHz · AutoProbe interface",
         specs=[("Type", "Single-ended active probe"),
                ("Bandwidth", "DC-1 GHz")]),
    dict(item_id="358244264123", brand="Mitutoyo", mpn="SJ-410", claim="none",
         name="Mitutoyo SURFTEST SJ-410",
         sub="Surface roughness tester",
         specs=[("Type", "Surface roughness tester")]),
    dict(item_id="358274785765", brand="Kowa", mpn="TSN-1 77-P", claim="none",
         name="KOWA TSN-1 77-P Spotting Scope",
         sub="With 20x / 40x / 60x eyepieces",
         specs=[("Type", "Spotting scope"),
                ("Magnification", "20x / 40x / 60x (eyepieces included)")],
         includes=[("Spotting scope", "KOWA TSN-1 77-P"),
                   ("Eyepieces", "20x / 40x / 60x")]),
    dict(item_id="358334960391", brand="AMETEK JOFRA", mpn="ITC-650A", claim="calibrated",
         name="AMETEK JOFRA ITC-650A",
         sub="Temperature calibrator · Calibrated 2024",
         specs=[("Type", "Temperature calibrator"),
                ("Calibration", "2024 (per title)")]),
    dict(item_id="358335153622", brand="RION", mpn="NA-28", claim="none",
         name="RION NA-28 Sound Level Meter",
         sub="Precision sound level meter",
         specs=[("Type", "Precision sound level meter")]),
    dict(item_id="358403831980", brand="AMADA", mpn="DIGIPRO", claim="tested",
         name="AMADA DIGIPRO Digital Protractor",
         sub="Bending angle measuring machine · Tested · With case",
         specs=[("Type", "Digital protractor (bending angle)")],
         includes=[("Digital protractor", "AMADA DIGIPRO"),
                   ("Carrying case", "As pictured")]),
]

CSS = """
.mh-wrap{background:#f6f2ea;color:#1a1817;font-family:'Inter','Helvetica Neue',Arial,sans-serif;font-size:15px;line-height:1.6;max-width:860px;margin:0 auto;padding:0;}
.mh-wrap *{box-sizing:border-box;}
.mh-wrap h1,.mh-wrap h2,.mh-wrap h3{font-family:'Cormorant Garamond','Times New Roman',serif;color:#1a1817;margin:0;}
.mh-mast{text-align:center;padding:44px 40px 32px;border-bottom:1px solid #d8cdb5;background:#fbf9f3;}
.mh-mast .mh-origin{font-family:'Cormorant Garamond',serif;font-size:11px;letter-spacing:8px;color:#a8341b;margin-bottom:14px;font-weight:500;}
.mh-mast h1{font-size:34px;font-weight:500;line-height:1.2;margin-bottom:10px;}
.mh-mast .mh-sub{font-family:'Cormorant Garamond',serif;font-style:italic;font-size:17px;color:#6b6157;margin:0;}
.mh-hanko{display:flex;gap:24px;align-items:center;padding:26px 32px;background:#fbf9f3;border:1px solid #d8cdb5;border-left:4px solid #a8341b;margin:32px 40px;}
.mh-hanko-seal{width:104px;height:104px;border-radius:50%;background:#a8341b;color:#fbf9f3;display:flex;flex-direction:column;align-items:center;justify-content:center;flex-shrink:0;box-shadow:inset 0 0 0 2px #fbf9f3,0 0 0 3px #a8341b;transform:rotate(-6deg);font-family:'Cormorant Garamond',serif;}
.mh-hanko-seal .mh-hs-t{font-size:10px;letter-spacing:3px;font-weight:500;}
.mh-hanko-seal .mh-hs-m{font-size:22px;font-weight:700;line-height:1;margin:3px 0;letter-spacing:1px;}
.mh-hanko-seal .mh-hs-b{font-family:'JetBrains Mono',monospace;font-size:8px;letter-spacing:2px;margin-top:2px;}
.mh-hanko-body h3{font-size:22px;font-weight:600;margin-bottom:8px;}
.mh-hanko-body p{margin:0;color:#3a332c;font-size:14px;line-height:1.55;}
.mh-hanko-body .mh-strong{color:#a8341b;font-weight:600;}
.mh-spec-strip{display:grid;grid-template-columns:1fr 1fr 1fr;margin:0 40px 32px;border-top:1px solid #d8cdb5;border-bottom:1px solid #d8cdb5;background:#fbf9f3;}
.mh-spec-strip > div{padding:16px 0;text-align:center;border-right:1px solid #d8cdb5;}
.mh-spec-strip > div:last-child{border-right:0;}
.mh-spec-strip .k{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:2px;color:#9a8f82;text-transform:uppercase;margin-bottom:4px;}
.mh-spec-strip .v{font-family:'Cormorant Garamond',serif;font-size:16px;font-weight:600;}
.mh-rank{padding:32px;background:#fbf9f3;border:1px solid #d8cdb5;margin:0 40px 32px;text-align:center;}
.mh-rank .mh-kicker{font-family:'Cormorant Garamond',serif;font-size:11px;letter-spacing:6px;color:#6b6157;margin-bottom:14px;text-transform:uppercase;}
.mh-rank h3{font-size:28px;font-weight:500;margin-bottom:6px;}
.mh-rank .mh-rank-jp{font-family:'Cormorant Garamond',serif;font-size:13px;color:#6b6157;margin-bottom:16px;letter-spacing:3px;}
.mh-rank .mh-quick{max-width:520px;margin:0 auto;padding:16px 20px;background:#f6f2ea;border-left:2px solid #1a1817;text-align:left;font-family:'Cormorant Garamond',serif;font-style:italic;font-size:16px;color:#3a332c;line-height:1.5;}
.mh-sec{padding:0 40px;margin-bottom:36px;}
.mh-sec-head{display:flex;align-items:baseline;gap:16px;margin-bottom:20px;padding-bottom:8px;border-bottom:1px solid #d8cdb5;}
.mh-sec-head h2{font-size:24px;font-weight:500;}
.mh-sec-head .mh-jp{font-size:11px;color:#6b6157;}
.mh-inc-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.mh-inc{padding:14px 16px 14px 36px;background:#fbf9f3;border:1px solid #d8cdb5;position:relative;font-size:14px;color:#3a332c;}
.mh-inc:before{content:'';position:absolute;left:14px;top:18px;width:10px;height:10px;border:1.5px solid #a8341b;border-radius:50%;}
.mh-inc strong{display:block;color:#1a1817;font-family:'Cormorant Garamond',serif;font-size:15px;font-weight:600;margin-bottom:2px;}
.mh-specs{width:100%;border-collapse:collapse;}
.mh-specs tr{border-bottom:1px solid #d8cdb5;}
.mh-specs tr:last-child{border-bottom:0;}
.mh-specs td{padding:14px 0;font-size:14px;vertical-align:top;}
.mh-specs td:first-child{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:2px;color:#6b6157;text-transform:uppercase;width:180px;padding-top:17px;}
.mh-specs td:last-child{font-family:'Cormorant Garamond',serif;font-size:17px;}
.mh-ship{padding:28px 32px;background:#1a1817;color:#f6f2ea;margin:0 40px 32px;}
.mh-ship h3{font-size:22px;color:#fbf9f3;margin-bottom:14px;font-weight:500;}
.mh-ship-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px 28px;}
.mh-ship-grid strong{display:block;font-family:'JetBrains Mono',monospace;font-size:9px;color:#d8cdb5;letter-spacing:2px;text-transform:uppercase;margin-bottom:3px;}
.mh-ship-grid span{color:#f6f2ea;font-size:14px;}
.mh-aside{padding:0 40px;margin-bottom:32px;}
.mh-aside .mh-box{padding:20px 24px;background:#fbf9f3;border:1px solid #d8cdb5;margin-bottom:14px;}
.mh-aside h3{font-size:18px;font-weight:600;margin-bottom:8px;}
.mh-aside p{margin:0;font-size:13px;color:#3a332c;line-height:1.6;}
.mh-about{text-align:center;padding:40px 40px 24px;border-top:1px solid #d8cdb5;}
.mh-about .mh-mark{font-family:'Cormorant Garamond',serif;font-size:22px;font-weight:600;letter-spacing:6px;margin-bottom:16px;}
.mh-about p{max-width:520px;margin:0 auto 10px;font-size:13px;color:#6b6157;line-height:1.65;}
.mh-about .mh-contact{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:2px;color:#9a8f82;text-transform:uppercase;margin-top:16px;}
@media (max-width:640px){.mh-mast{padding:32px 20px 24px;}.mh-mast h1{font-size:26px;}.mh-hanko{flex-direction:column;text-align:center;margin:24px 20px;padding:22px;}.mh-rank,.mh-ship,.mh-spec-strip{margin-left:20px;margin-right:20px;}.mh-sec,.mh-aside,.mh-about{padding-left:20px;padding-right:20px;}.mh-inc-grid,.mh-ship-grid{grid-template-columns:1fr;}.mh-specs td:first-child{width:110px;}}
"""


def build_html(it: dict) -> str:
    head, jp_hint, quick, _cd = CLAIMS[it["claim"]]
    spec_rows = "\n".join(
        f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>"
        for k, v in ([("Brand", it["brand"]), ("Model", it["mpn"])] + it["specs"])
    )
    strip = "".join(
        f'<div><div class="k">{k}</div><div class="v">{escape(v)}</div></div>'
        for k, v in [("Brand", it["brand"]), ("Model", it["mpn"]), ("Ships From", "Japan")]
    )
    includes = it.get("includes") or [("Main unit", "As pictured")]
    inc_rows = "\n".join(
        f'<div class="mh-inc"><strong>{escape(a)}</strong>{escape(b)}</div>'
        for a, b in includes
    ) + '\n<div class="mh-inc"><strong>Note</strong>Only the items shown in the photos are included</div>'
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500;600;700&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
<div class="mh-wrap gadget">
  <div class="mh-mast">
    <div class="mh-origin">S H I P P E D&nbsp;&nbsp;F R O M&nbsp;&nbsp;J A P A N</div>
    <h1>{escape(it["name"])}</h1>
    <p class="mh-sub">{it["sub"]}</p>
  </div>
  <div class="mh-hanko">
    <div class="mh-hanko-seal"><span class="mh-hs-t">C E R T</span><span class="mh-hs-m">DDP</span><span class="mh-hs-b">PREPAID</span></div>
    <div class="mh-hanko-body">
      <h3>No surprise customs fees. Ever.</h3>
      <p>Since October 2025, the US eliminated the $800 de minimis exemption &mdash; most sellers now ship DDU, meaning buyers receive unexpected customs invoices after delivery.<br><br>
      <span class="mh-strong">MonoHonpo ships every US order DDP (Delivered Duty Paid).</span> Your listed price includes the item, international shipping, US customs duties, and import taxes. When the package arrives, you pay nothing more.</p>
    </div>
  </div>
  <div class="mh-spec-strip">{strip}</div>
  <div class="mh-rank">
    <div class="mh-kicker">C O N D I T I O N</div>
    <h3>{head}</h3>
    <div class="mh-rank-jp">{jp_hint}</div>
    <div class="mh-quick">{quick}</div>
  </div>
  <div class="mh-sec">
    <div class="mh-sec-head"><h2>In the box</h2><div class="mh-jp">I N C L U D E D</div></div>
    <div class="mh-inc-grid">{inc_rows}</div>
  </div>
  <div class="mh-sec">
    <div class="mh-sec-head"><h2>Specifications</h2><div class="mh-jp">S P E C S</div></div>
    <table class="mh-specs">{spec_rows}</table>
  </div>
  <div class="mh-ship">
    <h3>Shipping &amp; handling</h3>
    <div class="mh-ship-grid">
      <div><strong>Origin</strong><span>Tokyo, Japan</span></div>
      <div><strong>Carrier</strong><span>FedEx / DHL International &middot; tracked, insured</span></div>
      <div><strong>Handling</strong><span>Ships within 1&ndash;3 business days</span></div>
      <div><strong>Delivery (US)</strong><span>6&ndash;10 business days typical</span></div>
      <div><strong>Packaging</strong><span>Double-boxed &middot; bubble-wrapped</span></div>
      <div><strong>Duties</strong><span>DDP &mdash; US import duties prepaid by seller</span></div>
    </div>
  </div>
  <div class="mh-aside">
    <div class="mh-box"><h3>Payment</h3><p>We accept eBay Managed Payments (credit cards, PayPal, Apple Pay, Google Pay). Import duties and taxes for US buyers are prepaid (DDP). International (non-US) buyers are responsible for their country's import duties. To cancel after purchase, please contact us immediately.</p></div>
    <div class="mh-box"><h3>Voltage notice</h3><p>Some electronics from Japan are designed for AC 100V. For use in regions with 200&ndash;240V, a step-down transformer may be required. Please confirm voltage compatibility before purchase. Damage caused by incorrect voltage is not covered by warranty.</p></div>
    <div class="mh-box"><h3>Physical store notice</h3><p>This item may also be listed in our physical workshop in Tokyo. In rare cases it may sell in-store before an online order is processed. If this occurs we will notify you immediately and issue a full refund.</p></div>
    <div class="mh-box" style="border-left:3px solid #a8341b;"><h3>If you need anything</h3><p>Questions before purchase are always welcome &mdash; messages answered within 24 hours on Tokyo business days. If something about the item doesn't match the description on arrival, contact us first: we cover return shipping on misdescribed items.</p></div>
  </div>
  <div class="mh-about">
    <div class="mh-mark">M O N O H O N P O</div>
    <p>One workbench in Tokyo. Every item sits on the bench for a full session before it is photographed, graded, and packed.</p>
    <p>We specialize in industrial sensors &amp; test gear, vintage Japanese audio, and hand-selected specialist tools. US buyers get DDP on every order.</p>
    <div class="mh-contact">eBay Messages &middot; Response &lt; 24h &middot; Tokyo JST</div>
  </div>
</div>"""


def fetch_existing_specifics(item_id: str, creds) -> list:
    """GetItem (IncludeItemSpecifics=true) で既存 ItemSpecifics を取得。

    ReviseItem の ItemSpecifics は replace-all のため、既存値を消さないよう
    マージ素材として必須。戻り値: [(name, [values]), ...] (出現順)。
    """
    app_id, dev_id, cert_id, user_token = creds
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken></RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <IncludeItemSpecifics>true</IncludeItemSpecifics>
</GetItemRequest>"""
    res = _call_trading_api("GetItem", xml_body, app_id, dev_id, cert_id, user_token)
    if not res.get("success"):
        raise RuntimeError(f"GetItem 失敗 ({item_id}): {res.get('message')}")
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    root = ET.fromstring(res["raw"])
    item = root.find("ns:Item", ns)
    out = []
    for nvl in item.findall("ns:ItemSpecifics/ns:NameValueList", ns):
        name = (nvl.findtext("ns:Name", namespaces=ns) or "").strip()
        vals = [(v.text or "").strip() for v in nvl.findall("ns:Value", ns)]
        if name:
            out.append((name, vals))
    return out


def build_revise_xml(it: dict, html: str, existing: list) -> str:
    _h, _j, _q, cond_desc = CLAIMS[it["claim"]]
    safe_html = html.replace("]]>", "]]]]><![CDATA[>")
    # マージ: 既存は出現順のまま保持、Brand/MPN のみ実値で上書き (無ければ追加)
    merged = []
    seen = set()
    overrides = {"Brand": [it["brand"]], "MPN": [it["mpn"]]}
    for name, vals in existing:
        merged.append((name, overrides.get(name, vals)))
        seen.add(name)
    for name, vals in overrides.items():
        if name not in seen:
            merged.append((name, vals))
    specifics = "".join(
        "\n      <NameValueList><Name>{}</Name>{}</NameValueList>".format(
            escape(n), "".join(f"<Value>{escape(v)}</Value>" for v in vs)
        )
        for n, vs in merged
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken></RequesterCredentials>
  <Item>
    <ItemID>{escape(it["item_id"])}</ItemID>
    <Description><![CDATA[{safe_html}]]></Description>
    <ConditionDescription>{escape(cond_desc)}</ConditionDescription>
    <ItemSpecifics>{specifics}
    </ItemSpecifics>
  </Item>
</ReviseItemRequest>"""


def _load_log() -> dict:
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_log(log: dict) -> None:
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)


def apply_one(it: dict, creds) -> dict:
    app_id, dev_id, cert_id, user_token = creds
    html = build_html(it)
    existing = fetch_existing_specifics(it["item_id"], creds)
    xml_body = build_revise_xml(it, html, existing)
    res = _call_trading_api("ReviseItem", xml_body, app_id, dev_id, cert_id, user_token)
    ok = bool(res.get("success"))
    warns = [w for w in (res.get("warnings") or []) if w]
    print(f"{it['item_id']} | {'OK' if ok else 'FAIL'} (ack={res.get('ack')}) "
          f"| desc={len(html)}字 | {it['name'][:40]}")
    if warns:
        print(f"    warnings: {'; '.join(warns)[:200]}")
    if not ok:
        print(f"    message: {res.get('message')}")
    return {"ok": ok, "ack": res.get("ack"), "message": res.get("message"),
            "warnings": warns, "desc_len": len(html)}


def cmd_verify(creds) -> None:
    app_id, dev_id, cert_id, user_token = creds
    ns = {"ns": "urn:ebay:apis:eBLBaseComponents"}
    n_fixed = 0
    for it in ITEMS:
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{{USER_TOKEN}}</eBayAuthToken></RequesterCredentials>
  <ItemID>{it['item_id']}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
  <IncludeItemSpecifics>true</IncludeItemSpecifics>
</GetItemRequest>"""
        res = _call_trading_api("GetItem", xml_body, app_id, dev_id, cert_id, user_token)
        if not res.get("success"):
            print(f"{it['item_id']} | GetItem FAIL: {res.get('message')}")
            continue
        root = ET.fromstring(res["raw"])
        item = root.find("ns:Item", ns)
        g = lambda p: (item.findtext(p, namespaces=ns) or "").strip()
        desc = g("ns:Description")
        cd = g("ns:ConditionDescription")
        sp = {(nvl.findtext("ns:Name", namespaces=ns) or ""):
              (nvl.findtext("ns:Value", namespaces=ns) or "")
              for nvl in item.findall("ns:ItemSpecifics/ns:NameValueList", ns)}
        brand_ok = sp.get("Brand", "").strip() not in ("", "NA")
        mpn_ok = sp.get("MPN", "").strip() not in ("", "NA")
        fixed = desc != "test" and len(desc) > 1000 and cd != "test" and brand_ok and mpn_ok
        n_fixed += fixed
        print(f"{it['item_id']} | {'FIXED' if fixed else 'NG   '} | desc={len(desc)}字 "
              f"| condDesc={cd[:42]!r} | Brand={sp.get('Brand','-')} MPN={sp.get('MPN','-')}")
    print(f"\n==== verify: {n_fixed}/16 FIXED ====")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "preview"
    by_id = {it["item_id"]: it for it in ITEMS}

    if cmd == "preview":
        iid = sys.argv[2] if len(sys.argv) > 2 else ITEMS[0]["item_id"]
        it = by_id[iid]
        html = build_html(it)
        out = rf"data\testdesc16_preview_{iid}.html"
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        _h, _j, _q, cd = CLAIMS[it["claim"]]
        print(f"preview 書出し: {out} ({len(html)} 字) / condDesc({len(cd)}字)={cd!r}")
        return

    creds = _get_credentials()
    if not creds:
        print("FAIL: creds 解決不可")
        sys.exit(1)

    if cmd == "apply":
        iid = sys.argv[2]
        log = _load_log()
        log[iid] = apply_one(by_id[iid], creds)
        _save_log(log)
    elif cmd == "apply-all":
        log = _load_log()
        todo = [it for it in ITEMS if not (log.get(it["item_id"]) or {}).get("ok")]
        print(f"対象 {len(todo)} 件 (完了済 {16 - len(todo)} 件 skip)")
        for it in todo:
            log[it["item_id"]] = apply_one(it, creds)
            _save_log(log)
            time.sleep(1.0)
        n_ok = sum(1 for v in log.values() if v.get("ok"))
        print(f"\n==== apply-all 完了: {n_ok}/16 ok (log: {LOG_PATH}) ====")
    elif cmd == "verify":
        cmd_verify(creds)
    else:
        print(f"unknown cmd: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
