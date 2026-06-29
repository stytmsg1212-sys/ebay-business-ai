"""W293 eBaymag セッション維持 heartbeat タスク (2026-06-29).

15 分ごとに eBaymag セッションの生死を確認し、状態変化 (dead/alive) を
action_required チャンネルへ Discord 通知する。episode dedupe で
dead が続く間は再通知しない (最初の 1 回だけ)。

フロー:
  1. kill switch 二重ガード
  2. cdp_lock.acquire(blocking=False) で mutating 操作中は skip_busy
  3. session_heartbeat() でセッション状態を判定
  4. record_heartbeat() で DB 記録
  5. get_last_definitive_heartbeat() と比較して episode 変化時のみ通知
  6. purge_old_heartbeat(30) でログ整理
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


def run_ebaymag_session_heartbeat(config: dict) -> dict:
    """eBaymag セッション heartbeat タスク本体。

    Returns:
        {"success": True, "outcome": ..., "message": ...}
        ※ session 切れは検知結果 (failure ではない) → success は常に True。
    """
    # ── kill switch 確認 (二重ガード) ──
    hb_cfg = (config.get("tasks_enabled") or {}).get("ebaymag_session_heartbeat") or {}
    if not hb_cfg.get("enabled", True):
        logger.info("[heartbeat] ebaymag_session_heartbeat disabled, skip")
        return {"success": True, "outcome": "skip_disabled",
                "message": "ebaymag_session_heartbeat disabled"}

    # ── cdp_lock: mutating 操作中なら即 skip (blocking=False) ──
    try:
        from monitor.cdp_lock import acquire as _cdp_lock_acquire, LockBusy as _LockBusy
    except ImportError as e:
        logger.error(f"[heartbeat] cdp_lock import 失敗: {e}")
        _record_safe("error", None, f"cdp_lock import 失敗: {e}")
        return {"success": True, "outcome": "error",
                "message": f"cdp_lock import 失敗: {e}"}

    try:
        with _cdp_lock_acquire(blocking=False):
            # lock 取得成功 → heartbeat 実行
            outcome, response_ms, note = _do_heartbeat()
    except _LockBusy:
        # mutating 操作が実行中 → skip_busy として記録して正常終了
        _record_safe("skip_busy", None, "mutating lock busy")
        logger.info("[heartbeat] CDP lock busy, skip_busy")
        return {"success": True, "outcome": "skip_busy",
                "message": "CDP lock busy (mutating 操作中)"}
    except OSError as e:
        # lock ファイル操作失敗
        logger.error(f"[heartbeat] cdp_lock open 失敗: {e}")
        _record_safe("error", None, f"cdp_lock open 失敗: {e}")
        return {"success": True, "outcome": "error", "message": str(e)}

    # ── episode 判定用: DB 記録 前 に前回 definitive を取得 (F: helper 統一)
    # _record_safe 失敗時でも prev_definitive は正しく比較に使える。
    prev_definitive = None
    if outcome in ("alive", "dead"):
        try:
            from monitor.database import get_last_definitive_heartbeat
            prev_definitive = get_last_definitive_heartbeat()
        except Exception as e:
            logger.warning(f"[heartbeat] 前回outcome取得失敗: {e}")

    # ── DB 記録 ──
    _record_safe(outcome, response_ms, note)

    # ── episode dedupe 通知 ──
    _notify_if_episode_changed(outcome, prev_definitive)

    # ── 古いログ削除 ──
    try:
        from monitor.database import purge_old_heartbeat
        purge_old_heartbeat(30)
    except Exception as e:
        logger.warning(f"[heartbeat] purge_old_heartbeat 失敗: {e}")

    return {"success": True, "outcome": outcome,
            "message": f"outcome={outcome} response_ms={response_ms}"}


def _do_heartbeat() -> tuple[str, Optional[float], Optional[str]]:
    """session_heartbeat を実行し (outcome, response_ms, note) を返す。"""
    try:
        from monitor.ebaymag_driver import session_heartbeat, PLAYWRIGHT_AVAILABLE
    except ImportError as e:
        return "error", None, f"ebaymag_driver import 失敗: {e}"

    if not PLAYWRIGHT_AVAILABLE:
        return "cdp_absent", None, "playwright 未インストール"

    t0 = time.perf_counter()
    try:
        result = session_heartbeat()
    except Exception as e:
        return "error", None, f"heartbeat 例外: {e}"
    response_ms = round((time.perf_counter() - t0) * 1000, 1)

    # outcome を log から取得 (session_heartbeat は log に "outcome=xxx" をセット済)
    log_str = " ".join(result.log)
    if "outcome=alive" in log_str:
        outcome = "alive"
    elif "outcome=dead" in log_str:
        outcome = "dead"
    elif "outcome=cdp_absent" in log_str:
        outcome = "cdp_absent"
    elif "outcome=error" in log_str:
        outcome = "error"
    elif result.ok:
        outcome = "alive"
    else:
        # error が設定済なら cdp_absent (CDP 接続不能)、それ以外は dead
        err = result.error or ""
        outcome = "cdp_absent" if "9222" in err or "接続できません" in err else "dead"

    note: Optional[str] = result.error
    return outcome, response_ms, note


def _record_safe(outcome: str, response_ms: Optional[float], note: Optional[str]) -> None:
    """record_heartbeat を呼ぶ。失敗しても task を落とさない (Q0 痕跡優先)。"""
    try:
        from monitor.database import record_heartbeat
        record_heartbeat(outcome, response_ms, note)
    except Exception as e:
        logger.warning(f"[heartbeat] record_heartbeat 失敗: {e}")


def _notify_if_episode_changed(
    current_outcome: str,
    prev_definitive: Optional[dict] = None,
) -> None:
    """alive↔dead の episode 変化時のみ Discord 通知 (dedupe)。

    F: inline SELECT を廃止し、呼び出し元が _record_safe 前に取得した
    get_last_definitive_heartbeat() の結果を受け取る設計に統一。
    _record_safe 書込失敗時でも prev_definitive は正しい値を持つ。

    skip_busy / cdp_absent / error は episode 判定に参加しない (caller で除外済)。
    prev_definitive は get_last_definitive_heartbeat() と同じ契約
    (None = 初回 / dict with "outcome" key = 前回 alive|dead)。
    """
    if current_outcome not in ("alive", "dead"):
        return  # skip_busy / cdp_absent / error は episode 不変

    if prev_definitive is None:
        # 初回 (前回なし) は通知しない (startup noise 回避)
        return

    prev_outcome = prev_definitive["outcome"]
    if prev_outcome == current_outcome:
        return  # 変化なし → 抑制

    # episode 変化 → 通知
    if current_outcome == "dead":
        icon = "⚠️"
        msg = "eBaymag セッション切れ — CDP Chrome (9222) で再ログインしてください"
    else:
        icon = "✅"
        msg = "eBaymag セッション復活"

    try:
        from notifiers.discord_notifier import notifier_for
        notifier_for("action_required").send_message(f"{icon} {msg}")
        logger.info(f"[heartbeat] Discord 通知送信: {icon} {msg}")
    except Exception as e:
        logger.warning(f"[heartbeat] Discord 通知失敗: {e}")
