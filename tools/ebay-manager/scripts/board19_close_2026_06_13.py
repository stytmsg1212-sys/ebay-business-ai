# -*- coding: utf-8 -*-
"""依頼ボード#19 クローズ + #17 質問回答 (2026-06-13).

① smoke 検証残骸 (supplier_candidates id 2085) を rejected 化
   - CLI 実機 smoke で作られた行。同一 URL (yahoo c1181810575) は user が
     候補 id 1974 で既に不採用判断済み + 紐付く listing (…6486) は 5/30 退役済み
   - user の既判断に整合させる 1 行 UPDATE (Q2: snapshot → 1件 → verify)
② board#19 を awaiting_check 化 (verify_steps 付き、正規 API 経由)
③ board#17 の user 質問「状態不明のタブはどうしたらいいか」へ回答し
   awaiting_check に戻す (verify_steps は既存を維持)
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from monitor.database import get_conn, set_user_request_status  # noqa: E402

# ---------------------------------------------------------------- ① id 2085
with get_conn() as conn:
    conn.row_factory = None
    snap = conn.execute(
        "SELECT id, ebay_item_id, candidate_url, status, match_score, "
        "candidate_price_jpy FROM supplier_candidates WHERE id = 2085"
    ).fetchone()
    print(f"[1] snapshot id2085: {snap}")
    if snap and snap[3] == "pending":
        # 整理理由は本スクリプト + session memory に記録 (note 列は存在しない)。
        # user_action_at は触らない (user 操作ではなく assistant の整理のため)
        cur = conn.execute(
            "UPDATE supplier_candidates SET status = 'rejected' "
            "WHERE id = 2085 AND status = 'pending'"
        )
        print(f"[1] updated rows = {cur.rowcount}")
    verify = conn.execute(
        "SELECT id, status FROM supplier_candidates WHERE id = 2085"
    ).fetchone()
    print(f"[1] verify: {verify}")

# ---------------------------------------------------------------- ② board#19
VERIFY_19 = """1. MonoDeck「在庫監視」タブで在庫切れ商品の「仕入先候補を即時検索」を押す
   (現在は在庫切れ一覧 0 件のため、次に在庫切れが出たタイミングで OK。
    一括検索ボタンでも同じ)
2. 実行中表示の後、結果が必ず何か表示されることを確認:
   - 新規候補あり → 「N 件保存」+ ページ更新で上に表示
   - 新規候補なし → 「新規候補なし」+ 内訳 (類似度基準未満 N / 既存・不採用済みと同一 N)
   - 探索失敗 → 「探索プロセスでエラー」(市場 0 件とは別表示)
   旧症状 (押しても何も反映されない) が再発しないこと
3. なお依頼の HIOKI 8972 自体は対応不要になっています:
   検索した listing は 5/30 の再出品で退役済み + 後継 listing は在庫 0 化済みのため
   在庫切れ一覧から消えるのが正常動作です (詳細は進捗ログ参照)"""

NOTE_19 = (
    "真因 4 層を特定し全て根治: "
    "(a) 検索の裏スレッドが画面状態に書き込めず結果が永遠に反映されなかった "
    "(ScriptRunContext 未結線) → 結線修正 / "
    "(b) 検索成功でも新規 0 件だと完全無表示 → 内訳表示を追加 / "
    "(c) 既存・不採用済みと同一 URL の重複除外が無音 → カウンタ表示追加 / "
    "(d) 最大の真因: MonoDeck (Streamlit) プロセス内ではブラウザ自動操作 "
    "(Playwright) が Windows の制約で起動できず、フリマ検索が常に『偽の市場 0 件』"
    "を返していた → 検索全体を別プロセス実行に変更して根治 (W228 FIX-E と同方式)。"
    "実機検証: 依頼の HIOKI 8972 で探索を実走し、ヤフオク候補 1 件 "
    "(¥21,780 / 利益見込 ¥62,737 / 類似度 72) の発見・保存に成功 = 修正が機能。"
    "ただし同一 URL は user が今朝既に不採用判断済みだったため、検証で出来た"
    "重複行は不採用に揃えて整理済み。また HIOKI の旧 listing (…6486) は 5/30 "
    "daily_relist で退役済み・後継 (…5035) は在庫 0 化済みのため、在庫切れ一覧に"
    "出ないのは正常。code-reviewer HIGH=0、回帰テスト 9+106 PASS、"
    "コマンドライン実機で候補発見〜保存まで成功、MonoDeck 再起動済 (新コード稼働中)。"
)

ok19 = set_user_request_status(
    19, "awaiting_check", note=NOTE_19, verify_steps=VERIFY_19, author="assistant",
)
print(f"[2] board#19 -> awaiting_check: {ok19}")

# ---------------------------------------------------------------- ③ board#17
NOTE_17 = (
    "ご質問『状態不明のタブはどうしたらいいか』への回答: 基本は放置で OK です。"
    "状態不明 = 仕入先ページから在庫有無を機械判定できなかった商品で、"
    "次回バッチ (毎日 02:30) で自動的に再判定されます。"
    "現在は 1 件のみ (Keyence CA-H2100M)。この 1 件は進行中のヤフオク"
    "オークションで、オークション進行中はページ構造上『不明』になりやすい"
    "だけで、実際は即決 ¥27,000 で購入可能なことを確認済みです。"
    "対応が必要なのは『同じ商品が何日も状態不明に残り続ける』場合のみで、"
    "その時は URL を開いて目視確認 → 売り切れなら在庫監視タブから在庫 0 化、"
    "在庫ありなら放置で大丈夫です。"
)

ok17 = set_user_request_status(
    17, "awaiting_check", note=NOTE_17, author="assistant",
)
print(f"[3] board#17 -> awaiting_check (回答済): {ok17}")
