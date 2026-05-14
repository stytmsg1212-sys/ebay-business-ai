"""
実行ログ記録とレポート生成モジュール
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


def setup_execution_logger(task_name: str) -> logging.Logger:
    """
    実行タスク用のロガーを作成
    """
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    logger = logging.getLogger(f"execution.{task_name}")

    # 既存ハンドラーをクリア
    logger.handlers.clear()

    # ファイルハンドラーを追加
    handler = logging.FileHandler(
        log_dir / "scheduler.log",
        encoding='utf-8'
    )
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    return logger


def log_execution_result(
    task_name: str,
    status: str,  # "success", "failed", "running"
    message: str,
    details: Dict[str, Any] = None,
    execution_time_sec: float = None
) -> Dict[str, Any]:
    """
    実行結果をログに記録

    Args:
        task_name: タスク名（inventory_check, product_search, ebay_sync）
        status: ステータス（success/failed/running）
        message: メッセージ
        details: 詳細情報
        execution_time_sec: 実行時間（秒）

    Returns:
        記録されたログエントリ
    """
    logger = setup_execution_logger(task_name)

    timestamp = datetime.now().isoformat()

    # ログレベルを決定
    log_level = {
        'success': logging.INFO,
        'failed': logging.ERROR,
        'running': logging.INFO,
    }.get(status, logging.INFO)

    # メッセージを構築
    log_message = f"[{task_name}] {status.upper()}: {message}"
    if execution_time_sec:
        log_message += f" ({execution_time_sec:.1f}s)"

    logger.log(log_level, log_message)

    # 記録エントリを作成
    entry = {
        'timestamp': timestamp,
        'task_name': task_name,
        'status': status,
        'message': message,
        'execution_time_sec': execution_time_sec,
        'details': details or {},
    }

    return entry


def save_execution_history(task_name: str, result: Dict[str, Any]) -> bool:
    """
    実行結果を JSON ファイルに保存

    Args:
        task_name: タスク名
        result: 実行結果

    Returns:
        保存成功の可否
    """
    try:
        history_dir = Path(__file__).parent / "data" / "execution_history"
        history_dir.mkdir(parents=True, exist_ok=True)

        # 日付ごとのファイルを作成
        today = datetime.now().strftime("%Y-%m-%d")
        history_file = history_dir / f"{today}_{task_name}_history.json"

        # 既存のデータを読み込む
        history = []
        if history_file.exists():
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)

        # 新しいエントリを追加
        history.append({
            'timestamp': datetime.now().isoformat(),
            'result': result,
        })

        # 保存（最新100件のみ）
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history[-100:], f, ensure_ascii=False, indent=2)

        return True
    except Exception as e:
        logger.error(f"履歴保存エラー: {e}", exc_info=True)
        return False


def send_discord_notification(webhook_url: str, task_name: str, status: str, details: Dict[str, Any] = None) -> bool:
    """
    Discord に実行結果を通知

    Args:
        webhook_url: Discord Webhook URL
        task_name: タスク名
        status: ステータス
        details: 詳細情報

    Returns:
        送信成功の可否
    """
    if not webhook_url:
        return False

    try:
        import httpx

        # ステータスに応じた色を決定
        color_map = {
            'success': 65280,   # 緑
            'failed': 16711680,  # 赤
            'running': 16776960, # 黄
        }
        color = color_map.get(status, 3447003)  # 灰色

        # Embed を構築
        embed = {
            "title": f"タスク実行: {task_name}",
            "color": color,
            "fields": [
                {"name": "ステータス", "value": status.upper(), "inline": True},
                {"name": "実行時刻", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": True},
            ],
            "timestamp": datetime.now().isoformat(),
        }

        # 詳細情報を追加
        if details:
            for key, value in details.items():
                if key not in ["timestamp", "task_name", "status"]:
                    embed["fields"].append({
                        "name": str(key),
                        "value": str(value)[:200],  # 200文字制限
                        "inline": False,
                    })

        payload = {
            "embeds": [embed],
        }

        # 送信
        response = httpx.post(webhook_url, json=payload, timeout=10.0)
        return response.status_code == 204
    except Exception as e:
        logger.error(f"Discord 通知エラー: {e}", exc_info=True)
        return False


def get_execution_statistics(task_name: str, days: int = 7) -> Dict[str, Any]:
    """
    タスクの実行統計を取得

    Args:
        task_name: タスク名
        days: 過去 N 日間

    Returns:
        統計情報
    """
    try:
        history_dir = Path(__file__).parent / "data" / "execution_history"
        if not history_dir.exists():
            return {
                'task_name': task_name,
                'total_executions': 0,
                'successful': 0,
                'failed': 0,
                'success_rate': 0.0,
            }

        total = 0
        successful = 0
        failed = 0

        # 過去 N 日のファイルを探す
        from datetime import timedelta
        for i in range(days):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            history_file = history_dir / f"{date}_{task_name}_history.json"

            if history_file.exists():
                with open(history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                    for entry in history:
                        result = entry.get('result', {})
                        total += 1
                        if result.get('status') == 'success':
                            successful += 1
                        elif result.get('status') == 'failed':
                            failed += 1

        success_rate = (successful / total * 100) if total > 0 else 0.0

        return {
            'task_name': task_name,
            'total_executions': total,
            'successful': successful,
            'failed': failed,
            'success_rate': success_rate,
            'period_days': days,
        }
    except Exception as e:
        logger.warning(f"統計取得エラー: {e}", exc_info=True)
        return {}
