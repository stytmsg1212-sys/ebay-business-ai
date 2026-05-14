"""
Scheduler integration module - reads and parses scheduler.log
"""
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any


def parse_log_line(line: str) -> Dict[str, Any]:
    """
    Parse a Python logging format line into structured data.

    Format: 2026-04-11 06:56:36,209 - __main__ - INFO - Message here
    """
    # Pattern: YYYY-MM-DD HH:MM:SS,mmm - logger - LEVEL - message
    pattern = r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) - ([^-]+) - ([A-Z]+) - (.*)$'
    match = re.match(pattern, line.strip())

    if not match:
        return None

    timestamp_str, logger, level, message = match.groups()

    try:
        timestamp = datetime.strptime(timestamp_str.split(',')[0], '%Y-%m-%d %H:%M:%S')
    except:
        timestamp = None

    return {
        'timestamp': timestamp,
        'timestamp_str': timestamp_str,
        'logger': logger,
        'level': level,
        'message': message,
    }


def get_latest_execution_logs(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Read scheduler.log and return latest N parsed log entries.
    """
    log_file = Path(__file__).parent / "logs" / "scheduler.log"

    if not log_file.exists():
        return []

    logs = []
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Parse last N*5 lines (in case some are unparseable)
        for line in reversed(lines[-limit*5:]):
            parsed = parse_log_line(line)
            if parsed:
                logs.append(parsed)
                if len(logs) >= limit:
                    break

        # Reverse to get chronological order
        logs.reverse()
    except Exception as e:
        print(f"Error reading scheduler.log: {e}")
        return []

    return logs


def get_execution_summary() -> Dict[str, Any]:
    """
    Get summary of today's executions based on log levels.
    Reliable method using ERROR/WARNING levels instead of hardcoded strings.
    """
    logs = get_latest_execution_logs(limit=1000)
    today = datetime.now().date()

    today_logs = [l for l in logs if l['timestamp'] and l['timestamp'].date() == today]

    # Count by log level - more reliable than string matching
    success_count = len([l for l in today_logs if l['level'] in ['INFO']])
    failed_count = len([l for l in today_logs if l['level'] in ['ERROR', 'CRITICAL']])
    warning_count = len([l for l in today_logs if l['level'] == 'WARNING'])

    # Also count task completions by looking for 【完了】in INFO logs
    task_success = len([l for l in today_logs if l['level'] == 'INFO' and '【完了】' in l['message']])
    task_failed = len([l for l in today_logs if l['level'] == 'ERROR' and '【エラー】' in l['message']])

    return {
        'total': len(today_logs),
        'success': task_success,
        'failed': task_failed,
        'warnings': warning_count,
        'info_total': success_count,
        'today': str(today),
    }


def get_execution_by_time(hour: int) -> Dict[str, Any]:
    """
    Get execution details for a specific time (05, 11, 17, 22).
    """
    logs = get_latest_execution_logs(limit=500)
    today = datetime.now().date()

    matching_logs = [
        l for l in logs
        if l['timestamp']
        and l['timestamp'].date() == today
        and l['timestamp'].hour == hour
    ]

    return {
        'time': f"{hour:02d}:00",
        'logs': matching_logs,
        'count': len(matching_logs),
    }
