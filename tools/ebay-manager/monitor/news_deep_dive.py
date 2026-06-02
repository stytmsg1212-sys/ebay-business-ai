#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W209 Phase 3: AI ニュース深掘り (Opus 4.8 直叩き).

Phase 2 (news_relevance.score_relevance) で relevance_score >= 60 を獲得した上位 3 件
のみを対象に、Opus 4.8 が元記事本文を読んで "ebay-manager のどのモジュールに
どう組み込むと何が得か" を JSON で返す。

研究 / 雑談 (axis=none) は呼ばない (上流 task_news_check で除外)。
budget は news_deep_dive sub-budget = $0.45/日 (anthropic 分)。残量 <= 0 なら
deep_dive_article は None を返し warning。Q0 silent skip 防止。

research-brain agent は subagent ゆえ scheduler 経由で起動できないため、
本モジュールは Anthropic SDK を直叩きする (subagent 不要)。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# .env ロード (claude_summarizer と同じパターン)
try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

# Opus 4.8 = pricing dict に登録済 (api_logger._PRICING)
MODEL = "claude-opus-4-8"

# 記事本文 fetch のタイムアウト (秒) / truncate 上限
_FETCH_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_FETCH_MAX_BYTES = 800_000     # 800KB cap (HTML)
_FETCH_MAX_CHARS = 12_000      # 抽出後 12k 文字 cap (Opus prompt サイズ抑制)
_USER_AGENT = "ebay-manager/2.0 (W209 deep-dive)"


DEEP_DIVE_SYSTEM = """あなたは越境EC物販ツール (ebay-manager) のシニア技術参謀です。
AI / Tech ニュースの本文を読み、本ツールの既存モジュールへの "具体的な組み込み案" を
日本語で 1 つ提示します。

既存モジュールの例 (target_module に書く時の候補):
- ebay_lister.py        (出品 XML 生成 / Item Specifics / Title)
- claude_summarizer.py  (メール要約 / ニュース要約)
- claude_evaluator.py   (仕入先候補評価)
- supplier_apply.py     (仕入先確定 → reprice)
- ebay_sync.py          (eBay ActiveList sync / GetItem)
- scrapers.py           (Mercari / Yahoo / Rakuten / Amazon)
- supplier_batch_evaluator.py
- customs_draft_generator.py (FedEx 通関ドラフト)
- image_composer.py / image_composer_gemini.py (画像合成)
- lowest_price.py       (最安値検出)
- research_brain.py     (リサーチ脳)
- task_news_check.py    (本ニュース取得タスク自身)
- 新規モジュール (上記に当てはまらない明確な機能なら 'NEW: 短い英名' で可)

出力は厳密な JSON のみ (```json フェンス禁止、余分なテキスト禁止):
{
  "summary_ja": "記事の要点を 2-3 文で日本語要約",
  "target_module": "上記候補から 1 つ",
  "integration_ja": "どのコードにどう組み込むかを 2-4 文で具体的に",
  "benefit_ja": "ebay-manager に何が得か (定量化できるなら数値を含めて 1-2 文)",
  "effort_estimate": "S" | "M" | "L",
  "confidence": "high" | "medium" | "low"
}

effort_estimate 目安:
- S: 半日以内 (既存関数の引数追加 / プロンプト調整 等)
- M: 1-3 日 (新規ファイル 1-2 個 + DB migration 1 段)
- L: 1 週間以上 (新規サブシステム / 外部 API 連携追加)

confidence 目安:
- high: 記事が API / SDK 公式 doc を含み、組み込み手順が明確
- medium: 記事は紹介止まりだが、推測で組み込み案を提示
- low: 記事内容が薄く、深掘りに値しない可能性あり (本来 Phase 2 で弾くべき)

ebay 物販固有の制約 (記事内容と矛盾しても守る):
- Country of Origin / Country of Manufacture は eBay 出品文に絶対記載しない
- SKU は listing 識別キーではない (ebay_item_id が listing キー)
- 米国向けは DDP = 関税売主負担
これらに違反する組み込み案は出さない。"""


