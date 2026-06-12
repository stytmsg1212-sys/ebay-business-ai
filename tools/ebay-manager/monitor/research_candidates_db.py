#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W228 商品リサーチ自動化 フェーズB MVP: research_candidates CRUD.

設計書: .company/engineering/docs/2026-06-07-product-research-automation-spec.md
       .company/engineering/docs/2026-06-10-w229-w228-full-automation-design.md
ロック: PoC は「1 商品手入力 → フリマ探索 + AI 同一性判定 → 着地」だけ。
       出品 / キーワード監視登録 / 仕入購入は実装しない (§8 P2 / ビルド順 step1-2)。

ルール準拠:
  - listing 識別は独自 PK `rc_id` (P0-1 設計): SKU 規約 (sku-rules.md) は出品済 entity
    の話で、未出品 research_candidate は ebay_item_id を持たない (将来出品時に
    `ebay_listings.ebay_item_id` で別 entity に昇格させる構想)。
    → ここで ebay_item_id 列を作って sentinel `rc:<n>` を挿す案は W185 (UNIQUE
      ebay_item_id ベース) を汚染するので採らない。
  - status は state machine (P0-2): 未定義文字列を黙って受け入れない。`update_status`
    が遷移先を `_VALID_STATUSES` で validate (Q0 silent skip 防止: 不正値は ValueError)。
  - silent skip 防止 (Q0): `needs_review` に落とす時は `needs_review_reason` 必須。
  - K1 Simplicity First: 状態は設計書 §4-2 に準拠。既存 8 status + 新規 7 status。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .database import get_conn

logger = logging.getLogger(__name__)

# ---- 状態機械定数 (P0-2) ----------------------------------------------------
# 既存 8 status (v67 PoC スコープ、変更なし)。
STATUS_NEW = "new"
STATUS_SOURCING = "sourcing"
STATUS_SOURCED = "sourced"
STATUS_NOT_FOUND = "not_found"
STATUS_NEEDS_REVIEW = "needs_review"
# 手動 Wizard 承認系 (W228 後続 UI 拡張 — DB migration 不要、status 列は自由 TEXT)。
STATUS_IDENTITY_APPROVED = "identity_approved"   # 同一性 OK (人間承認)
STATUS_IDENTITY_REJECTED = "identity_rejected"   # 同一性 NG (人間却下)
STATUS_WATCH_REGISTERED = "watch_registered"     # キーワード新着監視に登録済

# 新規 7 status (設計書 §4-2 / W229 ハーベスト + 承認キュー経路)。
# 既存 status は一切変更しない (K2 後方互換)。
STATUS_HARVESTED = "harvested"               # Terapeak 発掘直後 (ゲート未判定) — Phase 2
STATUS_GATE_PASSED = "gate_passed"           # ゲート target_* 判定済・探索待ち — Phase 2
STATUS_GATE_REJECTED = "gate_rejected"       # ゲート reject_*/skip_* (非表示・再判定用) — Phase 2
STATUS_AWAITING_APPROVAL = "awaiting_approval"  # 探索+利益完了・承認キュー表示中 — Phase 3
STATUS_APPROVED = "approved"                 # user 承認 → 出品下書き生成へ — Phase 4
STATUS_DRAFT_GENERATED = "draft_generated"   # 出品下書き生成済 (個別出品 prefill 待ち) — Phase 4
STATUS_LISTED = "listed"                     # user が eBay 公開済 (終端) — Phase 4

_VALID_STATUSES: set[str] = {
    STATUS_NEW,
    STATUS_SOURCING,
    STATUS_SOURCED,
    STATUS_NOT_FOUND,
    STATUS_NEEDS_REVIEW,
    STATUS_IDENTITY_APPROVED,
    STATUS_IDENTITY_REJECTED,
    STATUS_WATCH_REGISTERED,
    # 新規 7 status
    STATUS_HARVESTED,
    STATUS_GATE_PASSED,
    STATUS_GATE_REJECTED,
    STATUS_AWAITING_APPROVAL,
    STATUS_APPROVED,
    STATUS_DRAFT_GENERATED,
    STATUS_LISTED,
}

