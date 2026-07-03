"""W314 Phase 2 S6 code-reviewer HIGH1 回帰テスト (2026-07-03).

同一 listing (ebay_item_id) に候補 2 件以上が同時 followup アクティブになると
render_supplier_followup_section が同じ eid で render_finishing_panel を 2 回
呼び、パネル側の widget key (pf_{eid}_*) が重複して StreamlitDuplicateElementKey
で followup 全体がクラッシュする。1 listing に複数候補は正常データなので
到達可能なパス。

fix (案 A / K1): _seen_eids で先着 cid のみ描画、後続 cid は st.caption で
「上のパネル完了後に順次表示」と明示 (Q0 silent skip 防止)。

本テストは streamlit を最小限モックした上で render_supplier_followup_section
を実行し、render_finishing_panel の呼出回数と第 1 引数 (eid) を検証する。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class _DummyCtx:
    """with 文サポートのダミーコンテキスト (st.container / st.spinner 等の代替)."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeStreamlit:
    """render_supplier_followup_section が触る streamlit API の最小モック.

    session_state は dict、その他の描画呼出は呼ばれても何もしない no-op。
    st.rerun は呼ばれる可能性があるが本テストでは button を False にするため到達しない。
    """

    def __init__(self, session_state: dict):
        self.session_state = session_state

    def container(self, *a, **kw):
        return _DummyCtx()

    def spinner(self, *a, **kw):
        return _DummyCtx()

    def expander(self, *a, **kw):
        return _DummyCtx()

    def columns(self, *a, **kw):
        # 呼ばれても with で使う対象になり得る。dummy を返す。
        return [_DummyCtx()] * 8

    def markdown(self, *a, **kw): pass
    def caption(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def success(self, *a, **kw): pass
    def write(self, *a, **kw): pass
    def image(self, *a, **kw): pass
    def metric(self, *a, **kw): pass
    def text_input(self, *a, **kw): return ""
    def text_area(self, *a, **kw): return ""
    def number_input(self, *a, **kw): return 0
    def selectbox(self, *a, **kw): return ""
    def radio(self, *a, **kw): return ""
    def button(self, *a, **kw): return False
    def table(self, *a, **kw): pass
    def dataframe(self, *a, **kw): pass
    def rerun(self, *a, **kw): pass

    def fragment(self, fn):
        return fn


def test_duplicate_eid_across_candidates_renders_panel_once(monkeypatch):
    """同一 eid に紐付く候補 2 件が同時 active な時、パネルは 1 回だけ描画される (HIGH1 回帰)."""
    import tabs._supplier_followup_section as sec

    # streamlit を fake に差し替え
    fake_ss: dict = {}
    fake_st = _FakeStreamlit(fake_ss)
    monkeypatch.setattr(sec, "st", fake_st)

    # 同一 eid=E1 に紐付く 2 候補を followup active にする
    cid1, cid2 = 101, 102
    eid = "E1"
    fake_ss[f"_sup_photo_prompt_{cid1}"] = True
    fake_ss[f"_sup_photo_prompt_{cid2}"] = True
    fake_ss[f"_sup_photo_meta_{cid1}"] = {"url": "https://a", "eid": eid, "title": "T1"}
    fake_ss[f"_sup_photo_meta_{cid2}"] = {"url": "https://b", "eid": eid, "title": "T2"}

    # render_finishing_panel の呼出を捕捉
    calls: list[tuple] = []

    def _fake_panel(eid_arg, config, *, candidate_id=None, candidate_url=None, source_tab):
        calls.append((eid_arg, candidate_id, source_tab))

    import tabs._finishing_panel as fp
    monkeypatch.setattr(fp, "render_finishing_panel", _fake_panel)

    rendered_any = sec.render_supplier_followup_section(source_tab="supplier")

    assert rendered_any is True, "followup 対象 cid が 2 件あるので True 返却"
    assert len(calls) == 1, (
        f"同一 eid ({eid}) では render_finishing_panel は 1 回のみのはず (実際 {len(calls)} 回)"
        f" — HIGH1 (StreamlitDuplicateElementKey 予防) 違反"
    )
    assert calls[0][0] == eid, "描画された eid が想定通り"
    # 先着 cid = sorted(cid1, cid2)[0] = cid1
    assert calls[0][1] == cid1, "先着 (最小) cid が描画されたこと (実装は sorted 順)"


def test_distinct_eids_render_each_panel(monkeypatch):
    """異なる eid の候補が同時 active な時は全件描画される (回帰過剰抑止防止)."""
    import tabs._supplier_followup_section as sec

    fake_ss: dict = {}
    fake_st = _FakeStreamlit(fake_ss)
    monkeypatch.setattr(sec, "st", fake_st)

    fake_ss["_sup_photo_prompt_201"] = True
    fake_ss["_sup_photo_prompt_202"] = True
    fake_ss["_sup_photo_meta_201"] = {"url": "https://a", "eid": "EA", "title": "T-A"}
    fake_ss["_sup_photo_meta_202"] = {"url": "https://b", "eid": "EB", "title": "T-B"}

    calls: list[tuple] = []

    def _fake_panel(eid_arg, config, *, candidate_id=None, candidate_url=None, source_tab):
        calls.append((eid_arg, candidate_id))

    import tabs._finishing_panel as fp
    monkeypatch.setattr(fp, "render_finishing_panel", _fake_panel)

    sec.render_supplier_followup_section(source_tab="inventory")

    assert len(calls) == 2, "異なる eid はそれぞれ描画される"
    assert {c[0] for c in calls} == {"EA", "EB"}


def test_empty_eid_candidates_do_not_share_seen_set(monkeypatch):
    """eid が空 (meta 補完失敗) の候補は互いに widget key 衝突しない (cid が識別子として機能)。

    _seen_eids は空文字を追加しないため、eid="" の候補は複数あっても全て描画される。
    パネル側の widget key は pf_{eid}_* だが eid が空文字だと候補間で衝突するように
    見える (実際は pf__title 等)。本テストでは実装が eid="" を _seen_eids に追加
    しないことのみを検証する (empty eid 挙動は現時点の仕様、meta 補完失敗自体が
    別の防御対象で本 HIGH1 の scope 外)。
    """
    import tabs._supplier_followup_section as sec

    fake_ss: dict = {}
    fake_st = _FakeStreamlit(fake_ss)
    monkeypatch.setattr(sec, "st", fake_st)

    fake_ss["_sup_photo_prompt_301"] = True
    fake_ss["_sup_photo_prompt_302"] = True
    # meta 空 = DB 補完も失敗する状況を作るため、DB 呼出を空にモック
    fake_ss["_sup_photo_meta_301"] = {"url": "", "eid": "", "title": ""}
    fake_ss["_sup_photo_meta_302"] = {"url": "", "eid": "", "title": ""}

    import monitor.database as db_mod

    class _EmptyCursor:
        def execute(self, *a, **kw):
            return self

        def fetchone(self):
            return None

    class _EmptyConn:
        def execute(self, *a, **kw):
            return _EmptyCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(db_mod, "get_conn", lambda: _EmptyConn())

    calls: list[tuple] = []

    def _fake_panel(eid_arg, config, *, candidate_id=None, candidate_url=None, source_tab):
        calls.append((eid_arg, candidate_id))

    import tabs._finishing_panel as fp
    monkeypatch.setattr(fp, "render_finishing_panel", _fake_panel)

    sec.render_supplier_followup_section(source_tab="supplier")

    # eid 空は _seen_eids に追加されないため、両方 (cid=301, cid=302) 描画される
    assert len(calls) == 2, "eid 空の候補は _seen_eids に追加せず全て描画"
