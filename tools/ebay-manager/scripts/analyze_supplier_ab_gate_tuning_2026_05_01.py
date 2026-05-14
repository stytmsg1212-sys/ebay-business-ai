"""W86/v2 A/B test data から Hybrid Escalation gate threshold 最適化 + keyword 拡張提案.

入力: supplier_ab_test_runs (test_mode=False = production 相当 v2 が望ましい)
出力: 標準出力に Markdown report. Gate (a)(b) の threshold 推奨値 + Gate (c)(d) の
      keyword coverage 評価 + Hybrid 採用時の cost 試算.

usage: python scripts/analyze_supplier_ab_gate_tuning_2026_05_01.py [run_id]
"""
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

db_path = Path(__file__).resolve().parents[1] / "data" / "monitor.db"

# Gate (c)(d) keyword の現状 list (run_supplier_ab_test_v2 と同期)
HIFI_BRANDS = [
    "Audio-Technica", "PIONEER", "Pioneer", "DENON", "TEAC",
    "Astell", "Sennheiser", "FOSTEX", "Marantz", "Onkyo",
    "Yamaha", "JBL", "Bose",
]
INDUSTRIAL_BRANDS = [
    "KEYENCE", "OMRON", "Omron", "Panasonic", "ADVANTEST", "Advantest",
    "GRAPHTEC", "HIROSE", "Mitutoyo", "KIKUSUI", "Kikusui",
    "YASKAWA", "Yaskawa", "Mitsubishi", "FANUC", "YOKOGAWA", "Yokogawa",
]
JUNK_KEYWORDS = [
    "ジャンク", "動作未確認", "難あり", "訳あり", "気泡", "破損",
    "液晶割れ", "通電のみ", "ノークレーム", "for parts", "Parts only",
    "未確認", "現状渡し", "故障",
]

# Pricing (cache 効果込み実機 calibrated)
COST_PER_OPUS_CALL_CACHED = 0.0181
COST_PER_OPUS_CALL_UNCACHED = 0.04
COST_PER_SONNET_CALL = 0.0089


def title_matches_any(title: str, keywords: list[str]) -> bool:
    if not title:
        return False
    return any(kw.lower() in title.lower() for kw in keywords)


