"""
Company integration module - reads /company directory for TODO and research
"""
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any


def get_company_root() -> Path:
    """Get the .company directory path."""
    # プロジェクトルート（ebay-manager の2階層上）
    project_root = Path(__file__).parent.parent.parent / ".company"
    if project_root.exists():
        return project_root

    # フォールバック: ホームディレクトリ
    company_path = Path.home() / ".company"
    if company_path.exists():
        return company_path

    return None


def get_today_todos() -> str:
    """
    Read today's TODO from secretary/todos/YYYY-MM-DD.md
    """
    company_root = get_company_root()
    if not company_root:
        return "### TODO リストが見つかりません\n\n.company ディレクトリが見つかりません。"

    today = datetime.now().strftime("%Y-%m-%d")
    todo_file = company_root / "secretary" / "todos" / f"{today}.md"

    if todo_file.exists():
        try:
            content = todo_file.read_text(encoding='utf-8')
            return content
        except Exception as e:
            return f"### TODO 読み込みエラー\n\n{str(e)}"

    return "### 今日の TODO\n\n今日の TODO はまだ作成されていません。"


def _get_tasks_file(filename: str) -> Path:
    """Get path to a tasks file in secretary/todos/."""
    company_root = get_company_root()
    if not company_root:
        return None
    return company_root / "secretary" / "todos" / filename


def parse_tasks(filepath: Path) -> List[Dict[str, Any]]:
    """Parse markdown task list into structured data."""
    if not filepath or not filepath.exists():
        return []

    content = filepath.read_text(encoding='utf-8')
    tasks = []
    current_section = ""

    for line in content.split('\n'):
        stripped = line.strip()
        if stripped.startswith('## '):
            current_section = stripped[3:].strip()
        elif stripped.startswith('- [ ] ') or stripped.startswith('- [x] '):
            done = stripped.startswith('- [x] ')
            text = stripped[6:].strip()

            # Parse fields: task text | 優先度: X | 期限: Y | 完了: Z
            parts = [p.strip() for p in text.split('|')]
            task_name = parts[0]
            priority = "通常"
            deadline = ""
            completed_date = ""
            gmail_id = ""
            link = ""
            for part in parts[1:]:
                if part.startswith('優先度:') or part.startswith('優先度：'):
                    priority = part.split(':', 1)[-1].split('：', 1)[-1].strip()
                elif part.startswith('期限:') or part.startswith('期限：'):
                    deadline = part.split(':', 1)[-1].split('：', 1)[-1].strip()
                elif part.startswith('完了:') or part.startswith('完了：'):
                    completed_date = part.split(':', 1)[-1].split('：', 1)[-1].strip()
                elif part.startswith('gmail:') or part.startswith('gmail：'):
                    gmail_id = part.split(':', 1)[-1].split('：', 1)[-1].strip()
                    link = f"https://mail.google.com/mail/u/0/#inbox/{gmail_id}"
                elif part.startswith('link:') or part.startswith('link：'):
                    link = part.split(':', 1)[-1].split('：', 1)[-1].strip()

            tasks.append({
                'done': done,
                'name': task_name,
                'priority': priority,
                'deadline': deadline,
                'completed_date': completed_date,
                'gmail_id': gmail_id,
                'link': link,
                'section': current_section,
                'raw': stripped,
            })

    return tasks


def get_active_tasks() -> List[Dict[str, Any]]:
    """Get all active (incomplete) tasks."""
    filepath = _get_tasks_file("active.md")
    return parse_tasks(filepath)


def get_archived_tasks() -> List[Dict[str, Any]]:
    """Get all archived (completed) tasks."""
    filepath = _get_tasks_file("archive.md")
    return parse_tasks(filepath)


