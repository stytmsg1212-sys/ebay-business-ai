# -*- coding: utf-8 -*-
"""
テスト結果CSVから、OKのテストケースだけを抽出して、test_report.pyを再構築
"""
import csv
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# CSVから現在のテストケースを読む
ok_tests = {}
with open("test_results2.csv", "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        if not row["サイト名"] or row["サイト名"].startswith("サイト"):
            continue
        if row["判定"] == "OK":
            key = (row["サイト名"], row["変換URL"], row["試験種別"])
            # 元のtest_report.pyから自動的に推定は困難なため、Noからミャッピング
            if row["No"]:
                try:
                    no = int(row["No"])
                    ok_tests[no] = (row["サイト名"], row["変換URL"], row["試験種別"], row["結果"])
                except:
                    pass

print(f"OKのテストケース: {len(ok_tests)}件")
for no, (site, cv, tt, result) in sorted(ok_tests.items())[:10]:
    print(f"{no}: {site} / {tt} => {result}")
