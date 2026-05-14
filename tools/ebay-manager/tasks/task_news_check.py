#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: AI/Claude ニュース確認
RSSフィード + Anthropic Changelog をチェックし、
eBayツールに影響しうる新機能・変更を検出

定時実行で使える手段（Claude不要）:
1. Anthropic公式のRSSフィード/changelogページ取得
2. 主要AIニュースRSSフィード取得
3. キーワードフィルタリング
4. Discord通知用に整形
"""

import sys
import re
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List
from xml.etree import ElementTree as ET

import httpx

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent

# チェック対象のニュースソース
NEWS_SOURCES = [
    {
        'name': 'Anthropic News',
        'url': 'https://www.anthropic.com/news',
        'type': 'html',
        'keywords': ['claude', 'api', 'model', 'tool', 'agent', 'sdk'],
    },
    {
        'name': 'Anthropic Engineering Blog',
        'url': 'https://www.anthropic.com/engineering',
        'type': 'html',
        'keywords': ['claude', 'api', 'performance', 'feature'],
    },
]

# eBayツールに関連するキーワード（影響判定用）
# 2026-04-24: 「Claude Design」「Introducing Claude Opus 4.7」等の重大情報が拾えていない
# 問題を修正、high に新モデル発表/新サービスローンチ系ワードを追加。
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


def fetch_html_titles(url: str, timeout: int = 10) -> List[str]:
    """HTMLページからリンクテキスト/タイトルを抽出"""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        if resp.status_code != 200:
            logger.warning(f"HTTP {resp.status_code}: {url}")
            return []

        # <h2>, <h3>, <a> タグ内のテキストを抽出
        titles = []
        # h2/h3タグ
        for tag in ['h2', 'h3']:
            matches = re.findall(rf'<{tag}[^>]*>(.*?)</{tag}>', resp.text, re.DOTALL | re.IGNORECASE)
            for m in matches:
                clean = re.sub(r'<[^>]+>', '', m).strip()
                if clean and len(clean) > 10:
                    titles.append(clean)

        # リンクテキスト（記事タイトルとして使われるもの）
        link_matches = re.findall(r'<a[^>]*>(.*?)</a>', resp.text, re.DOTALL | re.IGNORECASE)
        for m in link_matches:
            clean = re.sub(r'<[^>]+>', '', m).strip()
            if clean and len(clean) > 20 and len(clean) < 200:
                titles.append(clean)

        # 重複除去
        seen = set()
        unique = []
        for t in titles:
            if t not in seen:
                seen.add(t)
                unique.append(t)

        return unique[:20]

    except Exception as e:
        logger.warning(f"HTML取得エラー ({url}): {e}")
        return []


def assess_impact(title: str) -> Dict:
    """ニュースタイトルからeBayツールへの影響度を判定"""
    title_lower = title.lower()

    # 最優先: open-challenges.md のキーワードマッチ → high 影響で通知
    for kw in OPEN_CHALLENGE_KEYWORDS:
        if kw in title_lower:
            return {'level': 'high', 'matched_keyword': kw, 'challenge': 'CHAL-001'}

    for level, keywords in IMPACT_KEYWORDS.items():
        for kw in keywords:
            if kw in title_lower:
                return {'level': level, 'matched_keyword': kw}

    return {'level': 'none', 'matched_keyword': None}


def filter_relevant_news(titles: List[str], source_keywords: List[str]) -> List[Dict]:
    """関連性のあるニュースのみフィルタ"""
    relevant = []

    for title in titles:
        title_lower = title.lower()

        # ソース固有のキーワードでフィルタ
        is_relevant = any(kw in title_lower for kw in source_keywords)
        if not is_relevant:
            continue

        impact = assess_impact(title)
        relevant.append({
            'title': title,
            'impact': impact['level'],
            'matched_keyword': impact['matched_keyword'],
        })

    return relevant


def save_news_results(news_items: List[Dict]) -> int:
    """ニュース結果をファイル＋DBの両方に保存。DB側は Claude 要約付き。

    W55 (2026-04-30): rowcount で実 INSERT 件数を集計し返す。
    関数全体の try/except Exception 握り潰し撤去 (Q0 silent skip 違反元凶)。
    個別 news の Claude enrichment 失敗のみ inner try で吸収し、INSERT は試行。
    DB 接続/SQL 自体の例外は上位に伝播 (run_news_check の outer except が success=False 化)。

    Returns:
        実 INSERT 件数 (UNIQUE(source, title) 衝突 IGNORE と事前 SELECT continue は除外)
    """
    # 1) 従来のファイル出力（後方互換のダッシュボード表示）
    output_dir = BASE_DIR / 'data' / 'news'
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    output_file = output_dir / f"{today}-news.json"
    existing = []
    if output_file.exists():
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"既存 news ファイル読み込み失敗 ({output_file}): {e}")
    existing_titles = {n.get('title') for n in existing}
    for item in news_items:
        if item['title'] not in existing_titles:
            item['checked_at'] = datetime.now().isoformat()
            existing.append(item)
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    logger.info(f"ニュース結果を保存: {output_file} ({len(existing)}件)")

    # 2) DB + Claude 要約
    from monitor.database import get_conn
    try:
        from monitor.claude_summarizer import summarize_news as _claude_summarize_news
    except ImportError as e:
        logger.warning(f"claude_summarizer import失敗: {e}")
        _claude_summarize_news = None

    inserted = 0
    with get_conn() as conn:
        for item in news_items:
            title = item.get('title') or ''
            source = item.get('source') or ''
            # 既に DB にあるか (Claude API call 抑制 + UNIQUE 衝突回避)
            exists = conn.execute(
                "SELECT id FROM news_items WHERE source=? AND title=?",
                (source, title),
            ).fetchone()
            if exists:
                continue

            # 個別 enrichment 失敗は warning + 空 summary で INSERT 試行
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
                    cats_str = ','.join(str(c) for c in cats) if isinstance(cats, list) else str(cats)
                except Exception as enrich_err:
                    logger.warning(
                        f"news enrichment 失敗 (title={title[:60]}): {enrich_err}"
                    )

            cur = conn.execute(
                """INSERT OR IGNORE INTO news_items
                   (source, title, url, summary_ja, impact_ja, impact_level, categories, published_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, title, item.get('url', ''),
                 summary_ja, impact_ja, impact_level, cats_str, item.get('published_at', '')),
            )
            inserted += cur.rowcount

    logger.info(f"ニュースDB保存: 候補{len(news_items)}件 / 新規INSERT {inserted}件")
    return inserted


