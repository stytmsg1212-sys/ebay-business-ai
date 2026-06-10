"""eBaymag プランv2 出品ドライバ (1 商品ずつ: 発見→解除→国トグル→保存→検証).

確立済み手順 (2026-06-10 TE Connectivity 91512-1 で実証):
  1. アーカイブ中の保存はサーバ側が黙って巻き戻す → 先に「戻す」必須
  2. 国トグル ON = 「掲載されている」ボタン click → 「リストされている」表示
  3. 「N 変動 を保存」の N が対象国数と一致しなければ ABORT (安全弁)
  4. リロード後に PSfVs class で定着検証

usage:
  python ebaymag_publish_driver_2026_06_11.py discover "91512-1"
  python ebaymag_publish_driver_2026_06_11.py apply "91512-1" 718746739 UK
  python ebaymag_publish_driver_2026_06_11.py apply "ICD-ST25" 12345 UK,DE,FR
"""
import sys
import time

sys.path.insert(0, r"C:/Users/gucch/projects/claude/tools/ebay-manager")
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

CDP = "http://localhost:9222"

SITE_MAP = {
    "UK": "ebay.co.uk", "DE": "ebay.de", "FR": "ebay.fr", "IT": "ebay.it",
    "ES": "ebay.es", "CA": "ebay.ca", "AU": "ebay.com.au", "US": "ebay.com",
}

# --- JS snippets (確立済みパターンの再利用) ---------------------------------

DISCOVER_JS = r"""() => {
  const out = [];
  // productId= を含む全リンク + 周辺テキスト
  for (const a of document.querySelectorAll('a[href*="productId="]')) {
    const m = a.href.match(/productId=(\d+)/);
    if (m) out.push({productId: m[1],
                     text: (a.closest('tr, li, [class*="row"], [class*="item"]') || a)
                           .innerText.slice(0, 160)});
  }
  // フィルタ結果の概況
  const body = document.body.innerText;
  return {links: out.slice(0, 20),
          empty: /商品が見つかりません|見つかりませんでした|0\s*アイテム/.test(body),
          head: body.slice(0, 300)};
}"""

