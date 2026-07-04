"""DASHBOARD 磨き込み (2026-07-04、依頼ボード #39 差し戻し) — 通知の内部変数露出を
render 時に業務語へ変換する層 (``tabs._notification_center_html.humanize_notification_text``)
の unit test。

対象パターンは notification_log の実データ棚卸し (2026-07-04 SELECT) で確認した
頻出タイトル (W153 truncation / W153 新規ライバル検出 / W301 rival_classify /
(W139) 系 W 番号 suffix) を実際の DB 文字列でそのまま検証する。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tabs._notification_center_html import (
    humanize_notification_text,
    render_notification_row_html,
)


# ---------------------------------------------------------------------------
# 既知パターン (実 DB 文字列そのまま)
# ---------------------------------------------------------------------------


def test_w301_rival_classify_maps_to_business_wording():
    title = (
        "⚠️ W301 rival_classify 要確認 (processed=868, issues=818)\n"
        "- 357710652709 / 396958315291: ai_cap_exceeded — "
        "max_ai_calls_per_run=50 超過のため AI 判定をスキップ (review へ、Q0 痕跡)\n"
        "... 他 813 件\n"
        "(cap 超過 / AI 呼出エラーは全て review へ倒しています。fail-closed)"
    )
    new_title, new_body = humanize_notification_text("rival", title, title)
    assert new_title == "AI店長: 818 件が要確認判定 (Shadow 運用中・対応不要)"
    # 内部ログの bullet 詳細は業務ユーザーに不要なため空にする
    assert new_body == ""
    # 変換後は内部変数トークンが一切残らない
    assert "processed=" not in new_title
    assert "W301" not in new_title
    assert "ai_cap_exceeded" not in new_title


def test_w153_truncation_maps_to_business_wording():
    title = (
        "⚠️ W153 truncation: 監視 ON listing が 70 件あり "
        "max_listings_per_run=30 を超えています。今回 40 件 skip "
        "(ORDER BY ebay_item_id 末尾は永久 starve リスク)。"
        "商品管理タブで ON 数を絞るか設定で cap を上げてください。"
    )
    new_title, new_body = humanize_notification_text("rival", title, title)
    assert new_title == "最安値チェック: 監視対象 70 件が処理上限 30 件を超過、今回 40 件が未処理"
    assert "対応" in new_body  # 何をすべきかが body に残る
    assert "max_listings_per_run" not in new_title
    assert "W153" not in new_title


def test_w153_new_rival_detection_body_cleared():
    """新規ライバル検出は姉妹パターン (W301/W153 truncation) と同扱いで body="" 化。

    2026-07-04 差戻し 2 段目: 旧実装は new_body=None を返して fallback へ委譲していたが、
    絵文字プレフィックス始まり ("🎯 W153 …") では fallback の `^W\\d+` アンカーが効かず、
    生ログ "🎯 W153 新規ライバル検出 (2 listings) - TRAVELER'S..." がプレビューに残存
    する不具合の直接原因になっていた。タイトルで情報完結する意図なので body="" に統一。
    """
    title = (
        "🎯 W153 新規ライバル検出 (2 listings)\n"
        "- TRAVELER'S notebook Limited Set TRAVELER (0235): 1 名\n"
        "- HIOKI DT4282 Digital Multimeter 10A Slig (9481): 1 名"
    )
    new_title, new_body = humanize_notification_text("rival", title, title)
    assert new_title == "新規ライバル出品を検出 (2 件)"
    assert "W153" not in new_title
    # 本文は空 (bullet 詳細は業務ユーザーに不要、カウントは title にある)
    assert new_body == ""
    # 差戻し FAIL の再現防止: 生ログトークンがどこにも残らない
    assert "listings" not in new_body
    assert "W153" not in new_body


# ---------------------------------------------------------------------------
# fallback: 未知パターンの W 番号 / 変数=値 トークン剥がし
# ---------------------------------------------------------------------------


def test_fallback_strips_paren_wrapped_w_number():
    title = "[緊急] 監視カバレッジ欠落検知 (W139)"
    new_title, _ = humanize_notification_text("system", title, "")
    assert new_title == "[緊急] 監視カバレッジ欠落検知"


def test_fallback_strips_paren_wrapped_w_number_with_suffix():
    title = "[緊急] URL乖離検知 23 件 (W139-revisit)"
    new_title, _ = humanize_notification_text("system", title, "")
    assert new_title == "[緊急] URL乖離検知 23 件"


def test_fallback_strips_leading_w_number_prefix():
    assert humanize_notification_text("research", "W228 リサーチ探索 (04:30)", "")[0] == "リサーチ探索 (04:30)"
    assert humanize_notification_text("research", "W229 商品リサーチ発掘 (03:30)", "")[0] == "商品リサーチ発掘 (03:30)"


def test_fallback_leaves_already_clean_title_untouched():
    """既に業務語だけの通知は無変換 (誤爆しないこと)。"""
    title = "売り切れ検知 → 仕入先候補探索 結果"
    new_title, new_body = humanize_notification_text("inventory", title, "")
    assert new_title == title
    assert new_body == ""


def test_fallback_var_eq_token_removed_when_boundary_is_numeric():
    new_title, _ = humanize_notification_text("system", "処理結果 count=5 完了", "")
    assert "count=" not in new_title
    assert "処理結果" in new_title and "完了" in new_title


def test_fallback_does_not_mangle_hyphenated_compound_value():
    """`band=1-2kg` のような複合値は数値のみの var=value ではないため破壊しない
    (負の副作用防止: ハイフン付き値の部分文字列破壊テスト)。"""
    title = "送料ポリシー未作成: band=1-2kg の policy token が未設定"
    new_title, _ = humanize_notification_text("default", title, "")
    assert "band=1-2kg" in new_title


def test_fallback_does_not_mangle_product_title_with_hyphenated_model_number():
    """商品型番 (例: Sony Cyber-shot DSC-W800) を W 番号と誤認して破壊しないこと
    (fallback は先頭 'W123 ' か '(W123)' のみに限定、mid-string は対象外)。"""
    title = "Sony Cyber-shot DSC-W800 Digital Camera"
    new_title, _ = humanize_notification_text("order", title, "")
    assert new_title == title


# ---------------------------------------------------------------------------
# render_notification_row_html 経由の統合確認 (visible title/sub に反映されること)
# ---------------------------------------------------------------------------


def _base_notif(**overrides) -> dict:
    row = {
        "id": 99,
        "category": "rival",
        "severity": "warning",
        "title": "",
        "body": "",
        "link_target": "rival",
        "link_ref": None,
        "discord_sent": 0,
        "created_at": "2026-07-04 00:00:00",
        "read_at": None,
    }
    row.update(overrides)
    return row


def test_render_row_applies_humanize_for_w301_pattern():
    title = "⚠️ W301 rival_classify 要確認 (processed=868, issues=818)\n- detail line"
    html = render_notification_row_html(_base_notif(title=title, body=title))
    assert "AI店長" in html
    assert "processed=" not in html
    assert "W301" not in html


def test_render_row_applies_fallback_for_unknown_w_number_title():
    html = render_notification_row_html(
        _base_notif(category="system", title="[緊急] 監視カバレッジ欠落検知 (W139)", body="")
    )
    assert "監視カバレッジ欠落検知" in html
    assert "W139" not in html


# ---------------------------------------------------------------------------
# 2026-07-04 差戻し 2 段目: 絵文字プレフィックス付き本文 / (N listings) 変換
# ---------------------------------------------------------------------------


def test_fallback_strips_emoji_prefix_before_w_number():
    """先頭の絵文字を剥がしてから `^W\\d+` を適用 = 絵文字プレフィックス始まりの
    未知パターンでも W 番号が確実に取れる (差戻し FAIL の直接原因への防御)。"""
    assert humanize_notification_text(
        "system", "🎯 W999 未知の内部通知", ""
    )[0] == "未知の内部通知"
    # 記号系絵文字 (⚠) + variation selector 混じり
    assert humanize_notification_text(
        "system", "⚠️ W888 別の内部アラート", ""
    )[0] == "別の内部アラート"


def test_fallback_converts_listings_paren_to_japanese():
    """`(N listings)` → `(N 件)` への機械変換 (発行側 tasks/ 修正が過去 DB に
    届かないため表示側で吸収)。"""
    assert humanize_notification_text(
        "rival", "🎯 W999 未知の内部通知 (42 listings)", ""
    )[0] == "未知の内部通知 (42 件)"
    # 単数形 listing も対応 (case-insensitive)
    assert humanize_notification_text(
        "rival", "Some Notice (1 Listing)", ""
    )[0] == "Some Notice (1 件)"


def test_fallback_preserves_legitimate_emoji_only_prefix():
    """W 番号が続かない legitimate な絵文字プレフィックス (「🛒 商品が売れました」等)
    は破壊しない — 先頭デコレーション剥がしは lookahead で W\\d に限定してある。"""
    assert humanize_notification_text(
        "order", "🛒 商品が売れました", ""
    )[0] == "🛒 商品が売れました"
    # hit_id=38257 は var= トークンとして全体除去 (`hit_id` が identifier)、
    # 末尾の空白は _DANGLE_CHARS で trim される。
    assert humanize_notification_text(
        "keyword", "🔔 キーワード新着 (🔨 ヤフオク) hit_id=38257", ""
    )[0] == "🔔 キーワード新着 (🔨 ヤフオク)"


def test_render_row_no_raw_log_leak_for_emoji_prefixed_w153_new_rival():
    """render 経由の統合確認: 差戻し FAIL の再現 case で 生ログトークンが
    visible span のどこにも残らないこと (nc-title/nc-sub 両方)。"""
    title = (
        "🎯 W153 新規ライバル検出 (2 listings)\n"
        "- TRAVELER'S notebook Limited Set TRAVELER (0235): 1 名"
    )
    html = render_notification_row_html(_base_notif(title=title, body=title))
    # 「新規ライバル出品を検出」までは業務語で表示される
    assert "新規ライバル出品を検出" in html
    # 差戻し FAIL の直接症状: 本文プレビュー領域 (nc-sub) の中に 生ログが残っていた
    import re as _re
    sub_match = _re.search(r'nc-sub">(.*?)</span>', html)
    sub_visible = sub_match.group(1) if sub_match else ""
    assert "W153" not in sub_visible
    assert "listings" not in sub_visible
    assert "TRAVELER" not in sub_visible
    # title 属性 (hover tooltip) はフル情報を残す設計だが、そこの検査はしない
    # (視覚上の生ログ露出を防げていれば差戻しの意図は満たされる)
