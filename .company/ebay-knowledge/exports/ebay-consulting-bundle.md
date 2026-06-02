# eBay 越境EC 相談ナレッジバンドル (MonoHonpo / TOYOTASUMI)

> このファイルは claude.ai の「プロジェクト」ナレッジにアップロードして使う、
> eBay 物販相談エージェント用の知識束です。コンサル相談ログの蒸留 + 自社の
> eBay 規制ルール + 送料/関税リファレンスを 1 ファイルに統合しています。
>
> **時間軸の鉄則**: 新しい情報が古い情報に優先。関税率/送料仕様/eBayポリシー/
> 手数料/ツール挙動は時限性が高い (⏰ マーク)。回答時は発言月を確認し、⏰ 項目は
> 「○○年○月時点の情報なので最新を要確認」と添えること。
> **規制業務 (HSコード分類/通関/VeRO知財判定) の最終責任は人間**。断定せず、
> 必要なら CBP CSMS / eBay公式 / 配送会社 / 通関士への確認を促すこと。


> 生成日時: 2026-06-02 10:53 / 本スクリプト: tools/ebay-manager/scripts/build_consulting_bundle.py



==============================================================================
# 【コンサル知見 コンパイル版 (時間軸統合・最重要)】
# source: C:\Users\gucch\projects\claude\.company\ebay-knowledge\topics\consultant-kb-compiled.md
==============================================================================

---
layer: 2
updated: 2026-06-02
sources:
  - chatwork room 409017841 (eBay越境ECコンサル相談グループ, 2025-09-01〜2026-06-02, 2256msgs)
  - 主回答者(★コンサル): 田中利季 / Yoshi。準権威: 松木祐磨・達也 (上級member)
metadata:
  wiki_type: reference
  raw: .company/ebay-knowledge/consultant-logs/chatwork_room409017841_2256msgs.json
  distilled_chunks: .company/ebay-knowledge/consultant-logs/_chunks/
---

# コンサル知見 コンパイル版 (eBay越境EC実務)

> **出典**: コンサル相談グループ chatwork (2025-09〜2026-06、田中利季さん/Yoshiさんが主回答者)。
> 9ヶ月分2256メッセージを6期間に分割し Opus 4.8 で蒸留 → 本ファイルで時間軸統合。
> **これは MonoHonpo/TOYOTASUMI の業務判断で read-first 参照する compiled wiki (Q7)**。

## ⚠️ この知識の使い方 (時間軸の鉄則 — user 指示 2026-06-02)

