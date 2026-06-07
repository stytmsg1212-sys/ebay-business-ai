#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W#3 ライバルセラー新規出品モニター (rival_seller_monitor.py)

日本の優良eBayセラー(5-15名)を手動登録→そのセラーの新規出品を差分検知→
AIで一次フィルタ→Discord通知。

設計方針:
  - listing 識別は ebay_item_id (sku-rules.md 準拠。SKU は一切使わない)
  - 監視対象は日本セラーのみ (JP未確認は is_jp_verified=0 で警告)
  - 購入・出品・値下げは一切しない (発掘=通知のみ)
  - Browse API sellers: フィルタで現出品取得→差分検知→claim-then-act dedupe
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ebay-manager root の .env を明示ロード
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

logger = logging.getLogger(__name__)

# ── JP判定: Browse API で確認できる item_location_country 値 ──────────
_JP_LOCATION_CODES = {"JP", "Japan", "JAPAN"}

# AI一次フィルタ: 自社が扱える/利益見込みのスコア閾値
# 仕入先候補 (60) より低め = 「自社未踏ジャンル発掘」が目的なので広めに捕捉
_EVAL_SCORE_THRESHOLD = 30

# Browse API: sellers フィルタの最大セラー数 (API 仕様上制限なし、実用上 1 call = 1 seller 直列)
_BROWSE_PAGE_SIZE = 50  # 1 セラーあたり最大取得件数


def get_conn():
    from monitor.database import get_conn as _gc
    return _gc()


# ── DB ヘルパー ────────────────────────────────────────────────────────

def add_monitored_seller(seller_id: str, label: str) -> tuple[int, bool, bool]:
    """セラーを登録。JP判定を試み、未確認なら is_jp_verified=0 で登録。

    Returns:
        (db_id, inserted_new, is_jp_verified)
        - inserted_new=True: 新規登録
        - is_jp_verified=True: JP確認済み / False: 未確認 (UIに警告)
    """
    seller_id = (seller_id or "").strip()
    if not seller_id:
        raise ValueError("seller_id は空文字列にできません")

    # JP確認: Browse API で該当セラーの出品1件を取得し item_location を確認
    is_jp = _verify_seller_is_jp(seller_id)
    is_jp_verified_int = 1 if is_jp else 0

    import sqlite3
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO monitored_sellers "
                "(seller_id, seller_label, is_jp_verified, is_active) "
                "VALUES (?,?,?,1)",
                (seller_id, label or seller_id, is_jp_verified_int),
            )
            if cur.rowcount == 1:
                return (cur.lastrowid, True, is_jp)
            # 既存 → 行を返す
            row = conn.execute(
                "SELECT id, is_jp_verified FROM monitored_sellers WHERE seller_id=?",
                (seller_id,),
            ).fetchone()
            return (row["id"], False, bool(row["is_jp_verified"]))
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id, is_jp_verified FROM monitored_sellers WHERE seller_id=?",
                (seller_id,),
            ).fetchone()
            return (row["id"], False, bool(row["is_jp_verified"]))


