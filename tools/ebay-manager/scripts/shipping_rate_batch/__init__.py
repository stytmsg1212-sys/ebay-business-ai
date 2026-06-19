"""eBay SpeedPAK DDP rate table 月次自動更新バッチ (Phase 9).

設計: .company/engineering/docs/2026-06-19-shipping-rate-table-phase9-monthly-batch-design.md (v2, Codex VERDICT B 反映済)

構成:
  config.py            定数・パス・閾値・サニティ境界
  fetch_fx.py          前月営業日平均 USD/JPY × (1-1%) (frankfurter.app)
  fuel.py              rate table 専用燃料率 (settings, 手動維持) の読取 + freshness/range
  parse_base_rates.py  SpeedPAK PDF から基本料金抽出 + アンカー検証 + キャッシュ
  compute.py           差額式 surcharge 計算
  manifest.py          canonical zone 定義 + 国セット ISO 正規化 bijection 照合
  guards.py            per-rate 変動ハイブリッドガード (F6)
  ebay_api.py          getRateTable / updateShippingCost
  db.py                shipping_rate_batch_log 監査
  run_batch.py         オーケストレータ (preflight_all→guard_all→snapshot_all→apply_all→verify_all)
"""
