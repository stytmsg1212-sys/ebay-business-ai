#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task: メール取得 - Gmail API で eBay関連メールをピックアップ
"""

import sys
import logging
import json
import base64
from pathlib import Path
from datetime import datetime, timedelta

# pythonw.exe では sys.stdout が None のため安全ガード
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)


def get_gmail_service(config):
    """Gmail API サービスを初期化（トークン保存・再利用対応）

    2026-04-24 W14 CRITICAL C-1 対応: Gmail token を OneDrive 同期外
    (%LOCALAPPDATA%\\ebay-manager\\) に保存し、refresh_token の
    クラウド漏洩リスクを排除. 旧 OneDrive 配下 token は自動移行される.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        # パスを解決（config内の相対パスはebay-manager基準）
        base_dir = Path(__file__).parent.parent
        cred_path = config.get('gmail', {}).get('credentials_path', './config/credentials.json')
        cred_file = base_dir / cred_path

        # Gmail token は OneDrive 外の secure store に保管 (CRITICAL C-1).
        # 旧 config/gmail_token.json があれば secure store へ自動移行される.
        from monitor.secure_paths import get_gmail_token_path
        token_file = get_gmail_token_path()

        SCOPES = config.get('gmail', {}).get('scopes', ['https://www.googleapis.com/auth/gmail.readonly'])

        creds = None

        # 保存済みトークンがあれば読み込む
        if token_file.exists():
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

        # トークンが無効または期限切れの場合
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("トークンをリフレッシュ中...")
                creds.refresh(Request())
            else:
                if not cred_file.exists():
                    logger.error(f"認証ファイルが見つかりません: {cred_file}")
                    return None
                logger.info("初回認証: ブラウザで認証してください")
                flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), SCOPES)
                creds = flow.run_local_server(port=0)

            # トークンを保存（次回以降はブラウザ不要）
            token_file.parent.mkdir(parents=True, exist_ok=True)
            with open(token_file, 'w') as f:
                f.write(creds.to_json())
            logger.info(f"トークンを保存しました: {token_file}")

        service = build('gmail', 'v1', credentials=creds)
        return service

    except ImportError:
        logger.warning("Google API クライアントがインストールされていません（pip install google-api-python-client google-auth-oauthlib）")
        return None
    except Exception as e:
        logger.error(f"Gmail API 初期化エラー: {e}")
        return None


def _html_to_text(html: str) -> str:
    """HTMLからテキストを抽出"""
    import re
    # script/styleタグを除去
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # brやpを改行に
    text = re.sub(r'<br\s*/?>|</p>|</div>|</tr>|</li>', '\n', text, flags=re.IGNORECASE)
    # 全タグ除去
    text = re.sub(r'<[^>]+>', '', text)
    # HTMLエンティティ
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&#39;', "'").replace('&quot;', '"')
    # 連続空白・改行を整理
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    return text.strip()


def _extract_email_body(payload: dict) -> str:
    """メールの本文をプレーンテキストで取得（HTML fallback付き）"""
    # text/plain を優先
    plain = _find_part(payload, 'text/plain')
    if plain:
        return plain

    # text/html からテキスト抽出
    html = _find_part(payload, 'text/html')
    if html:
        return _html_to_text(html)

    return ""


def _find_part(payload: dict, mime_type: str) -> str:
    """再帰的に指定MIMEタイプのパートを探す"""
    if payload.get('mimeType') == mime_type and payload.get('body', {}).get('data'):
        return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')
    for part in payload.get('parts', []):
        result = _find_part(part, mime_type)
        if result:
            return result
    return ""


