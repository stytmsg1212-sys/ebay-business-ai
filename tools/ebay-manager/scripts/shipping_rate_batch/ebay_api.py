"""eBay Sell Account v2 rate table API ラッパー (§8)。

- getRateTable: GET /sell/account/v2/rate_table/{id}  (実機検証済 HTTP200)
- updateShippingCost: POST /sell/account/v2/rate_table/{id}/update_shipping_cost
    payload {"rates":[{"rateId","shippingCost":{"value","currency":"USD"}}]} (bare array 不可、204 成功)
OAuth: monitor.ebay_oauth_refresh.get_valid_access_token()。

⚠️ API は既存 rateId の **金額更新のみ**。行(国)の追加・再編は不可 (= 構造変更は UI 限定)。
読戻しは eBay eventual consistency 考慮で短時間 retry (Codex F4)。
"""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.ebay.com/sell/account/v2/rate_table"
# rate table は marketplace スコープ。Phase 6 live-verified 経路 (phase6_apply_band.py:23)
# が GET/POST 双方でこのヘッダを送っていた。欠落すると shippingRegionNames の
# ローカライズ (国の表示名) が token 既定依存になり、F5 bijection が静かに崩れ得る
# (money-direct)。決定論的に EBAY_US 固定する。
MARKETPLACE_ID = "EBAY_US"


def _token() -> str:
    from monitor.ebay_oauth_refresh import get_valid_access_token
    tok = get_valid_access_token()
    if not tok:
        raise RuntimeError("eBay OAuth access token 取得失敗 (sell.account scope 要確認)")
    return tok


def get_rate_table(table_id: str, token: str | None = None, timeout: float = 30.0) -> dict:
    """rate table 全構造を取得。rates[] を含む dict を返す。"""
    token = token or _token()
    r = httpx.get(
        f"{API_BASE}/{table_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def update_shipping_cost(
    table_id: str, rate_updates: list[dict], token: str | None = None, timeout: float = 30.0
) -> dict:
    """金額更新。rate_updates = [{"rateId": str, "usd": int}, ...] (変更行のみで可)。

    Returns: {"ok": bool, "status": int, "body": str}。204 が成功。
    """
    token = token or _token()
    rates = [
        {"rateId": str(u["rateId"]), "shippingCost": {"value": f'{int(u["usd"])}.00', "currency": "USD"}}
        for u in rate_updates
    ]
    if not rates:
        return {"ok": True, "status": 0, "body": "no changes"}
    r = httpx.post(
        f"{API_BASE}/{table_id}/update_shipping_cost",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json", "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID},
        json={"rates": rates},
        timeout=timeout,
    )
    ok = r.status_code == 204
    if not ok:
        logger.warning(f"updateShippingCost {table_id} status={r.status_code} body={r.text[:300]}")
    return {"ok": ok, "status": r.status_code, "body": r.text[:500]}


def readback_verify(
    table_id: str, expected_zone_usd: dict, match_fn, token: str | None = None,
    retries: int = 3, delay_sec: float = 3.0,
) -> dict:
    """適用後の読戻し検証 (eventual consistency 考慮で retry、Codex F4)。

    Args:
        expected_zone_usd: {zone: usd} 期待値。
        match_fn: (live_rows, ) -> {"ok","zone_to_old_usd",...} (manifest.match_live_rows_to_zones の部分適用)。

    Returns: {"ok": bool, "mismatches": [str,...], "attempts": int}。
    """
    token = token or _token()
    last_mismatches: list[str] = []
    for attempt in range(1, retries + 1):
        data = get_rate_table(table_id, token=token)
        matched = match_fn(data.get("rates", []))
        if not matched["ok"]:
            last_mismatches = ["読戻し時の zone bijection 失敗: " + "; ".join(matched["errors"])]
        else:
            cur = matched["zone_to_old_usd"]
            last_mismatches = [
                f"zone {z}: live ${cur.get(z)} != 期待 ${exp}"
                for z, exp in expected_zone_usd.items() if cur.get(z) != exp
            ]
        if not last_mismatches:
            return {"ok": True, "mismatches": [], "attempts": attempt}
        if attempt < retries:
            time.sleep(delay_sec)
    return {"ok": False, "mismatches": last_mismatches, "attempts": retries}
