"""2026-05-03 市場分析バッチ全件実行 (W7-A primary_market 割り振り再開).

5/1 で 31 件分析済 → そこで停止していた残 411 件を全件処理する one-shot script.

設定:
  - skip_recent_hours=72 で 5/1 分析済 31 件を skip (誤再分析防止)
  - sleep_seconds=3.0 (eBay 規制回避)
  - stop_on_consecutive_failures=5 (連続失敗自動停止)
  - use_ai_keyword=True (Haiku でタイトル → keyword 抽出)
  - day_range=90 (Terapeak 集計期間)

前提:
  - CDP Chrome (port 9222) 起動済 + Terapeak ページ表示 + eBay ログイン済 (verify 済)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# .env を load (project root の .env から ANTHROPIC_API_KEY 等を環境変数化)
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(_env_path)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

from tasks.task_market_analysis_refresh import run_market_analysis_refresh

cfg_path = Path(__file__).resolve().parent.parent / "config" / "schedule_config.json"
cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}

result = run_market_analysis_refresh(
    cfg,
    limit=None,                          # 全件
    use_ai_keyword=True,                 # Haiku 使用
    skip_recent_hours=72,                # 5/1 分析済 (~48h 前) を skip
    stop_on_consecutive_failures=5,
    sleep_seconds=3.0,
    day_range=90,
)

print("=" * 60)
print("FINAL RESULT:")
print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
