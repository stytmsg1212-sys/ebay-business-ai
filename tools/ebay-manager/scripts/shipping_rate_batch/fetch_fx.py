"""前月営業日平均 USD/JPY × (1-1%) を外部 API で取得 (§3.3 / Codex F2)。

源: frankfurter.app (ECB データ、httpx・非ブラウザ・API key 不要)。
定義: 「取得できた前月の営業日レートの単純平均」× 0.99 を ceil。暦日補間はしない。
fail-closed (F2): 取得失敗 / 観測営業日不足 / 範囲外 は ok=False。
  auto 側は ok=False なら更新せず dry-run 通知のみ (前回値での適用はしない)。
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import date
from typing import Optional

import httpx

from . import config

logger = logging.getLogger(__name__)

# 前月の営業日レートが最低これだけ取れていないと信頼しない (祝日等で目減りしても ~18 は出る)
MIN_OBSERVED_DAYS = 15
FX_API_BASE = "https://api.frankfurter.app"


def _prev_month_range(today: date) -> tuple[str, str]:
    """today の前月の [初日, 末日] を ISO 文字列で返す。"""
    y, m = today.year, today.month
    if m == 1:
        py, pm = y - 1, 12
    else:
        py, pm = y, m - 1
    first = date(py, pm, 1)
    # 末日 = 翌月初日の前日
    if pm == 12:
        nxt = date(py + 1, 1, 1)
    else:
        nxt = date(py, pm + 1, 1)
    last = date.fromordinal(nxt.toordinal() - 1)
    return first.isoformat(), last.isoformat()


def fetch_prev_month_fx(today: Optional[date] = None) -> dict:
    """前月平均 USD/JPY × 0.99 を取得。

    Args:
        today: 基準日 (省略時は date.today())。テスト用に注入可。

    Returns:
        {
            "ok": bool,
            "fx": int | None,            # 採用 FX (円/$, ceil)
            "raw_avg": float | None,      # 平均 (×0.99 前)
            "observed_days": int,
            "period": [first, last],
            "errors": [str, ...],
        }
    """
    if today is None:
        today = date.today()
    first, last = _prev_month_range(today)
    errors: list[str] = []
    try:
        r = httpx.get(
            f"{FX_API_BASE}/{first}..{last}",
            params={"base": "USD", "symbols": "JPY"},
            timeout=20,
            follow_redirects=True,   # .app は 301 を返すため必須
        )
        r.raise_for_status()
        data = r.json()
        rates = data.get("rates", {})
        vals = [v["JPY"] for v in rates.values() if isinstance(v, dict) and "JPY" in v]
    except Exception as e:  # noqa: BLE001 - 取得失敗は ok=False で fail-closed
        return {"ok": False, "fx": None, "raw_avg": None, "observed_days": 0,
                "period": [first, last], "errors": [f"FX API 失敗: {type(e).__name__}: {e}"]}

    observed = len(vals)
    if observed < MIN_OBSERVED_DAYS:
        errors.append(f"観測営業日不足: {observed} < {MIN_OBSERVED_DAYS} ({first}..{last})")
        return {"ok": False, "fx": None, "raw_avg": None, "observed_days": observed,
                "period": [first, last], "errors": errors}

    avg = statistics.mean(vals)
    fx = math.ceil(avg * 0.99)
    if not (config.FX_MIN <= fx <= config.FX_MAX):
        errors.append(f"FX 範囲外: {fx} not in [{config.FX_MIN},{config.FX_MAX}] (avg={avg:.3f})")
        return {"ok": False, "fx": fx, "raw_avg": avg, "observed_days": observed,
                "period": [first, last], "errors": errors}

    return {"ok": True, "fx": fx, "raw_avg": avg, "observed_days": observed,
            "period": [first, last], "errors": []}


def save_fx_state(result: dict) -> None:
    """採用 FX を履歴として保存 (監査用)。ok=True 時のみ last_good を更新。"""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {}
    if config.FX_STATE.exists():
        try:
            state = json.loads(config.FX_STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    history = state.get("history", [])
    history.append({k: result.get(k) for k in ("ok", "fx", "raw_avg", "observed_days", "period")})
    state["history"] = history[-24:]  # 直近 24 ヶ月
    if result.get("ok"):
        state["last_good"] = {"fx": result["fx"], "period": result["period"]}
    config.FX_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