_SUPPLIER_SENDER_HINTS = (
    # 仕入先 (user が商品を購入する側) の購入確認メール sender。
    # 2026-05-20 user 緊急要望: tab_purchase_confirm は本来「user 仕入購入」
    # メールから listing 在庫追加するための機能だが、旧 Gmail query が eBay
    # のみだったため仕入購入メールが DB に入らず機能が破綻していた。
    'mercari.jp',                # メルカリ (no-reply@mercari.jp 等)
    'auctions.yahoo.co.jp',      # ヤフオク
    'mail.yahoo.co.jp',          # Yahoo 系
    'rakuten.co.jp',             # 楽天 (info@order.rakuten.co.jp 等)
    'order.rakuten.co.jp',
    'amazon.co.jp',              # Amazon JP (auto-confirm@/order-update@)
    'paypay.ne.jp',              # PayPay フリマ
    'suruga-ya.jp',              # 駿河屋
    'mandarake.co.jp',           # まんだらけ
)

_SUPPLIER_PURCHASE_SUBJECT_HINTS = (
    'ご注文', '注文確認', '注文受付', 'ご購入', '購入完了', 'order',
    '発送', '出荷', 'shipped',
    '落札', 'お買い上げ', 'お買いもの',  # ヤフオク落札 / 楽天お買い上げ
    'your order', 'confirmation', 'receipt',  # Amazon 等 英語
)

# 2026-05-21 user 要望 Phase A: 関税系 sender。FedEx/UPS/DHL から user の
# 通関情報要求メールを emails テーブルにも取込 → DASHBOARD で個別件名閲覧可。
# 既存 task_customs_check は customs_requests テーブル独立管理だが、
# DASHBOARD で件名/本文を user が直接読みたい要望に応える。
_CUSTOMS_SENDER_HINTS = (
    'fedex.com',
    'ups.com',
    'dhl.com',
    'aramex.com',
)

_CUSTOMS_SUBJECT_HINTS = (
    'customs', 'clearance', '通関', '関税', 'duty', 'tariff',
    'awb', 'trk', 'tracking', '輸入', '税関', '情報提出', '情報のご提供',
)


def _categorize_email(subject: str, sender: str) -> str:
    """メールをカテゴリ分けする.

    2026-05-07: listing_notification カテゴリ追加.
    user 自身の出品時に eBay から届く通知 (subject「🏷️ ... が出品されました」/
    英語版「has been listed」) を分類し、MonoDeck DASHBOARD で除外する.

    2026-05-20: supplier_purchase カテゴリ追加. user が仕入先 (メルカリ/
    ヤフオク/楽天/Amazon/PayPay/駿河屋/まんだらけ) で購入した時の確認メールを
    分類し、tab_purchase_confirm 「入荷確認」UI で対象絞り込み可能化する.
    判定: sender が _SUPPLIER_SENDER_HINTS のいずれかを含む AND
    subject が _SUPPLIER_PURCHASE_SUBJECT_HINTS のいずれかを含む (両方必須 =
    プロモ/メルマガ等の false positive 防止)。
    """
    subject_lower = subject.lower()
    sender_lower = (sender or '').lower()

    # 2026-05-21 Phase A: customs_request 判定 (FedEx/UPS/DHL の通関情報要求)。
    # supplier_purchase より先に判定 (両者で sender 重複しないが、関税系を
    # 明確優先したいため)。sender AND subject の AND で false positive 防止。
    if any(h in sender_lower for h in _CUSTOMS_SENDER_HINTS):
        if any(
            h.lower() in subject_lower if h.isascii() else h in subject
            for h in _CUSTOMS_SUBJECT_HINTS
        ):
            return 'customs_request'
        # 配送通知 (Tracking number only) 等は other で続行

    # supplier_purchase 判定: 仕入先 sender + 購入関連 subject の AND
    if any(h in sender_lower for h in _SUPPLIER_SENDER_HINTS):
        if any(
            h.lower() in subject_lower if h.isascii() else h in subject
            for h in _SUPPLIER_PURCHASE_SUBJECT_HINTS
        ):
            return 'supplier_purchase'
        # 仕入先からの非購入メール (プロモ等) は other で続行

    # 出品通知 (user 自身の出品時): 🏷️ 絵文字 + 「が出品されました」/「has been listed」
    if (
        'が出品されました' in subject
        or 'has been listed' in subject_lower
        or '\U0001f3f7' in subject  # 🏷️ tag emoji
    ):
        return 'listing_notification'
    if 'sent a message' in subject_lower:
        return 'buyer_message'
    elif 'sold' in subject_lower or 'item sold' in subject_lower or 'が売れました' in subject:
        return 'sale'
    elif 'offer' in subject_lower:
        return 'offer'
    elif 'return' in subject_lower or 'refund' in subject_lower:
        return 'return'
    elif 'invoice' in subject_lower or 'payment' in subject_lower:
        return 'payment'
    elif 'feedback' in subject_lower:
        return 'feedback'
    else:
        return 'other'


