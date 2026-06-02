/* ============================================================================
 * Chatwork ログ全件エクスポート (ブラウザコンソール版)
 *   出典ロジック: swdyh/goodbye_chatwork (gateway.php?cmd=load_old_chat)
 *   作成: 2026-06-02 / 用途: コンサルログを全件取得し Claude に渡して学習・体系化
 *
 * 性質:
 *   - 読み取り専用。あなたのメッセージを取得してローカルに JSON ダウンロードするだけ。
 *   - 送信先は Chatwork 自身(同一オリジン)のみ。トークンは外部に出ない。
 *   - パスワード不要・インストール不要・reCAPTCHA 無関係(ログイン済セッションを使う)。
 *
 * 使い方:
 *   1. ブラウザで Chatwork にログインし、抽出したい「部屋(コンサルとのチャット)」を開く
 *      (URL が ...#!rid12345678 の状態)
 *   2. F12 → 「Console」タブ
 *   3. 本ファイルの中身を全部コピーして貼り付け → Enter
 *   4. 進捗が console に出る。完了すると chatwork_room{rid}_{N}msgs.json が DL される
 *   5. その JSON を .company/ebay-knowledge/consultant-logs/ に置いて「取り込んで」と伝える
 *
 * 複数の部屋がある場合: 各部屋を開いて 2〜4 を繰り返す(部屋ごとに 1 ファイル)。
 *
 * 動かない時(kubell 移行で内部 API が変わっている可能性):
 *   - console のエラーメッセージ / 「生レスポンス先頭」をそのまま貼って教えてください。
 *   - または Network タブでメッセージ読み込み時のリクエスト URL を 1 つ共有 → 私が修正します。
 * ========================================================================== */
(async () => {
  const origin = location.origin;

  // --- token / myid をページから取得 ---
  let token = (typeof ACCESS_TOKEN !== 'undefined' && ACCESS_TOKEN) || window.ACCESS_TOKEN;
  let myid  = (typeof MYID !== 'undefined' && MYID) || window.MYID;
  if (!token) { const m = document.documentElement.innerHTML.match(/ACCESS_TOKEN\s*=\s*'([^']+)'/); token = m && m[1]; }
  if (!myid)  { const m = document.documentElement.innerHTML.match(/MYID\s*=\s*'([^']+)'/);          myid  = m && m[1]; }

  // --- room_id を URL hash から取得(無ければ手入力) ---
  let rid = (location.hash.match(/rid(\d+)/) || [])[1];
  if (!rid) rid = prompt('room_id を入力してください (URL の #!rid の後の数字)');

  if (!token || !myid || !rid) {
    console.error('[chatwork-export] 取得失敗', { tokenFound: !!token, myid, rid },
      '→ Chatwork にログインし対象の部屋を開いた状態で実行してください。');
    return;
  }
  console.log(`[chatwork-export] 開始 room_id=${rid} (myid=${myid})`);

  const CHAT_SIZE = 40;          // goodbye_chatwork と同じ batch サイズ
  let fid = 0, all = [], batch = 0;

  while (true) {
    const url = `${origin}/gateway.php?cmd=load_old_chat&myid=${myid}&_v=1.80a&_av=5`
      + `&_t=${token}&ln=ja&room_id=${rid}&last_chat_id=0&first_chat_id=${fid}`
      + `&jump_to_chat_id=0&unread_num=0&file=1&desc=1`;

    let res, text;
    try {
      res = await fetch(url, { credentials: 'include' });
      text = await res.text();
    } catch (e) {
      console.error('[chatwork-export] fetch 失敗:', e); break;
    }

    let data;
    try { data = JSON.parse(text); }
    catch (e) {
      console.error('[chatwork-export] JSON parse 失敗。生レスポンス先頭500字 ↓\n' + text.slice(0, 500));
      break;
    }

    // メッセージ配列を防御的に探索(内部 API のラップ構造差異に対応)
    const list =
      (data.result && (data.result.chat_list || data.result.chatList || data.result.messages)) ||
      data.chat_list ||
      (Array.isArray(data.result) ? data.result : null) ||
      (Array.isArray(data) ? data : null);

    if (!list) {
      console.error('[chatwork-export] メッセージ配列が見つかりません。下の構造を私に共有してください ↓', data);
      break;
    }
    if (list.length === 0) { console.log('[chatwork-export] これ以上ありません'); break; }

    all = all.concat(list);
    batch++;
    console.log(`[chatwork-export] batch ${batch}: +${list.length} (累計 ${all.length})`);

    if (list.length < CHAT_SIZE) break;           // 先頭(最古)に到達
    const lastId = list[list.length - 1].id;
    if (lastId == null || lastId === fid) break;   // 進まなくなったら停止
    fid = lastId;

    if (batch > 5000) { console.warn('[chatwork-export] batch 上限到達で停止'); break; }
    await new Promise(r => setTimeout(r, 300));     // サーバ負荷配慮(0.3s 間隔)
  }

  // 重複除去 + id 昇順(時系列)
  const seen = new Set(), uniq = [];
  for (const m of all) { const id = m.id; if (!seen.has(id)) { seen.add(id); uniq.push(m); } }
  uniq.sort((a, b) => Number(a.id) - Number(b.id));
  console.log(`[chatwork-export] 完了: 総メッセージ ${uniq.length} 件`);

  // JSON ダウンロード
  const payload = { room_id: rid, exported_at: new Date().toISOString(), count: uniq.length, messages: uniq };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `chatwork_room${rid}_${uniq.length}msgs.json`;
  a.click();
  console.log(`[chatwork-export] ${a.download} をダウンロードしました。`);
})();
