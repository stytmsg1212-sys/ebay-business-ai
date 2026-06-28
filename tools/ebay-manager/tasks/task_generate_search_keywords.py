#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W119 Step 3: Anthropic Messages Batches API で全 listing の検索ワードを一括生成.

設計核心:
  - **Opus 4.8 batch** で 50% off + 1 回限りの cost (~$3/580 listings).
  - **未生成 listing のみ対象** (search_keyword IS NULL). 再生成は force_all=True.
  - **errored は NULL のまま** (DLQ 不要). UI で user が手動編集可能.
  - **prompt template は user 編集可能** (file 上部の SEARCH_KEYWORD_PROMPT 定数).

呼出経路:
  - MonoDeck 最安値チェックタブ → 商品リサーチ wizard Step 3
    → "🔑 検索ワード一括生成 (Opus 4.8 batch)" ボタン

詳細: `data/system_improvements.json` id=203 / W119 entry.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import anthropic

from monitor.database import get_conn

logger = logging.getLogger(__name__)


# Opus 4.8 model ID (1M context). batch API は別枠で 1 日 30 calls 制約に該当しない.
KEYWORD_MODEL = "claude-opus-4-8"

# Batch API SLA: 公式 24h、実測大半 1h 以内.
_DEFAULT_POLL_INTERVAL_SEC = 60
_DEFAULT_HARD_TIMEOUT_SEC = 4 * 3600  # 4h

# 1 listing あたり最大トークン数. 検索ワードは短いので 30 で十分.
_MAX_TOKENS = 30


# =============================================================================
# Prompt template (user 編集可能)
# =============================================================================
# eBay 越境 EC seller の domain knowledge に基づき、user が直接 prompt を調整できる.
# 5/10 W119 設計時点の暫定 prompt (Phase 5b 着手時に user に書き直してもらう想定):

SEARCH_KEYWORD_PROMPT = """You are an eBay search keyword extractor for a Japan→US cross-border seller.

Given an eBay listing title, output the optimal short keyword that finds similar competitor listings on eBay.

# Rules
- Output Brand + Model number (most important for narrowing results)
  - Example: "maxell MXCP-P100" / "Audio-Technica ATH-CKS330NC" / "SONY WM-DD9"
- Skip filler words: condition (NEW/Used/Mint), color, year, packaging, region tags ("from Japan", "F/S")
- If brand is missing, output the most distinctive product identifier
- Output a single line, words separated by spaces (URL-friendly, will be URL-encoded later)
- Do NOT add quotes, prefixes, explanations, or trailing punctuation

# Title
{title}

# Output"""


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class KeywordItem:
    """1 件分の入力."""
    ebay_item_id: str
    title: str

    @property
    def custom_id(self) -> str:
        """batch API custom_id. ebay_item_id 自体を使う (一意保証).
        ebay_item_id は 12 桁の数字なので custom_id 制約 (1-64 文字) を満たす."""
        return f"w119-{self.ebay_item_id}"


@dataclass
class KeywordBatchResult:
    """batch 全体の集計."""
    batch_id: str
    submitted: int = 0
    succeeded: int = 0  # search_keyword 取得成功
    errored: int = 0  # batch errored or parse error (DB は NULL のまま)
    duration_sec: float = 0.0
    timeout: bool = False
    error_message: Optional[str] = None


# =============================================================================
# Client / batch construction
# =============================================================================

def _get_client() -> Optional[anthropic.Anthropic]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _build_batch_request(item: KeywordItem, model: str) -> dict:
    """1 件分の batch request. cache_control なし (各 listing で title 異なるため)."""
    prompt_text = SEARCH_KEYWORD_PROMPT.format(title=item.title)
    return {
        "custom_id": item.custom_id,
        "params": {
            "model": model,
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt_text}],
        },
    }


