"""ライバル「保存 + 価格再取得」一体化ボタンの回帰テスト (2026-06-11 user 指示).

旧フロー: item id 直打ち → 💾DB保存 → 反映確認 → 🔄再取得 (3 ステップ、
保存忘れで旧ライバルを再取得する罠)。
新フロー: 直打ち → 「💾 ライバル保存 + 価格再取得」1 押下で
upsert (全置換保存) → bump_db_version (W134) → refresh → rerun を連続実行。

ソーステキスト検証 (Streamlit 実行不要) で以下を固定する:
  1. ボタン名に「保存」が含まれる (user 要望: 保存されることがわかる名前)
  2. ハンドラ内の実行順序 = upsert → bump_db_version → refresh → st.rerun
  3. 表示条件が pricing_rows 単独でない (初回登録 = pricing_rows 空でも
     グリッド入力 (comp_list) があればボタンが出る)
"""
from pathlib import Path

_SRC = (
    Path(__file__).resolve().parents[1]
    / "tabs" / "tab_product_management.py"
).read_text(encoding="utf-8")


def _handler_block() -> str:
    """pm_refresh_comp ボタンのハンドラ部分 (key 出現から 40 行) を切り出す."""
    lines = _SRC.splitlines()
    for i, line in enumerate(lines):
        if "pm_refresh_comp_" in line and not line.lstrip().startswith("#"):
            # ボタン定義の少し前 (label/条件) から 40 行
            return "\n".join(lines[max(0, i - 5): i + 40])
    raise AssertionError("pm_refresh_comp ボタンが見つからない")


def test_button_label_mentions_save():
    block = _handler_block()
    assert "ライバル保存 + 価格再取得" in block, (
        "ボタン名に『保存』が含まれていない (user 要望: 保存される"
        "ことがわかる名前)"
    )


def test_handler_order_upsert_bump_refresh_rerun():
    block = _handler_block()
    i_upsert = block.find("upsert_listing_competitors(")
    i_bump = block.find("bump_db_version()")
    i_refresh = block.find("refresh_competitor_pricing(")
    i_rerun = block.find("st.rerun()")
    assert -1 not in (i_upsert, i_bump, i_refresh, i_rerun), (
        f"ハンドラに必須呼出が欠落: upsert={i_upsert} bump={i_bump} "
        f"refresh={i_refresh} rerun={i_rerun}"
    )
    assert i_upsert < i_bump < i_refresh < i_rerun, (
        "実行順序が upsert → bump_db_version (W134) → refresh → rerun "
        "になっていない"
    )


def test_visible_without_pricing_rows():
    """初回登録 (登録済ライバル 0 件) でもグリッド入力があればボタン表示."""
    block = _handler_block()
    assert "(pricing_rows or comp_list)" in block, (
        "表示条件が pricing_rows 単独に戻っている (初回登録でボタンが"
        "出なくなる)"
    )