# 許容遷移グラフ (既存 + 新規)。spec §8 P0-2 「明示的な状態機械」要件。
# 既存遷移は一切変更しない (K2)。新規遷移を末尾に追加。
#
# 既存:
#  new              → sourcing / needs_review (人手入力直後の異常終了)
#  sourcing         → sourced / not_found / needs_review (探索結果 3 分岐 = §2-C 表)
#  sourced          → needs_review / identity_approved / identity_rejected
#  not_found        → sourcing (再探索) / needs_review
#  needs_review     → sourcing (修正後再試行) / sourced (人手で利益確定し承認)
#                     / not_found / identity_approved / identity_rejected
#  identity_approved → watch_registered / needs_review (再検討)
#  identity_rejected → sourcing (別候補で再探索) / needs_review
#  watch_registered  → needs_review (監視解除・再検討)
#
# 新規 (§4-3):
#  harvested        → gate_passed / gate_rejected / needs_review
#  gate_passed      → sourcing / needs_review
#  gate_rejected    → (終端。skip_too_new のみ再判定で harvested 復帰可)
#  awaiting_approval → approved / gate_rejected (見送り) / needs_review
#  approved         → draft_generated / needs_review
#  draft_generated  → listed / needs_review
#  listed           → (終端)
#
# needs_review は全ての非終端 status から遷移可 (技術失敗 = needs_review 原則)。
# 追加 needs_review 遷移元を既存 dict に統合する。
# 値は frozenset で immutable 化 (test の意図しない mutate を防止)。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    # ---- 既存 (変更なし) ----
    # FIX-1 追加: new → gate_passed / gate_rejected (手動 Wizard 経路。
    #   insert → save_gate_decision(move_status=True) の fast-path で
    #   harvested 中間 status をスキップする)。
    STATUS_NEW: frozenset({
        STATUS_SOURCING, STATUS_NEEDS_REVIEW,
        STATUS_GATE_PASSED, STATUS_GATE_REJECTED,  # manual Wizard fast-path
    }),
    STATUS_SOURCING: frozenset(
        {STATUS_SOURCED, STATUS_NOT_FOUND, STATUS_NEEDS_REVIEW}
    ),
    STATUS_SOURCED: frozenset(
        {STATUS_NEEDS_REVIEW, STATUS_IDENTITY_APPROVED, STATUS_IDENTITY_REJECTED,
         STATUS_AWAITING_APPROVAL,  # 自動経路: sourced → awaiting_approval (Phase 3)
         STATUS_NOT_FOUND}  # 自動経路: match_score < floor = 別商品判定 → 仕入先未発見に降格
        # (2026-06-11 Phase 3 Q1 実機で発覚: evaluate_product は手動 UI 前提
        #  「保存のみ、最終確定は人間」§2-B のため score=0 でも sourced を返す。
        #  自動経路は supplier-matching-rules <60 除外を task 側で適用する)
    ),
    STATUS_NOT_FOUND: frozenset(
        {STATUS_SOURCING, STATUS_NEEDS_REVIEW,
         STATUS_AWAITING_APPROVAL}  # 在庫0+過去取引ありで監視候補
    ),
    STATUS_NEEDS_REVIEW: frozenset(
        {STATUS_SOURCING, STATUS_SOURCED, STATUS_NOT_FOUND,
         STATUS_IDENTITY_APPROVED, STATUS_IDENTITY_REJECTED,
         STATUS_HARVESTED, STATUS_GATE_PASSED}  # 技術失敗の再試行経路
    ),
    STATUS_IDENTITY_APPROVED: frozenset({STATUS_WATCH_REGISTERED, STATUS_NEEDS_REVIEW}),
    STATUS_IDENTITY_REJECTED: frozenset({STATUS_SOURCING, STATUS_NEEDS_REVIEW}),
    STATUS_WATCH_REGISTERED: frozenset({STATUS_NEEDS_REVIEW}),
    # ---- 新規 (§4-3) ----
    STATUS_HARVESTED: frozenset(
        {STATUS_GATE_PASSED, STATUS_GATE_REJECTED, STATUS_NEEDS_REVIEW}
    ),
    STATUS_GATE_PASSED: frozenset({STATUS_SOURCING, STATUS_NEEDS_REVIEW}),
    STATUS_GATE_REJECTED: frozenset({STATUS_HARVESTED}),  # skip_too_new 再判定のみ
    STATUS_AWAITING_APPROVAL: frozenset(
        {STATUS_APPROVED, STATUS_GATE_REJECTED, STATUS_NEEDS_REVIEW}
    ),
    STATUS_APPROVED: frozenset({
        STATUS_DRAFT_GENERATED, STATUS_NEEDS_REVIEW,
        STATUS_WATCH_REGISTERED,  # watch-only 承認 (found_url 無し監視候補) の終端
    }),
    STATUS_DRAFT_GENERATED: frozenset({STATUS_LISTED, STATUS_NEEDS_REVIEW}),
    STATUS_LISTED: frozenset(),  # 終端
}


