#!/bin/bash
# UserPromptSubmit rule router (2026-05-21 W: rule hybrid 化 / booster):
#   user prompt の keyword を解析し、.claude/rule-snippets/ から該当 rule の
#   抜粋を additionalContext として JIT 注入する.
#
# 設計位置づけ (Codex devil's advocate review NO-GO 経由で確定):
#   - 本 router は BOOSTER であり、PRIMARY DEFENDER ではない
#   - Critical 7 rule (silent-skip / sku / db-migration / cascade-update /
#     md-files-can-be-wrong / sqlite-timezone / karpathy) は .claude/rules/
#     に残り always-load 維持. 本 router の発火有無に依存しない
#   - Lower-risk 5 rule (wiki-frontmatter / contradiction-annotation /
#     discord-notification / supplier-matching-rules / llm-wiki-compilation)
#     は .claude/rule-snippets/ へ移動. 本 router が keyword 一致時に 1 行
#     index + 該当ファイル path を additionalContext に出力 → assistant が
#     必要なら Read する想定
#
# Codex 指摘事項の遵守:
#   - RCE 安全: user prompt は Python に渡し shell に interpolate しない
#   - multibyte: UTF-8 強制 + unicodedata NFKC casefold で日英混在 prompt 対応
#   - 10K char output cap: 出力は短い index のみ (full content は inject しない)
#   - 既存 clear-discipline.sh の Python JSON parse pattern を踏襲
#
# 入力 (stdin): {session_id, prompt, transcript_path, ...}
# 出力: 一致 keyword あり時 hookSpecificOutput.additionalContext, なし時 silent (exit 0)

set -u
export PYTHONIOENCODING=utf-8

INPUT=$(cat)

ROOT_DIR="${CLAUDE_PROJECT_DIR:-C:/Users/gucch/projects/claude}"
export ROOT_DIR
# heredoc は Python stdin を消費するため env var 経由で payload を渡す
# (shell interpolation を介さない = RCE 安全)
export PROMPT_PAYLOAD="$INPUT"

python <<'PYEOF'
import os, sys, json, unicodedata, re

# UTF-8 stdout/stderr (Windows Git Bash cp932 silent drop 防止)
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass

# ------ payload parse (env var 経由 / Codex 指摘の RCE 安全パターン) ------
raw = os.environ.get('PROMPT_PAYLOAD', '')
try:
    payload = json.loads(raw) if raw.strip() else {}
except Exception as e:
    print(f'[rule-router] payload parse failed: {type(e).__name__}: {e}', file=sys.stderr)
    sys.exit(0)

prompt = payload.get('prompt', '') or ''
if not isinstance(prompt, str) or not prompt.strip():
    sys.exit(0)

# ------ normalize prompt (NFKC + casefold = 日英 case 揺れ吸収) ------
try:
    normalized = unicodedata.normalize('NFKC', prompt).casefold()
except Exception:
    normalized = prompt.lower()

# ------ keyword → rule snippet マップ ------
# 各 rule の trigger 設計方針:
#   - prompt 段階で確実に出る keyword のみ拾う (false positive 抑制)
#   - critical rule (silent-skip / sku / db-migration / cascade-update /
#     md-files-can-be-wrong / sqlite-timezone) は always-load 済なので
#     ここで再注入しない (重複 = context 浪費)
#   - lower-risk 5 snippet のみ booster 注入対象
ROUTER_MAP = [
    # (rule_path, [keyword 候補... ], one-line hint)
    (
        '.claude/rule-snippets/discord-notification.md',
        ['discord', 'webhook', 'ebay manager', '通知', 'notif', 'alert',
         '#bot通知', 'channel_id', 'visual verify'],
        'Discord 通知系: webhook URL / channel_id / R-11 user 実視認 verify 必須.',
    ),
    (
        '.claude/rule-snippets/supplier-matching-rules.md',
        ['supplier', '仕入先', '仕入', 'match_score', 'alt_listing',
         'junk_likely_untested', 'ジャンク', '候補'],
        '仕入先候補: match_score<60 除外 / 別 SKU 機会 / ジャンク 2 種類判別.',
    ),
    (
        '.claude/rule-snippets/wiki-frontmatter.md',
        ['frontmatter', 'wiki_type', 'layer:', 'updated:', 'sources:',
         'wiki-frontmatter', 'metadata.wiki_type'],
        'memory / KB frontmatter: layer / updated / sources / metadata.wiki_type 規約.',
    ),
    (
        '.claude/rule-snippets/contradiction-annotation.md',
        ['両論併記', 'contradiction', 'supersede', '現状の見解', '過去の見解',
         'contradiction-annotation', '矛盾'],
        '両論併記: 現状 / 過去 / 変更理由 の 3 block 書式.',
    ),
    (
        '.claude/rule-snippets/llm-wiki-compilation.md',
        ['read-first', 'q7', 'compiled wiki', 'ingest', 'oplog',
         'llm-wiki', 'wiki compilation', 'query-save'],
        'Q7 LLM Wiki Compilation: read-first 規律 / INGEST 再構成 / QUERY-save 3 軸.',
    ),
    (
        '.claude/rule-snippets/browser-ui-native-input.md',
        ['ebaymag', 'playwright', 'cdp', 'ブラウザ操作', 'browser', '自動操作',
         'connect_over_cdp', 'locator', 'graphql', 'upsertprofile'],
        'ブラウザ UI は native locator method 第一選択 (合成クリックは controlled component 無効) / eBaymag 各国タブ input name 構造 / 負の能力主張ゲート技術編.',
    ),
]

# ------ keyword matching ------
hits = []
for path, keywords, hint in ROUTER_MAP:
    for kw in keywords:
        kw_normalized = unicodedata.normalize('NFKC', kw).casefold()
        if kw_normalized in normalized:
            hits.append((path, hint, kw))
            break  # 同 rule 内で複数 hit しても 1 件のみ

# ------ no match → silent exit (PreToolUse 等の path と異なり、本 hook は
#        毎発話で発火するため、unrelated prompt で context 注入は害) ------
if not hits:
    sys.exit(0)

# ------ additionalContext 構築 (10K char cap 厳守、index のみ inject) ------
lines = ['[rule-router] keyword 一致による on-demand snippet hint:']
for path, hint, matched_kw in hits:
    lines.append(f'  - {path}: {hint}')
    lines.append(f'    (matched keyword: "{matched_kw}". Read this file if rule detail needed.)')

context = '\n'.join(lines)

# 防御的に 9000 char で truncate (10K cap manifest)
MAX_CHARS = 9000
if len(context) > MAX_CHARS:
    context = context[:MAX_CHARS] + '\n\n[... truncated, Read snippet files directly ...]'

print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'UserPromptSubmit',
        'additionalContext': context,
    }
}, ensure_ascii=False))
PYEOF

exit 0
