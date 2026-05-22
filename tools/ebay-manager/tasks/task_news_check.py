#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""W154 (2026-05-22 PM): 統合 AI ニュース取得タスク.

旧 W55 task_news_check.py (Anthropic 公式 HTML scrape) と
旧 W13 task_x_news_check.py (X(Grok) + Reddit + HN) を本ファイルに統合.

X (Grok 経由 search-x) はリストラ:
  - パロディ / 偽スクショ / 煽動コンテンツ混在で信頼性低い
  - engagement 加重 → sensationalist 浮上で金銭直結業務 (API 価格変更 / モデル
    deprecation 等) の情報源として不適
  - 月 ~$60 (xai_daily_cap_usd $2.0/day) 削減

代替 = 公式 RSS + 編集付きメディア RSS + 既存 Reddit + 既存 HN:
  - Tier 0 (公式 lab blog): Anthropic / OpenAI / Google DeepMind / Hugging Face
  - Tier 1 (編集付きメディア): MIT Tech Review / VentureBeat / TechCrunch / Ars Technica
  - Tier 3 (community): Reddit 6 sub (r/singularity → r/MachineLearning 入替) + HN 4 query

W55 既存の業務 logic (assess_impact / save_news_results / claude_summarizer 統合)
は維持. fetcher 群のみ差替.
"""

import sys
import re
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

# Reddit/HN/RSS 共通 HTTP timeout (s)
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# ASCII にも見える User-Agent (Reddit は名乗らない UA を block する).
_USER_AGENT = 'ebay-manager/2.0 (W154 AI news digest)'

# Reddit: min_score / HN: min_points で noise を除外
REDDIT_MIN_SCORE = 50
HN_MIN_POINTS = 50

# NEWS_SOURCES: 各 source は {'name', 'type', ...} を必須.
# type ∈ {'rss', 'reddit', 'hn'}.
#   rss:    {'url', 'keywords'? (空なら filter なし = 全件通す)}
#   reddit: {'subreddit', 'listing'?='hot', 'limit'?=15}
#   hn:     {'query', 'days'?=3, 'max'?=15}
NEWS_SOURCES = [
    # Tier 0: 公式 AI ラボ blog (一次情報, RSS 確認済 2026-05-22)
    {
        'name': 'Anthropic News',
        'type': 'rss',
        'url': 'https://raw.githubusercontent.com/taobojlen/anthropic-rss-feed/main/anthropic_news_rss.xml',
        'tier': 0,
        'keywords': ['claude', 'opus', 'sonnet', 'haiku', 'api', 'agent', 'sdk', 'anthropic'],
    },
    {
        'name': 'OpenAI News',
        'type': 'rss',
        'url': 'https://openai.com/news/rss.xml',
        'tier': 0,
        'keywords': ['gpt', 'chatgpt', 'openai', 'api', 'codex', 'sora'],
    },
    {
        'name': 'Google DeepMind',
        'type': 'rss',
        'url': 'https://deepmind.google/blog/rss.xml',
        'tier': 0,
        'keywords': ['gemini', 'deepmind', 'model', 'api', 'google'],
    },
    {
        'name': 'Hugging Face Blog',
        'type': 'rss',
        'url': 'https://huggingface.co/blog/feed.xml',
        'tier': 0,
        'keywords': ['llm', 'model', 'open source', 'transformers', 'hugging'],
    },
    # Tier 1: 編集者付き世界的メディア (AI セクション feed のみ取得 = カテゴリ filter 済)
    {
        'name': 'MIT Tech Review AI',
        'type': 'rss',
        'url': 'https://www.technologyreview.com/topic/artificial-intelligence/feed/',
        'tier': 1,
        'keywords': [],  # AI tag feed = 全件 relevant
    },
    {
        'name': 'VentureBeat AI',
        'type': 'rss',
        'url': 'https://venturebeat.com/category/ai/feed/',
        'tier': 1,
        'keywords': [],
    },
    {
        'name': 'TechCrunch AI',
        'type': 'rss',
        'url': 'https://techcrunch.com/category/artificial-intelligence/feed/',
        'tier': 1,
        'keywords': [],
    },
    {
        'name': 'Ars Technica AI',
        'type': 'rss',
        'url': 'https://arstechnica.com/ai/feed',
        'tier': 1,
        'keywords': [],
    },
    # Tier 3: コミュニティ (Reddit hot listing, score filter で noise 除外)
    # r/singularity (AGI 終末論 / 人類超消滅系 noise 多い) は除外、r/MachineLearning に入替.
    {'name': 'r/ClaudeAI',        'type': 'reddit', 'subreddit': 'ClaudeAI',        'listing': 'hot', 'limit': 20},
    {'name': 'r/MachineLearning', 'type': 'reddit', 'subreddit': 'MachineLearning', 'listing': 'hot', 'limit': 15},
    {'name': 'r/LocalLLaMA',      'type': 'reddit', 'subreddit': 'LocalLLaMA',      'listing': 'hot', 'limit': 15},
    {'name': 'r/OpenAI',          'type': 'reddit', 'subreddit': 'OpenAI',          'listing': 'hot', 'limit': 15},
    {'name': 'r/Flipping',        'type': 'reddit', 'subreddit': 'Flipping',        'listing': 'hot', 'limit': 10},
    {'name': 'r/Ebay',            'type': 'reddit', 'subreddit': 'Ebay',            'listing': 'hot', 'limit': 10},
    # HN: keyword search (Algolia API), days で recency filter
    {'name': 'HN: anthropic',     'type': 'hn', 'query': 'anthropic',     'days': 3, 'max': 15},
    {'name': 'HN: claude opus',   'type': 'hn', 'query': 'claude opus',   'days': 3, 'max': 15},
    {'name': 'HN: llm',           'type': 'hn', 'query': 'llm',           'days': 1, 'max': 15},
    {'name': 'HN: ai agent',      'type': 'hn', 'query': 'ai agent',      'days': 2, 'max': 15},
]

# eBay ツールに関連するキーワード (影響判定用)
# 2026-04-24 W55: 「Claude Design」「Introducing Claude Opus 4.7」等の重大情報が拾えていない
# 問題を修正、high に新モデル発表/新サービスローンチ系ワードを追加.
IMPACT_KEYWORDS = {
    'high': [
        'api change', 'breaking', 'deprecat', 'pricing', 'rate limit',
        'new model', 'introducing', 'introduce', 'announce', 'announcement',
        'launch', 'release', 'new version', 'new feature', 'general availability',
        'ga release', 'opus 4', 'sonnet 4', 'haiku 4', 'opus 5', 'sonnet 5', 'haiku 5',
        'claude design', 'claude code', 'claude agent', 'agent sdk',
        'partner network', 'partner program', 'enterprise',
    ],
    'medium': ['tool use', 'agent', 'sdk', 'mcp', 'vision', 'batch', 'update', 'improved'],
    'low': ['blog', 'research', 'safety', 'alignment'],
}

# open-challenges.md 監視キーワード: hit したら High 影響で通知
# ロゴプレート + 商品写真合成の品質改善ニュース検出 (CHAL-001)
OPEN_CHALLENGE_KEYWORDS = [
    # CHAL-001: 画像合成品質
    'image composition', 'multi-reference', 'image edit', 'pixel-accurate',
    'seedream', 'seededit', 'flux kontext', 'gemini image',
    '3d-aware', 'inpaint', 'mask based edit', 'reference image',
    'ideogram', 'firefly composition', 'photoroom api',
    'product photography ai', 'brand placement',
]


# ─────────────────────────────────────────────
# Fetchers (type='rss' / 'reddit' / 'hn')
# 各 fetcher は List[Dict] を返す: {'title', 'url', 'published_at', ...}
# ─────────────────────────────────────────────

def _epoch_to_iso(epoch) -> Optional[str]:
    """UNIX epoch → ISO-8601 UTC (None 安全)."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return None