def can_transition(old: str, new: str) -> bool:
    """状態機械: old → new 遷移が許容されるか (同値 no-op は False)."""
    return new in _ALLOWED_TRANSITIONS.get(old, frozenset())


# ---- CRUD ------------------------------------------------------------------

def insert_research_candidate(
    title_ja: str,
    *,
    manual_weight_g: Optional[float] = None,
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    terapeak_avg_price_usd: Optional[float] = None,
    harvest_pattern: Optional[str] = None,
    status: str = STATUS_NEW,
) -> int:
    """research_candidate を 1 件作成して rc_id を返す.

    探索前の "手入力済" 状態 (status=new)。フリマ探索後に
    `update_research_candidate_result` で found_* / match_* / estimated_profit_usd
    と status (sourced/not_found/needs_review) を埋める。

    Q0: title_ja は必須 (空/None は ValueError)。後段で「無題で needs_review」と
    silent に落とす穴を最初から塞ぐ。

    harvest_pattern: W229 ハーベスト由来の収穫パターン識別子 (設計書 §5-0 Q10)。
        'fresh_24h' = Last 7d 新着順 / 'two_year_echo' = Last 2y 古い順 / None = 手動入力。
        Phase 2 (task_research_harvest) からのみ非 None 値が渡る予定。
    """
    if not title_ja or not title_ja.strip():
        raise ValueError("title_ja is required (empty not allowed)")
    # Codex 2段指摘#2: insert が任意 valid status を受け付けると、状態機械を迂回して
    # needs_review (reason 必須) や sourced を直接作れてしまう。insert は **必ず初期状態
    # 'new'** のみ。以降の遷移は update_status (遷移検証 + reason 必須) を通す。
    if status != STATUS_NEW:
        raise ValueError(
            f"insert_research_candidate only creates status='new' (got {status!r}); "
            "use update_status for transitions (state-machine integrity / Q0)"
        )

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO research_candidates "
            "(title_ja, manual_weight_g, length_cm, width_cm, height_cm, "
            " terapeak_avg_price_usd, harvest_pattern, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title_ja.strip(),
                manual_weight_g,
                length_cm,
                width_cm,
                height_cm,
                terapeak_avg_price_usd,
                harvest_pattern,
                status,
            ),
        )
        rc_id = cur.lastrowid
    if rc_id is None:
        # SQLite AUTOINCREMENT で lastrowid=None は理論上ありえないが Q0 防御。
        raise RuntimeError("insert_research_candidate: lastrowid is None")
    return int(rc_id)