def _translate_to_ja(text: str) -> str:
    """英語テキストを日本語に翻訳（失敗時は空文字）"""
    if not text or not text.strip():
        return ""
    try:
        from deep_translator import GoogleTranslator
        # 長すぎるテキストは先頭3000文字に制限
        trimmed = text[:3000]
        result = GoogleTranslator(source='auto', target='ja').translate(trimmed)
        return result or ""
    except Exception as e:
        logger.debug(f"翻訳エラー: {e}")
        return ""


def _save_emails_to_db(emails: list) -> int:
    """メールをDBに保存。Claude で日本語要約＋優先度判定し summary_ja/action_ja/priority_ai に書き込む。

    既存カラム body_ja (Google Translate raw) は後方互換のため残す（低品質なので非推奨）。

    W54 (2026-04-30): cursor.rowcount で実 INSERT 件数を集計し返す。
    関数全体の try/except Exception 握り潰しは Q0 silent skip 元凶のため撤去。
    DB 接続/SQL 自体の例外は上位に伝播 (scheduler が success=False 記録)。
    個別メールの translate / Claude summarizer 失敗は内部で吸収して INSERT は試行する。

    W66 (2026-04-30): 事前 SELECT で gmail_id 既存メールを skip し Claude API 課金を抑制
    (subject filter 撤去で取得件数が増え、大半が既存メールになるため).

    W66 第二弾 (2026-04-30、DB lock 衝突 hotfix): Claude API call (~10s/件) を DB 接続外で実行.
    旧実装は単一 `with get_conn()` 内で Claude call をループしていたため transaction が
    数十分継続 → 他 UPDATE (例: supplier_apply) が busy_timeout 超過で fail. 修正:
    Phase 1 で existing IDs 一括 SELECT (短時間 lock) → Phase 2 は Claude call (lock 外) →
    per-row 新規 conn で INSERT (~ms lock).

    Returns:
        実 INSERT 件数 (事前 SELECT skip と gmail_id PK 衝突 IGNORE は除外)
    """
    from monitor.database import get_conn
    try:
        from monitor.claude_summarizer import summarize_email as _claude_summarize
    except ImportError as e:
        logger.warning(f"claude_summarizer import失敗: {e}")
        _claude_summarize = None

    if not emails:
        logger.info("メールDB保存: 取得 0 件 / 処理 skip")
        return 0

    # Phase 1: 既存 gmail_id を一括 SELECT (DB lock を短時間しか保持しない).
    gmail_ids = [em['id'] for em in emails]
    placeholders = ','.join('?' * len(gmail_ids))
    with get_conn() as conn:
        existing_ids = {
            row[0] for row in conn.execute(
                f"SELECT gmail_id FROM emails WHERE gmail_id IN ({placeholders})",
                gmail_ids,
            )
        }
    new_emails = [em for em in emails if em['id'] not in existing_ids]
    skipped_existing = len(emails) - len(new_emails)

    # Phase 2: 新規メールのみ enrichment + INSERT. Claude call は DB connection 外で実行
    # して他 UPDATE/SELECT を阻害しない. INSERT は per-row 新規 conn で短時間 lock.
    inserted = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for em in new_emails:
        body = em.get('body', '')
        try:
            body_ja = _translate_to_ja(body)  # 後方互換
            ai = _claude_summarize(em['subject'], em['from'], body) if _claude_summarize else None
            summary_ja = (ai or {}).get('summary_ja') or ''
            action_ja = (ai or {}).get('action_ja') or ''
            buyer_msg_ja = (ai or {}).get('buyer_message_ja') or ''
            priority_ai = (ai or {}).get('priority') or ''
            category_ai = (ai or {}).get('category') or em.get('category', 'other')
        except Exception as enrich_err:
            logger.warning(
                f"メール enrichment 失敗 (gmail_id={em.get('id')}): {enrich_err}"
            )
            body_ja = ''
            summary_ja = action_ja = buyer_msg_ja = priority_ai = ''
            category_ai = em.get('category', 'other')
            ai = None

        # Per-row INSERT: 新規 connection を都度開閉し DB lock 保持時間を ms オーダーに抑える.
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO emails
                   (gmail_id, subject, sender, date, body_text, body_ja, category, fetched_at,
                    summary_ja, action_ja, buyer_message_ja, priority_ai, category_ai, summarized_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (em['id'], em['subject'], em['from'], em['date'],
                 body, body_ja, em.get('category', 'other'), now,
                 summary_ja, action_ja, buyer_msg_ja, priority_ai, category_ai,
                 now if ai else None)
            )
            inserted += cur.rowcount

    logger.info(
        f"メールDB保存: 取得{len(emails)}件 / 新規INSERT {inserted}件 / "
        f"既存skip {skipped_existing}件"
    )
    return inserted