def _verify_seller_is_jp(seller_id: str) -> bool:
    """Browse API sellers: フィルタで出品1件を取得し item_location_country=JP を確認。

    失敗時は False (is_jp_verified=0) で登録させ、UIに警告表示。
    """
    try:
        client = _get_browse_client()
        if client is None:
            logger.warning(f"Browse API client 未設定。seller={seller_id} の JP 判定をスキップ")
            return False
        token = client._get_token()
        import httpx
        resp = httpx.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
            params={
                "q": "*",
                "filter": f"sellers:{{{seller_id}}}",
                "limit": 1,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            summaries = data.get("itemSummaries") or []
            if summaries:
                loc = (summaries[0].get("itemLocation") or {}).get("country", "")
                if loc in _JP_LOCATION_CODES:
                    return True
                # country が空の場合はタイトルのみで判定できないため未確認扱い
                logger.info(
                    f"seller={seller_id} の item_location.country={loc!r} → JP未確認"
                )
                return False
            else:
                # 出品が0件 = JP判定不能
                logger.info(f"seller={seller_id}: Browse API 0件 → JP判定不能")
                return False
        else:
            logger.warning(
                f"seller={seller_id} JP判定 Browse API HTTP {resp.status_code}"
            )
            return False
    except Exception as e:
        logger.warning(f"seller={seller_id} JP判定エラー: {e}")
        return False


def list_monitored_sellers(active_only: bool = True) -> list[dict]:
    """登録セラー一覧。active_only=True で is_active=1 のみ。"""
    sql = (
        "SELECT * FROM monitored_sellers "
        + ("WHERE is_active=1 " if active_only else "")
        + "ORDER BY added_at DESC"
    )
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def toggle_seller_active(seller_db_id: int, is_active: bool) -> bool:
    """セラーの active/inactive を切り替える。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE monitored_sellers SET is_active=? WHERE id=?",
            (1 if is_active else 0, seller_db_id),
        )
        return cur.rowcount > 0


def delete_monitored_seller(seller_db_id: int) -> bool:
    """セラーを削除 (検知済み listings も同時削除)。"""
    with get_conn() as conn:
        # seller_id を先に取得
        row = conn.execute(
            "SELECT seller_id FROM monitored_sellers WHERE id=?", (seller_db_id,)
        ).fetchone()
        if not row:
            return False
        sid = row["seller_id"]
        conn.execute(
            "DELETE FROM monitored_seller_listings WHERE seller_id=?", (sid,)
        )
        cur = conn.execute(
            "DELETE FROM monitored_sellers WHERE id=?", (seller_db_id,)
        )
        return cur.rowcount > 0


def _claim_new_listing(seller_id: str, ebay_item_id: str,
                       title: str, price_usd: float) -> bool:
    """claim-then-act: INSERT OR IGNORE で UNIQUE(ebay_item_id) 重複はスキップ。
    True=新規 / False=既知。
    """
    import sqlite3
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO monitored_seller_listings "
                "(seller_id, ebay_item_id, title, price_usd) "
                "VALUES (?,?,?,?)",
                (seller_id, ebay_item_id, title, price_usd),
            )
            return cur.rowcount == 1
        except sqlite3.IntegrityError:
            return False


def _mark_notified(ebay_item_id: str, eval_score: Optional[int],
                   eval_reason: Optional[str], notified: bool = True) -> None:
    """eval 結果を記録。notified 列は **実際に Discord 送信できた時のみ 1**。

    MEDIUM-2 (Codex): webhook 未設定 / 送信失敗 / 閾値未満スキップ で notified=1 を
    立てると「Discord 通知済み」フィルタ (tab UI) に偽計上される。送信成功時のみ 1。
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE monitored_seller_listings "
            "SET notified=?, eval_score=?, eval_reason=? "
            "WHERE ebay_item_id=?",
            (1 if notified else 0, eval_score, eval_reason, ebay_item_id),
        )


def _update_seller_last_checked(seller_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE monitored_sellers SET last_checked_at=CURRENT_TIMESTAMP "
            "WHERE seller_id=?",
            (seller_id,),
        )


def get_recent_detections(seller_id: Optional[str] = None,
                          limit: int = 50) -> list[dict]:
    """検知済み listing 一覧 (UI表示用)。"""
    if seller_id:
        sql = (
            "SELECT * FROM monitored_seller_listings "
            "WHERE seller_id=? ORDER BY first_seen_at DESC LIMIT ?"
        )
        params = (seller_id, limit)
    else:
        sql = (
            "SELECT * FROM monitored_seller_listings "
            "ORDER BY first_seen_at DESC LIMIT ?"
        )
        params = (limit,)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Browse API ────────────────────────────────────────────────────────

def _get_browse_client():
    """BrowseAPIClient を返す。app_id/cert_id 未設定なら None。"""
    try:
        from monitor.credentials import get_ebay_credentials
        creds = get_ebay_credentials()
        app_id = creds.get("app_id") or os.environ.get("EBAY_APP_ID")
        cert_id = creds.get("cert_id") or os.environ.get("EBAY_CERT_ID")
        if not app_id or not cert_id:
            return None
        from tasks.ebay_browse_api import BrowseAPIClient
        return BrowseAPIClient(app_id=app_id, cert_id=cert_id)
    except Exception as e:
        logger.warning(f"BrowseAPIClient 生成失敗: {e}")
        return None


