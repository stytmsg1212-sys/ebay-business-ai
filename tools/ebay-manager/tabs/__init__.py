"""
eBay Manager の UI タブ分離モジュール (W9 Phase 5 から導入)

app.py が肥大化しすぎたため、タブ単位で別ファイルに分離する新構造の起点。
各 tab_*.py は `render_tab(s)` 関数を公開する (s = settings dict)。
"""
