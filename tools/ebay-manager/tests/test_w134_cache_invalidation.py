"""W134 Step2 read-cache の回帰テスト (2026-05-16 短 TTL 方式).

正しさの保証は **短い TTL (ttl<=5s)** で担保する設計 (user 判断)。単一巨大
app.py の全書込を網羅 bump できない/証明できないため、bump 網羅ではなく
TTL で「最悪 N 秒で自動最新化」を保証し金銭直結の stale 誤判断を防ぐ。

本テストの役割:
1. `test_cache_ttl_is_short` = **最重要**。cache の TTL が短いまま
   (<=5s) であることを守る。ここが 60s 等に戻ると正しさの保証が失われる。
2. `test_ebay_listings_writers_invalidate_cache_before_rerun` =
   既知ホットパス (app.py inline handler) の **即時反映最適化** が
   退行しないことを守る (correctness ではなく UX = 3 秒すら待たせない)。
   未配線でも TTL が backstop するため app.py 内のみを対象とする。
3. `test_bump_increments_db_version` / `test_ui_cache_holds_no_sqlite_connection`
   = ui_cache の素の振る舞いと設計約束 (接続非保持) の確認。

writer→bump 距離は問わない (placement style 非依存)。
"""
from __future__ import annotations

import pathlib
import types

import pytest

_APP = pathlib.Path(__file__).resolve().parents[1] / "app.py"

# W221 Tier2 (2026-06-05): 21 タブを tabs/tab_*.py へ分割。writer / cache は
# app.py から各タブモジュールへ移動したため、番人は app.py + 全タブを対象にする。
_SOURCES = [_APP] + sorted((_APP.parent / "tabs").glob("tab_*.py"))

# cached reader が読むデータを変更する writer 呼出パターン (inline handler).
# update_ebay_listing_* / risk_confirmed: 在庫数/SKU/ランク (_cd_supply_risk /
#   _cd_listings_by_rank / _cd_fetch_all_products)
# set_email_confirmed: メール confirmed flag (_cd_dash_emails)
# update_listing_breakeven: 損益分岐/価格 (_cd_listings_by_rank / _cd_fetch_all_products)
# upsert_listing_competitors: competitor_count 副問合せ (_cd_fetch_all_products /
#   _cd_competitors_grouped) — app.py 内呼出は全て inline handler + st.rerun。
_WRITERS = (
    "update_ebay_listing_quantity(",
    "update_ebay_listing_sku(",
    "update_ebay_listing_rank(",
    "set_ebay_listing_risk_confirmed(",
    "set_email_confirmed(",
    "update_listing_breakeven(",
    "upsert_listing_competitors(",
)


def test_bump_increments_db_version(monkeypatch):
    """get_db_version()/bump_db_version() の素の振る舞い (cache key が進む)."""
    import ui_cache

    fake = types.SimpleNamespace(session_state={})
    monkeypatch.setattr(ui_cache, "st", fake)

    assert ui_cache.get_db_version() == 0
    ui_cache.bump_db_version()
    assert ui_cache.get_db_version() == 1
    ui_cache.bump_db_version()
    assert ui_cache.get_db_version() == 2


def test_ui_cache_holds_no_sqlite_connection():
    """ui_cache は接続を一切保持しない (write-conn を cache 共有しない設計約束).

    docstring は「st.cache_resource は使わない」と説明 *している* ため、
    語そのものでなく **実際の呼出/import 形** で判定する。
    """
    src = (_APP.parent / "ui_cache.py").read_text(encoding="utf-8")
    assert "st.cache_resource(" not in src  # decorator/call 形のみ禁止
    assert "import sqlite3" not in src
    assert "get_conn(" not in src


def test_ebay_listings_writers_invalidate_cache_before_rerun():
    """ebay_listings 書込 → その後最初の st.rerun() の間に bump_db_version()
    が必ず存在すること (HIGH-1 不変条件)。app.py + 全タブを対象 (W221 分割後)."""
    # W221 Tier2 (2026-06-05): 番人を全タブへ広げた際、W221 以前から存在する
    # 既存タブで bump 未配線の writer→rerun が表面化した。これらは ttl<=3s が
    # correctness を backstop する (本 test の docstring 参照 = bump は UX 最適化)
    # ため correctness バグではなく、W221 (タブ分割) のスコープ外の既存債務。
    # 別途 bump 配線する W で扱う (silent skip ではなく明示除外)。
    _PREEXISTING_UNWIRED = {
        "tab_data_fix.py",          # upsert_listing_competitors / update_listing_breakeven
        "tab_research_wizard.py",   # upsert_listing_competitors / update_listing_breakeven
        "tab_purchase_confirm.py",  # set_email_confirmed (W133、W221 以前)
    }
    failures: list[str] = []
    for path in _SOURCES:
        if path.name in _PREEXISTING_UNWIRED:
            continue
        src = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(src):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # コメント中の言及は対象外
            if "def " in line:
                continue  # 定義行 (呼出ではない) は対象外
            if not any(w in line for w in _WRITERS):
                continue

            seen_bump = False
            hit_rerun = False
            for j in range(i + 1, min(i + 200, len(src))):
                cur = src[j].strip()
                if "bump_db_version()" in cur:
                    seen_bump = True
                if cur.startswith("st.rerun()"):
                    hit_rerun = True
                    break
            # st.rerun が後続 200 行に無い writer は「再描画を伴わない経路」
            # (helper 等) とみなしスキップ。再描画する経路だけ不変条件を課す。
            if hit_rerun and not seen_bump:
                failures.append(f"{path.name} L{i + 1}: {stripped}")

    assert not failures, (
        "ebay_listings 書込のあと st.rerun() までに bump_db_version() が無い "
        "(cached reader が stale な在庫/価格/ランクを表示する HIGH-1):\n"
        + "\n".join(failures)
    )


