# -*- coding: utf-8 -*-
"""
test_results2.csvから、OKテストケースのURLを抽出してtest_report.pyを再構築
"""
import csv
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# CSVから元の情報を取得するため、test_report.pyを解析
test_cases_map = {}
with open("test_report.py", "r", encoding="utf-8") as f:
    lines = f.readlines()
    for i, line in enumerate(lines):
        if '("' in line and ')', in line:
            # テストケース行を解析
            try:
                # 行を評価してタプルを取得
                parts = line.strip().rstrip(",").split("(", 1)[1]
                # 簡単な構文解析（本来はastを使うべき）
                test_cases_map[i] = line.strip()
            except:
                pass

print(f"テストケース行を検出: {len(test_cases_map)}件")

# CSVからOKテストケースを抽出
ok_lines = []
with open("test_results2.csv", "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row.get("判定") == "OK" and row.get("No"):
            ok_lines.append(row)

print(f"OKテストケース: {len(ok_lines)}件")
print()
print("OKテストケース一覧:")
for row in ok_lines[:10]:
    print(f"  {row['No']}: {row['サイト名']} / {row['試験種別']}")
