#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知ファサード (依頼ボード #39 Phase A S2) — record_and_maybe_send.

役割: あらゆる通知の **単一 choke point**。以下 2 つを常に分離して行う。

  1. 記録 (Q0): `notification_log` へ必ず INSERT する。Discord へ実送信するか
     どうかに関わらず、DASHBOARD (S4) が全通知を後から閲覧できるようにする。
  2. 送信可否判定: config `discord_category_gate[category]` が ON、かつ
     dedupe_key 指定時は直近ヒットが無い場合のみ実際に Discord へ POST する。

設計メモ:
  - category ゲート ON/OFF は「Discord へ push するか」だけを制御し、記録自体は
    ゲートに関わらず必ず行う (silent skip 禁止、silent-skip-prevention.md)。
  - dedupe 判定は **INSERT より前** に行う。先に INSERT してしまうと、直後の
    has_recent_dedupe が「たった今 INSERT した自分自身」にヒットし、2 回目以降
    ではなく **1 回目から** 送信抑止される自己 dedupe バグになる。
  - Discord POST は notifiers.discord_notifier.resolve_webhook(category) で
    直接叩く。DiscordNotifier.send_message は本モジュールの
    record_and_maybe_send を呼ぶ側 (choke point 統合) なので、ここから
    notifier_for/send_message へ戻ると無限再帰になる。両モジュール間の import は
    循環を避けるため関数内 (lazy) import にする。
  - config 読込は都度ディスクから行う (通知頻度は低く、キャッシュによる stale
    設定反映漏れの方が実害が大きい。K1: シンプルさ優先)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import requests

from monitor.notification_log_db import has_recent_dedupe, insert_notification

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
_TIMEOUT_SEC = 10

# config/schedule_config.json に discord_category_gate セクションが無い/読込失敗
# 時のコード定数 fallback。order/action_required/keyword/rival は金銭直結 or
# user 対応が必要な性質のため既定 ON、他は DASHBOARD (S4) での閲覧を主経路とし
# Discord 通知過多を避けるため既定 OFF。
_DEFAULT_GATE: dict = {
    "order": True,
    "action_required": True,
    "keyword": True,
    "rival": True,
    "system": False,
    "inventory": False,
    "research": False,
    "pricing": False,
    "default": False,
}

# 統合レビュー HIGH-1 対応 (依頼ボード#39 Phase A S2 追記、2026-07-03):
# critical/error severity はカテゴリゲートに関わらず**常時 Discord 送信**する。
# 理由: gate=OFF の system/inventory/pricing 等にも「欠落タスク検知」「監視カバレッジ
# 欠落」「URL乖離」「値下げ実行失敗」など安全網 / money-direct アラートが含まれ、
# 単純に category gate だけで判定すると黙殺される (Q0 silent skip 再発)。
# info/warning は従来通り category gate に従う (通知過多防止の主目的は維持)。
_ALWAYS_SEND_SEVERITIES = frozenset({"critical", "error"})