def main(run_id: str | None = None):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if not run_id:
        # 最新 run_id を取得 (variance run は除外)
        row = cur.execute(
            "SELECT run_id FROM supplier_ab_test_runs "
            "WHERE run_id NOT LIKE '%_var' "
            "GROUP BY run_id ORDER BY MIN(created_at) DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("no run found")
            return
        run_id = row["run_id"]

    print(f"# Hybrid Escalation Gate 分析 (run_id={run_id})\n")

    # candidate ペア × Opus / Sonnet score
    cur.execute(
        """
        SELECT ebay_item_id, ebay_title, candidate_index, candidate_title,
               candidate_url, candidate_price_jpy, candidate_platform,
               MAX(CASE WHEN model='claude-opus-4-7' THEN match_score END) AS opus_score,
               MAX(CASE WHEN model='claude-sonnet-4-6' THEN match_score END) AS sonnet_score
        FROM supplier_ab_test_runs
        WHERE run_id=? AND error IS NULL
        GROUP BY ebay_item_id, candidate_index
        HAVING opus_score IS NOT NULL AND sonnet_score IS NOT NULL
        """,
        (run_id,),
    )
    pairs = [dict(r) for r in cur.fetchall()]
    if not pairs:
        print("no valid pairs")
        return

    n_total = len(pairs)
    print(f"## 全体統計\n")
    print(f"- ペア総数: **{n_total}**")
    diffs = [abs(p["opus_score"] - p["sonnet_score"]) for p in pairs]
    verdict_disagree = sum(1 for p in pairs if (p["opus_score"] >= 60) != (p["sonnet_score"] >= 60))
    print(f"- 平均 score 差: **{statistics.mean(diffs):.1f}** 点")
    print(f"- score 差 標準偏差: {statistics.stdev(diffs) if len(diffs)>1 else 0:.1f}")
    print(f"- 採用判定 (60+/60-) 不一致: **{verdict_disagree}/{n_total} ({verdict_disagree/n_total*100:.1f}%)**\n")

    # ── Gate (a) borderline 範囲最適化 ──
    print("## Gate (a) borderline 範囲 最適化")
    print("\n各 cutoff 範囲 [L, R] 内の Sonnet score について「採用判定不一致率」を計算.")
    print("狭すぎ = miss (escalate されない不一致)、広すぎ = waste (Sonnet で当たっていた件も escalate).\n")
    print("| 範囲 | 該当件数 | escalate 後判定一致率 (Opus が override) | 不一致 catch 率 |")
    print("|---|---|---|---|")
    candidates_a = [(35, 65), (40, 60), (45, 55), (40, 70), (30, 70)]
    for L, R in candidates_a:
        in_range = [p for p in pairs if L <= p["sonnet_score"] <= R]
        if not in_range:
            continue
        # この範囲を escalate していたら回避できた不一致数
        disagree_in = sum(1 for p in in_range if (p["opus_score"] >= 60) != (p["sonnet_score"] >= 60))
        catch_rate = disagree_in / verdict_disagree * 100 if verdict_disagree else 0
        print(f"| [{L}, {R}] | {len(in_range)} | — | {disagree_in}/{verdict_disagree} ({catch_rate:.0f}%) |")

    # ── Gate (b) cost threshold 最適化 ──
    print("\n## Gate (b) 仕入価格 threshold 最適化")
    print("\nUSD 換算 (¥150/USD 想定) で各 threshold での「不一致 catch」を計算.\n")
    print("| threshold (USD) | 該当ペア | 不一致 catch | escalation 率 |")
    print("|---|---|---|---|")
    fx = 150
    for thresh in [200, 250, 300, 400, 500]:
        in_thresh = [p for p in pairs if (p["candidate_price_jpy"] or 0) / fx > thresh]
        disagree_in = sum(1 for p in in_thresh if (p["opus_score"] >= 60) != (p["sonnet_score"] >= 60))
        catch = disagree_in / verdict_disagree * 100 if verdict_disagree else 0
        rate = len(in_thresh) / n_total * 100
        print(f"| ${thresh}+ | {len(in_thresh)} | {disagree_in}/{verdict_disagree} ({catch:.0f}%) | {rate:.0f}% of total |")

    # ── Gate (c) ブランド keyword 評価 ──
    print("\n## Gate (c) ブランド keyword カバレッジ")
    print("\n現状 list で Hi-Fi / Industrial に該当するペア (listing title or candidate title).\n")
    hifi_pairs = [p for p in pairs if title_matches_any(p["ebay_title"], HIFI_BRANDS) or title_matches_any(p["candidate_title"] or "", HIFI_BRANDS)]
    industrial_pairs = [p for p in pairs if title_matches_any(p["ebay_title"], INDUSTRIAL_BRANDS) or title_matches_any(p["candidate_title"] or "", INDUSTRIAL_BRANDS)]
    print(f"- Hi-Fi 該当ペア: {len(hifi_pairs)} (うち不一致: {sum(1 for p in hifi_pairs if (p['opus_score'] >= 60) != (p['sonnet_score'] >= 60))})")
    print(f"- Industrial 該当ペア: {len(industrial_pairs)} (うち不一致: {sum(1 for p in industrial_pairs if (p['opus_score'] >= 60) != (p['sonnet_score'] >= 60))})")

    # gate(c) でカバーされない不一致のブランド/keyword 抽出
    uncovered_disagree = [
        p for p in pairs
        if (p["opus_score"] >= 60) != (p["sonnet_score"] >= 60)
        and not (title_matches_any(p["ebay_title"], HIFI_BRANDS + INDUSTRIAL_BRANDS) or
                 title_matches_any(p["candidate_title"] or "", HIFI_BRANDS + INDUSTRIAL_BRANDS))
    ]
    if uncovered_disagree:
        print(f"\n### Gate (c) 漏れ不一致 ({len(uncovered_disagree)} 件、ブランド list 拡張候補)\n")
        for p in uncovered_disagree[:8]:
            print(f"- Opus={p['opus_score']:3d} Sonnet={p['sonnet_score']:3d}: {(p['candidate_title'] or '')[:65]}")

    # ── Gate (d) ジャンク keyword ──
    print("\n## Gate (d) ジャンク keyword カバレッジ")
    junk_pairs = [p for p in pairs if title_matches_any(p["candidate_title"] or "", JUNK_KEYWORDS)]
    junk_disagree = sum(1 for p in junk_pairs if (p["opus_score"] >= 60) != (p["sonnet_score"] >= 60))
    print(f"\n- ジャンク keyword 該当: {len(junk_pairs)} (うち不一致: {junk_disagree})")

    # ── Hybrid 採用時の cost 試算 ──
    print("\n## Hybrid Escalation cost 試算 (本 run の data に基づく)")

    def gate_match(p, gate_a_range=(40, 60), gate_b_thresh=300, fx=150):
        if gate_a_range[0] <= p["sonnet_score"] <= gate_a_range[1]:
            return True
        if (p["candidate_price_jpy"] or 0) / fx > gate_b_thresh:
            return True
        if title_matches_any(p["ebay_title"], HIFI_BRANDS + INDUSTRIAL_BRANDS) or title_matches_any(p["candidate_title"] or "", HIFI_BRANDS + INDUSTRIAL_BRANDS):
            return True
        if title_matches_any(p["candidate_title"] or "", JUNK_KEYWORDS):
            return True
        return False

    print("\n| Gate 設定 | escalate 件数 | escalation 率 | catch 不一致 | cost 試算 |")
    print("|---|---|---|---|---|")
    for label, gate_a, gate_b in [
        ("現案 (40-60 / $300)", (40, 60), 300),
        ("広め (35-65 / $250)", (35, 65), 250),
        ("狭め (45-55 / $400)", (45, 55), 400),
    ]:
        escalated = [p for p in pairs if gate_match(p, gate_a, gate_b)]
        catch = sum(1 for p in escalated if (p["opus_score"] >= 60) != (p["sonnet_score"] >= 60))
        rate = len(escalated) / n_total * 100
        cost = (
            n_total * COST_PER_SONNET_CALL +
            len(escalated) * COST_PER_OPUS_CALL_CACHED
        )
        print(f"| {label} | {len(escalated)} | {rate:.0f}% | {catch}/{verdict_disagree} | ${cost:.4f} |")

    # 比較: 全 Sonnet / 全 Opus
    cost_sonnet_only = n_total * COST_PER_SONNET_CALL
    cost_opus_only = n_total * COST_PER_OPUS_CALL_CACHED
    print(f"| Sonnet 全置換 (catch なし) | 0 | 0% | 0/{verdict_disagree} | ${cost_sonnet_only:.4f} |")
    print(f"| Opus 全維持 (catch 100%) | {n_total} | 100% | {verdict_disagree}/{verdict_disagree} | ${cost_opus_only:.4f} |")

    conn.close()


if __name__ == "__main__":
    run_id = sys.argv[1] if len(sys.argv) > 1 else None
    main(run_id)
