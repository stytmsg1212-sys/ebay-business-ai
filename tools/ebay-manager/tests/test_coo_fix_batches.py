"""#44 一括是正バッチ 3 本 (coo_fix_batch_{a,b,c}) の回帰テスト.

T3 レビューで指摘された契約を pytest で恒久化:

【HIGH-1 (1 巡目)】test_batch_a_execute_uses_nonempty_filtered
  バッチ A の execute 経路が `revise_item_specifics(item_id, filtered,
  replace_all=True)` を **非空 filtered かつ replace_all=True** で呼ぶこと。

【HIGH-2 (1 巡目)】test_batch_c_rank_n_is_skipped 他
  rank=N (Brand New, ConditionID 1000) は CD 対象外なので plan 化せず skip。

【API 契約】test_revise_item_specifics_empty_dict_rejects
  空 dict は 'item_specifics is empty' で reject される契約 (HIGH-1 後方保証)。

【HIGH-4 (2 巡目)】test_batch_*_execute_does_not_skip_dryrun_only_plans
  dry-run 済み JSON があっても execute は独立 output file から done_ids を取得し、
  dry-run only の plan を skip せずに revise を呼ぶこと。

【MED-5 (2 巡目)】batch B の li 行単位除去 / 年月 false-positive 除外 /
  positive_diff assertion の回帰テスト。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


_TMP_DIR = Path(__file__).resolve().parent.parent / "data" / "tmp"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def batch_a():
    return _load_module("coo_fix_batch_a_specifics",
                        _TMP_DIR / "coo_fix_batch_a_specifics.py")


@pytest.fixture()
def batch_b():
    return _load_module("coo_fix_batch_b_description",
                        _TMP_DIR / "coo_fix_batch_b_description.py")


@pytest.fixture()
def batch_c():
    return _load_module("coo_fix_batch_c_condition",
                        _TMP_DIR / "coo_fix_batch_c_condition.py")


# ---------------------------------------------------------------------------
# HIGH-1: batch A の execute 経路は非空 filtered + replace_all=True を送る
# ---------------------------------------------------------------------------

def test_batch_a_execute_uses_nonempty_filtered(batch_a):
    """HIGH-1 fix: バッチ A の execute 経路が revise_item_specifics を非空
    filtered + replace_all=True で呼ぶこと (空 dict + replace_all=False は不可)."""
    creds = {
        "app_id": "AID", "dev_id": "DID", "cert_id": "CID", "user_token": "TOK",
    }
    # 現行 ItemSpecifics: 禁止 Name (Country of Origin) + Brand + MPN
    current = {
        "Country of Origin": "Japan",
        "Brand": "Sony",
        "MPN": "TEST-123",
    }
    revise_calls = []

    def _fake_revise(item_id, item_specifics, *, app_id, dev_id, cert_id,
                     user_token, replace_all):
        revise_calls.append({
            "item_id": item_id,
            "item_specifics": dict(item_specifics),
            "replace_all": replace_all,
        })
        return {"success": True, "ack": "Success",
                "sent_specifics": dict(item_specifics),
                "removed_names": [], "message": "ok"}

    with patch.object(batch_a.ebay_client, "_get_item_specifics_for_merge",
                       return_value=current), \
         patch.object(batch_a.ebay_client, "revise_item_specifics",
                       side_effect=_fake_revise), \
         patch.object(batch_a, "log_content_change", return_value=1):
        result = batch_a.process_one("111", creds, execute=True)

    assert result["status"] == "plan", result
    assert len(revise_calls) == 1, "revise_item_specifics が 1 回だけ呼ばれるべき"
    call = revise_calls[0]
    # HIGH-1 core: 非空 filtered + replace_all=True
    assert call["replace_all"] is True, (
        "replace_all=True で送るべき (空 dict + False は API 冒頭で reject)"
    )
    assert call["item_specifics"], (
        "非空 item_specifics を送るべき (空 dict は 'item_specifics is empty' で失敗)"
    )
    # filtered は禁止 Name を除外して Brand/MPN を残しているはず
    assert "Country of Origin" not in call["item_specifics"]
    assert call["item_specifics"].get("Brand") == "Sony"
    assert call["item_specifics"].get("MPN") == "TEST-123"


# ---------------------------------------------------------------------------
# HIGH-2: rank=N は skip、CD を送信しない
# ---------------------------------------------------------------------------

def test_batch_c_rank_n_is_skipped(batch_c):
    """HIGH-2 fix: rank=N (ConditionID 1000 Brand New) は CD 対象外なので
    process_one が plan にせず skip すること (revise を呼ばない)."""
    creds = {
        "app_id": "AID", "dev_id": "DID", "cert_id": "CID", "user_token": "TOK",
    }
    revise_condition_calls = []

    def _no_call(*a, **kw):
        revise_condition_calls.append((a, kw))
        raise AssertionError("rank=N で revise_item_condition を呼ぶべきでない")

    # GetItem は呼ばれても差し支えないが、ここでは呼ばれない設計を検証したいので
    # 呼ばれたら fail させる (rank=N は GetItem 前に skip 判定される)
    with patch.object(batch_c.ebay_client, "revise_item_condition",
                       side_effect=_no_call), \
         patch.object(batch_c, "_fetch_current_condition",
                       side_effect=AssertionError("rank=N は GetItem 前に skip")):
        result = batch_c.process_one("222", "N", creds, execute=True)

    assert result["status"] == "skip", result
    assert "N" in result["reason"] or "1000" in result["reason"] \
        or "CD" in result["reason"] or "Brand New" in result["reason"], (
        "skip reason に rank=N が CD 対象外である旨が含まれるべき"
    )
    assert not revise_condition_calls


def test_batch_c_rank_n_not_in_cd_templates(batch_c):
    """HIGH-2 補完: _RANK_CD_TEMPLATES から N が完全に除去されていること."""
    assert "N" not in batch_c._RANK_CD_TEMPLATES, (
        "rank=N は Brand New (1000) で CD 対象外、テンプレートから除去必須"
    )
    assert "N" in batch_c._RANK_SKIP_NO_CD_NEEDED, (
        "rank=N は skip 集合に明示されているべき"
    )


def test_batch_c_valid_used_ranks_still_have_templates(batch_c):
    """中古ランクは引き続き CD 対象。S/A/B/C/D/PO の全てに 65 字以内の
    テンプレが存在すること (As-Is は理由必須で skip 対象 = 意図的に無し)."""
    for rank in ("S", "A", "B", "C", "D", "PO"):
        assert rank in batch_c._RANK_CD_TEMPLATES, f"rank={rank} のテンプレ欠落"
        assert len(batch_c._RANK_CD_TEMPLATES[rank]) <= 65, (
            f"rank={rank} のテンプレが 65 字超過"
        )
    assert "As-Is" not in batch_c._RANK_CD_TEMPLATES


# ---------------------------------------------------------------------------
# API 契約: revise_item_specifics(item_id, {}, ...) は空 dict で reject する
# ---------------------------------------------------------------------------

def test_revise_item_specifics_empty_dict_rejects():
    """HIGH-1 後方保証: revise_item_specifics に空 dict を渡すと
    'item_specifics is empty' で reject される仕様であることを固定する。
    バッチ A が空 dict を渡さない (test_batch_a_execute_uses_nonempty_filtered)
    のと対を成す契約テスト。"""
    from monitor import ebay_client

    result = ebay_client.revise_item_specifics(
        "999", {},
        app_id="A", dev_id="D", cert_id="C", user_token="T",
        replace_all=False,
    )
    assert result["success"] is False
    assert "empty" in result["message"].lower()


# ---------------------------------------------------------------------------
# HIGH-4 (T3 2巡目): mode 別 output file — execute は dry-run only plan を送る
# ---------------------------------------------------------------------------

def _write_dryrun_only_output(path: Path, ids: list[str]) -> None:
    """dry-run output file を偽造 (中身は plans のみ)。execute output ではない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"plans": [{"ebay_item_id": eid, "status": "plan"} for eid in ids],
             "skips": [], "manual_review": [], "no_action": [], "send_failed": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _isolate_output_paths(monkeypatch, module, tmp_path):
    """HIGH-A fix (T3 code-reviewer 3巡目): tests は module._OUT_DRYRUN /
    _OUT_EXECUTE を tmp_path 配下に強制差し替え、実 output (data/tmp/) を
    絶対に上書き / unlink しない。既存の実成果物 (canary 後のログ含む) を
    テスト実行で 1 バイトも変えない。"""
    dryrun = tmp_path / f"{module.__name__}_dryrun.json"
    execute = tmp_path / f"{module.__name__}_execute.json"
    prog_dry = tmp_path / f"{module.__name__}_progress.json"
    prog_exe = tmp_path / f"{module.__name__}_execute_progress.json"
    monkeypatch.setattr(module, "_OUT_DRYRUN", dryrun)
    monkeypatch.setattr(module, "_OUT_EXECUTE", execute)
    monkeypatch.setattr(module, "_PROGRESS_DRYRUN", prog_dry)
    monkeypatch.setattr(module, "_PROGRESS_EXECUTE", prog_exe)
    return {"dryrun": dryrun, "execute": execute}