# フィルタ結果の行 (タイトル text) をクリックして panel を開く →
# URL に productId が付与されるのでそれを回収する。skip = 何番目のマッチか (0 始まり)
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
  // クリック可能な祖先 (row) を探してクリック
  let el = tnode.parentElement;
  for (let i = 0; i < 6 && el; i++) {
    if (el.tagName === 'A' || el.onclick || el.getAttribute('role') === 'button'
        || /cursor:\s*pointer/.test(getComputedStyle(el).cssText)
        || getComputedStyle(el).cursor === 'pointer') {
      el.click();
      return 'CLICKED';
    }
    el = el.parentElement;
  }
  // fallback: text node 直親をクリック
  tnode.parentElement.click();
  return 'CLICKED_FALLBACK';
}"""

PANEL_TITLE_JS = r"""() => {
  const body = document.body.innerText;
  // panel タイトル = アクション button を含むコンテナの先頭行
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
  // itm リンク = panel の「リストを表示」→ eBay item id (照合の権威)
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

# site 行を特定し ON ボタン (「掲載されている」) をクリック
# 行同定 = 「距離 site 名 1 種類のみ + ボタン 2 個以上」(regex 交替順バグの再発防止で
# ebay.com.au を ebay.com と誤マッチさせない: distinct mention set で判定)
TOGGLE_JS_TMPL = r"""(siteName) => {
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

# site 行の OFF ボタン (「掲載されていません」) をクリック (revoke 用)
TOGGLE_OFF_JS_TMPL = r"""(siteName) => {
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


def _get_page(p):
    browser = p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0]
    target = next((pg for pg in ctx.pages if "ebaymag.com" in pg.url), None)
    if target is None:
        print("eBaymag タブなし")
        sys.exit(1)
    return target


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


def cmd_discover(query: str, archived: bool = True, skip: int = 0) -> None:
    base = "https://ebaymag.com/stock?archived=true" if archived else "https://ebaymag.com/stock?"
    url = f"{base}&name={query}".replace("?&", "?")
    with sync_playwright() as p:
        page = _get_page(p)
        _goto_and_wait(page, url)
        res = page.evaluate(DISCOVER_JS)
        cur = page.evaluate("() => location.href")
        print("URL:", cur[:120])
        print("empty:", res["empty"])
        for ln in res["links"]:
            print(f"  productId={ln['productId']} | {ln['text'][:100]}")
        if res["links"] or res["empty"]:
            return
        # リンクなし → 行クリックで panel を開いて URL から productId 回収
        r = page.evaluate(OPEN_ROW_JS, [query, skip])
        print("row click:", r)
        if not r.startswith("CLICKED"):
            print("--- 画面冒頭 ---")
            print(res["head"])
            return
        time.sleep(4)
        cur = page.evaluate("() => location.href")
        print("URL(後):", cur[:140])
        import re
        m = re.search(r"productId=(\d+)", cur)
        print("productId:", m.group(1) if m else "NOT_FOUND")
        # itm リンクは描画が遅れることがある → 最長 12s ポーリング
        info = page.evaluate(PANEL_TITLE_JS)
        deadline = time.time() + 12
        while info["itm"] is None and time.time() < deadline:
            time.sleep(2)
            info = page.evaluate(PANEL_TITLE_JS)
        print("itm:", info["itm"], "/ title:", info["title"][:90])


def cmd_apply(query: str, product_id: str, sites_csv: str, expected_itm: str) -> None:
    sites = [SITE_MAP[s.strip().upper()] for s in sites_csv.split(",")]
    arch_url = f"https://ebaymag.com/stock?archived=true&name={query}&productId={product_id}"
    active_url = f"https://ebaymag.com/stock?name={query}&productId={product_id}"

    with sync_playwright() as p:
        page = _get_page(p)

        # Step 1: アーカイブ panel を開いて「戻す」
        _goto_and_wait(page, arch_url)
        info = page.evaluate(PANEL_TITLE_JS)
        print("[1] panel:", info["url"][:110], "/ アクションbtn:", info["hasAction"])
        print("[1] panel タイトル:", info["title"][:120] or "(取得不能)", "/ itm:", info["itm"])
        # 安全弁 (権威): panel の itm リンク item id が期待値と不一致なら誤商品 → ABORT
        if info["hasAction"] and info["itm"] != expected_itm:
            print(f"[1] ABORT: itm={info['itm']} != expected={expected_itm} (誤商品)")
            sys.exit(2)
        if info["hasAction"]:
            r = page.evaluate(UNARCHIVE_JS)
            print("[1] unarchive:", r)
            if r == "MENU_OPENED":
                page.wait_for_timeout(1500)
                r = page.evaluate(UNARCHIVE_JS)
                print("[1] unarchive(2):", r)
            if r == "RESTORE_CLICKED":
                page.wait_for_timeout(5000)
            elif r != "RESTORE_CLICKED":
                print("[1] 戻すなし → 既にアクティブとみなし続行")
        else:
            print("[1] アーカイブ一覧に panel なし → 既にアクティブとみなし続行")

        # Step 2: アクティブ panel を開く
        _goto_and_wait(page, active_url)
        info = page.evaluate(PANEL_TITLE_JS)
        print("[2] panel:", info["url"][:110])
        print("[2] panel タイトル:", info["title"][:120] or "(取得不能)", "/ itm:", info["itm"])
        # 安全弁 (権威): itm 不一致 or panel 未取得なら mutation せず ABORT
        if not info["hasAction"] or info["itm"] != expected_itm:
            print(f"[2] ABORT: itm={info['itm']} != expected={expected_itm} → トグル操作せず終了")
            sys.exit(2)

        # Step 3: 各国トグル ON
        clicked = 0
        for site in sites:
            r = page.evaluate(TOGGLE_JS_TMPL, site)
            print(f"[3] toggle {site}: {r}")
            if r == "CLICKED":
                clicked += 1
                page.wait_for_timeout(1200)
            elif r.startswith("ALREADY_ON"):
                pass
            else:
                print(f"[3] ABORT: {site} のトグル失敗 → 保存せず終了")
                sys.exit(2)

        if clicked == 0:
            print("[3] 変更なし (全国既に ON) → 保存不要")
            sys.exit(0)

        # Step 4: 保存 (変動数チェック)
        page.wait_for_timeout(1000)
        r = page.evaluate(SAVE_JS, clicked)
        print("[4] save:", r)
        if not r.startswith("SAVED"):
            sys.exit(2)
        page.wait_for_timeout(6000)

        # Step 5: リロード検証
        _goto_and_wait(page, active_url)
        res = page.evaluate(VERIFY_JS)
        ok = True
        want = set(sites)
        for s in res["sites"]:
            mark = "ON " if (s["on"] and ("リスト" in (s["on"] or "") or s["on"] == "完売")) else \
                   ("on?" if s["on"] and "掲載されている" in s["on"] else "off")
            print(f"[5] {s['site']:<14} {mark} ({s['on']})")
        # 完売 = qty=0 でトグル ON 定着済 (在庫復活まで live なし) → 定着扱い
        on_sites = {s["site"] for s in res["sites"]
                    if s["on"] and ("リスト" in s["on"] or s["on"] in ("掲載されている", "完売"))}
        missing = want - on_sites
        if missing:
            print("[5] NG: 未定着 =", missing)
            ok = False
        if res["errors"]:
            print("[5] 画面エラー語:", res["errors"][:200])
            ok = False
        print("RESULT:", "OK" if ok else "NG")
        sys.exit(0 if ok else 3)


def cmd_revoke(query: str, product_id: str, sites_csv: str, expected_itm: str) -> None:
    """誤適用 undo: アクティブ panel → itm 照合 → 各国 OFF → 保存 → 検証."""
    sites = [SITE_MAP[s.strip().upper()] for s in sites_csv.split(",")]
    active_url = f"https://ebaymag.com/stock?name={query}&productId={product_id}"

    with sync_playwright() as p:
        page = _get_page(p)

        # Step 1: アクティブ panel を開いて itm 照合 (権威)
        _goto_and_wait(page, active_url)
        info = page.evaluate(PANEL_TITLE_JS)
        print("[1] panel:", info["url"][:110])
        print("[1] panel タイトル:", info["title"][:120] or "(取得不能)", "/ itm:", info["itm"])
        if not info["hasAction"] or info["itm"] != expected_itm:
            print(f"[1] ABORT: itm={info['itm']} != expected={expected_itm} → トグル操作せず終了")
            sys.exit(2)

        # Step 2: 各国トグル OFF
        clicked = 0
        for site in sites:
            r = page.evaluate(TOGGLE_OFF_JS_TMPL, site)
            print(f"[2] toggle-off {site}: {r}")
            if r == "CLICKED":
                clicked += 1
                page.wait_for_timeout(1200)
            elif r == "ALREADY_OFF":
                pass
            else:
                print(f"[2] ABORT: {site} の OFF 失敗 → 保存せず終了")
                sys.exit(2)

        if clicked == 0:
            print("[2] 変更なし (全国既に OFF) → 保存不要")
            sys.exit(0)

        # Step 3: 保存 (変動数チェック)
        page.wait_for_timeout(1000)
        r = page.evaluate(SAVE_JS, clicked)
        print("[3] save:", r)
        if not r.startswith("SAVED"):
            sys.exit(2)
        page.wait_for_timeout(6000)

        # Step 4: リロード検証 (対象国が全て OFF = 「掲載されていません」)
        _goto_and_wait(page, active_url)
        res = page.evaluate(VERIFY_JS)
        want = set(sites)
        still_on = set()
        for s in res["sites"]:
            on = s["on"] and s["on"] != "掲載されていません"
            print(f"[4] {s['site']:<14} {'ON ' if on else 'off'} ({s['on']})")
            if on and s["site"] in want:
                still_on.add(s["site"])
        ok = not still_on
        if still_on:
            print("[4] NG: まだ ON =", still_on)
        if res["errors"]:
            print("[4] 画面エラー語:", res["errors"][:200])
            ok = False
        print("RESULT:", "OK" if ok else "NG")
        sys.exit(0 if ok else 3)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "discover":
        cmd_discover(sys.argv[2],
                     archived=(len(sys.argv) < 4 or sys.argv[3] != "active"),
                     skip=int(sys.argv[4]) if len(sys.argv) > 4 else 0)
    elif cmd == "apply":
        if len(sys.argv) < 6:
            print("usage: apply <query> <productId> <SITES_CSV> <expected_itm>")
            sys.exit(1)
        cmd_apply(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif cmd == "revoke":
        if len(sys.argv) < 6:
            print("usage: revoke <query> <productId> <SITES_CSV> <expected_itm>")
            sys.exit(1)
        cmd_revoke(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    else:
        print("unknown cmd:", cmd)
        sys.exit(1)