def extract_ebay_emails(service):
    """Gmail から eBay関連メールを抽出（本文含む）

    W66 (2026-04-30): subject keyword filter を撤去し from 限定に変更. 設計意図
    (feedback_email_triage.md: 「重要なのはメール取得ではなく重要メールを Claude が
    分別して優先度順で知らせる」) との乖離修正. 旧 subject filter は英語キーワードのみ
    だったため eBay の日本語 subject (「が売れました」「が出品されました」等) が
    全件 silent drop = AI トリアージに到達していなかった (~80件/14日 漏れ).
    Claude API call 増加対策は _save_emails_to_db 内の事前 SELECT で実装済.

    Returns: (emails: list, inserted_count: int) のタプル.
      inserted_count は _save_emails_to_db の実 INSERT 件数.
    """
    try:
        # W66: from のみ + 14 日範囲. subject filter 撤去で全 eBay メールを Claude triage 対象にする.
        # maxResults: 200 で 14 日分を概ねカバー (cap 抜け対策は別途 ROADMAP 化).
        # 2026-05-20 user 緊急要望: 仕入先 (メルカリ/ヤフオク/楽天/Amazon/PayPay/
        # 駿河屋/まんだらけ) の購入確認メールも取込対象に追加。tab_purchase_confirm
        # 「入荷確認」UI の根本データ不足を解消 (旧 query では eBay 系のみで仕入購入
        # メールが永久に DB に入らない設計バグだった)。
        query = (
            'newer_than:14d ('
            'from:ebay.com OR from:ebay.co.jp'
            ' OR from:mercari.jp'
            ' OR from:auctions.yahoo.co.jp OR from:mail.yahoo.co.jp'
            ' OR from:rakuten.co.jp OR from:order.rakuten.co.jp'
            ' OR from:amazon.co.jp'
            ' OR from:paypay.ne.jp'
            ' OR from:suruga-ya.jp'
            ' OR from:mandarake.co.jp'
            # 2026-05-21 Phase A: 関税系 (FedEx/UPS/DHL/Aramex) 追加。
            # task_customs_check pipeline と並行取込 (Gmail API 読取は副作用なし)。
            ' OR from:fedex.com OR from:ups.com OR from:dhl.com'
            ' OR from:aramex.com'
            ')'
        )
        # maxResults を 300 に増 (eBay 200 + 仕入 ~100 想定、超過は 14 日内に再収束)
        results = service.users().messages().list(
            userId='me', q=query, maxResults=300,
        ).execute()

        messages = results.get('messages', [])
        emails = []

        for msg in messages:
            try:
                msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
                headers = msg_data['payload']['headers']

                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'N/A')
                sender = next((h['value'] for h in headers if h['name'] == 'From'), 'N/A')

                email_info = {
                    'id': msg['id'],
                    'timestamp': datetime.now().isoformat(),
                    'subject': subject,
                    'from': sender,
                    'date': next((h['value'] for h in headers if h['name'] == 'Date'), 'N/A'),
                    'body': _extract_email_body(msg_data['payload']),
                    'category': _categorize_email(subject, sender),
                }

                emails.append(email_info)
                logger.info(f"メール取得: {email_info['subject']}")

            except Exception as e:
                logger.warning(f"メール解析エラー: {e}")
                continue

        # DBに保存 + タスク自動追記
        inserted = 0
        if emails:
            inserted = _save_emails_to_db(emails)
            _sync_emails_to_tasks(emails)

        return emails, inserted

    except Exception as e:
        logger.error(f"eBay メール抽出エラー: {e}")
        return [], 0


