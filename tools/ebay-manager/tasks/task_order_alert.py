"""W7-A 注文アラート (30 分 polling).

検知対象 2 種:
  1. **DDP-B 発送 invoice アラート**: primary_market='US_only' SKU の新規注文
     → 発送時 invoice には商品価格 (関税抜) を記載するよう Discord で通知
  2. **Override #2 改 ($1500+ DE/IT/FR/KZ アラート)**: 高額 EU 注文発生
     → DDU 発送 + 関税通知メールテンプレを生成 → user に提示

両方とも GetOrders API で 30 分ごとに新規注文を検知.

冪等性:
  - high_value_eu_alerts.order_id / ddpb_dispatch_alerts.order_id で dedupe
  - INSERT OR IGNORE 使用
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Override #2 改 適用対象国
HIGH_VALUE_COUNTRIES = {"DE", "IT", "FR", "KZ"}
HIGH_VALUE_THRESHOLD_USD = 1500.0

# 動画 [dM6U7cVYumE] (2026-04-21) 最新値
DEFAULT_TARIFF_RATE = 0.10  # US 向け日本製基本値 10%


def _conn() -> sqlite3.Connection:
    """Wave A (2026-05-12): monitor.database.get_conn に統一.

    旧実装はハードコード path で `monitor.database.DB_PATH` の monkeypatch を
    bypass していたため、pytest tmp_db fixture が機能しなかった (test 不能).
    `get_conn` は WAL + busy_timeout 付きで本番経路と同条件、かつ tmp_db で差替可能.
    """
    from monitor.database import get_conn
    return get_conn()


def _get_credentials() -> Optional[tuple]:
    """eBay API 認証情報を (app_id, dev_id, cert_id, user_token) タプルで取得.

    get_ebay_credentials() は **dict** を返す. dict のまま呼び出し側で
    `a, b, c, d = creds` するとキー文字列 ("app_id" 等) が入り eBay GetOrders
    認証が静かに全滅する (2026-05-16 W133 item2 で inventory_sync と同型の
    本番ブロッカーを発見; pytest は tuple の fake creds を mock し不可視だった).
    ここで明示的にタプル化し、4 値が 1 つでも空なら None を返して
    呼び出し側の `if not creds` ガードを機能させる.
    """
    try:
        from monitor.credentials import (
            ebay_credentials_ok,
            get_ebay_credentials,
        )
        creds = get_ebay_credentials()
        if not ebay_credentials_ok(creds):
            logger.error(
                "eBay 認証情報が未設定 "
                "(app_id/dev_id/cert_id/user_token のいずれか空)"
            )
            return None
        return (
            creds["app_id"], creds["dev_id"],
            creds["cert_id"], creds["user_token"],
        )
    except (ImportError, KeyError, OSError, ValueError) as e:
        # H8 (Wave C): broad except → specific exceptions (qiita 9 原則).
        # ImportError: credentials module 読込失敗 / KeyError: dict 構造不整合 /
        # OSError: file IO エラー / ValueError: 復号失敗等.
        logger.error(f"認証取得失敗: {e}")
        return None


def _record_sales_history_fetch_failure(order_id: str, error: str, webhook: str) -> None:
    """W149: order 処理失敗を retry queue に記録.

    シンプル方針 (K1): retry は polling 内で発生した失敗の即時 retry のみ.
    過去失敗の遅延 retry は実装しない (次回 polling で同 order が再 hit すれば
    add_sale が走る、UNIQUE INDEX で重複なし). queue は監視用 + 5 回連続失敗
    で Discord 1 回通知 (alert fatigue 防止に discord_notified flag).

    HIGH-2 (code-reviewer Phase D, 2026-05-22): INSERT OR IGNORE 後の rowcount==0
    (race で他 worker が同時 INSERT 成立) を検出して UPDATE 経路へ fall through.
    これがないと race 時に skip 側で attempt_count が進まず 5 回失敗 Discord 通知が
    遅延する (alert 遅延 = Q0 silent skip 親戚).
    """
    if not order_id:
        return

    def _update_existing(c, existing_row) -> None:
        new_count = existing_row["attempt_count"] + 1
        c.execute(
            """UPDATE sales_history_fetch_failures SET
               attempt_count = ?, last_attempt_at = CURRENT_TIMESTAMP, last_error = ?
               WHERE id = ?""",
            (new_count, error[:500], existing_row["id"]),
        )
        if new_count >= 5 and existing_row["discord_notified"] == 0:
            _send_discord(webhook, {
                "title": "[ALERT] W149 売却注文取得 5 回連続失敗",
                "description": (
                    f"order_id={order_id} の sales_history 記録が 5 回失敗.\n"
                    f"最終エラー: {error[:200]}\n"
                    "MonoDeck で当該 order を手動確認してください."
                ),
                "color": 0xD84C38,
                "timestamp": datetime.now().isoformat(),
            })
            c.execute(
                "UPDATE sales_history_fetch_failures SET discord_notified = 1 "
                "WHERE id = ?",
                (existing_row["id"],),
            )

    with _conn() as c:
        existing = c.execute(
            "SELECT id, attempt_count, discord_notified FROM sales_history_fetch_failures "
            "WHERE ebay_order_id = ?",
            (order_id,),
        ).fetchone()
        if existing:
            _update_existing(c, existing)
            return
        cur = c.execute(
            "INSERT OR IGNORE INTO sales_history_fetch_failures (ebay_order_id, last_error) "
            "VALUES (?, ?)",
            (order_id, error[:500]),
        )
        if cur.rowcount == 0:
            # race: 同時に他 worker が INSERT 成立 → skip 側を UPDATE 経路へ fall through
            existing2 = c.execute(
                "SELECT id, attempt_count, discord_notified FROM sales_history_fetch_failures "
                "WHERE ebay_order_id = ?",
                (order_id,),
            ).fetchone()
            if existing2:
                _update_existing(c, existing2)


def _clear_sales_history_fetch_failure(order_id: str) -> None:
    """W149: 過去失敗が今回成功した時 queue から remove (再通知防止)."""
    if not order_id:
        return
    with _conn() as c:
        c.execute(
            "DELETE FROM sales_history_fetch_failures WHERE ebay_order_id = ?",
            (order_id,),
        )


def _send_discord(webhook: str, embed: dict) -> bool:
    if not webhook:
        return False
    try:
        import httpx
        r = httpx.post(webhook, json={"embeds": [embed]}, timeout=10.0)
        return r.status_code in (200, 204)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        # H8 (Wave C): broad except → httpx specific exceptions.
        # HTTPError は HTTPStatusError / RequestError 等の親、TimeoutException も含む.
        logger.warning(f"Discord 送信失敗: {e}")
        return False


def _build_high_value_template(order: dict) -> str:
    """Override #2 改 関税通知メールテンプレ."""
    buyer_country = order.get("buyer_country_name") or order.get("buyer_country") or ""
    item_price = order.get("item_price_usd") or 0
    title = order.get("title") or ""
    order_id = order.get("order_id") or ""

    return (
        f"Dear Buyer,\n\n"
        f"Thank you for your purchase of:\n"
        f"  {title}\n"
        f"  Order #: {order_id}\n\n"
        f"Please be aware that this item will be shipped via DDU "
        f"(Delivered Duty Unpaid). Import customs duties and VAT (typically "
        f"around 19-22% of the item value, varies by country) will be "
        f"collected by the carrier upon delivery.\n\n"
        f"Estimated item value declared: ${item_price:.2f} USD\n"
        f"Destination: {buyer_country}\n\n"
        f"If you have any concerns, please reply to this message within "
        f"48 hours so we can discuss alternatives before shipment.\n\n"
        f"Thank you for your understanding.\n\n"
        f"Best regards,\n"
        f"MonoHonpo Japan"
    )


