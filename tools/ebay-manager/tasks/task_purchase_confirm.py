"""W133 (2026-05-16): 有在庫の入荷 (仕入確認) ロジック.

仕入入荷を知らせるメール本文から、対象 listing 候補を類似度で提示し、
user が listing + 仕入個数を **手動確定** すると inventory_count を加算する.

設計方針:
  - Streamlit 非依存の純ロジック (pytest 可能、UI は tab_purchase_confirm.py).
  - listing 識別は **必ず ebay_item_id** (sku-rules.md 準拠). SKU は
    `WHERE sku LIKE 'stock%'` の集合フィルタ (有在庫判定) にのみ使用、
    listing の特定 / 集約 / 辞書キー / JOIN には一切使わない.
  - 類似度は標準ライブラリ difflib.SequenceMatcher.ratio() (rapidfuzz 未導入、
    Claude API は過剰 & コストのため不使用 — W133 Phase 0 決定).
  - 二重加算ガード: purchase_confirmation_log の UNIQUE(gmail_id, ebay_item_id)
    + INSERT OR IGNORE の rowcount で「既処理」を検出.
  - **自動確定は絶対しない** (user の明示操作のみ inventory を増やす).
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SIM_THRESHOLD = 0.55
# 2026-05-20 user 緊急要望 / Codex UX 推奨: top-1 自動推薦 threshold。
# これ以上なら「ワンクリック確定」ボタンをハイライト (user 労力最小化)。
# 0.72 = Codex 推奨値 (similarity の経験則: 0.70 以下は別商品候補リスク高)。
TOP1_AUTO_RECOMMEND_THRESHOLD = 0.72


def _similarity(a: str, b: str) -> float:
    """0.0-1.0 の類似スコア (大文字小文字無視, 標準 difflib)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def extract_purchase_qty(email_text: str) -> Optional[int]:
    """2026-05-20: 購入確認メール本文から数量を自動抽出 (Codex UX 推奨 #1)。

    パターン: 「個数 1」「数量: 2」「2 個」「Qty 3」 etc。
    複数 hit 時は最初のもの (subject に近い順)。
    1-999 の常識範囲外 (negative/0/1000+) は無効として None。

    Q0: 失敗時 None (UI 側 default=1 にフォールバック)。誤抽出は user が
    number_input で上書き可能 = 不可逆 risk なし。
    """
    if not email_text:
        return None
    # 優先度高い順 (specific → generic)
    patterns = (
        r'個数\s*[:：]?\s*(\d+)',
        r'数量\s*[:：]?\s*(\d+)',
        r'数\s*[:：]\s*(\d+)',
        r'\bQty\.?\s*[:：]?\s*(\d+)',
        r'\bquantity\s*[:：]?\s*(\d+)',
        r'(\d+)\s*個',
        r'(\d+)\s*点',
        r'(\d+)\s*pcs',
    )
    for pat in patterns:
        m = re.search(pat, email_text, flags=re.IGNORECASE)
        if m:
            try:
                v = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= v <= 999:
                return v
    return None


def suggest_listings(
    email_text: str,
    top: int = 5,
    threshold: float = DEFAULT_SIM_THRESHOLD,
) -> list[dict]:
    """メール本文に近い有在庫 listing 候補を score 降順で返す.

    Args:
        email_text: 入荷メール本文 (件名 + 本文を連結して渡す想定).
        top: 返す候補数上限.
        threshold: これ未満の score は low_confidence:True フラグを付ける
            (除外はしない — user が最終判断する).

    Returns:
        [{ebay_item_id, sku, title, inventory_count, score, low_confidence}, ...]
        score 降順. ebay_item_id 単位 (SKU で束ねない).
    """
    from monitor.database import get_conn

    text = (email_text or "").strip()
    with get_conn() as c:
        # 集合フィルタ: 有在庫 (stock prefix) listing のみ対象.
        # listing 識別は ebay_item_id (SKU は種別フラグとしてのみ使用).
        rows = c.execute(
            """SELECT ebay_item_id, sku, title, inventory_count
               FROM ebay_listings
               WHERE sku LIKE 'stock%'
                 AND (is_ended IS NULL OR is_ended = 0)
                 AND title IS NOT NULL AND title != ''"""
        ).fetchall()

    if not text:
        return []

    scored = []
    for r in rows:
        title = r["title"] or ""
        score = _similarity(text, title)
        scored.append(
            {
                "ebay_item_id": r["ebay_item_id"],
                "sku": r["sku"],
                "title": title,
                "inventory_count": r["inventory_count"],
                "score": round(score, 4),
                "low_confidence": score < threshold,
            }
        )

    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[: max(0, int(top))]


