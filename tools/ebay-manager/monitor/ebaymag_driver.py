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
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
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

# W317: eBaymag listing の publicationUrl から eBay item id (9桁以上) を抽出。
# /itm/ アンカー + slug セグメント 1 個許容 (/itm/<id> と /itm/<slug>/<id> の両形式)。
# 素の /(\d{9,})/ は slug 内の紛れ数字 (型番等) を誤抽出するクラスがあるため
# アンカー化 (Phase0 の 912/912 は抽出率であって値の正しさは未検証)。
ITEM_ID_RE = re.compile(r"/itm/(?:[^/]*/)?(\d{9,})")

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

# --- 送料ポリシー付替 (W284 Phase2-3, 2026-06-21 / picker 修正 2026-07-05) ---
# 商品モーダル (productId panel) で「別のポリシーを選択」を押し、typeahead 入力欄に
# target token を入力して候補を絞り込んでから選択する。
#
# live probe (2026-07-05) で確定した真因:
#   1. 「別のポリシーを選択」押下後、typeahead 入力欄 (placeholder に
#      「配送ポリシーを選択」を含む) を native click して初めて候補が描画される。
#      入力欄に触れず即座に候補を探すと常に 0 件 (OPTION_NOT_FOUND) だった。
#   2. 候補行の innerText は「token + 説明文」の複合のため、
#      innerText.trim()===token の完全一致は候補描画後も成立しない (contains 判定が必要)。
# → native locator (browser-ui-native-input 規約 第一選択) で
#   入力欄 click→fill、候補行は「token を含む」判定 + 可視候補ちょうど 1 件の
#   assert (誤選択防止の安全弁) を経て選択する (_select_policy_option 参照)。

# 「別のポリシーを選択」ボタン押下は native locator + wait で行う (assign_policy
# Step 2 参照)。one-shot JS query は render race で NOT_FOUND になるため廃止
# (canary#4 実測 2026-07-05)。

# typeahead 入力欄 (native locator で click→fill する、CSS selector のみ定数化)
POLICY_PICKER_INPUT_SELECTOR = 'input[placeholder*="配送ポリシー"]'

# 候補行 — canary#3 実測 (2026-07-05) で候補行は BUTTON > DIV.label 構造
# (role 属性なし・li 非使用) と確定したため button 単独。has_text=token
# (contains 判定) で絞り込み、可視のもののみ採用する。万一の button 入れ子は
# AMBIGUOUS 弁 (可視 1 件 assert) が引き続き守る。
POLICY_OPTION_CANDIDATE_SELECTOR = 'button'