def _parse_keyword_from_response(message) -> Optional[str]:
    """Claude response の text を取り出して trim → search_keyword として返す.
    空文字 / 異常 token なら None.
    """
    try:
        if not hasattr(message, "content") or not message.content:
            return None
        text_block = message.content[0]
        if not hasattr(text_block, "text"):
            return None
        kw = text_block.text.strip()
        # 末尾の句読点除去, クォート除去
        kw = kw.strip(' "\'.,!?;:')
        # 改行混入時は最初の行だけ採用
        kw = kw.split("\n", 1)[0].strip()
        if not kw or len(kw) > 200:  # 200 文字超は異常 (eBay search の URL 制約も考慮)
            return None
        return kw
    except Exception as e:
        logger.warning(f"_parse_keyword_from_response failed: {e}")
        return None


# 単発同期の検索ワード生成 (W288 / 2026-06-27)。
# research_poc 等「未出品候補 (ebay_listings.search_keyword が無い)」の探索前に、
# Brand+型番へ蒸留した検索ワードを 1 件だけ同期生成する (batch 不要)。
# 既存 SEARCH_KEYWORD_PROMPT + _parse_keyword_from_response を再利用 (K2)。
# 失敗 (API key 無し / 例外 / 空) は None → 呼出側はフルタイトル fallback (取りこぼし防止)。
_SYNC_KEYWORD_MODEL = "claude-haiku-4-5-20251001"  # 単発抽出は Haiku で十分・安価


def generate_keyword_sync(title: str, model: str = _SYNC_KEYWORD_MODEL) -> Optional[str]:
    """1 件のタイトルから Brand+型番の検索ワードを同期生成する (batch を使わない単発版).

    用途: research_poc など未出品候補の探索前蒸留 (英語フルタイトル直投げ=真因A の根治)。

    Args:
        title: eBay/Terapeak タイトル (英語混じり)。
        model: 抽出モデル。既定は Haiku 4.5 (単発・安価)。

    Returns:
        蒸留した検索ワード、または None (生成不能 → 呼出側はフルタイトル fallback)。
    """
    if not title or not title.strip():
        return None
    client = _get_client()
    if client is None:
        logger.warning(
            "[generate_keyword_sync] ANTHROPIC_API_KEY 無し → None (caller は fallback)"
        )
        return None
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            messages=[
                {"role": "user",
                 "content": SEARCH_KEYWORD_PROMPT.format(title=title.strip())}
            ],
        )
    except Exception as e:  # noqa: BLE001 — 生成失敗で探索を止めない (None で fallback)
        logger.warning(f"[generate_keyword_sync] API 失敗 → None (fallback): {e}")
        return None
    return _parse_keyword_from_response(msg)


# =============================================================================
# Listings query
# =============================================================================

def _fetch_target_listings(force_all: bool = False) -> list[KeywordItem]:
    """search_keyword 未生成 listing を取得 (force_all=True で全件).

    対象条件:
      - title が NOT NULL かつ非空
      - is_ended=0 (active 出品のみ、ended 出品は不要)
      - force_all=False の場合 search_keyword IS NULL のみ
    """
    where = [
        "title IS NOT NULL",
        "title != ''",
        "(is_ended IS NULL OR is_ended=0)",
    ]
    if not force_all:
        where.append("search_keyword IS NULL")
    sql = (
        f"SELECT ebay_item_id, title FROM ebay_listings "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY ebay_item_id"
    )
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [KeywordItem(ebay_item_id=r[0], title=r[1]) for r in rows]


