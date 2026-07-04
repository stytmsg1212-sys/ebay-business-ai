# -*- coding: utf-8 -*-
"""W317: eBaymag discover の GraphQL 商品 ID 照合 (product_map) 回帰テスト.

対象:
  - monitor.ebaymag_graphql.list_products (filters=None を必ず渡す)
  - monitor.ebaymag_driver._build_id_map / _item_id_from_url (抽出/衝突除外/US tie-break)
  - tasks.task_ebaymag_apply_queue._process_job の discover 分岐 (map hit / miss fallback)
  - tasks.task_ebaymag_apply_queue._build_product_map_if_needed (map 取得失敗時の継続)

GraphQL / CDP は mock (live 実行なし、S2 が担当)。
"""
import monitor.database as db
import monitor.ebaymag_driver as drv
from monitor import ebaymag_graphql as G
from monitor.ebaymag_driver import _build_id_map, _item_id_from_url


# ───────── list_products: filters=None を必ず渡す ─────────

def test_list_products_passes_filters_none(monkeypatch):
    """list_products は variables に filters=None を明示 ({} を渡さない = archived 込み全件)."""
    captured = {}

    def fake_gql(page, op, query, variables):
        captured["op"] = op
        captured["variables"] = variables
        return {"products": {"totalCount": 5,
                             "pageInfo": {"hasNextPage": False, "endCursor": None},
                             "nodes": []}}

    monkeypatch.setattr(G, "gql", fake_gql)
    conn = G.list_products(object(), first=200, after="CUR1")

    assert captured["op"] == "Products"
    v = captured["variables"]
    assert v["first"] == 200
    assert v["after"] == "CUR1"
    # {} ではなく filters=None (None を渡さないと暗黙 archived:false に絞られ map 欠落)
    assert "filters" in v and v["filters"] is None
    assert conn["totalCount"] == 5


# ───────── _item_id_from_url / _build_id_map ─────────

def _node(pid, listings):
    return {"id": pid, "listings": listings}


def _li(site, url):
    return {"site": {"id": site}, "publicationUrl": url}


def test_item_id_extraction():
    assert _item_id_from_url("https://www.ebay.com/itm/358689688709") == "358689688709"
    assert _item_id_from_url("https://www.ebay.it/itm/358663940394?foo=1") == "358663940394"
    assert _item_id_from_url(None) is None
    assert _item_id_from_url("https://www.ebay.com/no-item-here") is None


def test_item_id_extraction_itm_anchored_no_slug_false_positive():
    """/itm/ アンカー化: slug 内の紛れ数字 (型番等) を item id と誤抽出しない."""
    # /itm/<slug>/<id> 形式 (slug に 9 桁以上の数字を含む) → 末尾の id を採用
    assert _item_id_from_url(
        "https://www.ebay.de/itm/sony-wm-123456789012-walkman/358663924423"
    ) == "358663924423"
    # /itm/ 外のパスセグメントに 9 桁数字があっても抽出しない (アンカー必須)
    assert _item_id_from_url("https://www.ebay.com/p/123456789012") is None
    assert _item_id_from_url("https://example.com/x/999888777666/y") is None
    # 末尾 id セグメントが数字で始まらない形式 → slug 数字を拾わない
    assert _item_id_from_url("https://www.ebay.com/itm/model-999888777666x") is None


def test_build_id_map_extracts_all_sites():
    """どのサイトの publicationUrl からでも item_id を採用 (US 限定にしない). null は無視."""
    nodes = [
        _node("P1", [_li("0", "https://www.ebay.com/itm/358689688709"),
                     _li("3", "https://www.ebay.co.uk/itm/358663924423")]),
        _node("P2", [_li("101", "https://www.ebay.it/itm/358663940394?x=1")]),
        _node("P3", [_li("77", None)]),  # そのサイトで非出品 = publicationUrl null → 抽出なし
    ]
    m, collisions = _build_id_map(nodes)
    assert collisions == 0
    assert m == {
        "358689688709": "P1",
        "358663924423": "P1",
        "358663940394": "P2",
    }


def test_build_id_map_collision_excluded():
    """同一 item_id が別 product_id を指し US も無い衝突 → map から除外 + カウント."""
    nodes = [
        _node("P1", [_li("3", "https://www.ebay.co.uk/itm/999888777666")]),
        _node("P2", [_li("101", "https://www.ebay.it/itm/999888777666")]),
    ]
    m, collisions = _build_id_map(nodes)
    assert collisions == 1
    assert "999888777666" not in m


def test_build_id_map_us_tiebreak():
    """衝突エントリ中の US (site 0) が一意の product_id を指す → US を tie-break 採用."""
    nodes = [
        _node("PUS", [_li("0", "https://www.ebay.com/itm/111222333444")]),
        _node("POTHER", [_li("3", "https://www.ebay.co.uk/itm/111222333444")]),
    ]
    m, collisions = _build_id_map(nodes)
    assert collisions == 0  # tie-break 成立 = 除外ではない
    assert m["111222333444"] == "PUS"


# ───────── _process_job discover 分岐 ─────────