def _days_ago_epoch(days: int) -> int:
    """N 日前の UNIX epoch (UTC, HN Algolia numericFilters 用)."""
    return int((datetime.now(tz=timezone.utc) - timedelta(days=int(days))).timestamp())


def fetch_rss_entries(source: Dict) -> List[Dict]:
    """RSS / Atom フィードから entries を取得.

    feedparser を late import で読み込み (起動高速化 + 単体テストで mock 容易).
    httpx で bytes 取得 → feedparser.parse(bytes) で構造化.

    Args:
        source: {'name', 'url', 'type'='rss', ...}

    Returns:
        list of {'title', 'url', 'published_at'} (上限 50 entries / feed)
    """
    import feedparser  # type: ignore

    url = source['url']
    name = source.get('name', url)
    try:
        with httpx.Client(
            timeout=_HTTP_TIMEOUT,
            headers={'User-Agent': _USER_AGENT},
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            logger.warning(f"RSS fetch HTTP {resp.status_code}: {name}")
            return []
        parsed = feedparser.parse(resp.content)
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"RSS fetch failed ({name}): {type(e).__name__}: {e}")
        return []

    # bozo=1 でも entries が取れていれば許容 (一部 feed は仕様外マークアップだが
    # entries は構造化可能 — Anthropic community feed 等)
    if not parsed.entries:
        if parsed.bozo:
            logger.warning(
                f"RSS parse 0 entries ({name}): bozo={parsed.bozo_exception!r}"
            )
        else:
            logger.warning(f"RSS parse 0 entries ({name}): empty feed")
        return []

    entries: List[Dict] = []
    for e in parsed.entries[:50]:
        title = (getattr(e, 'title', '') or '').strip()
        if not title:
            continue
        link = (getattr(e, 'link', '') or '').strip()
        published = (
            getattr(e, 'published', '')
            or getattr(e, 'updated', '')
            or ''
        )
        entries.append({
            'title': title[:200],
            'url': link,
            'published_at': published,
        })
    return entries


