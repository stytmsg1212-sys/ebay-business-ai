"""仕入先候補カード HTML レンダラ (純関数、DB アクセスなし).

W212-supplier-card-cleanup (2026-06-04):
app.py L5531-5557 の巨大 ``st.markdown`` を抽出し、表示専用ヘルパに分離.
money-direct path (採用/不採用 eBay ReviseItem・在庫 revise・候補 DB、SKU 規約)
は一切変更しない. **表示・配置・CSS のみ** の整理.

設計指針:
- 純関数 (caller が ``ebay_price_usd`` / ``ebay_price_jpy`` / ``profit_jpy`` /
  ``parent_status`` を取得済の値として渡す). DB を引かない.
- 採算 2 軸 (eBay 出品 $ / 仕入 ¥ → 利益 +¥(率)) を 2 行レイアウトで強調.
- 利益正負で緑/赤 (3 色: profitable/loss/N/A).
- score / 採算 / model 評価を inline badge で可視化.
- self-contained CSS (ヘルパ先頭で一度だけ ``<style>`` 出力. 商品管理 pm-*
  注入には非依存).
"""
from __future__ import annotations

from html import escape as _esc
from typing import Optional


# ---------------------------------------------------------------------------
# CSS (self-contained、初回呼出時のみ HTML 文字列に prefix される)
# ---------------------------------------------------------------------------

# scoped class prefix: sc-*  (supplier-card)
_CARD_CSS = """
<style>
.sc-card{
  border:1px solid rgba(120,180,255,0.30);
  border-radius:6px;
  padding:10px 14px;
  margin:6px 0;
  background:rgba(20,30,50,0.40);
}
.sc-row1{
  display:flex;
  gap:10px;
  align-items:center;
  flex-wrap:wrap;
  font-family:Share Tech Mono,monospace;
}
.sc-score{
  font-size:22px;
  font-weight:700;
  min-width:42px;
  text-align:center;
}
.sc-badge{
  font-size:11px;
  padding:2px 8px;
  border-radius:10px;
  letter-spacing:0.3px;
  white-space:nowrap;
}
.sc-badge-mute{
  color:rgba(200,200,200,0.65);
  background:rgba(255,255,255,0.04);
}
.sc-badge-info{
  color:rgba(180,220,255,0.85);
  background:rgba(120,180,255,0.10);
}
.sc-badge-good{
  color:rgba(118,255,3,0.95);
  background:rgba(118,255,3,0.10);
}
.sc-badge-warn{
  color:rgba(255,180,80,0.95);
  background:rgba(255,180,80,0.10);
}
.sc-iid code{
  background:rgba(120,200,255,0.12);
  padding:1px 6px;
  border-radius:3px;
  color:rgba(200,230,255,0.95);
  user-select:all;
  cursor:text;
  font-size:11px;
}
.sc-title{
  margin-top:6px;
  font-size:13px;
  color:rgba(255,255,255,0.92);
  line-height:1.35;
}
.sc-money{
  margin-top:8px;
  display:flex;
  gap:14px;
  align-items:baseline;
  flex-wrap:wrap;
  font-family:Share Tech Mono,monospace;
  font-size:13px;
  color:#d8cdb5;
}
.sc-money .sc-ebay{color:#76ff03;font-weight:600;}
.sc-money .sc-cost{color:#d8cdb5;}
.sc-money .sc-profit-pos{color:#76ff03;font-weight:700;font-size:14px;}
.sc-money .sc-profit-neg{color:#ff6b6b;font-weight:700;font-size:14px;}
.sc-money .sc-profit-na{color:rgba(200,200,200,0.55);font-size:11px;}
.sc-money .sc-link{margin-left:auto;color:rgba(120,200,255,0.92);}
.sc-money .sc-muted{color:rgba(200,200,200,0.55);font-size:11px;}
.sc-note{
  margin-top:8px;
  padding-top:6px;
  border-top:1px dashed rgba(120,180,255,0.20);
  font-size:11px;
  color:rgba(200,220,255,0.70);
  line-height:1.45;
}
.sc-note-alt{color:rgba(180,255,200,0.78);border-top-color:rgba(180,255,200,0.20);}
.sc-note-junk{color:rgba(255,200,120,0.88);border-top-color:rgba(255,200,120,0.20);}
.sc-recovered{
  margin-top:8px;
  padding:6px 10px;
  background:rgba(240,180,48,0.10);
  border-left:3px solid rgba(240,180,48,0.85);
  font-size:11px;
  color:rgba(255,220,120,0.95);
}
.sc-imgpair{
  display:flex;
  gap:8px;
  margin-top:8px;
  align-items:flex-start;
}
.sc-imgpair-cell{
  flex:1 1 0;
  max-width:48%;
  display:flex;
  flex-direction:column;
  align-items:center;
  gap:4px;
}
.sc-imgpair-cell img{
  max-height:150px;
  max-width:100%;
  object-fit:contain;
  border-radius:4px;
  background:rgba(255,255,255,0.04);
}
.sc-imgpair-placeholder{
  height:150px;
  width:100%;
  background:rgba(100,120,150,0.12);
  border:1px dashed rgba(120,160,200,0.25);
  border-radius:4px;
  display:flex;
  align-items:center;
  justify-content:center;
  font-size:11px;
  color:rgba(180,200,220,0.50);
}
.sc-imgpair-caption{
  font-size:11px;
  color:rgba(200,210,230,0.65);
  text-align:center;
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
  max-width:100%;
}
</style>
"""


