"""W314 商品仕上げパネル Phase1 S1 (2026-07-03): listing コンテンツ変更監査ログ.

設計書: .company/engineering/docs/2026-07-03-finishing-panel-design.md §6
(監査ログ API 契約 / migration v88)。

title/description/images/rank/quantity の revise 前後を before/after 全文で
DB に記録する (確定判断2: 復元可能性優先、DB 肥大は許容)。listing 識別は
`ebay_item_id` のみ (sku-rules.md 準拠、SKU は本モジュールで一切使わない)。

changed_at は SQL 側 `CURRENT_TIMESTAMP` (UTC) で採番する。Python
`datetime.now()` を bind しない (`.claude/rules/sqlite-timezone.md` 準拠)。
"""
from __future__ import annotations

import json
import logging

from monitor.database import get_conn

logger = logging.getLogger(__name__)

# field 許容値 (設計書 §6 API 契約 コメント準拠)。
# condition_description: #44 パネルの CD 反映で使用 (元 VALID_FIELDS 漏れで
# ValueError → 呼出側 try/except に飲まれ監査証跡欠落するバグを修正、2026-07-04)。
# item_specifics: #44 で ItemSpecifics 反映実装中のため先行追加。
VALID_FIELDS = frozenset({
    "title", "description", "images", "rank", "quantity",
    "condition_description", "item_specifics",
})


def _serialize(value):
    """list/tuple (images の URL 配列) は JSON 文字列化、それ以外は素通し.

    設計書: 「images はURL配列を JSON 文字列化」。field 分岐せず値の型で
    汎用的に判定する (list/tuple を渡すのは実質 images のみ、K1 simplicity)。
    """
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    return value


def log_content_change(
    ebay_item_id: str,
    field: str,
    before_value: str | list | None,
    after_value: str | list | None,
    *,
    source_tab: str | None = None,
    candidate_id: int | None = None,
    success: bool = False,
    ebay_ack: str | None = None,
) -> int:
    """listing_content_change_log に 1 行 INSERT し、採番された id を返す.

    Args:
        ebay_item_id: listing 識別キー (必須、SKU 不使用)
        field: 'title'|'description'|'images'|'rank'|'quantity'
        before_value / after_value: 変更前後の値 (全文保存)。images は
            URL 配列 (list[str]) を渡すと JSON 文字列化して保存する
        source_tab: 呼出元タブ名 (任意、監査用)
        candidate_id: 仕入先候補 id (任意)
        success: revise API 呼出の成否
        ebay_ack: eBay API の ack 応答 (任意)

    Raises:
        ValueError: ebay_item_id が空、または field が VALID_FIELDS 外
    """
    if not ebay_item_id:
        raise ValueError("ebay_item_id は必須です (空文字/None 不可)")
    if field not in VALID_FIELDS:
        raise ValueError(
            f"field='{field}' は不正です (許容値: {sorted(VALID_FIELDS)})"
        )

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO listing_content_change_log "
            "(ebay_item_id, field, before_value, after_value, "
            " source_tab, candidate_id, success, ebay_ack) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ebay_item_id,
                field,
                _serialize(before_value),
                _serialize(after_value),
                source_tab,
                candidate_id,
                1 if success else 0,
                ebay_ack,
            ),
        )
        return cur.lastrowid


def get_content_changes(ebay_item_id: str, limit: int = 50) -> list[dict]:
    """listing の変更履歴を新しい順で返す (将来の履歴表示用、最小限).

    UI 表示は呼出側の責務 (JST 換算等)。DB は UTC 保存 (sqlite-timezone.md)。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ebay_item_id, field, before_value, after_value, "
            "       source_tab, candidate_id, success, ebay_ack, changed_at "
            "FROM listing_content_change_log "
            "WHERE ebay_item_id=? "
            # CURRENT_TIMESTAMP は秒精度のため同一秒内の連続 INSERT で
            # changed_at が同値になり得る (tie)。id DESC を副ソートに
            # 加えて新しい順を決定的にする。
            "ORDER BY changed_at DESC, id DESC LIMIT ?",
            (ebay_item_id, limit),
        ).fetchall()
    return [
        {
            "id": r[0],
            "ebay_item_id": r[1],
            "field": r[2],
            "before_value": r[3],
            "after_value": r[4],
            "source_tab": r[5],
            "candidate_id": r[6],
            "success": bool(r[7]),
            "ebay_ack": r[8],
            "changed_at": r[9],
        }
        for r in rows
    ]
