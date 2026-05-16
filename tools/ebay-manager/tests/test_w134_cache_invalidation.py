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

# cached reader が読むデータを変更する writer 呼出パターン (app.py inline handler).
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
    """app.py: ebay_listings 書込 → その後最初の st.rerun() の間に
    bump_db_version() が必ず存在すること (HIGH-1 不変条件)."""
    src = _APP.read_text(encoding="utf-8").splitlines()
    failures: list[str] = []

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
            failures.append(f"L{i + 1}: {stripped}")

    assert not failures, (
        "ebay_listings 書込のあと st.rerun() までに bump_db_version() が無い "
        "(cached reader が stale な在庫/価格/ランクを表示する HIGH-1):\n"
        + "\n".join(failures)
    )


def test_at_least_one_writer_checked():
    """テストの空振り防止: writer 呼出を実際に検出できていること."""
    src = _APP.read_text(encoding="utf-8").splitlines()
    n = sum(
        1
        for ln in src
        if not ln.strip().startswith("#")
        and "def " not in ln
        and any(w in ln for w in _WRITERS)
    )
    assert n >= 10, f"writer 呼出検出数が想定より少ない (n={n})"


def test_cache_ttl_is_short():
    """最重要: cache の TTL が短い (<=5s) ままであること.

    新設計では invalidation の正しさを bump 網羅ではなく **短い TTL** で
    担保する。誰かが ttl=60 等へ戻すと、bump 漏れ経路で最大 60s 古い在庫/
    価格が表示され金銭直結の誤判断を招く。その退行を物理的に止める番人。
    """
    import re

    pat = re.compile(r"@st\.cache_data\(\s*ttl\s*=\s*(\d+)")
    targets = [
        _APP,
        _APP.parent / "tabs" / "tab_product_management.py",
    ]
    found = 0
    for path in targets:
        for m in pat.finditer(path.read_text(encoding="utf-8")):
            found += 1
            ttl = int(m.group(1))
            assert ttl <= 5, (
                f"{path.name}: @st.cache_data ttl={ttl}s が長すぎる "
                f"(<=5s 必須 = 新設計の正しさ保証)。bump 漏れ経路で "
                f"最大 {ttl}s stale な金銭データ表示の退行リスク。"
            )
    assert found >= 8, (
        f"@st.cache_data(ttl=...) の検出数が想定より少ない (found={found})。"
        "デコレータ書式変更で TTL ガードが空振りしていないか確認。"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