def confirm_purchase(gmail_id: str, ebay_item_id: str, qty: int) -> dict:
    """user が確定した仕入個数を inventory_count に加算する.

    二重加算ガード: purchase_confirmation_log の UNIQUE(gmail_id, ebay_item_id)
    に INSERT OR IGNORE → rowcount==0 なら既処理として何もしない.

    Returns dict:
        success      : bool
        already      : bool        (既に同 gmail×listing で確定済 = 二重加算回避)
        ebay_item_id  : str
        quantity_added: int
        old_count     : int | None
        new_count     : int | None
        sync_success  : bool        (eBay 数量反映の結果)
        message       : str
    """
    from monitor.database import get_conn

    result = {
        "success": False,
        "already": False,
        "ebay_item_id": ebay_item_id,
        "quantity_added": 0,
        "old_count": None,
        "new_count": None,
        "sync_success": False,
        "message": "",
    }

    qty = int(qty)
    if qty <= 0:
        result["message"] = "仕入個数は 1 以上で指定してください"
        return result
    if not gmail_id or not ebay_item_id:
        result["message"] = "gmail_id / ebay_item_id が空です"
        return result

    with get_conn() as c:
        # 在庫種別フラグ確認 (有在庫 listing のみ加算対象).
        row = c.execute(
            "SELECT sku, inventory_count FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        if row is None:
            result["message"] = (
                f"ebay_item_id={ebay_item_id} が ebay_listings に無い"
            )
            return result
        sku = row["sku"] or ""
        if not sku.startswith("stock"):
            result["message"] = (
                "無在庫 SKU (stock prefix でない) は仕入確認対象外です"
            )
            return result

        # 二重加算ガード: claim を先に取る (check-then-act race 排除).
        cur = c.execute(
            """INSERT OR IGNORE INTO purchase_confirmation_log
               (gmail_id, ebay_item_id, sku, quantity_added,
                old_inventory_count, new_inventory_count, ebay_qty_sync_ok)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (gmail_id, ebay_item_id, sku, qty, None, None),
        )
        if cur.rowcount == 0:
            result["already"] = True
            result["message"] = (
                "この入荷メール×listing は既に確定済 (二重加算を防止しました)"
            )
            return result

        # lost-update 防止 (HIGH-1, code-review 2026-05-16): stale な Python 側
        # read を base にせず SQL 内で atomic 加算 (並行 decrement と整合).
        c.execute(
            "UPDATE ebay_listings "
            "SET inventory_count = COALESCE(inventory_count, 0) + ? "
            "WHERE ebay_item_id=?",
            (qty, ebay_item_id),
        )
        new_row = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        new_count = int(new_row["inventory_count"])
        old_int = new_count - qty
        c.execute(
            """UPDATE purchase_confirmation_log
               SET old_inventory_count=?, new_inventory_count=?
               WHERE gmail_id=? AND ebay_item_id=?""",
            (old_int, new_count, gmail_id, ebay_item_id),
        )

    result["quantity_added"] = qty
    result["old_count"] = old_int
    result["new_count"] = new_count

    # eBay へ数量反映 (listing 識別 ebay_item_id, SKU 不使用).
    from monitor import inventory_sync
    sync_res = inventory_sync.sync_listing_quantity(ebay_item_id)
    result["sync_success"] = bool(sync_res.get("success"))

    with get_conn() as c:
        c.execute(
            """UPDATE purchase_confirmation_log
               SET ebay_qty_sync_ok=?
               WHERE gmail_id=? AND ebay_item_id=?""",
            (1 if result["sync_success"] else 0, gmail_id, ebay_item_id),
        )

    result["success"] = True
    if result["sync_success"]:
        result["message"] = (
            f"在庫 {old_int} → {new_count} に加算、eBay 数量も反映しました"
        )
    else:
        # 加算は成功、eBay 反映だけ失敗 → 偽装成功にしない (痕跡を残す).
        result["message"] = (
            f"在庫 {old_int} → {new_count} に加算しましたが、eBay 数量反映に"
            f"失敗しました: {sync_res.get('message') or '不明'}"
            " (商品管理タブで再反映できます)"
        )
    return result


def undo_purchase(gmail_id: str, ebay_item_id: str) -> dict:
    """直前の confirm_purchase を取り消す (最小取消).

    対象 log 行の quantity_added を inventory_count から引き戻し、log 行を削除
    (= 再度 confirm 可能に戻す). 負値ガード (max(0, ...)).

    Returns dict: success / ebay_item_id / restored_count / message
    """
    from monitor.database import get_conn

    result = {
        "success": False,
        "already": False,
        "ebay_item_id": ebay_item_id,
        "restored_count": None,
        "sync_success": False,
        "message": "",
    }
    if not gmail_id or not ebay_item_id:
        result["message"] = "gmail_id / ebay_item_id が空です"
        return result

    with get_conn() as c:
        log = c.execute(
            """SELECT quantity_added FROM purchase_confirmation_log
               WHERE gmail_id=? AND ebay_item_id=?""",
            (gmail_id, ebay_item_id),
        ).fetchone()
        if log is None:
            # 既に取消済 / 未確定 = benign no-op (二重 undo 防止の一経路).
            # already=True で UI は error(赤) でなく warning(穏当) 表示にする.
            result["already"] = True
            result["message"] = (
                "取消対象の確定履歴が見つかりません (既に取消済か未確定)"
            )
            return result
        qty = int(log["quantity_added"] or 0)

        # 原子的 claim (F3, Codex 2026-05-16): DELETE を先に実行し rowcount で
        # 所有権を確定 → 二重 undo (UI 二度押し/並行) の二重減算を物理排除.
        # 在庫 mutation は claim 成立後のみ (loser は在庫を一切触らず abort).
        delc = c.execute(
            """DELETE FROM purchase_confirmation_log
               WHERE gmail_id=? AND ebay_item_id=?""",
            (gmail_id, ebay_item_id),
        )
        if delc.rowcount == 0:
            result["already"] = True
            result["message"] = "既に取消済みです (二重取消を防止しました)"
            return result

        # claim 成立 → SQL atomic 減算 (lost-update 防止 + 負値ガード MAX(0,...)).
        c.execute(
            "UPDATE ebay_listings "
            "SET inventory_count = MAX(0, COALESCE(inventory_count, 0) - ?) "
            "WHERE ebay_item_id=?",
            (qty, ebay_item_id),
        )
        restored_row = c.execute(
            "SELECT inventory_count FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        restored = (
            int(restored_row["inventory_count"]) if restored_row is not None else 0
        )

    result["restored_count"] = restored

    # eBay へ取消後の数量を反映.
    from monitor import inventory_sync
    sync_res = inventory_sync.sync_listing_quantity(ebay_item_id)
    result["sync_success"] = bool(sync_res.get("success"))
    result["success"] = True  # DB 取消自体は成立 (claim + 減算 完了)
    if result["sync_success"]:
        result["message"] = (
            f"取消しました (在庫 → {restored}、eBay 数量も反映)"
        )
    else:
        # 偽装成功にしない (F2): DB 取消は成功・eBay 反映のみ失敗を明示.
        result["message"] = (
            f"取消しました (在庫 → {restored})。eBay 数量反映に失敗: "
            f"{sync_res.get('message') or '不明'} (商品管理タブで再反映できます)"
        )
    return result
