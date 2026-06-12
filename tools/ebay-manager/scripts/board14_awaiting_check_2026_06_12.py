# -*- coding: utf-8 -*-
"""依頼ボード#14 を awaiting_check 化 (verify_steps 付き、正規 API 経由)."""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import set_user_request_status  # noqa: E402

VERIFY = """1. MonoDeck「在庫監視」タブを開く
2. 「状態不明」セクションが新設されていることを確認 (従来どこにも出なかった『不明』商品の一覧)
3. PayPay フリマの 2 商品が正しく再分類されたことを確認:
   - KEYENCE KV-XLE02 (…9000) = 「ページなし」(在庫切れ側に表示)
   - KEYENCE FD-Q10C (…9142) = 「在庫無」(在庫切れ側に表示)
4. PayPay フリマで「不明」のまま残っている商品が 0 件であること"""

NOTE = (
    "22:10 batch (22:45 完了) で PayPay 再分類を実機確認: KV-XLE02=ページなし / "
    "FD-Q10C=在庫無、PayPay 不明残 0 件。検知もれの真因 (banner 画像誤取得→"
    "判定テキスト不一致) は #17 の D 修正 (Yahoo) と同系統で、PayPay 側は "
    "site_config 修正済み。"
)

ok = set_user_request_status(
    14, "awaiting_check", note=NOTE, verify_steps=VERIFY, author="assistant",
)
print(f"board#14 -> awaiting_check: {ok}")
