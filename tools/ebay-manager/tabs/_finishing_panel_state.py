"""W314 Phase 2 S5 (2026-07-03): 統一「商品仕上げパネル」state 層.

設計書: .company/engineering/docs/2026-07-03-finishing-panel-design.md §1-§5
モックアップ: 2026-07-03-finishing-panel-mockup.html (同ディレクトリ、user 承認済)

streamlit に依存しない純関数群 (dirty 判定 / 変更プレビュー組立 / ヘッダ指標算出 /
反映ディスパッチ) を集約する。UI 層 (`tabs/_finishing_panel.py`) はこれらを呼び出す
薄い wrapper に徹する (unit test しやすさ優先、K1)。

listing 識別は ebay_item_id のみ (sku-rules.md 準拠、SKU は一切使わない)。

Phase 2 スコープ:
    - コンテンツ (タイトル/description/ランク/数量) の dirty 追跡 + 一括反映
    - 画像は対象外 (`tabs._supplier_photo_pipeline.render_supplier_photo_apply_section`
      が独自の反映ボタンで完結するため、本モジュールの一括反映ディスパッチには含めない。
      設計書§5「価格・送料は別ボタン」と同じ隔離思想を画像にも適用した Phase 2 の
      明示的な簡略化 — 詳細は _finishing_panel.py 冒頭コメント参照)
    - 価格・送料は完全に対象外 (T3 money-direct、商品管理タブへ誘導のみ)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, MutableMapping, Optional

logger = logging.getLogger(__name__)

SS_PREFIX = "pf_"

# CLAUDE.md コンディションランク 8 段階
RANK_CHOICES: tuple[str, ...] = ("N", "S", "A", "B", "C", "D", "PO", "As-Is")

RANK_LABELS_JA: dict[str, str] = {
    "N": "N — 新品・未開封",
    "S": "S — 新品同様",
    "A": "A — 美品・動作確認済",
    "B": "B — 並品・動作確認済",
    "C": "C — 使用感あり・動作確認済",
    "D": "D — 難あり・動作確認済",
    "PO": "PO — 通電のみ・動作未確認",
    "As-Is": "As-Is — 未確認/部品取り",
}

# 商品ランク → eBay ConditionID (CLAUDE.md 8 段階表準拠)。
# tab_product_management.py::_RANK_TO_CONDITION_ID /
# _supplier_description_pipeline.py::_RANK_TO_CONDITION_ID_SUPPLIER と同値の
# 3 箇所目の参照 (Phase 3 §8「採用ロジック単一化」で統合予定。現時点は
# 各モジュール独立参照を維持する K1: 新規ファイルのみの scope で既存ファイルを
# 触らないため、統合は別 Phase に委ねる)。
RANK_TO_CONDITION_ID: dict[str, str] = {
    "N": "1000", "S": "1500",
    "A": "3000", "B": "3000", "C": "3000", "D": "3000", "PO": "3000",
    "As-Is": "7000",
}

# #44 バグ2修正 (2026-07-04): ConditionDescription をランクから決定論的に導出する
# 定型文マップ (tools/ebay-manager/CLAUDE.md「ConditionDescription 運用方針」準拠、
# 65字以内・英語)。以下のランクは意図的に**含めない**:
#   - N (ConditionID 1000): eBay は新品 1000 で ConditionDescription 非対応。
#     カテゴリによっては Ack=Failure で N への変更操作自体が通らないため、
#     N には CD を一切送らない (HIGH-1 修正 2026-07-04 T3 レビュー)。
#   - As-Is (7000): 商品固有の理由が必須で定型化不能。
# `resolve_condition_description_for_rank` は上記に該当するランクで空文字 or
# AI 生成値を返し (As-Is のみ)、送信直前 `_apply_content_changes` 側で
# ConditionID==1000 の時は最終的に None に落として送信除外する二段防御。
#
# 書式更新 (2026-07-04 user 意図の反映): 「conditionはランクを記載」= ランク表記を
# 明示してほしい、との user 指摘に基づき「ランクラベル先頭 + 短い状態文」に統一。
# 例: A = "Rank A — Excellent. Tested, fully working. Minor wear." (54字)。
# 全て 65 字以内厳守 (As-Is は resolve_condition_description_for_rank 側で AI 生成の
# `As-Is — <reason>` を通す既存挙動を維持)。
RANK_CONDITION_DESCRIPTION_TEMPLATE: dict[str, str] = {
    "S": "Rank S — New (Opened). Unused, no visible wear.",
    "A": "Rank A — Excellent. Tested, fully working. Minor wear.",
    "B": "Rank B — Good. Tested, fully working. Visible wear.",
    "C": "Rank C — Fair. Tested, fully working. Heavy wear.",
    "D": "Rank D — Issues. Tested; works within limits.",
    "PO": "Rank PO — Power-On Only. Full function not verified.",
}


# tools/ebay-manager/CLAUDE.md「コンディションランク 8 段階」の英語ラベル。
# `retarget_rank_headers_in_description` が description 本文の Rank 見出しを
# 追従させる時に使う。RankClassification が渡されない場合の fallback。
RANK_LABELS_EN: dict[str, str] = {
    "N": "New",
    "S": "New (Opened)",
    "A": "Excellent",
    "B": "Good",
    "C": "Fair",
    "D": "Issues",
    "PO": "Power-On Only",
    "As-Is": "As-Is",
}

# description 本文の Rank 見出し追従用パターン (2026-07-04 バグ2 修正 358754421540)。
#
# 【厳格化 2026-07-04 実機事故】1 回目の実装は `Rank\s+[A-Za-z][A-Za-z-]{0,10}` と
# 緩く、`Rank Block (Enso brush)` / `Rank definitions` 等の非見出し文言まで誤マッチ
# して description を破壊した (Q0 preventable の実バグ)。以降は **8 段階の rank code
# 集合と完全一致** し、必ず em-dash + Label が続くパターンだけを対象にする
# (label 単独欠落は対象外にして誤マッチ余地を排除、CLAUDE.md 8 段階に無い綴りは無視)。
#
# 想定マッチ例 (すべて em-dash / entity 必須):
#   - `Rank B — Good`                   (h3 見出し / p 内本文、リテラル em-dash)
#   - `Rank B &mdash; Good`             (v4 テンプレ実物、`listing-description-template.md` L239)
#   - `Rank PO — Power-On Only`
#   - `Rank As-Is — As-Is`              (As-Is は label 部で重複しても許容、置換時は "Rank As-Is" へ落とす)
#   - `Rank S — New (Opened)`           (label 部に括弧を含む場合)
#   - `Rank B &#8212; Good`             (数値参照エンティティ)
#
# 誤マッチしない (重要):
#   - `Rank Block (Enso brush)`         ← "Block" は rank code に無い
#   - `Rank definitions`                ← "definitions" は rank code に無い
#   - `Rank Definitions`                ← 同上
#   - `Rank B` (単独)                   ← em-dash/entity + Label が必須
#   - `<td>A</td><td>Excellent &mdash; Minor wear</td>` ← 'Rank ' prefix が無い定義表行
#
# 【HIGH-1 修正 2026-07-04 verify wave】区切りパターンにリテラル (—/-/–) だけでなく
# HTML entity (&mdash; &ndash; &#8212; &#8211;) も含める。本番 description の見出しは
# `<h3>Rank B &mdash; Good</h3>` の entity 形式で、リテラル em-dash に絞ると無音 no-op
# だった (v4 テンプレ正源準拠)。置換文字列も `Rank {code} &mdash; {label}` の entity
# 形式に統一 (テンプレとの整合)。
#
# 【MED-3 hardening 2026-07-04】 label 部を「RANK_LABELS_EN の 8 値の whitelist」に
# 限定 (`[A-Za-z ()\-]{0,29}` の char class 版だと prose 中の
# `Rank B — Good condition overall` を `Good condition overall` まで飲み込む latent)。
# 順番は「長いラベルを先」に並べる (regex は最左優先で最初のマッチを取るため、
# `New (Opened)` を `New` より先に置かないと `New (Opened)` が `New` で切れる)。
_RANK_HEADER_PATTERN = re.compile(
    r"Rank\s+"
    r"(?P<code>N|S|A|B|C|D|PO|As-Is)"                              # 8 段階のいずれか、完全一致
    r"\s+(?:[—\-–]|&mdash;|&ndash;|&#8212;|&#8211;)\s+"           # em-dash リテラルまたは entity (必須)
    r"(?:New \(Opened\)|Power-On Only|Excellent|Issues|Fair|Good|As-Is|New)"  # 既知 8 ラベルのみ
)

# 【round-trip fix 2026-07-04 実機再確認】As-Is 遷移時に置換形 `Rank As-Is` (em-dash
# + Label 無し) を吐いていた旧実装のため、そこから戻り遷移 (As-Is → A 等) の再
# マッチが不可能で片道切符になっていた。fix:
#   (a) 置換文字列を常に `Rank {code} &mdash; {label}` の**完全形**で emit
#       (以降の書込は round-trip-safe、v4 テンプレ実物 358042514439 と同形状)
#   (b) 過去 stale の bare `Rank As-Is` を回収するため専用パターンを追加
#       (`Rank As-Is` 単独 = 直後に em-dash が続かないケースだけ)
_RANK_HEADER_BARE_AS_IS_PATTERN = re.compile(
    r"Rank\s+As-Is\b(?!\s*(?:[—\-–]|&mdash;|&ndash;|&#8212;|&#8211;))"
)

# 【v4 テンプレ Rank ブロック 3 要素の追従、2026-07-04 実機再確認】
# 実物構造 (data/testdesc16_previews/358*.html で確認):
#   <div class="mh-rb-letter">B</div>                          ← letter (rank code)
#   <h3>Rank B &mdash; Good</h3>                                ← header (既存 helper)
#   <div class="mh-rank-jp">Tested &middot; Working</div>       ← chip (状態語彙)
#   <div class="mh-quick">...(自由文)...</div>                  ← quick notes (自動追従不可)
_RANK_LETTER_PATTERN = re.compile(
    r'(<div\s+class="mh-rb-letter"[^>]*>)([^<]*)(</div>)'
)
_RANK_CHIP_PATTERN = re.compile(
    r'(<div\s+class="mh-rank-jp"[^>]*>)([^<]*)(</div>)'
)

# ランク別 chip 語彙 (v4 テンプレの `mh-rank-jp` セクション用)。
# coordinator 指示 (2026-07-04): テンプレ実物 (`Tested &middot; Working` 等) と
# 揃えつつ user 意図 (「ランクを明示」) を反映した語彙。`&middot;` エンティティで統一。
RANK_CHIP_EN: dict[str, str] = {
    "N":     "Sealed",
    "S":     "Unused",
    "A":     "Tested &middot; Minor Wear",
    "B":     "Tested &middot; Visible Wear",
    "C":     "Tested &middot; Heavy Wear",
    "D":     "Tested &middot; Has Issues",
    "PO":    "Power-On Only",
    "As-Is": "Not Tested",
}


# =============================================================================
# description retarget pending キー (HIGH crash 修正 2026-07-04 live QA)
#
# description text_area (widget) が既に instantiate 済みの状態で `desc_key` へ
# 直接書込むと Streamlit は「widget 生成後の session_state 変更不可」制約で
# StreamlitAPIException を投げる (panel 全体クラッシュ)。retarget は widget
# 生成前に pending へ書き、次サイクルの widget 生成前で吸い上げる二段方式にする。
# =============================================================================


def _pending_desc_retarget_key(eid: str) -> str:
    return pf_key(eid, "desc_retarget_pending")


def schedule_desc_retarget(
    session_state: MutableMapping[str, Any], eid: str, new_html: str,
) -> None:
    """description 本文の retarget 結果を pending キーに書く.

    呼び出し側 (`_render_condition_subblock` のランク変更ハンドラ) は本関数を
    呼んだ後 `st.rerun(scope="fragment")` して次サイクルへ譲る。次サイクルの
    widget 生成前に `consume_pending_desc_retarget` が desc_key へ反映する。

    直接 `desc_key` を触らないので widget instantiate 制約に抵触しない。
    """
    session_state[_pending_desc_retarget_key(eid)] = new_html


def consume_pending_desc_retarget(
    session_state: MutableMapping[str, Any], eid: str,
) -> bool:
    """pending 済み retarget を desc_key に反映し pending をクリアする.

    `_render_description_field` の description text_area (widget) 生成**直前**で
    呼ぶ。pending が無ければ何もしない (False)、あれば desc_key へ書いて pending を
    削除 (True)。

    Returns:
        pending を消費して desc_key を更新したかどうか。
    """
    pending_key = _pending_desc_retarget_key(eid)
    if pending_key not in session_state:
        return False
    new_html = session_state.pop(pending_key)
    session_state[pf_key(eid, "description")] = new_html
    return True


def retarget_rank_headers_in_description(
    html: str, rank_code: Optional[str], rank_label: Optional[str] = None,
) -> tuple[str, bool]:
    """description HTML 本文の `Rank X — Label` 見出しをランク変更に追従させる.

    出典: 2026-07-04 user 追加報告 (358754421540) — ランク変更時 CD だけ同期
    していたため、AI 生成済み description 本文の CONDITION RANK 見出し
    (`<h3>Rank B — Good</h3>` 等) が古いランクで取り残される実バグを修正。
    軽量な regex 置換で追従させる (全文再生成しない、K1 Simplicity)。

    ロジック:
      - `_RANK_HEADER_PATTERN` が html 内に無ければ (html, False) を返す (置換なし)。
      - あれば全マッチを `Rank {code} — {label}` に置換して (html', True) を返す。
      - rank_label 欠落時は RANK_LABELS_EN から補完 (N/S/A-D/PO/As-Is)。
      - As-Is / N など label が「Rank X — X」で重複する場合は "Rank X" のみに落とす。

    Args:
        html: description HTML 本文 (session_state[pf_key(eid,"description")] の内容)
        rank_code: 新ランクコード ('N'/'S'/'A'-'D'/'PO'/'As-Is'/None)
        rank_label: 新ランクラベル (EN、未指定なら RANK_LABELS_EN から補完)

    Returns:
        (置換後 HTML, 変更フラグ)。変更フラグは呼出側の caption 通知条件に使う。
    """
    if not html or not rank_code:
        return html, False
    label = (rank_label or RANK_LABELS_EN.get(rank_code) or "").strip() or rank_code
    # 【HIGH-1 修正 2026-07-04 verify wave】置換文字列は `&mdash;` エンティティで統一
    # (v4 テンプレ正源 `listing-description-template.md` に整合、リテラル em-dash と
    # 混在させると同一 description 内で表記ゆれが発生するのを避ける)。
    # 【round-trip fix 2026-07-04 実機再確認】As-Is も `Rank As-Is &mdash; As-Is` の
    # 完全形で emit する (v4 テンプレ実物 358042514439.html と同形状、以降のランク
    # 変更で `_RANK_HEADER_PATTERN` に再マッチ可能 = 往復対応)。従来「As-Is は label
    # 重複を避けて "Rank As-Is" のみ」の分岐は片道切符バグの原因だったため削除。
    replacement = f"Rank {rank_code} &mdash; {label}"
    changed = False
    # 完全形マッチ (通常経路)
    if _RANK_HEADER_PATTERN.search(html):
        html = _RANK_HEADER_PATTERN.sub(replacement, html)
        changed = True
    # legacy `Rank As-Is` bare (旧 emit) を回収 (round-trip 往復対応)
    if _RANK_HEADER_BARE_AS_IS_PATTERN.search(html):
        html = _RANK_HEADER_BARE_AS_IS_PATTERN.sub(replacement, html)
        changed = True
    return html, changed


def retarget_rank_block_in_description(
    html: str, rank_code: Optional[str], rank_label: Optional[str] = None,
) -> dict:
    """v4 テンプレ Rank ブロックの 3 要素 (letter / h3 / chip) を同時にランク変更へ追従.

    出典: 2026-07-04 実機再確認 — 従来 `retarget_rank_headers_in_description` は h3 のみ
    追従で、`<div class="mh-rb-letter">B</div>` バッジ文字と
    `<div class="mh-rank-jp">Tested · Working</div>` 状態チップが古いランクのまま
    stale する部分追従バグ。決定論で書き換え可能な 3 要素 (letter/h3/chip) を同時
    追従し、自由文 `mh-quick` は「自動追従不可」と報告する (呼出側で caption 表示)。

    決定論書換え不可の要素:
      - `mh-quick` の Quick Notes 自由文 (商品固有) → `quick_notes_present=True`
        で呼出側に flag だけ返す。整合させるには Description 再生成が必要。

    Args:
        html: description HTML
        rank_code: 新ランクコード
        rank_label: 新ランクラベル EN (省略時 RANK_LABELS_EN から補完)

    Returns:
        {
          "new_html": str,
          "h3_changed": bool,
          "letter_changed": bool,
          "chip_changed": bool,
          "any_changed": bool,
          "quick_notes_present": bool,   # mh-quick クラスの自由文が存在するか
        }
    """
    result = {
        "new_html": html or "",
        "h3_changed": False,
        "letter_changed": False,
        "chip_changed": False,
        "any_changed": False,
        "quick_notes_present": False,
    }
    if not html or not rank_code:
        return result

    # (a) h3 見出し (既存 helper 経由、bare As-Is 回収込み)
    new_html, h3_changed = retarget_rank_headers_in_description(html, rank_code, rank_label)
    result["h3_changed"] = h3_changed

    # (b) letter バッジ (`<div class="mh-rb-letter">X</div>`)
    letter_before = new_html
    def _sub_letter(m: re.Match) -> str:
        return f"{m.group(1)}{rank_code}{m.group(3)}"
    new_html = _RANK_LETTER_PATTERN.sub(_sub_letter, new_html)
    result["letter_changed"] = (new_html != letter_before)

    # (c) chip 状態語彙 (`<div class="mh-rank-jp">X</div>`)
    chip_text = RANK_CHIP_EN.get(rank_code)
    if chip_text:
        chip_before = new_html
        def _sub_chip(m: re.Match) -> str:
            return f"{m.group(1)}{chip_text}{m.group(3)}"
        new_html = _RANK_CHIP_PATTERN.sub(_sub_chip, new_html)
        result["chip_changed"] = (new_html != chip_before)

    result["new_html"] = new_html
    result["any_changed"] = (
        result["h3_changed"] or result["letter_changed"] or result["chip_changed"]
    )
    result["quick_notes_present"] = ('mh-quick' in new_html)
    return result


def resolve_condition_description_for_rank(
    rank_code: Optional[str], ai_generated: Optional[str] = None,
) -> str:
    """ランクから ConditionDescription を決定論的に導出する (#44 バグ2修正 2026-07-04).

    出典: user 報告「コンディション欄 (ConditionDescription/Seller Notes) に商品説明が
    残る・入る」。AI 自由文 (`monitor.listing_generator.generate_listing` が返す
    `condition_description`) は system prompt でランク要約のみと指示しているが、
    LLM が商品固有の長文を紛れ込ませるリスクを完全には排除できない
    (プロンプト guard のみに依存する脆さ、原産国 4層防御と同じ教訓)。

    定型を持つランク (S/A/B/C/D/PO) は機械的にテンプレへ差し替え、AI 自由文は一切使わない
    (65字を確実に守り、商品固有の長文が混入する経路そのものを断つ)。

    ランク別の例外挙動:
      - **N (ConditionID 1000)**: eBay 仕様上 ConditionDescription 非対応。テンプレを
        意図的に持たない。AI 生成値も採用せず**空文字**を返す
        (HIGH-1 修正 2026-07-04 T3 レビュー)。apply 層 (`_apply_content_changes`) 側でも
        cond_id==1000 で CD を None 化するが、state 層でも入口で空にする二段防御。
      - **As-Is (7000)**: 商品固有の理由が必須で定型化不能。AI 生成値
        (ai_generated) をそのまま使う (呼出側 `validate_as_is_condition_description`
        で 65字 + 必須を検証)。

    Args:
        rank_code: 'N'/'S'/'A'-'D'/'PO'/'As-Is' のいずれか (None なら AI 生成値 fallback)
        ai_generated: As-Is 時にのみ参照する AI 生成の理由テキスト

    Returns:
        ConditionDescription 文字列。N は "" 固定、As-Is は ai_generated、
        その他は RANK_CONDITION_DESCRIPTION_TEMPLATE の定型、
        rank_code=None/未知は ai_generated の strip 済み値。
    """
    if not rank_code:
        return (ai_generated or "").strip()
    if rank_code == "N":
        # eBay ConditionID 1000 は CD 非対応 (Ack=Failure リスク)。空文字固定。
        return ""
    if rank_code == AS_IS_RANK:
        return (ai_generated or "").strip()
    return RANK_CONDITION_DESCRIPTION_TEMPLATE.get(rank_code) or (ai_generated or "").strip()

FIELD_LABELS_JA: dict[str, str] = {
    "title": "タイトル",
    "description": "Description",
    "images": "画像",
    "rank": "ランク",
    # 2026-07-03 user 追加要望: コンディション理由 (eBay ConditionDescription)。
    # 中古ランクでの「動作確認結果」記載欄。As-Is (7000) は eBay 制約で必須
    # (tools/ebay-manager/CLAUDE.md 「As-Is 出品の XML 必須要件」)。
    "condition_description": "コンディション理由",
    # #44 (2026-07-04): eBay Item Specifics (name:value)。AI 生成/手動編集どちらも
    # 対応、rank/condition_description と同様 DISPATCH_FIELD_ORDER には含めず
    # dirty 時のみ _finishing_panel.py 側で動的に register する。
    "item_specifics": "Item Specifics",
    "quantity": "数量",
}

# 変更プレビュー表示順 (設計書§3 の表と同順)。condition_description は rank の直後
# (「Description と Condition はセット」の視覚的順序 = user 要望)。item_specifics は
# その直後 (Description & Condition 枠の下に描画される UI 配置と同順、#44)。
PREVIEW_FIELD_ORDER: tuple[str, ...] = (
    "title", "description", "images", "rank", "condition_description",
    "item_specifics", "quantity",
)

# コンテンツ一括反映 (🚀 eBay へ反映 ボタン) の対象フィールド。
# 画像は対象外 (モジュール docstring 参照)。condition_description は rank と bundled
# された時は rank apply 内で送信 (同 revise_item_condition 呼出、二重 API 回避)。
# cd 単独 dirty 時は _finishing_panel.py 側で `condition_description` 固有の
# dispatch エントリを register する (dispatch 順序上ここには含めない = 動的判断)。
# item_specifics も同様に動的判断 (#44、revise_item_specifics 経由で独立送信)。
DISPATCH_FIELD_ORDER: tuple[str, ...] = ("title", "description", "rank", "quantity")


# =============================================================================
# session_state ヘルパ (namespace `pf_{eid}_*`)
# =============================================================================

def pf_key(eid: str, suffix: str) -> str:
    """商品仕上げパネル session_state key を組み立てる (`pf_{eid}_{suffix}`)."""
    return f"{SS_PREFIX}{eid}_{suffix}"


def seed_session_value(session_state: MutableMapping[str, Any], key: str, value: Any) -> Any:
    """key が未設定なら value で初期化し、確定値 (既存 or 新規) を返す (idempotent).

    Streamlit の widget key と併用する場合は widget 生成前に呼ぶこと
    (`value=` 引数は key 既存時に Streamlit が無視するため — tab_product_management.py
    `_resolve_pm_search_seed` と同じ確立パターン)。
    """
    if key not in session_state:
        session_state[key] = value
    return session_state[key]


def seed_initial(
    session_state: MutableMapping[str, Any], eid: str, field: str, value: Any,
) -> Any:
    """フィールドの dirty 判定基準値 (baseline) を 1 度だけ確定する.

    初回 render 時に DB/eBay の現在値を焼き付け、以後 user が widget を編集して
    rerun してもこの基準値は変わらない (widget 側の値との差分で dirty を判定する)。
    """
    return seed_session_value(session_state, pf_key(eid, f"{field}_initial"), value)


def mark_field_synced(
    session_state: MutableMapping[str, Any], eid: str, field: str, value: Any,
) -> None:
    """反映成功後、baseline を反映後の値に更新して dirty をクリアする."""
    session_state[pf_key(eid, f"{field}_initial")] = value


# =============================================================================
# ランク解決
# =============================================================================

def resolve_rank_initial(row: dict) -> str:
    """listing の商品ランク初期値を解決する (condition_rank 優先 → ebay_condition_id 逆引き).

    tab_product_management.py::_condition_widget_initial と同ロジック
    (Used=3000 はサブランク A/B/C/D/PO 逆引き不能のため "" = 未設定を返す)。
    """
    sub = (row.get("condition_rank") or "").strip()
    if sub in RANK_CHOICES:
        return sub
    cid = str(row.get("ebay_condition_id") or "").strip()
    return {"1000": "N", "1500": "S", "7000": "As-Is"}.get(cid, "")


def rank_to_condition_id(rank: Optional[str]) -> Optional[str]:
    """商品ランクコードを eBay ConditionID へ変換 (不明な rank は None)."""
    if not rank:
        return None
    return RANK_TO_CONDITION_ID.get(rank)


def compute_dirty_dispatch_fields(
    fields: dict[str, dict], effective_condition_id: Optional[str] = None,
) -> list[str]:
    """変更プレビュー / 反映ボタン件数の元になる dirty フィールド名リストを返す
    (T1 修正 2026-07-04 実機 E2E: 表示件数と実送信件数の一致を保つ).

    ロジック (`_render_content_group` の実装と同期):
      1. `DISPATCH_FIELD_ORDER` (title/description/rank/quantity) の中で dirty のもの
      2. `condition_description` は特殊: dirty ならリストに追加するが、以下は除外:
         - effective_condition_id == "1000" (N=新品): eBay 仕様上 CD 非対応で
           apply 層が送信しないため、件数からも除外して不整合を防ぐ
      3. `item_specifics` は特殊: dirty かつ `dispatch_disabled` が False の時のみ追加
         (H2 = baseline 取得失敗 / MED = multi-value 検出のいずれも抑止対象)

    Returns:
        dirty フィールド名の list (順序保持、DISPATCH_FIELD_ORDER 順 → cd → specifics)。
        UI 側はこの長さを「反映 (N件の変更)」ラベルに出す。
    """
    out: list[str] = []
    for f in DISPATCH_FIELD_ORDER:
        data = fields.get(f)
        if not data:
            continue
        if is_field_dirty(f, data.get("before"), data.get("after")):
            out.append(f)

    _cd = fields.get("condition_description")
    if (
        _cd
        and is_field_dirty(
            "condition_description", _cd.get("before", ""), _cd.get("after", ""),
        )
        and effective_condition_id != "1000"
    ):
        out.append("condition_description")

    _sp = fields.get("item_specifics")
    if (
        _sp
        and is_field_dirty(
            "item_specifics", _sp.get("before") or {}, _sp.get("after") or {},
        )
        and not _sp.get("dispatch_disabled")
    ):
        out.append("item_specifics")

    return out


def resolve_effective_condition_id_for_cd_dispatch(
    rank_field: Optional[dict], fallback_ebay_condition_id: Optional[str] = None,
) -> Optional[str]:
    """CD (ConditionDescription) 送信判定に使う effective ConditionID を解決する
    (T1 修正 2026-07-04 実機 E2E).

    優先順位:
      1. rank_field['after'] (user が編集中のランク) → rank_to_condition_id
      2. rank_field['before'] (現行ランク) → rank_to_condition_id
      3. fallback_ebay_condition_id (row から直接、DB `ebay_condition_id` を想定)

    Args:
        rank_field: `_render_content_group` が `fields["rank"]` に立てる dict
            ({'before': str|None, 'after': str|None})。None/欠損可。
        fallback_ebay_condition_id: rank から解決できない時に使う DB 側の値

    Returns:
        ConditionID 文字列 (例: "1000"/"1500"/"3000"/"7000") または None。
        呼出側は "1000" 判定で CD dispatch を抑止する
        (eBay は N=1000 で ConditionDescription 非対応)。
    """
    rf = rank_field or {}
    for key in ("after", "before"):
        _cid = rank_to_condition_id(rf.get(key))
        if _cid:
            return _cid
    _fb = (fallback_ebay_condition_id or "")
    _fb = str(_fb).strip()
    return _fb or None


# =============================================================================
# dirty 判定
# =============================================================================

def is_field_dirty(field: str, before: Any, after: Any) -> bool:
    """フィールド種別ごとの dirty 判定 (共通ロジック).

    - title/description/rank: 文字列 strip 比較。空文字への変更は dirty
      扱いしない (誤操作で全消し→反映ボタン活性化を防ぐ、既存 title_is_dirty
      `_supplier_followup_state.py` と同方針)。
    - quantity: 整数比較。after が None/変換不能なら dirty 扱いしない
      (0 への変更自体は許容 = 在庫0化は正当な操作)。
    - images / 未知フィールド: 単純な非空 + 不一致判定 (呼出側が summary 文字列
      等を渡す想定、本モジュールの一括反映ディスパッチの対象外だが
      build_change_preview からは利用可能にしておく)。
    """
    if field in ("title", "description", "rank"):
        a = (after or "").strip() if isinstance(after, str) else after
        b = (before or "").strip() if isinstance(before, str) else before
        return bool(a) and a != b
    if field == "condition_description":
        # 空 → 非空: dirty (未設定に理由を追加)
        # 非空 → 別値: dirty (理由変更)
        # 非空 → 空: dirty (理由削除、eBay 側で空文字列送信 → 既存維持)
        #   ⚠️ 空文字は送信時に revise_item_condition の cd=None にマップ
        #      (「値なし = 既存維持」= eBay 挙動)。それでも UI 上「削除操作」を
        #      dirty として認識するため、strip 比較で before != after を真とする。
        a = (after or "").strip() if isinstance(after, str) else ""
        b = (before or "").strip() if isinstance(before, str) else ""
        return a != b
    if field == "quantity":
        if after is None:
            return False
        try:
            return int(after) != int(before or 0)
        except (TypeError, ValueError):
            return False
    if field == "item_specifics":
        # #44 (2026-07-04): dict 比較 (str キー/値へ正規化)。行の追加/削除/値変更の
        # いずれも dirty (data_editor の num_rows="dynamic" で行数が変わり得るため)。
        a = {str(k): str(v) for k, v in after.items()} if isinstance(after, dict) else {}
        b = {str(k): str(v) for k, v in before.items()} if isinstance(before, dict) else {}
        return a != b
    return bool(after) and after != before


# =============================================================================
# 変更プレビュー
# =============================================================================

def summarize_description(text: Optional[str], head_chars: int = 120) -> str:
    """description の UI プレビュー用要約 (先頭 head_chars 字 + 全文文字数).

    監査ログ (listing_content_change_log) には全文を保存する (確定判断2、
    設計書§0)。UI プレビューは要約表示で十分 (S5 タスク指示)。
    """
    t = text or ""
    head = t[:head_chars]
    suffix = "…" if len(t) > head_chars else ""
    return f"{head}{suffix} ({len(t)}文字)"


def summarize_images(mode_label: str, count: int) -> str:
    """画像プレビュー用の要約表示 (モード名 + 枚数)."""
    return f"{mode_label} ({count}枚)"


def summarize_specifics(specifics: Optional[dict], max_items: int = 5) -> str:
    """Item Specifics の UI プレビュー用要約 (先頭 max_items 件 + 総項目数, #44)."""
    if not specifics:
        return "—"
    items = list(specifics.items())
    head = ", ".join(f"{k}: {v}" for k, v in items[:max_items])
    suffix = f" …他{len(items) - max_items}件" if len(items) > max_items else ""
    return f"{head}{suffix} ({len(items)}項目)"


def _default_display(field: str, value: Any) -> str:
    if field == "description":
        return summarize_description(value)
    if field == "item_specifics":
        return summarize_specifics(value)
    if value is None or value == "":
        return "—"
    return str(value)


def build_change_preview(fields: dict[str, dict]) -> list[dict]:
    """dirty フィールドのみを (field, label, before, after) 表示用リストで返す.

    Args:
        fields: {field: {'before': Any, 'after': Any,
                          'before_display': 任意, 'after_display': 任意}}
            before_display/after_display 省略時は _default_display で自動要約する
            (description は summarize_description、それ以外は str()).

    Returns:
        PREVIEW_FIELD_ORDER 順の [{'field','label','before','after'}] (dirty のみ)。
    """
    preview: list[dict] = []
    for field in PREVIEW_FIELD_ORDER:
        data = fields.get(field)
        if not data:
            continue
        before, after = data.get("before"), data.get("after")
        if not is_field_dirty(field, before, after):
            continue
        before_disp = data.get("before_display")
        after_disp = data.get("after_display")
        preview.append({
            "field": field,
            "label": FIELD_LABELS_JA.get(field, field),
            "before": before_disp if before_disp is not None else _default_display(field, before),
            "after": after_disp if after_disp is not None else _default_display(field, after),
        })
    return preview


# =============================================================================
# ヘッダ指標 (価格・利益・在庫・ステータス)
# =============================================================================

def compute_header_metrics(row: dict, settings: Optional[dict] = None) -> dict:
    """パネルヘッダの 4 指標 (価格・利益・在庫・ステータス) を算出する.

    Args:
        row: monitor.database.get_ebay_listing_by_item_id() が返す 1 行 dict。
        settings: calculator.load_settings() 相当 (省略時は自動読込)。

    tab_product_management.py::_render_hero_metrics の軽量版 (設計書§3「軽量版」
    指示準拠)。bp_state pill / カテゴリ FVF pill 等の重い付随表示は含めない。

    purchase_yen / weight_g が DB に欠けていれば profit は None を返す
    (0 円等の誤値で誤魔化さない = Q0)。利益試算で例外が出てもヘッダ全体は
    落とさず None のまま返す (ヘッダは補助表示、パネル本体を壊さない)。
    """
    price = float(row.get("current_price") or 0)
    quantity = int(row.get("quantity_ebay") or 0)
    status = "Ended" if int(row.get("is_ended") or 0) == 1 else "Active"

    result: dict[str, Any] = {
        "price_usd": price,
        "profit_jpy": None,
        "profit_rate_pct": None,
        "quantity": quantity,
        "status": status,
    }

    pyen = row.get("purchase_yen")
    weight_g = row.get("weight_g")
    if not (price > 0 and pyen and weight_g):
        return result

    try:
        from calculator import CalcInput, calculate
        if settings is None:
            from calculator import load_settings
            settings = load_settings()
        calc_res = calculate(
            CalcInput(
                purchase_yen=float(pyen),
                item_price_usd=price,
                weight_g=float(weight_g),
                length_cm=float(row.get("length_cm") or 0),
                width_cm=float(row.get("width_cm") or 0),
                height_cm=float(row.get("height_cm") or 0),
                category_id=int(row.get("category_id") or 0),
                is_ddu=False,
                country_code="US",
                point_yen=(
                    float(row["point_yen"]) if row.get("point_yen") is not None else None
                ),
            ),
            settings,
        )
        if calc_res.service_results:
            best = max(calc_res.service_results, key=lambda s: s.profit_with_refund)
            result["profit_jpy"] = round(best.profit_with_refund)
            result["profit_rate_pct"] = round(best.profit_with_refund_rate * 100, 1)
    except Exception as e:  # noqa: BLE001 -- ヘッダは補助表示、失敗しても panel を壊さない
        logger.warning(
            "compute_header_metrics: 利益試算失敗 eid=%s: %s",
            row.get("ebay_item_id"), e,
        )
    return result


# =============================================================================
# 仕入先 URL 解決 (画像/description 生成の入力元)
# =============================================================================

def resolve_source_url(
    candidate_url: Optional[str],
    row: Optional[dict],
    user_input: Optional[str] = None,
) -> str:
    """description 生成 / 画像取得で使う仕入先 URL を解決する.

    優先順位 (設計書§4 + task 指示): candidate_url > row["source_url"] > user_input。
    いずれも空なら "" を返す (呼出側が「URL 未指定」ハンドリング)。

    (candidate_url) の空文字 "" は None と同じく "未指定" として扱う (S6 が
    None を渡すか "" を渡すか実装差があるため両対応)。
    """
    for cand in (candidate_url, (row or {}).get("source_url"), user_input):
        s = (cand or "").strip() if isinstance(cand, str) else ""
        if s:
            return s
    return ""


# =============================================================================
# description の eBay 取得 (「⬇️ eBay から取得」ボタン)
# =============================================================================

def fetch_description_from_ebay(ebay_item_id: str, config: Optional[dict] = None) -> dict:
    """eBay GetItem で現行 description を取得する.

    tab_product_management.py::_render_desc_fetch_button と同じ
    `monitor.ebay_client.get_single_listing` 経由 (既存パターン踏襲)。

    Returns:
        {'success': bool, 'description': str, 'message': str}
    """
    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    from monitor.ebay_client import get_single_listing

    try:
        creds = get_ebay_credentials(config)
    except Exception as e:  # noqa: BLE001 -- credentials 解決の多様な例外を UI に伝える
        return {"success": False, "description": "", "message": f"credentials 取得エラー: {e}"}
    if not ebay_credentials_ok(creds):
        return {"success": False, "description": "", "message": "eBay credentials 未設定"}

    snap = get_single_listing(
        ebay_item_id, creds["app_id"], creds["dev_id"], creds["cert_id"], creds["user_token"],
    )
    if snap is None or snap.get("description") is None:
        return {
            "success": False, "description": "",
            "message": "取得失敗 (GetItem 応答なし / Description 空)",
        }
    return {"success": True, "description": snap.get("description") or "", "message": "取得しました"}


# =============================================================================
# ConditionDescription / Item Specifics の eBay 取得 (CD 欄・Item Specifics
# プレビュー欄の baseline、#44 2026-07-04)
# =============================================================================

def fetch_condition_and_specifics_from_ebay(
    ebay_item_id: str, config: Optional[dict] = None,
) -> dict:
    """eBay GetItem で現行 ConditionDescription + ItemSpecifics を取得する.

    CD 欄 / Item Specifics プレビュー欄の baseline (dirty 判定・監査ログ before の
    正確化) に使う。`monitor.ebay_client` / `monitor.ebay_listing_snapshot` は
    G2 が並行実装中のため触らず、既存の `_build_get_item_xml`
    (IncludeSelector=Details,ItemSpecifics 済) を import のみで再利用し、本関数内で
    ConditionDescription / ItemSpecifics を追加 parse する
    (`monitor.ebay_listing_snapshot.fetch_listing_snapshot` と同じ「ebay_client の
    private helper を import して独自 parse」パターン)。

    Returns:
        {'success': bool, 'condition_description': str,
         'item_specifics': dict[str, str],
         'multi_value_names': list[str],  # MED (2026-07-04 Codex): 同一 Name に複数
             # <Value> が並ぶ Item Specific (multi-value aspect) を検出したら Name を
             # 列挙する。UI 側 (`_render_item_specifics_field`) はこのリストが非空なら
             # dispatch_disabled 扱いにして反映を抑止する (先頭値だけ dict 化した状態で
             # replace_all=True 送信すると追加値が消えるため。データモデル拡張は Phase3)。
         'message': str}
    """
    import xml.etree.ElementTree as ET

    import httpx

    from monitor.credentials import ebay_credentials_ok, get_ebay_credentials
    from monitor.ebay_client import (
        API_VERSION,
        TRADING_API_URL,
        _build_get_item_xml,
        _resolve_active_token,
    )

    out: dict = {
        "success": False, "condition_description": "", "item_specifics": {},
        "multi_value_names": [], "message": "",
    }

    try:
        creds = get_ebay_credentials(config)
    except Exception as e:  # noqa: BLE001 -- credentials 解決の多様な例外を UI に伝える
        out["message"] = f"credentials 取得エラー: {e}"
        return out
    if not ebay_credentials_ok(creds):
        out["message"] = "eBay credentials 未設定"
        return out

    token = _resolve_active_token(creds["user_token"])
    xml_body = _build_get_item_xml(ebay_item_id).replace("{USER_TOKEN}", token)
    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": API_VERSION,
        "X-EBAY-API-CALL-NAME": "GetItem",
        "X-EBAY-API-APP-NAME": creds["app_id"],
        "X-EBAY-API-DEV-NAME": creds["dev_id"],
        "X-EBAY-API-CERT-NAME": creds["cert_id"],
        "Content-Type": "text/xml",
    }
    try:
        resp = httpx.post(
            TRADING_API_URL, content=xml_body.encode("utf-8"), headers=headers, timeout=30,
        )
        resp.raise_for_status()
    except (httpx.HTTPError, OSError) as e:
        out["message"] = f"通信エラー: {e}"
        return out

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        out["message"] = f"XML parse error: {e}"
        return out

    ns = {"n": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext("n:Ack", namespaces=ns) or "Fail"
    if ack not in ("Success", "Warning"):
        errs = root.findall(".//n:Errors/n:LongMessage", namespaces=ns)
        msg = "; ".join(e.text for e in errs if e.text) or "Unknown error"
        out["message"] = f"API エラー: {msg}"
        return out

    item = root.find(".//n:Item", namespaces=ns)
    if item is None:
        out["message"] = "GetItem に Item ノードが無い"
        return out

    condition_description = item.findtext("n:ConditionDescription", namespaces=ns) or ""
    specifics: dict[str, str] = {}
    # MED (2026-07-04 Codex): multi-value aspect (同一 Name に複数 <Value>) を検出する。
    # 例: Item Specifics で "Features" が ["Waterproof", "Bluetooth", "Noise Cancelling"] の
    # ように配列で登録されているケース。従来 `findtext("n:Value")` は先頭値しか読まず、
    # dict[Name]=Value 化してそのまま replace_all=True で送ると追加値が消える。
    # 最小対応として本 parse で **検出のみ** 行い、UI 側で dispatch_disabled 扱いに落とす
    # (dict[Name]=list[str] へのデータモデル拡張は Phase3 で扱う、K1)。
    multi_value_names: list[str] = []
    nvl = item.find("n:ItemSpecifics", namespaces=ns)
    if nvl is not None:
        for nv in nvl.findall("n:NameValueList", namespaces=ns):
            name = (nv.findtext("n:Name", namespaces=ns) or "").strip()
            if not name:
                continue
            value_nodes = nv.findall("n:Value", namespaces=ns)
            values = [(v.text or "").strip() for v in value_nodes if (v.text or "").strip()]
            if len(values) > 1:
                multi_value_names.append(name)
            # dict は先頭値で保持 (UI プレビュー用。dispatch_disabled で反映は起きない)
            specifics[name] = values[0] if values else ""

    out.update({
        "success": True,
        "condition_description": condition_description,
        "item_specifics": specifics,
        "multi_value_names": multi_value_names,
        "message": "取得しました",
    })
    return out


# =============================================================================
# description の AI 生成 (「🤖 AI で生成」)
# =============================================================================

def generate_description_via_ai(
    candidate_url: str,
    *,
    candidate_id: int = 0,
    in_stock: bool = False,
    rank_override_code: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    existing_listing_context: Optional[dict] = None,
) -> dict:
    """仕入先 URL から description HTML を AI 生成する (既存パイプライン再利用).

    tab_product_management.py::_render_url_direct_description_section と同じ
    `_supplier_description_pipeline.generate_supplier_description` を呼ぶ
    (candidate_id=0 で「URL 直接投入」経路、supplier_candidates INSERT なし)。

    候補が既に紐付いていれば呼出側で candidate_id を渡す (監査ログ紐付けに使う。
    generate_supplier_description は candidate_id を DB 更新には使わないので
    0 でも実処理は同じ、ログ属性としてのみ機能する)。

    2026-07-04 user 恒久仕様追加: 引用元 URL が無くても extra_instructions が
    あれば生成可能にする (既存 listing 情報を代替コンテキストとして使う)。
    - URL のみ: 従来通り (scrape ベース)
    - 指示のみ: URL scrape をスキップし、existing_listing_context + 指示のみで生成
    - 両方: scrape + 指示を両方使う (矛盾時は指示を優先、プロンプト側で明示)
    - 両方空: エラー (「URL か指示のどちらかを入力してください」)

    Args:
        candidate_url: 仕入先 URL (空文字可、extra_instructions があれば生成続行)
        candidate_id: supplier_candidates.id (無ければ 0)
        in_stock: 有在庫扱い (`sku.startswith("stock")` の判定結果を渡す)
        rank_override_code: 手動指定ランク (N/S/A/B/C/D/PO/As-Is、None なら AI 判定)
        extra_instructions: description に入れたい文言 (任意、URL 空時は必須)
        existing_listing_context: URL 空時に使う代替コンテキスト
            ({'title': str, 'condition_rank': str, 'listing_description': str} 等、
            仕上げパネルの既存 listing row から渡される。呼出元指定なしなら None)

    Returns:
        {'success': bool, 'description_html': str, 'rank_code': str,
         'title_en': str, 'item_specifics': dict, 'condition_description': str,
         'message': str}
    """
    _url = (candidate_url or "").strip()
    _extra = (extra_instructions or "").strip()
    if not _url and not _extra:
        return {
            "success": False, "description_html": "", "rank_code": "",
            "title_en": "", "item_specifics": {}, "condition_description": "",
            "message": "引用元 URL か「description に入れたい文言・指示」のいずれかを入力してください",
        }
    try:
        from tabs._supplier_description_pipeline import generate_supplier_description
    except ImportError as e:
        logger.exception("generate_supplier_description import 失敗")
        return {
            "success": False, "description_html": "", "rank_code": "",
            "title_en": "", "item_specifics": {}, "condition_description": "",
            "message": f"description 生成モジュール読込失敗: {e}",
        }
    try:
        result = generate_supplier_description(
            candidate_id=candidate_id,
            candidate_url=_url,
            in_stock=in_stock,
            rank_override_code=rank_override_code,
            extra_instructions=extra_instructions,
            existing_listing_context=existing_listing_context,
        )
    except Exception as e:  # noqa: BLE001 -- 生成パイプラインの多様な例外を UI に伝える
        logger.exception("generate_supplier_description raised")
        return {
            "success": False, "description_html": "", "rank_code": "",
            "title_en": "", "item_specifics": {}, "condition_description": "",
            "message": f"{type(e).__name__}: {e}",
        }
    # generate_supplier_description が返す dict をそのまま透過 (schema 同一)
    return {
        "success": bool(result.get("success")),
        "description_html": result.get("description_html") or "",
        "rank_code": result.get("rank_code") or "",
        "title_en": result.get("title_en") or "",
        # #44 (2026-07-04): AI 生成が CD 欄/Item Specifics プレビュー欄へ自動セット
        # される経路 (_finishing_panel.py::_render_description_ai_controls)。
        "item_specifics": dict(result.get("item_specifics") or {}),
        "condition_description": (result.get("condition_description") or "")[:65],
        "message": result.get("message") or "",
    }


# =============================================================================
# As-Is (7000) の ConditionDescription 必須ガード
# =============================================================================

# tools/ebay-manager/CLAUDE.md「As-Is 出品の XML 必須要件」:
#   65 字以内 / 英文 / `As-Is — <reason>` 形式
AS_IS_RANK = "As-Is"
AS_IS_CD_MAX_LEN = 65


def validate_as_is_condition_description(
    effective_rank: Optional[str], effective_cd: Optional[str],
) -> Optional[str]:
    """As-Is (7000) 反映時の condition_description バリデーション.

    Returns:
        None: バリデーション PASS (As-Is でない、または理由が正当)
        str: エラーメッセージ (呼出側が st.error で表示 + 反映拒否する)

    Args:
        effective_rank: 反映後の実効ランク ('N'/'S'/'A'-'D'/'PO'/'As-Is'/None)
        effective_cd: 反映後の実効理由 (dirty なら新値、無 dirty なら既存の欄値)

    ルール (tools/ebay-manager/CLAUDE.md 準拠):
      - As-Is 以外は無検査 (PASS)。
      - As-Is で理由が空 → 反映拒否 (欠落は buyer 紛争で Defect 確定リスク)。
      - As-Is で理由 > 65 字 → 反映拒否 (eBay XML 65 字制約、超過は VerifyAdd で警告)。
    """
    if effective_rank != AS_IS_RANK:
        return None
    cd = (effective_cd or "").strip()
    if not cd:
        return (
            "As-Is (7000) はコンディション理由が必須です "
            "(例: 'As-Is — No AC adapter for testing', 'As-Is — PCB burn damage')。"
            "欠落すると buyer 紛争で Defect 確定リスクがあります "
            "(tools/ebay-manager/CLAUDE.md As-Is XML 必須要件)。"
        )
    if len(cd) > AS_IS_CD_MAX_LEN:
        return (
            f"As-Is のコンディション理由は {AS_IS_CD_MAX_LEN} 字以内 (現在 {len(cd)} 字)。"
            "eBay XML 制約のため短縮してください "
            "(推奨形式: 'As-Is — <reason>')。"
        )
    # HIGH-1 修正 (2026-07-04 Codex): 別ランク定型 (`Rank B — Good. ...`) が cd_key に
    # 残留した状態で As-Is へ切替えても validate は「非空・65字以内」しか見ず素通り
    # していた → As-Is 商品に「動作確認済」等の矛盾 Seller Notes が送信される事故経路。
    # 二重ゲート: `Rank ` prefix / `As-Is` 不在のいずれも reject する。
    if cd.startswith("Rank "):
        return (
            "As-Is に別ランクの定型文が残留しています (例: 'Rank B — Good. ...')。"
            "As-Is の商品固有理由 (`As-Is — <reason>`) を入力してください "
            "(旧ランク定型のまま送信すると Seller Notes と実状が矛盾します)。"
        )
    if "As-Is" not in cd:
        return (
            "As-Is 理由は 'As-Is — <reason>' 形式で書いてください "
            "(現在の値には 'As-Is' が含まれていません。buyer に「なぜ As-Is か」を"
            "明示する必要があります)。"
        )
    return None


# =============================================================================
# Item Specifics の eBay 反映 (#44 2026-07-04)
# =============================================================================

def apply_item_specifics_to_ebay(
    ebay_item_id: str,
    item_specifics: dict,
    *,
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
) -> dict:
    """Item Specifics を eBay へ反映する (G2 `revise_item_specifics` 契約に委譲).

    `monitor.ebay_client.revise_item_specifics` は G2 が並行実装中のため import は
    try/except ImportError で no-op fallback する (未実装環境でもパネル全体を
    壊さない。Q0: 理由を明示したメッセージを返す、サイレント失敗にしない)。

    契約: `revise_item_specifics(ebay_item_id, item_specifics: dict, *, app_id,
    dev_id, cert_id, user_token, replace_all=True) -> dict` (keys: success, message,
    removed_names)。removed_names は eBay ポリシー上除去された項目名 (原産国等)。

    Returns:
        {'success': bool, 'message': str, 'removed_names': list[str]}
    """
    try:
        from monitor.ebay_client import revise_item_specifics
    except ImportError as e:
        logger.warning("revise_item_specifics import 失敗 (G2 未実装?): %s", e)
        return {
            "success": False,
            "message": "Item Specifics 反映機能は未実装です (revise_item_specifics 未提供)",
            "removed_names": [],
        }
    try:
        result = revise_item_specifics(
            ebay_item_id, item_specifics,
            app_id=app_id, dev_id=dev_id, cert_id=cert_id, user_token=user_token,
            replace_all=True,
        )
    except Exception as e:  # noqa: BLE001 -- revise 呼出の多様な例外を UI に伝える
        logger.exception("revise_item_specifics raised eid=%s", ebay_item_id)
        return {"success": False, "message": f"{type(e).__name__}: {e}", "removed_names": []}
    return {
        "success": bool(result.get("success")),
        "message": result.get("message") or "",
        "removed_names": list(result.get("removed_names") or []),
    }


# =============================================================================
# コンテンツ一括反映ディスパッチ
# =============================================================================

def dispatch_content_changes(
    ebay_item_id: str,
    changes: list[dict],
    *,
    source_tab: Optional[str] = None,
    candidate_id: Optional[int] = None,
    log_fn: Optional[Callable[..., int]] = None,
) -> dict[str, dict]:
    """コンテンツ一括反映のディスパッチ (dirty フィールド列 → revise 実行 → 監査ログ).

    Args:
        ebay_item_id: 反映先 listing (sku-rules: 識別は ebay_item_id のみ)。
        changes: 各要素は以下の dict:
            {'field': str, 'before': Any, 'after': Any, 'apply': Callable[[], dict]}
            `apply()` は revise API 呼出 + (成功時) DB 同期まで完結させ
            {'success': bool, 'message': str} を返す呼び出し側の責務。
            revise 関数そのものを DI するのではなく「呼べば完結する callable」を
            DI することで、本関数は streamlit / credentials / DB 更新の詳細に
            一切依存しない (streamlit 非依存の純関数 unit test を可能にする)。
        log_fn: `monitor.listing_content_change_log.log_content_change` の
            差し替え用 (テスト DI)。省略時は実モジュールを import する。

    Returns:
        {field: {'success': bool, 'message': str}} — 全フィールドについて実値を
        返す (1 つ失敗しても残りは続行し、握り潰さない = Q0)。

    監査ログは成功/失敗を問わず全フィールドに対して記録する (success 列で判別、
    `apply_followup_title_to_ebay` と同方針)。ログ記録自体の失敗で revise 結果を
    握り潰さない (try/except を分離)。
    """
    if log_fn is None:
        from monitor.listing_content_change_log import log_content_change as log_fn

    results: dict[str, dict] = {}
    for change in changes:
        field = change["field"]
        before = change.get("before")
        after = change.get("after")
        apply_fn = change["apply"]

        try:
            outcome = apply_fn()
        except Exception as e:  # noqa: BLE001 -- 1 フィールド失敗で残りを止めない (Q0)
            logger.warning(
                "dispatch_content_changes: apply 例外 eid=%s field=%s: %s",
                ebay_item_id, field, e,
            )
            outcome = {"success": False, "message": f"{type(e).__name__}: {e}"}

        ok = bool(outcome.get("success"))
        message = outcome.get("message") or ""
        results[field] = {"success": ok, "message": message}

        try:
            log_fn(
                ebay_item_id, field, before, after,
                source_tab=source_tab, candidate_id=candidate_id,
                success=ok, ebay_ack=message,
            )
        except Exception as e:  # noqa: BLE001 -- 監査ログ失敗で revise 結果を握り潰さない
            logger.warning(
                "dispatch_content_changes: log_content_change 失敗 eid=%s field=%s: %s",
                ebay_item_id, field, e,
            )

    return results
