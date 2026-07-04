#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#44 バッチB (dry-run 構築): description 内の原産国句除去 (対象 120件)

description_hits を持つ対象について GetItem (C1修正済み builder) で現行
description (HTML) を取得し、原産国句を **行/要素単位でピンポイント除去**
する (全文再生成しない)。実際の description を調査した結果、原産国記載は
概ね以下 5 パターンに収束することを確認済み (data/tmp/coo_scan_result_*.json
の description_hits 内訳を目視確認):
  R1) テーブル行:      <tr><td>Country of Origin</td><td>Japan</td></tr>
  R2) 箇条書き:        <li><strong>Country of Origin:</strong> Japan</li>
                       <li>Manufactured in Japan</li>
  R3) 全角コロン段落:  <p>Country/Region of Manufacture：Japan</p>
  R4) 見出し+値の対:   <h3>Country/Region of Manufacture</h3> <p>Japan</p>
  R5) 中黒+<br>区切り: ・Country of Manufacture: Japan<br>
      (同じ行内の他の項目 ・Manufacturer: ... <br> 等は残す。除去対象外)

R1-R5 のいずれにも一致しない残存箇所 (地の文に埋め込まれた "made in japan" /
"manufactured in" 等、例: "...from BIG Daishowa, manufactured in Japan and
designed for...") は、文の境界検出が機械的に安全とは言えないため **自動除去せず
manual review へ回す** (K2 Surgical / Q0: 無理に自動化して文意を壊さない)。

"manufactured in <年/月>" (例: "manufactured in 2024", "manufactured in
November 2022") は原産国ではなく製造年月への言及であり、そもそも規約違反ではない
**false positive** として除外する (スキャン側の正規表現が year/month も拾って
しまうため)。

除去後は HTML タグバランス (li/p/tr/td/h3/div/ul/table/strong/span/section の
開閉数の増減が一致するか) を検証してから dry-run 出力する。タグバランスが崩れた
場合は自動修正を中止し skip へ回す (K2: 壊れる可能性がある変更は適用しない)。

**eBay への書込 (ReviseItem 実送信) は既定で一切行わない。**
--execute 指定時のみ ebay_client.revise_item_description で実送信する
(本タスクでは --execute は使用しない。実行は別途 canary 手順で行う)。

入力:
  data/tmp/coo_scan_result_2026_07_04.json (description_hits 非空の対象を抽出)
出力:
  data/tmp/coo_fix_batch_b_dryrun.json   (plans / skips / manual_review / no_action)
  data/tmp/coo_fix_batch_b_progress.json (50件ごとの進捗スナップショット)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.credentials import get_ebay_credentials  # noqa: E402
from monitor import ebay_client  # noqa: E402
from monitor.listing_content_change_log import log_content_change  # noqa: E402

_INPUT = _ROOT / "data" / "tmp" / "coo_scan_result_2026_07_04.json"
# HIGH-4 fix: mode 別 output file (batch A と同じ設計)
_OUT_DRYRUN = _ROOT / "data" / "tmp" / "coo_fix_batch_b_dryrun.json"
_OUT_EXECUTE = _ROOT / "data" / "tmp" / "coo_fix_batch_b_execute.json"
_PROGRESS_DRYRUN = _ROOT / "data" / "tmp" / "coo_fix_batch_b_progress.json"
_PROGRESS_EXECUTE = _ROOT / "data" / "tmp" / "coo_fix_batch_b_execute_progress.json"

_THROTTLE_SEC = 0.5
_NS = {"n": "urn:ebay:apis:eBLBaseComponents"}

# =========================================================================
# 除去ルール (R1-R5)
# =========================================================================

_LABELS_ALT = (
    r"(?:Country of Origin|Country of Manufacture|Country/Region of Manufacture)"
)

_RULE_TABLE_ROW = re.compile(
    rf"<tr>\s*<td>\s*{_LABELS_ALT}\s*</td>\s*<td>[^<]*</td>\s*</tr>",
    re.IGNORECASE,
)
# <li>...</li> の除去 (行単位)。ラベル明記 (Country of X / Manufactured in)
# だけでなく「Made in Japan/China」の地の文言及 (先頭に限らずli内のどこでも) も
# 対象にする。実データ確認 (2026-07-04) で <li> 内が <br> で複数行に分かれ、
# 原産国以外の独立した情報 (例: "Soft, textured leather that improves with
# use.<br>Made in Japan, ensuring high-quality craftsmanship.") が同居する
# ケースを確認したため、**li 全体を即消去せず <br> 区切りの行単位で判定**し、
# 原産国に触れる行だけを落として残りの行は保持する (K2 Surgical — 無関係な
# 販促文言を巻き込んで消さない)。全行が原産国関連の場合のみ li ごと消える。
_LI_LABEL_RE = re.compile(
    rf"{_LABELS_ALT}|Manufactured in|Made in Japan|Made in China",
    re.IGNORECASE,
)
_LI_TRIGGER = re.compile(
    rf"<li>((?:(?!</li>).)*?"
    rf"(?:{_LABELS_ALT}|Manufactured in|Made in Japan|Made in China)"
    rf"(?:(?!</li>).)*?)</li>",
    re.IGNORECASE | re.DOTALL,
)


# 安全側検出用: bare "in Japan/China" 等の追加 COO 標識 (label は無くても地の文で
# 原産国を示す)。_segment_is_coo の date-form 例外の後、この pattern が同 segment に
# 残っていれば「まだ COO 言及あり」= 保持しない (COO として扱う)。
_EXTRA_COO_SIGNAL = re.compile(
    r"\bin\s+(?:Japan|China|Korea|Taiwan|Vietnam|Thailand|Germany|France|"
    r"Italy|United States|USA|UK|Netherlands|Denmark|Sweden|Switzerland|Hungary)\b",
    re.IGNORECASE,
)


def _segment_is_coo(seg: str) -> bool:
    """1 segment (li 内の <br> 区切り 1 行) が原産国言及に該当するか判定する.

    HIGH-4 fix (T3 Codex): _FALSE_POSITIVE_DATE (「manufactured in <年月>」) は
    製造年月表記であり原産国ではない。docstring と実装の乖離を根治するため、
    "manufactured in" にマッチしても、当該出現位置が date pattern に前方一致
    するなら「非 COO」= 保持対象とする。

    ただし混在 (「Manufactured in 2024 in Japan」のように date と bare "in Japan"
    が同居) は安全側で COO 扱い = 保持しない (残存させる → li 部分除去に回す)。
    """
    label_m = _LI_LABEL_RE.search(seg)
    if not label_m:
        return False
    matched_text = label_m.group(0).lower()
    if matched_text.startswith("manufactured in"):
        anchored = _FALSE_POSITIVE_DATE.match(seg, label_m.start())
        if anchored is not None:
            remainder = seg[:label_m.start()] + seg[anchored.end():]
            # 他に COO ラベル (_LI_LABEL_RE) or bare "in <国名>" が残っていなければ
            # 純粋な date 表記 → 非 COO
            if not (_LI_LABEL_RE.search(remainder)
                    or _EXTRA_COO_SIGNAL.search(remainder)):
                return False
    return True


def _strip_li_inner(inner: str) -> str | None:
    """<li> の中身を <br> 区切りの行単位で判定し、原産国関連行だけを除去する。

    Returns:
        None: 全行が対象 → li ごと削除すべき
        inner (変更なし): トリガー箇所が判定不能 (呼出側で remaining_hits 検出に委ねる)
        新しい inner: 一部の行だけ除去した結果
    """
    parts = re.split(r"(<br\s*/?>)", inner, flags=re.IGNORECASE)
    segments = parts[0::2]
    if len(segments) <= 1:
        # <br> 区切りなし = 単一トピックの1行として扱い、全体を対象とする
        return None if _segment_is_coo(inner) else inner
    keep = [not _segment_is_coo(seg) for seg in segments]
    if not any(keep):
        return None
    if all(keep):
        return inner
    kept_segments = [seg for seg, k in zip(segments, keep) if k]
    return "<br>".join(kept_segments)


def _li_replacer(removed_log: list, replacements: list) -> "callable":
    """MED-B-1 fix: 除去 (置換) の完全な (before, after) ペアを replacements に
    記録する — positive reassembly assertion 用に unshortened で保持する。
    removed_log は人間可読な displays 用 (先頭200字 truncate)。"""
    def _replace(m: re.Match) -> str:
        before = m.group(0)
        inner = m.group(1)
        new_inner = _strip_li_inner(inner)
        if new_inner is None:
            removed_log.append(f"[li全体除去] {before[:200]}")
            replacements.append((before, ""))
            return ""
        if new_inner == inner:
            return before  # 変更なし (remaining_hits で検出させる)
        after = f"<li>{new_inner}</li>"
        removed_log.append(f"[li部分除去] before={before[:200]} | after={after[:200]}")
        replacements.append((before, after))
        return after
    return _replace
_RULE_P_FULLWIDTH = re.compile(
    rf"<p>\s*{_LABELS_ALT}\s*：\s*[^<]*</p>",
    re.IGNORECASE,
)
# MED-B fix (T3 review): 直後 <p> の値が国名パターンに一致する時のみ除去に厳格化。
# 従来は <h3>Country of Origin</h3><p>...</p> の直後 <p> を無条件に巻き込んでいたが、
# 「値が国名でない場合」 (誤解釈による巻き込み) を防ぐ。国名は英語表記の1-2語 (Japan
# / United States / South Korea 等) を許容し、任意の前置装飾 (改行/空白) は許可、
# 制御文字/HTML tag が入る形は除外。
#
# MED-3 fix (T3 2巡目): 全体 re.IGNORECASE 下では国名パターンの「大文字始まり」意図が
# 無効化される (小文字 "japan" 等も一致してしまう)。国名部分だけを `(?-i:...)` inline
# case-sensitivity override で厳格化し、ラベル部分 (Country of Origin 等) は
# 大文字小文字許容のまま維持する。
_COUNTRY_VALUE_RE = (
    r"(?-i:[A-Z][A-Za-z]+(?:[\s\-'][A-Z][A-Za-z]+)*)"  # 大文字始まり複数語 (case-sensitive)
)
_RULE_H3_P_PAIR = re.compile(
    rf"<h3>\s*{_LABELS_ALT}\s*</h3>\s*<p>\s*{_COUNTRY_VALUE_RE}\s*</p>",
    re.IGNORECASE,
)
_RULE_BULLET_BR = re.compile(
    rf"・\s*{_LABELS_ALT}\s*:\s*[^<]*<br\s*/?>\s*",
    re.IGNORECASE,
)
# <p><strong>Origin</strong><br> Made in Japan ...</p> 単一トピック段落
# (見出しが "Origin" 単独、他情報と同居しないことを lookahead で確認してから
# 段落全体を除去する)。
#
# MED-1 fix (T3 Codex): 従来の lookahead は「made in の存在」だけを確認しており、
# <p><strong>Origin</strong><br>Made in Japan<br>Weight: 200g<br>Color: black</p>
# のような複数 <br> 行を含む段落まで丸ごと消してしまう。**単一トピック段落**
# (見出し直後の 1 <br> だけで <p> が閉じるもの) に厳格化する。複数行を含む
# 段落は本 rule で touch せず、後段の `<li>` 行単位判定に相当する処理が
# 別 rule で拾わなければ manual_review へ落ちる (安全側)。
_RULE_P_ORIGIN_HEADING = re.compile(
    r"<p>\s*<strong>\s*Origin\s*</strong>\s*<br\s*/?>\s*"
    r"(?-i:[Mm])ade in [^<]*</p>\s*",
    re.IGNORECASE | re.DOTALL,
)

# 単純な正規表現 sub で丸ごと除去してよいルール (li は行単位判定が必要なため別扱い)
_SIMPLE_RULES = (
    _RULE_TABLE_ROW, _RULE_P_ORIGIN_HEADING, _RULE_P_FULLWIDTH,
    _RULE_H3_P_PAIR, _RULE_BULLET_BR,
)

# coo_scan.py と同一の走査パターン (残存確認用)
_DESC_SCAN_PATTERNS = [
    "country of origin", "country/region of manufacture", "country of manufacture",
    "made in japan", "made in china", "manufactured in",
]

# "manufactured in <year/month>" は原産国ではなく製造年月表記 (false positive)
_FALSE_POSITIVE_DATE = re.compile(
    r"manufactured in\s*(?:<[^>]+>\s*)*"
    r"(?:(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+)?(?:<[^>]+>\s*)*(?:19|20)\d{2}\b",
    re.IGNORECASE,
)

# HIGH-3 fix (T3 Codex): "manufactured in" は「manufactured in Japan」 (真の COO) と
# 「manufactured in November 2022」 (単なる製造年月 = false positive) が混在し得る。
# 従来は _FALSE_POSITIVE_DATE.search(html) と document 全体走査で「date が 1 つでも
# あれば全て false positive」と誤判定していた → 真の COO 残存が隠蔽されていた。
# 本 helper は "manufactured in" の**出現箇所ごと**に後続文字列を個別チェックし、
# date にマッチしない出現が 1 つでもあれば「real hit あり」を返す。
_MANUFACTURED_IN_RE = re.compile(r"manufactured in", re.IGNORECASE)


def _scan_manufactured_in_occurrences(html: str) -> dict:
    """"manufactured in" の全出現箇所を分類する.

    Returns:
        {
          'real_hits': list[dict],     # date でない出現 (真の COO 残存)
          'false_positive_hits': list[dict],  # date に続く出現 (製造年月)
        }
        各要素は {'start': int, 'context': str (前後 80 字)}。
    """
    real: list[dict] = []
    fp: list[dict] = []
    for m in _MANUFACTURED_IN_RE.finditer(html):
        # 出現位置から `_FALSE_POSITIVE_DATE` を anchor match で試す
        window_start = m.start()
        # _FALSE_POSITIVE_DATE は "manufactured in ..." で始まるので同じ start から
        # match() を試みる (前方一致)
        dm = _FALSE_POSITIVE_DATE.match(html, window_start)
        ctx_l = max(0, window_start - 40)
        ctx_r = min(len(html), window_start + 120)
        rec = {"start": window_start, "context": html[ctx_l:ctx_r]}
        if dm is not None:
            fp.append(rec)
        else:
            real.append(rec)
    return {"real_hits": real, "false_positive_hits": fp}

_TAG_BALANCE_NAMES = [
    "li", "p", "tr", "td", "h3", "div", "ul", "table", "strong", "span", "section",
]


def _tag_counts(html: str) -> dict:
    counts = {}
    for name in _TAG_BALANCE_NAMES:
        opens = len(re.findall(rf"<{name}\b[^>]*>", html, re.IGNORECASE))
        closes = len(re.findall(rf"</{name}\s*>", html, re.IGNORECASE))
        counts[name] = (opens, closes)
    return counts


def _scan_remaining(html: str) -> list[str]:
    lowered = html.lower()
    return [pat for pat in _DESC_SCAN_PATTERNS if pat in lowered]


def _reassemble_from_replacements(
    new_html: str, replacements: list[tuple[str, str]],
) -> str:
    """MED-B-1: new_html に対して (before, after) の逆置換を全て順不同で 1 回ずつ
    適用し、元の old_html を復元する。matches が multiple sub で collapse された
    順序の逆順で戻すため、each replacement を1回だけ (最初の一致位置で) 逆適用
    する。順序変動吸収のため各 after 文字列で先頭一致を探し失敗なら next。
    """
    result = new_html
    # after の長い順に処理 (短い ""=削除は最後にすると位置がずれるので先に処理)
    # "" (削除)   : new_html には残っていない → その場所を特定できない
    #               → 代替として old_html を再構築せず「新旧の diff サイズ差」で
    #               検証する方針にする (下記 asserion)
    # str -> str  : 各 after を before に戻す
    for before, after in replacements:
        if after == "":
            # 削除は空文字列に置換したので位置情報が失われている。
            # ここでは何もせず、呼出側の「削除断片の全長合計 == old-new の長さ差」
            # で fold して検証する。
            continue
        idx = result.find(after)
        if idx < 0:
            # 他 rule で連鎖破壊された可能性 (通常発生しない)
            return None  # type: ignore[return-value]
        result = result[:idx] + before + result[idx + len(after):]
    return result


def fix_description(html: str) -> dict:
    """1件の description HTML から原産国句を除去する (R1-R5 + li行単位判定 適用)。

    Returns:
        {
          'new_html': str,
          'removed_fragments': list[str],   # 実際に除去した断片 (先頭200字)
          'remaining_hits': list[str],      # 自動除去できず残存 (manual review 要)
          'false_positive_hits': list[str], # manufactured in <年月> 等
          'tag_balance_ok': bool,
          'positive_diff_ok': bool,         # MED-B-1: 除去対象以外1バイト不変
          'diff_report': dict,              # positive_diff_ok=False 時の詳細
        }
    """
    before_counts = _tag_counts(html)
    new_html = html
    removed_fragments: list[str] = []
    # (before_full, after_full) を全て記録 (MED-B-1 reassembly 検証用)
    replacements: list[tuple[str, str]] = []

    for rule in _SIMPLE_RULES:
        for m in rule.finditer(new_html):
            removed_fragments.append(m.group(0)[:200])
            replacements.append((m.group(0), ""))
        new_html = rule.sub("", new_html)

    # <li> は行 (<br> 区切り) 単位で判定 (単純 sub ではなく callback 経由)
    li_removed: list[str] = []
    new_html = _LI_TRIGGER.sub(_li_replacer(li_removed, replacements), new_html)
    removed_fragments.extend(f[:200] for f in li_removed)

    after_counts = _tag_counts(new_html)
    tag_balance_ok = True
    for name in _TAG_BALANCE_NAMES:
        b_open, b_close = before_counts[name]
        a_open, a_close = after_counts[name]
        if (b_open - a_open) != (b_close - a_close):
            tag_balance_ok = False

    remaining_raw = _scan_remaining(new_html)
    remaining_hits: list[str] = []
    false_positive_hits: list[str] = []
    for pat in remaining_raw:
        if pat == "manufactured in":
            # HIGH-3 fix (T3 Codex): 出現箇所ごとに判定。date でない出現が 1 つでも
            # 残れば remaining_hits に昇格 (真の COO 残存を隠蔽しない)。
            occ = _scan_manufactured_in_occurrences(new_html)
            if occ["real_hits"]:
                remaining_hits.append(pat)
            elif occ["false_positive_hits"]:
                false_positive_hits.append(pat)
            continue
        remaining_hits.append(pat)

    # MED-B-1: positive reassembly assertion.
    # 1) 部分置換 (li の一部行除去) は before/after ペアで逆適用 → old と一致確認
    # 2) 完全削除 (after="") は逆適用不能なので、削除文字列長の合計 == old-new の
    #    長さ差 で fold 検証
    partial_reps = [(b, a) for b, a in replacements if a != ""]
    full_deletes = [b for b, a in replacements if a == ""]
    # 部分置換だけを逆適用した new_html
    step1 = _reassemble_from_replacements(new_html, partial_reps)
    if step1 is None:
        positive_diff_ok = False
        diff_report = {"phase": "step1_reverse_partial_repl", "err": "not_found"}
    else:
        # step1 に完全削除文字列を再挿入する必要はないが、長さ整合を検証
        expected_len_after_add = len(step1) + sum(len(b) for b in full_deletes)
        if expected_len_after_add == len(html):
            # 総和が一致し、部分置換の逆適用でも old_html と部分同一が確認できれば OK
            # step1 は full_deletes を戻し切っていない (位置不明) ため直接 == 不可
            # → 代替として: (a) old_html 内に全 full_delete 断片が実在すること
            #             (b) len 整合 (上記)
            # の 2 条件成立なら「除去以外 1 バイト不変」と結論できる
            missing = [b for b in full_deletes if b not in html]
            if missing:
                positive_diff_ok = False
                diff_report = {
                    "phase": "full_delete_source_missing",
                    "missing_count": len(missing),
                }
            else:
                positive_diff_ok = True
                diff_report = {
                    "phase": "ok",
                    "partial_repl_count": len(partial_reps),
                    "full_delete_count": len(full_deletes),
                    "len_diff": len(html) - len(new_html),
                }
        else:
            positive_diff_ok = False
            diff_report = {
                "phase": "length_mismatch",
                "expected_add": expected_len_after_add,
                "actual_old_len": len(html),
                "new_len": len(new_html),
            }

    return {
        "new_html": new_html,
        "removed_fragments": removed_fragments,
        "remaining_hits": remaining_hits,
        "false_positive_hits": false_positive_hits,
        "tag_balance_ok": tag_balance_ok,
        "positive_diff_ok": positive_diff_ok,
        "diff_report": diff_report,
    }


# =========================================================================
# GetItem 取得
# =========================================================================

def _load_targets() -> list[str]:
    data = json.loads(_INPUT.read_text(encoding="utf-8"))
    return [d["ebay_item_id"] for d in data if d.get("description_hits")]


def _load_existing(out_path: Path) -> dict:
    if out_path.exists():
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # L1 fix (T3 4巡目): 破損 file を silent に落とすと done_ids リセット
            # で全件再送になる。stderr 1 行 warning で異常検知可能に。
            print(
                f"WARNING: _load_existing: {out_path.name} 読込失敗 "
                f"({type(e).__name__}: {e}) → 空 dict で継続 (done_ids リセット)",
                file=sys.stderr, flush=True,
            )
    # HIGH-1 fix (T3 Codex): send_failed バケット (execute 失敗 / postverify NG 用)
    return {"plans": [], "skips": [], "manual_review": [], "no_action": [],
            "send_failed": []}


def _derive_ack(exec_result: dict) -> str | None:
    """MED-1 fix (T3 2巡目): revise_item_description は _call_trading_api を
    経由せず自前で XML parse する実装のため、返却 dict に 'ack' キーが**無い**。
    success フラグから 'Success' を導出する (source_tab から導出値と識別可能)。"""
    ack = exec_result.get("ack")
    if ack:
        return ack
    if exec_result.get("success"):
        return "Success (derived from success flag; revise_item_description dict has no 'ack' key)"
    return None


def _fetch_description(item_id: str, creds: dict) -> tuple[str | None, str | None]:
    """Returns (description_html, error). description が空文字列の場合も
    error=None で返す (取得成功したが空、と通信失敗を区別する)。"""
    app, dev, cert, tok = (
        creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
    )
    result = ebay_client._call_trading_api(
        "GetItem", ebay_client._build_get_item_xml(item_id),
        app, dev, cert, tok,
    )
    if not result.get("success") or not result.get("raw"):
        return None, result.get("message", "GetItem失敗 (詳細不明)")
    try:
        root = ET.fromstring(result["raw"])
    except ET.ParseError as e:
        return None, f"XML parse error: {e}"
    desc = root.findtext(".//n:Item/n:Description", namespaces=_NS)
    return (desc or ""), None


def process_one(item_id: str, creds: dict, execute: bool) -> dict:
    desc, err = _fetch_description(item_id, creds)
    if err is not None:
        return {"ebay_item_id": item_id, "status": "skip", "reason": err}
    if not desc:
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": "description が空 (取得値なし)",
        }

    result = fix_description(desc)

    if not result["tag_balance_ok"]:
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": "タグバランス崩壊の疑いのため自動修正を中止 (要目視確認)",
        }

    # MED-B-1 fix: positive assertion (除去対象以外1バイト不変)
    if not result["positive_diff_ok"]:
        return {
            "ebay_item_id": item_id, "status": "skip",
            "reason": (
                f"positive diff assertion 失敗 (除去以外の変化検出): "
                f"{result['diff_report']}"
            ),
        }

    if result["remaining_hits"]:
        return {
            "ebay_item_id": item_id, "status": "manual_review",
            "reason": f"自動除去不能な残存パターン: {result['remaining_hits']} "
                      f"(地の文埋め込み等、機械的除去は文意破壊リスクあり)",
            "false_positive_hits": result["false_positive_hits"],
        }

    if not result["removed_fragments"]:
        # false positive のみ (manufactured in <年> 等) で実質除去対象なし
        return {
            "ebay_item_id": item_id, "status": "no_action_needed",
            "reason": (
                f"検出パターンは false positive のみ: "
                f"{result['false_positive_hits']} (原産国言及ではない)"
            ),
        }

    plan = {
        "ebay_item_id": item_id,
        "status": "plan",
        "removed_fragments": result["removed_fragments"],
        "false_positive_hits": result["false_positive_hits"],
        "new_description_len": len(result["new_html"]),
        "old_description_len": len(desc),
    }

    if execute:
        app, dev, cert, tok = (
            creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
        )
        exec_result = ebay_client.revise_item_description(
            item_id, result["new_html"], app, dev, cert, tok,
        )
        plan["execute_result"] = exec_result
        # MED-B-2 fix: execute 後の GetItem 再 scan で hits=0 verify.
        # M2 fix (T3 4巡目): log_content_change は postverify 後に移動し、
        # success フラグを bool(exec_ok AND verify_ok) で記録する (監査ログと
        # 実状態を一致させる。以前は revise API の success だけを記録し、
        # postverify NG の item が監査上「成功」に見えていた)。
        if exec_result.get("success"):
            desc_after, err_after = _fetch_description(item_id, creds)
            if err_after is not None:
                plan["postverify"] = {
                    "ok": False,
                    "reason": f"GetItem 再取得失敗: {err_after}",
                }
            else:
                remaining_after = _scan_remaining(desc_after)
                # HIGH-3 fix (T3 Codex): 出現箇所ごとに判定。document 全体で
                # search すると date と真の COO が混在した場合に真の COO
                # 残存を隠蔽する。
                remaining_after_filtered = []
                for p in remaining_after:
                    if p == "manufactured in":
                        occ = _scan_manufactured_in_occurrences(desc_after)
                        if occ["real_hits"]:
                            remaining_after_filtered.append(p)
                    else:
                        remaining_after_filtered.append(p)
                plan["postverify"] = {
                    "ok": len(remaining_after_filtered) == 0,
                    "remaining_hits_after": remaining_after_filtered,
                }
        # 監査ログを最後に記録 (postverify 結果を反映)
        exec_ok = bool(exec_result.get("success"))
        verify_ok = plan.get("postverify", {}).get("ok", True)
        try:
            log_content_change(
                item_id, "description",
                before_value=desc,
                after_value=result["new_html"],
                source_tab="coo_fix_batch_b",
                success=bool(exec_ok and verify_ok),
                ebay_ack=_derive_ack(exec_result),
            )
        except (ValueError, RuntimeError, sqlite3.Error, OSError) as e:
            plan["log_error"] = f"log_content_change 失敗: {type(e).__name__}: {e}"
    else:
        # dry-run でも before/after のサンプル確認用に保持 (肥大化防止で先頭のみ)
        plan["new_description_head500"] = result["new_html"][:500]

    return plan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                     help="実際に eBay へ ReviseItem (description) を送信する (既定は dry-run)")
    ap.add_argument("--limit", type=int, default=None,
                     help="先頭 N 件だけ処理 (動作確認用)")
    args = ap.parse_args()

    if args.execute:
        print("*** --execute 指定: 実際に eBay へ書込みます ***", flush=True)
    else:
        print("dry-run モード (eBay への書込みなし)", flush=True)

    creds = get_ebay_credentials({})
    if not all([creds.get("app_id"), creds.get("dev_id"),
                creds.get("cert_id"), creds.get("user_token")]):
        print("ERROR: eBay credentials 不在 (.env 確認)")
        sys.exit(1)

    # HIGH-4 fix: mode 別 output path
    out_path = _OUT_EXECUTE if args.execute else _OUT_DRYRUN
    progress_path = _PROGRESS_EXECUTE if args.execute else _PROGRESS_DRYRUN
    print(f"output: {out_path.name}", flush=True)

    targets = _load_targets()
    if args.limit:
        targets = targets[: args.limit]

    existing = _load_existing(out_path)
    plans = existing.get("plans", [])
    skips = existing.get("skips", [])
    manual_review = existing.get("manual_review", [])
    no_action = existing.get("no_action", [])
    send_failed = existing.get("send_failed", [])
    # HIGH-1 fix: send_failed は done_ids に**含めない** (次回実行で再試行対象に戻す)。
    done_ids = (
        {p["ebay_item_id"] for p in plans}
        | {s["ebay_item_id"] for s in skips}
        | {m["ebay_item_id"] for m in manual_review}
        | {n["ebay_item_id"] for n in no_action}
    )
    remaining = [t for t in targets if t not in done_ids]
    total = len(remaining)
    print(f"START batch B: target={len(targets)} already_done={len(done_ids)} "
          f"remaining={total} (retry_from_failed={len(send_failed)})", flush=True)

    # HIGH-4 fix: execute + remaining==0 は loud warning + exit(2)
    if args.execute and total == 0:
        print("!" * 60, flush=True)
        print("!! FATAL: execute 指定なのに remaining=0 (送信対象 0 件)。", flush=True)
        print(
            f"!! done_ids は {out_path.name} から読込。追加送信したい場合は"
            f" out_path を rename か削除してから再実行してください。",
            flush=True,
        )
        print("!" * 60, flush=True)
        sys.exit(2)

    def _flush_b():
        out_path.write_text(
            json.dumps({
                "plans": plans, "skips": skips,
                "manual_review": manual_review, "no_action": no_action,
                "send_failed": send_failed,
            }, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    for i, item_id in enumerate(remaining, 1):
        result = process_one(item_id, creds, args.execute)
        status = result["status"]
        # HIGH-1/2 fix (T3 Codex): 送信失敗 or postverify.ok=False は send_failed へ。
        if status == "plan" and args.execute:
            exec_result = result.get("execute_result") or {}
            postverify = result.get("postverify") or {}
            exec_ok = bool(exec_result.get("success"))
            verify_ok = postverify.get("ok", True) if postverify else True
            if not (exec_ok and verify_ok):
                result["status"] = "send_failed"
                result["failure_reason"] = (
                    f"exec_ok={exec_ok} verify_ok={verify_ok} "
                    f"exec_msg={exec_result.get('message', '')[:200]}"
                )
                send_failed.append(result)
                status = "send_failed"
            else:
                # M1 fix (T3 4巡目): retry 成功時に旧 send_failed エントリ除去
                send_failed[:] = [
                    x for x in send_failed if x.get("ebay_item_id") != item_id
                ]

        if status == "plan":
            plans.append(result)
        elif status == "manual_review":
            manual_review.append(result)
        elif status == "no_action_needed":
            no_action.append(result)
        elif status == "send_failed":
            pass
        else:
            skips.append(result)

        # HIGH-3 fix: execute 時は毎件 flush
        flush_now = args.execute or (i % 50 == 0) or (i == total)
        if flush_now:
            _flush_b()
            progress_path.write_text(
                json.dumps({
                    "processed_this_run": i, "remaining_total": total,
                    "plans": len(plans), "skips": len(skips),
                    "manual_review": len(manual_review), "no_action": len(no_action),
                    "send_failed": len(send_failed),
                }, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            print(f"[{i}/{total}] plans={len(plans)} skips={len(skips)} "
                  f"manual_review={len(manual_review)} no_action={len(no_action)} "
                  f"send_failed={len(send_failed)}", flush=True)

        time.sleep(_THROTTLE_SEC)

    _flush_b()
    print("=" * 60)
    print(f"DONE batch B. plans={len(plans)} skips={len(skips)} "
          f"manual_review={len(manual_review)} no_action={len(no_action)} "
          f"send_failed={len(send_failed)}")
    print(f"出力: {out_path}")


if __name__ == "__main__":
    main()