def _apply_keyword_to_db(ebay_item_id: str, keyword: str) -> None:
    """1 件 UPDATE (test 用 / 手動補正用). 580 件一括は _apply_keywords_batch を使うこと."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET "
            "search_keyword=?, "
            "search_keyword_generated_at=datetime('now'), "
            "search_keyword_source='opus_batch' "
            "WHERE ebay_item_id=?",
            (keyword, ebay_item_id),
        )


def _apply_keywords_batch(updates: list[tuple[str, str]]) -> None:
    """[(ebay_item_id, keyword), ...] を 1 connection で一括 UPDATE.

    H-4 fix (2026-05-10 code-reviewer): 580 件 N+1 で connection open/close すると
    SQLite WAL でも数秒のオーバーヘッド + lock 競合リスク. executemany で 1 connection.
    """
    if not updates:
        return
    with get_conn() as conn:
        conn.executemany(
            "UPDATE ebay_listings SET "
            "search_keyword=?, "
            "search_keyword_generated_at=datetime('now'), "
            "search_keyword_source='opus_batch' "
            "WHERE ebay_item_id=?",
            [(kw, eid) for eid, kw in updates],
        )


# =============================================================================
# Public API
# =============================================================================

def run_generate_search_keywords(
    force_all: bool = False,
    poll_interval_sec: int = _DEFAULT_POLL_INTERVAL_SEC,
    hard_timeout_sec: int = _DEFAULT_HARD_TIMEOUT_SEC,
    model: Optional[str] = None,
) -> KeywordBatchResult:
    """orchestrator: 未生成 listing → batch submit → poll → DB UPDATE.

    Args:
        force_all: True なら全 listing 再生成 (cost 注意 ~$3 for 580 listings).
        poll_interval_sec: poll 間隔.
        hard_timeout_sec: 超過時 timeout 扱い (未完了 listing は NULL のまま).
        model: モデル ID. None で KEYWORD_MODEL (claude-opus-4-8).

    Returns: KeywordBatchResult.

    Q0 防御:
      - submit 失敗 → error_message + return (DB は変更しない)
      - poll timeout → timeout=True + 部分結果のみ DB に反映
      - 個別 errored → DB は NULL のまま (UI で user 手動編集)
    """
    start_time = time.time()
    items = _fetch_target_listings(force_all=force_all)
    if not items:
        logger.info("[w119_keyword] 対象 listing 0 件 (全件 search_keyword 生成済?)")
        return KeywordBatchResult(batch_id="", submitted=0)

    _model = model or KEYWORD_MODEL
    client = _get_client()
    if not client:
        logger.error("[w119_keyword] ANTHROPIC_API_KEY 未設定")
        return KeywordBatchResult(
            batch_id="",
            submitted=len(items),
            errored=len(items),
            error_message="ANTHROPIC_API_KEY not set",
        )

    # Phase 1: submit
    requests = [_build_batch_request(it, _model) for it in items]
    try:
        batch = client.messages.batches.create(requests=requests)
        batch_id = batch.id
        logger.info(
            f"[w119_keyword] submitted batch_id={batch_id} items={len(items)} "
            f"model={_model}"
        )
    except Exception as e:
        logger.error(f"[w119_keyword] submit failed: {e}")
        return KeywordBatchResult(
            batch_id="",
            submitted=len(items),
            errored=len(items),
            error_message=f"batch submit failed: {e}",
            duration_sec=time.time() - start_time,
        )

    # Phase 2: poll until ended / timeout
    deadline = start_time + hard_timeout_sec
    while True:
        try:
            batch = client.messages.batches.retrieve(batch_id)
        except Exception as e:
            logger.warning(
                f"[w119_keyword] poll retrieve failed: {e}, retry in {poll_interval_sec}s"
            )
            time.sleep(poll_interval_sec)
            if time.time() >= deadline:
                break
            continue

        if batch.processing_status == "ended":
            break
        if batch.processing_status in ("canceled", "expired"):
            logger.warning(
                f"[w119_keyword] batch {batch_id} status={batch.processing_status}"
            )
            break
        if time.time() >= deadline:
            logger.warning(
                f"[w119_keyword] hard_timeout exceeded batch_id={batch_id} "
                f"({hard_timeout_sec}s). 未完了 listing は NULL のまま."
            )
            return KeywordBatchResult(
                batch_id=batch_id,
                submitted=len(items),
                errored=len(items),
                timeout=True,
                error_message=f"hard_timeout {hard_timeout_sec}s exceeded",
                duration_sec=time.time() - start_time,
            )
        time.sleep(poll_interval_sec)

    # Phase 3: results 取得 + DB UPDATE (H-4 fix: 結果収集 → 一括 executemany)
    succeeded = 0
    errored = 0
    item_by_cid = {it.custom_id: it for it in items}
    pending_updates: list[tuple[str, str]] = []  # [(ebay_item_id, keyword), ...]

    # api_logger 経由で cost 集計に乗せる (W94 H-5 と同方針)
    try:
        from monitor.api_logger import log_anthropic_response
    except Exception:
        log_anthropic_response = None  # type: ignore

    try:
        for r in client.messages.batches.results(batch_id):
            cid = r.custom_id
            it = item_by_cid.get(cid)
            if not it:
                logger.warning(f"[w119_keyword] unknown custom_id={cid} (skip)")
                continue

            if r.result.type == "succeeded":
                kw = _parse_keyword_from_response(r.result.message)
                if kw:
                    pending_updates.append((it.ebay_item_id, kw))
                    succeeded += 1
                    if log_anthropic_response:
                        try:
                            log_anthropic_response(
                                "w119_keyword_batch",
                                _model,
                                r.result.message,
                                success=True,
                            )
                        except Exception:
                            pass
                else:
                    errored += 1
                    logger.warning(
                        f"[w119_keyword] empty/invalid keyword for "
                        f"ebay_item_id={it.ebay_item_id} (DB は NULL のまま)"
                    )
            else:
                # errored / canceled / expired
                err_type = r.result.type
                err_detail = ""
                if err_type == "errored":
                    err_detail = str(getattr(r.result, "error", ""))[:200]
                logger.info(
                    f"[w119_keyword] item {cid} {err_type}: {err_detail} (DB は NULL のまま)"
                )
                errored += 1

        # 一括 UPDATE: 580 件分を 1 connection で executemany.
        _apply_keywords_batch(pending_updates)
    except Exception as e:
        logger.error(f"[w119_keyword] results fetch failed batch_id={batch_id}: {e}")
        return KeywordBatchResult(
            batch_id=batch_id,
            submitted=len(items),
            succeeded=succeeded,
            errored=len(items) - succeeded,
            error_message=f"results fetch failed: {e}",
            duration_sec=time.time() - start_time,
        )

    duration = time.time() - start_time
    logger.info(
        f"[w119_keyword] completed batch_id={batch_id} duration={duration:.1f}s "
        f"submitted={len(items)} succeeded={succeeded} errored={errored}"
    )
    return KeywordBatchResult(
        batch_id=batch_id,
        submitted=len(items),
        succeeded=succeeded,
        errored=errored,
        duration_sec=duration,
    )


# =============================================================================
# Manual edit (UI から呼ぶ用)
# =============================================================================

def update_search_keyword_manual(ebay_item_id: str, keyword: str) -> bool:
    """user が UI で手動編集した keyword を DB に保存. source='manual_edit'.

    Returns: True if 1 row updated, False otherwise.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE ebay_listings SET "
            "search_keyword=?, "
            "search_keyword_generated_at=datetime('now'), "
            "search_keyword_source='manual_edit' "
            "WHERE ebay_item_id=?",
            (keyword, ebay_item_id),
        )
        return cur.rowcount > 0


