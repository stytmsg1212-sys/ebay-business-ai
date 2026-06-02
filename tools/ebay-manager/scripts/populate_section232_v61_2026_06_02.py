"""W212 prep one-shot: Section 232 該当 13 件に section232_class / duty_rate_pct を設定.

2026-06-02 user 承認済 (全商品調査 → 該当 13/533、+5% 手数料 buffer 込)。
- I-A 55% (純金属・鉄、閾値なし自動): STAUB 鋳鉄ダッチオーブン 1 件
- I-B 30% (派生品・金属≥15%): トランス7/モーター2/電熱家電3 = 12 件
非該当 (NULL) は calculator が global 20% に fallback。本 script はデータ保持のみ
(calculator が読む配線は W212 本実装)。

listing 識別は ebay_item_id (sku-rules.md)。直接 UPDATE は db-migration-rules 6 step
(snapshot → 1 件試行 → 全件 → SELECT 検証) に従う。冪等 (既存値があっても同値上書き)。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor.database import init_db, DB_PATH  # noqa: E402

# (ebay_item_id, section232_class, duty_rate_pct)
TARGETS = [
    ("358556302102", "I-A", 55.0),  # STAUB Cocotte Cast Iron Dutch Oven
    ("358293723954", "I-B", 30.0),  # Zojirushi NW-JE10 IH Rice Cooker
    ("358198845736", "I-B", 30.0),  # Netsuken NV-25 Rice Warmer
    ("358313895116", "I-B", 30.0),  # Nissyo NDF-1500 Step Down Transformer
    ("358321209729", "I-B", 30.0),  # Yaskawa SGMJV AC Servo Motor
    ("358342224526", "I-B", 30.0),  # entre ET-100 Step-Up Transformer
    ("358228925781", "I-B", 30.0),  # FOSTEX R100T Attenuator Transformer
    ("357963553298", "I-B", 30.0),  # TANGO FW-20S Output Transformer
    ("357383352429", "I-B", 30.0),  # Fostex R100T2 Transformer
    ("358537337245", "I-B", 30.0),  # ORIENTAL MOTOR ARM911AC Stepping Motor
    ("358548831391", "I-B", 30.0),  # RSA-1 Slidac Variable Auto Transformer
    ("358403890670", "I-B", 30.0),  # Tokyo Koden TA-600 Step-Down Transformer
    ("357907189869", "I-B", 30.0),  # NETSUKEN Ever Hot NS-21N Rice Warmer
]


def main() -> None:
    # Step 0: migration 適用 (v61 カラム追加)
    init_db()
    ids = [t[0] for t in TARGETS]
    ph = ",".join("?" * len(ids))

    with sqlite3.connect(str(DB_PATH)) as conn:
        # Step 1: snapshot (rollback 用)
        print("=== Step1 snapshot (適用前) ===")
        before = conn.execute(
            f"SELECT ebay_item_id, section232_class, duty_rate_pct, "
            f"substr(title,1,40) FROM ebay_listings WHERE ebay_item_id IN ({ph})",
            ids,
        ).fetchall()
        for r in before:
            print("  ", r)
        if len(before) != len(TARGETS):
            raise SystemExit(
                f"対象 {len(TARGETS)} 件中 {len(before)} 件しか見つからない = 中断"
            )

        # Step 2: 1 件試行 (STAUB I-A 55)
        t0 = TARGETS[0]
        cur = conn.execute(
            "UPDATE ebay_listings SET section232_class=?, duty_rate_pct=? "
            "WHERE ebay_item_id=?",
            (t0[1], t0[2], t0[0]),
        )
        assert cur.rowcount == 1, f"試行 UPDATE rowcount={cur.rowcount} (期待1)"
        chk = conn.execute(
            "SELECT section232_class, duty_rate_pct FROM ebay_listings "
            "WHERE ebay_item_id=?",
            (t0[0],),
        ).fetchone()
        print(f"=== Step2 試行1件 {t0[0]} -> {chk} (期待 ('I-A', 55.0)) ===")
        assert chk == (t0[1], t0[2]), "試行結果不一致 = 中断"

        # Step 3: 残り全件
        for eid, cls, rate in TARGETS[1:]:
            cur = conn.execute(
                "UPDATE ebay_listings SET section232_class=?, duty_rate_pct=? "
                "WHERE ebay_item_id=?",
                (cls, rate, eid),
            )
            assert cur.rowcount == 1, f"{eid} rowcount={cur.rowcount}"
        conn.commit()

        # Step 4: SELECT 検証
        print("=== Step4 検証 (適用後) ===")
        after = conn.execute(
            f"SELECT ebay_item_id, section232_class, duty_rate_pct, "
            f"substr(title,1,40) FROM ebay_listings WHERE ebay_item_id IN ({ph}) "
            f"ORDER BY duty_rate_pct DESC, ebay_item_id",
            ids,
        ).fetchall()
        for r in after:
            print("  ", r)

        # 全体影響の sanity: 13 件だけが non-null であること
        n_set = conn.execute(
            "SELECT COUNT(*) FROM ebay_listings WHERE duty_rate_pct IS NOT NULL"
        ).fetchone()[0]
        print(f"=== duty_rate_pct 設定済 = {n_set} 件 (期待 {len(TARGETS)}) ===")
        assert n_set == len(TARGETS), f"想定外: {n_set} 件に設定された"

    print("\n完了: Section 232 分類 13 件を保存。calculator 配線は W212 本実装で実施。")


if __name__ == "__main__":
    main()
