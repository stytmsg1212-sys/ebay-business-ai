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
    # --- W142 追加 (末尾、default 付き = 既存生成箇所/テスト不変、frozen 後方互換) ---
    # combined-新BP override の <ShippingServicePriority> 整合に使う。
    # eBay 公式 (Sell Account API ShippingService): REST フィールドは `sortOrder`
    # (整数、domestic 範囲 1-4)。legacy XML Business Policies では `sortOrderId`。
    # 公式: ShippingServiceCostOverride.ShippingServicePriority は BP の matching
    # service の sortOrder と一致させる。sortOrder 未供給時は policy 内記載順
    # = 単一 domestic なら position 1 (W136 hardcode=1 が単一 domestic では
    # 偶然正しかった理由)。
    domestic_sort_order: Optional[int] = None        # 単一 domestic 時の解決済 priority
    domestic_service_codes: tuple[str, ...] = ()     # DOMESTIC code 列 (記載順保持)


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


def _as_int(v) -> Optional[int]:
    """REST sortOrder の型ゆれ (int / 数値 str / None) を吸収。失敗は None."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def resolve_domestic_priority(
    pol: ShippingPolicyInfo,
    target_service_code: Optional[str] = None,
) -> tuple[Optional[int], str]:
    """W142: combined-新BP override 用 <ShippingServicePriority> を解決.

    返り値 (priority|None, reason). priority=None は preflight abort 対象
    = combined を中止し既存 2 経路へ degrade 案内 (Q0: 無音失敗させない)。

    eBay 公式 (Sell Account API): ShippingServiceCostOverride.
    ShippingServicePriority は BP の matching service の sortOrder と一致
    させる。単一 domestic service なら sortOrder (無ければ記載順=1) を
    そのまま使う。複数 domestic は対象が一意でなく W136 系の無音失敗
    (Ack=Success だが override 黙殺 = DDP buffer 喪失 = Section 232
    数百ドル/件) を招くため初版は中止 (一意化には現 listing の service
    code を snapshot から取る拡張が要り K1 違反気味、3 回要望で別 W)。
    target_service_code は将来の複数 domestic 一意化用 (初版未使用)。
    """
    n = pol.domestic_service_count
    if n == 0:
        return None, "no-domestic-service"
    if n == 1:
        # domestic_sort_order は parse 時に「sortOrder int / 無ければ 1」で
        # 確定済 (公式の記載順ルール、単一 domestic は position 1)。
        prio = (pol.domestic_sort_order
                if pol.domestic_sort_order is not None else 1)
        # W142 Codex-R3 MEDIUM: eBay 公式 domestic ShippingServicePriority
        # は 1-4。0/負/範囲外を XML に出すと override 無音失敗で DDP buffer
        # 喪失。preflight 段階で abort (degrade) し money-changing revise を
        # 実行させない (silent-skip-prevention: 不正値で送信しない)。
        if not (1 <= prio <= 4):
            return None, f"invalid-sort-order:{prio}"
        return prio, "single-domestic"
    return None, "multi-domestic-ambiguous"


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
        dom_codes: list[str] = []
        dom_sort_first: Optional[int] = None  # 最初の DOMESTIC service の sortOrder
        for opt in p.get("shippingOptions") or []:
            is_dom = (opt.get("optionType") == "DOMESTIC")
            for sv in opt.get("shippingServices") or []:
                code = sv.get("shippingServiceCode")
                if code:
                    svc_names.append(str(code))
                if is_dom:
                    dom_count += 1
                    if code:
                        dom_codes.append(str(code))
                    if dom_count == 1:
                        # REST 公式名は `sortOrder`、legacy/test mock は
                        # `sortOrderId`。live raw 未捕捉のため両名に寛容
                        # (money-direct 防御、Q1 実機で実応答確定)。
                        dom_sort_first = _as_int(
                            sv.get("sortOrder", sv.get("sortOrderId"))
                        )
        # 単一 domestic: sortOrder int があればそれ、無ければ eBay 公式の
        # 記載順ルールで position=1。複数/0 domestic は dsort=None とし
        # resolve_domestic_priority 側で abort 判定 (combined 中止 degrade)。
        dsort = (
            (dom_sort_first if dom_sort_first is not None else 1)
            if dom_count == 1 else None
        )
        policies.append(ShippingPolicyInfo(
            policy_id=pid,
            name=str(p.get("name") or pid),
            service_names=svc_names,
            domestic_service_count=dom_count,
            domestic_sort_order=dsort,
            domestic_service_codes=tuple(dom_codes),
        ))

    if not policies:
        return _empty("active fulfillment policy が 0 件")
    # name 昇順 (Q-5 user 承認: name 昇順表示)
    policies.sort(key=lambda x: x.name.lower())
    return ShippingPolicyList(policies=policies, ok=True, error=None)
