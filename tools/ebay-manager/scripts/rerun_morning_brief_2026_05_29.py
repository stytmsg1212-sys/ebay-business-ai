"""今朝 timeout 失敗した morning_brief を timeout=300 で再実行・検証.

直接 run_research_morning_brief を呼ぶ (scheduler batch_ctx 無しなので
task_execution_log への自動記録は無いが、brief 生成 + 成否確認が目的)。
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tasks.task_research_morning_brief import run_research_morning_brief  # noqa: E402

print("morning_brief 再実行 (timeout=300, Opus 4.8)... 60-300s")
res = run_research_morning_brief({})
print("=== RESULT ===")
print(json.dumps({k: (str(v)[:200] if k != "answer_preview" else str(v)[:300])
                  for k, v in res.items()}, ensure_ascii=False, indent=2))
print("SUCCESS:" , res.get("success"))
