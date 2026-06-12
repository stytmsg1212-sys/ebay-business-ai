# -*- coding: utf-8 -*-
"""依頼ボード#17 を awaiting_check 化 (verify_steps 付き、正規 API 経由)."""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import set_user_request_status  # noqa: E402

VERIFY = """1. Discord「eBay Manager」チャンネルで次回の売り切れ検知後に
   「売り切れ検知 → 仕入先候補探索 結果」embed が届くことを確認
   (候補あり/なし/失敗の集計 + 商品別の結果。従来は探索結果が一切通知されなかった)
2. 候補ありの商品は MonoDeck「仕入先候補」タブに保存されている → 採用/不採用を判断
3. MonoDeck「在庫監視」タブに「状態不明」セクションが新設されていることを確認
   (従来 UI から消えていた『不明』商品の一覧。GS-71N5 等の Yahoo 定額ページは
   今回の修正で自動判定が復活し、不明 9 件 → 6 件に減少)
4. CB100 (…2525) のような「探索後に再び売り切れた」商品が、7 日待たず
   即時に再探索対象へ入ることを次回検知時に確認"""

NOTE = (
    "真因 5 件を修正: (A) 売切商品→eBay listing の紐付けが URL 完全一致依存で乖離時に"
    "黙って脱落 → 監視リスト由来の ID 直結に変更 / (B) 7 日スロットルが『探索後の再売切』"
    "もブロック → 売切イベント後の探索有無で判定 (探索試行マーカー列 v74 新設、"
    "探索の無限ループも構造的に遮断) / (C) 『不明』商品が UI から完全に消えていた → "
    "状態不明セクション新設 / (D) Yahoo 定額出品が常に不明 stuck → ページ内 JSON で判定 / "
    "(E) 探索結果が通知されなかった → Discord embed 通知新設。"
    "code-reviewer 2 巡で HIGH=0、回帰テスト 20 件 + 関連 290 件 PASS、"
    "scheduler 再起動済 (新コード反映)。"
)

ok = set_user_request_status(
    17, "awaiting_check", note=NOTE, verify_steps=VERIFY, author="assistant",
)
print(f"board#17 -> awaiting_check: {ok}")