def _sync_emails_to_tasks(emails: list) -> None:
    """要対応メールをactive.mdのタスクに自動追記"""
    try:
        import re
        base_dir = Path(__file__).parent.parent
        active_path = base_dir.parent / '.company' / 'secretary' / 'todos' / 'active.md'
        if not active_path.exists():
            return

        content = active_path.read_text(encoding='utf-8')
        today = datetime.now().strftime("%Y-%m-%d")

        # 既存のgmail IDを収集（重複防止）
        existing_gmail_ids = set(re.findall(r'gmail:\s*(\w+)', content))

        new_tasks = []
        for em in emails:
            gmail_id = em['id']
            if gmail_id in existing_gmail_ids:
                continue

            category = em.get('category', 'other')
            subject = em.get('subject', '')
            sender = em.get('from', '').split('<')[0].strip().strip('"').replace('eBay - ', '')

            # 商品名抽出
            prod_match = re.search(r'about (.+?)(?:\s*#\d|$)', subject)
            product = prod_match.group(1).strip()[:40] if prod_match else ''

            if category == 'buyer_message':
                is_reply = subject.startswith('Re:')
                task = f"{sender} への返信（{product}）" if product else f"{sender} への返信"
                priority = "高"
                new_tasks.append(f"- [ ] {task} | 優先度: {priority} | 期限: 未定 | gmail: {gmail_id}")
            elif category == 'return':
                task = f"{sender} の返品リクエスト対応（{product}）" if product else f"{sender} の返品リクエスト対応"
                new_tasks.append(f"- [ ] {task} | 優先度: 高 | 期限: 未定 | gmail: {gmail_id}")
            elif category == 'offer':
                task = f"{sender} からのオファー確認（{product}）" if product else f"{sender} からのオファー確認"
                new_tasks.append(f"- [ ] {task} | 優先度: 中 | 期限: 未定 | gmail: {gmail_id}")

        if not new_tasks:
            return

        # 「要対応（メールチェック）」セクションを更新 or 作成
        section_header = f"## 要対応（メールチェック {today}）"
        old_section_pattern = re.compile(r'^## 要対応（メールチェック .+?）$', re.MULTILINE)

        if old_section_pattern.search(content):
            # 既存セクションのヘッダーを今日の日付に更新し、タスクを追記
            old_header = old_section_pattern.search(content).group()
            content = content.replace(old_header, section_header)
            # セクション末尾（次のセクションの前）にタスクを追加
            lines = content.split('\n')
            insert_idx = None
            in_section = False
            for i, line in enumerate(lines):
                if line.strip() == section_header:
                    in_section = True
                    continue
                if in_section and line.strip().startswith('## '):
                    insert_idx = i
                    break
            if insert_idx is None:
                # セクションが最後の場合
                for i in range(len(lines) - 1, -1, -1):
                    if lines[i].strip().startswith('- [ ] ') or lines[i].strip().startswith('- [x] '):
                        insert_idx = i + 1
                        break
            if insert_idx is not None:
                for j, task in enumerate(new_tasks):
                    lines.insert(insert_idx + j, task)
                content = '\n'.join(lines)
            else:
                content += '\n' + '\n'.join(new_tasks) + '\n'
        else:
            # セクション新規作成（先頭セクションの前に挿入）
            first_section = re.search(r'^## ', content, re.MULTILINE)
            if first_section:
                pos = first_section.start()
                new_section = section_header + '\n\n' + '\n'.join(new_tasks) + '\n\n'
                content = content[:pos] + new_section + content[pos:]
            else:
                content += '\n' + section_header + '\n\n' + '\n'.join(new_tasks) + '\n'

        # アトミック書き込み: 一時ファイルに書いてから os.replace でリネーム
        # （書き込み途中でプロセスが落ちてもactive.mdが0バイトにならない）
        import os
        tmp_path = active_path.with_suffix(active_path.suffix + '.tmp')
        tmp_path.write_text(content, encoding='utf-8')
        os.replace(tmp_path, active_path)
        logger.info(f"タスク自動追記: {len(new_tasks)}件")

    except Exception as e:
        logger.warning(f"タスク追記エラー: {e}")