1. **新しい情報が古い情報に優先**。関税・送料・ポリシー・手数料・ツール仕様は日々変わる。下表の「変更タイムライン」と各項目の発言月を必ず見る。
2. **⏰ マーク = 時限性**。関税率/閾値/配送各社仕様/eBayポリシー/手数料/ツール挙動は **使う前に一次情報(CBP CSMS / eBay公式 / 配送会社)で再確認**。古い数値をそのまま価格・出荷判断に転用しない (本プロジェクトの「関税時代区分」セルフチェックと同義)。
3. **★コンサル発言 = 権威。member発言 = 論点**(達也さんのネガ削除手順のように田中さんが是認したものは準権威)。
4. **規制業務(HS分類/通関/知財VeRO判定)の最終責任は人間**。コンサルも「最終は通関士/eBay公式に確認」を繰り返している。自動BLOCK化しない (Cal Rueb red flag #3)。
5. **MonoHonpo 既存ルールと衝突する助言あり**(§6)。衝突時は MonoHonpo ルールを優先。

---

## 1. ⏰ 時限性ウォッチリスト (使用前に必ず再確認)

| 領域 | 最新の理解 (発言月) | 過去の経緯 / 注意 |
|---|---|---|
| **米国DDP** | 米国向けはDDP前提=関税未払いは起きないがセラー負担。US宛は送料安のFedEx推奨 (2026-01〜02 ★松木) | 2025-10-17 にDDP義務化発効。それ以前はDDU可だった |
| **DDU/DDPデフォルト** | 一度「強制DDP」になったが **2026-01頃にDDUがデフォルトに戻った** (member井上)。米国以外はDDU可 | DDP時代のVAT二重課税はOC返金困難 → 今後はDDU+VAT PAID明記 |
| **暫定関税率** | 2026-02-20 通商法122条で**全世界一律10%・150日暫定**、日本製は当面10%。15%に戻る可能性も言及 (2026-03 ★Yoshi, `ebay.co.jp/tariffs`) | 2025-09時点は「日本製15% / 中国製30%」の素朴理解だった。**⚠️ MonoHonpoのSection 232 (Annex I-A 50%/I-B 25%派生品) とは別軸 — §6参照** |
| **書籍関税** | HS 4901990093 等の書籍は米国0%のはずだが**実際に課税された実例あり**(2026-05、原因未解決)。CPaSS HTSデータベースは2026年版に更新済 | — |
| **CPaSS関税概算** | 商品単価の概ね15%目安、予測関税と実関税の差額はCPaSSが自動返金(申請不要、到着後約1ヶ月)(2026-01) | WorldTariffはCPaSS基準とズレる。実関税確定は発送から約30日 |
| **署名オプション(DSR)** | **$750以上は署名必須**(これ未満は不要)。Economyは署名不可なので$750以上はExpedited/クーリエ (2025-12〜2026-04) | — |
| **SpeedPAK Economy制限** | 申告額150EUR未満まで / 電池・帯電デバイス(イヤホン/スピーカー/腕時計)発送不可の国あり / 署名なし / 燃料割増・繁忙期追加なし(料金内包) (2026-04) | リチウムイオン充電ケース=航空危険物でDHL不可、FedExは可な時あり(3個以上申請)。**そもそも危険物は販売非推奨**(松木) |
| **OrangeConnex稼働** | **2026-06時点 ドバイ等中東でSpeedPAK発送中止中**。代替=elogi→FedEx集荷 (member佐藤) | 中東/ドバイはeBay上で非表示=販売不可の時期あり |
| **発送代行** | WML(eパケットライト対応)/ オークレボ(2026-05時点eパケットライト非対応) (★Yoshi) | — |
| **eBayフィードバック削除** | **2025-04ポリシー更新で厳格化**。フリーリターン後のネガが自動削除されにくくなり、CS依頼でも消えにくい。関税理由・誹謗中傷は今も削除可能性高い (2025-12〜2026-05) | それ以前はリターンクローズで自動削除されやすかった |
| **Terapeak制限** | 1日約250件で制限(アカウントID単位、IPでない)、約1日で解除 (2026-01〜04) | — |
| **eBaymag各国手数料** | 各国Site手数料 +3〜8%上乗せ、豪のみ-2% (2026-01)。US在庫と連動、新規登録は制限中(代替WebInterpret) | — |
| **在庫0自動End** | quantity=0が **180日連続でEnd**。Sold積み維持は数量を1→0に戻してリセット (2026-02) | — |
| **日本郵便 米国向け** | $100以下贈与品以外は停止(2025-12)。今年から書留廃止=追跡付き不可の国も (2026-01) | 2025-08末に米国向け小型包装物停止 |

---

## 2. 変更タイムライン (supersession — 古い前提が覆った点)

- **2025-04**: eBayフィードバック削除ポリシー更新 → ネガ自動削除が厳格化(後に2025-12〜2026に体感顕著)。
- **2025-08末〜09**: デミニミス(少額免税)撤廃。日本郵便 米国向け小型包装物停止。eBay JapanがDDP発送必須メール配信(9/17頃)。→ pre_tariff → transition 境界。
- **2025-10-17**: **米国向けDDP義務化 発効**。DDU発送だとセラー保護が受けられなくなる(米国のみ強制、他国はDDU可)。
- **2025-11〜12**: post_tariff 定着。CPaSS関税概算15%・実関税確定30日後・$750署名・150EUR境界 等の運用が固まる。
- **2026-01**: **DDUがデフォルトに戻る**(強制DDPから緩和)。予測関税とのCPaSS自動差額返金。CPaSSユーザー1.8万人超で関税請求遅延。
- **2026-02-20**: 通商法122条で全世界一律10%・150日暫定関税。
- **2026-03**: ドル/円表記バグ(eBay既知障害)。CPaSS HTSデータベース2026年版更新で旧コード無効化。★田中が未着ケースの助言を自己訂正(「未着で負けるとDefect→先回り全額返金でDefect回避」)。
- **2026-04**: eBay大規模障害(4/27 ログイン不可)。
- **2026-05〜06**: OrangeConnexがドバイ等で発送中止 → elogi→FedEx代替。ネガ削除実務知見が更新(達也さんの Abusive Buyer/Feedback Extortion 引用手順を田中さんが是認)。

---

## 3. 恒久ベストプラクティス: CS・クレーム・返品・Dispute (最重要・最頻出)

**貫く原則 = 「ケースの勝ち負けより Defect / サービスメトリクス悪化の回避を最優先」**。

### 未着ケース (Item Not Received)
- 配達済み(追跡が「到着」/GPS・置き配写真あり)で**期限内発送**ならほぼセラー勝利。「配達(試行/attempted)」ステータスはバイヤー責任。
- エスカレーション後、`https://ocswf.ebay.com/mudcwf?deptName=USMemberResolution` にケースID+追跡済みスクショ+追跡番号を提出(Document type=General Information)。3営業日でセラー有利クローズ。
- **問い合わせは eBay US へ。eBay Japan に相談すると「返金しろ」と言われがち**。
- 辻褄が合わないケース(未着と言いつつ破損主張等)は追跡番号アップして放置 → eBayが自動クローズ。
- **未着で負けるとDefectが付く** → 負けそうなら先回りで全額返金してDefect回避(2026-03 ★田中が自己訂正した重要判断軸)。後日届いたらeBayにアピール(45日以内に Appeal a case decision)。
- CPaSS発送はハンドリングタイム内なら追跡アップでクローズしてもDefect/メトリクスにカウントされない。

### リターン / 返品リクエスト
- **リターンケースは勝ち負けに関係なくDefectは付かない**(全額返金になるだけ)。Payment Disputeは内容次第で負け得る。
- リターン品が戻らない時: **返送ラベルを発行・アップロード**すれば、バイヤーが返送しなければ自動でセラー有利クローズ。ラベル提出済みなら一部返金する必要は一切ない。
- リターン品が戻ったら**到着2日以内に自分で返金処理**。怠ってeBay強制クローズされるとDefect。
- フリーリターン期間=**到着から30日**。30日超過なら返品拒否OK。期間内にケースを開かれたらメッセージ中でも期限内に返金 or 返送ラベルアップが義務。
- 部分返金の妥当%: 開封未使用品は eBay記事(article95)基準で **5-10%**、控除上限は50%。低単価品は返送させると関税+送料で赤字 → 5-7割一部返金で再購入誘導が損失最小。金額大なら50%チャレンジも可(非承認リスクあり)。
- send refund の返金理由はeBay評価に影響しない(★Yoshi)。ただし member慣行は「Other adjustment」を選ぶ。Item not as described でリターンを挙げられるとサービスメトリクス悪化 → 部分返金は send refund 先行が無難。

### Payment Dispute (カード決済会社への異議)
- challenge the dispute を選び「発送前は正常動作」の流れ。動作品写真がなければ仕入れ元画像でも可。
- 提出資料 = **署名入り配達証明 + 問題解決を試みたメッセージのスクショ**(バイヤー返信なしでも「解決しようとしたが返事がない」で十分)。破損写真依頼文は「配送事故と断定しない」表現が安全。
- カード会社判定でセラー不利でも、その後 **eBayにアピールするとセラー保護・補填される場合あり**(負けても再審の価値)。
- 発送前にDisputeを上げられたら**発送せず受け入れてクローズ**(クローズ後にキャンセル処理しないと未発送扱い)。Dispute中はキャンセル不可、数日後可能に。

### ネガティブ/ニュートラル評価削除
- **関税理由・誹謗中傷(詐欺師等)は削除可能性高い**。商品説明に「関税はバイヤー負担」明記が前提。eBay Japan / US どちらでも、片方ダメならもう一方(Japanが「不可」と言ってもUS再チャレンジで成功例多数)。
- **テレコ(誤)発送・配送遅延起因のネガは基本消えない**。
- 達也さんのネガ削除3要件(田中さん是認): ①セラーが解決に協力したか ②バイヤーに規約違反(Abusive Buyer Policy / Feedback Extortion Policy)があるか ③バイヤーが実際に返送したか。「ネガ削除を条件に返金強要」は Feedback Extortion 違反。
- 削除交渉を何度送ってもアカウントに影響なし。

### 詐欺・トラブル類型
- すり替え返品 → 返送品開封は必ず動画撮影、異なる物ならeBay通報+ネガ削除申請(警察相談→番号提出で返金される可能性)。
- 「PayPal返金してくれたらポジティブ残す」=詐欺の可能性高い、リターンで返送させる。
- 怪しいバイヤー(登録直後・評価0)のオファーは受けずブラックリスト。Payment policyは Immediate payment 必須。

---

## 4. 恒久ベストプラクティス: 出荷・配送

- **配送会社の関税挙動**: DHL=関税未払いなら配達しない(セラー請求されにくい、EU/中東向けに有利)。FedEx=未払いでも配達しセラーに後日請求(立替)。UPS=原則関税後配達だが稀に先配達。→ EU/中東はDHL、US(DDP)はFedEx推奨(★松木 2026-02)。
- **$750以上は署名オプション必須**(Economyは署名不可→Expedited/クーリエ)。
- **電池・危険物**: SpeedPAK Economyは電池含有不可。リチウムイオン充電ケース(ワイヤレスイヤホン)は航空危険物でDHL不可・販売自体非推奨。機器内蔵100Wh未満なら可。
- **PO Box**: クーリエ(FedEx/DHL)は不可で除外必要。CPaSS Economy(US宛)/日本郵便EMSなら可。
- **返送**: SpeedPAK EconomyはCPaSSから返送ラベル発行不可 → 直契約FedEx/DHL or Zipx/elogi。**US宛返品はCPaSSのアメリカ倉庫に返送可**(事前にOrangeConnex申請、しばらく無料)。FedEx直契約は輸出入共通で返送ラベル作成可・最安。
- **補償**: FedEx紛失上限約$100(or 1kg$20の高い方)/受領21日以内。DHLは1kgあたり26 SDR(実例22,070円補償)/30日書面。Economyは商法576条市場価格/60日。破損補償は緩衝材8cm(3インチ)以上が基本条件(配送会社過失を物証+バイヤー協力で立証すれば8cm未満でも勝てた例あり)。
- **直契約は維持推奨**: CPaSS多用でFedEx直契約の利用頻度が下がり返品輸入コストが5000円→50000円に上昇した例。
- **重量入力**: CPaSSは梱包込み総重量を単位重量欄に入力(梱包重量は別途入力しない)。
- **発送通知タイミング**: Economyでもコンビニ/郵便局持込日に発送通知でよい(OC倉庫到着待ち不要)。
- **同梱**: 同一バイヤーの複数注文はHirogete/CPaSSで同梱可(同一追跡番号を複数注文にアップロード成功例あり)。
- **SpeedPAKポリシーのまま直契約FedEx/DHL発送OK**(FICP or IP選択なら速度同等)。

---

## 5. 恒久ベストプラクティス: アカウント健全性 / 出品 / 仕入れ / 価格

### アカウント健全性・キャンセル・ポリシー
- **キャンセル理由とアカウント影響**: 在庫切れ(セラー都合)は悪影響=Defect1個。バイヤー都合・住所都合は影響なし。**バイヤーからキャンセルリクエストを送ってもらう**のが原則(承認はアカウント影響なし)。「在庫がない」と言わず「期限内発送できるか不明なので急ぐならキャンセル推奨」と誘導。
- **在庫切れ対処序列**: ①期限まで待ち追跡未アップ ②テレコ発送(似た安物を送りトラッキングアップ→後日 Other adjustment 返金、CPaSS経由ならメトリクス非カウント、頻発NG・未着/リターン返金と半々で混ぜる) ③セラー都合キャンセル(Defect余裕あれば最もクリーン)。※テレコ発送・低額申告再送はグレー運用、MonoHonpoでは人間判断必須。
- Handling Time上限=**最大40日**。Self Pickup/購入者ラベル発送は非推奨($50以下のみ可、セラー保護喪失リスク、中国系バイヤーが提案しがち)。
- 資金保留(Payment hold)対策: 全販売の追跡番号+全出品の仕入証明(Invoice/レシート、**手書き無効・商品名/価格/数量/総額必須**)。**フリマ仕入れは領収書がなく否認リスク**→無在庫モデルの構造的弱点。
- 新規/復帰アカウントは最初2ヶ月ほど$2000以上の高額品を避ける。複数人の直接ログインは現在は問題なし(昔はセキュリティロックあり)。

### VeRO・知財
- **Schedule A訴訟リスク**: 「マーシャと熊」等キャラIPで日本人セラー一括提訴・資金/Payoneer凍結例(2025-10)。**アニメ/映画/ゲームのキャラクターグッズは中古・新品問わず米国で著作権侵害判断リスク** → 仕入候補に出たらuser通知レベルが妥当。
- VeROサイト未掲載でも警告は来る → **警告が来たら取り下げで対処すれば問題なし**。ポリシー違反削除が多発するメーカー品は出品回避。SHIMANO等は「リコール製品」で出品禁止(VeROと別、「ブランド名 リコール」で品番確認)。
- パテントトロール和解フロー(★田中): 知らず出品した謝罪/販売実績なし/生活困窮を強調/自分から示談金を提示せず相手の出方を見る。

### 出品・SEO・在庫
- **重複出品**: 全く同じコンディションの重複はポリシー違反で削除。**コンディション違い(良い中古/悪い中古)・色違い(タイトルに色明記)は別ページでOK**=トラブル回避。
- **Sold積み**: 状態違いは別ページで積む。同ページで積むなら既販売バイヤーに旧ページのスクショ送付+更新告知(配達中の差し替えはクレーム)。
- **draft送料反映バグ**: 出品時draftで米国送料を変えても反映されないケース多発→出品後に再編集で反映(送料無料事故あり)。**→ 送料変更後は必ず GetItem/VerifyAdd で実反映確認**(MonoHonpoのQ1 DoDの正当性を裏付け)。
- description内URLはポリシー違反で非表示→削除で復活。ウォーターマーク/ストアロゴ入り画像は通報リスク(他セラー同画像・白背景でなくてもペナルティなし)。
- **eBay API出品の罠**: File Exchangeは必須欠落でエラー→未出品だが、**eBay APIは必須欠落(中古なのにコンディションなし等)でも出品される**→SEO低下。出品前確認/出品後Bulk Edit修正。

### 仕入れ・リサーチ
- **無在庫監視の盲点**: 「カートに入れる」がLIVEでも実は「取り寄せ商品」で estocks が在庫切れを検知できない(Amazonは初見で判断不可、メルカリ/ヤフオクは販売履歴で予測可)。→ MonoHonpoの楽天/Yahoo/Amazon監視の false-OOS/HIDDEN_STOCK 判定と同根。
- ヤフオク無在庫: 入札なし終了後の再出品が多い→「入札なし時自動停止」OFF推奨、複数仕入先登録。
- 逆VeRO脅迫: 無在庫仕入元(eBayセラー)が「画像無断使用でVeRO通報する」と脅す→取引キャンセル有無は他セラーから判別不可、手元にあれば無視発送・なければ別仕入先。
- 出品基準(初期1年目): けいすけ基準 利益率6% / 利益額600円(撒き餌商品は別)。

### 価格・eBaymag多国展開
- eBaymagはUS在庫と連動(US在庫管理できていればeStocksでOK)、各国版は別リスティング。リミット少ない時は売れやすい英/豪/独に絞る。同期バグ(US0でも他国在庫有)あり→目視チェック。
- 申告価格は商品単体価格(送料・関税・手数料を除く、FOB的)が原則だが「100%正しいとは言えない」運用。**アンダーバリュー依頼は応じない**(税関の輸出事後調査あり)。テンプレ: `If this is not acceptable for you, please feel free to cancel the order request.`
- eBay手数料には消費税10%上乗せ(×1.1)。利益計算は確定CPaSS送料で記録(推定値は不正確)。

---

## 6. MonoHonpo 既存ルールとの整合・衝突 (重要)

| 論点 | コンサルログ | MonoHonpo ルール | 判定 |
|---|---|---|---|
| **原産国記載** | member masatoが「Country/Region of Manufactureは正確記載が安全、中国製は空欄に」と提起(2025-09、★コンサル回答なし=member私見) | **eBay出品文(Title/HTML/Item Specifics)に原産国を絶対記載しない**(関税リスク) | **MonoHonpo優先**。masato説は採用しない |
| **関税率** | 素朴な「日本15%/中国30%」(2025-09)→「全世界10%暫定」(2026-02)等のセラー界隈経験則 | **Section 232 Annex I-A 50%(純金属)/ I-B 25%(派生品・家電HS8516等)/ III 15%** のHS別公式率 + IEEPA reciprocal | **別軸**。ログの経験則を Section 232 buffer に転用しない。家電SKUはI-B 25%直撃なのでログの10-15%目安では不足し得る。高value($500+)はCBP CSMSで再確認 |
| **送料4区分** | SpeedPAK Economy=燃料割増内包/署名なし/$750不可/電池不可、Expedited=燃料割増別途 | US軸差分式 + 4区分 primary_market、`<ShippingType>Flat</ShippingType>` | **整合**。発送可否判定(危険物/$750/Economy制限)を区分ロジックに反映価値あり |
| **Q1 DoD(送料実反映verify)** | draft送料反映バグはコンサルも認める既知事象 | 送料変更後はGetItem実反映確認必須 | **裏付け強化** |
| **無在庫モデルのリスク** | フリマ仕入の仕入証明否認・逆VeRO脅迫・取り寄せ商品の在庫誤判定 | 無在庫(`ebay**_*****` SKU)監視 | **構造的弱点として認識**。監視ロジックの限界(Amazon取り寄せ予測不可)を記録 |
| **SpeedPAKエラー216250** | Economy→Expedited変更で発生、「Claude Code使用中だと出る」疑い(★Yoshi 2026-05) | 本プロジェクトの出品自動化 | **要注意**。SpeedPAKポリシー変更実装時はVerifyAdd再検証必須(Q0) |
| **追加送料の徴収** | 「送料専用listing作成=無形商材で規約違反+追跡二重使用リスク」(★田中) | — | **ガードレール**。システムが送料専用listingを自動生成する実装は禁止 |

---

## 7. ツール・サービス glossary

| ツール | 用途 / メモ |
|---|---|
| **CPaSS (OrangeConnex/OC)** | eBay公式配送。SpeedPAK(DDP)、Economy(US/UK/独/墺)、米国倉庫返送、差額関税自動返金、補償窓口。推定送料=「送料+関税手数料」合算表示(カーソルで内訳) |
| **SpeedPAK** | CPaSS内の配送方法(Economy/Expedited/Express)。DDP対応はこの系統選択が条件 |
| **eLogi** | FedEx/DHL発送代行(自前アカウント不要)。サポート手厚い。OC停止時のFedExラベル代替 |
| **WML / オークレボ(aucrevo)** | 発送代行(承継提携)。オークレボは2026-05時点eパケットライト非対応 |
| **eBaymag / WebInterpret** | 多国展開出品(US在庫連動)。eBaymag新規制限中の代替がWebInterpret |
| **Terapeak** | 競合リサーチ(1日250件制限・ID単位)。MonoHonpoの365日sold判定の元データ源 |
| **eStocks / AIリサーチャー** | 無在庫在庫監視・リサーチ。取り寄せ商品を検知できない盲点 |
| **AlertCrawler** | 新着監視ツール。**MonoHonpoのW148/W206/W207キーワード監視の元ネタ** |
| **Hirogete / Ship&co / Zipx / Zonos** | ラベル作成・返送・郵便局事前関税支払い |
| **File Exchange / eBay API** | 一括出品。APIは必須欠落でも出品される(SEOリスク) |
| **tsukanshi.com / WorldTariff** | HTS/HSコード検索(WorldTariffはCPaSS基準とズレる) |
| **BidMachine / aucfan(オークファン)** | ヤフオク自動入札 |
| **利益計算表/売上管理表(みかっち作)** | 利益・売上管理(月跨ぎでデータ消失バグ、窓口=みかっちさん) |

---

## 8. 人物・運営

- **田中利季さん / Yoshiさん** = ★主コンサル(回答数最多)。**松木祐磨さん / 達也さん** = 上級member(準コンサル級、田中さん是認の助言あり)。
- メンタル: 「問い合わせても無駄なことは多い、自分でコントロールできる範囲に時間を使う」「理不尽は他ビジネスも同じ、eBayは稼げるので続けた方がよい」(★松木)。

---

## 関連
- 本プロジェクトルール: `tools/ebay-manager/CLAUDE.md`(DDP/Section 232/送料/コンディション/通関) / `.claude/rules/` / `reference_shipping_tariff_logic.md` / `section_232_tariff_2026_04.md`
- 原文: `.company/ebay-knowledge/consultant-logs/chatwork_room409017841_2256msgs.json`
- 時限性項目(⏰)は使用前に一次情報照合。新しい相談ログを追加取得したら本ファイルに追記し、矛盾は「変更タイムライン」に追記(両論併記・新しい方優先)。



==============================================================================
# 【eBay 規制業務ルール (出品/通関/DDP/Section232/コンディションランク)】
# source: C:\Users\gucch\projects\claude\tools\ebay-manager\CLAUDE.md
==============================================================================

# eBay Manager (tools/ebay-manager) 固有ルール

このファイルは `tools/ebay-manager/**` 配下を編集する際に Claude Code が自動 load する subdir CLAUDE.md (公式 lazy-load 機能)。
eBay 出品 / 関税 / 送料 / 商品ランク等の **規制業務 rules** を 1 ファイルに集約 (Cal Rueb red flag #3 対応)。

横断 rule (Karpathy 4 原則 / DB migration 冪等性 / silent skip 禁止 / 仕入先判定) は `.claude/rules/` 配下を参照。

---

## 出品ルール

### 価格管理

- USD 基本通貨。JPY 換算が必要な場合は記録時に為替レートを明記
- **米国向けは DDP 出荷 = 関税は売主負担**。Section 232 派生品 25% 直撃で赤字化リスク
- 販売価格に **関税 buffer 必須** (詳細は本ファイル下の「DDP 出荷 / Section 232」section)
- 利益率を必ず記録 (仕入価格 / 販売価格 / 利益率)

### 送料ルール (US 軸差分式 + 4 区分 primary_market)

詳細: `reference_shipping_tariff_logic.md` v1.0 (2026-05-01 制定、業務仕様の権威).

- **計算式**: `各国表示送料 = (各国実送料 - US 実送料) + (DDP 関税 if 米国向け)`
- **DDP 関税**: 米国向けのみ送料欄に上乗せ (商品価格には含めない、ただし US_only 区分は商品価格包含)
- **4 区分**: US_only / mixed_global / global_only / unknown (Terapeak 365 日 sold で listing 単位判定、v2.0 / W110(2) 2026-05-09)
- **暫定運用**: 4 区分別実装は候補 C/D 進行中、現行 `ebay_lister.py` L222 は `price * 0.20` (β fix `<ShippingServiceCostOverrideList>` で BP override 経由)
- XML 必須要素: `<ShippingType>Flat</ShippingType>` 維持
- ShipToLocations: 全 4 区分とも全国必須 (eBay 仕様で US 除外不可)
- 検証: eBay GetItem API で実反映確認 (pytest だけで完了宣言禁止 / Q1)

### Country of Origin / Manufacturer の layer 分離

- **eBay 出品文 (Title / HTML description / Item Specifics)**: Country of Origin / Country of Manufacture / Manufacturer の **いずれも記載禁止** (US Customs が原産国を再計算する根拠を与えない)
- **通関書類 (FedEx Invoice / HS code)**: Manufacturer = **日本代理店** (詳細は本ファイル下の「通関ルール」section)
- 混同事故防止: eBay XML builder は Manufacturer 欄を **空文字列で送出**

### eBay XML 制約 (出品前 自動 validate)

- **Title ≤ 80 文字** (Mojibake 後文字数 / バイト数注意)
- **Item Specifics 各値 ≤ 65 文字**、**Brand / MPN 必須** (Listing Quality 直撃)
- 中古品 (S/A/B/C/D/PO/As-Is) は **ConditionDescription 必須** (これ無いと defect 増)
- VeRO 該当ブランドは `data/vero_brands.json` で事前判定

### SKU 規約 (用途は 2 つのみ、キー使用禁止)

出典: 2026-04-30 SKU 規約改訂。詳細経緯: `feedback_sku_misuse_repeat_offense.md` / `.claude/rules/sku-rules.md`

| 在庫種別 | SKU 形式 | 性質 |
|---|---|---|
| **有在庫** | `stock**` で始まる文字列 (stock:01 / stock1 / stock 等、表記揺れあり) | **同一 SKU を多数 listing が持つのが正常** (在庫種別フラグであって集約キーではない。在庫数・識別は `ebay_item_id` 単位、SKU で束ねない) |
| **無在庫** | `ebay**_*****` (例: `ebayyh_p1221413657` / `ebayme_m32400850054`) | SKU 変換 → 仕入先候補 URL |

**SKU の用途は 2 つだけ** (これ以外で SKU を使うのは絶対禁止):
1. 有在庫 / 無在庫 の判定 (prefix で判別)
2. 無在庫の場合、SKU 変換 → 仕入先候補 URL を得る

**絶対禁止** (違反 = 品質事故。詳細: `.claude/rules/sku-rules.md`):
- ❌ SKU を listing 一意キー (主キー / 重複検出キー) として使う
- ❌ `WHERE sku=?` で 1 listing 特定 / `WHERE sku IN (...)` 複数抽出 / `GROUP BY sku` 集計 / `UNIQUE(sku)` 制約
- ❌ `JOIN ON a.sku = b.sku` / `dict[sku] = listing` / `set(skus)` 重複排除
- ❌ 「同 SKU が複数 listing に存在 = 異常」と判定する

**listing 識別は必ず `ebay_item_id`** を使う (eBay 側の一意 ID、migration v26 で listing 単位化済)。

**判定 OK な使い方** (2026-04-30 user 公認、上記用途 2 つに限定):
- 有/無在庫判定: `sku.startswith("stock")` / `sku.startswith("ebay")` / SQL `WHERE sku LIKE 'stock%' OR sku LIKE 'ebay%'`
- 無在庫の仕入先 URL 変換: `sku_mapping_manager.generate_url(sku)`

ログ / pending_market_changes 通知 = SKU + タイトル併記 (区別容易化のため)。

### 商品の呼称 (UI / 対 user メッセージ)

- ❌ 「stock:01 が...」「ebayyh_p1221413657 を出品」 → SKU は商品識別に使えない (有在庫は同 SKU 共有が正常、無在庫は URL そのもので冗長)
- ✅ 「Audio-Technica ATH-CKS330NC が...」 → `ebay_listings.title` で呼ぶ
- 同タイトル listing が複数存在する場合 = `title (ebay_item_id 末尾 4 桁)` で区別

### eBay ポリシー遵守 (常時参照)

- 出品ポリシー / 禁止商品 / VeRO を出品前に必ず参照
- カスタマーサポート: 返信 **24h 以内**
- **Defect 率最優先** (アカウント停止リスク直結)
- Promoted Listings = 2% (W21 標準)

### 検証 (Q1 DoD 11 ステップに従う)

- 出品 / 価格 / 送料 / 文言変更後は **eBay GetItem で実反映確認** 必須
- pytest PASS のみで完了宣言は K3 違反 = 禁止
- Streamlit 再起動 + Playwright MCP で 1 往復 E2E + DB 直接クエリ

---

## 通関ルール (FedEx / UPS / DHL 通関書類回答)

出典: 2026-04-24 FedEx TRK#xxxxxxxxxxxx ドラフト事故 (馬鹿正直記述で user 厳重注意)

**核心**: 商業転売 / 中国原産 / 高関税対象を **不必要に自認しない**。回答は事実だが戦略的に。

### Manufacturer Name

- **第一選択**: 日本国内の正規代理店 (SKT株式会社 / Razer / Le Creuset Japan K.K. 等)
- **NG**: 中国・東南アジア本社を第一選択にしない。原産国情報を不必要に強調しない
- **例外**: ブランド本社が米国内 (Razer Inc. 米国 HQ 等) なら堂々と記載 OK

### End Use

- **第一選択**: 商品の **実用途** のみ (例: e-reader → "Personal e-book reading device")
- **NG**: 販売チャネル / `resale` / `commercial` / `eBay` を書かない
- 狙い: FedEx の "End Use" は「何に使うものか」を問うており、商取引形態を求めていない

### 素材記述 (鉄鋼・アルミ関税対策)

- アルミ・鉄を含まない商品は **明示的に "No aluminum or steel parts"** と書く (Section 232 派生品の対象外宣言、詳細は本ファイル下の「DDP 出荷 / Section 232」section)

### 定型句 (末尾必須)

```
The shipper is a retailer and is not the manufacturer.
```

→ 発送人 = 製造元でないことを明記、法的立場の切り分け

### HTS コード

- 根拠 Ruling (例: NY N215220) を脚注で引用
- 最終判断は現地通関士に委ねる: `Please verify with your customs broker.`

### 運用

- ドラフトは必ず `.company/daily-operations/fedex-drafts/YYYY-MM-DD-TRK_xxx.md` に保存
- 商品写真は `*-photos/` 配下に DL して添付準備
- **v2 レビュー必須**: 以下 2 経路で過去応答と照合
  - Gmail (MCP or web): `to:paperwork@fedex.com OR to:customs@fedex.com` で過去 1 年検索
  - 0 件時: `.company/daily-operations/fedex-drafts/` 配下の直近 5 件を grep

---

## DDP 出荷 / Section 232

出典: 2026-04-25 TRK#xxxxxxxxxxxx (Netsuken NV-25 / $798) で Section 232 派生品 25% 関税 ~$200 売主負担が判明

### DDP ルール (米国向け原則)

- 米国向け発送 = **DDP (Delivered Duty Paid)** 運用
- 通関時の **全関税・税金・FedEx Disbursement Fee は売主負担** (TOYOTASUMI)
- buyer は追加請求なし (Negative feedback リスクなし、ただし **利益直撃**)
- DDU との混同禁止: DDU=情報提供のみ / DDP=直接損益

### 販売価格設計式

```
販売価格 = 原価 + 国際送料 + 関税 buffer + PLS 2% + eBay fee + 利益
                          └ 最低 15% (IEEPA reciprocal)
                          └ Section 232: I-A=50% (純金属) / I-B=25% (派生品) / III=15% (transitional)
```

### Section 232 該当 HS リスト (3 階層、HS で判定)

#### Annex I-A (50%、Chapter 72-74/76 純金属製品、**重量閾値なし=自動課税**)

- HS 73xx (鉄鋼製品 = ストーブ / 鍋 / フライパン / 保温ジャー)
- HS 76xx (アルミ製品)
- HS 74xx (銅製品)

#### Annex I-B (25% 派生品、Chapter 84-87、**metal weight ≥15% で課税**)

- HS 8516.60.40 (電気炊飯器 / オーブン) — Netsuken NV-25 該当
- HS 8418.10/21/29/30/40 (冷蔵・冷凍)
- HS 8501.64 (特定モーター) / 8504.31-33 (変圧器)
- HS 8415 (エアコン) / 8517.71 / 8544.42-49 (電線)
- HS 8708.xx (自動車部品) / 8716.xx (トレーラー)
- 重量算定根拠を記録 (customs_requests に WORKSHEET 添付)

#### Annex III (15% transitional、~2027-12-31)

- HS 8421.29 (液体ろ過) / 8424.89.90 / 8428.32-70 (コンベア / 産業ロボット)

### IEEPA 重複回避

- **Section 232 該当品は IEEPA reciprocal 15% exempt** (二重課税防止)
- 該当判定後は IEEPA 計算除外、Section 232 のみ適用
- ⚠️ **例外**: semiconductors / automotive parts (HS 8708 等) は IEEPA exempt 対象外、IEEPA 重複適用リスクあり

### 出品判断ルール

- **High-value 商品 ($500+)**: 出品前に customs broker classification 確認推奨
- **同型番リピート出品**: `customs_requests` / freee の該当案件を参照、関税実績を価格反映
- **赤字案件化判定**: 粗利率 30% 未満 + Section 232 該当 = **user に通知して承認待ち** (assistant 自動 BLOCK しない、user 機会損失リスク回避)

### 詳細 KB 参照

`.company/ebay-knowledge/topics/section_232_tariff_2026_04.md` (2026-04-06 改訂、Annex I-A/I-B/II/III/IV 全 HTS リスト、計算ワークフロー、ケーススタディ収録)

⚠️ **最終確認: 2026-04-30 / 高 value 商品 ($500+) 出品時は CBP CSMS で再確認必須** (CBP CSMS は 2-4 週で改訂・追補が出る)

---

## コンディションランク 8 段階

出典: 全 eBay 出品で一貫適用 (W9 individual-listing で Claude 自動推定)。外観 × 動作確認の 2 軸統合。

### 8 段階体系

| Rank | EN | JP | eBay Cond ID | 適用 |
|---|---|---|---|---|
| N | New | 新品・未開封 | 1000 | シュリンク / 工場出荷 |
| S | New (Opened) | 新品同様 | 1500 (※) | 開封済みだが未使用、使用痕なし |

※ **Cond ID 1500 はカテゴリ依存** (Consumer Electronics > Portable Audio & Headphones 等で制限)。GetCategoryFeatures / Taxonomy API で事前確認、不可カテゴリでは **1000 fallback** (条件満たす場合) or **3000 + "Open box" description** に降格。出品時 VerifyAdd で再検証必須 (Q0 サイレントスキップ防止)。

| A | Excellent | 美品・動作確認済 | 3000 | 小さな使用痕、全機能動作 |
| B | Good | 並品・動作確認済 | 3000 | 目立つ使用痕、全機能動作 |
| C | Fair | 使用感あり・動作確認済 | 3000 | 使用感強い、全機能動作 |
| D | Issues | 難あり・動作確認済 | 3000 | 外観/機能に問題、動作するが限定 |
| PO | Power-On Only | 通電のみ、動作未確認 | 3000 | 電源 ON 確認だけ |
| As-Is | As-Is | 未確認 or 部品取り | 7000 | 無保証販売、**理由必須** |

### N vs S 判別

- ✅ 家電量販店の新品シュリンク品 → **N**
- ❓ デッドストック / 未使用だが保管年数長い → **S 推奨**
- ❓ 個人出品の「新品未使用」(開封痕確認困難) → **S 推奨**
- **VeRO リスク** (Apple / Nintendo 等): 非正規ルート品は **S 以下** が安全

### Claude 自動推定 (仕入先日本語キーワード)

| 仕入先表記 | 推定ランク |
|---|---|
| 「新品」「未開封」「シュリンク付き」 | **N** |
| 「新品同様」「未使用」「開封品」 | **S** |
| 「美品」「美品に近い」 | **A** |
| 「良品」「並品」「普通」 | **B** |
| 「使用感あり」 | **C** |
| 「傷あり」「難あり」「訳あり」 | **D** |
| 「通電確認のみ」「通電のみ」 | **PO** |
| 「動作未確認」「ジャンク」「部品取り」「故障」 | **As-Is** |

### ブランド別特例

- **PIONEER Lonesome Carboy 等年代物 AV**: 動作確認必須、ジャンク即 As-Is
- **KEYENCE センサー単体**: ジャンクでもテスト前提で B/C 推定可
- **本 rule 内では 2 例のみ抜粋**。VeRO ブランド (Apple / Nintendo / SONY 等) や Audio/AV/産業機器の判定は必ず `feedback_condition_by_brand.md` を参照。未収載ブランドは N/S 判定前に該当 memory check 必須

### Quick Notes (description aside 冒頭、Rank Definition Table と併設)

- **A/B/C/D**: 具体的動作確認結果。例: `Tested and confirmed working (2026-04). Power on/off: OK / Audio: OK / Bluetooth: OK`
- **PO**: `Powered on, but full function not verified`
- **As-Is**: **必ず理由明示**。例: `No AC adapter for testing` / `PCB burn damage` / `Heavy contamination prevented testing`

### As-Is 出品の XML 必須要件

- eBay XML `<ConditionDescription>` に Quick Notes の As-Is 理由を **必ず転記**
- **65 字以内** (eBay 制約) / 英文 / `As-Is — <reason>` 形式
- 欠落時は VerifyAdd 警告だが通る → buyer 紛争で **Defect 確定リスク** (アカウント停止直結)

### タイトル / description

- タイトルには Rank 表記 **しない** (80 字制限圧迫防止)
- description aside 冒頭に **Rank Definition Table** 含める
- テンプレート: `.company/ebay-knowledge/topics/listing-description-template.md`

---

## 関連 rule (横断)

always-load (`.claude/rules/` 配下):
- `karpathy-principles.md` — Karpathy 4 原則 (K0-K3 常時適用)
- `db-migration-rules.md` — DB 冪等性 (try/except OperationalError、DROP one-shot 化、24h retrospective review)
- `silent-skip-prevention.md` — Q0 サイレントスキップ / 偽装成功 / 逃避修正 絶対禁止

on-demand snippet (`.claude/rule-snippets/` 配下、2026-05-21 hybrid 化):
- `supplier-matching-rules.md` — 仕入先候補判定 (match_score < 60 除外、別 SKU 機会、ジャンク表記判別)



==============================================================================
# 【送料・関税ロジック (US軸差分式 + 4区分 primary_market + DDP)】
# source: C:\Users\gucch\.claude\projects\C--Users-gucch-projects-claude\memory\reference_shipping_tariff_logic.md
==============================================================================

---
name: shipping_tariff_logic
description: eBay 出品時の送料・関税の業務ロジック完全版 (US 軸差分式 + DDP/DDU + 4 区分 primary_market マトリクス + ShippingServiceCostOverrideList β fix). 関税率変更や新規発生時に version up.
type: reference
wiki_type: synthesis
genre: shipping-tariff
related:
  - reference_shipping_method_vs_ddu_taxonomy
  - reference_section_232_kb
  - feedback_ddp_shipping_policy
layer: wiki
version: 2.3
last_updated: 2026-05-17
updated: 2026-05-17
sources:
  - https://www.help.cbp.gov/s/article/Article-1919
  - https://www.federalregister.gov/documents/2025/09/02/2025-16802/notice-of-implementation-of-the-presidents-executive-order-14324-suspending-duty-free-de-minimis
  - https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/ShippingServiceCostOverrideListType.html
  - tools/ebay-manager/monitor/terapeak_scraper.py
  - tools/ebay-manager/monitor/ebay_lister.py
originSessionId: 962c7843-f89c-45ef-a184-17d13cb05633
---
# eBay 送料・関税ロジック (常時参照)

**適用エージェント**: research-brain / code-architect / generator / planner / claude_evaluator / ebay-listing / ebay-manager-qa / Explore / general-purpose / 直接 Read している main agent も同様

**バージョン管理**: 関税率の改訂 (Section 232 / IEEPA / デミニミス追加変更等) があれば本ファイルを更新し `version` を bump する。最新が必ずここにあること。

---

## 1. 時代区分 (重要前提)

**2025-08-29 デミニミス完全撤廃** (Executive Order 14324 / 2025-07-30 発令、CBP CSMS 公式) により US 向け運用が **DDP 推奨** に変更。詳細: `feedback_tariff_era.md`

| 区分 | 期間 | 米国向け運用 |
|---|---|---|
| Pre-tariff | ~2024-09 | 関税基本免除 ($800 以下) |
| Transition | 2024-10 〜 2025-08-28 | 部分関税 + 段階的撤廃 (中国向け 2025-05-02 で先行終了) |
| **Post-tariff (現在)** | **2025-08-29〜** | **デミニミス完全撤廃 = DDP 推奨** |

---

## 2. 基本原則

- **US 向け = DDP (Delivered Duty Paid) 推奨** = 売主が事前関税徴収する運用 (buyer 不満 / Defect 回避のため業務選択、絶対法的義務ではないが実質必須)
- **US 以外 = DDU (Delivered Duty Unpaid)** = buyer 現地で関税払い
- DDP 関税は **eBay 送料欄に上乗せ表示** (商品価格には含めない、ただし US_only 区分は例外)

---

## 3. 送料計算式 (US 軸差分)

eBay 出品時の送料表示は **US を基本軸の差分式**:

```
各国表示送料 = (各国実送料 - US 実送料) + (DDP 関税 if 米国向け else 0)
```

**US 実送料は売主の仕入費用扱い** (商品価格に既に内包済)、buyer 表示は差分のみ。

### 例 ($300 商品、US 実送料 $30、EU 実送料 $42、US 関税 $60)

| 国 | 実送料 | 差分 | 関税 (DDP/DDU) | eBay 表示送料 |
|---|---|---|---|---|
| US | $30 | $0 | $60 (DDP 売主負担) | **$60** |
| EU | $42 | $12 | $0 (DDU buyer 払) | **$12** |

→ buyer は商品 $300 + 表示送料を払う:
- US buyer: $300 + $60 = $360
- EU buyer: $300 + $12 = $312

---

## 4. 4 区分 primary_market マトリクス (W7-A)

Terapeak 365 日 sold データで listing 単位に判定 → 4 区分のいずれか (v2.0 / W110(2) で 90→365 に変更):

### 4.1 判定式

```python
# monitor/terapeak_scraper.py (v2.1 / 2026-05-15 訂正、US_only 含め一律 sample>=3)
US_ONLY_THRESHOLD = 0.70             # ≥70% → US_only
GLOBAL_ONLY_THRESHOLD = 0.30         # ≤30% → global_only
MIN_SAMPLE_SIZE = 3                  # 全区分共通 (<3 件で unknown)

def _judge_primary_market(us_count, non_us_count):
    total = us_count + non_us_count
    if total < MIN_SAMPLE_SIZE:
        return "unknown"
    us_ratio = us_count / total
    if us_ratio >= US_ONLY_THRESHOLD:
        return "US_only"
    if us_ratio <= GLOBAL_ONLY_THRESHOLD:
        return "global_only"
    return "mixed_global"
```

### 4.2 区分別 業務戦略

| 区分 | 判定条件 | 商品価格 | ShipToLocations | US 表示送料 | 非 US 表示送料 | ターゲット |
|---|---|---|---|---|---|---|
| **US_only** | sample≥3 + US≥70% | **商品代 + DDP 関税 (包含)** | 全国 (eBay 仕様で必須) | **$0 Free** | $0 Free (考慮外) | US 客向け SEO 強化 |
| **mixed_global** | sample≥3 かつ 30%<US<70% | 商品代のみ | 全国 | 差分0 + DDP 関税 | 差分のみ | バランス |
| **global_only** | sample≥3 + US≤30% | 商品代のみ | 全国 (eBay 仕様で除外不可) | **$0 Free** (US 客来ない前提) | 差分のみ | 非 US 客向け SEO |
| **unknown** | sample<3 | mixed_global と同 default | 全国 | 差分0 + DDP 関税 | 差分のみ | sample 蓄積待ち |

### 4.3 区分別の送料・関税構造の核心

- **US_only**: 商品価格に **DDP 関税を包含** ($300 → $360)、送料は全国 $0 Free → SEO ◎
- **mixed_global**: 商品価格は **商品代のみ** ($300)、DDP 関税は **送料欄に上乗せ** (US のみ $60 加算)
- **global_only**: 商品価格は **商品代のみ** ($300)、US 客が万が一来ても **DDP 関税は売主自腹リスク許容** (機会損失分とみなす)、global SEO 重視
- **unknown**: sample<3 で統計不能、mixed_global と同等の default 運用

### 4.4 区分判定 → 業務 KPI

- US_only: 「US 客がほぼ独占、価格競争で SEO ブースト」
- mixed_global: 「混在、価格 buffer 不要 + 送料で関税吸収」
- global_only: 「米国客が来ない前提、global 競争力最大化」
- unknown: 「sample 不足、保守的 default」

---

## 5. eBay XML 実装 (β fix `ShippingServiceCostOverrideList`)

### 5.1 eBay 公式機能

`<ShippingServiceCostOverrideList>` (eBay Trading API 公式) を使い、BP (Business Policy) ベースで国別 cost を個別 override する。2026-05-01 β fix で実装済 (詳細: `session_2026_05_01_individual_listing_w75_w76_w77_w81.md` の Bug 4)。

### 5.2 XML 構造例 (2026-05-17 eBay 一次情報で訂正)

```xml
<Item>
  <ItemID>...</ItemID>
  <!-- BP は SellerProfiles/SellerShippingProfile で別途維持。
       override list は Item 直下の sibling -->
  <ShippingServiceCostOverrideList>
    <ShippingServiceCostOverride>
      <ShippingServiceType>Domestic</ShippingServiceType>
      <ShippingServicePriority>1</ShippingServicePriority>
      <ShippingServiceCost currencyID="USD">60.00</ShippingServiceCost>
      <ShippingServiceAdditionalCost currencyID="USD">0.00</ShippingServiceAdditionalCost>
    </ShippingServiceCostOverride>
    <!-- International は <ShippingServiceType>International</ShippingServiceType> -->
  </ShippingServiceCostOverrideList>
</Item>
```

eBay 公式 (`ShippingServiceCostOverrideType` の子要素、一次情報確認 2026-05-17):

| 要素 | 役割 |
|---|---|
| `ShippingServiceType` | **Domestic / International** (この要素名が正。`ShippingServiceCostOverrideType` という子要素名は存在しない) |
| `ShippingServicePriority` | 単一 domestic service の policy では実質 `1` 固定で正 (Account API は単一 service の sortOrderId を None 返し = priority 1 相当を実機確認 2026-05-17)。複数 service BP のみ sortOrderId 整合が要点 |
| `ShippingServiceCost` | 1 個目送料 (Buyer pays) |
| `ShippingServiceAdditionalCost` | 追加同梱品 1 つあたり送料 |

`ShippingService` (サービス名) / `ShipToLocation` は **この型の子要素ではない** (旧例の誤り)。

#### ⚠️ override 無音失敗の真因 = Revise XML の SellerProfiles 欠落 (2026-05-17 確定、W137 で修正)

**現状の見解 (2026-05-17〜)**: BP 管理 listing の ReviseFixedPriceItem では
`<ShippingServiceCostOverrideList>` を **同一 request 内の `<SellerProfiles><SellerShippingProfile>`
と同梱**しないと、eBay は Ack=Success を返しつつ override を無音で無視する (BP default cost 継続)。
出品 Add 経路 (`ebay_lister.py` L516-526) は SellerProfiles 同梱で効くが、旧 Revise 経路
(`ebay_client.py::_build_revise_with_shipping_xml`) は SellerProfiles を欠落させていた = 真因。
GetItem は override 適用後の実効値を `Item/ShippingServiceCostOverrideList` 側に返し
`ShippingServiceOptions` は BP default を返し得る (verify は両コンテナを読み override 優先)。
**W137 (2026-05-17) で修正済**: 反映前 GetItem の 3 profile ID を `seller_profiles` で
同梱 + 反映後 GetItem 実値 verify。`ShippingServiceType=Domestic` / `priority=1` は維持 (正)。

**過去の見解 (〜2026-05-17)**: 真因を「`ShippingServicePriority=1` ハードコードが BP の
`sortOrderId` 不一致時に無音失敗。根治には Business Policies Management API で sortOrderId
動的取得が必要」と推定 (Codex finding1 + audit + architect C1 案)。

**矛盾点 / 変更理由**:
- 変更日: 2026-05-17
- 契機: W137 着手前の読み取り専用 Account API 実機検証で **target policy 377279110023
  の DOMESTIC service の `sortOrderId` が None** (単一 service)、かつ listing GetItem の
  ShippingServicePriority は元々 1 と判明 → priority/sortOrderId 不一致説は **falsified**
  (verify-before-build のゲートが誤実装 C1 を寸前回避)。
- 何が違うか: 真因は priority でなく **Revise への SellerProfiles 非同梱**。Add 経路
  (同梱=効く) と Revise 経路 (欠落=無音失敗) の実コード非対称 + eBay 一次情報
  (SellerShippingProfile は Add/**Revise**/Relist で BP を参照) + Codex 2 段検証で確定。
- 何が同じか: `audit-2026-05-01` の 8/9 不適用という症状・経験的証拠は不変
  (原因の解釈のみ訂正)。`ShippingServiceType=Domestic` 要素名が正も不変。
- 教訓: md-files-can-be-wrong R-1 / Codex は eBay 仕様で hallucinate し得る →
  実機 (Account API/GetItem) 一次検証で仮説を falsify してから実装すること。

### 5.3 4 区分別 XML 切替 (実装は候補 D / W84+)

| 区分 | 商品価格 | Override US | Override 他国 |
|---|---|---|---|
| US_only | 商品代+関税 | $0 | $0 (考慮外) |
| mixed_global | 商品代 | $0 + 関税 | 差分 + $0 |
| global_only | 商品代 | $0 (リスク許容) | 差分 + $0 |
| unknown | 商品代 | $0 + 関税 (default) | 差分 + $0 |

注: `<ShippingType>Flat</ShippingType>` は必須維持 (`tools/ebay-manager/CLAUDE.md` L20-22)。

---

## 6. Section 232 関税 (高関税対象)

**詳細 KB**: `.company/ebay-knowledge/topics/section_232_tariff_2026_04.md` (2026-04-06 改訂)

### 6.1 関税レイヤ別比率

| Annex | 比率 | 対象 (Chapter / HS) |
|---|---|---|
| **I-A** | **50%** | Chapter 72-74/76 純金属製品 (鉄/アルミ/銅) — 重量閾値なし |
| **I-B** | **25%** | Chapter 84-87 派生品 — metal weight ≥15% で課税 |
| **III** | **15% transitional** | HS 8421/8424/8428 (~2027-12-31) |
| IEEPA reciprocal | **15%** | デフォルト (Section 232 該当時は exempt) |

### 6.2 IEEPA 重複回避

- Section 232 該当品 = IEEPA exempt (二重課税防止)
- ⚠️ **例外**: semiconductors / automotive parts (HS 8708) は IEEPA exempt 対象外

### 6.3 高 value 商品 ($500+) 確認手順

CBP CSMS で 2-4 週ごとの改訂を再確認 (本 KB は最終確認 2026-04-30)。

---

## 7. 通関書類 (FedEx/UPS/DHL) 戦略

詳細: `tools/ebay-manager/CLAUDE.md` の「通関ルール」section + `feedback_customs_response_strategy.md`

- **Manufacturer**: 第一選択 = 日本国内代理店 (NG = 中国・東南アジア本社)
- **End Use**: 商品の実用途のみ (NG = `resale` / `commercial` / `eBay`)
- **HTS**: 根拠 Ruling 引用 + `Please verify with your customs broker.` 末尾必須
- **素材記述**: アルミ/鉄なし → "No aluminum or steel parts" 明示

---

## 8. クロスリファレンス

| 関連先 | 役割 |
|---|---|
| `feedback_tariff_era.md` | 時代区分 (pre/transition/post-tariff) |
| `feedback_ddp_shipping_policy.md` | DDP 出荷ポリシー詳細 |
| `feedback_customs_response_strategy.md` | 通関書類回答戦略 |
| `tools/ebay-manager/CLAUDE.md` | eBay 規制 4 セクション (出品/通関/DDP/ランク) |
| `.company/ebay-knowledge/topics/section_232_tariff_2026_04.md` | Section 232 詳細 KB |
| `session_2026_05_01_individual_listing_w75_w76_w77_w81.md` | β fix 実装履歴 (`<ShippingServiceCostOverrideList>`) |

---

## 9. バージョン履歴

### v2.3 (2026-05-17) — 送料 override 無音失敗の真因訂正 (priority説 falsified → SellerProfiles 欠落)

- v2.2 の「真因 = ShippingServicePriority=1 vs BP sortOrderId 不一致、要 Business
  Policies API」を **falsify**。W137 着手前の Account API 実機検証で sortOrderId=None
  (単一 service = priority 1 相当)、真因は **Revise XML の SellerProfiles 非同梱**と確定。
- §5.2 の priority 行 + 系統バグ block を contradiction-annotation 形式で訂正。
  W137 (2026-05-17) で seller_profiles 同梱 + 反映後 GetItem 実値 verify として修正済。
- ShippingServiceType=Domestic 要素名が正・audit 8/9 症状は不変 (原因解釈のみ訂正)。

### v2.2 (2026-05-17) — §5.2 XML 例を eBay 一次情報で訂正 + 系統的 priority バグ明記

**矛盾アノテーション (contradiction-annotation 規約)**:

- **現状の見解 (2026-05-17〜)**: ShippingServiceCostOverride の子要素は
  `ShippingServiceType` (Domestic/International) + `ShippingServicePriority`
  (BP の sortOrderId と一致必須) + `ShippingServiceCost` +
  `ShippingServiceAdditionalCost`。`ShippingService` / `ShipToLocation` /
  `ShippingServiceCostOverrideType` という子要素は存在しない。
- **過去の見解 (〜2026-05-17)**: §5.2 例が
  `<ShippingServiceCostOverrideType>` + `<ShippingService>` +
  `<ShipToLocation>` という構造を提示していた。
- **矛盾点 / 変更理由**:
  - 変更日: 2026-05-17
  - 契機: W133 後の「商品管理 eBay 反映」未反映バグ調査。実コード
    (`ebay_client.py::_build_revise_with_shipping_xml` /
    `ebay_lister.py` + test 2 本) は `<ShippingServiceType>` を使用、
    旧 §5.2 例と矛盾。eBay 公式 (ShippingServiceCostOverrideType /
    ReviseFixedPriceItem doc、WebSearch 一次情報) で **コードが正・
    旧 §5.2 例が誤り** と確定 (md-files-can-be-wrong R-1: コードが真実)。
  - 影響: 旧誤例が本調査で assistant を「要素名バグ」と一時誤誘導
    (verify-before-asserting で誤修正は寸前回避)。
  - 何が同じか: `<ShippingType>Flat</ShippingType>` 維持 / BP は
    SellerProfiles で維持しつつ cost のみ override する方針は不変。
- **新規追記**: 真の系統的バグ = `ShippingServicePriority` の `1`
  ハードコードが BP `sortOrderId` 不一致時に override 無音失敗
  (audit-2026-05-01-shipping.md で 8/9 不適用の経験的証拠)。§5.2 末尾に
  詳細。根治は Business Policies API で sortOrderId 動的取得 = 要 /feature-dev。

### v2.1 (2026-05-15) — US_only も一律 sample≥3 統一 (Codex review 由来 + user 訂正)

- **US_only 確定も sample≥3 で OK** (旧 v2.0 の `MIN_SAMPLE_SIZE_US_ONLY = 5` を撤回)
  - 経緯: 2026-05-15 W124 Phase D で Codex が「文書とコード不整合」を flag → user 訂正「US_only 含め一律 3 件未満で判定不可」
  - 影響: sample 3-4 で US≥70% は **US_only 確定** (旧 v2.0 では mixed_global に格下げしていた、これが過剰保守)
  - 関連 code 修正必要: `monitor/terapeak_scraper.py` の `MIN_SAMPLE_SIZE_US_ONLY = 5` を削除、`_judge_primary_market` から US_only sample fallback 分岐を撤去 (本 doc 更新と同 session 内に予定)

### v2.0 (2026-05-09) — W110(2) 2 段サンプル閾値 + dayRange 365 日標準化 ⛔ 部分 SUPERSEDED by v2.1

- **MIN_SAMPLE_SIZE: 5 → 3** (一般判定の最低値) ✅ 維持
- ~~**MIN_SAMPLE_SIZE_US_ONLY = 5** 新規導入~~ ⛔ v2.1 で撤回
  - ~~理由: sample 3/3 完全 US でも偶然性高、DDP 関税 buffer 内蔵で機会損失リスク回避~~
  - ~~sample 3-4 で US≥70% は mixed_global に格下げ (安全側)~~
- **dayRange default: 90 → 365** (期間 1 年標準化) ✅ 維持
  - 理由: 90日 + sample≥5 で active 432 件中 348 件 (約 8 割) が unknown 確定 = 旧ルールが厳しすぎ
  - dayRange 365 で sample 母数増 → unknown 比率 大幅減 見込
- **request 間隔ジッタ追加** (W110(3)): `sleep_seconds * [0.7, 1.5]` で固定間隔回避 (anti-bot 対策)
- **scraper DOM 残留 fix** (W110(1)): `wait_for_load_state('networkidle', 20s)` 追加 + BuyerLocation timeout 時 1 回 reload retry
- 該当 code: `monitor/terapeak_scraper.py` `_judge_primary_market` / `_scrape_via_search_box_impl`、`tasks/task_market_analysis_refresh.py`

### v1.0 (2026-05-01) — 初版作成

- 4 区分 primary_market マトリクス確定 (US_only / mixed_global / global_only / unknown)
- 送料計算式: US 軸差分 (`各国送料 = 実送料 - US 送料`)
- DDP 関税の送料欄上乗せ運用 (mixed_global/unknown のみ)
- US_only 区分のみ「商品価格に DDP 関税包含」、global_only は「US 客自腹リスク許容」
- eBay XML 実装: `<ShippingServiceCostOverrideList>` β fix (2026-05-01 個別出品 6 bug fix)
- Section 232 (Annex I-A 50% / I-B 25% / III 15%) + IEEPA reciprocal 15%
- ShipToLocations は eBay 仕様で全 4 区分とも全国必須

### 改訂時の更新ガイド

以下が発生したら version bump (例: v1.0 → v1.1):
1. 関税率変更 (Section 232 / IEEPA / 新規関税)
2. デミニミス再変更 (例: 一部商品で再導入)
3. 4 区分の閾値調整 (US_ONLY_THRESHOLD / GLOBAL_ONLY_THRESHOLD / MIN_SAMPLE_SIZE)
4. eBay XML 実装変更 (`ShippingServiceCostOverrideList` の API 仕様改訂等)
5. 新区分追加 (例: US_priority 等)

更新時は本ファイル冒頭の `version`, `last_updated` を更新 + 「## 9. バージョン履歴」に変更内容を追記。

---

## 10. 注意事項 (品質保護)

- 本ファイル記述と実コード (`monitor/terapeak_scraper.py` / `monitor/ebay_lister.py`) が矛盾する場合は **コードが真実** (`md-files-can-be-wrong.md` R-1)
- ただし本ファイルは「業務仕様の権威」として扱う = コードが矛盾していたら code-reviewer で fix 対象
- エージェントが本ファイルを参照する際、最新 version を確認 + ファイル冒頭の `last_updated` 日付の鮮度を確認 (`feedback_memory_staleness_2026_04_30.md`)



==============================================================================
# 【配送方法 vs DDU/DDP 分類 (混同禁止)】
# source: C:\Users\gucch\.claude\projects\C--Users-gucch-projects-claude\memory\reference_shipping_method_vs_ddu_taxonomy.md
==============================================================================

---
name: 配送方法 (carrier) vs 関税ポリシー (DDU/DDP) の正しい区分
description: eBay の「配送方法」(SpeedPAK Economy / FedEx 等) と「関税ポリシー」(DDU/DDP) は独立した別軸. 混同禁止
type: reference
wiki_type: comparison
genre: shipping
related:
  - reference_shipping_tariff_logic
  - feedback_ddp_shipping_policy
confirmed_at: 2026-05-12
originSessionId: 2026-05-12-evening-w119-correction
layer: wiki
updated: 2026-05-14

---

# 配送方法 (carrier) vs 関税ポリシー (DDU/DDP) — 別軸である

## 結論 (1 行)

**配送方法 ≠ DDU/DDP**. 両者は **独立した別概念**. 競合分析・フィルタ実装時に **混同禁止**.

## 定義

### 配送方法 (Shipping Method / Carrier / Service)

- 配送業者・サービス名 (例: "eBay SpeedPAK Economy", "FedEx International Priority", "DHL Express")
- Browse API 判別: `shippingOptions[0].shippingServiceCode` (get_item_by_legacy_id レスポンス、item_summary/search では返らない)
- 関連: 配送速度・追跡・送料・配送窓 (transit time)

### 関税ポリシー (Import Duty Policy)

- 関税負担者の指定:
  - **DDU** (Delivered Duty Unpaid): **buyer 負担** (荷物到着時に関税請求が来る)
  - **DDP** (Delivered Duty Paid): **seller 負担** (商品価格・送料に既含み、buyer 追加負担なし)
- eBay 上の設定: listing 編集で「Includes import fees」flag を on/off
- Browse API 判別: import charges field (具体的 field 名は実装時に getItem で確認、`taxes` 配列内の可能性高い)

## 独立軸の組み合わせ (4 通り)

| 配送方法 | 関税ポリシー | 例 |
|---------|------------|---|
| Economy 配送 | DDU | 一般的な低単価 SpeedPAK Economy seller (関税分余裕なし) |
| Economy 配送 | DDP | 理論上可能、実例少ない (低単価で関税分上乗せ可) |
| Express 配送 | DDU | FedEx / DHL で関税 buyer 払い |
| Express 配送 | DDP | TOYOTASUMI のような DDP express seller (関税込み価格) |

## 競合分析での扱い

`feedback_competitor_jp_sellers_only.md` 「3 軸独立判定」に従う:

1. 所在地 (国) — 日本のみ
2. 配送方法 — Economy 系除外
3. 関税ポリシー — DDU 除外

各軸独立に判定. SpeedPAK Economy + DDP も SpeedPAK Express + DDU もそれぞれ理論上ありえる.

## ⚠️ 過去の誤り (2026-05-12 訂正)

- assistant が 2026-05-11 に「SpeedPAK Economy = DDU」と独自推論で memory に書き込み
- UI warning 文言・コード comment にも「DDU 発送」と表示
- 動画学習 KB は「DDU 出品者は比較対象外」「SpeedPAK Economy は低単価商品向け配送サービス」と **別個に** 記載していたが、私が混同
- user 指摘で訂正

## 関連 file / 関連 memory

- `tabs/tab_research_wizard.py:is_likely_ddu_shipping()` — 配送窓 proxy (近似 DDU、本来 Economy 系判定)
- `tabs/tab_product_management.py:_is_economy_shipping()` — shippingServiceCode ベース Economy 系判定
- (今後実装) `_is_ddu_policy()` — import charges field ベース DDU 判定
- `.company/ebay-knowledge/topics/operation-rules.md` 「DDU 出品者は最初から比較対象外」
- [[feedback_competitor_jp_sellers_only]] — 3 軸独立除外ルール
- [[feedback_ddp_shipping_policy]] — TOYOTASUMI 自身の DDP 運用ポリシー (米国向け Section 232 対応)



==============================================================================
# 【Section 232 関税 詳細KB (Annex I-A/I-B/III HTSリスト)】
# source: C:\Users\gucch\projects\claude\.company\ebay-knowledge\topics\section_232_tariff_2026_04.md
==============================================================================

# Section 232 関税 2026-04 改訂版 KB

**最終更新**: 2026-04-25
**情報源**: ホワイトハウス公式 Annexes I-A/I-B/II/III/IV PDF（2026-04-02 Proclamation）+ White & Case / Phillips Lytle / GHY / Greenberg Traurig 解説

## 1. 改訂サマリ（2026-04-06 発効）

| 区分 | 税率 | 対象 | HTSUS heading |
|------|------|------|---------------|
| **Annex I-A** | **50%** | 主要鉄/アルミ/銅製品（Chapter 72-74, 76 中心、280 コード） | 9903.82.02 |
| **Annex I-B** | **25%** | 鉄/アルミ/銅 derivatives（410 HTSUS コード、Chapter 84-87 含む） | 9903.82.04-17 |
| **Annex II** | 適用除外 | 食品、化学品、化粧品、motorcycle 部品など | — |
| **Annex III** | **15%** （transitional） | 産業用機械の一部、Dec 31 2027 まで | — |
| **Annex IV** | （技術付録） | I-A/I-B を HTSUS 9903.82.XX へ翻訳 | — |
| US-melted exemption | **10%** | 米国溶解金属 95%+ 含有品 | — |
| **重量閾値ルール** | **適用** | Chapter 72/73/74/76 以外は metal weight ≥15% で課税 | — |

## 2. eBay 物販で頻出する HTSUS の Annex 該当性

### 🔴 Annex I-B（25% 確定）対象の HTS（鉄/アルミ derivatives）

| HTS | 商品例 | 注意点 |
|-----|--------|--------|
| **8516.60.40** | 電気炊飯器、オーブン（家庭用） | NV-25 等、本件で確認 |
| **8516.60.60** | 電熱ホットプレート、グリラ等 | |
| **8516.29.00** | 電気スペースヒーター（蓄熱式以外） | |
| **8516.90.50 / .8050** | 電気調理器具の部品 | |
| **7321.xx** | 鋼鉄製ストーブ、レンジ、暖炉 | metal 比率高いため確実に課税 |
| **7322.xx** | 鋼鉄製ラジエータ | |
| **7323.xx** | 鋼鉄製食卓・台所用品 | 鍋、フライパン、保温ジャー等 |
| **7324.xx** | 鋼鉄製衛生陶器 | |
| **8418.10/21/29/30/40** | 冷蔵庫、冷凍庫 | |
| **8415.xx** | エアコン | |
| **8501.64** | 特定モーター | |
| **8504.31-33** | 変圧器 | |
| **8517.71** | 電気通信機器の部品 | |
| **8544.42/49/60** | 絶縁電線、ケーブル | |
| **8708.xx** | 自動車部品（多数） | バンパー、シャシー、ボディ、ホイール等 |
| **8716.xx** | トレーラー部品 | |

### 🟢 Annex II（除外）対象の HTS

motorcycle 製造専用で輸入される Chapter 84/85/87 部品のみ除外。一般輸入には影響なし。

### 🟡 Annex III（15% transitional、〜2027-12-31）

産業用機械中心：
- 8401.40（核反応炉部品）
- 8417.90（産業炉部品）
- 8421.29（液体ろ過装置）
- 8424.89.90（噴霧装置）
- 8428.32/33/39/60/70（コンベア、産業ロボット）
- 8431.39（持上げ機械部品）
※ 家電は対象外。

## 3. 重量閾値ルール（Annex IV §c 第2文）

> "For articles classified in the listed provisions that are not in chapters 72, 73, 74 or 76 of the HTSUS, headings 9903.82.02 and 9903.82.04–9903.82.17 only apply where the weight of the applicable metal is at least 15 percent of the weight of the imported article."

### 解釈
- Chapter 72-74, 76（純金属製品）：閾値なし、自動課税
- Chapter 84-87 等の derivatives：**金属の重量 ≥15% の場合のみ課税**
- 各金属（鉄/アルミ/銅）ごとに独立判定
- 例：8.5kg の家電で steel 4kg(47%) → steel 派生品として課税 / aluminum 0.5kg(6%) → aluminum 派生品としては不課税

## 4. IEEPA reciprocal との関係（重要）

### Section 232 が優先・IEEPA exempt
> "Products subject to Section 232 aluminium, steel, copper, or timber tariffs— but not semiconductors, automotive or automotive parts tariffs— are exempt from IEEPA tariffs."

つまり：
- Section 232 該当品 → **IEEPA reciprocal 15% は適用しない**
- 派生品 25% のみが追加コスト
- Section 232 非該当品（Annex II 等） → IEEPA reciprocal が適用される

### 非該当品の IEEPA Japan 取扱い
- 日本産品の reciprocal IEEPA = **15%（MFN inclusive）**
- MFN < 15% の場合：top-up で 15% に
- MFN ≥ 15% の場合：top-up なし、MFN がそのまま
- WTO 民間航空機協定対象品 / 日本産医薬品 / 半導体は別ルール（IEEPA + Section 232 とも除外または特別レート）

## 5. 計算ワークフロー（DDP 価格設定用）

```
入力: HS コード, 商品価格 USD, 商品重量 kg, 主要材料の重量内訳
↓
Step 1: HS コードを Annex I-A/I-B/II/III と突合
  - I-A 該当 → Section 232 = 50%
  - I-B 該当 → Step 2 へ
  - III 該当 → Section 232 = 15%（〜2027-12-31）
  - II 該当 → Section 232 適用なし、Step 4 へ
  - 全リスト外 → Step 4 へ
Step 2: Chapter 確認
  - 72/73/74/76 → そのまま課税
  - 84/85/86/87 等 → Step 3 へ
Step 3: 金属重量比率を判定
  - steel/aluminum/copper のいずれかが ≥15% → Section 232 課税
  - すべて <15% → Section 232 不課税、Step 4 へ
Step 4: IEEPA reciprocal
  - 日本産で MFN < 15% → IEEPA top-up = (15% - MFN%) × 価格
  - 日本産で MFN ≥ 15% → IEEPA = 0
Step 5: MFN base duty を加算
合計 = Section 232 + IEEPA + MFN + FedEx Disbursement Fee (2-3%)
```

## 6. ケーススタディ：Netsuken NV-25（TRK#870480400096）

```
HS:        8516.60.4000
価格:      USD 798
重量:      8.5 kg（steel 4.0kg=47%, aluminum 0.8kg=9.4%, copper 0.05kg=0.6%）
MFN:       Free
原産国:    日本

Step 1: Annex I-B 該当 ✓
Step 2: Chapter 85 → 重量閾値ルール適用
Step 3: steel 47% ≥15% → Section 232 steel derivative 課税
        aluminum 9.4% <15% → 不課税
        copper 0.6% <15% → 不課税
Step 4: Section 232 該当 → IEEPA exempt
Step 5: MFN Free → 加算なし

合計追加関税: 25% × $798 = $199.50
FedEx Disbursement Fee 2-3%: ~$5-7
DDP 売主負担: 約 $205-207 (≈ ¥31,000)
```

## 7. 実務上のガイドライン

### 出品前チェック
1. 商品の HS code を推定（FedEx Trade Tools 等で）
2. このKBの「Annex I-B 対象 HTSUS」と突合
3. 該当する場合は重量と材料比率を確認
4. 価格設計に Section 232 25% buffer を組込

### 高リスク商品カテゴリ（Annex I-B 該当が多い）
- 家電・調理器具（炊飯器、オーブン、冷蔵庫、エアコン、ヒーター）
- 鋳鉄・ステンレス調理器具（鍋、フライパン、保温ジャー）
- 自動車部品（バンパー、ホイール、シャシー）
- 電線・ケーブル類
- 産業機械の部品（要 Annex III 確認、15% 軽減ありえる）

### 低リスク商品カテゴリ
- 精密電子機器（カメラ、e-reader、ガジェット）→ Chapter 85 内でも HS が異なる
- 衣類、玩具、書籍 → Section 232 対象外
- 非金属製品全般

### 「Section 232 派生品リスクスコアリング」を出品時自動算出する機能（W20 候補）
- HS code 入力 → Annex 該当判定 + 適用税率推定
- 商品重量・材料比率（写真から AI 推定 or seller 入力）→ 課税判定
- 推定追加コスト → 売価に reverse 計算で必要マージン算出

## 8. 公式情報源

- Annexes I-A/I-B/II/III/IV PDF: https://www.whitehouse.gov/wp-content/uploads/2026/04/ANNEXES-I-A-I-B-II-III-IV.pdf
- CBP CSMS # 68253075: https://content.govdelivery.com/accounts/USDHSCBP/bulletins/4117593
- White & Case 解説: https://www.whitecase.com/insight-alert/united-states-modifies-steel-aluminum-and-copper-section-232-tariffs
- Phillips Lytle 解説: https://phillipslytle.com/administration-restructures-section-232-tariffs-on-metal-and-derivative-products/
- GHY International: https://www.ghy.com/trade-compliance/us-adjusts-section-232-tariffs-on-aluminum-steel-and-copper-full-customs-value-now-applies/
- Greenberg Traurig（IEEPA refund + Section 232 重複の取扱い）: https://www.gtlaw.com/en/insights/2026/4/updates-for-importers-on-ieepa-refunds-and-section-232-metals
