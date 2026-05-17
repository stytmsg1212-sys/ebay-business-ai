# 意思決定ログ 2026-05-17

## W138 (id=222) shipping policy 表示+変更 — クローズ (live eBay 検証を意図的省略)

- **決定 (2026-05-17, user)**: W138-A を **pytest + 2 段レビュー担保で省略しクローズ**。DoD の「実 BP 変更 (live eBay 書込, user 監視下)」ステップを**意図的に waive**。
- **背景**: 実装・検証は最大限完遂 — full pytest 1196 passed / 内部 code-reviewer HIGH=0 ×3 / Codex 2 段ループ ×2 / Playwright E2E (BP 自動表示・↻ 実機) / backfill 421 件 apply + 24h retrospective HIGH=0。残るは live eBay listing の BP 実変更→原状回復のみ。
- **判断根拠**: write 経路 (`revise_shipping_profile` / Codex#1 dirty-flag / source-contract 番人) は pytest (`test_codex1_*` / `test_w137` BP系) + 内部 HIGH=0 + Codex 2 段で論理担保済。live mutation は belt-and-suspenders。W133 item2 (無監視 real-eBay 書込で実ミス) の再発回避とのトレードオフを user が weigh し省略を選択。
- **2 段レビューの価値再実証**: 内部 code-reviewer + pytest 1196 が見落とした金銭直結バグ 2 件を Codex が捕捉 — 設計 Codex#1 (DB列化×pre-snapshot 交差で stale BP 巻き戻し) / 実装 Finding1 (`st.selectbox(key=)` 永続化で ↻ 後 stale 巻き戻し、Section 232 数百ドル/件)。いずれも根治 + 退行物理 BLOCK テスト追加。
- **波及**: cascade 外部対象なし確定 (Q-1 撤回の矛盾アノテーションは W138-A 設計書内自己完結。reference_shipping_tariff_logic.md §5 は revise XML 機構の権威で W138-A 不改修ゆえ unrelated = K2)。
- **状態**: ROADMAP id=222 status=完了 / completed=2026-05-17。MonoDeck は W138-A コードで稼働継続 (PID 45264)。commit は user 指示待ち (確立規約)。