def get_research_candidate(rc_id: int) -> Optional[dict]:
    """rc_id で 1 件取得 (見つからなければ None)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM research_candidates WHERE rc_id=?", (rc_id,)
        ).fetchone()
    return dict(row) if row else None


def update_input_snapshot(
    rc_id: int,
    *,
    manual_weight_g: Optional[float] = None,
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    terapeak_avg_price_usd: Optional[float] = None,
) -> None:
    """手入力スナップショット列を上書きする (FIX-A 再利用パス用).

    evaluate_product が gate 経由の既存行を再利用する場合 INSERT が走らないため、
    今回の評価に使った入力値 (terapeak / 重量 / 寸法) が行に残らない
    (2026-06-10 Q1 実機 rc_id=10 で発覚)。承認キューは「Terapeak 売れ行きと
    利益額を見て承認」が前提のため、評価に使った値をそのまま書き戻す。
    None も verbatim に書く = 行は常に「最後の評価の入力」を反映し、
    利益値と入力値の不整合 (古い terapeak × 新しい利益) を作らない。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE research_candidates SET manual_weight_g=?, length_cm=?, "
            "width_cm=?, height_cm=?, terapeak_avg_price_usd=? WHERE rc_id=?",
            (
                manual_weight_g,
                length_cm,
                width_cm,
                height_cm,
                terapeak_avg_price_usd,
                rc_id,
            ),
        )
        if cur.rowcount == 0:
            # Q0: 存在しない rc_id への silent no-op を防ぐ
            raise ValueError(f"update_input_snapshot: rc_id={rc_id} not found")


def list_research_candidates(
    status: Optional[str] = None, limit: int = 200
) -> list[dict]:
    """status で絞り込んで新しい順に返す. status=None で全件 (limit 上限)."""
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(f"invalid status filter: {status!r}")
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM research_candidates WHERE status=? "
                "ORDER BY created_at DESC, rc_id DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM research_candidates "
                "ORDER BY created_at DESC, rc_id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    return [dict(r) for r in rows]


def _apply_status_in_conn(
    conn,
    rc_id: int,
    new_status: str,
    needs_review_reason: Optional[str],
) -> bool:
    """渡された conn 上で status 遷移を実行 (検証 + compare-and-swap).

    Codex 2段指摘#3/#4 反映:
      - #3 原子性: 呼出側が結果列 UPDATE と同一 conn (= 同一 transaction) で本関数を
        呼ぶことで、遷移検証で raise した時に結果列 UPDATE も rollback される。
      - #4 race: `WHERE rc_id=? AND status=?` (読んだ old_status を条件) + rowcount==1
        で compare-and-swap。並行で status が変わっていたら後勝ち更新せず False。
    不正遷移 / 不正値 / reason 欠落は ValueError (Q0 silent skip 禁止)。
    """
    if new_status not in _VALID_STATUSES:
        raise ValueError(f"invalid new_status: {new_status!r}")
    if new_status == STATUS_NEEDS_REVIEW and not (
        needs_review_reason and needs_review_reason.strip()
    ):
        raise ValueError(
            "needs_review_reason is required when transitioning to 'needs_review' "
            "(Q0 silent skip 防止)"
        )
    row = conn.execute(
        "SELECT status FROM research_candidates WHERE rc_id=?", (rc_id,)
    ).fetchone()
    if not row:
        return False
    old_status = row[0]
    if old_status == new_status:
        return False  # 同値 no-op
    if not can_transition(old_status, new_status):
        raise ValueError(
            f"transition not allowed: {old_status} -> {new_status}"
        )
    if needs_review_reason is not None:
        cur = conn.execute(
            "UPDATE research_candidates SET status=?, needs_review_reason=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE rc_id=? AND status=?",
            (new_status, needs_review_reason.strip(), rc_id, old_status),
        )
    else:
        cur = conn.execute(
            "UPDATE research_candidates SET status=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE rc_id=? AND status=?",
            (new_status, rc_id, old_status),
        )
    # rowcount != 1 = 並行で status が変わった (compare-and-swap 失敗)。後勝ちしない。
    return cur.rowcount == 1


