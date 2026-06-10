#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Company Router - 自動タスクの結果を .company 各部署に配信

daily_scheduler の各タスク完了後に呼び出され、
結果を人間が読めるMarkdown形式で該当部署フォルダに書き込む。

data/ = 機械用（JSON）、.company/ = 人間用（Markdown）
"""

import sys
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)


def get_company_root() -> Optional[Path]:
    """Get the .company directory path."""
    project_root = Path(__file__).parent.parent.parent / ".company"
    if project_root.exists():
        return project_root
    company_path = Path.home() / ".company"
    if company_path.exists():
        return company_path
    return None


def _append_to_file(file_path: Path, content: str):
    """ファイルに追記（なければ新規作成）"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    mode = 'a' if file_path.exists() else 'w'
    with open(file_path, mode, encoding='utf-8') as f:
        f.write(content)


def _write_file(file_path: Path, content: str):
    """ファイルを書き込み（上書き）"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_time() -> str:
    return datetime.now().strftime("%H:%M")


# ============================================================
# 各タスクのルーティング関数
# ============================================================

def route_ebay_sync(result: Dict, company_root: Path):
    """
    eBay同期結果 → daily-operations/logs/YYYY-MM-DD.md に追記
    """
    if not result or not result.get('success'):
        return

    log_file = company_root / "daily-operations" / "logs" / f"{_today()}.md"
    details = result.get('details', {})
    sync = details.get('sync', {})
    rank = details.get('rank', {})
    report = details.get('report', {})

    content = f"\n## eBay同期 ({_now_time()})\n\n"
    content += f"- 同期件数: {result.get('synced_count', 0)}件\n"

    if sync:
        content += f"- マッチ: {sync.get('matched', 0)}件\n"
        content += f"- エラー: {sync.get('errors', 0)}件\n"

    if rank:
        dist = rank.get('distribution', {})
        if dist:
            parts = [f"{k}:{v}" for k, v in sorted(dist.items())]
            content += f"- ランク分布: {', '.join(parts)}\n"

    if report:
        breakdown = report.get('status_breakdown', {})
        if breakdown:
            content += f"- ソースステータス: "
            content += ", ".join(f"{k}:{v}" for k, v in breakdown.items())
            content += "\n"

    content += "\n"
    _append_to_file(log_file, content)
    logger.info(f"[Router] eBay同期 → daily-operations/logs/")


def route_inventory_check(result: Dict, company_root: Path):
    """
    在庫チェック結果 → daily-operations/logs/YYYY-MM-DD.md に追記
    """
    if not result or not result.get('success'):
        return

    log_file = company_root / "daily-operations" / "logs" / f"{_today()}.md"
    results_data = result.get('results', {})

    content = f"\n## 在庫チェック ({_now_time()})\n\n"
    content += f"- チェック数: {result.get('checked_count', 0)}件\n"
    content += f"- 在庫有: {results_data.get('in_stock', 0)}件\n"
    content += f"- 在庫無: {results_data.get('out_of_stock', 0)}件\n"
    content += f"- ページなし: {results_data.get('page_not_found', 0)}件\n"
    content += f"- エラー: {results_data.get('error', 0)}件\n"

    # ソース別
    by_source = results_data.get('by_source', {})
    if by_source:
        content += "\n**ソース別:**\n\n"
        content += "| ソース | 在庫有 | 在庫無 | エラー |\n"
        content += "|--------|--------|--------|--------|\n"
        for source, stats in by_source.items():
            content += f"| {source} | {stats.get('in_stock', 0)} | {stats.get('out_of_stock', 0)} | {stats.get('error', 0)} |\n"

    # 変動があれば
    changes = result.get('changes', {})
    became_oos = changes.get('became_out_of_stock', [])
    if became_oos:
        content += f"\n**在庫切れ変動: {len(became_oos)}件**\n"
        for item in became_oos[:10]:
            content += f"- {item.get('sku', '?')}: {item.get('title', '')[:40]}\n"

    content += "\n"
    _append_to_file(log_file, content)
    logger.info(f"[Router] 在庫チェック → daily-operations/logs/")


def route_inventory_alert(result: Dict, company_root: Path):
    """
    在庫切れアラート → secretary/inbox/YYYY-MM-DD.md に追記
    """
    if not result or not result.get('success'):
        return

    alert_count = result.get('alert_count', 0)
    if alert_count == 0:
        return

    inbox_file = company_root / "secretary" / "inbox" / f"{_today()}.md"
    alerts = result.get('alerts', [])

    content = f"\n## ⚠ 在庫切れアラート ({_now_time()})\n\n"
    content += f"**{alert_count}件の在庫切れを検出**\n\n"

    for alert in alerts[:15]:
        sku = alert.get('sku', '?')
        source = alert.get('source', '?')
        change = alert.get('status_change', '?')
        name = alert.get('product_name', '')[:40]
        content += f"- `{sku}` [{source}] {change} — {name}\n"

    if alert_count > 15:
        content += f"- ...他 {alert_count - 15}件\n"

    content += "\n"
    _append_to_file(inbox_file, content)
    logger.info(f"[Router] 在庫アラート({alert_count}件) → secretary/inbox/")


def route_rival_detection(result: Dict, company_root: Path):
    """
    ライバル検出結果 → research/notes/YYYY-MM-DD-rival-report.md
    """
    if not result or not result.get('success'):
        return

    new_count = result.get('new_sellers_count', 0)
    total = result.get('total_scanned', 0)
    sellers = result.get('sellers', [])

    report_file = company_root / "research" / "notes" / f"{_today()}-rival-report.md"

    # 同日追記
    if report_file.exists():
        content = f"\n---\n## ライバル検出 ({_now_time()})\n\n"
    else:
        content = f"# ライバル検出レポート - {_today()}\n\n"
        content += f"## ライバル検出 ({_now_time()})\n\n"

    content += f"- スキャン: {total}セラー\n"
    content += f"- 新規検出: {new_count}件\n\n"

    if sellers:
        content += "| セラー | フィードバック | 競合商品数 | 初検出 |\n"
        content += "|--------|---------------|-----------|--------|\n"
        for s in sellers[:20]:
            content += f"| {s.get('seller', '?')} | {s.get('feedback_score', 0)} | {s.get('competing_count', 0)} | {s.get('first_seen', '')[:10]} |\n"
        content += "\n"

        # 競合商品の詳細
        for s in sellers[:5]:
            items = s.get('competing_items', [])
            if items:
                content += f"\n**{s['seller']}** の競合商品:\n"
                for item in items[:3]:
                    content += f"- ${item.get('price_usd', 0):.2f} — {item.get('keyword', '')}\n"
    else:
        content += "新規ライバルは検出されませんでした。\n"

    content += "\n"
    _append_to_file(report_file, content)
    logger.info(f"[Router] ライバル検出 → research/notes/")


def route_news_check(result: Dict, company_root: Path):
    """
    ニュース確認結果 → secretary/inbox/YYYY-MM-DD.md に追記
    高影響ニュースがあれば engineering/notes/ にも書き込む
    """
    if not result or not result.get('success'):
        return

    news_count = result.get('news_count', 0)
    if news_count == 0:
        return

    news = result.get('news', [])
    high_impact = [n for n in news if n.get('impact') == 'high']
    medium_impact = [n for n in news if n.get('impact') == 'medium']

    # 秘書の inbox に全ニュース
    inbox_file = company_root / "secretary" / "inbox" / f"{_today()}.md"

    content = f"\n## AI/Claude ニュース ({_now_time()})\n\n"
    content += f"検出: {news_count}件（高影響: {len(high_impact)}, 中影響: {len(medium_impact)}）\n\n"

    for n in news[:10]:
        impact_mark = {'high': '🔴', 'medium': '🟡', 'low': '⚪'}.get(n.get('impact', 'low'), '⚪')
        content += f"- {impact_mark} {n.get('title', '?')[:70]}"
        if n.get('matched_keyword'):
            content += f" [{n['matched_keyword']}]"
        content += f" ({n.get('source', '?')})\n"

    content += "\n"
    _append_to_file(inbox_file, content)

    # 高影響ニュースがあれば engineering にも通知
    if high_impact:
        eng_file = company_root / "engineering" / "notes" / f"{_today()}-ai-news-alert.md"
        eng_content = f"\n## 高影響AIニュース ({_now_time()})\n\n"
        eng_content += "以下のニュースはeBayツールに影響する可能性があります:\n\n"
        for n in high_impact:
            eng_content += f"- **{n.get('title', '?')}** [{n.get('matched_keyword', '')}]\n"
            eng_content += f"  ソース: {n.get('source', '?')}\n\n"
        _append_to_file(eng_file, eng_content)
        logger.info(f"[Router] 高影響ニュース → engineering/notes/")

    logger.info(f"[Router] ニュース({news_count}件) → secretary/inbox/")


def route_supplier_select(result: Dict, company_root: Path):
    """
    仕入先候補結果 → research/notes/YYYY-MM-DD-supplier-candidates.md
    """
    if not result or not result.get('success'):
        return

    product_count = result.get('product_count', 0)
    if product_count == 0:
        return

    suppliers = result.get('suppliers', [])
    report_file = company_root / "research" / "notes" / f"{_today()}-supplier-candidates.md"

    if report_file.exists():
        content = f"\n---\n## 仕入先候補 ({_now_time()})\n\n"
    else:
        content = f"# 仕入先候補レポート - {_today()}\n\n"
        content += f"## 仕入先候補 ({_now_time()})\n\n"

    content += f"対象商品: {product_count}件\n\n"

    for s in suppliers[:10]:
        sku = s.get('sku', '?')
        source = s.get('source', '?')
        content += f"### {sku} ({source})\n\n"

        candidates = s.get('supplier_candidates', [])
        if candidates:
            content += "| 候補 | スコア | 在庫 | 価格 | 配送日数 |\n"
            content += "|------|--------|------|------|----------|\n"
            for c in candidates:
                details = c.get('details', {})
                content += f"| {c.get('name', '?')} | {c.get('score', 0):.2f} | "
                content += f"{details.get('stock', '?')} | ¥{details.get('price', 0):,.0f} | "
                content += f"{details.get('shipping_days', '?')}日 |\n"
            content += "\n"

    _append_to_file(report_file, content)
    logger.info(f"[Router] 仕入先候補({product_count}件) → research/notes/")


def route_product_search(result: Dict, company_root: Path):
    """
    同等商品検索タスク準備結果 → secretary/inbox/ に通知
    """
    if not result or not result.get('success'):
        return

    tasks_prepared = result.get('tasks_prepared', 0)
    if tasks_prepared == 0:
        return

    inbox_file = company_root / "secretary" / "inbox" / f"{_today()}.md"
    content = f"\n## 同等商品検索タスク ({_now_time()})\n\n"
    content += f"- {tasks_prepared}件の検索タスクを準備しました\n"
    content += f"- ファイル: {result.get('tasks_file', 'equivalence_check_tasks.json')}\n"
    content += "\n"

    _append_to_file(inbox_file, content)
    logger.info(f"[Router] 検索タスク準備({tasks_prepared}件) → secretary/inbox/")


def route_email(result: Dict, company_root: Path):
    """
    メール取得結果 → secretary/inbox/ に追記（重要メールのみ）
    """
    if not result or not result.get('success'):
        return

    count = result.get('count', 0)
    if count == 0:
        return

    inbox_file = company_root / "secretary" / "inbox" / f"{_today()}.md"
    emails = result.get('emails', [])

    content = f"\n## メール確認 ({_now_time()})\n\n"
    content += f"eBay関連メール: {count}件\n\n"

    for email in emails[:10]:
        subject = email.get('subject', '(件名なし)')[:60]
        sender = email.get('from', '?')[:30]
        content += f"- **{subject}** — {sender}\n"

    content += "\n"
    _append_to_file(inbox_file, content)
    logger.info(f"[Router] メール({count}件) → secretary/inbox/")


# ============================================================
# メインルーター
# ============================================================

# タスク名 → ルーティング関数のマッピング
# W160 (2026-05-24): route_sales_tracking 削除. W149 で sales_tracking task が
# enabled:false 化 + task_order_alert.GetOrders に置換、W160 で物理削除済.

def route_price_optimization(result: Dict, company_root: Path):
    """価格最適化 → task内で secretary/inbox に保存済み。追加ルーティング不要"""
    pass


def route_data_sync(result: Dict, company_root: Path):
    """データ統合 → ログのみ"""
    if not result or not result.get('success'):
        return
    total = result.get('total_updated', 0)
    if total > 0:
        logger.info(f"[Router] データ統合: {total}件更新（DB内で完結）")


ROUTE_TABLE = {
    'ebay_sync': route_ebay_sync,
    'inventory_check': route_inventory_check,
    'inventory_alert': route_inventory_alert,
    'rival_detection': route_rival_detection,
    'news': route_news_check,
    'supplier_select': route_supplier_select,
    'product_search': route_product_search,
    'email_pickup': route_email,  # W244: results キーを 'email' → 'email_pickup' に統一
    'email': route_email,  # 旧キー互換 (route_all_results は results キーで引くため両対応)
    'price_optimization': route_price_optimization,
    'data_sync': route_data_sync,
    # research と company_secretary は自身で .company に書き込み済み
}


def route_all_results(results: Dict):
    """
    全タスクの結果を各部署にルーティング

    Args:
        results: daily_scheduler の execute_daily_tasks() が返す結果辞書
                 {'ebay_sync': {...}, 'inventory_check': {...}, ...}
    """
    company_root = get_company_root()
    if not company_root:
        logger.warning("[Router] .company フォルダが見つかりません。ルーティングをスキップ。")
        return

    # 日次ログのヘッダー（daily-operations）
    log_file = company_root / "daily-operations" / "logs" / f"{_today()}.md"
    if not log_file.exists():
        header = f"# 業務ログ - {_today()}\n"
        _write_file(log_file, header)

    # inbox のヘッダー
    inbox_file = company_root / "secretary" / "inbox" / f"{_today()}.md"
    if not inbox_file.exists():
        header = f"# 秘書 受信箱 - {_today()}\n"
        _write_file(inbox_file, header)

    routed_count = 0
    for task_name, result in results.items():
        if result is None:
            continue

        route_func = ROUTE_TABLE.get(task_name)
        if route_func:
            try:
                route_func(result, company_root)
                routed_count += 1
            except Exception as e:
                logger.error(f"[Router] {task_name} のルーティングエラー: {e}")

    logger.info(f"[Router] {routed_count}/{len(results)} タスクの結果を組織に配信しました")


def generate_daily_dashboard(results: Dict) -> str:
    """
    全タスク結果から組織ダッシュボードを生成

    Returns:
        Markdown形式のダッシュボード文字列
    """
    now = datetime.now()
    lines = []
    lines.append(f"# 組織ダッシュボード - {now.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # eBay同期
    ebay = results.get('ebay_sync', {})
    if ebay and ebay.get('success'):
        lines.append(f"**eBay**: {ebay.get('synced_count', 0)}件同期済")
    else:
        lines.append("**eBay**: 未同期")

    # 在庫
    inv = results.get('inventory_check', {})
    if inv and inv.get('success'):
        r = inv.get('results', {})
        lines.append(f"**在庫**: 有{r.get('in_stock', 0)} / 無{r.get('out_of_stock', 0)} / エラー{r.get('error', 0)}")
    else:
        lines.append("**在庫**: 未チェック")

    # アラート
    alert = results.get('inventory_alert', {})
    if alert and alert.get('alert_count', 0) > 0:
        lines.append(f"**アラート**: ⚠ {alert['alert_count']}件の在庫切れ変動")
    else:
        lines.append("**アラート**: なし")

    # ライバル
    rival = results.get('rival_detection', {})
    if rival and rival.get('success'):
        lines.append(f"**ライバル**: 新規{rival.get('new_sellers_count', 0)}件 / {rival.get('total_scanned', 0)}件スキャン")

    # ニュース
    news = results.get('news', {})
    if news and news.get('success'):
        high = news.get('high_impact_count', 0)
        total = news.get('news_count', 0)
        if high > 0:
            lines.append(f"**ニュース**: {total}件（⚠ 高影響{high}件）")
        else:
            lines.append(f"**ニュース**: {total}件")

    # メール (W244: results キーを 'email' → 'email_pickup' に統一、旧キーも互換読み)
    email = results.get('email_pickup') or results.get('email', {})
    if email and email.get('success'):
        lines.append(f"**メール**: {email.get('count', 0)}件")

    lines.append("")
    return "\n".join(lines)