def _load_gate_config() -> dict:
    """discord_category_gate セクションを読み込む (失敗時は空 dict → 既定 fallback)."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("discord_category_gate") or {}
    except (OSError, json.JSONDecodeError) as e:  # noqa: BLE001
        logger.warning(f"schedule_config.json 読込失敗、既定ゲートを使用: {e}")
        return {}


def _gate_open(category: str) -> bool:
    """category の Discord 送信ゲートが ON か判定 (config 優先、無ければ既定定数)."""
    gate = _load_gate_config()
    if category in gate:
        return bool(gate[category])
    return _DEFAULT_GATE.get(category, False)


def _post_to_discord(category: str, title: str, body: str,
                      embed: Optional[dict]) -> bool:
    """resolve_webhook(category) 経由で Discord へ実際に POST する.

    embed 指定時は embeds のみ送る (content は付けない、既存 task 側 alert 関数群
    と同じ payload 形式を踏襲)。embed 無しの時は title (+ body) を content に使う
    (DiscordNotifier.send_message の従来 payload 形式を踏襲)。
    """
    from notifiers.discord_notifier import resolve_webhook  # 循環 import 回避 (lazy)

    webhook_url = resolve_webhook(category)
    if not webhook_url:
        return False
    payload: dict = {}
    if embed:
        payload["embeds"] = [embed]
    else:
        payload["content"] = f"{title}\n{body}" if body else title
    try:
        r = requests.post(webhook_url, json=payload, timeout=_TIMEOUT_SEC)
        return r.status_code in (200, 204)
    except Exception as e:  # noqa: BLE001 — 送信失敗を silent にしない (Q0)
        logger.error(f"Discord POST 失敗 (category={category}): {e}")
        return False


def record_and_maybe_send(
    category: str,
    severity: str,
    title: str,
    body: str = "",
    *,
    link_target: Optional[str] = None,
    link_ref: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    embed: Optional[dict] = None,
) -> dict:
    """通知を記録し、カテゴリゲート ON かつ dedupe 未ヒットなら Discord へ送信する.

    常に notification_log へ INSERT する (Q0: 送信可否に関わらず記録は必須)。
    notification_log への記録が失敗しても Discord 送信判定・送信自体は続行する
    (記録失敗で通知フロー全体を落とさない、logger.error で痕跡を残す)。

    Args:
        category: notification_log の category whitelist (NOTIFICATION_CATEGORIES)
            かつ discord_notifier.WEBHOOK_CATEGORY_ENV のいずれか + 'default'。
        severity: notification_log の severity whitelist (info/warning/error/critical)。
        title: 通知タイトル (embed 未指定時は Discord content にもそのまま使う)。
        body: 通知本文 (embed 未指定時のみ Discord content に併記、embed 指定時は
            notification_log の記録用途のみ)。
        link_target / link_ref: DASHBOARD (S4) のナビ遷移先ヒント (任意)。
        dedupe_key: 指定時、直近 24h 以内に同一 key で送信済なら Discord 送信を
            抑止する (record 自体はしない ように呼び出し元で扱いたい場合は呼び出し
            前に自前で判定すること。本関数は record は必ず行う設計)。
        embed: Discord embed dict (指定時は payload に embeds のみ使う)。

    Returns:
        dict: {
            "notification_id": int | None,  # 記録失敗時は None
            "discord_sent": bool,           # 実際に Discord へ送信できたか
            "gated": bool,                  # category ゲートが OFF だったか
                                            #  (severity bypass で実送信されても True のまま
                                            #   = gate 設定状態を後から追跡可能に保つ)
            "deduped": bool,                # dedupe_key ヒットで送信スキップしたか
            "severity_bypassed": bool,      # critical/error で gate OFF を bypass したか
        }
    """
    gate_open = _gate_open(category)

    deduped = False
    if dedupe_key:
        try:
            deduped = has_recent_dedupe(dedupe_key)
        except Exception as e:  # noqa: BLE001 — dedupe 判定失敗は fail-safe で送信側へ
            logger.warning(f"dedupe 判定失敗 (fail-safe で送信側に倒す): {e}")
            deduped = False

    # 統合レビュー HIGH-1 (2026-07-03): critical/error は gate OFF でも常時送信。
    # gate 判定と送信判定を明確に分離 (result["gated"] は「本来 category gate 的に
    # OFF だったか」を返し、severity bypass で実送信された場合も True のまま = user
    # が gate 状態を後から確認できる情報として保持する)。
    bypass_active = severity in _ALWAYS_SEND_SEVERITIES
    should_send = (gate_open or bypass_active) and not deduped
    sent = _post_to_discord(category, title, body, embed) if should_send else False

    notification_id: Optional[int] = None
    try:
        notification_id = insert_notification(
            category, severity, title, body or None,
            link_target=link_target, link_ref=link_ref,
            discord_sent=sent, dedupe_key=dedupe_key,
        )
    except Exception as e:  # noqa: BLE001 — 記録失敗でも通知フロー自体は継続 (Q0)
        logger.error(
            f"notification_log 記録失敗 (category={category!r}, title={title!r}): {e}")

    return {
        "notification_id": notification_id,
        "discord_sent": sent,
        "gated": not gate_open,
        "deduped": deduped,
        "severity_bypassed": bypass_active and not gate_open,
    }
