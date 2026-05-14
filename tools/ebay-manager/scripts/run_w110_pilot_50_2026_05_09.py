"""W110(4) pilot 50 件 re-scrape (one-shot, 2026-05-09).

W110(2) 新仕様 (MIN_SAMPLE_SIZE=3 / MIN_SAMPLE_SIZE_US_ONLY=5 / dayRange=365) +
W110(1) DOM fix + W110(3) request 間隔ジッタ の効果検証 pilot.

期待動作:
  - dayRange=365 で sample 数が増え、unknown 比率が下がる
  - DOM 残留 bug は networkidle 待機 + reload retry で抑止
  - ジッタ (sleep * [0.7, 1.5]) で固定間隔の anti-bot pattern 回避
  - 50 件で 12-15 分想定 (3s ジッタ平均 = ~3.45s + scrape ~10s = 13.5s/件)

abort 条件:
  - stop_on_consecutive_failures=5 で連続 5 件失敗時停止 (anti-bot 検知 fast-fail)
  - thread timeout 180s (1 件単位)

実行: python scripts/run_w110_pilot_50_2026_05_09.py
ログ: logs/w110_pilot_50_2026_05_09.log
"""
import sys
import json
import logging
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/w110_pilot_50_2026_05_09.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)

cfg_path = Path('config') / 'schedule_config.json'
config = {}
if cfg_path.exists():
    with open(cfg_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

from tasks.task_market_analysis_refresh import run_market_analysis_refresh

print("=" * 60)
print("W110(4) pilot 50 件 re-scrape - 2026-05-09")
print("新仕様: MIN_SAMPLE_SIZE=3 / MIN_SAMPLE_SIZE_US_ONLY=5 / dayRange=365 + ジッタ")
print("=" * 60)

result = run_market_analysis_refresh(
    config=config,
    limit=50,                              # pilot
    skip_recent_hours=24,                  # 5/9 07 時の 39 件 skip = OOM fix verify は残 11 件で
    stop_on_consecutive_failures=5,
    sleep_seconds=3.0,                     # 3s * jitter [0.7, 1.5] = 2.1-4.5s
)

print('\n' + "=" * 60)
print('=== W110(4) pilot 50 RESULT ===')
print(json.dumps({k: v for k, v in result.items() if k != 'errors'}, ensure_ascii=False, indent=2))
print(f'errors (first 5): {result.get("errors", [])[:5]}')
print("=" * 60)