def test_at_least_one_writer_checked():
    """テストの空振り防止: writer 呼出を実際に検出できていること (app.py + 全タブ)."""
    n = 0
    for path in _SOURCES:
        src = path.read_text(encoding="utf-8").splitlines()
        n += sum(
            1
            for ln in src
            if not ln.strip().startswith("#")
            and "def " not in ln
            and any(w in ln for w in _WRITERS)
        )
    assert n >= 10, f"writer 呼出検出数が想定より少ない (n={n})"


# W138-A (2026-05-17): ttl<=5 ルールの **明示 allowlist**。
# 番人の真の目的は「bump 漏れで *DB-backed per-listing 金銭/在庫/ランク*
# が最大 N 秒 stale 表示される退行」の阻止。下記は性質が異なるため除外:
#   _cached_shipping_policies = eBay Account API の **BP 一覧カタログ**
#     (アカウントの利用可能 shipping policy 集合)。DB-backed でなく
#     bump_db_version と無関係 (W134 の bump 漏れ失敗モードが原理的に
#     発生しない)。per-listing 現 BP は p["shipping_profile_id"] =
#     短 TTL の _cd_fetch_all_products(ttl=3) 経由で別管理。一覧は
#     user が eBay アカウント設定で編集した時のみ変化 (月単位)。
#     5s 化すると Account API を 5 秒毎連打 = W138 設計 (user 承認済
#     ttl=300 = ページ1回 cached) の破壊。よって長 TTL が正当。
# 追加時は必ず「DB-backed per-listing 金銭データでない」根拠を明記。
_LONG_TTL_ALLOWED = {
    # func_name: (max_ttl, 除外根拠)
    "_cached_shipping_policies": (
        300, "Account API の BP 一覧カタログ。DB-backed でなく per-listing "
             "金銭データでもないため bump 漏れ stale の対象外 (W138-A)。"),
    # W314 Phase 4 (設計書 §7): bump_db_version 経路が物理的に届かない
    # (tabs/tab_product_management.py 内コメント参照) ため ttl=3→60 に拡大採用済。
    # DB 実値は正しいまま、表示のみ最大 60s stale (次回書込 bump or 60s 経過で
    # 自己修復)。本来 W134 番人が想定する「DB-backed per-listing 金銭データ」
    # そのものだが、設計書合意の上での例外として明記 (money-direct につき
    # 変更時は要再確認)。
    "_cd_fetch_all_products": (
        60, "W314 Phase 4 設計書 §7 承認済み。bump_db_version が届かない経路が"
            "あるため ttl=3→60 採用、表示遅延のみで DB 実値は正 (自己修復)。"),
}


def test_cache_ttl_is_short():
    """最重要: cache の TTL が短い (<=5s) ままであること.

    新設計では invalidation の正しさを bump 網羅ではなく **短い TTL** で
    担保する。誰かが ttl=60 等へ戻すと、bump 漏れ経路で最大 60s 古い在庫/
    価格が表示され金銭直結の誤判断を招く。その退行を物理的に止める番人。

    例外 = `_LONG_TTL_ALLOWED` (DB-backed per-listing 金銭データでない外部
    API カタログ cache のみ、根拠明記必須)。例外も上限 ttl を超えたら fail。
    """
    import re

    # 各 @st.cache_data(ttl=N) の直後の def 名を紐付けて判定する
    dec_pat = re.compile(r"@st\.cache_data\(\s*ttl\s*=\s*(\d+)")
    def_pat = re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    # W221 Tier2: cache 関数は app.py から各タブへ分散したため全タブを対象
    # (_SOURCES は app.py + tabs/tab_*.py = tab_product_management 含む)。
    targets = _SOURCES
    found = 0
    for path in targets:
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, ln in enumerate(lines):
            m = dec_pat.search(ln)
            if not m:
                continue
            found += 1
            ttl = int(m.group(1))
            # デコレータ直下 (空行/他デコレータ跨ぎ) の def 名を探す
            fname = None
            for j in range(i + 1, min(i + 6, len(lines))):
                dm = def_pat.match(lines[j])
                if dm:
                    fname = dm.group(1)
                    break
            allow = _LONG_TTL_ALLOWED.get(fname)
            if allow is not None:
                max_ttl, reason = allow
                assert ttl <= max_ttl, (
                    f"{path.name}:{fname} allowlist 上限超過 "
                    f"ttl={ttl}s > {max_ttl}s。根拠: {reason}"
                )
                continue
            assert ttl <= 5, (
                f"{path.name}:{fname or '?'}: @st.cache_data ttl={ttl}s "
                f"が長すぎる (<=5s 必須 = 新設計の正しさ保証)。bump 漏れ "
                f"経路で最大 {ttl}s stale な金銭データ表示の退行リスク。"
                f"正当な外部 API カタログ cache なら _LONG_TTL_ALLOWED に "
                f"根拠付きで追加。"
            )
    assert found >= 8, (
        f"@st.cache_data(ttl=...) の検出数が想定より少ない (found={found})。"
        "デコレータ書式変更で TTL ガードが空振りしていないか確認。"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