def fetch_reddit_entries(source: Dict) -> List[Dict]:
    """Reddit /r/{sub}/{listing}.json (old.reddit.com = 寛容な公開 API).

    403 / 429 は 1 source skip (他は続行).
    score < REDDIT_MIN_SCORE は除外.

    Args:
        source: {'name', 'subreddit', 'listing'='hot', 'limit'=15}
    """
    sub = source['subreddit']
    listing = source.get('listing', 'hot')
    limit = int(source.get('limit', 15))
    url = f'https://old.reddit.com/r/{sub}/{listing}.json'
    try:
        with httpx.Client(
            timeout=_HTTP_TIMEOUT,
            headers={'User-Agent': _USER_AGENT},
        ) as client:
            r = client.get(url, params={'limit': limit})
        if r.status_code in (403, 429):
            logger.warning(f"Reddit r/{sub}: HTTP {r.status_code} (skip)")
            return []
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"Reddit r/{sub} failed: {type(e).__name__}: {e}")
        return []

    entries: List[Dict] = []
    for child in data.get('data', {}).get('children', []):
        d = child.get('data') or {}
        score = int(d.get('score') or 0)
        if score < REDDIT_MIN_SCORE:
            continue
        permalink = d.get('permalink') or ''
        title = (d.get('title') or '').strip()
        if not title:
            continue
        entries.append({
            'title': title[:200],
            'url': f'https://www.reddit.com{permalink}' if permalink else '',
            'published_at': _epoch_to_iso(d.get('created_utc')) or '',
            'extra_score': score,
        })
    return entries


