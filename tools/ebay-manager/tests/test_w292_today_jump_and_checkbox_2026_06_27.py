"""W292 回帰テスト: jump seed (bug#1 HIGH-1) + checkbox clobber 防止 (bug#2).

HIGH-1 修正確認
  - _resolve_pm_search_seed が純関数 (Streamlit 非依存) で正しく動作する。
  - jump 値は key 既存でも session_state["pm_search"] を上書きする性質
    (value= ではなく ss 直書きパターン) をシミュレートして確認。

HIGH-2 (checkbox bug#2)
  - set_initial_registered が ebay_item_id で書込む。
  - 外部 OFF (DB 真値 = False) が次 render の DB 真値に反映される
    (on_change callback の clobber でなく DB 側が正として振る舞う)。
  - 存在しない ebay_item_id は False を返す (silent skip でなく明示失敗)。

DB fixture: tmp_path / monkeypatch で本番 data/monitor.db を汚さない。
listing 識別は ebay_item_id (SKU 不使用 / sku-rules 準拠)。
"""
from __future__ import annotations

import pytest

from tabs.tab_today_tasks import _SS


# ─────────────────────────────────────────────────────────────────────────────
# Fixture: tmp DB (本番 DB を汚さない)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """tmp_path 配下に monitor.db を作成し、DB_PATH を monkeypatch で差し替える。"""
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    return db_mod