def _patch_process_job_common(monkeypatch, *, product):
    """_process_job が使う DB/driver 関数を mock. desired_sites 空 + band 一致 (両軸 no-op)."""
    upserts: list = []
    marks: list = []
    monkeypatch.setattr(db, "get_ebaymag_desired", lambda eid: {"desired_sites": []})
    monkeypatch.setattr(db, "get_ebaymag_product", lambda eid: product)
    monkeypatch.setattr(
        db, "upsert_ebaymag_product",
        lambda eid, product_id=None, site_states=None: upserts.append((eid, product_id, site_states)),
    )
    monkeypatch.setattr(
        db, "mark_ebaymag_apply_status",
        lambda job_id, status, **kw: marks.append((job_id, status, kw)),
    )
    # band truthy + applied==canonical → band sync 回避 + 軸2 no-op
    monkeypatch.setattr(db, "get_ebaymag_policy_state",
                        lambda eid: {"band": "B1", "applied_token": "TOK1"})
    monkeypatch.setattr(db, "get_canonical_policy_token", lambda band: "TOK1")
    monkeypatch.setattr(db, "record_ebaymag_policy_applied", lambda eid, token: True)
    return upserts, marks


def test_process_job_map_hit_uses_graphql(monkeypatch):
    """map hit → product_id 即確定 + upsert、title discover は呼ばれない (method=graphql)."""
    upserts, marks = _patch_process_job_common(monkeypatch, product=None)
    discover_calls: list = []
    monkeypatch.setattr(drv, "discover_product_id",
                        lambda q, itm: discover_calls.append((q, itm)))

    from tasks.task_ebaymag_apply_queue import _process_job
    job = {"id": 7, "ebay_item_id": "358689688709", "attempts": 0}
    res = _process_job(job, {}, {"358689688709": "PID_MAP"})

    assert discover_calls == []  # GraphQL map hit なので title 探索は不発
    assert ("358689688709", "PID_MAP", None) in upserts
    assert any(m[1] == "done" for m in marks)
    assert res["result"] in ("no_change", "applied")


def test_process_job_map_miss_falls_back_to_title(monkeypatch):
    """map miss (空 dict) → 既存タイトル/検索語 discover に降格 (method=title)."""
    upserts, marks = _patch_process_job_common(monkeypatch, product=None)

    class _Cur:
        def fetchone(self):
            return {"title": "Some Product Title", "search_keyword": ""}

    class _Conn:
        def execute(self, *a, **k):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db, "get_conn", lambda: _Conn())

    discover_calls: list = []

    def fake_discover(query, expected_itm):
        discover_calls.append((query, expected_itm))
        r = drv.EbaymagResult()
        r.ok = True
        r.product_id = "PID_TITLE"
        return r

    monkeypatch.setattr(drv, "discover_product_id", fake_discover)

    from tasks.task_ebaymag_apply_queue import _process_job
    job = {"id": 8, "ebay_item_id": "358724549446", "attempts": 0}
    res = _process_job(job, {}, {})  # 空 map = miss

    assert discover_calls and discover_calls[0] == ("Some Product Title", "358724549446")
    assert ("358724549446", "PID_TITLE", None) in upserts
    assert res["result"] in ("no_change", "applied")


# ───────── _build_product_map_if_needed ─────────

def test_build_product_map_if_needed_skips_when_all_registered(monkeypatch):
    """全 job が product_id 登録済 → GraphQL を叩かず空 dict."""
    monkeypatch.setattr(db, "get_ebaymag_product", lambda eid: {"product_id": "P"})

    def boom():
        raise AssertionError("fetch_product_map は呼ばれてはならない")

    monkeypatch.setattr(drv, "fetch_product_map", boom)

    from tasks.task_ebaymag_apply_queue import _build_product_map_if_needed
    assert _build_product_map_if_needed([{"ebay_item_id": "X"}]) == {}


def test_build_product_map_if_needed_returns_empty_on_failure(monkeypatch):
    """map 構築失敗 (ok=False) → 空 dict を返し title fallback に委ねる (全滅させない)."""
    monkeypatch.setattr(db, "get_ebaymag_product", lambda eid: None)
    failed = drv.EbaymagResult()
    failed.ok = False
    failed.error = "CDP 不在"
    monkeypatch.setattr(drv, "fetch_product_map", lambda: failed)

    from tasks.task_ebaymag_apply_queue import _build_product_map_if_needed
    assert _build_product_map_if_needed([{"ebay_item_id": "X"}]) == {}


def test_build_product_map_if_needed_returns_map_on_success(monkeypatch):
    """discover 要 job あり + 構築成功 → product_map を返す."""
    monkeypatch.setattr(db, "get_ebaymag_product", lambda eid: None)
    ok = drv.EbaymagResult()
    ok.ok = True
    ok.product_map = {"358689688709": "PA"}
    monkeypatch.setattr(drv, "fetch_product_map", lambda: ok)

    from tasks.task_ebaymag_apply_queue import _build_product_map_if_needed
    assert _build_product_map_if_needed([{"ebay_item_id": "X"}]) == {"358689688709": "PA"}