def _process_high_value_eu(order: dict, webhook: str) -> bool:
    """$1500+ DE/IT/FR/KZ 検知 + 通知."""
    item_price = order.get("item_price_usd") or 0
    shipping = order.get("shipping_usd") or 0
    total = item_price + shipping
    buyer_country = order.get("buyer_country") or ""
    order_id = order.get("order_id") or ""

    if total < HIGH_VALUE_THRESHOLD_USD:
        return False
    if buyer_country not in HIGH_VALUE_COUNTRIES:
        return False
    if not order_id:
        return False

    # 既存 alert 確認 (dedupe)
    with _conn() as c:
        existing = c.execute(
            "SELECT id, discord_sent FROM high_value_eu_alerts WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if existing:
            return False  # 既処理

        template = _build_high_value_template(order)
        c.execute(
            """INSERT INTO high_value_eu_alerts
               (order_id, sku, ebay_item_id, buyer_country, item_price_usd,
                shipping_usd, total_usd, detected_at, discord_sent, template_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (order_id, order.get("sku"), order.get("ebay_item_id"),
             buyer_country, item_price, shipping, total,
             datetime.now().isoformat(), template),
        )
        new_id = c.lastrowid

    # Discord 通知
    embed = {
        "title": f"[CRITICAL] $1500+ EU 高額注文 ({buyer_country})",
        "description": (
            f"DDU 発送 + 関税通知メール送信が必要です.\n"
            f"テンプレートを MonoDeck 「市場戦略」タブで確認 → eBay メッセージで送信.\n"
            f"発送 timeout: 48 時間"
        ),
        "color": 0xD84C38,
        "fields": [
            {"name": "Order", "value": order_id, "inline": True},
            {"name": "SKU", "value": order.get("sku") or "-", "inline": True},
            {"name": "Buyer", "value": buyer_country, "inline": True},
            {"name": "Item price", "value": f"${item_price:.2f}", "inline": True},
            {"name": "Shipping", "value": f"${shipping:.2f}", "inline": True},
            {"name": "Total", "value": f"${total:.2f}", "inline": True},
            {"name": "Title", "value": (order.get("title") or "")[:200], "inline": False},
        ],
        "timestamp": datetime.now().isoformat(),
    }
    sent = _send_discord(webhook, embed)
    if sent:
        with _conn() as c:
            c.execute(
                "UPDATE high_value_eu_alerts SET discord_sent = 1 WHERE id = ?",
                (new_id,),
            )
    logger.info(f"high_value_eu alert: order={order_id} sent={sent}")
    return True


def _process_ddpb_dispatch(order: dict, webhook: str) -> bool:
    """DDP-B (US_only) 注文の発送 invoice アラート."""
    sku = order.get("sku") or ""
    order_id = order.get("order_id") or ""
    if not sku or not order_id:
        return False

    # listing 単位 primary_market 確認 (W7-A Phase 3 SKU cascade 排除).
    # order の ebay_item_id 優先. 無ければ警告して skip (Q0 silent skip 防止).
    ebay_item_id = order.get("ebay_item_id") or ""
    with _conn() as c:
        if ebay_item_id:
            row = c.execute(
                "SELECT primary_market FROM ebay_listings "
                "WHERE ebay_item_id = ?",
                (ebay_item_id,),
            ).fetchone()
        else:
            logger.warning(
                f"order_id={order_id} sku={sku} に ebay_item_id 無し → "
                "DDP-B 判定 skip (要 order 取得元の調査)"
            )
            return False
        # FINDING 6 (2026-05-05): primary_market=NULL (= W7-A 分析未済) と
        # 'US_only 以外' を区別. NULL の場合は Discord に warning + alert を出して
        # 「分析未済 listing で US 注文発生」を user に通知 (silent skip 防止 / Q0 適合).
        # 業務リスク: Section 232 該当品が NULL のまま US 売上発生 → 関税申告漏れで赤字.
        if not row:
            return False
        pm = row["primary_market"]
        if pm is None:
            # 分析未済 listing で US 注文 → 既存 _send_discord helper で警告通知.
            warn_embed = {
                "title": "⚠️ DDP-B 判定不能 (primary_market 未分析)",
                "description": (
                    f"ebay_item_id={ebay_item_id or '?'} sku={sku or '?'} order_id={order_id} の "
                    "primary_market が NULL です. W7-A market_analysis_refresh で当該 listing を "
                    "再分析してください. Section 232 該当の場合 invoice 関税申告漏れリスクあり."
                ),
                "color": 16753920,  # オレンジ (warning)
            }
            # _send_discord は内部で httpx HTTPError を吸収して bool を返すため、
            # 通常の path で例外は出ない. 防御的に httpx 系のみ catch.
            import httpx as _h
            try:
                _send_discord(webhook, warn_embed)
            except (_h.HTTPError, _h.TimeoutException) as _e:
                logger.warning(f"Discord 通知失敗 (primary_market=NULL): {_e}")
            logger.warning(
                f"primary_market=NULL listing で US 注文発生: ebay_item_id={ebay_item_id} "
                f"order_id={order_id} → DDP-B 通知 skip (W7-A 分析後に再評価必要)"
            )
            return False
        # W109(3) (2026-05-09): primary_market='unknown' (= W110 新標準でも sample <3 で
        # 統計不能と確定済) は警告 spam 不要、ただし「分析済だが skip した」痕跡は logger.info
        # で必ず残す (Q0 silent skip 防止). NULL=未分析 vs unknown=分析済不能 を区別.
        if pm == "unknown":
            logger.info(
                f"primary_market='unknown' listing で US 注文発生: "
                f"ebay_item_id={ebay_item_id} sku={sku or '?'} order_id={order_id} "
                f"→ DDP-B 通知 skip (sample 不足で統計不能、Discord 警告は省略)"
            )
            return False
        if pm != "US_only":
            return False

        existing = c.execute(
            "SELECT id FROM ddpb_dispatch_alerts WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if existing:
            return False  # 既処理

        sale_price = order.get("item_price_usd") or 0
        tariff_buffer = sale_price * DEFAULT_TARIFF_RATE / (1 + DEFAULT_TARIFF_RATE)
        # 例: 販売 $115 → buffer $10.45 → invoice 申告 $104.55
        invoice_declared = sale_price - tariff_buffer

        c.execute(
            """INSERT INTO ddpb_dispatch_alerts
               (order_id, sku, ebay_item_id, buyer_country, sale_price_usd,
                tariff_buffer_usd, invoice_declared_usd, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, sku, order.get("ebay_item_id"),
             order.get("buyer_country") or "",
             sale_price, tariff_buffer, invoice_declared,
             datetime.now().isoformat()),
        )

    # Discord 通知 (注文直後)
    embed = {
        "title": "[ALERT] DDP-B 発送 invoice 注意",
        "description": (
            f"US_only SKU の注文発生. 発送 invoice には **関税抜き商品価格** を "
            f"記載してください (販売価格そのままだと二重課税)."
        ),
        "color": 0xC89B2A,
        "fields": [
            {"name": "Order", "value": order_id, "inline": True},
            {"name": "SKU", "value": sku, "inline": True},
            {"name": "Buyer", "value": order.get("buyer_country") or "", "inline": True},
            {"name": "販売価格", "value": f"${sale_price:.2f}", "inline": True},
            {"name": "推定関税 (10%)", "value": f"${tariff_buffer:.2f}", "inline": True},
            {"name": "Invoice 申告額", "value": f"**${invoice_declared:.2f}**", "inline": True},
            {"name": "Title", "value": (order.get("title") or "")[:200], "inline": False},
        ],
        "timestamp": datetime.now().isoformat(),
    }
    sent = _send_discord(webhook, embed)
    logger.info(f"ddpb_dispatch alert: order={order_id} sku={sku} sent={sent}")
    return True


def _decrement_inventory_for_stock_sku(order: dict) -> Optional[dict]:
    """W119 (2026-05-12): 有在庫 SKU の order 検知時に inventory_count を売れた数だけ減算.

    対象: order["sku"] が "stock" prefix の listing.
    冪等性: order_inventory_decrement_log で order_id × ebay_item_id を track して dedupe.

    Returns: 減算実施時 dict (notify 用)、対象外 / 既処理なら None.
    """
    sku = order.get("sku") or ""
    if not sku.startswith("stock"):
        return None
    ebay_item_id = order.get("ebay_item_id") or ""
    order_id = order.get("order_id") or ""
    qty = int(order.get("qty") or 1)
    if not ebay_item_id or not order_id or qty <= 0:
        return None

    # H2 (Wave A) atomic ordering:
    #   INSERT OR IGNORE を **先に** 実行し rowcount で「真に新規 insert か」判定.
    #   rowcount=0 = 既処理 (重複 polling) → 即 return None.
    #   rowcount=1 = 自分が claim 成立 → 在庫 UPDATE + new_inventory_count back-fill.
    # これにより check-then-act race (二重減算) を排除.
    # inventory_decrement_log のスキーマは migration v37 で init_db に集約済 (H1).
    with _conn() as c:
        # placeholder new_inventory_count=-1 で claim. 後で UPDATE で実値書込.
        cur = c.execute(
            """INSERT OR IGNORE INTO inventory_decrement_log
               (order_id, ebay_item_id, sku, quantity_decremented, new_inventory_count)
               VALUES (?, ?, ?, ?, ?)""",
            (order_id, ebay_item_id, sku, qty, -1),
        )
        if cur.rowcount == 0:
            return None  # 重複 polling、何もしない

        # claim 成立 → 在庫 UPDATE.
        row = c.execute(
            "SELECT inventory_count, title FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        if not row:
            # claim を rollback (整合性維持)
            c.execute(
                "DELETE FROM inventory_decrement_log WHERE order_id=? AND ebay_item_id=?",
                (order_id, ebay_item_id),
            )
            logger.warning(
                f"[inventory] order {order_id} の ebay_item_id={ebay_item_id} が DB に無い"
            )
            return None
        current = row["inventory_count"]
        title = row["title"]
        if current is None:
            # claim を rollback (在庫数未入力 listing は減算対象外)
            c.execute(
                "DELETE FROM inventory_decrement_log WHERE order_id=? AND ebay_item_id=?",
                (order_id, ebay_item_id),
            )
            logger.warning(
                f"[inventory] {ebay_item_id} sku={sku} inventory_count=NULL "
                f"(stock SKU だが在庫数未設定). 商品管理タブで入力推奨. 減算 skip."
            )
            return None

        # F4 (Codex 2026-05-16): SQL atomic 相対減算へ. 旧 Python 絶対値書込は
        # 並行 confirm_purchase / 手動在庫編集の加算を lost-update で潰す危険が
        # あった (order dedupe は同一 order 二重減算のみ防ぐ、別経路加算は守れない).
        # W133 が inventory_count への並行 writer (confirm) を新設したため実害化.
        c.execute(
            "UPDATE ebay_listings "
            "SET inventory_count = MAX(0, COALESCE(inventory_count, 0) - ?) "
            "WHERE ebay_item_id=?",
            (qty, ebay_item_id),
        )
        _nrow = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        new_count = int(_nrow["inventory_count"]) if _nrow is not None else 0
        c.execute(
            """UPDATE inventory_decrement_log SET new_inventory_count=?
               WHERE order_id=? AND ebay_item_id=?""",
            (new_count, order_id, ebay_item_id),
        )

    logger.info(
        f"[inventory] decremented {ebay_item_id} sku={sku}: "
        f"{current} → {new_count} (order {order_id} qty={qty})"
    )
    return {
        "order_id": order_id,
        "ebay_item_id": ebay_item_id,
        "sku": sku,
        "title": title,
        "qty": qty,
        "old_count": int(current),
        "new_count": new_count,
    }


def _process_memo_sale_warning(order: dict, webhook: str) -> bool:
    """W140 (2026-05-19): メモ付き listing が売れたら発送前警告を確保 + 通知.

    listing 識別は ebay_item_id (sku-rules: SKU 不使用)。claim-then-act
    (record_sale_warning = UNIQUE(order_id, ebay_item_id) + rowcount) で、
    同一注文の二重 polling でも警告 1 行・Discord 1 通に限定 (二重通知防止、
    既存 inventory_decrement_log と同型)。MonoDeck バナーは
    listing_sale_warnings.status='open' を表示し続ける = Discord 送信失敗
    でも user は MonoDeck で気付ける (発送見落とし防止の主経路)。

    Returns: 新規 claim して通知対象化したら True、対象外/重複なら False。
    """
    from monitor.database import (
        get_listing_note,
        record_sale_warning,
        set_sale_warning_discord_sent,
    )
    ebay_item_id = order.get("ebay_item_id") or ""
    order_id = order.get("order_id") or ""
    if not ebay_item_id or not order_id:
        # Codex 2段 HIGH-1 (Q0): SKU fallback 禁止のため ItemID 欠落注文は
        # メモ有無を評価できない。silent return せず痕跡を残す
        # (_process_ddpb_dispatch の ItemID 欠落 Q0 パターンと同様)。
        logger.warning(
            f"memo_sale_warning: order_id={order_id or '?'} に ebay_item_id "
            f"無し → メモ警告を評価不能 (SKU fallback 禁止)。手動確認推奨"
        )
        return False
    note = (get_listing_note(ebay_item_id) or "").strip()
    if not note:
        return False  # メモ無し listing は対象外 (正当な非該当、silent skip 不該当)
    # claim-then-act: 最初の polling のみ True (= Discord も 1 回のみ)
    if not record_sale_warning(order_id, ebay_item_id, note):
        return False  # 既処理 (二重 polling) → 何もしない
    title = ""
    with _conn() as c:
        row = c.execute(
            "SELECT title FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        if row:
            title = row["title"] or ""
    label = (f"{title[:50]} ({str(ebay_item_id)[-4:]})"
             if title else str(ebay_item_id))
    embed = {
        "title": "📎 メモ付き listing が売れました (発送前にメモ確認)",
        "description": (
            f"**{label}**\nOrder #{order_id}\n\n"
            f"**メモ:**\n{note[:1500]}"
        ),
        "color": 0xE8A33D,
        "timestamp": datetime.now().isoformat(),
    }
    sent = _send_discord(webhook, embed)
    set_sale_warning_discord_sent(order_id, ebay_item_id, sent)
    logger.info(
        f"memo_sale_warning: order={order_id} eid={ebay_item_id} sent={sent}"
    )
    return True


def run_order_alert_check(config: Optional[dict] = None,
                          num_days: int = 1) -> dict:
    """30 分 cron で呼ばれる本体.

    Args:
        config: schedule_config.json
        num_days: GetOrders 取得日数 (default 1, 30分polling なら 1 で十分)
    """
    cfg = config or {}
    webhook = cfg.get("discord", {}).get("webhook_url") or ""

    creds = _get_credentials()
    if not creds:
        return {"success": False, "error": "no_credentials"}

    app_id, dev_id, cert_id, user_token = creds

    from monitor.ebay_client import get_orders
    started_at = datetime.now()
    result = get_orders(app_id, dev_id, cert_id, user_token, num_days=num_days)
    if not result["success"]:
        logger.error(f"GetOrders failed: {result.get('message')}")
        return {"success": False, "error": result.get("message")}

    orders = result.get("orders") or []
    high_value_alerts = 0
    ddpb_alerts = 0
    inventory_decrements = 0
    memo_warnings = 0  # W140: メモ付き listing 売却の発送前警告 (新規 claim 数)
    order_processing_errors = 0  # H3 (Wave A): order 単位失敗を transparency 確保
    inventory_zero_listings: list[dict] = []  # 在庫 0 になった listing (DASHBOARD で表示)
    # W149: sales_history 充填 + fulfillment 自動ひも付け
    sales_recorded = 0       # 新規 INSERT 成立件数
    sales_skipped_dup = 0    # UNIQUE 衝突で skip (再 polling で同 order)
    fulfillment_links_realtime = 0  # link_one_by_sale で realtime ひも付き
    sales_failures = 0       # add_sale 自体が例外で失敗 (retry queue 行き)

    for order in orders:
        try:
            if _process_high_value_eu(order, webhook):
                high_value_alerts += 1
            if _process_ddpb_dispatch(order, webhook):
                ddpb_alerts += 1
            # W119 (2026-05-12): 有在庫 SKU の自動 inventory 減算
            dec = _decrement_inventory_for_stock_sku(order)
            if dec:
                inventory_decrements += 1
                # W133 (2026-05-16): 減算後の inventory_count を eBay へ反映.
                # listing 識別は ebay_item_id (SKU 不使用). 数量0 は inventory_sync
                # 側で OOS Control 機械検証してから revise (Defect 防止).
                from monitor import inventory_sync
                sync_res = inventory_sync.sync_listing_quantity(dec["ebay_item_id"])
                dec["sync_success"] = bool(sync_res.get("success"))
                dec["sync_skipped_zero_unsafe"] = bool(
                    sync_res.get("skipped_zero_unsafe")
                )
                dec["sync_message"] = sync_res.get("message") or ""
                # 在庫0 になった / sync 抑止 / sync 失敗 のいずれかなら Discord 対象.
                if (
                    dec["new_count"] == 0
                    or dec["sync_skipped_zero_unsafe"]
                    or not dec["sync_success"]
                ):
                    inventory_zero_listings.append(dec)
            # W140 (2026-05-19): メモ付き listing が売れたら発送前警告
            # (claim-then-act + Discord 1 回。失敗でも MonoDeck バナーで残る)。
            if _process_memo_sale_warning(order, webhook):
                memo_warnings += 1
        except (sqlite3.Error, KeyError, TypeError) as e:
            order_processing_errors += 1
            logger.warning(f"order {order.get('order_id')} 処理失敗: {e}")

    # W149 (2026-05-22): sales_history 充填 + fulfillment 自動ひも付け.
    # get_orders() は transaction flatten 構造で shipping_usd が order レベル値を
    # 全 txn に複製コピーするため、素朴に各 txn を add_sale すると 1 order N 商品で
    # shipping を N 倍重複計上 → 利益計算大狂い (ebay_client.py L1481-1512 で確認).
    # → order_id で group → qty 比按分が正解. eBay fee は GetOrders 戻り値に
    # 含まれず別 API (GetItemTransactions/GetAccount) で別 W に取得、本 W は 0 初期化.
    from monitor.database import add_sale
    from monitor.fulfillment_order_matcher import link_one_by_sale

    orders_by_id: dict[str, list] = {}
    for txn in orders:
        oid = txn.get("order_id")
        if oid:
            orders_by_id.setdefault(oid, []).append(txn)

    for order_id, txns in orders_by_id.items():
        try:
            # HIGH-1 (Codex/code-reviewer Phase D, 2026-05-22): paid_time 空注文は
            # 入れない. GetOrders は OrderStatus=Active (未払い 13 日以内) も返却し
            # PaidTime 空. sold_at='' で INSERT すると:
            #   (a) 商品管理 sold_at DESC 並び順で空文字列行が先頭固定 → W149 主目的破綻
            #   (b) matcher の sold_at <= confirmed_at が文字列比較で常に True →
            #       時系列ガード崩壊 (仕入が売却より先のケース誤マッチ)
            # 未払い注文は次回 polling で paid_time 入った後に取込 (UNIQUE 複合キーで
            # 衝突防止、re-polling で正しい sold_at で INSERT 成立).
            paid_time = txns[0].get("paid_time") or ""
            if not paid_time:
                logger.info(
                    f"order {order_id} paid_time 空 (未払い) → sales_history "
                    f"取込 skip (次回 polling で paid 後に取込)"
                )
                continue
            total_qty = sum(int(t.get("qty") or 1) for t in txns) or 1
            order_shipping = float(txns[0].get("shipping_usd") or 0.0)
            for txn in txns:
                qty = int(txn.get("qty") or 1)
                ship_share = (order_shipping * qty / total_qty) if total_qty > 0 else 0.0
                sid = add_sale(
                    ebay_item_id=txn.get("ebay_item_id") or "",
                    sku=txn.get("sku") or "",
                    title=txn.get("title") or "",
                    sold_price_usd=float(txn.get("item_price_usd") or 0.0),
                    sold_at=paid_time,
                    buyer_country=txn.get("buyer_country") or "",
                    shipping_cost_usd=ship_share,
                    ebay_fee_usd=0.0,
                    ebay_order_id=order_id,
                )
                if sid > 0:
                    sales_recorded += 1
                    # 過去失敗だった order が今回成功 → queue から remove
                    _clear_sales_history_fetch_failure(order_id)
                    # realtime ひも付け (この sale 対応の最古 unmatched fulfillment)
                    try:
                        matched_fid = link_one_by_sale(sid)
                        if matched_fid is not None:
                            fulfillment_links_realtime += 1
                    except sqlite3.Error as ee:
                        logger.warning(
                            f"link_one_by_sale failed sale_id={sid}: {ee}"
                        )
                elif sid == 0:
                    sales_skipped_dup += 1
        except (sqlite3.Error, KeyError, TypeError, ValueError) as e:
            sales_failures += 1
            _record_sales_history_fetch_failure(order_id, str(e), webhook)
            logger.warning(f"sales_history record failed order={order_id}: {e}")

    # W133 (2026-05-16): 在庫0 / sync 抑止 / sync 失敗 listing を 1 回まとめて
    # Discord 通知 (Q0 痕跡層の 1 つ). 商品呼称は title (ebay_item_id 末尾4桁) で、
    # SKU は表示しない (CLAUDE.md 商品呼称ルール). webhook は既存取得済変数を流用.
    if inventory_zero_listings:
        fields = []
        for d in inventory_zero_listings[:20]:  # embed field 上限保護
            label = f"{(d.get('title') or '(no title)')[:40]} "
            label += f"({str(d.get('ebay_item_id') or '')[-4:]})"
            if d.get("sync_skipped_zero_unsafe"):
                state = f"在庫{d.get('new_count')} / ⚠️ eBay反映抑止 (OOS未確認)"
            elif not d.get("sync_success", True):
                state = (
                    f"在庫{d.get('new_count')} / ❌ eBay反映失敗: "
                    f"{(d.get('sync_message') or '')[:80]}"
                )
            else:
                state = f"在庫{d.get('new_count')} / ✅ eBay反映済"
            fields.append({"name": label, "value": state, "inline": False})
        embed = {
            "title": "[在庫] 有在庫 listing が在庫0 / eBay反映に注意",
            "description": (
                f"{len(inventory_zero_listings)} 件の有在庫 listing が在庫0 化、"
                "または eBay 数量反映が抑止/失敗しました. 補充 or 出品停止を確認してください."
            ),
            "color": 0xD84C38,
            "fields": fields,
            "timestamp": datetime.now().isoformat(),
        }
        _send_discord(webhook, embed)

    duration = (datetime.now() - started_at).total_seconds()
    logger.info(
        f"order_alert_check: orders={len(orders)} hv_eu={high_value_alerts} "
        f"ddpb={ddpb_alerts} inv_dec={inventory_decrements} "
        f"memo_warn={memo_warnings} "
        f"inv_zero={len(inventory_zero_listings)} errors={order_processing_errors} "
        f"sales_rec={sales_recorded} sales_dup={sales_skipped_dup} "
        f"fol_realtime={fulfillment_links_realtime} sales_fail={sales_failures} "
        f"duration={duration:.1f}s"
    )
    return {
        "success": True,
        "orders_checked": len(orders),
        "high_value_eu_alerts": high_value_alerts,
        "ddpb_alerts": ddpb_alerts,
        "inventory_decrements": inventory_decrements,
        "memo_warnings": memo_warnings,
        "inventory_zero_listings": inventory_zero_listings,
        "order_processing_errors": order_processing_errors,
        "sales_recorded": sales_recorded,
        "sales_skipped_dup": sales_skipped_dup,
        "fulfillment_links_realtime": fulfillment_links_realtime,
        "sales_failures": sales_failures,
        "duration_sec": duration,
        "message": (
            f"orders={len(orders)} hv_eu={high_value_alerts} ddpb={ddpb_alerts} "
            f"inv_dec={inventory_decrements} memo_warn={memo_warnings} "
            f"sales_rec={sales_recorded} sales_dup={sales_skipped_dup} "
            f"fol_realtime={fulfillment_links_realtime} sales_fail={sales_failures} "
            f"errors={order_processing_errors}"
        ),
    }


if __name__ == "__main__":
    import sys
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    result = run_order_alert_check(cfg, num_days=1)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