def _insert_listing(conn, ebay_item_id: str, title: str = "Test Listing") -> None:
    """テスト用 listing を INSERT (ebay_item_id が一意識別子)."""
    conn.execute(
        """INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price)
           VALUES (?, ?, ?, ?)""",
        (ebay_item_id, "stock:01", title, 99.0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# HIGH-1: jump seed — _resolve_pm_search_seed 純関数テスト
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_seed_jump_overwrites_existing():
    """jump 時 (initial_search 非空): key 既存でも jump 値が勝つ。

    session_state["pm_search"] に既存 user 入力 "old query" があっても、
    initial_search = "123456789012" が指定されたら jump 値を返す。
    """
    from tabs.tab_product_management import _resolve_pm_search_seed

    current = "old query"
    result = _resolve_pm_search_seed("123456789012", current)
    assert result == "123456789012", (
        "jump 時は initial_search が current を上書きしなければならない"
    )


def test_resolve_seed_non_jump_preserves_existing():
    """非 jump (initial_search 空): 既存 user 入力を維持する。

    initial_search が空文字列 → current がそのまま返る (session_state 温存)。
    """
    from tabs.tab_product_management import _resolve_pm_search_seed

    current = "my search term"
    result = _resolve_pm_search_seed("", current)
    assert result == "my search term", (
        "非 jump 時は user 入力を維持しなければならない"
    )


def test_resolve_seed_empty_both():
    """initial_search も current も空 → 空文字列。"""
    from tabs.tab_product_management import _resolve_pm_search_seed

    result = _resolve_pm_search_seed("", "")
    assert result == ""


def test_resolve_seed_session_state_write_pattern():
    """session_state 直書きパターンのシミュレーション。

    Streamlit の session_state を plain dict で代替し、
    「key 既存でも jump 値が session_state を上書きする」動作を確認する。
    value= 方式では key 既存時に Streamlit が無視するため、
    session_state 直書きが唯一の確定 seed 手段。
    """
    from tabs.tab_product_management import _resolve_pm_search_seed

    # pm_search key が既に存在する状態 (商品管理を開いたまま jump)
    fake_ss: dict = {"pm_search": "残存した前の検索値"}

    jump_eid = "111222333444"
    new_seed = _resolve_pm_search_seed(jump_eid, fake_ss.get("pm_search", ""))
    # session_state に直書き (= _apply_filter_and_sort の実装を模倣)
    fake_ss["pm_search"] = new_seed

    assert fake_ss["pm_search"] == jump_eid, (
        "jump 後 pm_search は item_id に差し替わらなければならない"
    )


def test_resolve_seed_consecutive_jump_a_then_b():
    """連続 jump: listing A → 戻る → listing B の順で正しく seed される。

    各 jump 時に _resolve_pm_search_seed が呼ばれ session_state を更新すると、
    前の jump 値 (A) が残存せず B に差し替わることを確認。
    """
    from tabs.tab_product_management import _resolve_pm_search_seed

    fake_ss: dict = {}

    # jump to A
    fake_ss["pm_search"] = _resolve_pm_search_seed(
        "111111111111", fake_ss.get("pm_search", "")
    )
    assert fake_ss["pm_search"] == "111111111111"

    # user が検索欄を消さずに別 listing へ jump (B)
    fake_ss["pm_search"] = _resolve_pm_search_seed(
        "222222222222", fake_ss.get("pm_search", "")
    )
    assert fake_ss["pm_search"] == "222222222222", (
        "連続 jump で B の値が A に差し替わらなければならない"
    )


# ─────────────────────────────────────────────────────────────────────────────
# HIGH-2 (bug#2): checkbox — set_initial_registered + clobber 防止
# ─────────────────────────────────────────────────────────────────────────────

def test_set_initial_registered_writes_by_ebay_item_id(tmp_db):
    """set_initial_registered が ebay_item_id で書き込む (SKU でなく)。"""
    from monitor.database import get_conn, set_initial_registered

    with get_conn() as c:
        _insert_listing(c, "EID_CHK_001")

    ok = set_initial_registered("EID_CHK_001", True)
    assert ok is True

    with get_conn() as c:
        row = c.execute(
            "SELECT initial_registered FROM ebay_listings WHERE ebay_item_id=?",
            ("EID_CHK_001",),
        ).fetchone()
    assert row is not None
    assert row[0] == 1, "initial_registered が 1 になっていない"


def test_external_off_reflected_in_db_true_value(tmp_db):
    """外部 OFF が次 render の DB 真値 (False) に反映される。

    外部で initial_registered を OFF に変更 → DB 真値が False になり、
    次の render でその値を取得すると False が返る (clobber が起きていない)。
    """
    from monitor.database import get_conn, set_initial_registered

    with get_conn() as c:
        _insert_listing(c, "EID_CHK_002")

    # ON にしてから外部で OFF
    set_initial_registered("EID_CHK_002", True)
    ok = set_initial_registered("EID_CHK_002", False)
    assert ok is True

    with get_conn() as c:
        row = c.execute(
            "SELECT initial_registered FROM ebay_listings WHERE ebay_item_id=?",
            ("EID_CHK_002",),
        ).fetchone()
    # DB の真値が False (0) になっている = 次 render で is_done=False が正しく届く
    assert row[0] == 0, "外部 OFF 後に DB 真値が False になっていない"


def test_checkbox_on_change_does_not_clobber_external_change(tmp_db):
    """on_change callback は toggle 後の値 (DB 真値) を反映し、
    外部変更 (別ルートでの DB 書込) を巻き戻さない。

    シナリオ:
      1. 外部で OFF が書込まれ DB = False
      2. render 時に session_state を DB 真値 (False) で同期
      3. on_change は session_state の現在値 (False) を DB に書く
      → DB は False のまま = clobber 無し
    """
    from monitor.database import get_conn, set_initial_registered

    with get_conn() as c:
        _insert_listing(c, "EID_CHK_003")

    # まず ON
    set_initial_registered("EID_CHK_003", True)

    # 外部 OFF (別 agent / 別 tab からの変更を模倣)
    set_initial_registered("EID_CHK_003", False)

    # render で DB 真値を読み session_state に同期
    fake_ss: dict = {}
    eid = "EID_CHK_003"
    chk_key = f"{_SS}chk_{eid}"

    with get_conn() as c:
        row = c.execute(
            "SELECT initial_registered FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    is_done = bool(row[0])        # DB 真値 = False
    fake_ss[chk_key] = is_done    # render 冒頭の session_state 同期 (実装模倣)

    # on_change 相当: session_state の現在値を DB に書く
    new_val = bool(fake_ss.get(chk_key, False))
    set_initial_registered(eid, new_val)

    with get_conn() as c:
        row2 = c.execute(
            "SELECT initial_registered FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert row2[0] == 0, "外部 OFF が on_change により clobber された (巻き戻りバグ)"


def test_set_initial_registered_nonexistent_returns_false(tmp_db):
    """存在しない ebay_item_id は False 返却 (rowcount=0 / silent skip でない)。"""
    from monitor.database import set_initial_registered

    ok = set_initial_registered("NONEXISTENT_EID_W292", True)
    assert ok is False, "存在しない ID への書込は False を返すべき"