def update_status(
    rc_id: int,
    new_status: str,
    *,
    needs_review_reason: Optional[str] = None,
) -> bool:
    """status 遷移. 不正遷移は ValueError (Q0 silent skip 禁止).

    needs_review に落とす場合は `needs_review_reason` を **必須**。
    付帯情報なしの needs_review = どこで詰まったか追跡不能 = silent skip と等価。
    """
    with get_conn() as conn:
        return _apply_status_in_conn(conn, rc_id, new_status, needs_review_reason)


def update_research_candidate_result(
    rc_id: int,
    *,
    found_url: Optional[str] = None,
    found_price_jpy: Optional[int] = None,
    found_condition_ja: Optional[str] = None,
    match_score: Optional[int] = None,
    match_reason: Optional[str] = None,
    estimated_profit_usd: Optional[float] = None,
    new_status: Optional[str] = None,
    needs_review_reason: Optional[str] = None,
) -> bool:
    """探索結果を一括書き込み + 必要なら status 遷移.

    `new_status` を渡せば `update_status` 経由で遷移検証付きで書く。
    status 遷移を伴わない部分 update も許容 (例: 追加 review メモ反映)。
    """
    if rc_id is None:
        raise ValueError("rc_id is required")

    sets: list[str] = []
    params: list = []
    if found_url is not None:
        sets.append("found_url=?")
        params.append(found_url)
    if found_price_jpy is not None:
        sets.append("found_price_jpy=?")
        params.append(int(found_price_jpy))
    if found_condition_ja is not None:
        sets.append("found_condition_ja=?")
        params.append(found_condition_ja)
    if match_score is not None:
        sets.append("match_score=?")
        params.append(int(match_score))
    if match_reason is not None:
        sets.append("match_reason=?")
        params.append(match_reason)
    if estimated_profit_usd is not None:
        sets.append("estimated_profit_usd=?")
        params.append(float(estimated_profit_usd))

    if not sets and new_status is None:
        # 何も書かないなら no-op (caller のロジックバグ可能性を Q0 で表面化)。
        logger.warning(
            "update_research_candidate_result: no fields to update "
            f"(rc_id={rc_id})"
        )
        return False

    # Codex 2段指摘#3: 結果列 UPDATE と status 遷移を **同一 conn (同一 transaction)** で
    # 実行。遷移検証で raise したら結果列 UPDATE も rollback され、found_url/profit だけ
    # 先に commit される半端な状態を作らない (金銭直結の原子性)。
    with get_conn() as conn:
        if sets:
            sets.append("updated_at=CURRENT_TIMESTAMP")
            params.append(rc_id)
            conn.execute(
                f"UPDATE research_candidates SET {', '.join(sets)} "
                "WHERE rc_id=?",
                params,
            )
        if new_status is not None:
            # 不正遷移 / reason 欠落 = ValueError → with を抜けて rollback。
            _apply_status_in_conn(
                conn, rc_id, new_status, needs_review_reason
            )

    return True


# ---- Phase 1 FIX-1: ゲート判定永続化 -----------------------------------------

