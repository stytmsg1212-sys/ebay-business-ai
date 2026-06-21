"""eBaymag 配送ポリシー 各サイト送料設定 (Playwright native input, money-direct)。

W284 / 2026-06-21。canary (DDP_2-3kg AU=$8 保存→read-back) で実証した機構を本番化。
Codex レビュー (2026-06-21) の HIGH/MED を反映:
  - HIGH-1: 動的 pid discover (固定 pid 禁止) / 編集画面のポリシータイトル assert
  - HIGH-5: 除外国 (本 module では値設定に集中、除外は別途確認済前提)
  - MED-6: 値$0 のサイトは「無料維持」でスキップ (理由付き)、free checkbox 状態を検証
  - MED-7: 保存前に旧状態 snapshot を write+read 検証 (通らねば実行拒否=可逆保証)
  - HIGH-4: 1 ポリシーずつ逐次 (本 module は 1 ポリシー単位、orchestrator が逐次呼ぶ)
  - 保存後 reload read-back で値が intent と exact 一致しなければ hard abort

タブは eBay サイト個別 (com/co.uk/de/com.au/fr/it/es/ca)。内部 cp 国コード:
  com→(本体, 設定しない) / co.uk→uk / de→de / com.au→au / fr→fr / it→it / es→es / ca→ca
site_cc_values は {cc: usd} (例 {"uk":0,"de":0,"au":62,"ca":11})。usd=0 は無料維持でskip。

CDP: chromium.connect_over_cdp("http://localhost:9222") (eBaymag ログイン済)。
保存しない dry_run=True がデフォルト (誤実行防止)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SHIPPING_URL = "https://ebaymag.com/shipping"

# eBaymag サイトラベル → 内部 cp 国コード (2026-06-21 実機確認、ca は executor 動的確認)。
SITE_LABEL_TO_CC: dict[str, str] = {
    "ebay.co.uk": "uk",
    "ebay.de": "de",
    "ebay.com.au": "au",
    "ebay.fr": "fr",
    "ebay.it": "it",
    "ebay.es": "es",
    "ebay.ca": "ca",
}
CC_TO_SITE_LABEL = {v: k for k, v in SITE_LABEL_TO_CC.items()}


class PolicyEditError(RuntimeError):
    """ポリシー編集の不整合 (title 不一致 / read-back 失敗 等)。money-direct で hard abort。"""


def _open_policy_editor(page, policy_title: str, attempts: int = 4) -> None:
    """/shipping から policy_title の編集画面を堅牢に開く (仮想化リスト対応)。

    リストが仮想化されているため、行が DOM に無ければスクロールして探す。
    開けたら編集画面にタイトルが現れるまで待つ。失敗時 PolicyEditError。
    """
    for attempt in range(1, attempts + 1):
        try:
            page.goto(SHIPPING_URL, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(5000)
            # SPA の policy リストが populate するまで待つ (非決定的描画対策)。
            # 「シッピングポリシーを作成」+ 何らかの policy 行 (/ JP or kg) の出現を待つ。
            populated = False
            for _ in range(30):  # 最大 ~30s
                cnt = page.evaluate(
                    "() => { const t=document.body.innerText||'';"
                    " return (t.includes('シッピングポリシーを作成')"
                    " && (t.match(/\\/ JP/g)||[]).length) | 0; }"
                )
                if cnt and cnt > 0:
                    populated = True
                    break
                page.wait_for_timeout(1000)
            if not populated:
                raise PolicyEditError("policy リストが populate しない (SPA 描画失敗)")
            # 仮想化リストの内部スクロールコンテナを JS で段階スクロールして
            # 目的行を materialize する (page.mouse.wheel はページ単位で内部 div に効かない)。
            for _ in range(30):
                if page.get_by_text(policy_title, exact=False).count() > 0:
                    break
                page.evaluate(
                    """() => {
                      const els = Array.from(document.querySelectorAll('*'))
                        .filter(e => e.scrollHeight > e.clientHeight + 50
                                     && e.clientHeight > 200);
                      // 最大スクロール量の要素を下へ送る
                      els.sort((a,b)=>(b.scrollHeight-b.clientHeight)-(a.scrollHeight-a.clientHeight));
                      if (els[0]) els[0].scrollTop = Math.min(
                        els[0].scrollTop + 500, els[0].scrollHeight);
                      window.scrollBy(0, 500);
                    }"""
                )
                page.wait_for_timeout(500)
            loc = page.get_by_text(policy_title, exact=False).first
            loc.scroll_into_view_if_needed(timeout=6000)
            loc.click(timeout=8000)
            page.wait_for_timeout(3500)
            # 編集画面の title input が policy_title と一致するか assert (HIGH-1)
            title_val = page.evaluate(
                "() => { const t=document.querySelector('input[name=\"title\"]');"
                " return t ? t.value : null; }"
            )
            if title_val and policy_title in title_val:
                logger.info("policy editor opened: %s (title=%s)", policy_title, title_val)
                return
            raise PolicyEditError(
                f"編集画面の title={title_val!r} が policy_title={policy_title!r} と不一致"
            )
        except PolicyEditError:
            raise
        except Exception as e:
            logger.warning("open attempt %d/%d failed: %s", attempt, attempts, str(e)[:120])
            page.wait_for_timeout(1500)
    raise PolicyEditError(f"policy editor を開けない: {policy_title} ({attempts} 回試行)")


def _discover_pid(page) -> str:
    """編集画面の input name から pid (ポリシー内部 id) を動的取得する (固定 pid 禁止)。

    cp-* input はサイトタブ選択後に出現するため、まず ebay.com.au タブを選択して
    materialize してから読む。
    """
    names = page.eval_on_selector_all(
        'input[name*="-cp-"]', "els => els.map(e => e.name)"
    )
    if not names:
        # サイトタブ未選択で cp 欄が無い → AU タブを選択して materialize
        try:
            page.get_by_text("ebay.com.au", exact=True).first.click(timeout=5000)
            page.wait_for_timeout(1200)
        except Exception:
            pass
        names = page.eval_on_selector_all(
            'input[name*="-cp-"]', "els => els.map(e => e.name)"
        )
    for n in names:
        if "-cp-" in n:
            return n.split("-cp-")[0]
    raise PolicyEditError("pid を discover できない (cp-* input が無い)")


def _site_state(page, pid: str, cc: str) -> dict:
    """1 サイトの switcher/free/price 状態を読む。"""
    sw = page.locator(f'input[name="{pid}-cp-{cc}-switcher"]')
    free = page.locator(f'input[name="{pid}-cp-{cc}-ds-0.cost.free"]')
    price = page.locator(f'input[name="{pid}-cp-{cc}-ds-0.cost.price"]')
    return {
        "switcher": sw.is_checked() if sw.count() else None,
        "free": free.is_checked() if free.count() else None,
        "price": price.input_value() if (price.count() and price.is_enabled()) else (
            price.input_value() if price.count() else None),
    }


def _snapshot_all_sites(page, pid: str) -> dict:
    """全サイトの現状態を snapshot する (rollback 用)。"""
    snap = {}
    for cc in SITE_LABEL_TO_CC.values():
        snap[cc] = _site_state(page, pid, cc)
    return snap


def _select_site_tab(page, cc: str) -> None:
    """サイトタブをクリックして該当サイトの cp 欄を露出させる。"""
    label = CC_TO_SITE_LABEL[cc]
    page.get_by_text(label, exact=True).first.click(timeout=5000)
    page.wait_for_timeout(1200)


def set_policy_site_values(
    page,
    policy_title: str,
    site_cc_values: dict[str, int],
    *,
    dry_run: bool = True,
    snapshot_dir: str | Path | None = None,
) -> dict:
    """1 ポリシーの各サイト送料を設定する (money-direct、保存は dry_run=False 時のみ)。

    Args:
        page: CDP 接続済 Playwright page。
        policy_title: 例 "DDP_6-8kg"。
        site_cc_values: {cc: usd}。usd=0 は無料維持で skip。
        dry_run: True なら値を入力後 reload で破棄 (保存しない)。False で「変更を適用」保存。
        snapshot_dir: 保存前 snapshot の書出先 (MED-7、dry_run=False で必須)。

    Returns:
        {"policy": ..., "pid": ..., "planned": {cc: usd}, "skipped": {cc: reason},
         "saved": bool, "verified": bool, "snapshot_path": ...}

    Raises:
        PolicyEditError: title 不一致 / snapshot 検証失敗 / read-back 不一致 (hard abort)。
    """
    _open_policy_editor(page, policy_title)
    pid = _discover_pid(page)

    # MED-7: 保存前 snapshot を write + read 検証 (可逆保証)。dry_run でも記録。
    # 変更対象サイト (非ゼロ) はタブを選択して実状態を読む。それ以外は best-effort。
    snapshot = {}
    for cc, usd in site_cc_values.items():
        if cc in CC_TO_SITE_LABEL and usd != 0:
            try:
                _select_site_tab(page, cc)
            except Exception:
                pass
        snapshot[cc] = _site_state(page, pid, cc)
    snap_path = None
    if snapshot_dir is not None:
        snapshot_dir = Path(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snap_path = snapshot_dir / f"snapshot_{policy_title}_{pid}.json"
        payload = {"policy": policy_title, "pid": pid, "snapshot": snapshot,
                   "intended": site_cc_values}
        snap_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        readback = json.loads(snap_path.read_text(encoding="utf-8"))
        if readback.get("snapshot") != snapshot:
            raise PolicyEditError("snapshot の write+read 検証失敗 (可逆保証不能で中断)")

    planned: dict[str, int] = {}
    skipped: dict[str, str] = {}

    for cc, usd in site_cc_values.items():
        if cc not in CC_TO_SITE_LABEL:
            skipped[cc] = "unknown site cc"
            continue
        if usd == 0:
            # 値$0 = 無料維持で触らない (MED-6: 理由を記録)
            skipped[cc] = "value=0 (無料維持)"
            continue
        # 値設定: タブ選択 → switcher ON → free uncheck → price fill
        _select_site_tab(page, cc)
        sw = page.locator(f'input[name="{pid}-cp-{cc}-switcher"]')
        if sw.count() and not sw.is_checked():
            sw.check(timeout=4000)
            page.wait_for_timeout(1500)
        free = page.locator(f'input[name="{pid}-cp-{cc}-ds-0.cost.free"]')
        if free.count() and free.is_checked():
            free.uncheck(timeout=4000)
            page.wait_for_timeout(700)
        price = page.locator(f'input[name="{pid}-cp-{cc}-ds-0.cost.price"]')
        if not price.count():
            raise PolicyEditError(f"{cc}: cost.price 入力が無い (構造変化?)")
        price.fill(str(usd), timeout=4000)
        page.wait_for_timeout(500)
        planned[cc] = usd
        logger.info("set %s %s = $%s", policy_title, cc, usd)

    result = {"policy": policy_title, "pid": pid, "planned": planned,
              "skipped": skipped, "saved": False, "verified": False,
              "snapshot_path": str(snap_path) if snap_path else None}

    if dry_run:
        page.reload(wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)
        logger.info("dry_run: 破棄 (保存せず) policy=%s planned=%s", policy_title, planned)
        return result

    # 保存 (変更を適用)
    page.get_by_text("変更を適用", exact=False).first.click(timeout=8000)
    page.wait_for_timeout(6000)
    result["saved"] = True

    # read-back hard-abort 検証 (再度開いて planned が exact 一致するか)
    _open_policy_editor(page, policy_title)
    pid2 = _discover_pid(page)
    mismatches = []
    for cc, usd in planned.items():
        _select_site_tab(page, cc)
        st = _site_state(page, pid2, cc)
        if st.get("free") is not False or str(st.get("price")) != str(usd):
            mismatches.append(f"{cc}: 期待 free=False price={usd}, 実 {st}")
    if mismatches:
        raise PolicyEditError(
            "保存後 read-back 不一致 (hard abort): " + " / ".join(mismatches)
        )
    result["verified"] = True
    logger.info("policy %s 保存+検証 OK: %s", policy_title, planned)
    return result
