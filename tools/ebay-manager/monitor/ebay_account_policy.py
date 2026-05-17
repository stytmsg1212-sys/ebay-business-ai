"""W138 (2026-05-17): eBay Sell Account API — shipping (fulfillment) policy 取得.

商品管理タブで現在の Business Policy (BP) 表示 / 変更 selectbox に使う
**read-only** client。`GET /sell/account/v1/fulfillment_policy?marketplace_id=EBAY_US`
で全 active fulfillment policy を取得し id→name 解決に使う。

設計根拠 (W138 設計書 / W137 prep 実機確認):
  - OAuth scope `sell.account` は ebay_oauth_refresh._SCOPES に既存 (追加 consent 不要)。
  - fulfillmentPolicyId は GetItem の SellerShippingProfile/ShippingProfileID と
    同一 ID 空間 (W137 prep で実証)。→ snapshot.shipping_profile_id を name 解決可。
  - Q0: 通信/認証/parse 失敗は raise せず ok=False + error。UI は selectbox を
    出さず「BP 一覧取得失敗」を明示 (不明を空成功と偽らない)。
  - client 自体は純関数 (キャッシュは UI 層 @st.cache_data でラップ、W134 流儀)。
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_ACCOUNT_API = (
    "https://api.ebay.com/sell/account/v1/fulfillment_policy"
    "?marketplace_id={mkt}"
)


@dataclass(frozen=True)
class ShippingPolicyInfo:
    policy_id: str
    name: str
    service_names: list[str] = field(default_factory=list)
    domestic_service_count: int = 0


@dataclass(frozen=True)
class ShippingPolicyList:
    policies: list[ShippingPolicyInfo]
    ok: bool
    error: Optional[str] = None

    def name_for(self, policy_id: Optional[str]) -> Optional[str]:
        """policy_id → name 解決 (見つからなければ None)."""
        if not policy_id:
            return None
        for p in self.policies:
            if p.policy_id == policy_id:
                return p.name
        return None


def _empty(err: str) -> ShippingPolicyList:
    return ShippingPolicyList(policies=[], ok=False, error=err)


def fetch_shipping_policies(
    config: dict,
    marketplace_id: str = "EBAY_US",
) -> ShippingPolicyList:
    """全 active fulfillment(shipping) policy を取得.

    ok=False の時 policies=[] + error。呼出側 (UI) は selectbox を出さず
    明示 (Q0: 不明を空成功と偽らない)。raise しない (UI クラッシュ防止)。
    """
    try:
        from monitor.ebay_oauth_refresh import get_valid_access_token
        token = get_valid_access_token()
    except (ImportError, OSError, ValueError, KeyError) as e:
        logger.warning(f"[bp] OAuth token 取得失敗: {e}")
        return _empty(f"OAuth token 取得失敗: {e}")
    if not token:
        return _empty("OAuth token が空 (sell.account scope 要確認)")

    url = _ACCOUNT_API.format(mkt=marketplace_id)
    req = urllib.request.Request(
        url, method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001  HTTPError/URLError/socket 等を一括 graceful
        logger.warning(f"[bp] Account API 通信エラー: {e}")
        return _empty(f"Account API 通信エラー: {e}")

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[bp] Account API JSON parse 失敗: {e}")
        return _empty(f"JSON parse 失敗: {e}")

    raw = data.get("fulfillmentPolicies")
    if not isinstance(raw, list):
        # eBay は warnings/errors を JSON で返すこともある
        msg = data.get("errors") or data.get("warnings") or "fulfillmentPolicies 不在"
        logger.warning(f"[bp] Account API 応答異常: {msg}")
        return _empty(f"応答異常: {msg}")

    policies: list[ShippingPolicyInfo] = []
    for p in raw:
        pid = str(p.get("fulfillmentPolicyId") or "").strip()
        if not pid:
            continue
        svc_names: list[str] = []
        dom_count = 0
        for opt in p.get("shippingOptions") or []:
            is_dom = (opt.get("optionType") == "DOMESTIC")
            for sv in opt.get("shippingServices") or []:
                code = sv.get("shippingServiceCode")
                if code:
                    svc_names.append(str(code))
                if is_dom:
                    dom_count += 1
        policies.append(ShippingPolicyInfo(
            policy_id=pid,
            name=str(p.get("name") or pid),
            service_names=svc_names,
            domestic_service_count=dom_count,
        ))

    if not policies:
        return _empty("active fulfillment policy が 0 件")
    # name 昇順 (Q-5 user 承認: name 昇順表示)
    policies.sort(key=lambda x: x.name.lower())
    return ShippingPolicyList(policies=policies, ok=True, error=None)