def _strip_fenced_json(text: str) -> Optional[str]:
    """```json フェンス除去 (claude_summarizer と同じ)."""
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if fence:
        return fence.group(1)
    greedy = re.search(r'\{[\s\S]*\}', text)
    if greedy:
        return greedy.group(0)
    open_brace = re.search(r'\{[\s\S]*$', text)
    if open_brace:
        return open_brace.group(0).rstrip() + "}"
    return None


def _url_host_is_public(url: str) -> bool:
    """URL の host を解決し、全 IP が public (グローバル) かを検証 (SSRF 防御)。

    2026-06-02 Codex/code-reviewer 指摘: 記事 URL は RSS/Reddit/HN/X 由来の外部値で、
    follow_redirects 経由で localhost / RFC1918 / link-local (169.254 metadata) 等の
    内部資源へ到達し得る。host を名前解決し、loopback/private/link-local/reserved/
    multicast/unspecified を 1 つでも含めば拒否する。
    """
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        # host が IP リテラルでも getaddrinfo は解決する。全 AF を検査。
        infos = socket.getaddrinfo(host, None)
        for fam, _, _, _, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                logger.warning(
                    f"fetch_article_text: 非public host を拒否 "
                    f"(host={host} ip={ip})"
                )
                return False
        return True
    except (socket.gaierror, ValueError, UnicodeError) as e:
        logger.warning(f"fetch_article_text: host 解決失敗 ({url[:80]}): {e}")
        return False


