#!/bin/bash
# PreToolUse hook: block writes to sensitive files
# CLAUDE_TOOL_INPUT contains JSON with file_path

FILE=$(echo "$CLAUDE_TOOL_INPUT" | node -e "
  let d=''; process.stdin.on('data',c=>d+=c);
  process.stdin.on('end',()=>{try{console.log(JSON.parse(d).file_path||'')}catch{console.log('')}})
" 2>/dev/null)

case "$FILE" in
  *.env|*.env.*|*credentials*|*token*|*secret*)
    echo '{"decision":"block","reason":"認証情報・シークレットファイルへの書き込みはブロックされました"}'
    ;;
  *)
    echo '{"decision":"approve"}'
    ;;
esac