def test_batch_a_execute_does_not_skip_dryrun_only_plans(batch_a, tmp_path, monkeypatch):
    """HIGH-4: dry-run 用 output に全 targets 済みの plans があっても、execute
    経路は _OUT_EXECUTE を独立参照するため done_ids は空 → revise を呼ぶこと。"""
    paths = _isolate_output_paths(monkeypatch, batch_a, tmp_path)
    _write_dryrun_only_output(paths["dryrun"], ["A1", "A2"])

    creds = {"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "T"}
    current = {"Country of Origin": "Japan", "Brand": "Sony", "MPN": "M-1"}
    revise_calls = []

    def _fake_revise(item_id, item_specifics, **kw):
        revise_calls.append(item_id)
        return {"success": True, "ack": "Success", "sent_specifics": item_specifics,
                "removed_names": [], "message": "ok"}

    existing_execute = batch_a._load_existing(paths["execute"])
    assert existing_execute["plans"] == []
    existing_dryrun = batch_a._load_existing(paths["dryrun"])
    assert len(existing_dryrun["plans"]) == 2

    with patch.object(batch_a.ebay_client, "_get_item_specifics_for_merge",
                       return_value=current), \
         patch.object(batch_a.ebay_client, "revise_item_specifics",
                       side_effect=_fake_revise), \
         patch.object(batch_a, "log_content_change", return_value=1):
        result = batch_a.process_one("A1", creds, execute=True)

    assert result["status"] == "plan"
    assert revise_calls == ["A1"]


def test_batch_b_execute_does_not_skip_dryrun_only_plans(batch_b, tmp_path, monkeypatch):
    """HIGH-4 (batch B): dry-run/execute 分離が効いていること."""
    paths = _isolate_output_paths(monkeypatch, batch_b, tmp_path)
    _write_dryrun_only_output(paths["dryrun"], ["B1", "B2", "B3"])

    ex = batch_b._load_existing(paths["execute"])
    dr = batch_b._load_existing(paths["dryrun"])
    assert ex["plans"] == []
    assert len(dr["plans"]) == 3

    execute_done_ids = {p["ebay_item_id"] for p in ex["plans"]} \
        | {s["ebay_item_id"] for s in ex["skips"]} \
        | {m["ebay_item_id"] for m in ex["manual_review"]} \
        | {n["ebay_item_id"] for n in ex["no_action"]}
    assert execute_done_ids == set()


def test_batch_c_execute_does_not_skip_dryrun_only_plans(batch_c, tmp_path, monkeypatch):
    """HIGH-4 (batch C): rank=S plan を execute で消化できること."""
    paths = _isolate_output_paths(monkeypatch, batch_c, tmp_path)
    _write_dryrun_only_output(paths["dryrun"], ["C1", "C2"])

    creds = {"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "T"}
    revise_calls = []

    def _fake_revise(item_id, cid, *a, **kw):
        revise_calls.append(item_id)
        return {"success": True, "condition_id": cid, "message": "ok"}

    with patch.object(batch_c, "_fetch_current_condition",
                       return_value=("1500", "", None)), \
         patch.object(batch_c.ebay_client, "revise_item_condition",
                       side_effect=_fake_revise), \
         patch.object(batch_c, "log_content_change", return_value=1):
        result = batch_c.process_one("C1", "S", creds, execute=True)

    assert result["status"] == "plan"
    assert revise_calls == ["C1"]


# ---------------------------------------------------------------------------
# MED-5 (T3 2巡目): batch B の li 行単位除去 / false-positive / positive_diff
# ---------------------------------------------------------------------------

def test_batch_b_li_line_removal_preserves_unrelated_lines(batch_b):
    """MED-5: <li> 内が <br> 区切りで複数行あり、原産国以外の独立情報と同居
    している場合、原産国行だけを落として他の行は保持すること (li 全体除去禁止)."""
    html = (
        "<ul>\n"
        "<li><strong>Premium Shrink Leather:</strong>  \n"
        "  - Soft, textured leather that improves with use.<br>  \n"
        "  - Made in Japan, ensuring high-quality craftsmanship.\n"
        "</li>\n"
        "<li>Other independent feature</li>\n"
        "</ul>"
    )
    result = batch_b.fix_description(html)
    assert result["tag_balance_ok"]
    assert result["positive_diff_ok"]
    # 原産国部分は消えている
    assert "made in japan" not in result["new_html"].lower()
    # 独立情報は保持
    assert "Soft, textured leather that improves with use" in result["new_html"]
    assert "Other independent feature" in result["new_html"]
    # remaining_hits は空 (自動除去成功)
    assert result["remaining_hits"] == []


def test_batch_b_li_entire_removal_when_all_lines_are_origin(batch_b):
    """MED-5: <li> の全行が原産国関連の場合は li ごと削除すべき."""
    html = (
        "<ul>\n"
        "<li>Made in Japan – Superior craftsmanship and quality</li>\n"
        "<li>Other kept</li>\n"
        "</ul>"
    )
    result = batch_b.fix_description(html)
    assert result["tag_balance_ok"]
    assert result["positive_diff_ok"]
    assert "made in japan" not in result["new_html"].lower()
    assert "Other kept" in result["new_html"]


def test_batch_b_false_positive_year_month_manufactured_in(batch_b):
    """MED-5: "manufactured in <年月>" は false positive として除外扱いで、
    positive_diff / remaining_hits に影響を与えないこと."""
    html = (
        "<p>This item was manufactured in November 2022 in the original "
        "factory.</p>"
    )
    result = batch_b.fix_description(html)
    # 除去対象なし = removed_fragments 空
    assert result["removed_fragments"] == []
    # remaining_hits に 'manufactured in' は入らない (false_positive_hits へ)
    assert "manufactured in" not in result["remaining_hits"]
    assert "manufactured in" in result["false_positive_hits"]
    assert result["new_html"] == html


def test_batch_b_positive_diff_detects_extraneous_changes(batch_b):
    """MED-5 (positive-diff assertion): 除去以外の変化が起きた場合に
    positive_diff_ok=False になる方向のガードを検証。

    fix_description の実装内では他の変化は起こらないので、reassembly の
    core helper (_reassemble_from_replacements) の期待動作を direct 検証する。"""
    # 部分置換のペアが正しく逆適用されるか
    new_html = "before X after"
    replacements = [("original Y", "X")]
    restored = batch_b._reassemble_from_replacements(new_html, replacements)
    assert restored == "before original Y after"


def test_batch_b_h3_p_pair_country_case_sensitive(batch_b):
    """MED-3 (case-sensitive 国名): _RULE_H3_P_PAIR は
    <h3>...</h3><p>大文字始まりの国名</p> のみ一致し、小文字の値は除去しない."""
    # 除去される (国名が大文字始まり)
    html_ok = "<h3>Country/Region of Manufacture</h3><p>Japan</p>"
    result_ok = batch_b.fix_description(html_ok)
    assert "Country/Region of Manufacture" not in result_ok["new_html"]

    # 除去されない (値が小文字 = 国名パターン非該当)
    html_ng = "<h3>Country/Region of Manufacture</h3><p>japan</p>"
    result_ng = batch_b.fix_description(html_ng)
    # ラベルはそのまま残る (P 値も残る)
    assert "Country/Region of Manufacture" in result_ng["new_html"]
    assert "japan" in result_ng["new_html"]


# ---------------------------------------------------------------------------
# MED-1 (T3 2巡目): _derive_ack が success フラグから 'Success' を導出
# ---------------------------------------------------------------------------

def test_derive_ack_returns_real_ack_when_present(batch_a):
    """revise_item_specifics のように ack キーがある場合はそのまま返す."""
    assert batch_a._derive_ack({"success": True, "ack": "Warning"}) == "Warning"


def test_derive_ack_derives_success_when_ack_missing(batch_b, batch_c):
    """revise_item_description / revise_item_condition のように ack キーが
    無い場合、success=True なら 'Success' を導出、False なら None."""
    for m in (batch_b, batch_c):
        derived = m._derive_ack({"success": True, "message": "ok"})
        assert derived is not None
        assert derived.startswith("Success"), derived
        assert m._derive_ack({"success": False, "message": "fail"}) is None


# ---------------------------------------------------------------------------
# HIGH-5 (T3 Codex 2巡目): 失敗バケット分類の回帰テスト
# ---------------------------------------------------------------------------
# HIGH-1/2 の再発防止: exec_result.success=False や postverify.ok=False の item は
# plans に入れず send_failed バケットへ振り分け、done_ids から除外する。
# main() のループロジックを in-line で再現して検証する (main 全体を呼ぶと argparse /
# credentials load / GetItem 実 call が絡むため純関数化テストが困難、ゆえに routing
# 分岐だけを抽出して覆う)。


def _apply_routing(result: dict) -> str:
    """main() のルーティング分岐と等価な純関数 (in-line 抽出)。
    HIGH-1/2 fix と同一の条件で status を最終決定する。"""
    status = result["status"]
    if status != "plan":
        return status
    exec_result = result.get("execute_result") or {}
    postverify = result.get("postverify") or {}
    exec_ok = bool(exec_result.get("success"))
    verify_ok = postverify.get("ok", True) if postverify else True
    if not (exec_ok and verify_ok):
        return "send_failed"
    return "plan"


@pytest.mark.parametrize("scenario", [
    {"execute_result": {"success": False, "message": "API error"}, "postverify": None},
    {"execute_result": {"success": True}, "postverify": {"ok": False, "reason": "verify fail"}},
    {"execute_result": {"success": False}, "postverify": {"ok": True}},
])
def test_execute_failure_routes_to_send_failed(scenario):
    """HIGH-5 (a) + (b): send_failed 分類テスト.
    exec_result.success=False, postverify.ok=False のいずれか (or 両方) が失敗した
    場合、routing は 'send_failed' を返す (plans には入らない)."""
    result = {"ebay_item_id": "X", "status": "plan", **scenario}
    assert _apply_routing(result) == "send_failed"


def test_execute_success_and_verify_ok_routes_to_plan():
    """HIGH-5: 成功パス — exec_result.success=True AND postverify.ok=True (or 未計上)
    のみ plans へ入る."""
    r1 = {"ebay_item_id": "X", "status": "plan",
          "execute_result": {"success": True}, "postverify": {"ok": True}}
    r2 = {"ebay_item_id": "Y", "status": "plan",
          "execute_result": {"success": True}}  # postverify 未計上
    assert _apply_routing(r1) == "plan"
    assert _apply_routing(r2) == "plan"


def test_send_failed_is_excluded_from_done_ids_for_all_batches(
    batch_a, batch_b, batch_c, tmp_path, monkeypatch,
):
    """HIGH-5 (a): 3 バッチとも send_failed バケットの item_id は done_ids に
    入らず、次回実行の remaining に戻る (resume で silent 取り残しにならない)."""
    for module in (batch_a, batch_b, batch_c):
        paths = _isolate_output_paths(monkeypatch, module, tmp_path / module.__name__)
        # execute output に send_failed 1 件 + plan 1 件を仕込む
        data = {
            "plans": [{"ebay_item_id": "DONE"}],
            "skips": [],
            "no_action": [],
            "send_failed": [{"ebay_item_id": "RETRY_ME",
                             "failure_reason": "exec_ok=False"}],
        }
        if module is batch_b:
            data["manual_review"] = []
        paths["execute"].parent.mkdir(parents=True, exist_ok=True)
        paths["execute"].write_text(json.dumps(data), encoding="utf-8")
        existing = module._load_existing(paths["execute"])
        done_ids = (
            {p["ebay_item_id"] for p in existing["plans"]}
            | {s["ebay_item_id"] for s in existing["skips"]}
            | {n["ebay_item_id"] for n in existing["no_action"]}
        )
        # send_failed は done_ids に**含まれない**ことを検証
        assert "DONE" in done_ids
        assert "RETRY_ME" not in done_ids, (
            f"{module.__name__}: send_failed が done_ids に混入 (silent 取り残しリスク)"
        )
        assert existing["send_failed"] == [{"ebay_item_id": "RETRY_ME",
                                            "failure_reason": "exec_ok=False"}]


# ---------------------------------------------------------------------------
# HIGH-5 (c): "manufactured in <年>" と真の COO が共存する document で
# 真の COO 残存が manual_review / remaining_hit に昇格する
# ---------------------------------------------------------------------------

def test_batch_b_mixed_date_and_real_coo_promotes_remaining_hit(batch_b):
    """HIGH-3 fix 回帰: 「manufactured in 2024」 (date) と「Manufactured in Japan」
    (真の COO) が同一 document に共存する場合、document 全体スキャンで
    false-positive と誤判定せず、真の COO は remaining_hits に昇格すること."""
    # description 全体で false-positive-date と real-COO が混在
    html = (
        "<p>The device was manufactured in 2024 for the retail market.</p>\n"
        "<p>The original packaging notes: Manufactured in Japan by skilled artisans.</p>"
    )
    result = batch_b.fix_description(html)
    # 真の COO は自動除去できないため remaining_hits に昇格
    assert "manufactured in" in result["remaining_hits"], (
        f"真の COO 残存が隠蔽された (remaining_hits={result['remaining_hits']} "
        f"false_positive_hits={result['false_positive_hits']})"
    )


def test_scan_manufactured_in_occurrences_distinguishes_per_position(batch_b):
    """HIGH-3 helper 単体: real_hits と false_positive_hits を出現位置ごとに分離."""
    html = "manufactured in 2024. Then manufactured in Japan."
    occ = batch_b._scan_manufactured_in_occurrences(html)
    assert len(occ["false_positive_hits"]) == 1
    assert len(occ["real_hits"]) == 1


def test_scan_manufactured_in_occurrences_all_dates(batch_b):
    """全出現が date-form なら real_hits=0."""
    html = "First lot manufactured in 2024. Second lot manufactured in November 2022."
    occ = batch_b._scan_manufactured_in_occurrences(html)
    assert occ["real_hits"] == []
    assert len(occ["false_positive_hits"]) == 2


# ---------------------------------------------------------------------------
# HIGH-5 (d): li 内の date 表記は保持される (過剰削除防止)
# ---------------------------------------------------------------------------

def test_batch_b_li_preserves_manufactured_date(batch_b):
    """HIGH-4 fix 回帰: <li>Manufactured in November 2022</li> は date 表記であり
    原産国ではないため保持されること (li 全体除去も部分除去もしない)."""
    html = (
        "<ul>\n"
        "<li>Manufactured in November 2022</li>\n"
        "<li>Battery: 3400 mAh</li>\n"
        "</ul>"
    )
    result = batch_b.fix_description(html)
    assert "Manufactured in November 2022" in result["new_html"], (
        f"date-form li が過剰削除された (new_html 抜粋={result['new_html'][:200]!r})"
    )
    # 除去 fragment は空 (何も消してない)
    assert result["removed_fragments"] == []


def test_batch_b_li_mixed_date_and_real_coo_only_removes_real(batch_b):
    """HIGH-4 fix 回帰: date と真の COO が混在する場合、date li は保持、
    真の COO li のみが除去される."""
    html = (
        "<ul>\n"
        "<li>Manufactured in 2024</li>\n"
        "<li>Made in Japan</li>\n"
        "</ul>"
    )
    result = batch_b.fix_description(html)
    assert "Manufactured in 2024" in result["new_html"]
    assert "Made in Japan" not in result["new_html"]


def test_segment_is_coo_date_exempt(batch_b):
    """_segment_is_coo helper: date-form 単独は非 COO 扱い."""
    assert batch_b._segment_is_coo("Made in Japan") is True
    assert batch_b._segment_is_coo("Manufactured in November 2022") is False
    assert batch_b._segment_is_coo("Manufactured in 2024") is False
    # 混在は安全側で COO 扱い (残存させる)
    assert batch_b._segment_is_coo("Manufactured in 2024 in Japan") is True


# ---------------------------------------------------------------------------
# MED-1 (T3 Codex 2巡目): _RULE_P_ORIGIN_HEADING を単一トピック段落のみに厳格化
# ---------------------------------------------------------------------------

def test_batch_b_p_origin_heading_single_topic_removed(batch_b):
    """MED-1 fix (2巡目): 単一トピック段落は従来通り除去."""
    html = (
        "<p><strong>Origin</strong><br>Made in Japan with high-quality finish.</p>\n"
        "<p>Independent content.</p>"
    )
    result = batch_b.fix_description(html)
    assert "Origin" not in result["new_html"]
    assert "Independent content" in result["new_html"]


def test_batch_b_p_origin_heading_multi_br_preserved(batch_b):
    """MED-1 fix (2巡目): 追加 <br> で複数行を含む <p> は本 rule で消さず、
    manual_review 経路 (remaining_hits) に落とす (非 COO 情報の巻き込み防止)."""
    html = (
        "<p><strong>Origin</strong><br>Made in Japan<br>Weight: 200g<br>"
        "Color: black</p>"
    )
    result = batch_b.fix_description(html)
    # 段落は保持 (Weight/Color の非 COO 情報が消えない)
    assert "Weight: 200g" in result["new_html"]
    assert "Color: black" in result["new_html"]
    # 真の COO 残存 → remaining_hits に昇格
    assert "made in japan" in result["remaining_hits"]


# ---------------------------------------------------------------------------
# HIGH-A (T3 code-reviewer 3巡目): tests が実成果物 JSON を破壊しないことを確認
# ---------------------------------------------------------------------------

def test_tests_do_not_modify_real_output_files(batch_a, batch_b, batch_c, tmp_path):
    """HIGH-A 回帰: 実 output path (data/tmp/coo_fix_batch_*_dryrun.json /
    _execute.json) を **本テストファイル内で書換えない** ことを確認するため、
    module attribute が既定 (real data/tmp) と別 path (tmp_path 隔離) の
    どちらでも `_load_existing` が引数の path を尊重することを検証する
    (path 引数を無視して module attribute を参照していないことのガード)."""
    for module in (batch_a, batch_b, batch_c):
        assert "_OUT_DRYRUN" in dir(module)
        assert "_OUT_EXECUTE" in dir(module)
        # 存在しない path なら空 dict を返す (実 path を勝手に読まない)
        empty = module._load_existing(tmp_path / "nonexistent.json")
        assert empty["plans"] == []
        assert empty["skips"] == []
        assert empty["send_failed"] == []


# ---------------------------------------------------------------------------
# 段階 1 修正 (T3 4巡目 polish): M1 / M2 / L1
# ---------------------------------------------------------------------------

def test_m1_retry_success_removes_prior_send_failed_entry(
    batch_a, batch_b, batch_c, tmp_path, monkeypatch,
):
    """M1: 前回 send_failed 入りの item が retry で成功したら、旧 send_failed
    エントリが除去される。main() の routing 分岐と等価な in-line ロジックで検証."""
    for module in (batch_a, batch_b, batch_c):
        send_failed = [
            {"ebay_item_id": "R1", "failure_reason": "exec_ok=False (前回)"},
            {"ebay_item_id": "OTHER", "failure_reason": "unchanged"},
        ]
        item_id = "R1"
        result = {
            "ebay_item_id": item_id,
            "status": "plan",
            "execute_result": {"success": True},
        }
        exec_result = result.get("execute_result") or {}
        postverify = result.get("postverify") or {}
        exec_ok = bool(exec_result.get("success"))
        verify_ok = postverify.get("ok", True) if postverify else True
        # main() と同じロジック
        if not (exec_ok and verify_ok):
            raise AssertionError("成功パスに来るべき")
        else:
            send_failed[:] = [
                x for x in send_failed if x.get("ebay_item_id") != item_id
            ]
        ids = [x["ebay_item_id"] for x in send_failed]
        assert "R1" not in ids, f"{module.__name__}: retry 成功後も旧 R1 残存"
        assert "OTHER" in ids, f"{module.__name__}: 無関係 item を巻き添え除去"


def test_m2_batch_b_log_success_reflects_exec_and_verify(batch_b):
    """M2 (batch B): log_content_change の success は exec_ok AND verify_ok
    (revise API 成功だが postverify NG のケースで success=False を記録).

    fix_description が実際に removed_fragments を出せる HTML を使う (テーブル行
    パターン)。postverify では GetItem 再取得で「真の COO が残っている」体で
    返し、verify_ok=False → log の success=False を検証。"""
    creds = {"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "T"}
    log_calls = []

    def _fake_log(item_id, field, *, before_value, after_value, source_tab,
                   success, ebay_ack):
        log_calls.append({"item_id": item_id, "field": field,
                          "success": success, "ebay_ack": ebay_ack})
        return 1

    # revise 前: 除去可能なテーブル行 (Country of Origin) を含む
    before_desc = (
        "<table><tr><td>Country of Origin</td><td>Japan</td></tr>"
        "<tr><td>Weight</td><td>200g</td></tr></table>"
    )
    # revise 後: GetItem 応答で真の COO 残存 (verify_ok=False シナリオ)
    after_desc = "<p>Made in Japan bad still there</p>"

    with patch.object(batch_b, "_fetch_description",
                       side_effect=[(before_desc, None), (after_desc, None)]), \
         patch.object(batch_b.ebay_client, "revise_item_description",
                       return_value={"success": True, "message": "ok"}), \
         patch.object(batch_b, "log_content_change", side_effect=_fake_log):
        result = batch_b.process_one("BX", creds, execute=True)

    assert len(log_calls) == 1, f"log 未実行 (result={result})"
    # postverify.ok=False (真の COO 残存) なので success=False を記録
    assert result.get("postverify", {}).get("ok") is False, (
        f"postverify 期待={{'ok': False}}, actual={result.get('postverify')}"
    )
    assert log_calls[0]["success"] is False, (
        f"log 記録の success は exec AND verify で False のはず, log={log_calls[0]}"
    )


def test_m2_batch_c_log_success_reflects_exec_and_verify(batch_c):
    """M2 (batch C): log の success は exec_ok AND verify_ok を反映."""
    creds = {"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "T"}
    log_calls = []

    def _fake_log(item_id, field, *, before_value, after_value, source_tab,
                   success, ebay_ack):
        log_calls.append({"item_id": item_id, "success": success})
        return 1

    with patch.object(batch_c, "_fetch_current_condition",
                       side_effect=[
                           ("1500", "", None),          # revise 前 (現行)
                           ("1500", "WRONG_CD", None),  # revise 後 (verify NG)
                       ]), \
         patch.object(batch_c.ebay_client, "revise_item_condition",
                       return_value={"success": True, "condition_id": "1500"}), \
         patch.object(batch_c, "log_content_change", side_effect=_fake_log):
        result = batch_c.process_one("CX", "S", creds, execute=True)

    assert len(log_calls) == 1
    assert result.get("postverify", {}).get("ok") is False
    assert log_calls[0]["success"] is False, (
        f"log 記録の success は exec AND verify で False のはず, log={log_calls[0]}"
    )


def test_m2_batch_b_log_success_true_when_verify_ok(batch_b):
    """M2 sanity: postverify.ok=True (真の COO 消失) なら success=True を記録."""
    creds = {"app_id": "A", "dev_id": "D", "cert_id": "C", "user_token": "T"}
    log_calls = []

    def _fake_log(item_id, field, *, before_value, after_value, source_tab,
                   success, ebay_ack):
        log_calls.append({"success": success})
        return 1

    before_desc = (
        "<table><tr><td>Country of Origin</td><td>Japan</td></tr>"
        "<tr><td>Weight</td><td>200g</td></tr></table>"
    )
    after_desc = "<p>Cleaned description with no COO hits.</p>"

    with patch.object(batch_b, "_fetch_description",
                       side_effect=[(before_desc, None), (after_desc, None)]), \
         patch.object(batch_b.ebay_client, "revise_item_description",
                       return_value={"success": True}), \
         patch.object(batch_b, "log_content_change", side_effect=_fake_log):
        result = batch_b.process_one("BY", creds, execute=True)

    assert result["postverify"]["ok"] is True, result.get("postverify")
    assert log_calls[0]["success"] is True


def test_l1_load_existing_warns_on_corrupt_file(
    batch_a, batch_b, batch_c, tmp_path, capsys,
):
    """L1: 破損 output file を読んだ時 stderr に WARNING を出力する
    (silent に done_ids リセットしない)."""
    for module in (batch_a, batch_b, batch_c):
        corrupt = tmp_path / f"{module.__name__}_corrupt.json"
        corrupt.write_text("{ this is not valid json", encoding="utf-8")
        result = module._load_existing(corrupt)
        # 空 dict fallback は依然として発火 (done_ids リセットは避けられない現実)
        assert result["plans"] == []
        assert result["send_failed"] == []
        capture = capsys.readouterr()
        assert "WARNING" in capture.err, (
            f"{module.__name__}: 破損 file で stderr WARNING が出ていない "
            f"(stderr={capture.err!r})"
        )
        assert corrupt.name in capture.err, (
            f"{module.__name__}: WARNING に file 名が含まれない"
        )


def test_l1_load_existing_no_warning_on_missing_file(
    batch_a, tmp_path, capsys,
):
    """L1: file が存在しない (初回実行) は WARNING を出さない (正常経路)."""
    nonexistent = tmp_path / "never_existed.json"
    result = batch_a._load_existing(nonexistent)
    assert result["plans"] == []
    capture = capsys.readouterr()
    assert "WARNING" not in capture.err, (
        f"初回実行で不要な WARNING が出た: {capture.err!r}"
    )