def fetch_article_text(url: str) -> str:
    """記事 URL を fetch し HTML の <script>/<style> 除去 → text を抽出 → truncate。

    失敗時は空文字を返す (deep_dive_article 側で title のみ渡しの fallback ができる)。

    K1 Simplicity: 重い本文抽出ライブラリ (trafilatura 等) を導入しない、
    最小の regex + tag strip で実用十分 (Opus が要約整形を行う)。

    SSRF 防御 (2026-06-02): 各 hop の host を public IP 検証し、follow_redirects は
    手動 (最大 3 hop) で各 Location を再検証。本文は stream で _FETCH_MAX_BYTES まで
    しか読まない (取得後 slice ではダウンロード自体を抑止できないため)。
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return ""

    raw = b""
    try:
        with httpx.Client(
            timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=False,
        ) as client:
            cur_url = url
            for _hop in range(4):  # 初回 + 最大 3 redirect
                if not _url_host_is_public(cur_url):
                    return ""
                with client.stream("GET", cur_url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location") or ""
                        if not loc:
                            return ""
                        # 相対 Location を絶対化
                        cur_url = str(httpx.URL(cur_url).join(loc))
                        continue
                    if resp.status_code != 200:
                        logger.warning(
                            f"fetch_article_text HTTP {resp.status_code}: {url[:80]}"
                        )
                        return ""
                    # stream で max bytes まで読む (巨大 body のダウンロード抑止)
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= _FETCH_MAX_BYTES:
                            break
                    raw = b"".join(chunks)[:_FETCH_MAX_BYTES]
                    break
            else:
                logger.warning(f"fetch_article_text: redirect 過多 {url[:80]}")
                return ""
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(
            f"fetch_article_text 失敗 ({url[:80]}): {type(e).__name__}: {e}"
        )
        return ""
    if not raw:
        return ""

    try:
        html_text = raw.decode("utf-8", errors="ignore")
    except UnicodeDecodeError:
        return ""

    # <script>/<style>/<nav>/<footer> 除去
    cleaned = re.sub(
        r'<(script|style|nav|footer|aside|header)[^>]*>.*?</\1>',
        ' ',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # 残りの HTML tag を除去
    cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
    # 連続空白 → 1 つ
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:_FETCH_MAX_CHARS]


def deep_dive_article(
    item: dict,
    *,
    budget_remaining_usd: float,
) -> Optional[dict]:
    """1 記事を Opus 4.8 で深掘りし、組み込み案 JSON を返す。

    Args:
        item: {'title': str, 'url': str, 'source': str,
               'relevance_score': int, 'axis': str,
               'summary_ja': str (optional, Phase 1 既存要約)}
        budget_remaining_usd: 残予算 (news_deep_dive sub-budget)。
            <= 0 なら本関数は None を返し warning (silent skip にせず痕跡を残す)。

    Returns:
        {'summary_ja', 'target_module', 'integration_ja', 'benefit_ja',
         'effort_estimate', 'confidence', 'model', 'cost_usd'} or None.
    """
    title = (item.get("title") or "").strip()
    url = (item.get("url") or "").strip()
    if not title:
        logger.warning("deep_dive_article: title 空 = skip")
        return None

    # budget gate (Q0: silent skip ではなく warning で痕跡)
    if budget_remaining_usd <= 0:
        logger.warning(
            f"deep_dive_article skip (budget exhausted): "
            f"remaining=${budget_remaining_usd:.4f} title={title[:60]!r}"
        )
        return None

    if not _ANTHROPIC_OK:
        logger.warning("anthropic package not installed (deep_dive_article)")
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY missing (deep_dive_article)")
        return None

    # 本文取得 (失敗時は title + 既存 summary のみで深掘り)
    article_text = fetch_article_text(url) if url else ""

    from monitor.api_logger import log_anthropic_response, _Timer, _estimate_cost_usd
    from monitor.database import add_api_cost

    user_parts = [
        f"Source: {item.get('source', '')}",
        f"Title: {title}",
        f"URL: {url}",
        f"Phase 2 関連度: score={item.get('relevance_score', 0)} axis={item.get('axis', 'none')}",
    ]
    if item.get("summary_ja"):
        user_parts.append(f"既存日本語要約: {item['summary_ja'][:500]}")
    if article_text:
        user_parts.append(f"\n本文 (抜粋):\n{article_text}")
    else:
        user_parts.append("\n(本文 fetch 失敗 = title + 既存要約のみで深掘り)")
    user_parts.append("\n上記を JSON で組み込み案にしてください。")
    user_text = "\n".join(user_parts)

    client = anthropic.Anthropic()
    msg = None
    cost_usd = 0.0
    try:
        with _Timer() as t:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=1200,
                system=[
                    {"type": "text", "text": DEEP_DIVE_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": user_text}],
            )
        log_anthropic_response(
            "news_deep_dive", MODEL, msg, duration_ms=t.duration_ms, success=True,
        )
        in_tok = int(getattr(msg.usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(msg.usage, "output_tokens", 0) or 0)
        cache_r = int(getattr(msg.usage, "cache_read_input_tokens", 0) or 0)
        cache_w = int(getattr(msg.usage, "cache_creation_input_tokens", 0) or 0)
        cost_usd = _estimate_cost_usd(MODEL, in_tok, out_tok, cache_r, cache_w)
        try:
            add_api_cost("anthropic", cost_usd, context="news_deep_dive")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"add_api_cost (news_deep_dive) 失敗: {e}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"deep_dive_article Opus API error: {e}")
        log_anthropic_response(
            "news_deep_dive", MODEL, None, duration_ms=None,
            success=False, error_message=str(e)[:500],
        )
        return None

    text = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    cand = _strip_fenced_json(text)
    if not cand:
        logger.warning(
            f"deep_dive_article: no JSON in response: {text[:120]!r}"
        )
        return None
    try:
        parsed = json.loads(cand)
    except json.JSONDecodeError as e:
        logger.warning(
            f"deep_dive_article JSON decode: {e}, raw={text[:120]!r}"
        )
        return None

    # 必須キーの sanity check + 正規化
    out = {
        "summary_ja": str(parsed.get("summary_ja") or "").strip()[:1000],
        "target_module": str(parsed.get("target_module") or "").strip()[:200],
        "integration_ja": str(parsed.get("integration_ja") or "").strip()[:1500],
        "benefit_ja": str(parsed.get("benefit_ja") or "").strip()[:800],
        "effort_estimate": str(
            parsed.get("effort_estimate") or "M"
        ).strip().upper()[:1],
        "confidence": str(
            parsed.get("confidence") or "medium"
        ).strip().lower(),
        "model": MODEL,
        "cost_usd": float(cost_usd),
    }
    if out["effort_estimate"] not in ("S", "M", "L"):
        out["effort_estimate"] = "M"
    if out["confidence"] not in ("high", "medium", "low"):
        out["confidence"] = "medium"
    return out
