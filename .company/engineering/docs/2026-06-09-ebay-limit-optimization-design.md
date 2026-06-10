# eBay $1M 販売上限 最適化 設計書 (2026-06-09)

3者レビュー (assistant Opus 4.8 / research-brain / Codex gpt-5.5) を経た確定設計。
出典検証済み (eBay Selling limits 公式 / eBaymag FAQ / Best Match / Out-of-Stock Control)。

## 背景・問題
- eBay 月間販売上限 $1M に到達 → daily_relist (end→sell similar) を走らせると上限超過で relist 失敗 → 毎晩 listing 喪失。**daily_relist 停止中 (7/1 枠リセットまで再開不可)**。
- eBaymag (無料多国出品ツール) で US本体を他7国 (UK/DE/FR/IT/ES/AU/CA) に複製。全商品全国展開で枠が膨張 (active snapshot 実質 $1.16M 相当)。

## 核心発見 (検証済み)
**eBay 月間上限は「出品数量を1個ずつ」カウントする** (公式: GTC 出品の available quantity が毎月 active allowance を占有)。
- active 506件のうち **qty≥20 が20件 (ほぼ PLOTTER 革バインダー) で US本体出品額の50% ($291K/$578K) を占有**。qty 50+ が19件。
- これら高数量品は **市場で年0-2個しか売れていない** (Terapeak 365日 sold)。在庫保有しているが全数 (84-96個) を出品。
- 試算: 出品数量を10にcap → US本体 $578K→$323K (−44%)。eBaymag (×8市場) で効果8倍 = **$2M+ 枠解放**。売上影響ゼロ (年0-2個しか売れないため)。

→ **$1M超過の真因は「国の出しすぎ」より「高数量品 × 全市場」。最大レバーは数量キャップ。**

## 最適化 4レバー (インパクト順)

### レバー① 数量キャップ (最大・最優先)
低回転×高数量品の **出品数量** を回転連動の少数に制限 (在庫は減らさず出品数量のみ)。
- **qty 初期値ルール** (Terapeak 365日 sold 基準):
  - sold 0-2個 → qty 3-5
  - sold 3-6個 → qty 5-10
  - sold 7個以上 → 30日販売分 + 安全在庫
- **Out-of-Stock Control 必須** (qty 0 で listing 終了 = 履歴/順位喪失を防ぐ)。**qty 0 放置回避 + 補充アラート**。
- eBaymag 同期: **US本体数量を source of truth** に。eBaymag 各国版を個別編集しない (FAQ 準拠)。
- ⚠️ **7/1前に 1 SKU で実測必須** (数量変更 → Seller Hub の remaining limit がどう動くか検証してから一括。今月の即時回復は eBaymag FAQ 上不確実、7/1 リセット後は確実)。

### レバー② 国の選択 (出品国プラン v2)
全国68 / 優先国190 / 出さない248 (別ファイル `ebaymag_country_plan_v2_2026_06_09.csv`)。$400K規模の補助レバー。
- 全国 = PLOTTER/Maxell/Google 全件 + ランクS×global/mixed。
- 優先国 = 実買い手国 (Terapeak countries_breakdown) の非US上位1-2サイト。
- ⚠️ 実行前提: eBaymag の商品×国 操作粒度を実画面確認 (未確認)。DE は LUCID/EPR + VAT/IOSS 整備が先。

### レバー③ 枠効率スコア + 月次入替 (動的最適化)
**Codex 修正反映: 分母に価格を必ず入れる (数量だけだと高額低回転品を過小罰則 = PLOTTER再発)**。

```
枠効率スコア = 非US粗利額 ÷ 予約枠額
            = 非US粗利額 ÷ (出品数量 × 現地価格USD × 国数)
```
- 仕入値ある12%は実粗利、残りは暫定で 非US売上額 (= 非US sold × price) を分子に。
- 3用途で式を使い分け:
  - 国選択: `非US粗利 / 非US予約枠`
  - US本体数量: `全市場sold速度 / US予約枠`
  - 全体配分: `限界粗利 / 限界予約枠`
- **月次で再ランク → 予算内で数量・国数を貪欲割当 → 低スコアは絞る。固定リストにせず入替制**。

### レバー④ unknown 実験枠
高額 unknown (Terapeak sold<3) を **月5-10件だけ DE/UK 1国で30日テスト** → watch/click/問合せ/売上を見て継続判断 → 売れたら昇格。全除外でなく小さく試す。

## 運用 KPI (新規)
- **予約枠レポート日次化**: SKU別 `reserved_limit = price × qty × site_count` を可視化。売上でなく「**枠占有**」を KPI 化。MonoDeck に枠占有ダッシュボード。

## 構造的解決 (並行)
- **販売上限 増枠申請**: $1M到達・在庫実在・低defect・発送実績を材料に eBay Japan 窓口へ交渉 (※ 2026-06 時点で $2M 増枠は却下済 = 当面期待薄だが再交渉余地)。

## 実装順序 (Codex 推奨)
1. 高数量・低回転 SKU を即 cap (レバー①) ← **1 SKU 実測 → 全件**
2. 高額・低非US実績 SKU の国を削る (レバー②)
3. 月次で数量と国数を再配分 (レバー③)
4. unknown を小さく試す (レバー④)

## レビュー記録
- assistant (Opus 4.8): 枠消費内訳の定量化、数量が国より効くと発見
- research-brain (Opus 4.8): 先行(本体直販空白国)/遅行(実買い手国)指標の使い分け、消費者ブランドvsB2B計測器の2系統
- Codex (gpt-5.5): **要修正 (方向性は強く採用可)**。スコア分母に価格必須、数量cap前の1 SKU実測必須、Out-of-Stock Control/予約枠KPI/増枠申請を追加
