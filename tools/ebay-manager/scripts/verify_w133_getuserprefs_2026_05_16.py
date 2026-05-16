"""W133 item2 (2026-05-16): GetUserPreferences 本番実機検証 (read-only / one-shot).

目的:
  - eBay Trading API GetUserPreferences で Out-of-Stock Control の ON/OFF を
    実本番アカウントから読み取り、W133 の Defect ゲート前提を実機裏取りする.
  - 検証は **修正済みの inventory_sync._get_credentials() 経由** で行い、
    2026-05-16 に発見・修正した creds dict→tuple バグの end-to-end も同時検証.

安全性:
  - GetUserPreferences は **読み取り専用**. listing への副作用ゼロ.
  - 認証値そのものは出力しない (security.md: 認証情報を log/print 禁止).

期待値:
  - True  : OOS Control ON (5/16 user が ON 化済の事実と一致 = 検証 PASS)
  - False : OFF (想定外 = 設定が実は OFF / アカウント不一致を疑う)
  - None  : 通信/認証/parse 失敗 (Q0: 不明を成功と偽装しない)
"""
import sys

sys.path.insert(0, r'C:/Users/gucch/projects/claude/tools/ebay-manager')

from monitor.inventory_sync import _get_credentials
from monitor.ebay_client import get_out_of_stock_control_enabled


def main() -> int:
    creds = _get_credentials()
    if not creds:
        print('RESULT: FAIL (eBay 認証が解決できない = _get_credentials None)')
        print('  → .env の EBAY_APP_ID/DEV_ID/CERT_ID/USER_TOKEN を確認')
        return 1

    # creds が「値タプル」であることを安全に可視化 (値そのものは出さない).
    app_id, dev_id, cert_id, user_token = creds
    print('creds 解決 OK (修正済 _get_credentials = 値タプル):')
    print(f'  app_id    : len={len(app_id)} nonempty={bool(app_id)}')
    print(f'  dev_id    : len={len(dev_id)} nonempty={bool(dev_id)}')
    print(f'  cert_id   : len={len(cert_id)} nonempty={bool(cert_id)}')
    print(
        f'  user_token: len={len(user_token)} '
        f"starts_v^={user_token.startswith('v^')}"
    )
    # 旧バグ回帰の即時検知: 値がキー文字列なら literal 一致する.
    if app_id == 'app_id' or user_token == 'user_token':
        print('RESULT: FAIL (creds がキー文字列 = dict→tuple バグ再発)')
        return 1

    print('--- GetUserPreferences (本番, 読み取り専用) 実行 ---')
    enabled = get_out_of_stock_control_enabled(
        app_id, dev_id, cert_id, user_token
    )
    print(f'OutOfStockControlPreference = {enabled!r}')

    if enabled is True:
        print('RESULT: PASS (OOS Control ON = 5/16 user ON 化と一致)')
        return 0
    if enabled is False:
        print(
            'RESULT: UNEXPECTED (OOS Control OFF). '
            '5/16 ON 化と矛盾 → アカウント不一致 or 設定未反映を要調査'
        )
        return 2
    print(
        'RESULT: INCONCLUSIVE (None = 通信/認証/parse 失敗). '
        'Q0: 不明を成功と偽装しない。ログ確認要'
    )
    return 3


if __name__ == '__main__':
    sys.exit(main())