# =============================================================================
# Sync (non-batch) execution — 2026-05-10 batch API service degradation 対応
# =============================================================================
# Plan B: Anthropic batch API が stuck する障害が発生したため、通常 API で
# 1 件ずつ loop 実行する path を追加. Cost は batch の 2 倍 (~$6 for 427 listings)
# だが 11 分前後で確実に完走、進捗を逐次 DB に保存するため中断可能.

@dataclass
class KeywordSyncResult:
    """sync loop 全体の集計."""
    submitted: int = 0
    succeeded: int = 0
    errored: int = 0
    duration_sec: float = 0.0
    error_message: Optional[str] = None


def run_generate_search_keywords_sync(
    force_all: bool = False,
    model: Optional[str] = None,
    max_items: Optional[int] = None,
    progress_callback=None,
) -> KeywordSyncResult:
    """通常 API (非 batch) で 1 件ずつ loop 実行.

    Args:
        force_all: True なら全件再生成.
        model: モデル ID. None で KEYWORD_MODEL.
        max_items: 上限件数 (test 用、None で全件).
        progress_callback: callable(idx, total, succeeded, errored). UI 進捗表示用.

    Returns: KeywordSyncResult.

    特徴:
      - 各 1 件ごとに DB UPDATE → 中断時も進捗保持.
      - 個別 errored は DB NULL のまま (UI 手動編集で補完).
      - api_call_log に各 call の cost を記録 (W94 H-5 と同方針).
    """
    start_time = time.time()
    items = _fetch_target_listings(force_all=force_all)
    if max_items is not None:
        items = items[:max_items]
    if not items:
        logger.info("[w119_keyword_sync] 対象 listing 0 件")
        return KeywordSyncResult(submitted=0)

    _model = model or KEYWORD_MODEL
    client = _get_client()
    if not client:
        logger.error("[w119_keyword_sync] ANTHROPIC_API_KEY 未設定")
        return KeywordSyncResult(
            submitted=len(items),
            errored=len(items),
            error_message="ANTHROPIC_API_KEY not set",
        )

    try:
        from monitor.api_logger import log_anthropic_response
    except Exception:
        log_anthropic_response = None  # type: ignore

    succeeded = 0
    errored = 0
    total = len(items)
    logger.info(f"[w119_keyword_sync] 開始 total={total} model={_model}")

    for idx, it in enumerate(items, start=1):
        prompt_text = SEARCH_KEYWORD_PROMPT.format(title=it.title)
        try:
            msg = client.messages.create(
                model=_model,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt_text}],
            )
        except Exception as e:
            logger.warning(
                f"[w119_keyword_sync] API call failed [{idx}/{total}] "
                f"{it.ebay_item_id}: {type(e).__name__}: {e}"
            )
            errored += 1
            if progress_callback:
                try:
                    progress_callback(idx, total, succeeded, errored)
                except Exception:
                    pass
            continue

        kw = _parse_keyword_from_response(msg)
        if kw:
            try:
                _apply_keyword_to_db(it.ebay_item_id, kw)
                succeeded += 1
            except Exception as e:
                logger.warning(
                    f"[w119_keyword_sync] DB UPDATE failed [{idx}/{total}] "
                    f"{it.ebay_item_id}: {e}"
                )
                errored += 1
        else:
            errored += 1
            logger.warning(
                f"[w119_keyword_sync] empty/invalid keyword [{idx}/{total}] "
                f"{it.ebay_item_id} (DB NULL のまま)"
            )

        if log_anthropic_response:
            try:
                log_anthropic_response("w119_keyword_sync", _model, msg, success=True)
            except Exception:
                pass

        if progress_callback:
            try:
                progress_callback(idx, total, succeeded, errored)
            except Exception:
                pass

    duration = time.time() - start_time
    logger.info(
        f"[w119_keyword_sync] completed duration={duration:.1f}s "
        f"submitted={total} succeeded={succeeded} errored={errored}"
    )
    return KeywordSyncResult(
        submitted=total,
        succeeded=succeeded,
        errored=errored,
        duration_sec=duration,
    )