def complete_task(task_name: str) -> bool:
    """Move a task from active.md to archive.md."""
    active_path = _get_tasks_file("active.md")
    archive_path = _get_tasks_file("archive.md")
    if not active_path or not active_path.exists():
        return False

    content = active_path.read_text(encoding='utf-8')
    today = datetime.now().strftime("%Y-%m-%d")

    # Find the line containing this task
    lines = content.split('\n')
    new_lines = []
    removed_task = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('- [ ] ') and task_name in stripped:
            removed_task = stripped
            continue
        new_lines.append(line)

    if not removed_task:
        return False

    # Remove empty sections (section header followed by no tasks)
    cleaned = _clean_empty_sections('\n'.join(new_lines))
    active_path.write_text(cleaned, encoding='utf-8')

    # Add to archive
    archive_entry = removed_task.replace('- [ ] ', '- [x] ')
    # Add completion date
    if '完了:' not in archive_entry and '完了：' not in archive_entry:
        archive_entry += f" | 完了: {today}"

    if archive_path and archive_path.exists():
        archive_content = archive_path.read_text(encoding='utf-8')
        # Insert after the header comment
        marker = "<!-- 完了タスクは新しいものが上に追加されます -->"
        if marker in archive_content:
            archive_content = archive_content.replace(
                marker,
                f"{marker}\n\n### {today}\n{archive_entry}"
            )
        else:
            archive_content += f"\n### {today}\n{archive_entry}\n"
        archive_path.write_text(archive_content, encoding='utf-8')

    return True


def _clean_empty_sections(content: str) -> str:
    """Remove section headers that have no tasks under them."""
    lines = content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith('## '):
            # Check if next non-empty line is a task or another section
            has_tasks = False
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                if stripped == '':
                    j += 1
                    continue
                if stripped.startswith('- [ ] ') or stripped.startswith('- [x] '):
                    has_tasks = True
                break
            if has_tasks:
                result.append(lines[i])
            # else: skip the empty section header
        else:
            result.append(lines[i])
        i += 1
    return '\n'.join(result)


def get_latest_research() -> str:
    """
    Get the most recent research file from .company/research/
    """
    company_root = get_company_root()
    if not company_root:
        return "### リサーチが見つかりません\n\n.company ディレクトリが見つかりません。"

    research_dir = company_root / "research"
    if not research_dir.exists():
        return "### リサーチフォルダが見つかりません\n\nまだリサーチが実施されていません。"

    # Get all markdown files recursively
    files = list(research_dir.glob("**/*.md"))
    if not files:
        return "### リサーチ結果がまだありません\n\nリサーチが実施されていません。"

    # Get the most recently modified file
    latest = max(files, key=lambda f: f.stat().st_mtime)

    try:
        content = latest.read_text(encoding='utf-8')
        # Truncate to first 800 chars
        if len(content) > 800:
            content = content[:800] + "\n\n...(続く)"
        return content
    except Exception as e:
        return f"### リサーチ読み込みエラー\n\n{str(e)}"


def get_inbox_items() -> List[Dict[str, Any]]:
    """
    Get recent items from secretary/inbox/
    """
    company_root = get_company_root()
    if not company_root:
        return []

    inbox_dir = company_root / "secretary" / "inbox"
    if not inbox_dir.exists():
        return []

    items = []
    for file in sorted(inbox_dir.glob("*.md"), reverse=True)[:5]:
        try:
            content = file.read_text(encoding='utf-8')
            items.append({
                'filename': file.name,
                'mtime': file.stat().st_mtime,
                'content': content[:200] + "..." if len(content) > 200 else content,
            })
        except:
            pass

    return items


def get_company_status() -> Dict[str, Any]:
    """
    Get overall /company status.
    """
    company_root = get_company_root()

    return {
        'exists': company_root is not None,
        'path': str(company_root) if company_root else "Not found",
        'has_secretary': (company_root / "secretary").exists() if company_root else False,
        'has_research': (company_root / "research").exists() if company_root else False,
        'has_finance': (company_root / "finance").exists() if company_root else False,
    }


def get_today_routine_result() -> Dict[str, Any]:
    """
    Get today's secretary routine result from routine_results/YYYY-MM-DD-routine.json
    """
    company_root = get_company_root()
    if not company_root:
        return {
            'exists': False,
            'message': '.company ディレクトリが見つかりません'
        }

    today = datetime.now().strftime("%Y-%m-%d")
    routine_file = company_root / "secretary" / "routine_results" / f"{today}-routine.json"

    if not routine_file.exists():
        return {
            'exists': False,
            'message': f'本日のルーティン結果がまだ生成されていません（{today}）'
        }

    try:
        with open(routine_file, 'r', encoding='utf-8') as f:
            result = json.load(f)
        result['exists'] = True
        return result
    except Exception as e:
        return {
            'exists': False,
            'message': f'ルーティン結果読み込みエラー: {str(e)}'
        }
