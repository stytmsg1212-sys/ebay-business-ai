"""eBay User Token をクリップボードから .env に設定する (2026-06-04 緊急復旧).

背景: OAuth refresh token 失効で EBAY_USER_TOKEN が hard expired → Trading API
(GetOrders/relist/価格改定) が全失敗。Developer Portal の「User Tokens」画面に
**Auth'n'Auth トークン (2027-11-26 まで有効)** が表示されており、ebay_client は
Trading API を <RequesterCredentials><eBayAuthToken> 方式で送る = このトークンを
そのまま使える。OAuth 再 consent 不要で即復旧する最短経路。

トークンをチャットに貼らずに済むよう、Portal の「Copy Token to Clipboard」で
コピーした値を **クリップボードから直接読み取り** .env に書く (transcript 非露出)。

使い方 (user が PowerShell で実行):
  1. Developer Portal の User Tokens 画面で「Copy Token to Clipboard」をクリック
  2. cd C:\\Users\\gucch\\projects\\claude\\tools\\ebay-manager
     python scripts/ebay_set_token_from_clipboard.py

  expires_at は Auth'n'Auth トークンの既定有効期限 (2027-11-26 07:13:36 UTC) を
  設定 = is_token_near_expiry が False になり、失効済 refresh token への
  無駄な auto-refresh 試行を止める (ログのエラー連発も解消)。
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.ebay_oauth_refresh import _load_env_dict, _write_env_values  # noqa: E402

# Portal 表示の Auth'n'Auth トークン有効期限 (Fri, 26 Nov 2027 07:13:36 GMT)。
# 別トークンを使う場合はこの定数を実際の期限に合わせて編集する。
_AUTHNAUTH_EXPIRES = datetime(2027, 11, 26, 7, 13, 36, tzinfo=timezone.utc)


def _read_clipboard() -> str:
    """Windows クリップボードを PowerShell Get-Clipboard で読む (改行結合)。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, timeout=15,
        )
        return (r.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        print(f"[error] クリップボード読取失敗: {e}")
        return ""


def main() -> int:
    token = _read_clipboard()
    if not token:
        print("[error] クリップボードが空です。Portal で「Copy Token to Clipboard」を押してから再実行してください。")
        return 2
    # eBay の User Token は 'v^1.1#' 接頭辞。複数行になっていれば結合済。
    token = "".join(token.split())  # 途中の改行/空白を除去 (Portal 表示折返し対策)
    if not token.startswith("v^") or len(token) < 80:
        print(f"[error] クリップボードの中身が eBay トークン形式ではありません "
              f"(先頭='{token[:6]}...', len={len(token)})。コピー対象を確認してください。")
        return 2

    expires_at = int(_AUTHNAUTH_EXPIRES.timestamp())
    cur = _load_env_dict()
    old_len = len(cur.get("EBAY_USER_TOKEN") or "")
    _write_env_values({
        "EBAY_USER_TOKEN": token,
        "EBAY_USER_TOKEN_EXPIRES_AT": str(expires_at),
    })
    print("[ok] .env を更新しました (トークン値は非表示):")
    print(f"  EBAY_USER_TOKEN: len {old_len} -> {len(token)}")
    print(f"  EBAY_USER_TOKEN_EXPIRES_AT = {expires_at} "
          f"({_AUTHNAUTH_EXPIRES.isoformat()})")
    print("\n次: アシスタントに「終わった」と伝えてください。GetOrders で復旧確認します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