# =============================================================================
# CLI entry (background 実行用)
# =============================================================================

def _cli_main() -> None:
    """`python -m tasks.task_generate_search_keywords` で直接実行可能.
    Streamlit UI 経由でなく background から走らせる用.
    """
    import argparse, sys
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--force-all", action="store_true", help="既存 keyword も再生成")
    parser.add_argument("--max-items", type=int, default=None, help="上限件数 (test 用)")
    parser.add_argument(
        "--mode", choices=["sync", "batch"], default="sync",
        help="sync=通常 API loop / batch=Anthropic Message Batches",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    if args.mode == "sync":
        result = run_generate_search_keywords_sync(
            force_all=args.force_all,
            max_items=args.max_items,
            progress_callback=lambda i, t, s, e: print(
                f"  [{i}/{t}] succ={s} err={e}", flush=True
            ) if i % 10 == 0 or i == t else None,
        )
    else:
        result = run_generate_search_keywords(force_all=args.force_all)

    print(f"\nDONE: submitted={result.submitted} succeeded={result.succeeded} "
          f"errored={result.errored} duration={result.duration_sec:.1f}s")
    if hasattr(result, "error_message") and result.error_message:
        print(f"ERROR: {result.error_message}")
        sys.exit(1)


if __name__ == "__main__":
    _cli_main()