def fetch_hn_entries(source: Dict) -> List[Dict]:
    """Hacker News Algolia Search API (公式、寛容).

    stories のみ取得 (Ask/Show/Job 除外).
    points >= HN_MIN_POINTS で noise 除外.

    Args:
        source: {'name', 'query', 'days'=3, 'max'=15}
    """
    query = source['query']
    days = int(source.get('days', 3))
    max_n = int(source.get('max', 15))
    base = 'https://hn.algolia.com/api/v1/search'
    params = {
        'query': query,
        'tags': 'story',
        'numericFilters': (
            f"created_at_i>{_days_ago_epoch(days)},"
            f"points>={HN_MIN_POINTS}"
        ),
        'hitsPerPage': max_n,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.get(base, params=params)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"HN fetch failed ('{query}'): {type(e).__name__}: {e}")
        return []

    entries: List[Dict] = []
    for hit in data.get('hits', []):
        title = (hit.get('title') or '').strip()
        if not title:
            continue
        url = hit.get('url') or (
            f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
        )
        entries.append({
            'title': title[:200],
            'url': url,
            'published_at': hit.get('created_at', ''),
            'extra_points': int(hit.get('points') or 0),
        })
    return entries


# ─────────────────────────────────────────────
# Impact assessment (W55 既存ロジック維持)
# ─────────────────────────────────────────────

def assess_impact(title: str) -> Dict:
    """ニュースタイトルから eBay ツールへの影響度を判定.

    最優先: OPEN_CHALLENGE_KEYWORDS hit → high + challenge tag.
    次点: IMPACT_KEYWORDS の high → medium → low の順で最初の hit.
    """
    title_lower = (title or '').lower()

    for kw in OPEN_CHALLENGE_KEYWORDS:
        if kw in title_lower:
            return {'level': 'high', 'matched_keyword': kw, 'challenge': 'CHAL-001'}

    for level, keywords in IMPACT_KEYWORDS.items():
        for kw in keywords:
            if kw in title_lower:
                return {'level': level, 'matched_keyword': kw}

    return {'level': 'none', 'matched_keyword': None}


def filter_relevant_news(entries: List[Dict], source_keywords: List[str]) -> List[Dict]:
    """関連性 filter + impact 判定.

    source_keywords が空 → 全件 relevant (Tier 1 編集付きメディアの AI feed など).
    空でない → タイトルに 1 つでも含むものだけ通す.
    """
    relevant: List[Dict] = []
    for entry in entries:
        title = entry.get('title', '') or ''
        title_lower = title.lower()
        if source_keywords:
            is_relevant = any(kw in title_lower for kw in source_keywords)
            if not is_relevant:
                continue
        impact = assess_impact(title)
        out = {
            **entry,
            'impact': impact['level'],
            'matched_keyword': impact['matched_keyword'],
        }
        relevant.append(out)
    return relevant


# ─────────────────────────────────────────────
# Save (W55 既存ロジック維持: rowcount ベース inserted_count + Claude enrichment)
# ─────────────────────────────────────────────

