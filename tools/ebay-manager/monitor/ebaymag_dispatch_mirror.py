"""US本体listingのDispatchTimeMaxをeBaymag各国版MAG割当へミラーする (money-direct)。

設計書: .company/engineering/docs/2026-06-26-ebaymag-us-dispatch-mirror-design.md
Codex/Fugu 2段レビュー反映済 (§6)。

真実の源 = US本体listing (siteId=0, user=seller) の DispatchTimeMax。
各商品が既に正しい重量帯の MAG_<band>_<series> に居る前提で、series を US に合わせて
同帯 twin へ付け替える (重量帯不変・送料値不変。値は band で決まるため)。

3層 (制御境界):
  1. US eBay listing  = 真実の源 (user 手編集)        ← 入力
  2. eBaymag MAGプロファイル/割当 = GraphQL/REST 制御可 ← 本module が触れる層
  3. eBay各国版 listing = 買い手が見る              ← eBaymag停滞sync依存・保証しない

money-direct ガード:
  - 付替先 twin の各国送料が canonical 期待値で完備しているサイトのみ移動 (未完備=hold)。
  - dispatch軸 (各MAGの profile.dispatchTime) と series ラベルの整合を起動時検証。
  - assign は ebaymag_assign.assign_product (REST PUT + read-back + assert_no_vanish)。
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

from monitor import ebaymag_assign as A
from monitor import ebaymag_graphql as G
from monitor.ebaymag_assign import SITE, TAB_SITES
from monitor.ebaymag_policy_mapping import build_canonical_policy
from monitor.ebay_client import _call_trading_api

_NS = {"ns": "urn:ebay:apis:eBLBaseComponents"}
_MAG_RE = re.compile(r"^MAG_(?P<band>.+)_(?P<series>1day|7day)$")
_ITEMID_RE = re.compile(r"/(\d{9,})(?:\D|$)")
# set_values が課金する各国 (US=0 は本体課金で $0 固定なので除外)
SITE_NON_US = frozenset(TAB_SITES["Europe"] + TAB_SITES["Australia"] + TAB_SITES["Canada"])
_SERIES_DISPATCH = {"1day": 1, "7day": 7}


class MirrorError(RuntimeError):
    """ミラー処理の構造的異常 (即停止すべき)。"""


# ---------- title / sku parse ----------

def parse_mag(title: str):
    """MAG_<band>_<series> を (band, series) に。MAG以外 / 想定外サフィックスは None。"""
    m = _MAG_RE.match(title or "")
    if not m:
        return None
    return m.group("band"), m.group("series")


def sku_expected_series(sku: str | None):
    """在庫業務ルール: stock*→1day, ebay*→7day。判定不能は None。

    ⚠️ ミラーの真実源には使わない (真実源=US)。SKU矛盾の監査専用 (Fugu MED-6)。
    """
    if not sku:
        return None
    if sku.startswith("stock"):
        return "1day"
    if sku.startswith("ebay"):
        return "7day"
    return None


def _item_id(url: str | None):
    m = _ITEMID_RE.search(url or "")
    return m.group(1) if m else None


# ---------- twin index ----------

def build_twin_index(profs: list[dict]) -> dict:
    """policy list から {policy_id: {band, series, title, twin_id}} を生成。

    series suffix は {1day,7day} 完全一致のみ受理 (Codex M1)。両 twin 欠落帯は明示エラー。
    """
    by_bs: dict[tuple[str, str], str] = {}
    mag: dict[str, dict] = {}
    for x in profs:
        title = x.get("title") or ""
        parsed = parse_mag(title)
        if not parsed:
            # "MAG_" 始まりなのに {1day,7day} でパース不能 = 想定外サフィックスを
            # 黙って無視しない (Q0 silent skip 防止、Codex M1)。
            if title.startswith("MAG_"):
                raise MirrorError(
                    f"MAG ポリシーだが series suffix が {{1day,7day}} でない: {title!r}")
            continue
        band, series = parsed
        by_bs[(band, series)] = str(x["id"])
        mag[str(x["id"])] = {"band": band, "series": series, "title": x["title"]}
    idx: dict[str, dict] = {}
    for pid, info in mag.items():
        other = "1day" if info["series"] == "7day" else "7day"
        twin = by_bs.get((info["band"], other))
        if twin is None:
            raise MirrorError(
                f"band={info['band']} の {other} twin が無い (title={info['title']})")
        idx[pid] = {**info, "twin_id": twin}
    return idx


def assert_dispatch_axis(page, twin_index: dict) -> None:
    """各 MAG の profile.dispatchTime 実値が series ラベルと整合するか検証 (Codex H1)。

    dispatch は title ラベルでなく dispatchTime フィールドに宿るため、flip 後に
    各国版 dispatch が変わらず G1 がサイレント未達になるのを防ぐ。
    """
    for pid, info in twin_index.items():
        pr = G.read_profile(page, pid)
        dt = pr.get("dispatchTime")
        want = _SERIES_DISPATCH[info["series"]]
        if dt != want:
            raise MirrorError(
                f"{info['title']} の dispatchTime={dt} が series '{info['series']}' "
                f"期待 {want} と不整合 (W284 でラベルと中身が乖離している可能性)")


# ---------- US 真実源 (GetItem) ----------

def us_info(item_id: str, creds) -> tuple[str | None, str | None, str | None]:
    """US本体listing の (series, sku, error)。series は '1day'/'7day'、想定外dtmはNone+理由。"""
    app, dev, cert, tok = creds
    xb = ('<?xml version="1.0"?><GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
          '<RequesterCredentials><eBayAuthToken>{USER_TOKEN}</eBayAuthToken>'
          '</RequesterCredentials>'
          f'<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel></GetItemRequest>')
    r = _call_trading_api("GetItem", xb, app, dev, cert, tok)
    if not r.get("success"):
        return None, None, f"GetItem失敗: {str(r.get('error') or r.get('errors'))[:80]}"
    root = ET.fromstring(r["raw"])
    it = root.find("ns:Item", _NS)
    if it is None:
        return None, None, "GetItem: Item要素なし"
    dtm = it.find("ns:DispatchTimeMax", _NS)
    sku = it.find("ns:SKU", _NS)
    dtm_val = dtm.text if dtm is not None else None
    sku_val = sku.text if sku is not None else None
    if dtm_val == "1":
        return "1day", sku_val, None
    if dtm_val == "7":
        return "7day", sku_val, None
    return None, sku_val, f"DispatchTimeMax={dtm_val} (想定外、要user判断)"


# ---------- twin 値完備チェック (money-direct ガード) ----------

def _domestic_cost(ep: dict) -> tuple:
    """(value, currency)。DOMESTIC service の国内送料。欠落は (None, None)。"""
    for so in (ep.get("payload") or {}).get("shippingOptions") or []:
        if so.get("optionType") == "DOMESTIC":
            for s in so.get("shippingServices") or []:
                cv = s.get("shippingCost")
                if isinstance(cv, dict):
                    return cv.get("value"), cv.get("currency")
                return cv, None
    return None, None


def twin_value_complete(page, twin_id: str, band: str, fx: dict) -> tuple[bool, str | None]:
    """付替先 twin の各国送料が canonical 期待値で完備か (Fugu H2)。

    全 7 非US サイトの profile が生成済 かつ 各サイトの国内送料 == ceil(canonical_usd * fx)
    かつ currency も一致 のとき完備 (set_values read-back と対称、code-reviewer MED)。
    1サイトでも未生成/不一致/$0/通貨違い なら未完備 (= leak リスク、hold)。
    """
    canon = build_canonical_policy(band)["tab_values"]  # {US,Europe,Australia,Canada}
    site_usd: dict[int, int] = {}
    for tab, sids in TAB_SITES.items():
        for sid in sids:
            site_usd[sid] = canon[tab]

    pr = G.read_profile(page, twin_id)
    present = {ep["siteId"] for ep in pr["shippingEbayProfiles"]} & SITE_NON_US
    missing = SITE_NON_US - present
    if missing:
        return False, f"未生成サイト {sorted(SITE.get(s, s) for s in missing)}"

    for ep in pr["shippingEbayProfiles"]:
        sid = ep["siteId"]
        if sid not in SITE_NON_US:
            continue
        _cc, cur, _country = SITE[sid]
        expect = math.ceil(site_usd[sid] * fx[cur])
        got_val, got_cur = _domestic_cost(ep)
        if got_val != expect or (got_cur is not None and got_cur != cur):
            return False, f"{SITE[sid][0]} 実{got_val}{got_cur or ''}≠canonical期待{expect}{cur}"
    return True, None


# ---------- plan / apply ----------

def list_mag_products(page) -> list[dict]:
    """全 MAG商品の {product_id, policy_id, us_item_id} を返す。"""
    q = ('query P($f:Int){products(first:$f){nodes{id shippingProfileId '
         'listings{site{id} publicationUrl}}}}')
    d = G.gql(page, "P", q, {"f": 300})
    return (d.get("products") or {}).get("nodes") or []


def plan_mirror(page, creds, fx, twin_index: dict) -> dict:
    """US dispatch にミラーする plan を生成。

    Returns: {"moves":[...], "holds":[...], "skips":[...], "sku_conflicts":[...]}
      moves: 値完備 twin への付替候補 (apply 対象)
      holds: 系列はズレるが twin 値未完備で保留 (leak回避)
      skips: US dispatch 取得不能 (要user判断)
      sku_conflicts: US series が SKU業務ルールと矛盾 (ミラーは実行、通知のみ)
    """
    moves, holds, skips, conflicts = [], [], [], []
    for x in list_mag_products(page):
        pid = str(x.get("shippingProfileId") or "").split(":")[-1]
        info = twin_index.get(pid)
        if not info:
            continue  # MAG以外 (twin_index は MAG のみ)
        us_item = None
        for li in x.get("listings") or []:
            if str((li.get("site") or {}).get("id")) == "0":
                us_item = _item_id(li.get("publicationUrl"))
                break
        product_id = str(x["id"])
        if not us_item:
            skips.append({"product_id": product_id, "title": info["title"],
                          "reason": "US本体listing無し"})
            continue
        us_series, sku, err = us_info(us_item, creds)
        if us_series is None:
            skips.append({"product_id": product_id, "title": info["title"],
                          "us_item": us_item, "reason": err})
            continue
        # SKU矛盾監査 (ミラーの判定には使わない)
        exp = sku_expected_series(sku)
        if exp is not None and exp != us_series:
            conflicts.append({"product_id": product_id, "us_item": us_item, "sku": sku,
                              "us_series": us_series, "sku_rule": exp,
                              "title": info["title"]})
        if us_series == info["series"]:
            continue  # 既に一致
        # 系列ズレ → 同帯 twin へ
        twin_id = info["twin_id"]
        complete, reason = twin_value_complete(page, twin_id, info["band"], fx)
        rec = {"product_id": product_id, "us_item": us_item, "band": info["band"],
               "from_policy": pid, "from_title": info["title"],
               "to_policy": twin_id, "to_series": us_series}
        if complete:
            moves.append(rec)
        else:
            rec["hold_reason"] = reason
            holds.append(rec)
    return {"moves": moves, "holds": holds, "skips": skips, "sku_conflicts": conflicts}


def apply_moves(page, moves: list[dict]) -> dict:
    """moves を assign_product で実行。1件失敗で即停止 (money-direct)。"""
    done, failed = [], None
    for mv in moves:
        try:
            A.assign_product(page, mv["product_id"], mv["to_policy"])
            done.append(mv)
        except Exception as e:  # noqa: BLE001 — money-direct: 失敗は即停止して報告
            failed = {"move": mv, "error": str(e)[:200]}
            break
    return {"done": done, "failed": failed}
