"""eBay OAuth 再 consent ヘルパ (2026-06-04, W-token).

EBAY_REFRESH_TOKEN が失効 (invalid / 18ヶ月超 / revoke) して
ebay_oauth_refresh の自動 refresh が 400 になった時に、**新しい refresh token を
取り直す** ための 2 段ツール。scope は monitor.ebay_oauth_refresh._SCOPES と
完全一致させる (api_scope + sell.inventory/account/fulfillment/finances/marketing)
ので、Developer Portal の User Token ツールより scope 漏れが無い。

トークン値は一切表示しない (security.md 遵守)。.env のみ更新。

使い方 (user がセッションで `! ` 付きで実行):

  # ① 同意 URL を表示 → ブラウザで開く → セラーアカウントでログイン&同意
  ! python scripts/ebay_oauth_consent.py url --runame "<RuName>"

  # ② リダイレクト先 URL の ?code=... の code をコピーして渡す → .env 更新
  ! python scripts/ebay_oauth_consent.py exchange --runame "<RuName>" --code "<code>"

  <RuName> = eBay Developer Portal の Redirect URL name
            (Application Keys → Production → User Tokens → "Get a Token from
             eBay via Your Application" の Your eBay Sign-in Settings に表示。
             例: gucch-xxxx-PRD-xxxxxxxxx-yyyyyyyy)。実 https URL ではなく
            この RuName 文字列を redirect_uri として渡すのが eBay 仕様。
"""
from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote

import httpx

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.ebay_oauth_refresh import (  # noqa: E402
    _SCOPES,
    _TOKEN_ENDPOINT,
    _load_env_dict,
    _write_env_values,
)

_AUTHORIZE_ENDPOINT = "https://auth.ebay.com/oauth2/authorize"


def _creds() -> tuple[str, str]:
    env = _load_env_dict()
    app_id = (env.get("EBAY_APP_ID") or "").strip()
    cert_id = (env.get("EBAY_CERT_ID") or "").strip()
    if not app_id or not cert_id:
        print("[error] .env に EBAY_APP_ID / EBAY_CERT_ID がありません。", file=sys.stdout)
        sys.exit(2)
    return app_id, cert_id


def cmd_url(runame: str) -> int:
    app_id, _ = _creds()
    params = (
        f"client_id={quote(app_id, safe='')}"
        f"&response_type=code"
        f"&redirect_uri={quote(runame, safe='')}"
        f"&scope={quote(_SCOPES, safe='')}"
        f"&prompt=login"
    )
    url = f"{_AUTHORIZE_ENDPOINT}?{params}"
    print("以下の URL をブラウザで開き、出品用 eBay アカウントでログイン&同意してください:\n")
    print(url)
    print("\n同意後、ブラウザは RuName の設定先 URL にリダイレクトされ、URL に")
    print("  ...?code=v^1.1#i^1#... (長い文字列) が付きます。その code をコピーし:")
    print('  python scripts/ebay_oauth_consent.py exchange --runame "<RuName>" --code "<code>"')
    return 0


def cmd_exchange(runame: str, code: str) -> int:
    app_id, cert_id = _creds()
    code = unquote(code.strip())  # redirect の url-encoded code を素の値に戻す
    auth_b64 = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                _TOKEN_ENDPOINT,
                headers={
                    "Authorization": f"Basic {auth_b64}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": runame,
                },
            )
    except httpx.HTTPError as e:
        print(f"[error] token endpoint 通信失敗: {e}")
        return 2

    if resp.status_code != 200:
        try:
            body = resp.json()
            detail = body.get("error_description") or body.get("error") or str(body)[:300]
        except ValueError:
            detail = resp.text[:300]
        print(f"[error] code 交換失敗 status={resp.status_code}: {detail}")
        print("[hint] code は数分で失効。url コマンドからやり直すか、RuName 一致を確認。")
        return 2

    data = resp.json()
    access = data.get("access_token") or ""
    refresh = data.get("refresh_token") or ""
    expires_in = int(data.get("expires_in") or 0)
    refresh_exp = int(data.get("refresh_token_expires_in") or 0)
    if not access or not refresh or expires_in <= 0:
        print(f"[error] 応答に access_token/refresh_token/expires_in が不足: keys={list(data)}")
        return 2

    expires_at = int(time.time()) + expires_in
    _write_env_values({
        "EBAY_USER_TOKEN": access,
        "EBAY_REFRESH_TOKEN": refresh,
        "EBAY_USER_TOKEN_EXPIRES_AT": str(expires_at),
    })
    # トークン値は出さない (security)。長さと有効期限のみ。
    print("[ok] .env を更新しました (トークン値は非表示):")
    print(f"  EBAY_USER_TOKEN        : len={len(access)} / expires_in={expires_in}s")
    print(f"  EBAY_REFRESH_TOKEN     : len={len(refresh)} / "
          f"valid={refresh_exp // 86400 if refresh_exp else '?'}日")
    print(f"  EBAY_USER_TOKEN_EXPIRES_AT = {expires_at}")
    print("\n次: アシスタントに知らせてください。GetOrders / refresh --force で復旧確認します。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pu = sub.add_parser("url", help="同意 URL を表示")
    pu.add_argument("--runame", required=True, help="eBay Redirect URL name (RuName)")
    pe = sub.add_parser("exchange", help="code を access/refresh token に交換し .env 更新")
    pe.add_argument("--runame", required=True, help="url コマンドと同じ RuName")
    pe.add_argument("--code", required=True, help="リダイレクト URL の ?code= の値")
    args = ap.parse_args()
    if args.cmd == "url":
        return cmd_url(args.runame)
    if args.cmd == "exchange":
        return cmd_exchange(args.runame, args.code)
    return 1


if __name__ == "__main__":
    sys.exit(main())