# 同一 Streamlit run 内で複数 card 呼ばれても <style> は 1 回で十分.
# render 側は state を持たせず、文字列の先頭に毎回 inline でも OK だが、
# Streamlit は同じ HTML を fragment 内で繰り返し render する (採用/不採用
# button rerun) ため、テスト容易性 (純関数) を優先して inline 同梱.
# CSS 重複は HTML 仕様上 last-wins で無害.


# 状態日本語化 (app.py 側の _STATUS_JA と同義、循環 import 防止のため独立).
_STATUS_JA_LOCAL = {
    "pending": "未判定",
    "accepted": "採用済",
    "rejected": "不採用",
    "applied": "反映済",
}


def _score_color(score: int) -> str:
    if score >= 80:
        return "rgba(118,255,3,0.95)"
    if score >= 60:
        return "rgba(240,200,48,0.95)"
    return "rgba(255,128,128,0.95)"


def _model_badge(eval_model: str) -> str:
    """評価モデル名から inline badge HTML を返す (該当なしは空文字)."""
    m = (eval_model or "").lower()
    if "opus" in m:
        label, color, bg = "Opus 4.7", "rgba(196,128,255,0.95)", "rgba(140,80,200,0.18)"
    elif "sonnet" in m:
        label, color, bg = "Sonnet 4.6", "rgba(120,200,255,0.95)", "rgba(80,140,200,0.15)"
    elif "haiku" in m:
        label, color, bg = "Haiku 4.5", "rgba(180,220,200,0.85)", "rgba(100,140,120,0.15)"
    else:
        return ""
    return (
        f'<span class="sc-badge" style="color:{color};background:{bg};'
        f'font-weight:600;">{_esc(label)}</span>'
    )