def _fetch_seller_listings(client, seller_id: str) -> list[dict]:
    """Browse API sellers: フィルタで seller の現在出品を取得。

    API仕様: filter=sellers:{username} で username は {}で囲む必要あり。
    複数セラーは sellers:{u1|u2} pipe区切りだが、本実装は1セラー1call直列。
    """
    import httpx
    try:
        token = client._get_token()
        resp = httpx.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
            params={
                "q": "*",
                "filter": f"sellers:{{{seller_id}}}",
                "sort": "newlyListed",
                "limit": _BROWSE_PAGE_SIZE,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        items = []
        for s in data.get("itemSummaries") or []:
            price_val = 0.0
            try:
                price_val = float((s.get("price") or {}).get("value", 0))
            except (ValueError, TypeError):
                pass
            items.append({
                "item_id": s.get("itemId", ""),
                "title": s.get("title", ""),
                "price_usd": price_val,
                "item_url": s.get("itemWebUrl", ""),
                "category_path": s.get("categoryPath", ""),
                "image_url": (s.get("image") or {}).get("imageUrl", ""),
            })
        logger.info(f"seller={seller_id}: Browse API {len(items)}件取得")
        return items
    except Exception as e:
        # HIGH-2 (code-reviewer/Codex): 取得失敗を [] で返すと「新規0件 = 在庫なし」に
        # 化けて silent skip になる (anti-bot で恒常0件でも気づけない)。re-raise して
        # 呼出側で error 化させ「取得失敗」と「新規なし」を区別する (Q0)。
        logger.warning(f"seller={seller_id}: Browse API エラー: {e}")
        raise


# ── AI 一次フィルタ ──────────────────────────────────────────────────────

_RIVAL_EVAL_SYSTEM = """\
あなたはeBayセラーの新規出品機会評価エキスパートです。
日本のライバルeBayセラーが新規出品した商品を見て、
「自社でも扱える商品か・利益が見込めるか」を評価します。

出力はJSON形式のみ。
{
  "score": 0-100の整数,
  "reason": "評価理由（日本語、1-2文）",
  "can_handle": true | false
}

score 50以上: 自社でも扱える可能性あり (家電/AV/計測機器/工具など自社カテゴリ)
score 50未満: 扱いが難しい (食品/衣料/消耗品/中国製バルク品 など)
can_handle: score>=50 の場合 true

コードブロック・前置き不要。JSONのみ出力。
"""


def _evaluate_listing(title: str, category_path: str,
                      price_usd: float) -> tuple[int, str]:
    """Haiku で新規出品の一次フィルタ評価。

    Returns: (score, reason)
    """
    try:
        import anthropic
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return (50, "ANTHROPIC_API_KEY 未設定 (デフォルト通知)")

        client = anthropic.Anthropic(api_key=key)
        user_text = (
            f"商品タイトル: {title}\n"
            f"カテゴリ: {category_path or '不明'}\n"
            f"価格: ${price_usd:.2f}\n\n"
            "自社eBayショップで扱える商品か、利益が見込めるか評価してください。"
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=_RIVAL_EVAL_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
        )
        text = "".join(
            getattr(b, "text", "") for b in msg.content
            if getattr(b, "type", None) == "text"
        )
        import json
        import re
        m = re.search(r'\{[\s\S]*\}', text or "")
        if m:
            data = json.loads(m.group(0))
            score = max(0, min(100, int(data.get("score", 0))))
            reason = str(data.get("reason", ""))[:300]
            return (score, reason)
    except Exception as e:
        logger.warning(f"AI評価エラー ({title[:40]}): {e}")
    return (50, "評価エラー (デフォルト通知)")


# ── Discord 通知 ─────────────────────────────────────────────────────────

def _send_rival_new_listing_alert(webhook_url: str, seller_id: str,
                                   item: dict, eval_score: int,
                                   eval_reason: str) -> bool:
    """ライバルセラー新規出品 Discord 通知。"""
    from monitor.notifier import _send_webhook
    from datetime import datetime

    score_label = "注目" if eval_score >= 70 else "参考"
    color = 0x00BFFF if eval_score >= 70 else 0x778899  # deep sky blue / slate gray

    payload = {
        "embeds": [{
            "title": f"[ライバル新規出品] {seller_id} ({score_label})",
            "description": (
                f"**{item['title'][:120]}**\n\n"
                f"価格: ${item['price_usd']:.2f}\n"
                f"カテゴリ: {item.get('category_path', '不明')[:80]}\n"
                f"AI評価: {eval_score}/100 — {eval_reason[:150]}\n\n"
                f"[eBayで見る]({item.get('item_url', '')})"
            ),
            "color": color,
            "footer": {
                "text": (
                    f"eBay Rival Monitor | "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
                )
            },
        }]
    }
    return _send_webhook(webhook_url, payload)


# ── メインロジック ────────────────────────────────────────────────────────

def check_seller_new_listings(seller_id: str, config: dict) -> dict:
    """指定セラーの新規出品を差分検知→AI評価→Discord通知。

    Returns:
        {'new_items': int, 'notified': int, 'skipped': int, 'error': str|None}
    """
    webhook_url = (
        (config.get("notifications") or {}).get("rival_seller_webhook_url")
        or (config.get("notifications") or {}).get("discord_webhook_url")
        or ""
    )

    client = _get_browse_client()
    if client is None:
        return {
            "new_items": 0, "notified": 0, "skipped": 0,
            "error": "eBay Browse API credentials 未設定",
        }

    try:
        listings = _fetch_seller_listings(client, seller_id)
    except Exception as e:
        # HIGH-2: 取得失敗は「新規0件」と別状態。last_checked は更新せず次回再試行。
        return {
            "new_items": 0, "notified": 0, "skipped": 0,
            "error": f"Browse 取得失敗: {type(e).__name__}: {e}",
        }
    new_items = 0
    notified = 0
    skipped = 0

    for item in listings:
        ebay_item_id = item["item_id"]
        if not ebay_item_id:
            continue

        # claim-then-act: 既知なら skip (dedupe)
        is_new = _claim_new_listing(
            seller_id=seller_id,
            ebay_item_id=ebay_item_id,
            title=item["title"],
            price_usd=item["price_usd"],
        )
        if not is_new:
            continue

        new_items += 1

        # AI 一次フィルタ
        eval_score, eval_reason = _evaluate_listing(
            title=item["title"],
            category_path=item.get("category_path", ""),
            price_usd=item["price_usd"],
        )

        if eval_score < _EVAL_SCORE_THRESHOLD:
            logger.debug(
                f"seller={seller_id} item={ebay_item_id} "
                f"score={eval_score} < {_EVAL_SCORE_THRESHOLD}: 通知スキップ"
            )
            _mark_notified(ebay_item_id, eval_score, eval_reason, notified=False)
            skipped += 1
            continue

        # Discord 通知
        sent = False
        if webhook_url:
            sent = _send_rival_new_listing_alert(
                webhook_url=webhook_url,
                seller_id=seller_id,
                item=item,
                eval_score=eval_score,
                eval_reason=eval_reason,
            )
            if sent:
                logger.info(
                    f"seller={seller_id} item={ebay_item_id} "
                    f"score={eval_score}: Discord 通知送信"
                )
            else:
                logger.warning(
                    f"seller={seller_id} item={ebay_item_id}: Discord 通知失敗"
                )
        else:
            # MEDIUM-2: webhook 未設定は「未送信」。notified=1 にしない (偽計上防止)。
            logger.info(
                f"seller={seller_id} item={ebay_item_id} "
                f"score={eval_score}: webhook 未設定 (DB のみ記録・未送信)"
            )

        _mark_notified(ebay_item_id, eval_score, eval_reason, notified=sent)
        if sent:
            notified += 1
        else:
            skipped += 1  # 送信失敗 / webhook 未設定 = 通知できていない

    _update_seller_last_checked(seller_id)
    return {
        "new_items": new_items,
        "notified": notified,
        "skipped": skipped,
        "error": None,
    }


def run_rival_seller_sweep(config: dict) -> dict:
    """全 active セラーを巡回して新規出品をチェック (scheduled task entry)。

    Returns:
        {
            'sellers_checked': int,
            'total_new': int,
            'total_notified': int,
            'total_skipped': int,
            'errors': list[str],
        }
    """
    sellers = list_monitored_sellers(active_only=True)
    if not sellers:
        logger.info("rival_seller_sweep: active セラー 0 件。スキップ。")
        return {
            "sellers_checked": 0,
            "total_new": 0,
            "total_notified": 0,
            "total_skipped": 0,
            "errors": [],
        }

    sellers_checked = 0
    total_new = 0
    total_notified = 0
    total_skipped = 0
    skipped_not_jp = 0
    errors: list[str] = []

    for seller in sellers:
        sid = seller["seller_id"]
        # HIGH-1 (code-reviewer/Codex): 競合は日本セラーのみ (feedback_competitor_jp_sellers_only)。
        # JP未確認セラーは自動巡回・通知しない。UI で JP 確認後に有効化させる。
        # silent skip にしないため log + skipped_not_jp に痕跡を残す (Q0)。
        if not seller.get("is_jp_verified"):
            logger.info(
                f"rival_seller_sweep: seller={sid} JP未確認のため自動巡回スキップ "
                "(UIでJP確認後に再有効化)"
            )
            skipped_not_jp += 1
            continue
        logger.info(f"rival_seller_sweep: checking seller={sid}")
        try:
            r = check_seller_new_listings(sid, config)
            total_new += r["new_items"]
            total_notified += r["notified"]
            total_skipped += r["skipped"]
            if r["error"]:
                errors.append(f"{sid}: {r['error']}")
            sellers_checked += 1
        except Exception as e:
            msg = f"{sid}: 予期しないエラー: {e}"
            logger.error(msg, exc_info=True)
            errors.append(msg)
        # Browse API 直列呼出: レート制限回避のため短い待機
        time.sleep(0.5)

    logger.info(
        f"rival_seller_sweep 完了: "
        f"sellers={sellers_checked} new={total_new} "
        f"notified={total_notified} skipped={total_skipped} "
        f"jp未確認skip={skipped_not_jp} errors={len(errors)}"
    )
    return {
        "sellers_checked": sellers_checked,
        "total_new": total_new,
        "total_notified": total_notified,
        "total_skipped": total_skipped,
        "skipped_not_jp": skipped_not_jp,
        "errors": errors,
    }