def save_gate_decision(
    rc_id: int,
    decision: str,
    reason: str,
    inputs_dict: dict[str, Any],
    *,
    move_status: bool = True,
) -> bool:
    """ゲート判定結果を research_candidates に永続化する (FIX-1).

    設計書 §3-2 / §4-3 準拠:
      - gate_decision / gate_reason / gate_inputs_json / gated_at(UTC) を書く。
      - move_status=True (既定) の場合、decision に応じ status を遷移する:
          target_instock / target_oos_watch → gate_passed
          reject_deadstock / skip_too_new / reject_no_demand → gate_rejected
      - move_status=False の場合、gate_* 列のみ書き status は動かさない
        (手動 Wizard 経路: 既存 status='new'/'sourcing' の候補に gate 情報を後付け保存)。

    Q0 silent skip 防止:
      - decision / reason は必須 (空なら ValueError)。
      - status 遷移失敗は ValueError を伝播 (呼び出し元でハンドル)。

    Args:
        rc_id: research_candidates の PK。
        decision: evaluate_sourcing_gate の返す decision 値 (DECISION_* 定数)。
        reason: evaluate_sourcing_gate の返す reason 文字列 (人間可読)。
        inputs_dict: スナップショット (sold_90d, has_active_listing 等)。JSON 化して保存。
        move_status: True なら status も遷移する (既定)。False は gate_* 列のみ。

    Returns:
        True = 書込成功 (status 遷移成功 or move_status=False で列書込成功)。
        False = rc_id が存在しない。
    """
    if not decision or not decision.strip():
        raise ValueError("decision is required for save_gate_decision (Q0)")
    if not reason or not reason.strip():
        raise ValueError("reason is required for save_gate_decision (Q0)")

    # target_* → gate_passed / それ以外 → gate_rejected
    from monitor.research_gate import (
        DECISION_TARGET_INSTOCK,
        DECISION_TARGET_OOS_WATCH,
    )
    if decision in {DECISION_TARGET_INSTOCK, DECISION_TARGET_OOS_WATCH}:
        target_status = STATUS_GATE_PASSED
    else:
        target_status = STATUS_GATE_REJECTED

    inputs_json = json.dumps(inputs_dict, ensure_ascii=False)

    with get_conn() as conn:
        # rc_id 実在確認
        row = conn.execute(
            "SELECT rc_id FROM research_candidates WHERE rc_id=?", (rc_id,)
        ).fetchone()
        if not row:
            return False

        # gate_* 列を書く (updated_at も更新)
        conn.execute(
            "UPDATE research_candidates "
            "SET gate_decision=?, gate_reason=?, gate_inputs_json=?, "
            "    gated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
            "WHERE rc_id=?",
            (decision.strip(), reason.strip(), inputs_json, rc_id),
        )

        # status 遷移 (move_status=True の場合のみ)
        if move_status:
            # 現在 status を読んで遷移可能か確認してから CAS
            _apply_status_in_conn(conn, rc_id, target_status, needs_review_reason=None)

    return True


# ---- Phase 1 FIX-2: 利益真値 + けいすけ基準の保存 ----------------------------

def save_profit_true(
    rc_id: int,
    profit_jpy_true: Optional[int],
    profit_usd_true: Optional[float],
    keisuke_pass: bool,
    keisuke_detail_json: str,
) -> bool:
    """calculator.calculate の真値利益 + けいすけ基準合否を永続化する (FIX-2).

    設計書 §3-2 / §14-Q1 準拠:
      - profit_jpy_true: calculator.calculate の profit (円, 還付抜き)。
        weight 欠落等で計算不能なら None を渡す (0 clip 禁止 / P1-1)。
      - profit_usd_true: profit_jpy_true / fx (表示用 USD)。None 時は NULL 保存。
      - keisuke_pass: けいすけ基準合否 (True/False → 1/0)。
      - keisuke_detail_json: keisuke_check の返り値を JSON 文字列化したもの。

    Q0: rc_id が存在しない場合は False を返す (ValueError は raise しない)。
        profit_jpy_true=None は許容 (weight 欠落 = NULL 保存)。

    Returns:
        True = 書込成功。False = rc_id 不在。
    """
    if rc_id is None:
        raise ValueError("rc_id is required for save_profit_true")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT rc_id FROM research_candidates WHERE rc_id=?", (rc_id,)
        ).fetchone()
        if not row:
            return False

        conn.execute(
            "UPDATE research_candidates "
            "SET profit_jpy_true=?, profit_usd_true=?, keisuke_pass=?, "
            "    keisuke_detail_json=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE rc_id=?",
            (
                int(profit_jpy_true) if profit_jpy_true is not None else None,
                float(profit_usd_true) if profit_usd_true is not None else None,
                1 if keisuke_pass else 0,
                keisuke_detail_json,
                rc_id,
            ),
        )
    return True


