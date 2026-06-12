# -*- coding: utf-8 -*-
"""依頼ボード#18 を awaiting_check 化 (verify_steps 付き、正規 API 経由)."""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import set_user_request_status  # noqa: E402

VERIFY = """1. MonoDeck「在庫監視」タブ → 「仕入先在庫切れ」の各商品カードを確認
   - 仕入先候補がある商品: 候補ごとに「採用」「不採用」ボタンだけが出る
     (旧: 確認チェック + 一括実行 + SKU/在庫の直接入力欄 → 全部撤去済)
   - 候補がない商品: 「はい、在庫を0にする」「いいえ、このまま様子見」の
     2 ボタンだけが出る
2. 「採用」を 1 回押すと、その場で eBay へ SKU 反映まで完了し、
   タブ最上部に写真/description のフォローアップ欄が出ることを確認
3. 反映後に「反映する」チェックや「📷 写真反映」ボタン等の謎の UI が
   出ないことを確認 (もしまだ謎のボタンが出たらスクリーンショットをください)
4. 「いいえ、このまま様子見」を押すと一覧から消えることを確認
   (eBay 在庫はそのまま。様子見解除は商品管理から risk_confirmed を戻す)
5. 一番下にあった「上記 N 件の在庫を一括で0にする」チェックボックスが
   無くなっていることを確認"""

NOTE = (
    "在庫監視タブを仕入先候補タブと同じ 1 クリック操作に全面簡素化。"
    "(1) 旧一括 UI (確認チェック + 一括実行 form + SKU/在庫直接編集 + "
    "一括在庫0 チェックボックス) を全撤去 / "
    "(2) 候補あり = 「採用」(accept→apply 正規経路で eBay SKU 反映まで即実行) + "
    "「不採用」、採用成功でタブ最上部に写真/description フォローアップ欄を展開 / "
    "(3) 候補なし = 「はい、在庫を0にする」「いいえ、このまま様子見」の 2 ボタン / "
    "(4) 「謎の設定ボタン」の正体 = 旧 UI の accepted 中間状態で出る"
    "「反映する」チェック+「📷 写真反映」ボタン → 単一「反映」ボタンに置換し根治 / "
    "(5) レビューで HIGH 1 件検出→根治: OOS 候補紐付けが SKU キーのままで、"
    "同一 SKU 共有 listing だと採用時の写真/description が別 listing に飛び得た → "
    "ebay_item_id キーに統一 (SKU 規約準拠) + 回帰テスト 2 件追加。"
    "code-reviewer 2 巡 HIGH=0、pytest 313 PASS (全体 2583 PASS)、"
    "Playwright 実機で新 UI 描画 + console エラー 0 確認済。"
)

ok = set_user_request_status(
    18, "awaiting_check", note=NOTE, verify_steps=VERIFY, author="assistant",
)
print(f"board#18 -> awaiting_check: {ok}")
