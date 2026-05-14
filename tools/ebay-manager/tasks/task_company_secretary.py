#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: 秘書ルーティン - /company 秘書室の日次処理
メール確認、TODO繰越、デイリーリサーチの3点セット
"""

import sys
import json
import logging
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)


def get_company_root():
    """Get the .company directory path."""
    # プロジェクトルート（ebay-manager の2階層上）
    project_root = Path(__file__).parent.parent.parent.parent / ".company"
    if project_root.exists():
        return project_root

    # フォールバック: ホームディレクトリ
    company_path = Path.home() / ".company"
    if company_path.exists():
        return company_path
    return None


def get_today_date():
    """今日の日付を取得"""
    return datetime.now().strftime("%Y-%m-%d")


def load_previous_todos():
    """直近のTODOファイルから未完了タスクを読み込む（複数日遡り対応）"""
    try:
        company_root = get_company_root()
        if not company_root:
            logger.warning(".company フォルダが見つかりません")
            return []

        todos_dir = company_root / "secretary" / "todos"
        if not todos_dir.exists():
            logger.warning(f"TODOディレクトリが見つかりません: {todos_dir}")
            return []

        today = datetime.now().strftime("%Y-%m-%d")

        # 既存のTODOファイルを日付降順で取得し、今日より前の最新を探す
        todo_files = sorted(todos_dir.glob("*.md"), reverse=True)
        prev_todo_file = None
        for f in todo_files:
            date_part = f.stem  # "2026-04-08"
            if date_part < today:
                prev_todo_file = f
                break

        if not prev_todo_file:
            logger.info("過去のTODOファイルが見つかりません")
            return []

        logger.info(f"繰越元TODOファイル: {prev_todo_file.name}")
        content = prev_todo_file.read_text(encoding='utf-8')

        # 未完了タスク（- [ ]）を抽出
        pending_todos = []
        for line in content.split('\n'):
            if line.strip().startswith('- [ ]'):
                pending_todos.append(line.strip())

        logger.info(f"未完了TODO: {len(pending_todos)}件（{prev_todo_file.stem}から繰越）")
        return pending_todos

    except Exception as e:
        logger.error(f"TODO読み込みエラー: {e}")
        return []


def create_today_todo(pending_todos):
    """本日のTODOファイルを作成"""
    try:
        company_root = get_company_root()
        if not company_root:
            return False

        today = get_today_date()
        today_todo_file = company_root / "secretary" / "todos" / f"{today}.md"

        # ファイルが既に存在する場合はスキップ
        if today_todo_file.exists():
            logger.info(f"本日のTODOファイルは既に存在します: {today}")
            return True

        # 新規作成
        content = f"# TODO - {today}\n\n"

        if pending_todos:
            content += "## 前日繰越\n\n"
            for todo in pending_todos:
                content += f"{todo}\n"
            content += "\n"

        content += "## 本日のタスク\n\n"
        content += "## 完了\n\n"

        today_todo_file.write_text(content, encoding='utf-8')
        logger.info(f"本日のTODOファイルを作成しました: {today}")
        return True

    except Exception as e:
        logger.error(f"TODOファイル作成エラー: {e}")
        return False


def get_email_summary(config):
    """メール確認結果を取得"""
    try:
        # 同一ディレクトリのモジュールをインポート
        spec = importlib.util.spec_from_file_location(
            "task_email_pickup",
            Path(__file__).parent / "task_email_pickup.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.run_email_pickup(config)

        return {
            'checked': result.get('success', False),
            'count': result.get('count', 0),
            'emails': result.get('emails', []),
            'status': result.get('status', 'unknown')
        }
    except Exception as e:
        logger.error(f"メール確認エラー: {e}")
        return {
            'checked': False,
            'count': 0,
            'emails': [],
            'status': 'error',
            'error': str(e)
        }


def get_research_summary(config):
    """リサーチ結果を取得 (W21: 死蔵化により本体削除).

    2026-04-26 W21: task_research.py の出力 .company/research/notes/*.md は
    DASHBOARD から削除済 (4/23) で誰も参照しないため task_research を削除した.
    本関数は API 互換のためスタブとして残す (将来 Research 脳 W23 で代替予定).
    """
    return {
        'executed': False,
        'topics': [],
        'results': [],
        'status': 'deprecated_w21',
        'note': 'task_research deleted in W21. See ROADMAP W23 (Research 脳) for replacement.',
    }


def save_routine_result(result):
    """秘書ルーティン結果をJSON保存"""
    try:
        company_root = get_company_root()
        if not company_root:
            logger.warning(".company フォルダが見つかりません")
            return False

        result_dir = company_root / "secretary" / "routine_results"
        result_dir.mkdir(parents=True, exist_ok=True)

        today = get_today_date()
        result_file = result_dir / f"{today}-routine.json"

        with open(result_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        logger.info(f"ルーティン結果を保存しました: {result_file}")
        return True

    except Exception as e:
        logger.error(f"結果保存エラー: {e}")
        return False


def run_company_secretary(config):
    """
    秘書ルーティン実行: メール確認 → TODO繰越 → リサーチ（スタブ）

    Args:
        config: 設定辞書

    Returns:
        {
            'success': bool,
            'timestamp': str,
            'email': dict,
            'todo_carried_over': int,
            'todo_file_created': bool,
            'research': dict,
            'message': str
        }
    """

    logger.info("【開始】秘書ルーティン")
    start_time = datetime.now()

    try:
        # Step 1: メール確認
        logger.info("【Step 1】メール確認")
        email_result = get_email_summary(config)

        # Step 2: 前日TODOを読み込んで本日に繰越
        logger.info("【Step 2】TODO繰越")
        pending_todos = load_previous_todos()
        todo_created = create_today_todo(pending_todos)

        # Step 3: リサーチ確認
        logger.info("【Step 3】デイリーリサーチ")
        research_result = get_research_summary(config)

        # 結果をまとめる
        result = {
            'timestamp': datetime.now().isoformat(),
            'date': get_today_date(),
            'status': 'success',
            'email': email_result,
            'todo': {
                'carried_over': len(pending_todos),
                'file_created': todo_created
            },
            'research': research_result,
            'execution_time_sec': (datetime.now() - start_time).total_seconds()
        }

        # 結果を保存
        save_routine_result(result)

        logger.info(f"【完了】秘書ルーティン (実行時間: {result['execution_time_sec']:.1f}秒)")

        return {
            'success': True,
            'timestamp': result['timestamp'],
            'email': email_result,
            'todo_carried_over': len(pending_todos),
            'todo_file_created': todo_created,
            'research': research_result,
            'message': f"秘書ルーティン完了: メール{email_result['count']}件、TODO繰越{len(pending_todos)}件"
        }

    except Exception as e:
        logger.error(f"秘書ルーティンエラー: {e}")
        return {
            'success': False,
            'timestamp': datetime.now().isoformat(),
            'email': {},
            'todo_carried_over': 0,
            'todo_file_created': False,
            'research': {},
            'error': str(e)
        }