def save_news_results(news_items: List[Dict]) -> int:
    """ニュース結果をファイル + DB の両方に保存. DB 側は Claude 要約付き.

    W55 (2026-04-30): rowcount で実 INSERT 件数を集計し返す.
    関数全体の try/except Exception 握り潰し撤去 (Q0 silent skip 違反元凶).
    個別 news の Claude enrichment 失敗のみ inner try で吸収し, INSERT は試行.
    DB 接続/SQL 自体の例外は上位に伝播 (run_news_check の outer except が
    success=False 化).

    Returns:
        実 INSERT 件数 (UNIQUE(source, title) 衝突 IGNORE と事前 SELECT continue は除外)
    """
    # 1) 従来のファイル出力 (後方互換のダッシュボード表示)
    output_dir = BASE_DIR / 'data' / 'news'
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    output_file = output_dir / f"{today}-news.json"
    existing: List[Dict] = []
    if output_file.exists():
        try:
            with output_file.open('r', encoding='utf-8') as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"既存 news ファイル読み込み失敗 ({output_file}): {e}")
    existing_titles = {n.get('title') for n in existing}
    for item in news_items:
        if item['title'] not in existing_titles:
            item['checked_at'] = datetime.now().isoformat()
            existing.append(item)
    with output_file.open('w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    logger.info(f"ニュース結果を保存: {output_file} ({len(existing)} 件)")

    # 2) DB + Claude 要約
    from monitor.database import get_conn
    try:
        from monitor.claude_summarizer import summarize_news as _claude_summarize_news
    except ImportError as e:
        logger.warning(f"claude_summarizer import 失敗: {e}")
        _claude_summarize_news = None

    inserted = 0
    with get_conn() as conn:
        for item in news_items:
            title = item.get('title') or ''
            source = item.get('source') or ''
            exists = conn.execute(
                "SELECT id FROM news_items WHERE source=? AND title=?",
                (source, title),
            ).fetchone()
            if exists:
                continue

            summary_ja = impact_ja = ''
            impact_level = item.get('impact') or 'none'
            cats_str = ''
            if _claude_summarize_news:
                try:
                    ai = _claude_summarize_news(title, source=source)
                    summary_ja = (ai or {}).get('summary_ja') or ''
                    impact_ja = (ai or {}).get('impact_ja') or ''
                    impact_level = (ai or {}).get('impact_level') or impact_level
                    cats = (ai or {}).get('categories') or []
                    cats_str = (
                        ','.join(str(c) for c in cats)
                        if isinstance(cats, list) else str(cats)
                    )
                except Exception as enrich_err:  # noqa: BLE001
                    # Anthropic SDK の例外型は version 差で安定しない. 個別 enrichment 失敗で
                    # 全件 silent skip させないため広く catch + warning (Q0 違反防止).
                    logger.warning(
                        f"news enrichment 失敗 (title={title[:60]}): {enrich_err}"
                    )

            cur = conn.execute(
                """INSERT OR IGNORE INTO news_items
                   (source, title, url, summary_ja, impact_ja, impact_level,
                    categories, published_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, title, item.get('url', ''),
                 summary_ja, impact_ja, impact_level, cats_str,
                 item.get('published_at', '')),
            )
            inserted += cur.rowcount

    logger.info(f"ニュース DB 保存: 候補 {len(news_items)} 件 / 新規 INSERT {inserted} 件")
    return inserted


# ─────────────────────────────────────────────
# Main entrypoint
# ─────────────────────────────────────────────

# NOTE: type → fetcher の dispatch は **runtime に globals() で resolve** する.
# `dict` リテラルで {type: fetch_rss_entries, ...} と書くと dict 生成時の
# 関数 reference を捕捉してしまい、`monkeypatch.setattr(t, "fetch_rss_entries", ...)`
# が dict 内 reference に反映されず unit test がパッチ無効化される
# (W154 実装中 2026-05-22 PM に内部 self-review で検出).
_FETCHER_BY_TYPE = {
    'rss': 'fetch_rss_entries',
    'reddit': 'fetch_reddit_entries',
    'hn': 'fetch_hn_entries',
}


def _resolve_fetcher(stype: str):
    """type 名で fetcher 関数を **module namespace から動的解決**.

    monkeypatch (test) と通常 import の両方で正しく動作するように
    sys.modules[__name__] 経由で attribute lookup する.
    """
    fn_name = _FETCHER_BY_TYPE.get(stype)
    if fn_name is None:
        return None
    import sys as _sys
    return getattr(_sys.modules[__name__], fn_name, None)


def run_news_check(config: Optional[Dict] = None) -> Dict:
    """AI / Claude ニュースを取得 → 影響度判定 → ファイル + DB 保存.

    1. NEWS_SOURCES を type 別に fetch (1 source 失敗 → 他は続行)
    2. source['keywords'] で関連性 filter (空なら全件)
    3. assess_impact で impact level 付与
    4. 影響度ソート (high → medium → low → none)
    5. save_news_results で永続化 (file + DB + Claude 要約)

    全 source raw=0 → 外部経路全滅 (Q0 silent skip 防止) → RuntimeError raise.

    Returns:
        {'success': bool, 'news_count': int, 'inserted_count': int,
         'raw_count': int, 'high_impact_count': int, 'medium_impact_count': int,
         'per_source': dict, 'message': str, 'news': list}
    """
    logger.info("【開始】AI / Claude ニュース取得タスク (W154 統合版)")

    try:
        all_news: List[Dict] = []
        fetched_entries_total = 0
        per_source: Dict[str, Dict] = {}

        for source in NEWS_SOURCES:
            stype = source.get('type')
            fetcher = _resolve_fetcher(stype)
            if fetcher is None:
                logger.warning(
                    f"unknown source type '{stype}' for {source.get('name')}, skip"
                )
                per_source[source.get('name', '?')] = {
                    'raw': 0, 'relevant': 0, 'error': 'unknown_type',
                }
                continue

            try:
                entries = fetcher(source)
            except Exception as e:  # noqa: BLE001
                # per-source top-level fail-safe: 1 source の予期せぬ例外で
                # 全 batch を倒さない (silent skip-prevention は raw=0 集計で別途検出).
                logger.warning(
                    f"fetcher 例外 ({source.get('name')}): {type(e).__name__}: {e}"
                )
                entries = []
                per_source[source.get('name', '?')] = {
                    'raw': 0, 'relevant': 0, 'error': type(e).__name__,
                }
            else:
                per_source[source.get('name', '?')] = {
                    'raw': len(entries), 'relevant': 0,
                }

            fetched_entries_total += len(entries)

            relevant = filter_relevant_news(entries, source.get('keywords') or [])
            for item in relevant:
                item['source'] = source['name']
            all_news.extend(relevant)
            per_source[source.get('name', '?')]['relevant'] = len(relevant)

            logger.info(
                f"  {source['name']}: raw={len(entries)} relevant={len(relevant)}"
            )

        # 全 source raw=0 → 外部経路全滅 (W55 silent skip 防止と同じ raise パターン)
        if fetched_entries_total == 0:
            raise RuntimeError(
                f"全 {len(NEWS_SOURCES)} source で raw entries 0 件 = "
                f"network 障害 or 全フィード改修の可能性"
            )

        # 影響度でソート (high > medium > low > none)
        priority_order = {'high': 0, 'medium': 1, 'low': 2, 'none': 3}
        all_news.sort(
            key=lambda x: priority_order.get(x.get('impact', 'none'), 3),
        )

        inserted = save_news_results(all_news) if all_news else 0

        high = sum(1 for n in all_news if n.get('impact') == 'high')
        medium = sum(1 for n in all_news if n.get('impact') == 'medium')

        logger.info(
            f"ニュース確認完了: raw 合計 {fetched_entries_total} / "
            f"候補 {len(all_news)} / DB INSERT {inserted} "
            f"(高 {high} / 中 {medium})"
        )

        return {
            'success': True,
            'news_count': len(all_news),
            'raw_count': fetched_entries_total,
            'inserted_count': inserted,
            'high_impact_count': high,
            'medium_impact_count': medium,
            'per_source': per_source,
            'message': (
                f'raw {fetched_entries_total} / 候補 {len(all_news)} / '
                f'INSERT {inserted}'
            ),
            'news': all_news[:10],  # 末尾配置: truncation で切れて他フィールドを保護
        }

    except Exception as e:  # noqa: BLE001
        # outer except: 想定内 (RuntimeError = 外部全滅 / DB error 等) も
        # 想定外も success=False で返す. Q0 silent skip 防止のため握り潰しではなく
        # error フィールドに伝播.
        logger.error(f"ニュース確認エラー: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'news_count': 0,
            'raw_count': 0,
            'inserted_count': 0,
            'error': str(e),
            'news': [],
        }


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    result = run_news_check({})
    print(json.dumps(result, ensure_ascii=False, indent=2))
