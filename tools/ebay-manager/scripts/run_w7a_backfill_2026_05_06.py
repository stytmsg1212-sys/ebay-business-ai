"""W7-A market_analysis backfill: ma_count=0 の active listing 全件を処理"""
import sys, json, logging
from pathlib import Path

# scripts/ から実行する場合、プロジェクトルートを sys.path に追加
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 作業ディレクトリも統一 (config / logs を相対パスで開けるように)
import os
os.chdir(PROJECT_ROOT)

# .env から ANTHROPIC_API_KEY 等を環境変数化 (Haiku keyword 抽出に必須)
# 2026-05-06: load_dotenv 忘れで Haiku 無効 → fallback の低品質 keyword で
# Terapeak hit せず unknown 量産事故の再発防止
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/w7a_backfill_2026_05_06.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)

cfg_path = Path('config') / 'schedule_config.json'
config = {}
if cfg_path.exists():
    with open(cfg_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

from tasks.task_market_analysis_refresh import run_market_analysis_refresh

result = run_market_analysis_refresh(
    config=config,
    skip_recent_hours=24,
    stop_on_consecutive_failures=5,
    sleep_seconds=3.0,
)

print('=== RESULT ===')
print(json.dumps({k: v for k, v in result.items() if k != 'errors'}, ensure_ascii=False, indent=2))
print(f'errors (first 5): {result.get("errors", [])[:5]}')