def record_listing_draft(rc_id: int, draft_id: int) -> bool:
    """出品下書き ID を research_candidates.listing_draft_id に記録する (Phase 4).

    Args:
        rc_id: research_candidates の PK。
        draft_id: listing_drafts の id (save_listing_draft の返り値)。

    Returns:
        True = 書込成功。False = rc_id 不在。

    Q0: rc_id / draft_id が不正なら ValueError。rowcount=0 は rc_id 不在の証拠。
    """
    if rc_id is None:
        raise ValueError("rc_id is required for record_listing_draft")
    if draft_id is None:
        raise ValueError("draft_id is required for record_listing_draft")

    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE research_candidates "
            "SET listing_draft_id=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE rc_id=?",
            (int(draft_id), rc_id),
        )
    if cur.rowcount == 0:
        logger.warning("record_listing_draft: rc_id=%s not found", rc_id)
        return False
    return True


def record_watch_ids(rc_id: int, watch_ids: list[int]) -> bool:
    """登録した keyword_watches.id のリストを watch_ids_json に記録する (Phase 4).

    Args:
        rc_id: research_candidates の PK。
        watch_ids: keyword_watches.id のリスト。

    Returns:
        True = 書込成功。False = rc_id 不在。
    """
    if rc_id is None:
        raise ValueError("rc_id is required for record_watch_ids")

    watch_ids_json = json.dumps(watch_ids, ensure_ascii=False)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE research_candidates "
            "SET watch_ids_json=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE rc_id=?",
            (watch_ids_json, rc_id),
        )
    if cur.rowcount == 0:
        logger.warning("record_watch_ids: rc_id=%s not found", rc_id)
        return False
    return True


def clear_profit_fields(rc_id: int) -> bool:
    """利益関連カラムを NULL に戻す (match_score < floor の別商品判定時).

    誤マッチ商品の価格で計算された利益真値は虚偽数値 (verify_numbers 違反源)
    のため、not_found 降格時に必ず無効化する。found_url / match_score /
    match_reason は「この候補を評価して棄却した」監査痕跡として残す。

    Returns:
        True = 書込成功。False = rc_id 不在。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rc_id FROM research_candidates WHERE rc_id=?", (rc_id,)
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE research_candidates "
            "SET profit_jpy_true=NULL, profit_usd_true=NULL, keisuke_pass=0, "
            "    keisuke_detail_json='{}', estimated_profit_usd=NULL, "
            "    updated_at=CURRENT_TIMESTAMP "
            "WHERE rc_id=?",
            (rc_id,),
        )
    return True


def clear_found_fields(rc_id: int) -> bool:
    """誤マッチ仕入先フィールドを NULL に戻す (監視候補として承認キュー再投入時).

    match_score < floor の行を not_found 終端に置く場合は found_url を監査痕跡と
    して残すが (clear_profit_fields docstring 参照)、承認キュー (awaiting_approval)
    に戻す行に残すと承認 UI (tab_w228_research) が found_url / found_price_jpy を
    仕入先としてそのまま下書き生成に消費し、誤商品 URL・虚偽原価の draft が生まれる
    (2026-06-12 retrospective review H1: rc 36 / draft 26 で実発生)。
    監査痕跡は match_score / match_reason / gate_inputs_json で維持する。

    Returns:
        True = 書込成功。False = rc_id 不在。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rc_id FROM research_candidates WHERE rc_id=?", (rc_id,)
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE research_candidates "
            "SET found_url=NULL, found_price_jpy=NULL, found_condition_ja=NULL, "
            "    updated_at=CURRENT_TIMESTAMP "
            "WHERE rc_id=?",
            (rc_id,),
        )
    return True