def run_email_pickup(config):
    """
    Gmail API で eBay関連メールを取得

    Args:
        config: 設定辞書

    Returns:
        {
            'success': bool,
            'count': int,            # Gmail から取得した件数 (後方互換)
            'inserted_count': int,   # 実 DB INSERT 件数 (W54 silent skip 観測)
            'message': str,
            'status': str,
            'emails': list,          # 後方互換 (task_company_secretary.get_email_summary が参照)
        }

        message truncation 対策: run_task が json.dumps(result)[:1000] で truncate するため,
        重要フィールド (inserted_count / message) を dict 先頭側に配置し emails は末尾に置く.
    """

    logger.info("【開始】メール取得タスク")

    try:
        # Gmail API 有効化確認
        gmail_enabled = config.get('gmail', {}).get('enabled', False)

        if not gmail_enabled:
            logger.warning("Gmail API が有効化されていません")
            return {
                'success': False,
                'count': 0,
                'inserted_count': 0,
                'message': 'Gmail API is not enabled',
                'emails': [],
            }

        # Gmail サービス初期化
        service = get_gmail_service(config)

        if service is None:
            logger.warning("Gmail API クライアント初期化失敗。Google API ライブラリがインストールされていない可能性があります")
            return {
                'success': False,
                'count': 0,
                'inserted_count': 0,
                'message': 'Gmail API initialization failed - google-auth libraries may not be installed',
                'status': 'failed',
                'emails': [],
            }

        # eBay メール抽出 + DB 保存 (W54: tuple 返却に変更)
        emails, inserted = extract_ebay_emails(service)

        # W66: 旧 W54 warning (inserted=0 + len>=1) は subject filter 撤去で常時発火 = noise 化のため削除.
        # subject filter なし環境では大半が既存メール = inserted=0 が正常運用. silent skip 検出は
        # 取得経路 (Gmail API 5xx 等) で extract_ebay_emails の outer except が捕捉する設計に変更.

        logger.info(
            f"メール取得完了: 取得 {len(emails)} 件 / DB新規INSERT {inserted} 件"
        )

        return {
            'success': True,
            'count': len(emails),
            'inserted_count': inserted,
            'message': f'取得 {len(emails)} 件 / DB INSERT {inserted} 件',
            'status': 'success',
            'emails': emails,  # 後方互換: dict 末尾配置で truncation 時に他フィールドを保護
        }

    except Exception as e:
        logger.error(f"メール取得エラー: {e}")
        return {
            'success': False,
            'count': 0,
            'inserted_count': 0,
            'error': str(e),
            'status': 'error',
            'emails': [],
        }
