# -*- coding: utf-8 -*-
"""eBaymag 国別出品 ON/OFF ドライバ (依頼ボード#10 / 2026-06-13).

scripts/ebaymag_publish_driver_2026_06_11.py (プラン v2 全 117 件反映で実証済) の
ライブラリ化。商品管理タブから 1 商品ずつ状態取得/反映する。

設計方針 (terapeak_scraper.py と同型):
  - user が事前に Chrome を --remote-debugging-port=9222 で起動 + eBaymag ログイン済
    + ebaymag.com タブを開いておく
  - 本モジュールは CDP attach して操作。CDP 不在 / タブ不在は明確なエラーで返す (Q0)
  - 安全弁 (実証済 3 種): (1) panel itm リンク照合 = 誤商品 mutation 防止の権威
    (2) 「N 変動を保存」の N が期待数一致必須 (3) リロード後 PSfVs class で定着検証
  - アーカイブ中の保存はサーバ側が黙って巻き戻す → 必要なら先に「戻す」

戻り値は EbaymagResult (ok / error / site_states / log)。例外は内部で吸収して
error に格納 (UI 層が st.error 表示)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger(__name__)

CDP_ENDPOINT = "http://localhost:9222"

# site code (UI / DB キー) ↔ eBaymag 表示ドメイン
SITE_MAP = {
    "UK": "ebay.co.uk", "DE": "ebay.de", "FR": "ebay.fr", "IT": "ebay.it",
    "ES": "ebay.es", "CA": "ebay.ca", "AU": "ebay.com.au",
}
DOMAIN_TO_CODE = {v: k for k, v in SITE_MAP.items()}
# US (ebay.com) は本体 listing そのもの = eBaymag からトグルしない (表示参考のみ)

# --- JS snippets (2026-06-10/11 実証済パターン、script から逐語移植) ---------

# productId= を含むリンクを収集 (discover_product_id 用)
DISCOVER_JS = r"""() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href*="productId="]')) {
    const m = a.href.match(/productId=(\d+)/);
    if (m) out.push({productId: m[1],
                     text: (a.closest('tr, li, [class*="row"], [class*="item"]') || a)
                           .innerText.slice(0, 160)});
  }
  const body = document.body.innerText;
  return {links: out.slice(0, 20),
          empty: /商品が見つかりません|見つかりませんでした|0\s*アイテム/.test(body),
          head: body.slice(0, 300)};
}"""

# フィルタ結果の行 (query を含む text node) をクリックして panel を開く
# URL に productId が付与されるので後段で回収する
OPEN_ROW_JS = r"""(args) => {
  const [query, skip] = args;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let tnode = null, seen = 0;
  while (walker.nextNode()) {
    const t = walker.currentNode.textContent;
    if (t && t.includes(query) && t.length < 200) {
      if (seen === skip) { tnode = walker.currentNode; break; }
      seen++;
    }
  }
  if (!tnode) return 'TITLE_NOT_FOUND(matches=' + seen + ')';
  let el = tnode.parentElement;
  for (let i = 0; i < 6 && el; i++) {
    if (el.tagName === 'A' || el.onclick || el.getAttribute('role') === 'button'
        || getComputedStyle(el).cursor === 'pointer') {
      el.click();
      return 'CLICKED';
    }
    el = el.parentElement;
  }
  tnode.parentElement.click();
  return 'CLICKED_FALLBACK';
}"""

PANEL_TITLE_JS = r"""() => {
  const body = document.body.innerText;
  const act = Array.from(document.querySelectorAll('button'))
    .find(b => b.innerText.trim() === 'アクション');
  let title = '';
  if (act) {
    let n = act.parentElement;
    for (let i = 0; i < 6 && n; i++) {
      if (n.innerText.length > 80) { title = n.innerText.split('\n')[0].trim(); break; }
      n = n.parentElement;
    }
  }
  let itm = null;
  for (const a of document.querySelectorAll('a[href*="ebay."]')) {
    const m = a.href.match(/itm\/.*?(\d{12})/);
    if (m) { itm = m[1]; break; }
  }
  return {url: location.href, title, itm, hasAction: !!act, head: body.slice(0, 300)};
}"""

UNARCHIVE_JS = r"""() => {
  const els = Array.from(document.querySelectorAll('li, a, button, [role="menuitem"]'));
  let restore = els.find(el => el.innerText && el.innerText.trim() === '戻す');
  if (!restore) {
    const act = Array.from(document.querySelectorAll('button'))
      .find(b => b.innerText.trim() === 'アクション');
    if (!act) return 'NO_ACTION_BUTTON';
    act.click();
    return 'MENU_OPENED';
  }
  restore.click();
  return 'RESTORE_CLICKED';
}"""

# 行同定 = 「site 名 1 種類のみ + ボタン 2 個以上」 (ebay.com.au を ebay.com と
# 誤マッチさせない distinct mention set 判定、regex 交替順バグの再発防止)
TOGGLE_ON_JS = r"""(siteName) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let tnode = null;
  while (walker.nextNode()) {
    if (walker.currentNode.textContent.trim() === siteName) { tnode = walker.currentNode; break; }
  }
  if (!tnode) return 'SITE_NOT_FOUND';
  let node = tnode.parentElement;
  for (let i = 0; i < 10 && node; i++) {
    const mentions = new Set(node.innerText.match(/ebay\.[a-z][a-z.]*[a-z]/g) || []);
    if (mentions.size > 1) return 'ROW_NOT_ISOLATED';
    const btns = Array.from(node.querySelectorAll('button'))
      .filter(b => /掲載され|リストされ|完売/.test(b.innerText));
    if (btns.length >= 2) {
      const offBtn = btns.find(b => /掲載されていません/.test(b.innerText.trim()));
      const onBtn = btns.find(b => b !== offBtn);
      if (!onBtn) return 'ON_BUTTON_NOT_FOUND';
      if (onBtn.className.includes('PSfVs')) return 'ALREADY_ON:' + onBtn.innerText.trim();
      onBtn.click();
      return 'CLICKED';
    }
    node = node.parentElement;
  }
  return 'ROW_NOT_FOUND';
}"""

TOGGLE_OFF_JS = r"""(siteName) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let tnode = null;
  while (walker.nextNode()) {
    if (walker.currentNode.textContent.trim() === siteName) { tnode = walker.currentNode; break; }
  }
  if (!tnode) return 'SITE_NOT_FOUND';
  let node = tnode.parentElement;
  for (let i = 0; i < 10 && node; i++) {
    const mentions = new Set(node.innerText.match(/ebay\.[a-z][a-z.]*[a-z]/g) || []);
    if (mentions.size > 1) return 'ROW_NOT_ISOLATED';
    const btns = Array.from(node.querySelectorAll('button'))
      .filter(b => /掲載され|リストされ|完売/.test(b.innerText));
    if (btns.length >= 2) {
      const offBtn = btns.find(b => /掲載されていません/.test(b.innerText.trim()));
      if (!offBtn) return 'OFF_BUTTON_NOT_FOUND';
      if (offBtn.className.includes('PSfVs')) return 'ALREADY_OFF';
      offBtn.click();
      return 'CLICKED';
    }
    node = node.parentElement;
  }
  return 'ROW_NOT_FOUND';
}"""

SAVE_JS = r"""(expected) => {
  const btn = Array.from(document.querySelectorAll('button'))
    .find(b => /変動\s*を保存/.test(b.innerText));
  if (!btn) return 'SAVE_BUTTON_NOT_FOUND';
  const label = btn.innerText.trim();
  const m = label.match(/^(\d+)\s/);
  if (!m) return 'ABORT: ラベル解析不能 ' + label;
  if (parseInt(m[1], 10) !== expected) return 'ABORT: 変動数=' + m[1] + ' 期待=' + expected;
  btn.click();
  return 'SAVED:' + label;
}"""

VERIFY_JS = r"""() => {
  const out = {sites: [], errors: null};
  const body = document.body.innerText;
  if (/エラー|失敗/.test(body.slice(0, 3000))) out.errors = body.slice(0, 300);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const names = [];
  while (walker.nextNode()) {
    const t = walker.currentNode.textContent.trim();
    if (/^ebay\.[a-z][a-z.]*[a-z]$/.test(t)) names.push(walker.currentNode);
  }
  for (const tn of names) {
    const site = tn.textContent.trim();
    let node = tn.parentElement;
    for (let i = 0; i < 8 && node; i++) {
      const mentions = new Set(node.innerText.match(/ebay\.[a-z][a-z.]*[a-z]/g) || []);
      if (mentions.size > 1) break;
      const btns = Array.from(node.querySelectorAll('button'))
        .filter(b => /掲載され|リストされ|完売/.test(b.innerText));
      if (btns.length >= 2) {
        const on = btns.find(b => b.className.includes('PSfVs'));
        out.sites.push({site, on: on ? on.innerText.trim() : null});
        break;
      }
      node = node.parentElement;
    }
  }
  return out;
}"""

# --- 送料ポリシー付替 (W284 Phase2-3, 2026-06-21) ---------------------------
# 商品モーダル (productId panel) で「別のポリシーを選択」を押し、配送ポリシー
# dropdown から target token のオプションを選択する。spike (2026-06-20) で
# 商品モーダル → 「別のポリシーを選択」→ dropdown → get_by_text(token) で
# option 特定可能を確認済み。誤付替防止は呼び出し側の itm 照合 (権威安全弁)。

# 「別のポリシーを選択」ボタンを押す (配送ポリシー編集 UI を開く)
OPEN_POLICY_PICKER_JS = r"""() => {
  const btn = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .find(el => el.innerText && el.innerText.trim().includes('別のポリシーを選択'));
  if (!btn) return 'PICKER_BUTTON_NOT_FOUND';
  btn.click();
  return 'PICKER_OPENED';
}"""

# 現在割当中の配送ポリシー名を読む (定着検証用)。
# 配送ポリシー欄の近傍テキストから token 候補を拾う。
READ_CURRENT_POLICY_JS = r"""(token) => {
  const body = document.body.innerText;
  return {hasToken: body.includes(token), head: body.slice(0, 400)};
}"""

# dropdown / リストから target token のオプションを選択する。
# money-direct (各国版送料の mutate) のため完全一致のみ採用する
# (reviewer MED-2: 部分一致フォールバックは誤付替リスク。未ヒットは中断=Q0)。
SELECT_POLICY_OPTION_JS = r"""(token) => {
  const cands = Array.from(document.querySelectorAll(
    'option, li, [role="option"], [role="menuitem"], div, span, button, a'));
  // 完全一致のみ (trim 後の innerText が token と一致するもの)。
  // 複数候補がある場合は最短 innerText を選ぶ (option 本体 > それを含む親要素)。
  const exact = cands
    .filter(e => e.innerText && e.innerText.trim() === token)
    .sort((a, b) => a.innerText.length - b.innerText.length);
  const el = exact[0];
  if (!el) return 'OPTION_NOT_FOUND';
  // <option> は click では選択されないため、select 要素経由で value を設定
  if (el.tagName === 'OPTION') {
    const sel = el.closest('select');
    if (sel) {
      sel.value = el.value;
      sel.dispatchEvent(new Event('change', {bubbles: true}));
      return 'OPTION_SELECTED(select)';
    }
  }
  el.click();
  return 'OPTION_CLICKED';
}"""

# ポリシー編集 UI の保存ボタン (国トグルの「N 変動を保存」とは別 UI)。
SAVE_POLICY_JS = r"""() => {
  const btn = Array.from(document.querySelectorAll('button'))
    .find(b => b.innerText && /保存|適用|Save|Apply/.test(b.innerText.trim())
               && !/変動/.test(b.innerText));
  if (!btn) return 'SAVE_POLICY_BUTTON_NOT_FOUND';
  btn.click();
  return 'POLICY_SAVED:' + btn.innerText.trim();
}"""


@dataclass
class EbaymagResult:
    ok: bool = False
    error: str | None = None
    site_states: dict[str, bool] = field(default_factory=dict)  # {"UK": True, ...}
    log: list[str] = field(default_factory=list)
    product_id: str | None = None  # discover_product_id が発見した productId

    def _log(self, msg: str) -> None:
        self.log.append(msg)
        logger.info("[ebaymag_driver] %s", msg)


def _label_is_on(label: str | None) -> bool:
    """ON 判定: 「リストされている」/「掲載されている」/「完売」(qty=0 で ON 定着)."""
    if not label:
        return False
    return "リスト" in label or label in ("掲載されている", "完売")


def _states_from_verify(verify: dict) -> dict[str, bool]:
    states: dict[str, bool] = {}
    for s in verify.get("sites", []):
        code = DOMAIN_TO_CODE.get(s["site"])
        if code:
            states[code] = _label_is_on(s.get("on"))
    return states


def _goto_and_wait(page, url: str, settle_s: float = 6.0) -> None:
    """予約遷移 → URL 変化 + 描画待ち (evaluate は遷移中に落ちるので retry)."""
    page.evaluate("url => { setTimeout(() => { location.href = url; }, 100); }", url)
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            cur = page.evaluate("() => location.href")
            if "productId=" in url:
                if "productId=" in cur:
                    break
            elif url.split("?")[0] in cur:
                break
        except Exception:
            continue
    time.sleep(settle_s)


def _read_panel(page, url: str) -> dict:
    """panel を開いて info を取得 (itm リンクは描画遅延あり → 最長 12s ポーリング)."""
    _goto_and_wait(page, url)
    info = page.evaluate(PANEL_TITLE_JS)
    deadline = time.time() + 12
    while info.get("itm") is None and info.get("hasAction") and time.time() < deadline:
        time.sleep(2)
        info = page.evaluate(PANEL_TITLE_JS)
    return info


def _open_panel_and_check_itm(
    page, url: str, expected_itm: str, res: EbaymagResult
) -> dict | None:
    """panel を開いて itm 照合 (権威安全弁)。OK なら panel info、NG なら None."""
    info = _read_panel(page, url)
    # ログイン切れ検知: login ページへリダイレクトされた場合は itm 照合前に中断
    panel_url = info.get("url") or ""
    # NOTE: login パス (ebaymag.com/login) 前提。eBaymag が /sign_in 等へ変更すると従来の fallback に縮退
    if "ebaymag.com/login" in panel_url:
        res._log(f"login redirect detected: {panel_url[:80]}")
        res.error = "eBaymag セッション切れ — CDP Chrome (9222) で再ログインしてください"
        return None
    res._log(f"panel: itm={info.get('itm')} title={ (info.get('title') or '')[:60] }")
    if not info.get("hasAction"):
        return None
    if info.get("itm") != expected_itm:
        res.error = (
            f"itm 照合 NG: panel={info.get('itm')} 期待={expected_itm} (誤商品防止で中断)"
        )
        return None
    return info


# --- Streamlit (Windows) 配下は subprocess 隔離 -------------------------------
# Tornado が SelectorEventLoop を強制するため Playwright の Node 起動
# (asyncio.create_subprocess_exec) が NotImplementedError (str 空) で必ず失敗する。
# supplier_scraper._should_isolate_playwright と同型の子プロセス隔離で回避
# (2026-06-13 依頼ボード#10 Q1 実機 verify で発覚)。


def _should_isolate() -> bool:
    """Streamlit (Windows) 配下なら True (env EBAYMAG_DRIVER_SUBPROCESS で強制可)."""
    override = os.environ.get("EBAYMAG_DRIVER_SUBPROCESS", "").strip()
    if override == "1":
        return True
    if override == "0":
        return False
    if sys.platform != "win32":
        return False
    try:
        import streamlit.runtime.scriptrunner as _sr
        return _sr.get_script_run_ctx() is not None
    except Exception:  # noqa: BLE001
        return False


def _run_isolated(func_name: str, kwargs: dict, timeout_sec: int) -> EbaymagResult:
    """子プロセスで driver 関数を実行し JSON で結果回収 (event loop 衝突回避)."""
    res = EbaymagResult()
    script = (
        "import json, sys; from monitor import ebaymag_driver as d; "
        "r = getattr(d, sys.argv[1])(**json.loads(sys.argv[2])); "
        "print(json.dumps({'ok': r.ok, 'error': r.error, "
        "'site_states': r.site_states, 'log': r.log, "
        "'product_id': r.product_id}, ensure_ascii=True))"
    )
    env = dict(os.environ)
    env["EBAYMAG_DRIVER_SUBPROCESS"] = "0"  # 再帰防止 (子は in-process 実行)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    project_root = str(Path(__file__).resolve().parent.parent)
    # apply 経路で子が死んだ場合、eBaymag 側は適用済みの可能性がある
    # (save 後 verify 前) — 状態不明性を error 文言に明示 (reviewer M1)
    apply_hint = ("。反映済みの可能性あり — 「状態取得」で再確認してください"
                  if func_name in ("apply_site_changes", "assign_policy") else "")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script, func_name, json.dumps(kwargs)],
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout_sec, env=env, cwd=project_root,
        )
    except subprocess.TimeoutExpired:
        res.error = f"eBaymag 操作 timeout ({timeout_sec}s, subprocess){apply_hint}"
        return res
    except Exception as e:  # noqa: BLE001
        res.error = f"subprocess 起動失敗: {str(e) or type(e).__name__}"
        return res
    if proc.returncode != 0:
        res.error = (f"subprocess 異常終了 rc={proc.returncode}: "
                     f"{(proc.stderr or '').strip()[-400:]}{apply_hint}")
        return res
    try:
        # node の deprecation warning 等が混ざっても最終行が JSON
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        # 先頭は warning 行で埋まり得るため tail を出す (reviewer M3)
        res.error = f"subprocess 出力 parse 失敗: {proc.stdout[-300:]!r}{apply_hint}"
        return res
    res.ok = bool(out.get("ok"))
    res.error = out.get("error")
    res.site_states = out.get("site_states") or {}
    res.log = out.get("log") or []
    res.product_id = out.get("product_id")
    for line in res.log:  # 子の log は親 logger に流れないため relay (reviewer LOW)
        logger.info("[ebaymag_driver/sub] %s", line)
    return res


def fetch_site_states(product_id: str, expected_itm: str) -> EbaymagResult:
    """read-only: アクティブ panel を開いて国別 ON/OFF 状態を取得.

    URL は productId 単独で panel が開く (ebaymag_audit_applied_2026_06_11.py 実証済)。
    """
    res = EbaymagResult()
    if not PLAYWRIGHT_AVAILABLE:
        res.error = "playwright 未インストール"
        return res
    if _should_isolate():
        return _run_isolated(
            "fetch_site_states",
            {"product_id": product_id, "expected_itm": expected_itm},
            timeout_sec=180)
    active_url = f"https://ebaymag.com/stock?productId={product_id}"
    try:
        with sync_playwright() as p:
            page = _get_ebaymag_page(p, res)
            if page is None:
                return res
            info = _open_panel_and_check_itm(page, active_url, expected_itm, res)
            if info is None:
                if res.error is None:
                    res.error = (
                        "アクティブ一覧に panel が開けません "
                        "(アーカイブ中 or productId 不一致の可能性)"
                    )
                return res
            verify = page.evaluate(VERIFY_JS)
            res.site_states = _states_from_verify(verify)
            if not res.site_states:
                # eBaymag UI 構造変化等で site 行が 1 つも拾えなかった場合に
                # ok=True/{} を返すと UI 側が正常キャッシュを {} で上書きするため
                # error 扱いにする (reviewer MEDIUM-1)
                res.error = (
                    "国別状態を 1 件も検出できず (eBaymag UI 変化の可能性) — "
                    "キャッシュ未更新"
                )
                return res
            res._log(f"states: {res.site_states}")
            res.ok = True
    except Exception as e:
        res.error = f"eBaymag 状態取得失敗: {str(e) or type(e).__name__}"
        logger.warning("fetch_site_states failed", exc_info=True)
    return res


def apply_site_changes(
    product_id: str,
    expected_itm: str,
    turn_on: list[str],
    turn_off: list[str],
) -> EbaymagResult:
    """国トグル変更を反映 (ON/OFF 混在可)。実証済 5 step + 安全弁 3 種.

    turn_on / turn_off は site code (UK/DE/...) のリスト。
    """
    res = EbaymagResult()
    if not PLAYWRIGHT_AVAILABLE:
        res.error = "playwright 未インストール"
        return res
    bad = [s for s in turn_on + turn_off if s not in SITE_MAP]
    if bad:
        res.error = f"未知の site code: {bad}"
        return res
    if not turn_on and not turn_off:
        res.error = "変更対象なし"
        return res
    if _should_isolate():
        return _run_isolated(
            "apply_site_changes",
            {"product_id": product_id, "expected_itm": expected_itm,
             "turn_on": turn_on, "turn_off": turn_off},
            timeout_sec=420)

    arch_url = f"https://ebaymag.com/stock?archived=true&productId={product_id}"
    active_url = f"https://ebaymag.com/stock?productId={product_id}"

    try:
        from monitor.cdp_lock import acquire as _cdp_lock_acquire
        # B: subprocess timeout=420s → lock timeout=300s (subprocess_timeout より短く)
        with _cdp_lock_acquire(blocking=True, timeout=300), sync_playwright() as p:
            page = _get_ebaymag_page(p, res)
            if page is None:
                return res

            # Step 1: ON する場合のみ、アーカイブ中なら先に「戻す」
            # (アーカイブ中の保存はサーバ側が黙って巻き戻すため)
            if turn_on:
                # itm ポーリング付きで読む (1 回評価だと描画遅延で偽 abort、
                # reviewer MEDIUM-2)
                info = _read_panel(page, arch_url)
                if info.get("hasAction"):
                    if info.get("itm") != expected_itm:
                        res.error = (
                            f"itm 照合 NG (アーカイブ panel): panel={info.get('itm')} "
                            f"期待={expected_itm}"
                        )
                        return res
                    r = page.evaluate(UNARCHIVE_JS)
                    res._log(f"unarchive: {r}")
                    if r == "MENU_OPENED":
                        page.wait_for_timeout(1500)
                        r = page.evaluate(UNARCHIVE_JS)
                        res._log(f"unarchive(2): {r}")
                    if r == "RESTORE_CLICKED":
                        page.wait_for_timeout(5000)
                else:
                    res._log("アーカイブ一覧に panel なし → 既にアクティブ")

            # Step 2: アクティブ panel + itm 照合 (権威)
            info = _open_panel_and_check_itm(page, active_url, expected_itm, res)
            if info is None:
                if res.error is None:
                    res.error = "アクティブ panel が開けません (mutation せず中断)"
                return res

            # Step 3: トグル (ON → OFF の順、各 1.2s 待ち)
            clicked = 0
            for code in turn_on:
                r = page.evaluate(TOGGLE_ON_JS, SITE_MAP[code])
                res._log(f"toggle-on {code}: {r}")
                if r == "CLICKED":
                    clicked += 1
                    page.wait_for_timeout(1200)
                elif not r.startswith("ALREADY_ON"):
                    res.error = f"{code} の ON 失敗 ({r}) → 保存せず中断"
                    return res
            for code in turn_off:
                r = page.evaluate(TOGGLE_OFF_JS, SITE_MAP[code])
                res._log(f"toggle-off {code}: {r}")
                if r == "CLICKED":
                    clicked += 1
                    page.wait_for_timeout(1200)
                elif r != "ALREADY_OFF":
                    res.error = f"{code} の OFF 失敗 ({r}) → 保存せず中断"
                    return res

            if clicked == 0:
                res._log("変更なし (全て既に目標状態) → 保存不要")
                verify = page.evaluate(VERIFY_JS)
                res.site_states = _states_from_verify(verify)
                res.ok = True
                return res

            # Step 4: 保存 (変動数チェック安全弁)
            page.wait_for_timeout(1000)
            r = page.evaluate(SAVE_JS, clicked)
            res._log(f"save: {r}")
            if not str(r).startswith("SAVED"):
                res.error = f"保存失敗: {r}"
                return res
            page.wait_for_timeout(6000)

            # Step 5: リロード定着検証
            _goto_and_wait(page, active_url)
            verify = page.evaluate(VERIFY_JS)
            res.site_states = _states_from_verify(verify)
            missing_on = [c for c in turn_on if not res.site_states.get(c)]
            still_on = [c for c in turn_off if res.site_states.get(c)]
            if missing_on or still_on:
                res.error = (
                    f"定着検証 NG: ON未定着={missing_on} OFF残存={still_on}"
                )
                return res
            if verify.get("errors"):
                res.error = f"画面エラー語検出: {verify['errors'][:200]}"
                return res
            res._log(f"states: {res.site_states}")
            res.ok = True
    except Exception as e:
        res.error = f"eBaymag 反映失敗: {str(e) or type(e).__name__}"
        logger.warning("apply_site_changes failed", exc_info=True)
    return res


def _get_ebaymag_page(p, res: EbaymagResult):  # -> Page | None (playwright optional)
    """CDP attach して ebaymag.com タブを取得。不在は明確なエラー (Q0)."""
    try:
        browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
    except Exception as e:
        res.error = (
            "CDP Chrome (localhost:9222) に接続できません。"
            "Chrome を --remote-debugging-port=9222 で起動してください。"
            f" ({e})"
        )
        return None
    if not browser.contexts:
        res.error = "CDP Chrome にコンテキストがありません"
        return None
    ctx = browser.contexts[0]
    # 非 login タブを優先して返す (login stale タブが先頭にある場合の false-positive 防止 W293)
    page = next(
        (pg for pg in ctx.pages
         if "ebaymag.com" in pg.url and "ebaymag.com/login" not in pg.url),
        None,
    )
    if page is None:
        # fallback: login タブしか無ければそれを返す (GraphQL 権威化により caller が判定)
        page = next((pg for pg in ctx.pages if "ebaymag.com" in pg.url), None)
    if page is None:
        res.error = (
            "ebaymag.com のタブが見つかりません。"
            "CDP Chrome で eBaymag を開いてログインしてください。"
        )
        return None
    return page


def session_heartbeat() -> EbaymagResult:
    """W293: eBaymag セッション生死確認 (read-only、新タブ開かない、mutation しない)。

    判定方式 (W293 fix 2026-06-29):
      GraphQL 権威化: ebaymag_graphql.list_profiles (page.evaluate) で
        - 例外なし → alive (nodes 0 でも cookie 有効)
        - EbaymagGraphQLError / 例外 → dead
      login URL はシグナルとして note 記録のみ (即 dead にしない)。
      CDP 自体に接続できない / タブ不在 → cdp_absent。

    背景: CDP に login stale タブと shipping 生存タブが共存する時、
    login タブを掴んで即 dead 誤検知していた (false-positive)。
    GraphQL は cookie ベースのため タブ URL に依存しない。

    Returns:
        EbaymagResult。ok=True が alive、ok=False + outcome='dead'/'cdp_absent'/'error'。
        log に outcome が記録される。
    """
    res = EbaymagResult()
    if not PLAYWRIGHT_AVAILABLE:
        res.error = "playwright 未インストール"
        res.log.append("outcome=cdp_absent")
        return res

    try:
        with sync_playwright() as p:
            # CDP 接続 (新タブは開かない、_get_ebaymag_page 流用)
            page = _get_ebaymag_page(p, res)
            if page is None:
                # _get_ebaymag_page が error をセット済 → cdp_absent か ebaymag タブ不在
                res.log.append("outcome=cdp_absent")
                return res

            # login URL は補助シグナル: note 記録のみ、即 dead にしない (GraphQL が権威)
            current_url = page.url or ""
            if "ebaymag.com/login" in current_url:
                res.log.append("note=login_url_detected checking_via_graphql")

            # GraphQL で profile 一覧取得 (成功で alive、例外で dead)
            try:
                from monitor import ebaymag_graphql as _G
                profiles = _G.list_profiles(page, first=1)
                # list_profiles が例外なく返れば alive (nodes 0 でも cookie 有効)
                res.ok = True
                res.log.append(f"outcome=alive profiles_count={len(profiles)}")
            except Exception as gql_e:
                res.error = f"GraphQL 応答なし: {gql_e}"
                res.log.append("outcome=dead")
    except Exception as e:
        res.error = f"heartbeat 例外: {str(e) or type(e).__name__}"
        res.log.append("outcome=error")
        logger.warning("session_heartbeat failed", exc_info=True)
    return res


def _build_stock_search_url(query: str) -> str:
    """discover_product_id の検索 URL を組み立てる (URL エンコード必須、依頼ボード#40).

    & / # / スペース連続 / 非 ASCII を含むタイトルでもクエリ境界が壊れないよう
    urllib.parse.quote(safe="") で percent-encode する (paypay_search.py と同流儀)。
    """
    return f"https://ebaymag.com/stock?archived=true&name={urllib.parse.quote(query, safe='')}"


def discover_product_id(query: str, expected_itm: str) -> EbaymagResult:
    """eBaymag を query で検索し、itm が expected_itm と一致する productId を返す。

    成功時: result.ok=True, result.product_id=<str>
    失敗時: result.ok=False, result.error に候補数/検索語/expected_itm を明記 (Q0)

    安全弁: panel の itm リンク (eBay item id 12桁) が expected_itm と一致した
    productId のみ採用する (誤商品防止、apply_site_changes と同じ思想)。

    試行順序:
      1. アーカイブ一覧 (name=query) で productId= リンクを収集 → itm 照合
      2. リンクなしの場合: 行クリックで panel を開き URL から productId を回収 → itm 照合
      3. 候補なし / 不一致 / 複数不一致は ok=False + error に詳細を明記
    """
    res = EbaymagResult()
    if not PLAYWRIGHT_AVAILABLE:
        res.error = "playwright 未インストール"
        return res
    if _should_isolate():
        return _run_isolated(
            "discover_product_id",
            {"query": query, "expected_itm": expected_itm},
            timeout_sec=180,
        )

    # アーカイブ一覧で検索 (実証済: archived=true がデフォルト検索対象)
    search_url = _build_stock_search_url(query)
    try:
        with sync_playwright() as p:
            page = _get_ebaymag_page(p, res)
            if page is None:
                return res

            _goto_and_wait(page, search_url)
            disc = page.evaluate(DISCOVER_JS)
            res._log(
                f"discover: query={query!r} links={len(disc['links'])} empty={disc['empty']}"
            )

            matched_product_id: str | None = None

            if disc["links"]:
                # productId= リンクが取れた場合: 各リンクの panel を開いて itm 照合
                matched: list[str] = []
                for link in disc["links"]:
                    pid = link["productId"]
                    panel_url = f"https://ebaymag.com/stock?archived=true&productId={pid}"
                    info = _read_panel(page, panel_url)
                    res._log(
                        f"  pid={pid} itm={info.get('itm')} "
                        f"title={(info.get('title') or '')[:60]}"
                    )
                    if info.get("itm") == expected_itm:
                        matched.append(pid)

                if len(matched) == 1:
                    matched_product_id = matched[0]
                elif len(matched) > 1:
                    res.error = (
                        f"itm 一致候補が複数 ({len(matched)}件) あり特定不能 — "
                        f"検索語={query!r} expected_itm={expected_itm} "
                        f"candidates={matched}"
                    )
                    return res
                else:
                    res.error = (
                        f"productId リンク {len(disc['links'])}件 中に "
                        f"expected_itm={expected_itm} と一致するものなし — "
                        f"検索語={query!r}"
                    )
                    return res

            elif disc["empty"]:
                res.error = (
                    f"eBaymag で商品が見つかりません (候補0件) — "
                    f"検索語={query!r} expected_itm={expected_itm}"
                )
                return res

            else:
                # リンクなし・空でもない = 行表示のみ → 行クリックで panel を開く
                r = page.evaluate(OPEN_ROW_JS, [query, 0])
                res._log(f"row click: {r}")
                if not r.startswith("CLICKED"):
                    res.error = (
                        f"行クリックで panel を開けませんでした ({r}) — "
                        f"検索語={query!r} expected_itm={expected_itm}"
                    )
                    return res

                time.sleep(4)
                cur = page.evaluate("() => location.href")
                m = re.search(r"productId=(\d+)", cur)
                if not m:
                    res.error = (
                        f"行クリック後も URL に productId が現れませんでした "
                        f"(url={cur[:120]!r}) — "
                        f"検索語={query!r} expected_itm={expected_itm}"
                    )
                    return res

                pid = m.group(1)
                # itm リンクは描画遅延あり → 最長 12s ポーリング (_read_panel 流用)
                info = page.evaluate(PANEL_TITLE_JS)
                deadline = time.time() + 12
                while info.get("itm") is None and info.get("hasAction") and time.time() < deadline:
                    time.sleep(2)
                    info = page.evaluate(PANEL_TITLE_JS)

                res._log(
                    f"panel (row click): pid={pid} itm={info.get('itm')} "
                    f"title={(info.get('title') or '')[:60]}"
                )
                if info.get("itm") != expected_itm:
                    res.error = (
                        f"itm 照合 NG: panel={info.get('itm')} 期待={expected_itm} "
                        f"(誤商品防止で採用せず) — 検索語={query!r}"
                    )
                    return res

                matched_product_id = pid

            res.product_id = matched_product_id
            res._log(f"discover OK: productId={matched_product_id}")
            res.ok = True

    except Exception as e:
        res.error = (
            f"eBaymag productId 発見失敗: {str(e) or type(e).__name__} — "
            f"検索語={query!r} expected_itm={expected_itm}"
        )
        logger.warning("discover_product_id failed", exc_info=True)
    return res


def assign_policy(
    product_id: str,
    expected_itm: str,
    target_policy_token: str,
) -> EbaymagResult:
    """商品の配送ポリシーを target_policy_token に付替する (W284 Phase2-3).

    フロー (spike 2026-06-20 で UI 操作可を確認):
      1. アクティブ panel を開いて itm 照合 (権威安全弁 = 誤商品 mutation 防止)
      2. 「別のポリシーを選択」ボタンを押して配送ポリシー編集 UI を開く
      3. dropdown / リストから target_policy_token のオプションを選択
      4. 保存
      5. リロードして panel 本文に target_policy_token が現れるか定着検証

    money-direct タスク (各国版送料を変える)。呼び出し側は feature flag OFF が
    既定 = canary まで本関数を実行しない。本関数自体は flag を見ず、呼ばれたら
    実行する (flag ゲートは消化タスク側の責務)。

    Args:
        product_id: eBaymag の productId。
        expected_itm: 期待する eBay item id (12桁)。panel の itm と照合する。
        target_policy_token: 付替先の配送ポリシー token (UI 表示名と一致させる)。

    Returns:
        EbaymagResult。ok=True で付替+定着検証成功。失敗時は error に詳細 (Q0)。
    """
    res = EbaymagResult()
    if not PLAYWRIGHT_AVAILABLE:
        res.error = "playwright 未インストール"
        return res
    if not target_policy_token or not str(target_policy_token).strip():
        res.error = "target_policy_token が空 (付替先未指定 — Q0 で中断)"
        return res
    if _should_isolate():
        return _run_isolated(
            "assign_policy",
            {"product_id": product_id, "expected_itm": expected_itm,
             "target_policy_token": target_policy_token},
            timeout_sec=300,
        )

    active_url = f"https://ebaymag.com/stock?productId={product_id}"
    try:
        from monitor.cdp_lock import acquire as _cdp_lock_acquire
        # B: subprocess timeout=300s → lock timeout=200s (subprocess_timeout より短く)
        with _cdp_lock_acquire(blocking=True, timeout=200), sync_playwright() as p:
            page = _get_ebaymag_page(p, res)
            if page is None:
                return res

            # Step 1: panel + itm 照合 (権威)
            info = _open_panel_and_check_itm(page, active_url, expected_itm, res)
            if info is None:
                if res.error is None:
                    res.error = "アクティブ panel が開けません (付替せず中断)"
                return res

            # Step 2: 「別のポリシーを選択」を開く
            r = page.evaluate(OPEN_POLICY_PICKER_JS)
            res._log(f"open policy picker: {r}")
            if r != "PICKER_OPENED":
                res.error = f"ポリシー選択 UI を開けません ({r}) — 付替せず中断"
                return res
            page.wait_for_timeout(1500)

            # Step 3: target token のオプションを選択
            r = page.evaluate(SELECT_POLICY_OPTION_JS, target_policy_token)
            res._log(f"select policy option: {r}")
            if r == "OPTION_NOT_FOUND":
                res.error = (
                    f"配送ポリシー一覧に token={target_policy_token!r} が見つかりません "
                    "(ポリシー未作成 or 表示名不一致 — 付替せず中断)"
                )
                return res
            if not (str(r).startswith("OPTION_CLICKED")
                    or str(r).startswith("OPTION_SELECTED")):
                res.error = f"ポリシー選択に失敗 ({r}) — 付替せず中断"
                return res
            page.wait_for_timeout(1200)

            # Step 4: 保存
            r = page.evaluate(SAVE_POLICY_JS)
            res._log(f"save policy: {r}")
            if not str(r).startswith("POLICY_SAVED"):
                res.error = f"ポリシー保存に失敗 ({r})"
                return res
            page.wait_for_timeout(5000)

            # Step 5: リロード定着検証 (panel 本文に token が現れるか)
            _goto_and_wait(page, active_url)
            check = page.evaluate(READ_CURRENT_POLICY_JS, target_policy_token)
            if not check.get("hasToken"):
                res.error = (
                    f"定着検証 NG: リロード後の panel に token={target_policy_token!r} "
                    f"が現れません (付替が反映されていない可能性)。head={check.get('head','')[:120]}"
                )
                return res
            res._log(f"policy assigned OK: token={target_policy_token}")
            res.ok = True
    except Exception as e:
        res.error = f"eBaymag ポリシー付替失敗: {str(e) or type(e).__name__}"
        logger.warning("assign_policy failed", exc_info=True)
    return res