def run_news_check(config):
    """
    AI/Claude ニュースを確認

    1. Anthropic公式サイトの新着を取得
    2. 関連キーワードでフィルタ
    3. eBayツールへの影響度を判定
    4. 結果を保存

    Args:
        config: 設定辞書

    Returns:
        {'success': bool, 'news_count': int, 'news': list}
    """

    logger.info("【開始】AI/Claudeニュース確認タスク")

    try:
        all_news = []
        fetched_titles_total = 0  # W55: filter 前の raw titles 合計、外部経路全滅検出用

        for source in NEWS_SOURCES:
            logger.info(f"チェック中: {source['name']}")

            if source['type'] == 'html':
                titles = fetch_html_titles(source['url'])
            else:
                titles = []
            fetched_titles_total += len(titles)

            if titles:
                relevant = filter_relevant_news(titles, source['keywords'])
                for item in relevant:
                    item['source'] = source['name']
                all_news.extend(relevant)

                logger.info(f"  取得: {len(titles)}件中 {len(relevant)}件が関連")
            else:
                logger.info(f"  取得: 0件")

        # W55: 全 URL から 1 件も raw titles を取得できなかった = 外部経路全滅
        # silent skip ("成功 0 件") 偽装を防ぐため raise → outer except で success=False に倒す
        if fetched_titles_total == 0:
            raise RuntimeError(
                f"全 {len(NEWS_SOURCES)} URL で raw titles 0 件 = HTML 取得経路全滅 "
                f"(network 障害 or サイト改修の可能性)"
            )

        # 影響度でソート（high > medium > low）
        priority_order = {'high': 0, 'medium': 1, 'low': 2, 'none': 3}
        all_news.sort(key=lambda x: priority_order.get(x.get('impact', 'none'), 3))

        # 保存 (W55: rowcount ベース inserted_count 集計)
        inserted = 0
        if all_news:
            inserted = save_news_results(all_news)

        high_impact = [n for n in all_news if n.get('impact') == 'high']
        medium_impact = [n for n in all_news if n.get('impact') == 'medium']

        logger.info(
            f"ニュース確認完了: 候補 {len(all_news)} 件 / DB新規INSERT {inserted} 件 "
            f"(高影響: {len(high_impact)} / 中影響: {len(medium_impact)})"
        )

        return {
            'success': True,
            'news_count': len(all_news),
            'inserted_count': inserted,
            'high_impact_count': len(high_impact),
            'medium_impact_count': len(medium_impact),
            'message': f'候補 {len(all_news)} 件 / DB INSERT {inserted} 件',
            'news': all_news[:10],  # 末尾配置: truncation で切れて他フィールドを保護
        }

    except Exception as e:
        logger.error(f"ニュース確認エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'news_count': 0,
            'inserted_count': 0,
            'error': str(e),
            'news': [],
        }
