"""依頼ボード#12 回帰テスト — 採用後フォローアップ「後でやる」両押下時の行き先通知。

2026-06-12: 復活候補 3 件を採用 → 写真/desc とも「いいえ、後でやる」押下で
フォローアップ欄ごと無言で消え「商品が全消失した」ように見えた。
修正 = 両側非アクティブで欄が消えるタイミングに `_sup_followup_later_notice` を
queue し、次 rerun 冒頭で pop → st.info 表示 (1 回限り)。

判定ロジックは tab_supplier_candidates.py の handler 内 inline (2 箇所利用 =
3 回ルール未満で共通化せず)。本テストは session_state を dict で模擬し、
queue 判定と pop-once セマンティクスを検証する。
"""
from __future__ import annotations


def _photo_no_handler(ss: dict, cid: int, title: str = "T", eid: str = "E") -> None:
    """写真側「いいえ、後でやる」handler 相当 (tab_supplier_candidates.py)。"""
    ss[f"_sup_photo_prompt_{cid}"] = False
    if not ss.get(f"_sup_desc_prompt_{cid}") and not ss.get(
        f"_sup_desc_open_inline_{cid}"
    ):
        ss.setdefault("_sup_followup_later_notice", []).append(
            {"title": title, "eid": eid}
        )


def _desc_no_handler(ss: dict, cid: int, title: str = "T", eid: str = "E") -> None:
    """desc 側「いいえ、後でやる」handler 相当。"""
    ss[f"_sup_desc_prompt_{cid}"] = False
    if not ss.get(f"_sup_photo_prompt_{cid}") and not ss.get(
        f"_sup_photo_open_inline_{cid}"
    ):
        ss.setdefault("_sup_followup_later_notice", []).append(
            {"title": title, "eid": eid}
        )


def test_photo_no_queues_notice_when_desc_inactive():
    """desc 側非アクティブ → 写真「後でやる」で通知 queue (欄が消えるケース)。"""
    ss = {"_sup_photo_prompt_5": True}
    _photo_no_handler(ss, 5)
    assert ss["_sup_followup_later_notice"] == [{"title": "T", "eid": "E"}]


def test_photo_no_does_not_queue_when_desc_prompt_active():
    """desc prompt がまだ出ている → 欄は残るので通知しない。"""
    ss = {"_sup_photo_prompt_5": True, "_sup_desc_prompt_5": True}
    _photo_no_handler(ss, 5)
    assert "_sup_followup_later_notice" not in ss


def test_photo_no_does_not_queue_when_desc_inline_open():
    """desc inline 展開中 (はい押下後) → 欄は残るので通知しない。"""
    ss = {"_sup_photo_prompt_5": True, "_sup_desc_open_inline_5": True}
    _photo_no_handler(ss, 5)
    assert "_sup_followup_later_notice" not in ss


def test_desc_no_after_photo_no_queues_once():
    """user 実シナリオ: 写真「後でやる」→ desc「後でやる」の順で通知 1 件。"""
    ss = {"_sup_photo_prompt_5": True, "_sup_desc_prompt_5": True}
    _photo_no_handler(ss, 5)  # desc 側まだ active → queue されない
    assert "_sup_followup_later_notice" not in ss
    _desc_no_handler(ss, 5)  # photo 側既に非アクティブ → queue
    assert len(ss["_sup_followup_later_notice"]) == 1


def test_notice_popped_once():
    """pop 消費型 = 2 回目の rerun では空 (1 回表示保証 + リークなし)。"""
    ss = {"_sup_followup_later_notice": [{"title": "T", "eid": "E"}]}
    first = ss.pop("_sup_followup_later_notice", [])
    assert first == [{"title": "T", "eid": "E"}]
    second = ss.pop("_sup_followup_later_notice", [])
    assert second == []


def test_multiple_cids_accumulate():
    """複数商品を連続採用 → 後でやる × N でも通知が商品ごとに積まれる。"""
    ss = {"_sup_photo_prompt_1": True, "_sup_photo_prompt_2": True}
    _photo_no_handler(ss, 1, title="A", eid="111")
    _photo_no_handler(ss, 2, title="B", eid="222")
    assert [n["eid"] for n in ss["_sup_followup_later_notice"]] == ["111", "222"]