# 現在割当中の配送ポリシー名を読む (定着検証用)。
# substring 判定は token を部分文字列に含む別ポリシー (MAG_..._v2 等) でも
# true になる弱点があるため、whitespace 区切りの exact word 判定にする (H2)。
READ_CURRENT_POLICY_JS = r"""(token) => {
  const body = document.body.innerText;
  return {hasToken: body.split(/\s+/).includes(token), head: body.slice(0, 400)};
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


def _match_policy_option_indices(visible_texts: list[str], token: str) -> list[int]:
    """token が **語単位で完全一致** する候補の index 一覧を返す純関数 (unit-testable)。

    候補行の innerText は「アイコン文字 + token + 説明文」の複合
    (canary#5 実測 2026-07-05: 'M\\nMAG_1-2kg_1day\\n無料, ...') のため、
    行頭一致 (startswith) では判定できない。whitespace split した語の中に
    token と完全一致する語があるかで判定する:
      - 行頭のアイコン文字/前置行に頑健 (位置を仮定しない)
      - `MAG_6-8kg_1day_v2` のような接尾辞つき別ポリシーは別語なので不一致
        (prefix 衝突封鎖を維持、Codex 指摘 2026-07-05)
    """
    return [i for i, t in enumerate(visible_texts) if token in t.split()]


def _decide_policy_option_selection(visible_texts: list[str], token: str) -> str:
    """可視候補のうち token に語単位一致する行数から選択可否を判定する純関数。

    誤選択防止のため 1 件確定時のみ選択可 (0 件 / 複数件は呼び出し側で中断)。
    クリック対象は _match_policy_option_indices の唯一 index の handle
    (visible_handles[0] 固定は一致行が先頭でない時に誤ポリシーを掴む — H1)。

    Returns:
        "OPTION_NOT_FOUND" (語単位一致 0件) / "AMBIGUOUS:N" (N>=2件) /
        "UNIQUE" (ちょうど1件)
    """
    matches = _match_policy_option_indices(visible_texts, token)
    if not matches:
        return "OPTION_NOT_FOUND"
    if len(matches) > 1:
        return f"AMBIGUOUS:{len(matches)}"
    return "UNIQUE"


@dataclass
class EbaymagResult:
    ok: bool = False
    error: str | None = None
    site_states: dict[str, bool] = field(default_factory=dict)  # {"UK": True, ...}
    log: list[str] = field(default_factory=list)
    product_id: str | None = None  # discover_product_id が発見した productId
    product_map: dict[str, str] = field(default_factory=dict)  # W317: eBay item_id → product_id
    mag_titles: set[str] = field(default_factory=set)  # 本番稼働中の MAG_ ポリシー title 集合

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
        "'product_id': r.product_id, 'product_map': r.product_map, "
        "'mag_titles': sorted(r.mag_titles)}, "
        "ensure_ascii=True))"
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
    res.product_map = out.get("product_map") or {}
    res.mag_titles = set(out.get("mag_titles") or [])
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


def _item_id_from_url(url: str | None) -> str | None:
    """publicationUrl から eBay item id を抽出 (無ければ None)。"""
    if not url:
        return None
    m = ITEM_ID_RE.search(url)
    return m.group(1) if m else None


def _build_id_map(all_nodes: list[dict]) -> tuple[dict[str, str], int]:
    """W317: products nodes 群から eBay item_id → product_id map を構築 (純関数)。

    規約 (設計書 map 構築規約):
      - どのサイトの publicationUrl から item_id が拾えても採用 (US 限定にしない)。
        eBaymag の 1 product = 複数サイト同時出品なので site を問わず同一 product を指す。
      - item_id 衝突 (2 つ以上の product_id が同一 item_id を指す) は誤 mutation 防止で
        原則 map から除外 + logger.warning (Q0 痕跡、silent に片方採用しない)。
      - 例外: 衝突エントリ中の US (siteId="0") listing が一意の product_id を指す場合のみ
        その US product_id を tie-break で採用 (US = 本体 listing の権威)。

    Returns:
        (product_map, collisions_excluded)。collisions_excluded は US tie-break でも
        解決できず除外した item_id 件数。
    """
    collected: dict[str, list[tuple[str, str]]] = {}  # item_id -> [(product_id, site_id)]
    for node in all_nodes:
        pid = node.get("id")
        if not pid:
            continue
        for li in (node.get("listings") or []):
            iid = _item_id_from_url(li.get("publicationUrl"))
            if not iid:
                continue
            sid = str((li.get("site") or {}).get("id"))
            collected.setdefault(iid, []).append((str(pid), sid))

    product_map: dict[str, str] = {}
    collisions_excluded = 0
    for iid, entries in collected.items():
        pids = {e[0] for e in entries}
        if len(pids) == 1:
            product_map[iid] = entries[0][0]
            continue
        # 衝突: US (site 0) の listing が指す product_id が一意ならそれを tie-break 採用
        us_pids = {e[0] for e in entries if e[1] == "0"}
        if len(us_pids) == 1:
            chosen = next(iter(us_pids))
            product_map[iid] = chosen
            logger.warning(
                "[fetch_product_map] item_id 衝突を US tie-break で解決: "
                "item_id=%s product_ids=%s → US=%s",
                iid, sorted(pids), chosen,
            )
            continue
        # US で解決不能 → 誤確定回避で map から除外 (Q0 痕跡)
        collisions_excluded += 1
        logger.warning(
            "[fetch_product_map] item_id 衝突を除外 (誤確定防止, US tie-break 不成立): "
            "item_id=%s product_ids=%s",
            iid, sorted(pids),
        )
    return product_map, collisions_excluded


def fetch_product_map() -> EbaymagResult:
    """W317: GraphQL で全商品を走査し eBay item_id → product_id map を構築 (read-only)。

    discover_product_id (タイトル/検索語一致) の前段として使い、item_id 完全一致で
    product_id を即時特定する。多言語タイトルのズレによる awaiting_import 滞留を解消。

    Relay pagination (first=200, pageInfo.hasNextPage + after) で全件走査する。

    res.ok は **GraphQL 呼び出し自体の成否**。map が空 (対象 item_id が 1 件も無い) でも
    呼び出しが成功していれば ok=True が正当 — 「map に対象 item_id が無い」は呼出元が
    タイトルフォールバックへ降格する判断材料であり、GraphQL の失敗ではない。ok=False は
    CDP 不在 / タブ不在 / GraphQL 例外のみ。
    """
    res = EbaymagResult()
    if not PLAYWRIGHT_AVAILABLE:
        res.error = "playwright 未インストール"
        return res
    if _should_isolate():
        return _run_isolated("fetch_product_map", {}, timeout_sec=180)
    try:
        with sync_playwright() as p:
            page = _get_ebaymag_page(p, res)
            if page is None:
                return res
            from monitor import ebaymag_graphql as _G

            all_nodes: list[dict] = []
            after: str | None = None
            page_no = 0
            total: int | None = None
            while True:
                page_no += 1
                conn = _G.list_products(page, first=200, after=after)
                if total is None:
                    total = conn.get("totalCount")
                all_nodes.extend(conn.get("nodes") or [])
                pi = conn.get("pageInfo") or {}
                if not pi.get("hasNextPage"):
                    break  # 正常終端
                next_cursor = pi.get("endCursor")
                if not next_cursor:
                    # hasNextPage=True なのに endCursor 欠損 = pagination 截断 (map 不完全)。
                    # silent に完走扱いしない (Q0 痕跡)。
                    msg = (f"[fetch_product_map] pagination 截断: hasNextPage=True だが "
                           f"endCursor 欠損 (page={page_no}, nodes={len(all_nodes)}) — map 不完全の可能性")
                    logger.warning(msg)
                    res.log.append(msg)
                    break
                if next_cursor == after:
                    # cursor が前回と同一 = サーバ側 stuck。進行保証のため打ち切り (無限ループ防止)。
                    msg = (f"[fetch_product_map] pagination stuck: endCursor が前回と同一 "
                           f"(page={page_no}, cursor={next_cursor[:40]!r}) — 打ち切り (map 不完全の可能性)")
                    logger.warning(msg)
                    res.log.append(msg)
                    break
                after = next_cursor
                if page_no > 30:  # 無限ループ防止 (200×30=6000 件で打ち切り)
                    res._log("[fetch_product_map] 30 page guard 到達で打ち切り")
                    break

            product_map, collisions = _build_id_map(all_nodes)
            res.product_map = product_map
            if total and not product_map:
                # 商品はあるのに抽出 0 = ITEM_ID_RE / GraphQL スキーマ破損の signal
                # (内部レビュー MEDIUM、Q0 痕跡)。ok=True のままだが warning で可視化。
                logger.warning(
                    "[fetch_product_map] totalCount=%s なのに product_map が空 — "
                    "regex/スキーマ破損の可能性 (nodes=%d)", total, len(all_nodes),
                )
            res._log(
                f"product_map built: total={total} nodes={len(all_nodes)} "
                f"mapped={len(product_map)} collisions_excluded={collisions}"
            )
            res.ok = True  # GraphQL 呼び出し成功 (map が空でも ok=True は正当、docstring 参照)
    except Exception as e:
        res.error = f"eBaymag product_map 構築失敗: {str(e) or type(e).__name__}"
        logger.warning("fetch_product_map failed", exc_info=True)
    return res


def fetch_mag_policy_titles() -> EbaymagResult:
    """本番稼働中の MAG_ 送料ポリシー title 集合を GraphQL で取得する (read-only)。

    ebaymag_assign.py / ebaymag_dispatch_mirror.py が運用する title 解決と同じ
    プリミティブ (ebaymag_graphql.list_profiles) を再利用する (コピー実装禁止)。
    軸2 (送料ポリシー付替) の target_token 候補 (`MAG_{band}_{dispatch}`) が実在
    するかを apply_queue 側が事前確認するために使う (Q0: 存在しない token で
    assign_policy を叩き続けない)。

    res.ok は GraphQL 呼び出し自体の成否 (fetch_product_map と同じ規約)。
    CDP 不在 / タブ不在 / GraphQL 例外のみ ok=False。
    """
    res = EbaymagResult()
    if not PLAYWRIGHT_AVAILABLE:
        res.error = "playwright 未インストール"
        return res
    if _should_isolate():
        return _run_isolated("fetch_mag_policy_titles", {}, timeout_sec=60)
    try:
        with sync_playwright() as p:
            page = _get_ebaymag_page(p, res)
            if page is None:
                return res
            from monitor import ebaymag_graphql as _G

            profiles = _G.list_profiles(page, first=200)
            res.mag_titles = {p_["title"] for p_ in profiles if p_.get("title")}
            res._log(f"mag_titles fetched: {len(res.mag_titles)} 件")
            res.ok = True
    except Exception as e:
        res.error = f"eBaymag MAG title 一覧取得失敗: {str(e) or type(e).__name__}"
        logger.warning("fetch_mag_policy_titles failed", exc_info=True)
    return res


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

    フロー (spike 2026-06-20 で UI 操作可を確認、picker 操作は live probe 2026-07-05 で修正):
      1. アクティブ panel を開いて itm 照合 (権威安全弁 = 誤商品 mutation 防止)
      2. 「別のポリシーを選択」ボタンを押して配送ポリシー編集 UI を開く
      2.5. typeahead 入力欄を native click → fill(token) して候補を描画させる
      3. token を含む可視候補が「ちょうど 1 件」であることを確認し native locator で選択
         (0件/複数件は誤選択防止のため中断)
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

            # Step 2: 「別のポリシーを選択」を開く (native locator + wait。
            # canary#4 実測 2026-07-05: ボタンは itm より遅れて描画されることがあり、
            # one-shot JS query では render race で PICKER_BUTTON_NOT_FOUND になる)
            try:
                page.get_by_text(
                    "別のポリシーを選択", exact=False
                ).first.click(timeout=10000)
            except PlaywrightTimeoutError:
                res.error = (
                    "ポリシー選択 UI を開けません (PICKER_BUTTON_NOT_FOUND) — 付替せず中断"
                )
                return res
            res._log("open policy picker: PICKER_OPENED (native)")
            page.wait_for_timeout(1500)

            # Step 2.5: typeahead 入力欄を native click → fill して候補を描画させる
            # (probe 実証: 入力欄に触れず即座に候補を探すと常に 0 件だった)
            try:
                picker_input = page.locator(POLICY_PICKER_INPUT_SELECTOR)
                picker_input.click(timeout=5000)
                picker_input.fill(target_policy_token, timeout=5000)
            except PlaywrightTimeoutError as e:
                res.error = (
                    f"配送ポリシー入力欄が見つかりません ({e}) — 付替せず中断"
                )
                return res
            res._log(f"typeahead filled: {target_policy_token}")
            page.wait_for_timeout(1000)

            # Step 3: token に語単位一致する可視候補が「ちょうど 1 件」であることを
            # 確認してから、その一致 index の handle を選択する (誤選択防止の安全弁。
            # visible_handles[0] 固定は一致行が先頭でない時に誤ポリシーを掴む — H1)
            candidates = page.locator(POLICY_OPTION_CANDIDATE_SELECTOR).filter(
                has_text=target_policy_token
            )
            visible_handles = [h for h in candidates.all() if h.is_visible()]
            visible_texts = [h.inner_text().strip() for h in visible_handles]
            match_idx = _match_policy_option_indices(visible_texts, target_policy_token)
            decision = _decide_policy_option_selection(visible_texts, target_policy_token)
            res._log(
                f"policy candidates: decision={decision} match_idx={match_idx} "
                f"texts={visible_texts[:5]}"
            )
            if decision == "OPTION_NOT_FOUND":
                res.error = (
                    f"配送ポリシー一覧に token={target_policy_token!r} が見つかりません "
                    "(ポリシー未作成 or 表示名不一致 — 付替せず中断)"
                )
                return res
            if decision != "UNIQUE":
                res.error = (
                    f"配送ポリシー候補が複数一致 ({decision}) — token={target_policy_token!r} "
                    "誤選択防止のため中断"
                )
                return res
            visible_handles[match_idx[0]].click()
            page.wait_for_timeout(1200)

            # Step 4: 保存 (保存ボタンにも render race があるため最大 8s poll — H3)
            r = None
            save_waited = 0
            while True:
                r = page.evaluate(SAVE_POLICY_JS)
                if str(r).startswith("POLICY_SAVED"):
                    break
                if save_waited >= 8000:
                    break
                page.wait_for_timeout(500)
                save_waited += 500
            res._log(f"save policy: {r} (waited {save_waited}ms)")
            if not str(r).startswith("POLICY_SAVED"):
                res.error = f"ポリシー保存に失敗 ({r})"
                return res
            page.wait_for_timeout(5000)

            # Step 5: リロード定着検証 (panel 本文に token が exact word で現れるか。
            # ラベル描画は非同期のことがあるため最大 12s poll — H3)
            _goto_and_wait(page, active_url)
            check = {}
            readback_waited = 0
            while True:
                check = page.evaluate(READ_CURRENT_POLICY_JS, target_policy_token)
                if check.get("hasToken"):
                    break
                if readback_waited >= 12000:
                    break
                page.wait_for_timeout(1000)
                readback_waited += 1000
            res._log(
                f"read-back: hasToken={check.get('hasToken')} (waited {readback_waited}ms)"
            )
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