def render_supplier_card_html(
    row: dict,
    ebay_price_usd: Optional[float],
    ebay_price_jpy: Optional[int],
    profit_jpy: Optional[float],
    parent_status: str,
    ebay_image_url: Optional[str] = None,
    candidate_image_url: Optional[str] = None,
) -> str:
    """1 候補カードの HTML 文字列を返す (Streamlit ``st.markdown`` 用).

    Args:
        row: ``supplier_candidates`` 1 行 (DB row dict). ``match_score`` /
            ``candidate_title`` / ``candidate_url`` / ``candidate_price_jpy`` /
            ``source_platform`` / ``status`` / ``profitable`` / ``alt_listing_possible`` /
            ``junk_likely_untested`` / ``match_reasoning`` / ``alt_listing_note`` /
            ``ebay_item_id`` / ``sku`` / ``eval_model`` を参照.
        ebay_price_usd: 親 listing の現在 USD 価格 (caller が
            ``get_ebay_listing_by_item_id`` で取得済). 不明時 None.
        ebay_price_jpy: 上記 JPY 換算 (caller 側の為替). 不明時 None.
        profit_jpy: ``row['profit_jpy']`` をそのまま渡す (DB 値). None 可.
        parent_status: 親 listing の ``source_status`` (``"在庫有"`` で復活警告
            出す). caller の ``_sup_parent_status`` dict から渡す.
        ebay_image_url: W258/Phase-B (2026-06-11) eBay 出品の 1 枚目画像 URL.
            None の場合はプレースホルダを表示. 両方 None なら imgpair ブロック非表示.
        candidate_image_url: W258/Phase-B (2026-06-11) 仕入先候補の 1 枚目画像 URL.
            None の場合はプレースホルダを表示. 両方 None なら imgpair ブロック非表示.

    Returns:
        ``str``: ``<style>`` + ``<div class="sc-card">...</div>`` を含む HTML.

    Note:
        DB アクセスを行わない (純関数、unit test 容易). caller は本関数の
        戻り値を ``st.markdown(..., unsafe_allow_html=True)`` に渡す.
    """
    score = int(row.get("match_score") or 0)
    platform = row.get("source_platform") or "?"
    price = row.get("candidate_price_jpy")
    title = row.get("candidate_title") or "(タイトル未取得)"
    url = row.get("candidate_url", "") or ""
    reasoning = row.get("match_reasoning") or ""
    alt_note = row.get("alt_listing_note") or ""
    junk_flag = int(row.get("junk_likely_untested") or 0)
    profitable = int(row.get("profitable") or 0)
    status = row.get("status", "pending")
    sku = row.get("sku", "?") or "?"
    eid = row.get("ebay_item_id") or ""
    is_alt = bool(row.get("alt_listing_possible"))

    # ── Row 1: メタ (score + 識別子 + badge) ──
    score_html = (
        f'<span class="sc-score" style="color:{_score_color(score)};">'
        f'{score}</span>'
    )
    sku_html = (
        f'<span class="sc-badge sc-badge-info">SKU {_esc(str(sku))}</span>'
    )
    iid_html = (
        f'<span class="sc-badge sc-badge-info sc-iid">ItemID '
        f'<code>{_esc(eid)}</code></span>'
        if eid else
        '<span class="sc-badge sc-badge-mute">ItemID -</span>'
    )
    platform_html = (
        f'<span class="sc-badge sc-badge-mute">{_esc(str(platform))}</span>'
    )
    profitable_html = (
        '<span class="sc-badge sc-badge-good">採算OK</span>'
        if profitable else
        '<span class="sc-badge sc-badge-warn">採算注意</span>'
    )
    status_html = (
        f'<span class="sc-badge sc-badge-mute">'
        f'{_esc(_STATUS_JA_LOCAL.get(status, str(status)))}</span>'
    )
    model_html = _model_badge(row.get("eval_model") or "")

    row1_html = (
        f'<div class="sc-row1">'
        f'{score_html}{sku_html}{iid_html}{platform_html}'
        f'{profitable_html}{status_html}{model_html}'
        f'</div>'
    )

    # ── 商品名 ──
    title_html = f'<div class="sc-title">{_esc(title)}</div>'

    # ── Row 2: 採算 2 軸 (eBay $ → 仕入 ¥ → 利益 +¥(率)) ──
    if ebay_price_usd:
        ebay_part = (
            f'<span class="sc-ebay">eBay出品 ${ebay_price_usd:.2f}'
            + (f' (¥{ebay_price_jpy:,})' if ebay_price_jpy else '')
            + '</span>'
        )
    else:
        ebay_part = '<span class="sc-muted">eBay出品: 未取得</span>'

    cost_part = (
        f'<span class="sc-cost">仕入 ¥{price:,}</span>'
        if price else
        '<span class="sc-muted">仕入: 不明</span>'
    )

    # 利益正負で 3 色:
    #   pos (green): profit_jpy>0 + price 既知 → 緑 + 大文字
    #   neg (red):  profit_jpy<=0 + price 既知 → 赤
    #   na  (mute): 別SKU出品機会 (profit 計算対象外) / 不明
    if profit_jpy is not None and price and price > 0:
        rate = (float(profit_jpy) / float(price)) * 100
        if profit_jpy > 0:
            profit_part = (
                f'<span class="sc-profit-pos">'
                f'利益 +¥{int(profit_jpy):,} ({rate:.0f}%)</span>'
            )
        else:
            profit_part = (
                f'<span class="sc-profit-neg">'
                f'利益 ¥{int(profit_jpy):,} ({rate:.0f}%)</span>'
            )
    elif is_alt:
        profit_part = (
            '<span class="sc-profit-na">'
            '利益: 別SKU出品機会 (計算対象外)</span>'
        )
    else:
        profit_part = '<span class="sc-profit-na">利益: 算出不可</span>'

    link_part = (
        f'<a class="sc-link" href="{_esc(url)}" target="_blank">'
        '商品ページを開く</a>'
        if url else ''
    )

    money_html = (
        f'<div class="sc-money">'
        f'{ebay_part}{cost_part}{profit_part}{link_part}'
        f'</div>'
    )

    # ── 画像比較カード (W258/Phase-B): eBay 1枚目 × 仕入先 1枚目 ──
    # 両方 None の場合はブロック自体を出さない (空白を増やさない)。
    # 片方 None の場合はプレースホルダ div (高さ 150px 維持、左右ズレ防止)。
    imgpair_html = ""
    _ebay_img = (ebay_image_url or "").strip()
    _cand_img = (candidate_image_url or "").strip()
    if _ebay_img or _cand_img:
        # eBay 側セル
        if _ebay_img:
            _ebay_cap = f"eBay ${ebay_price_usd:.2f}" if ebay_price_usd else "eBay"
            _ebay_cell = (
                f'<div class="sc-imgpair-cell">'
                f'<a href="{_esc(_ebay_img)}" target="_blank" rel="noopener">'
                f'<img src="{_esc(_ebay_img)}" alt="eBay" loading="lazy">'
                f'</a>'
                f'<div class="sc-imgpair-caption">{_esc(_ebay_cap)}</div>'
                f'</div>'
            )
        else:
            _ebay_cell = (
                '<div class="sc-imgpair-cell">'
                '<div class="sc-imgpair-placeholder">画像未取得</div>'
                '<div class="sc-imgpair-caption">eBay</div>'
                '</div>'
            )
        # 仕入先側セル
        _cand_price = row.get("candidate_price_jpy")
        _cand_cap = f"¥{_cand_price:,}" if _cand_price else "仕入先"
        if _cand_img:
            _cand_cell = (
                f'<div class="sc-imgpair-cell">'
                f'<a href="{_esc(_cand_img)}" target="_blank" rel="noopener">'
                f'<img src="{_esc(_cand_img)}" alt="仕入先" loading="lazy">'
                f'</a>'
                f'<div class="sc-imgpair-caption">{_esc(_cand_cap)}</div>'
                f'</div>'
            )
        else:
            _cand_cell = (
                '<div class="sc-imgpair-cell">'
                '<div class="sc-imgpair-placeholder">画像未取得</div>'
                f'<div class="sc-imgpair-caption">{_esc(_cand_cap)}</div>'
                '</div>'
            )
        imgpair_html = (
            f'<div class="sc-imgpair">{_ebay_cell}{_cand_cell}</div>'
        )

    # ── Note (判定理由 / 別出品提案 / ジャンク警告 / 仕入先復活警告) ──
    reasoning_html = (
        f'<div class="sc-note">判定: {_esc(reasoning)}</div>'
        if reasoning else ""
    )
    alt_html = (
        f'<div class="sc-note sc-note-alt">別出品提案: {_esc(alt_note)}</div>'
        if alt_note else ""
    )
    junk_html = (
        '<div class="sc-note sc-note-junk">注意: 「動作未確認ジャンク」の'
        '可能性あり（仕入先は動作確認していないだけの可能性）</div>'
        if junk_flag else ""
    )
    recovered_html = ""
    if parent_status == "在庫有":
        _rec_msg = (
            "仕入先が在庫有に復活しています — この候補は不要の可能性。"
            + ("採用済ですが反映前に「不採用」に戻すことを推奨。"
               if status == "accepted" else "")
        )
        recovered_html = (
            f'<div class="sc-recovered">仕入先復活: {_esc(_rec_msg)}</div>'
        )

    card_html = (
        f'<div class="sc-card">'
        f'{row1_html}{imgpair_html}{title_html}{money_html}'
        f'{reasoning_html}{alt_html}{junk_html}{recovered_html}'
        f'</div>'
    )

    return _CARD_CSS + card_html
