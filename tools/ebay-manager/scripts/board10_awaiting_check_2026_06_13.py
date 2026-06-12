# -*- coding: utf-8 -*-
"""依頼ボード#10 を awaiting_check 化 (verify_steps 付き、正規 API 経由)."""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import set_user_request_status  # noqa: E402

VERIFY = """1. MonoDeck「商品管理」で任意の商品行をクリック → 編集ゾーン下部に
   「🌍 eBaymag 国別出品 (UK / DE / FR / IT / ES / CA / AU)」expander が
   あることを確認 (6/11 プラン v2 反映済の 119 商品は productId 紐付け済)
2. 「🔄 eBaymag から現在状態を取得」を押す → 国別 checkbox が eBaymag の
   実状態に同期されることを確認 (例: SONY ICD-ST25 は UK のみ ON)
   ※ 前提: CDP Chrome (port 9222) で eBaymag にログイン済 + タブが開いている
3. checkbox を変更して「📤 eBaymag に反映」→ eBaymag 実画面で国別出品が
   切り替わることを確認 (itm 照合安全弁付き、差分なし時はボタン無効)
4. プラン v2 未反映の商品 (productId 未紐付け) は案内メッセージが出ることを確認"""

NOTE = (
    "商品管理タブに eBaymag 国別出品管理セクションを新設。"
    "(1) ebaymag_products テーブル v75 (ebay_item_id↔productId + 国別状態キャッシュ、"
    "6/11 実機ログから 119 件 seed 済) / "
    "(2) 6/11 プラン v2 反映で実証済の操作ロジックを ebaymag_driver にライブラリ化 "
    "(itm 照合・保存変動数チェック・リロード定着検証の安全弁 3 種を継承) / "
    "(3) UI: 状態取得 → 国別 checkbox → 差分のみ反映。誤 OFF 防止のため状態未取得では"
    "反映ボタン無効。Q1 実機 verify で Streamlit 配下の Playwright event loop 衝突 "
    "(エラー空表示) を発見 → subprocess 隔離 (supplier_scraper 前例) で根治し、"
    "実機で eBaymag からの状態取得成功を確認済。"
    "code-reviewer 2 巡 HIGH=0、回帰テスト 22 件 PASS。"
)

ok = set_user_request_status(
    10, "awaiting_check", note=NOTE, verify_steps=VERIFY, author="assistant",
)
print(f"board#10 -> awaiting_check: {ok}")
